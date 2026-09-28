const fields = {
  "moisture": ["moisture_percent", 1], "soil-temp": ["soil_temperature_c", 1],
  "conductivity": ["conductivity_us_cm", 0], "air-temp": ["air_temperature_c", 1],
  "humidity": ["air_humidity_percent", 1]
};
const chemistryValueIds = {
  soil_ph: "ph", nitrogen_mg_kg: "nitrogen",
  phosphorus_mg_kg: "phosphorus", potassium_mg_kg: "potassium"
};
const metricPrecision = {
  soil_ph: 2,
  air_temperature_c: 1,
  soil_temperature_c: 1,
  air_humidity_percent: 1
};
const sensorOfflineAfterMs = 10_000;

/* Lazy page updates.
 *
 * The page refreshes every second and most of it is the same as a second ago.
 * Every write goes through one of these helpers, which leave an element alone
 * when it already shows the value, and every list or chart is rebuilt only when
 * what it is drawn from has changed. Nothing flickers, nothing loses a hover or
 * a text selection, and an animation is not restarted by an unchanged value. */
function setText(element, text) {
  const value = text == null ? "" : String(text);
  if (element.textContent !== value) element.textContent = value;
}

// By attribute rather than the hidden property, which SVG elements lack.
function setHidden(element, hidden) {
  if (element.hasAttribute("hidden") !== Boolean(hidden)) element.toggleAttribute("hidden", Boolean(hidden));
}

function setData(element, key, value) {
  const text = String(value);
  if (element.dataset[key] !== text) element.dataset[key] = text;
}

function setAttr(element, name, value) {
  const text = String(value);
  if (element.getAttribute(name) !== text) element.setAttribute(name, text);
}

function setDisabled(element, disabled) {
  if (element.disabled !== Boolean(disabled)) element.disabled = Boolean(disabled);
}

function setStyle(element, property, value) {
  if (element.style.getPropertyValue(property) !== value) element.style.setProperty(property, value);
}

function setClass(element, name, on) {
  if (element.classList.contains(name) !== Boolean(on)) element.classList.toggle(name, Boolean(on));
}

// True, and remembered, when an element's source differs from what it was last
// built from; false when rebuilding it would draw the same thing again.
function renderKeyChanged(element, source) {
  const key = JSON.stringify(source);
  if (element.dataset.renderKey === key) return false;
  element.dataset.renderKey = key;
  return true;
}
const historyRanges = {
  "live": { label: "Live (up to 24 hours)", milliseconds: 24 * 60 * 60 * 1000, fitSamples: true },
  "24h": { label: "Last 24 hours", milliseconds: 24 * 60 * 60 * 1000 },
  "2d": { label: "Last 2 days", milliseconds: 2 * 24 * 60 * 60 * 1000 },
  "7d": { label: "Last 7 days", milliseconds: 7 * 24 * 60 * 60 * 1000 },
  "30d": { label: "Last 30 days", milliseconds: 30 * 24 * 60 * 60 * 1000 }
};
let profiles = null;
let latestReading = null;
// The hub's pore-water estimate that came with the latest reading: current,
// the last valid one while the soil is too dry, or none.
let latestChemistry = null;
// When the latest reading was taken, or when it was received if the sensor did
// not know the time. Ranges and calendars need a time either way; the reading
// itself keeps saying the time was unknown.
let latestReadingAt = null;
let selectedHistoryRange = "live";
let historyRequestKey = null;
let drainageAssessments = [];
let renderedCareEventIds = new Set();
let careLogInitialized = false;
let wateringCalendarRequestKey = null;
let wateringIntervalSummary = null;
let selectedSensorId = null;
let selectedSensor = null;
let managingSensorId = null;
let fleetSensors = [];
let unclaimedSensors = [];
let renderedSettingsKey = null;
let fleetRequestSequence = 0;
let sensorSettingsDraftSensorId = null;
let sensorSettingsSubmitSequence = 0;
let householdNetwork = null;
let scannerHealth = null;
let onboardingFinishedAt = null;
let rooms = [];
let householdNetworkDraft = false;
let renderedSettingsSensorsKey = null;
let onboardingStep = "find";
let onboardingSensorId = null;
let onboardingDraft = false;
let onboardingResult = null;


function pageFromLocation() {
  const path = window.location.pathname;
  /* A sensor's configuration is its own page.
   *
   * Plant care and device administration are different jobs done at different
   * times, and the forms for the second were crowding the first. */
  if (path.startsWith("/sensors/") && path.endsWith("/settings")) return "config";
  if (path.startsWith("/sensors/")) return "detail";
  if (path === "/settings" || path.startsWith("/settings/")) return "settings";
  if (path === "/onboarding" || path.startsWith("/onboarding/")) return "onboarding";
  return "fleet";
}

function settingsTabFromLocation() {
  const path = window.location.pathname;
  if (path === "/settings/wifi") return "wifi";
  if (path === "/settings/rooms") return "rooms";
  if (path === "/settings/firmware") return "firmware";
  return "sensors";
}

function sensorIdFromLocation() {
  const match = window.location.pathname.match(/^\/sensors\/([^/]+)(?:\/settings)?$/);
  return match ? decodeURIComponent(match[1]) : null;
}

function sensorPath(sensorId, suffix = "") {
  return "/sensors/" + encodeURIComponent(sensorId) + suffix;
}

function sensorQuery() {
  return selectedSensorId ? `?${new URLSearchParams({ sensor_id: selectedSensorId })}` : "";
}

function renderPage() {
  const page = pageFromLocation();
  document.querySelectorAll("[data-page]").forEach((element) => {
    setHidden(element, element.dataset.page !== page);
  });
  setClass(document.getElementById("fleet-link"), "active", page === "fleet");
  setClass(document.getElementById("settings-link"), "active", page === "settings");
  if (selectedSensorId) {
    setAttr(document.getElementById("detail-config-link"), "href", sensorPath(selectedSensorId, "/settings"));
    setAttr(document.getElementById("config-back-link"), "href", sensorPath(selectedSensorId));
  }
  if (page === "config") {
    setText(document.getElementById("config-plant-name"), selectedSensor
      ? selectedSensor.display_name || selectedSensor.sensor_id
      : selectedSensorId || "Sensor");
    renderSensorFirmware();
  }
  if (page === "settings") renderSettingsTab();
  if (page === "onboarding") renderOnboarding();
  document.title = titleForPage(page);
}

function titleForPage(page) {
  if (page === "detail" && selectedSensor) {
    return `${selectedSensor.display_name || selectedSensor.sensor_id} · Open Plant Pulse`;
  }
  if (page === "config") {
    const name = selectedSensor?.display_name || selectedSensorId || "Sensor";
    return `${name} · Configuration · Open Plant Pulse`;
  }
  if (page === "settings") return "Settings · Open Plant Pulse";
  if (page === "onboarding") return "Add a sensor · Open Plant Pulse";
  return "Plants · Open Plant Pulse";
}

function navigate(path) {
  window.history.pushState({}, "", path);
  renderPage();
}

function sensorState(sensor) {
  const latest = sensor.latest?.reading;
  const profileId = sensor.profile_id || profiles?.default_profile;
  const profile = profiles?.profiles?.[profileId];
  const moistureLow = profile?.watering?.refill_below;
  const conductivityHigh = profile?.root_zone?.conductivity_us_cm?.ideal?.[1];
  // The profile's conductivity range is pore-water EC, so it is held against
  // the hub's estimate of that; the probe's bulk EC always reads lower.
  const poreWaterEc = sensor.latest?.chemistry?.pore_water_ec_us_cm;
  const alerts = [];
  if (moistureLow != null && latest?.moisture_percent < moistureLow) {
    alerts.push("moisture low");
  }
  if (conductivityHigh != null && poreWaterEc > conductivityHigh) {
    alerts.push("nutrients high");
  }
  // Not hearing a sensor and hearing one that measures nothing are different
  // faults with different fixes, so they must not share a word.
  if (sensor.freshness === "stale") return "Not reporting";
  if (sensor.measurements === "none") return "No measurements";
  if (sensor.measurements === "stale") return "Measurements stale";
  return alerts.length ? alerts.join(", ") : "Healthy";
}

function updateFleetCard(card, sensor) {
  setAttr(card, "href", `/sensors/${encodeURIComponent(sensor.sensor_id)}`);
  setText(card.children[0], sensor.display_name || sensor.sensor_id);
  setText(card.children[1], sensor.sensor_id);
  const reportedAt = sensor.latest?.received_at || sensor.last_seen_at;
  setText(card.children[2], reportedAt
    ? `Last report ${new Date(reportedAt).toLocaleString()}`
    : "No reports received");
  const state = sensorState(sensor);
  setText(card.children[3], state);
  setData(card.children[3], "state", state.toLowerCase());
}

function createFleetCard(sensor) {
  const card = document.createElement("a");
  card.className = "fleet-card";
  setData(card, "sensorId", sensor.sensor_id);
  card.innerHTML = `<strong></strong><code></code><span></span><small></small>`;
  updateFleetCard(card, sensor);
  return card;
}

function renderFleet() {
  setText(document.getElementById("inbox-count"), unclaimedSensors.length);
  const grid = document.getElementById("fleet-grid");
  const cardsBySensorId = new Map(
    Array.from(grid.querySelectorAll(".fleet-card"), (card) => [card.dataset.sensorId, card])
  );
  if (!fleetSensors.length) {
    for (const card of cardsBySensorId.values()) card.remove();
    if (!grid.querySelector(".empty-state")) {
      const empty = document.createElement("p");
      empty.className = "empty-state";
      setText(empty, "No sensors are enrolled. Open the sensor inbox to enroll a nearby sensor.");
      grid.append(empty);
    }
    return;
  }

  grid.querySelector(".empty-state")?.remove();
  const currentSensorIds = new Set(fleetSensors.map((sensor) => sensor.sensor_id));
  for (const [sensorId, card] of cardsBySensorId) {
    if (!currentSensorIds.has(sensorId)) card.remove();
  }
  fleetSensors.forEach((sensor, index) => {
    const card = cardsBySensorId.get(sensor.sensor_id) || createFleetCard(sensor);
    updateFleetCard(card, sensor);
    const cardAtIndex = grid.children[index];
    if (cardAtIndex !== card) grid.insertBefore(card, cardAtIndex || null);
  });
}

function renderInbox() {
  const list = document.getElementById("inbox-list");
  if (!renderKeyChanged(list, unclaimedSensors.map(sensor => [
    sensor.sensor_id, sensor.transport, sensor.latest_rssi, sensor.last_seen_at
  ]))) return;
  list.replaceChildren();
  if (!unclaimedSensors.length) {
    const empty = document.createElement("p");
    setText(empty, "No unclaimed sensors are nearby.");
    list.append(empty);
    return;
  }
  for (const sensor of unclaimedSensors) {
    const item = document.createElement("article");
    item.className = "inbox-item";
    const name = document.createElement("strong");
    setText(name, sensor.sensor_id);
    const details = document.createElement("small");
    setText(details, `${sensor.transport} · RSSI ${sensor.latest_rssi ?? "unknown"} · last seen ${new Date(sensor.last_seen_at).toLocaleString()}`);
    const enroll = document.createElement("button");
    enroll.type = "button";
    setText(enroll, "Enroll");
    enroll.addEventListener("click", () => openSettings(sensor));
    item.append(name, enroll, details);
    list.append(item);
  }
}

function formatReportingInterval(intervalSeconds) {
  if (intervalSeconds < 60) {
    return `${intervalSeconds} second${intervalSeconds === 1 ? "" : "s"}`;
  }
  const intervalMinutes = intervalSeconds / 60;
  if (intervalMinutes < 60) {
    return `${intervalMinutes} minute${intervalMinutes === 1 ? "" : "s"}`;
  }
  const intervalHours = intervalMinutes / 60;
  return `${intervalHours} hour${intervalHours === 1 ? "" : "s"}`;
}

function selectReportingInterval(select, intervalSeconds) {
  select.querySelectorAll("[data-current-interval]").forEach((option) => option.remove());
  const value = String(intervalSeconds);
  if (![...select.options].some((option) => option.value === value)) {
    const currentOption = new Option(`${formatReportingInterval(intervalSeconds)} (current)`, value);
    setData(currentOption, "currentInterval", "true");
    select.add(currentOption);
  }
  select.value = value;
}

/* Configuration is delivered rather than saved, so the number that matters is
 * the one the sensor has acknowledged. Flashing it when it moves is how somebody
 * watching sees that a change actually travelled, rather than inferring it from
 * a status line that reads the same either way. */
let renderedConfigRevision = null;

function renderConfigRevision(sensor) {
  const element = document.getElementById("settings-config-revision");
  const applied = sensor.device_config_applied_revision;
  const text =
    applied > 0 ? `revision ${applied}` : "no configuration delivered yet";
  if (element.textContent === text) return;
  setText(element, text);
  if (renderedConfigRevision !== null && applied !== renderedConfigRevision) {
    element.classList.remove("just-changed");
    // Reading offsetWidth restarts the animation when the value changes twice
    // in quick succession.
    void element.offsetWidth;
    element.classList.add("just-changed");
  }
  renderedConfigRevision = applied;
}

function renderSensorSettings() {
  if (!selectedSensor || !profiles) return;
  const preserveDraft = sensorSettingsDraftSensorId === selectedSensor.sensor_id;
  const renderKey = JSON.stringify([
    selectedSensor.sensor_id,
    selectedSensor.display_name,
    selectedSensor.room,
    selectedSensor.profile_id,
    selectedSensor.expected_interval_seconds,
    selectedSensor.sensor_reporting_interval_seconds,
    selectedSensor.device_config_status,
    selectedSensor.device_config_revision,
    selectedSensor.device_config_error,
    unclaimedSensors.map((sensor) => sensor.sensor_id)
  ]);
  if (renderedSettingsKey === renderKey) return;
  renderedSettingsKey = renderKey;
  setText(document.getElementById("settings-sensor-id"), selectedSensor.sensor_id);
  renderConfigRevision(selectedSensor);
  if (!preserveDraft) {
    document.getElementById("detail-setting-name").value = selectedSensor.display_name || "";
    renderRoomChoices();
    const detailRoom = document.getElementById("detail-setting-room");
    if (selectedSensor.room_id != null) detailRoom.value = String(selectedSensor.room_id);
    document.getElementById("detail-setting-profile").value = selectedSensor.profile_id || profiles.default_profile;
    selectReportingInterval(
      document.getElementById("detail-setting-reporting-interval"),
      selectedSensor.expected_interval_seconds
    );
    setText(document.getElementById("sensor-settings-message"), "");
  }
  // The console switch belongs with the sensor's other settings as well as on
  // the Settings list, because this is the page somebody is on when they want
  // to reach the sensor's own diagnostics.
  const consoleToggle = document.getElementById("detail-console-toggle");
  const consoleState = document.getElementById("detail-console-state");
  consoleToggle.checked = Boolean(selectedSensor.wifi_enabled);
  setData(consoleToggle, "sensorId", selectedSensor.sensor_id);
  setDisabled(consoleToggle, !householdNetwork || !householdNetwork.wifi_ssid);
  setText(consoleState, consoleToggle.disabled
    ? "No household network saved yet. Add one in Settings to switch this on."
    : describeWifi(selectedSensor));
  // Only offered once the sensor has said it reached the network and given its
  // address. Before that there is nothing at the other end of the link.
  const consoleLink = document.getElementById("detail-console-link");
  const reachable =
    selectedSensor.wifi_enabled &&
    selectedSensor.wifi_state === "joined" &&
    selectedSensor.wifi_address;
  setHidden(consoleLink, !reachable);
  if (reachable) {
    setAttr(consoleLink, "href", "http://" + selectedSensor.wifi_address + "/");
    setText(consoleLink, "Open this sensor's console at " + selectedSensor.wifi_address);
  }
  const sensorInterval = selectedSensor.sensor_reporting_interval_seconds;
  document.getElementById("sensor-reporting-interval").value = sensorInterval == null
    ? "Not synced yet"
    : formatReportingInterval(sensorInterval);
  const delivery = document.getElementById("device-config-status");
  if (selectedSensor.device_config_status === "applied") {
    setText(delivery, `Applied on sensor · revision ${selectedSensor.device_config_revision}`);
  } else if (selectedSensor.device_config_status === "retrying") {
    setText(delivery, `Delivery will retry on the next report · ${selectedSensor.device_config_error}`);
  } else {
    setText(delivery, "Waiting to send when the sensor next reports.");
  }
}

/* The debug log stays as it was left.
 *
 * It sits last on the page and is closed until somebody wants it, but watching
 * reports arrive means reloading this page repeatedly, and reopening the same
 * section every time is a chore. Browser storage can be missing or refuse the
 * write, which costs only the memory of the last state, so it is never trusted
 * for anything the page needs to work. */
const RAW_REPORTS_OPEN_KEY = "open-plant-pulse.raw-reports-open";

function trackRawReportsDisclosure() {
  const log = document.getElementById("raw-report-log");
  try {
    log.open = window.localStorage.getItem(RAW_REPORTS_OPEN_KEY) === "true";
  } catch (_error) {
    log.open = false;
  }
  log.addEventListener("toggle", () => {
    try {
      window.localStorage.setItem(RAW_REPORTS_OPEN_KEY, String(log.open));
    } catch (_error) {
      /* Nothing to do: the section still opens and closes. */
    }
  });
}

const PACKET_KIND_LABELS = { packet1: "packet 1", packet2: "packet 2" };

function rawReportLabel(item) {
  if (item.packet_kind === "beacon") return "beacon";
  if (item.report_id == null) return "—";
  if (!item.packet_kind) return String(item.report_id);
  return `${item.report_id} · ${PACKET_KIND_LABELS[item.packet_kind] || item.packet_kind}`;
}

function renderRawReports(items) {
  const rows = document.getElementById("raw-report-list");
  if (!renderKeyChanged(rows, items)) return;
  if (!items.length) {
    const cell = Object.assign(document.createElement("td"), {
      className: "raw-report-empty",
      colSpan: 6,
      textContent: "No raw BLE reports stored yet."
    });
    const row = document.createElement("tr");
    row.append(cell);
    rows.replaceChildren(row);
    return;
  }
  rows.replaceChildren(...items.map((item) => {
    const row = document.createElement("tr");
    const status = Object.assign(document.createElement("span"), {
      className: "raw-report-status",
      textContent: item.decode_status,
      title: item.decode_error || ""
    });
    setData(status, "status", item.decode_status);
    const payload = Object.assign(document.createElement("code"), {
      textContent: item.service_data_hex || "unavailable",
      title: `SHA-256 ${item.payload_sha256}`
    });
    const values = [
      new Date(item.received_at).toLocaleString(),
      rawReportLabel(item),
      status,
      item.rssi == null ? "—" : `${item.rssi} dBm`,
      [item.source_adapter, item.observed_identifier].filter(Boolean).join(" · "),
      payload
    ];
    values.forEach((value) => {
      const cell = document.createElement("td");
      if (value instanceof Node) cell.append(value);
      else cell.textContent = value;
      row.append(cell);
    });
    return row;
  }));
}

function renderHubHealth(health) {
  const scanner = health.scanner || { status: "unknown" };
  scannerHealth = scanner;
  renderScanState();
  const lines = [
    `Hub started ${new Date(health.started_at).toLocaleString()}`,
    `Database: ${health.database.status}`,
    `BLE scanner: ${scanner.status}`,
    `Last BLE receive: ${scanner.last_receive_at ? new Date(scanner.last_receive_at).toLocaleString() : "none"}`
  ];
  const panel = document.getElementById("hub-health");
  if (!renderKeyChanged(panel, lines)) return;
  panel.replaceChildren(...lines.map(line => Object.assign(document.createElement("span"), { textContent: line })));
}

async function refreshFleet() {
  const requestSequence = ++fleetRequestSequence;
  const [fleetResponse, inboxResponse, healthResponse] = await Promise.all([
    fetch("/api/sensors?status=enrolled"),
    fetch("/api/sensors?status=unclaimed"),
    fetch("/api/health")
  ]);
  if (!fleetResponse.ok || !inboxResponse.ok || !healthResponse.ok) {
    throw new Error("fleet request failed");
  }
  const [fleetPayload, inboxPayload, healthPayload] = await Promise.all([
    fleetResponse.json(),
    inboxResponse.json(),
    healthResponse.json()
  ]);
  if (requestSequence !== fleetRequestSequence) return;
  fleetSensors = fleetPayload.items;
  unclaimedSensors = inboxPayload.items;
  renderHubHealth(healthPayload);
  selectedSensorId = sensorIdFromLocation();
  selectedSensor = fleetSensors.find((sensor) => sensor.sensor_id === selectedSensorId) || null;
  if (selectedSensor) {
    plantLabel.value = selectedSensor.display_name || selectedSensor.sensor_id;
    if (profiles?.profiles[selectedSensor.profile_id]) {
      const profile = profiles.profiles[selectedSensor.profile_id];
      setText(document.getElementById("plant-profile-name"), profile.name);
      setText(document.getElementById("scientific-name"), profile.scientific_name);
    }
  }
  renderFleet();
  renderInbox();
  renderSensorSettings();
  renderSettingsSensors();
  renderHouseholdNetwork();
  renderRoomChoices();
  renderPage();
}

async function selectSensor(sensorId) {
  sensorSettingsDraftSensorId = null;
  selectedSensorId = sensorId;
  selectedSensor = fleetSensors.find((sensor) => sensor.sensor_id === sensorId) || null;
  history.pushState({}, "", `/sensors/${encodeURIComponent(sensorId)}`);
  latestReading = null;
  latestReadingAt = null;
  historyRequestKey = null;
  moistureTrendItems = [];
  moistureTrendSensorId = null;
  wateringCalendarRequestKey = null;
  renderedCareEventIds = new Set();
  careLogInitialized = false;
  renderPage();
  await refresh();
}

function openSettings(sensor) {
  managingSensorId = sensor.sensor_id;
  document.getElementById("setting-name").value = sensor.display_name || "";
  document.getElementById("setting-room").value = sensor.room || "";
  document.getElementById("setting-profile").value = sensor.profile_id || profiles.default_profile;
  selectReportingInterval(
    document.getElementById("setting-reporting-interval"),
    sensor.expected_interval_seconds
  );
  setText(document.getElementById("settings-message"), "");
  if (document.getElementById("inbox-dialog").open) document.getElementById("inbox-dialog").close();
  document.getElementById("settings-dialog").showModal();
}

async function saveManagedSensor(event) {
  event.preventDefault();
  const response = await fetch(`/api/sensors/${encodeURIComponent(managingSensorId)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      display_name: document.getElementById("setting-name").value,
      room: document.getElementById("setting-room").value,
      profile_id: document.getElementById("setting-profile").value,
      expected_interval_seconds: Number(document.getElementById("setting-reporting-interval").value)
    })
  });
  const payload = await response.json();
  if (!response.ok) {
    setText(document.getElementById("settings-message"), payload.error || "Could not save sensor");
    return;
  }
  document.getElementById("settings-dialog").close();
  selectedSensorId = payload.sensor_id;
  await refreshFleet();
  await selectSensor(payload.sensor_id);
}

function formatValue(value, metricName) {
  return Number(value).toFixed(metricPrecision[metricName] || 0);
}

function formatDifference(value, metricName) {
  const precision = metricPrecision[metricName] || 0;
  const resolution = 10 ** -precision;
  if (value > 0 && value < resolution / 2) return `<${resolution.toFixed(precision)}`;
  return Number(value).toFixed(precision);
}

function assessMetric(metricName, metric, value) {
  const numericValue = Number(value);
  if (value == null || !Number.isFinite(numericValue)) return null;

  const [idealLow, idealHigh] = metric.ideal;
  const [scaleLow, scaleHigh] = metric.scale;
  let distance = 0;
  let direction = "inside target";
  if (numericValue < idealLow) {
    distance = idealLow - numericValue;
    direction = "below target";
  } else if (numericValue > idealHigh) {
    distance = numericValue - idealHigh;
    direction = "above target";
  }
  return {
    metricName,
    label: metric.label,
    unit: metric.unit,
    distance,
    direction,
    deviation: distance / (scaleHigh - scaleLow)
  };
}

function assessWatering(watering, value) {
  const numericValue = Number(value);
  if (value == null || !Number.isFinite(numericValue)) return null;
  const [, cycleHigh] = watering.comfortable_cycle;

  if (numericValue <= watering.concern_below) {
    return { label: "Soil moisture", deviation: 0.31, reason: `Soil moisture is critically dry at ${numericValue.toFixed(1)}%.` };
  }
  if (numericValue < watering.refill_below) {
    return { label: "Soil moisture", deviation: 0.15, reason: `Soil moisture is ready for water at ${numericValue.toFixed(1)}%.` };
  }
  if (numericValue >= watering.concern_above) {
    return { label: "Soil moisture", deviation: 0.31, reason: `Soil moisture is above the safe cycle at ${numericValue.toFixed(1)}%.` };
  }
  if (numericValue > cycleHigh) {
    return { label: "Soil moisture", deviation: 0.15, reason: `Soil moisture is wetter than its usual cycle at ${numericValue.toFixed(1)}%.` };
  }
  return { label: "Soil moisture", deviation: 0 };
}

function wateringPhase(watering, value) {
  if (value <= watering.concern_below) return "Critically dry";
  if (value < watering.refill_below) return "Ready for water";
  if (value >= watering.concern_above) return "Too wet";
  if (value > watering.comfortable_cycle[1]) return "Allow to drain";
  if (value >= watering.post_water_target[0]) return "Comfortably watered";
  return "Drying normally";
}

function renderMoistureVessel(profile) {
  const value = Number(latestReading.moisture_percent);
  if (!Number.isFinite(value)) return;
  const watering = profile.watering;
  const vessel = document.getElementById("moisture-vessel");
  const [targetLow, targetHigh] = watering.post_water_target;
  const [cycleLow, cycleHigh] = watering.comfortable_cycle;
  const clamp = number => Math.max(0, Math.min(100, number));

  setStyle(vessel, "--fill", `${clamp(value)}%`);
  setStyle(vessel, "--target-low", `${clamp(targetLow)}%`);
  setStyle(vessel, "--target-size", `${clamp(targetHigh) - clamp(targetLow)}%`);
  setStyle(vessel, "--cycle-low", `${clamp(cycleLow)}%`);
  setStyle(vessel, "--cycle-size", `${clamp(cycleHigh) - clamp(cycleLow)}%`);
  setStyle(vessel, "--refill", `${clamp(watering.refill_below)}%`);
  setAttr(vessel, "aria-label", `Soil moisture ${value.toFixed(1)} percent. ${wateringPhase(watering, value)}.`);
  setData(vessel, "phase", wateringPhase(watering, value).toLowerCase().replaceAll(" ", "-"));
  setText(document.getElementById("moisture-cycle-label"), `Comfortable ${cycleLow}–${cycleHigh}%`);
  setText(document.getElementById("moisture-target-label"), `After watering ${targetLow}–${targetHigh}%`);
  setText(document.getElementById("moisture-refill-label"), `Water below ${watering.refill_below}%`);
  setText(document.getElementById("moisture-phase"), wateringPhase(watering, value));
  setText(document.getElementById("moisture-strategy"), watering.label);
}

// The value a profile metric is judged on. Conductivity targets describe the
// soil water, so they are held against the pore-water estimate, never the
// probe's bulk EC. pH and N/P/K are not judged at all: the probe derives N/P/K
// from EC, and its pH is unverified.
function assessedValue(metricName) {
  if (metricName === "conductivity_us_cm") return latestChemistry?.pore_water_ec_us_cm ?? null;
  return latestReading[metricName];
}

function renderPlantAssessment(profile) {
  const metrics = ["root_zone", "climate"]
    .flatMap(section => Object.entries(profile[section] || {}))
    .filter(([metricName]) => metricName !== "moisture_percent");
  const assessments = metrics
    .map(([metricName, metric]) => assessMetric(metricName, metric, assessedValue(metricName)))
    .filter(Boolean);
  const moistureAssessment = assessWatering(profile.watering, latestReading.moisture_percent);
  if (moistureAssessment) assessments.push(moistureAssessment);
  const metricCount = metrics.length + 1;
  const panel = document.getElementById("plant-assessment");
  const status = document.getElementById("plant-status");
  const reason = document.getElementById("plant-status-reason");

  if (!assessments.length) {
    setData(panel, "level", "waiting");
    setText(status, "Unknown");
    setText(reason, "No profiled measurements are available yet.");
    return;
  }

  const worst = assessments.reduce((current, item) => item.deviation > current.deviation ? item : current);
  const level = profiles.assessment_policy.levels.find(
    item => item.max_deviation == null || worst.deviation <= item.max_deviation
  );
  setData(panel, "level", level.status.toLowerCase());
  setText(status, level.status);

  const coverage = `${assessments.length} of ${metricCount} readings assessed`;
  if (worst.deviation === 0) {
    setText(reason, `All ${assessments.length} readings are within ${profile.name} healthy limits.`);
  } else if (worst.reason) {
    setText(reason, `${worst.reason} ${coverage}.`);
  } else {
    const suffix = worst.unit ? ` ${worst.unit}` : "";
    setText(reason, `${worst.label} is ${formatDifference(worst.distance, worst.metricName)}${suffix} ${worst.direction}. ${coverage}.`);
  }
}

function renderGauge(card, metricName, metric, value) {
  const [scaleLow, scaleHigh] = metric.scale;
  const [idealLow, idealHigh] = metric.ideal;
  const percentage = number => (number - scaleLow) / (scaleHigh - scaleLow) * 100;
  setStyle(card.querySelector(".ideal-range"), "left", `${percentage(idealLow)}%`);
  setStyle(card.querySelector(".ideal-range"), "width", `${percentage(idealHigh) - percentage(idealLow)}%`);
  setText(card.querySelector(".scale-low"), scaleLow);
  setText(card.querySelector(".scale-high"), scaleHigh);
  setText(card.querySelector(".ideal-label"), `Ideal ${idealLow}–${idealHigh}`);

  const status = card.querySelector(".range-status");
  const valueMarker = card.querySelector(".value-marker");
  // A value the sensor did not send is not a value of zero, which is where a
  // missing number would otherwise put the marker.
  setHidden(valueMarker, value == null);
  const inRange = value != null && value >= idealLow && value <= idealHigh;
  setClass(status, "in-range", inRange);
  setClass(status, "out-range", value != null && !inRange);
  if (value == null) {
    setText(status, "Not in the latest report");
    return;
  }
  setStyle(valueMarker, "left", `${Math.max(0, Math.min(100, percentage(value)))}%`);
  const suffix = metric.unit ? ` ${metric.unit}` : "";
  if (value < idealLow) {
    setText(status, `${formatDifference(idealLow - value, metricName)}${suffix} below ideal`);
  } else if (value > idealHigh) {
    setText(status, `${formatDifference(value - idealHigh, metricName)}${suffix} above ideal`);
  } else {
    const margin = Math.min(value - idealLow, idealHigh - value);
    setText(status, `Inside ideal zone · ${formatValue(margin, metricName)}${suffix} to nearest edge`);
  }
}

function renderProfileRanges() {
  if (!profiles || !latestReading) return;
  const profileId = selectedSensor?.profile_id || profiles.default_profile;
  const profile = profiles.profiles[profileId];
  setText(document.getElementById("scientific-name"), profile.scientific_name);
  setText(document.getElementById("chemistry-profile"), `${profile.name} starter targets`);
  renderMoistureVessel(profile);
  renderMoistureTrend(profile);
  renderPlantAssessment(profile);
  renderDrainageAssessment(profile);

  for (const card of document.querySelectorAll(".root-zone-gauge")) {
    const metricName = card.dataset.metric;
    renderGauge(card, metricName, profile.root_zone[metricName], latestReading[metricName]);
  }
  for (const card of document.querySelectorAll(".climate-gauge")) {
    const metricName = card.dataset.metric;
    renderGauge(card, metricName, profile.climate[metricName], latestReading[metricName]);
  }
  // The probe's pH and N/P/K are shown as it reports them, with no target
  // band: they are labelled in the page as unverified and trend-only.
  for (const [metricName, id] of Object.entries(chemistryValueIds)) {
    const value = latestReading[metricName];
    setText(document.getElementById(id), value == null ? "--" : formatValue(value, metricName));
  }
  renderPoreWaterEc(profile);
}

function formatAge(seconds) {
  if (seconds == null) return "";
  if (seconds < 120) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} minutes ago`;
  const hours = Math.round(seconds / 3600);
  if (hours < 48) return `${hours} hour${hours === 1 ? "" : "s"} ago`;
  return `${Math.round(seconds / 86400)} days ago`;
}

/**
 * Nutrient level and the pore-water EC it comes from. The estimate is the
 * latest reading's when it has one; while the soil is too dry the hub sends the
 * last valid one instead, which is shown with what it was worked out from and
 * how old it is, so it is never mistaken for a live value.
 */
function renderPoreWaterEc(profile) {
  const card = document.getElementById("pore-ec-card");
  const metric = profile.root_zone.conductivity_us_cm;
  const chemistry = latestChemistry;
  const status = chemistry?.status || "none";
  const estimate = status === "none" ? null : chemistry.pore_water_ec_us_cm;
  setData(card, "status", status);
  setData(card, "level", chemistry?.nutrient_level || "none");
  renderGauge(card, "conductivity_us_cm", metric, estimate);

  const level = { low: "Low", ok: "OK", high: "High" }[chemistry?.nutrient_level];
  setText(document.getElementById("nutrient-level"), level || "--");
  setText(document.getElementById("nutrient-level-basis"),
    status === "last_valid" ? "· last estimate" : "");
  setText(document.getElementById("pore-ec"), estimate == null ? "--" : String(estimate));
  setText(document.getElementById("pore-ec-target"), `(target ${metric.ideal[0]}–${metric.ideal[1]})`);

  const basisText = document.getElementById("pore-ec-basis");
  const dryNote = document.getElementById("pore-ec-dry-note");
  if (status === "none") {
    setText(card.querySelector(".range-status"), "No recent estimate — water the plant to get one");
    setText(basisText, "");
  } else {
    const basis = chemistry.basis;
    const afterWatering = basis.post_watering ? " after watering" : "";
    setText(basisText,
      `from EC ${basis.conductivity_us_cm} µS/cm at ${Math.round(basis.moisture_percent)} % moisture, ` +
      `${Number(basis.soil_temperature_c).toFixed(1)} °C${afterWatering} · ${formatAge(chemistry.age_seconds)}`);
  }
  const moisture = latestReading.moisture_percent;
  setHidden(dryNote, !chemistry?.too_dry);
  setText(dryNote, chemistry?.too_dry
    ? `Now too dry to estimate (moisture ${moisture == null ? "--" : Math.round(moisture)} %) — updates after the next watering`
    : "");
}

function drainageResponseClass(retainedFraction) {
  if (retainedFraction < 0.45) return "fast";
  if (retainedFraction <= 0.8) return "balanced";
  return "retaining";
}

function renderDrainageAssessment(profile) {
  const panel = document.getElementById("drainage-assessment");
  const status = document.getElementById("drainage-status");
  const reason = document.getElementById("drainage-status-reason");
  const interval = document.getElementById("watering-interval");
  const sensorEvents = drainageAssessments;
  const requiredCycles = 3;

  if (wateringIntervalSummary?.typical_days != null) {
    const days = wateringIntervalSummary.typical_days;
    const formattedDays = Number.isInteger(days) ? days.toFixed(0) : days.toFixed(1);
    setText(interval, `Typical dry-down cycle: ${formattedDays} days`);
  } else {
    setText(interval, "Two watering dates are needed to learn the usual interval");
  }

  if (sensorEvents.length < requiredCycles) {
    setData(panel, "level", "learning");
    setText(status, "Learning");
    setText(reason, `${sensorEvents.length} of ${requiredCycles} watering cycles learned. ${profile.drainage.label}.`);
    return;
  }

  const observations = sensorEvents.slice(0, requiredCycles);
  const retainedFraction = observations.reduce(
    (total, event) => total + Number(event.changes.retained_fraction), 0
  ) / observations.length;
  const settleMinutes = Math.max(...observations.map(event => Number(event.changes.settle_minutes)));
  const responseClass = drainageResponseClass(retainedFraction);
  const classMatches = profile.drainage.acceptable_responses.includes(responseClass);
  const settlesInTime = settleMinutes <= profile.drainage.maximum_settle_minutes;
  const responseLabel = responseClass === "retaining" ? "water-retaining" : `${responseClass}-draining`;

  if (classMatches && settlesInTime) {
    setData(panel, "level", "suitable");
    setText(status, "Suitable");
    setText(reason, `This pot appears suitable for ${profile.name}. It is consistently ${responseLabel} and settles within ${settleMinutes.toFixed(0)} minutes.`);
  } else {
    setData(panel, "level", "unsuitable");
    setText(status, "Possibly unsuitable");
    const issue = classMatches
      ? `takes up to ${settleMinutes.toFixed(0)} minutes to settle`
      : `behaves as ${responseLabel}`;
    setText(reason, `The pot ${issue}; ${profile.name} prefers ${profile.drainage.label.toLowerCase()}. Check substrate, drainage holes, and probe placement.`);
  }
}

/**
 * A phone-style battery: an outline with a terminal nub, filled in proportion
 * to the charge, then the percentage and, when reported, the voltage. The fill
 * turns amber at 20 % and red at 10 %; an unknown level is an empty outline.
 * While charging, a bolt sits over the middle, as on a phone. Not charging and
 * not said look the same: no bolt.
 */
function renderBattery(element, reading) {
  const percent = reading.battery_percent;
  const known = percent != null;
  const charging = known && reading.battery_charging === true;
  const voltage = known && reading.battery_voltage_v != null
    ? `${Number(reading.battery_voltage_v).toFixed(2)} V`
    : null;
  setData(element, "level", !known ? "unknown" : percent <= 10 ? "critical" : percent <= 20 ? "low" : "normal");
  setStyle(element, "--charge", known ? String(Math.min(100, Math.max(0, percent))) : "0");
  const details = [`${percent}%`, charging ? "charging" : null, voltage].filter(Boolean);
  const label = known ? `Battery ${details.join(", ")}` : "Battery not reported";
  setAttr(element, "aria-label", label);
  if (element.title !== label) element.title = label;
  if (!renderKeyChanged(element, [known, percent, charging, voltage])) return;

  const glyph = document.createElement("span");
  glyph.className = "battery-glyph";
  setAttr(glyph, "aria-hidden", "true");
  const fill = document.createElement("span");
  fill.className = "battery-fill";
  glyph.append(fill);
  if (charging) {
    const bolt = document.createElement("span");
    bolt.className = "battery-bolt";
    glyph.append(bolt);
  }
  const text = document.createElement("span");
  text.className = "battery-percent";
  setText(text, known ? `${percent}%` : "\u2014");
  element.replaceChildren(glyph, text);
  if (voltage) {
    const secondary = document.createElement("span");
    secondary.className = "battery-voltage";
    setText(secondary, voltage);
    element.append(secondary);
  }
}

function renderReading(payload) {
  const reading = payload.reading;
  latestReading = reading;
  latestChemistry = payload.chemistry || null;
  for (const [id, [key, precision]] of Object.entries(fields)) {
    const value = reading[key];
    setText(document.getElementById(id), value == null ? "--" : Number(value).toFixed(precision));
  }
  const receivedAt = Date.parse(payload.received_at);
  const staleAfterMs = selectedSensor
    ? (selectedSensor.sensor_reporting_interval_seconds
      ?? selectedSensor.expected_interval_seconds) * 2_000
    : sensorOfflineAfterMs;
  const isLive = Number.isFinite(receivedAt) && Date.now() - receivedAt <= staleAfterMs;
  setText(document.getElementById("status"), isLive ? "Sensor fresh" : "Sensor stale");
  setClass(document.getElementById("status-dot"), "live", isLive);
  latestReadingAt = reading.observed_at || payload.received_at;
  const takenAt = reading.observed_at
    ? new Date(reading.observed_at).toLocaleTimeString()
    : `time unknown, received ${new Date(payload.received_at).toLocaleTimeString()}`;
  const report = reading.report_id == null ? "Sample" : `Report ${reading.report_id}`;
  setText(document.getElementById("updated"), `${report} · ${takenAt}`);
  renderBattery(document.getElementById("battery"), reading);
  renderProfileRanges();
}

function svgElement(name, attributes, text = null) {
  const element = document.createElementNS("http://www.w3.org/2000/svg", name);
  for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, value);
  if (text != null) element.textContent = text;
  return element;
}

function historyTickLabel(timestamp) {
  const options = selectedHistoryRange === "live" || selectedHistoryRange === "24h"
    ? { hour: "numeric", minute: "2-digit" }
    : { month: "short", day: "numeric" };
  return new Intl.DateTimeFormat(undefined, options).format(new Date(timestamp));
}

function renderHistoryChart(chartId, emptyId, items, metric, unit, startTime, endTime) {
  const chart = document.getElementById(chartId);
  const empty = document.getElementById(emptyId);
  const points = items.map((item) => ({
    timestamp: Date.parse(item.reading.observed_at || item.received_at),
    value: item.reading[metric] == null ? null : Number(item.reading[metric])
  })).filter((point) => Number.isFinite(point.timestamp) && Number.isFinite(point.value));
  if (!renderKeyChanged(chart, [points, unit, startTime, endTime, selectedHistoryRange])) return;
  chart.replaceChildren();
  setHidden(chart, points.length === 0);
  setHidden(empty, points.length !== 0);
  if (!points.length) return;

  const width = 720;
  const height = 220;
  const plot = { left: 52, right: 16, top: 16, bottom: 34 };
  const plotWidth = width - plot.left - plot.right;
  const plotHeight = height - plot.top - plot.bottom;
  const values = points.map((point) => point.value);
  const observedMinimum = Math.min(...values);
  const observedMaximum = Math.max(...values);
  let minimum = observedMinimum;
  let maximum = observedMaximum;
  const padding = Math.max((maximum - minimum) * 0.12, metric === "air_temperature_c" ? 0.5 : 2);
  minimum -= padding;
  maximum += padding;
  if (metric === "air_humidity_percent") {
    minimum = Math.max(0, minimum);
    maximum = Math.min(100, maximum);
  }
  const x = timestamp => plot.left + ((timestamp - startTime) / (endTime - startTime)) * plotWidth;
  const y = value => plot.top + ((maximum - value) / (maximum - minimum)) * plotHeight;

  for (let index = 0; index <= 4; index += 1) {
    const yPosition = plot.top + (plotHeight * index) / 4;
    const value = maximum - ((maximum - minimum) * index) / 4;
    chart.append(
      svgElement("line", { class: "grid-line", x1: plot.left, x2: width - plot.right, y1: yPosition, y2: yPosition }),
      svgElement("text", { class: "axis-label", x: plot.left - 8, y: yPosition + 4, "text-anchor": "end" }, value.toFixed(1))
    );
  }
  [startTime, startTime + (endTime - startTime) / 2, endTime].forEach((timestamp, index) => {
    chart.append(svgElement("text", {
      class: "axis-label",
      x: x(timestamp),
      y: height - 8,
      "text-anchor": index === 0 ? "start" : index === 2 ? "end" : "middle"
    }, historyTickLabel(timestamp)));
  });

  const linePoints = points.map((point) => (
    `${x(point.timestamp).toFixed(2)},${y(point.value).toFixed(2)}`
  )).join(" ");
  const latest = points[points.length - 1];
  if (points.length > 1) {
    chart.append(svgElement("polyline", { class: "history-line", points: linePoints }));
  }
  for (const [index, point] of points.entries()) {
    chart.append(svgElement("circle", {
      class: index === points.length - 1 ? "history-point latest-point" : "history-point",
      cx: x(point.timestamp),
      cy: y(point.value),
      r: index === points.length - 1 ? 4 : 2.5
    }));
  }
  chart.setAttribute(
    "aria-label",
    `${historyRanges[selectedHistoryRange].label} ${unit} history. `
      + `${points.length} readings from ${observedMinimum.toFixed(1)} to ${observedMaximum.toFixed(1)}; `
      + `latest ${latest.value.toFixed(1)} ${unit}.`
  );
}

function renderClimateHistory(items, startTime, endTime) {
  const sampleTimes = items
    .map(item => Date.parse(item.reading.observed_at || item.received_at))
    .filter(Number.isFinite);
  let chartStartTime = startTime;
  let chartEndTime = endTime;
  if (historyRanges[selectedHistoryRange].fitSamples && sampleTimes.length > 1) {
    const firstSampleTime = Math.min(...sampleTimes);
    const lastSampleTime = Math.max(...sampleTimes);
    if (lastSampleTime > firstSampleTime) {
      chartStartTime = firstSampleTime;
      chartEndTime = lastSampleTime;
    }
  }
  const temperatures = items
    .map(item => item.reading.air_temperature_c)
    .filter(value => value != null && Number.isFinite(Number(value)))
    .map(Number);
  const humidities = items
    .map(item => item.reading.air_humidity_percent)
    .filter(value => value != null && Number.isFinite(Number(value)))
    .map(Number);
  setText(document.getElementById("history-temperature"), temperatures.length
    ? temperatures[temperatures.length - 1].toFixed(1) : "--");
  setText(document.getElementById("history-humidity"), humidities.length
    ? humidities[humidities.length - 1].toFixed(1) : "--");
  renderHistoryChart(
    "temperature-history-chart", "temperature-history-empty", items,
    "air_temperature_c", "degrees Celsius", chartStartTime, chartEndTime
  );
  renderHistoryChart(
    "humidity-history-chart", "humidity-history-empty", items,
    "air_humidity_percent", "percent relative humidity", chartStartTime, chartEndTime
  );
  const status = document.getElementById("climate-history-status");
  setClass(status, "error", false);
  const sampleStatus = items.length === 1
    ? "Collecting another reading to draw the line"
    : `${items.length} stored readings`;
  setText(status, `${historyRanges[selectedHistoryRange].label} · ${sampleStatus} · Updates live`);
}

async function refreshClimateHistory() {
  if (!latestReading || !latestReadingAt || !selectedSensorId) return;
  const range = historyRanges[selectedHistoryRange];
  const endTime = Date.parse(latestReadingAt);
  if (!Number.isFinite(endTime)) return;
  const startTime = endTime - range.milliseconds;
  const requestKey = `${selectedSensorId}:${selectedHistoryRange}:${latestReadingAt}:${latestReading.report_id}`;
  if (historyRequestKey === requestKey) return;
  historyRequestKey = requestKey;
  const query = new URLSearchParams({
    sensor_id: selectedSensorId,
    start: new Date(startTime).toISOString(),
    end: new Date(endTime).toISOString()
  });
  try {
    const response = await fetch(`/api/readings/history?${query}`);
    if (!response.ok) throw new Error("history request failed");
    const items = (await response.json()).items;
    if (historyRequestKey !== requestKey) return;
    renderClimateHistory(items, startTime, endTime);
  } catch (_error) {
    if (historyRequestKey !== requestKey) return;
    historyRequestKey = null;
    const status = document.getElementById("climate-history-status");
    setClass(status, "error", true);
    setText(status, "Climate history is temporarily unavailable.");
  }
}

// A month of soil moisture beside the soil temperature. The month is fetched
// at most once a minute; the reading that arrives in between is appended, so
// the line's end is always the reading the pot shows.
const moistureTrendMs = 30 * 24 * 60 * 60 * 1000;
const moistureTrendRefetchMs = 60_000;
let moistureTrendItems = [];
let moistureTrendSensorId = null;
let moistureTrendFetchedAt = 0;
// Counts fetched months, so a new fetch redraws even with the same last point.
let moistureTrendVersion = 0;

function moistureTrendPoints() {
  const points = moistureTrendItems.map(item => ({
    timestamp: Date.parse(item.reading.observed_at || item.received_at),
    value: item.reading.moisture_percent
  }));
  if (latestReading && latestReadingAt) {
    points.push({ timestamp: Date.parse(latestReadingAt), value: latestReading.moisture_percent });
  }
  const valid = points
    .filter(point => point.value != null && Number.isFinite(point.timestamp) && Number.isFinite(Number(point.value)))
    .map(point => ({ timestamp: point.timestamp, value: Number(point.value) }))
    .sort((a, b) => a.timestamp - b.timestamp);
  return valid.filter((point, index) => index === 0 || point.timestamp > valid[index - 1].timestamp);
}

function renderMoistureTrend(profile) {
  const chart = document.getElementById("moisture-trend-chart");
  const empty = document.getElementById("moisture-trend-empty");
  const summary = document.getElementById("moisture-trend-summary");
  const points = moistureTrendPoints();
  setHidden(chart, points.length === 0);
  setHidden(empty, points.length !== 0);
  const width = Math.max(240, Math.round(chart.getBoundingClientRect().width) || 360);
  if (!renderKeyChanged(chart, [moistureTrendVersion, points.length, points.at(-1), width, profile?.watering])) return;
  chart.replaceChildren();
  if (!points.length) {
    setText(summary, "");
    return;
  }

  // A sensor with less than a month of history is drawn across the time it
  // has, so a new pot does not show as a sliver at the right edge.
  const endTime = points[points.length - 1].timestamp;
  const firstTime = points[0].timestamp;
  const fitted = endTime - firstTime < moistureTrendMs;
  const startTime = fitted ? Math.min(firstTime, endTime - 60 * 60 * 1000) : endTime - moistureTrendMs;
  // Drawn at the card's own width, so the labels keep their size on a wide
  // phone layout instead of scaling up with the chart.
  const height = 150;
  setAttr(chart, "viewBox", `0 0 ${width} ${height}`);
  const plot = { left: 30, right: 8, top: 8, bottom: 22 };
  const plotWidth = width - plot.left - plot.right;
  const plotHeight = height - plot.top - plot.bottom;
  const x = timestamp => plot.left + ((timestamp - startTime) / (endTime - startTime)) * plotWidth;
  const y = value => plot.top + ((100 - Math.max(0, Math.min(100, value))) / 100) * plotHeight;

  const watering = profile?.watering;
  if (watering) {
    const [cycleLow, cycleHigh] = watering.comfortable_cycle;
    chart.append(
      svgElement("rect", {
        class: "trend-cycle", x: plot.left, width: plotWidth,
        y: y(cycleHigh), height: y(cycleLow) - y(cycleHigh)
      }),
      svgElement("line", {
        class: "trend-cycle-edge", x1: plot.left, x2: width - plot.right, y1: y(cycleHigh), y2: y(cycleHigh)
      }),
      svgElement("text", {
        class: "trend-cycle-label", x: plot.left + 4, y: y(cycleHigh) + 12, "text-anchor": "start"
      }, `Comfortable ${cycleLow}–${cycleHigh}%`),
      svgElement("line", {
        class: "trend-refill", x1: plot.left, x2: width - plot.right,
        y1: y(watering.refill_below), y2: y(watering.refill_below)
      }),
      svgElement("text", {
        class: "trend-refill-label", x: plot.left + 4, y: y(watering.refill_below) + 12, "text-anchor": "start"
      }, `Water below ${watering.refill_below}%`)
    );
  }
  for (const value of [0, 50, 100]) {
    chart.append(
      svgElement("line", { class: "grid-line", x1: plot.left, x2: width - plot.right, y1: y(value), y2: y(value) }),
      svgElement("text", { class: "axis-label", x: plot.left - 6, y: y(value) + 4, "text-anchor": "end" }, `${value}`)
    );
  }
  const tickFormat = endTime - startTime < 36 * 60 * 60 * 1000
    ? { hour: "numeric", minute: "2-digit" }
    : { month: "short", day: "numeric" };
  [startTime, endTime].forEach((timestamp, index) => {
    chart.append(svgElement("text", {
      class: "axis-label", x: x(timestamp), y: height - 5, "text-anchor": index === 0 ? "start" : "end"
    }, new Intl.DateTimeFormat(undefined, tickFormat).format(new Date(timestamp))));
  });

  if (points.length > 1) {
    chart.append(svgElement("polyline", {
      class: "history-line",
      points: points.map(point => `${x(point.timestamp).toFixed(2)},${y(point.value).toFixed(2)}`).join(" ")
    }));
  }
  const latest = points[points.length - 1];
  chart.append(svgElement("circle", {
    class: "history-point latest-point", cx: x(latest.timestamp), cy: y(latest.value), r: 3.5
  }));

  const values = points.map(point => point.value);
  const low = Math.min(...values).toFixed(0);
  const high = Math.max(...values).toFixed(0);
  const since = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })
    .format(new Date(firstTime));
  setText(summary, fitted
    ? `Low ${low}% · high ${high}% · since ${since}`
    : `Low ${low}% · high ${high}% over 30 days`);
  chart.setAttribute(
    "aria-label",
    `Soil moisture ${fitted ? `since ${since}` : "over the last 30 days"}: `
      + `${points.length} readings from ${low} to ${high} percent; latest ${latest.value.toFixed(1)} percent.`
  );
}

async function refreshMoistureTrend() {
  if (!latestReadingAt || !selectedSensorId) return;
  const sensorId = selectedSensorId;
  if (moistureTrendSensorId === sensorId && Date.now() - moistureTrendFetchedAt < moistureTrendRefetchMs) return;
  const endTime = Date.parse(latestReadingAt);
  if (!Number.isFinite(endTime)) return;
  moistureTrendSensorId = sensorId;
  moistureTrendFetchedAt = Date.now();
  const query = new URLSearchParams({
    sensor_id: sensorId,
    start: new Date(endTime - moistureTrendMs).toISOString(),
    end: new Date(endTime).toISOString()
  });
  try {
    const response = await fetch(`/api/readings/history?${query}`);
    if (!response.ok) throw new Error("moisture history request failed");
    const items = (await response.json()).items;
    if (selectedSensorId !== sensorId) return;
    moistureTrendItems = items;
    moistureTrendVersion += 1;
    renderProfileRanges();
  } catch (_error) {
    // The next attempt waits out the usual minute; the live point still draws.
  }
}

async function loadProfiles() {
  const response = await fetch("/api/plant-profiles");
  profiles = await response.json();
  const settingSelect = document.getElementById("setting-profile");
  const detailSettingSelect = document.getElementById("detail-setting-profile");
  const onboardingSelect = document.getElementById("onboarding-profile");
  for (const [id, profile] of Object.entries(profiles.profiles)) {
    settingSelect.add(new Option(profile.name, id));
    detailSettingSelect.add(new Option(profile.name, id));
    onboardingSelect.add(new Option(profile.name, id));
  }
  setText(document.getElementById("chemistry-guidance"), profiles.guidance);
  renderProfileRanges();
}

const plantLabel = document.getElementById("plant-label");

function renderCareLog(items) {
  const list = document.getElementById("care-log-list");
  if (!renderKeyChanged(list, items)) return;
  list.replaceChildren();
  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "care-log-empty";
    setText(empty, "No care events detected yet.");
    list.append(empty);
    return;
  }

  for (const event of items) {
    const entry = document.createElement("article");
    entry.className = `care-event ${event.kind}`;
    if (careLogInitialized && !renderedCareEventIds.has(event.event_id)) {
      entry.classList.add("new");
    }
    const heading = document.createElement("div");
    heading.className = "care-event-heading";
    const title = document.createElement("strong");
    setText(title, event.title);
    const confidence = document.createElement("span");
    setText(confidence, event.kind === "drainage_assessment"
      ? "Estimated response"
      : event.confidence === "high" ? "High confidence" : "Confirm this event");
    heading.append(title, confidence);
    const summary = document.createElement("p");
    setText(summary, event.summary);
    const timestamp = document.createElement("time");
    timestamp.dateTime = event.detected_at;
    setText(timestamp, new Date(event.detected_at).toLocaleString([], {
      month: "short", day: "numeric", hour: "numeric", minute: "2-digit"
    }));
    entry.append(timestamp, heading, summary);
    list.append(entry);
  }
  renderedCareEventIds = new Set(items.map(event => event.event_id));
  careLogInitialized = true;
}

function utcDateKey(date) {
  return date.toISOString().slice(0, 10);
}

function wateringCalendarRange(observedAt) {
  const today = new Date(observedAt);
  const todayStart = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate()));
  const year = todayStart.getUTCFullYear();
  const yearStart = new Date(Date.UTC(year, 0, 1));
  const yearEnd = new Date(Date.UTC(year + 1, 0, 1));
  const start = new Date(yearStart);
  start.setUTCDate(start.getUTCDate() - start.getUTCDay());
  const end = new Date(yearEnd);
  end.setUTCDate(end.getUTCDate() + (7 - end.getUTCDay()) % 7);
  const weekCount = Math.round((end - start) / (7 * 24 * 60 * 60 * 1000));
  return { todayStart, year, yearStart, yearEnd, start, end, weekCount };
}

function dailyMoistureColor(value, watering) {
  const low = watering.concern_below;
  const high = watering.post_water_target[1];
  const ratio = Math.max(0, Math.min(1, (value - low) / (high - low)));
  return {
    background: `hsl(139  ${25 + ratio * 25}% ${90 - ratio * 43}%)`,
    border: `hsl(139 ${24 + ratio * 23}% ${78 - ratio * 41}%)`
  };
}

function renderWateringCalendar(items, range, watering) {
  if (!renderKeyChanged(document.getElementById("watering-calendar-grid"), [
    items, range.year, range.todayStart, watering
  ])) return;
  const activityByDay = new Map(items.map(item => [item.date, item]));

  const grid = document.getElementById("watering-calendar-grid");
  const months = document.getElementById("watering-calendar-months");
  const layout = document.querySelector(".watering-calendar-layout");
  const chartWidth = range.weekCount * 14 - 3;
  layout.style.width = `${24 + chartWidth}px`;
  layout.style.gridTemplateColumns = `24px ${chartWidth}px`;
  months.style.gridTemplateColumns = `repeat(${range.weekCount}, 11px)`;
  grid.style.gridTemplateColumns = `repeat(${range.weekCount}, 11px)`;
  setAttr(grid, "aria-label", `Watering events for ${range.year}`);
  setText(document.getElementById("watering-calendar-title"), `${range.year} watering history`);
  grid.replaceChildren();
  months.replaceChildren();
  let previousMonth = -1;
  for (let index = 0; index < range.weekCount * 7; index += 1) {
    const date = new Date(range.start);
    date.setUTCDate(date.getUTCDate() + index);
    const dateKey = utcDateKey(date);
    const activity = activityByDay.get(dateKey) || { watering_count: 0, drying_level: 0 };
    const count = activity.watering_count;
    const moisture = activity.final_moisture_percent;
    const cell = document.createElement("span");
    const formattedDate = date.toLocaleDateString([], { timeZone: "UTC", month: "long", day: "numeric", year: "numeric" });
    cell.className = "watering-calendar-day";
    setData(cell, "state", count ? "watered" : activity.drying_level ? "drying" : moisture != null ? "moisture" : "none");
    setData(cell, "level", count ? Math.min(3, count) : activity.drying_level);
    setData(cell, "future", date > range.todayStart ? "true" : "false");
    setData(cell, "outsideYear", date < range.yearStart || date >= range.yearEnd ? "true" : "false");
    setAttr(cell, "role", "gridcell");
    if (!count && !activity.drying_level && moisture != null) {
      const color = dailyMoistureColor(moisture, watering);
      cell.style.backgroundColor = color.background;
      cell.style.borderColor = color.border;
    }
    const moistureDescription = moisture == null ? "" : `; final soil moisture ${moisture.toFixed(1)}%`;
    const description = count
      ? `${count} watering event${count === 1 ? "" : "s"}${moistureDescription}`
      : activity.drying_level
        ? `watering overdue, drying level ${activity.drying_level} of 3${moistureDescription}`
        : moisture != null
          ? `final soil moisture ${moisture.toFixed(1)}%`
          : "no reading";
    setAttr(cell, "aria-label", `${formattedDate}: ${description}`);
    cell.title = cell.getAttribute("aria-label");
    grid.append(cell);

    if (date.getUTCFullYear() === range.year && date.getUTCMonth() !== previousMonth && date.getUTCDate() <= 7) {
      const label = document.createElement("span");
      setText(label, date.toLocaleDateString([], { timeZone: "UTC", month: "short" }));
      label.style.gridColumn = String(Math.floor(index / 7) + 1);
      months.append(label);
    }
    previousMonth = date.getUTCMonth();
  }

  const total = items.reduce((sum, item) => sum + item.watering_count, 0);
  setText(document.getElementById("watering-calendar-summary"),
    `${total} watering event${total === 1 ? "" : "s"} in ${range.year}`);
}

async function refreshWateringCalendar() {
  if (!latestReading || !latestReadingAt) return;
  const range = wateringCalendarRange(latestReadingAt);
  const profileId = selectedSensor?.profile_id || profiles.default_profile;
  const requestKey = [
    latestReading.sensor_id,
    latestReadingAt,
    range.year,
    profileId
  ].join(":");
  if (wateringCalendarRequestKey === requestKey) return;
  wateringCalendarRequestKey = requestKey;
  try {
    const query = new URLSearchParams({
      sensor_id: latestReading.sensor_id,
      start: range.yearStart.toISOString(),
      end: range.yearEnd.toISOString()
    });
    const response = await fetch(`/api/watering-calendar?${query}`);
    if (!response.ok) throw new Error("calendar request failed");
    const payload = await response.json();
    if (wateringCalendarRequestKey !== requestKey) return;
    wateringIntervalSummary = payload.watering_interval;
    renderWateringCalendar(payload.items, range, profiles.profiles[profileId].watering);
    if (profiles) {
      renderDrainageAssessment(profiles.profiles[profileId]);
    }
  } catch (_error) {
    if (wateringCalendarRequestKey === requestKey) {
      wateringCalendarRequestKey = null;
      setText(document.getElementById("watering-calendar-summary"), "History unavailable");
    }
  }
}

async function refreshPotResponse() {
  if (!latestReading) return;
  try {
    const query = new URLSearchParams({ sensor_id: latestReading.sensor_id });
    const response = await fetch(`/api/pot-response?${query}`);
    if (!response.ok) throw new Error("pot response request failed");
    drainageAssessments = (await response.json()).items;
    if (profiles) {
      const profileId = selectedSensor?.profile_id || profiles.default_profile;
      renderDrainageAssessment(profiles.profiles[profileId]);
    }
  } catch (_error) {
    return;
  }
}

async function refreshPlantJourney() {
  if (!latestReading) return;
  try {
    const query = new URLSearchParams({ sensor_id: latestReading.sensor_id });
    const response = await fetch(`/api/plant-journey?${query}`);
    if (!response.ok) throw new Error("plant journey request failed");
    const journey = await response.json();
    setText(document.getElementById("journey-days"), journey.monitored_days);
    setText(document.getElementById("journey-waterings"), journey.watering_count);
    setText(document.getElementById("journey-fertilizing"), journey.fertilizing_count);
    setText(document.getElementById("journey-missed"), journey.missed_watering_count);
    setText(document.getElementById("journey-started"), journey.started_at
      ? `Since ${new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeZone: "UTC" }).format(new Date(journey.started_at))}`
      : "No sensor history yet");
  } catch (_error) {
    return;
  }
}

async function refresh() {
  try {
    await refreshFleet();
    const page = pageFromLocation();
    if (page === "settings") {
      if (settingsTabFromLocation() === "firmware") await refreshFirmwareImages();
      return;
    }
    if (page !== "detail" && page !== "config") return;
    if (!selectedSensorId || !selectedSensor) {
      setText(document.getElementById("status"), "Sensor not found");
      setClass(document.getElementById("status-dot"), "live", false);
      return;
    }
    const query = sensorQuery();
    // Each page asks only for what it shows. The configuration page has the raw
    // report log and nothing that plots a reading; the plant page is the other
    // way round, and neither should pay for the other's requests every second.
    if (page === "config") {
      const rawReportsResponse = await fetch(`/api/raw-reports${query}`);
      if (rawReportsResponse.ok) renderRawReports((await rawReportsResponse.json()).items);
      await refreshFirmwareImages();
      return;
    }
    const [latestResponse, careLogResponse] = await Promise.all([
      fetch(`/api/readings/latest${query}`),
      fetch(`/api/care-log${query}`)
    ]);
    if (latestResponse.ok) renderReading(await latestResponse.json());
    if (careLogResponse.ok) renderCareLog((await careLogResponse.json()).items);
    await Promise.all([
      refreshClimateHistory(), refreshMoistureTrend(), refreshWateringCalendar(),
      refreshPotResponse(), refreshPlantJourney()
    ]);
  } catch (_error) {
    setText(document.getElementById("status"), "Hub unavailable");
    setClass(document.getElementById("status-dot"), "live", false);
  }
}

/* Firmware images, and what a sensor is doing with one.
 *
 * The image is read by the hub, not described by whoever uploaded it, so the
 * list here shows what each image says about itself: its version, when it was
 * built, and the digest a sensor will check what it downloaded against. */
let firmwareImages = [];

function formatBytes(bytes) {
  return (bytes / 1024 / 1024).toFixed(2) + " MB";
}

async function refreshFirmwareImages() {
  try {
    const response = await fetch("/api/firmware");
    if (!response.ok) return;
    firmwareImages = (await response.json()).items;
  } catch (_error) {
    return;
  }
  renderFirmwareImages();
  renderSensorFirmware();
}

function renderFirmwareImages() {
  const list = document.getElementById("firmware-list");
  if (list === null) return;
  if (!renderKeyChanged(list, firmwareImages)) return;
  list.replaceChildren();
  if (!firmwareImages.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    setText(empty, "No firmware images yet. Add the image you built above.");
    list.append(empty);
    return;
  }
  firmwareImages.forEach((image) => {
    const card = document.createElement("div");
    card.className = "settings-sensor";

    const heading = document.createElement("div");
    heading.className = "settings-sensor-heading";
    const name = document.createElement("strong");
    setText(name, image.version);
    const state = document.createElement("small");
    setText(state, image.available ? "Ready" : "File missing");
    heading.append(name, state);

    const identity = document.createElement("code");
    setText(identity,
      formatBytes(image.size_bytes) +
      " · built " + (image.built_at || "at an unrecorded time") +
      " · " + image.idf_version +
      " · " + image.digest.slice(0, 12));

    const body = document.createElement("div");
    body.className = "settings-sensor-body";
    body.append(heading, identity);

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "danger";
    setText(remove, "Delete");
    remove.addEventListener("click", () => deleteFirmwareImage(image));

    const actions = document.createElement("div");
    actions.className = "settings-sensor-actions";
    actions.append(remove);
    card.append(body, actions);
    list.append(card);
  });
}

async function uploadFirmwareImage(file) {
  const message = document.getElementById("firmware-upload-message");
  setText(message, "Reading " + file.name + "…");
  try {
    const response = await fetch("/api/firmware", { method: "POST", body: file });
    const payload = await response.json();
    if (!response.ok) {
      setText(message, payload.error || "Could not store that image");
      return;
    }
    setText(message, "Stored firmware " + payload.version + ".");
  } catch (_error) {
    setText(message, "Could not store that image");
    return;
  }
  await refreshFirmwareImages();
}

async function deleteFirmwareImage(image) {
  const confirmed = window.confirm(
    "Delete firmware " + image.version + " from the hub?\n\n" +
    "Any sensor waiting for it stops waiting. Sensors already running it are unaffected."
  );
  if (!confirmed) return;
  await fetch("/api/firmware/" + encodeURIComponent(image.digest), { method: "DELETE" });
  await refreshFirmwareImages();
}

/* What an update is doing, in the sensor's own terms.
 *
 * Every state says who is acting and what would move it on, because the only
 * thing worse than a slow update is one that has silently stopped. */
function describeFirmwareUpdate(sensor) {
  const target = sensor.firmware_update_version || "the chosen image";
  switch (sensor.firmware_update_state) {
    case "pending":
      return "Waiting to reach the sensor · sent the next time it reports";
    case "commanded":
      return "The sensor has been told to install " + target;
    case "downloading":
      return "Downloading " + target + " · " + sensor.firmware_update_percent + "%";
    case "installing":
      return "Installing " + target;
    case "rebooting":
      return "Restarting to run " + target;
    case "succeeded":
      return "Updated to " + target;
    case "failed":
      return "Update failed · " + (sensor.firmware_update_error || "no reason given");
    default:
      return "No update in progress.";
  }
}

function firmwareUpdateIsRunning(sensor) {
  return ["pending", "commanded", "downloading", "installing", "rebooting"].includes(
    sensor.firmware_update_state
  );
}

function renderSensorFirmware() {
  const choice = document.getElementById("firmware-choice");
  if (choice === null || !selectedSensor) return;
  setText(document.getElementById("firmware-current"), selectedSensor.firmware_version
    ? "Running " + selectedSensor.firmware_version
    : "Version not reported yet");
  setText(document.getElementById("firmware-update-state"),
    describeFirmwareUpdate(selectedSensor));

  const running = firmwareUpdateIsRunning(selectedSensor);
  const progress = document.getElementById("firmware-progress");
  setHidden(progress, !running);
  setStyle(document.getElementById("firmware-progress-bar"), "width",
    Math.max(2, selectedSensor.firmware_update_percent || 0) + "%");
  setHidden(document.getElementById("firmware-cancel"), !running);

  const renderKey = JSON.stringify(firmwareImages.map((image) => [image.version, image.digest]));
  if (choice.dataset.renderKey !== renderKey) {
    setData(choice, "renderKey", renderKey);
    choice.replaceChildren();
    firmwareImages
      .filter((image) => image.available)
      .forEach((image) => {
        const option = document.createElement("option");
        option.value = image.digest;
        setText(option, image.version + " · " + formatBytes(image.size_bytes));
        choice.append(option);
      });
  }

  /* The sensor downloads over Wi-Fi, so being on the network is a precondition
   * rather than something to discover halfway through an update. Say so where
   * the button is, and do not offer the button. */
  const onNetwork = Boolean(selectedSensor.wifi_enabled && selectedSensor.wifi_state === "joined");
  const note = document.getElementById("firmware-precondition");
  const install = document.getElementById("firmware-install");
  if (!firmwareImages.some((image) => image.available)) {
    setText(note,
      "No firmware images are stored on this hub. Add one under Settings · Firmware.");
  } else if (!onNetwork) {
    setText(note,
      "This sensor downloads firmware over the household network. Switch its web console on " +
      "above and wait until it reports an address, then come back.");
  } else {
    setText(note,
      "The hub tells the sensor which image to fetch over Bluetooth; the sensor downloads it " +
      "from the hub over Wi-Fi and restarts. It keeps its pairing, plant and settings.");
  }
  setDisabled(install, running || !onNetwork || choice.options.length === 0);
}

async function installFirmware() {
  const choice = document.getElementById("firmware-choice");
  if (!selectedSensorId || !choice.value) return;
  const image = firmwareImages.find((candidate) => candidate.digest === choice.value);
  const confirmed = window.confirm(
    "Send firmware " + (image ? image.version : "") + " to " +
    (selectedSensor.display_name || selectedSensorId) + "?\n\n" +
    "The sensor downloads it, installs it and restarts. If the new firmware does not start, " +
    "the sensor goes back to the one it is running now."
  );
  if (!confirmed) return;
  await fetch("/api/sensors/" + encodeURIComponent(selectedSensorId) + "/firmware", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ digest: choice.value }),
  });
  await refreshFleet();
}

async function cancelFirmwareUpdate() {
  if (!selectedSensorId) return;
  await fetch("/api/sensors/" + encodeURIComponent(selectedSensorId) + "/firmware", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ digest: "" }),
  });
  await refreshFleet();
}

document.getElementById("open-inbox").addEventListener("click", () => document.getElementById("inbox-dialog").showModal());
document.getElementById("close-settings").addEventListener("click", () => document.getElementById("settings-dialog").close());
document.getElementById("settings-form").addEventListener("submit", saveManagedSensor);
document.querySelectorAll("[data-history-range]").forEach((button) => {
  button.addEventListener("click", () => {
    selectedHistoryRange = button.dataset.historyRange;
    historyRequestKey = null;
    document.querySelectorAll("[data-history-range]").forEach((option) => {
      setAttr(option, "aria-pressed", String(option === button));
    });
    refreshClimateHistory();
  });
});
const sensorSettingsForm = document.getElementById("sensor-settings-form");
sensorSettingsForm.addEventListener("input", () => {
  sensorSettingsDraftSensorId = selectedSensorId;
});
sensorSettingsForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!selectedSensorId) return;
  const sensorId = selectedSensorId;
  const submitSequence = ++sensorSettingsSubmitSequence;
  sensorSettingsDraftSensorId = sensorId;
  const message = document.getElementById("sensor-settings-message");
  const response = await fetch(`/api/sensors/${encodeURIComponent(sensorId)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      display_name: document.getElementById("detail-setting-name").value,
      room_id: Number(document.getElementById("detail-setting-room").value) || null,
      profile_id: document.getElementById("detail-setting-profile").value,
      expected_interval_seconds: Number(
        document.getElementById("detail-setting-reporting-interval").value
      )
    })
  });
  const payload = await response.json();
  if (submitSequence !== sensorSettingsSubmitSequence || selectedSensorId !== sensorId) return;
  if (!response.ok) {
    setText(message, payload.error || "Could not save sensor configuration");
    return;
  }
  sensorSettingsDraftSensorId = null;
  fleetRequestSequence += 1;
  fleetSensors = fleetSensors.map((sensor) => sensor.sensor_id === sensorId ? payload : sensor);
  selectedSensor = payload;
  renderedSettingsKey = null;
  renderFleet();
  renderSensorSettings();
  renderPage();
  setText(message, payload.device_config_status === "applied"
    ? "Sensor configuration is already applied."
    : "Configuration saved. It will be sent when the sensor next reports.");
});
document.getElementById("settings-link").addEventListener("click", (event) => {
  event.preventDefault();
  navigate("/settings");
});
document.getElementById("fleet-link").addEventListener("click", (event) => {
  event.preventDefault();
  navigate("/");
});
document.getElementById("room-form").addEventListener("submit", saveRoom);
document.getElementById("cancel-room-edit").addEventListener("click", clearRoomForm);
document.getElementById("room-list").addEventListener("click", (event) => {
  const edit = event.target.closest(".edit-room");
  if (edit) {
    startRoomEdit(Number(edit.dataset.roomId));
    return;
  }
  const remove = event.target.closest(".delete-room");
  if (remove) deleteRoom(Number(remove.dataset.roomId));
});
document.getElementById("onboarding-room").addEventListener("change", renderRoomChoices);
document.getElementById("detail-console-toggle").addEventListener("change", (event) => {
  renderedSettingsKey = null;
  setSensorConsole(event.target.dataset.sensorId, event.target.checked);
});
document.querySelectorAll(".settings-tab").forEach((tab) => {
  tab.addEventListener("click", (event) => {
    event.preventDefault();
    navigate(tab.getAttribute("href"));
  });
});
document.getElementById("household-network-form").addEventListener("submit", saveHouseholdNetwork);
document.getElementById("wifi-ssid").addEventListener("input", () => {
  householdNetworkDraft = true;
});
document.getElementById("forget-household-network").addEventListener("click", forgetHouseholdNetwork);
document.getElementById("settings-sensor-list").addEventListener("click", (event) => {
  const forget = event.target.closest(".forget-sensor");
  if (forget) forgetSensor(forget.dataset.sensorId);
});
document.querySelector(".settings-page .primary-action").addEventListener("click", (event) => {
  event.preventDefault();
  resetOnboarding();
  navigate("/onboarding");
});
document.getElementById("onboarding-candidates").addEventListener("click", (event) => {
  const choice = event.target.closest(".onboarding-candidate");
  if (!choice) return;
  onboardingSensorId = choice.dataset.sensorId;
  setData(document.getElementById("onboarding-candidates"), "renderKey", "");
  renderOnboarding();
});
["onboarding-name", "onboarding-room"].forEach((id) => {
  document.getElementById(id).addEventListener("input", () => {
    onboardingDraft = true;
  });
});
document.getElementById("onboarding-step-details").addEventListener("submit", (event) => {
  event.preventDefault();
});
document.getElementById("onboarding-back").addEventListener("click", () => {
  const index = ONBOARDING_STEPS.indexOf(onboardingStep);
  if (index <= 0) {
    resetOnboarding();
    navigate("/settings");
    return;
  }
  onboardingStep = ONBOARDING_STEPS[index - 1];
  renderOnboarding();
});
document.getElementById("onboarding-next").addEventListener("click", async () => {
  if (onboardingStep === "find") {
    if (!onboardingSensorId) return;
    onboardingStep = "pair";
  } else if (onboardingStep === "pair") {
    onboardingStep = "details";
    onboardingDraft = false;
  } else if (onboardingStep === "details") {
    if (await finishOnboarding()) onboardingStep = "done";
  } else {
    const finished = onboardingSensorId;
    resetOnboarding();
    if (finished) {
      await selectSensor(finished);
      return;
    }
    navigate("/settings");
    return;
  }
  renderOnboarding();
});
window.addEventListener("popstate", () => {
  sensorSettingsDraftSensorId = null;
  selectedSensorId = sensorIdFromLocation();
  refresh();
});
async function poll() {
  await refresh();
  window.setTimeout(poll, 1000);
}
document.getElementById("firmware-file").addEventListener("change", (event) => {
  const file = event.target.files[0];
  if (file) uploadFirmwareImage(file);
  event.target.value = "";
});
document.getElementById("firmware-install").addEventListener("click", installFirmware);
document.getElementById("firmware-cancel").addEventListener("click", cancelFirmwareUpdate);
trackRawReportsDisclosure();
loadProfiles().then(refreshHouseholdNetwork).then(refreshRooms).then(refreshFirmwareImages).then(poll);