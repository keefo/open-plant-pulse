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
const historyRanges = {
  "live": { label: "Live (up to 24 hours)", milliseconds: 24 * 60 * 60 * 1000, fitSamples: true },
  "24h": { label: "Last 24 hours", milliseconds: 24 * 60 * 60 * 1000 },
  "2d": { label: "Last 2 days", milliseconds: 2 * 24 * 60 * 60 * 1000 },
  "7d": { label: "Last 7 days", milliseconds: 7 * 24 * 60 * 60 * 1000 },
  "30d": { label: "Last 30 days", milliseconds: 30 * 24 * 60 * 60 * 1000 }
};
let profiles = null;
let latestReading = null;
let selectedHistoryRange = "live";
let historyRequestKey = null;
let drainageAssessments = [];
let renderedCareEventIds = new Set();
let careLogInitialized = false;
let clockObservedAt = null;
let clockAnchoredAt = null;
let clockScale = null;
let previousClockSample = null;
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
let householdNetworkDraft = false;
let renderedSettingsSensorsKey = null;
let onboardingStep = "find";
let onboardingSensorId = null;
let onboardingDraft = false;
let onboardingResult = null;


function pageFromLocation() {
  const path = window.location.pathname;
  if (path.startsWith("/sensors/")) return "detail";
  if (path === "/settings" || path.startsWith("/settings/")) return "settings";
  if (path === "/onboarding" || path.startsWith("/onboarding/")) return "onboarding";
  return "fleet";
}

function settingsTabFromLocation() {
  return window.location.pathname === "/settings/wifi" ? "wifi" : "sensors";
}

function sensorIdFromLocation() {
  const match = window.location.pathname.match(/^\/sensors\/(.+)$/);
  return match ? decodeURIComponent(match[1]) : null;
}

function sensorQuery() {
  return selectedSensorId ? `?${new URLSearchParams({ sensor_id: selectedSensorId })}` : "";
}

function renderPage() {
  const page = pageFromLocation();
  document.querySelectorAll("[data-page]").forEach((element) => {
    element.hidden = element.dataset.page !== page;
  });
  document.getElementById("fleet-link").classList.toggle("active", page === "fleet");
  document.getElementById("settings-link").classList.toggle("active", page === "settings");
  if (page === "settings") renderSettingsTab();
  if (page === "onboarding") renderOnboarding();
  document.title = titleForPage(page);
}

function titleForPage(page) {
  if (page === "detail" && selectedSensor) {
    return `${selectedSensor.display_name || selectedSensor.sensor_id} · Open Plant Pulse`;
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
  const alerts = [];
  if (moistureLow != null && latest?.moisture_percent < moistureLow) {
    alerts.push("moisture low");
  }
  if (conductivityHigh != null && latest?.conductivity_us_cm > conductivityHigh) {
    alerts.push("conductivity high");
  }
  if (sensor.freshness === "stale") return "Stale";
  return alerts.length ? alerts.join(", ") : "Healthy";
}

function updateFleetCard(card, sensor) {
  card.href = `/sensors/${encodeURIComponent(sensor.sensor_id)}`;
  card.children[0].textContent = sensor.display_name || sensor.sensor_id;
  card.children[1].textContent = sensor.sensor_id;
  const reportedAt = sensor.latest?.received_at || sensor.last_seen_at;
  card.children[2].textContent = reportedAt
    ? `Last report ${new Date(reportedAt).toLocaleString()}`
    : "No reports received";
  const state = sensorState(sensor);
  card.children[3].textContent = state;
  card.children[3].dataset.state = state.toLowerCase();
}

function createFleetCard(sensor) {
  const card = document.createElement("a");
  card.className = "fleet-card";
  card.dataset.sensorId = sensor.sensor_id;
  card.innerHTML = `<strong></strong><code></code><span></span><small></small>`;
  updateFleetCard(card, sensor);
  return card;
}

function renderFleet() {
  document.getElementById("inbox-count").textContent = unclaimedSensors.length;
  const grid = document.getElementById("fleet-grid");
  const cardsBySensorId = new Map(
    Array.from(grid.querySelectorAll(".fleet-card"), (card) => [card.dataset.sensorId, card])
  );
  if (!fleetSensors.length) {
    for (const card of cardsBySensorId.values()) card.remove();
    if (!grid.querySelector(".empty-state")) {
      const empty = document.createElement("p");
      empty.className = "empty-state";
      empty.textContent = "No sensors are enrolled. Open the sensor inbox to enroll a nearby sensor.";
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
  list.replaceChildren();
  if (!unclaimedSensors.length) {
    const empty = document.createElement("p");
    empty.textContent = "No unclaimed sensors are nearby.";
    list.append(empty);
    return;
  }
  for (const sensor of unclaimedSensors) {
    const item = document.createElement("article");
    item.className = "inbox-item";
    const name = document.createElement("strong");
    name.textContent = sensor.sensor_id;
    const details = document.createElement("small");
    details.textContent = `${sensor.transport} · RSSI ${sensor.latest_rssi ?? "unknown"} · last seen ${new Date(sensor.last_seen_at).toLocaleString()}`;
    const enroll = document.createElement("button");
    enroll.type = "button";
    enroll.textContent = "Enroll";
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
    currentOption.dataset.currentInterval = "true";
    select.add(currentOption);
  }
  select.value = value;
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
  document.getElementById("settings-sensor-id").textContent = selectedSensor.sensor_id;
  if (!preserveDraft) {
    document.getElementById("detail-setting-name").value = selectedSensor.display_name || "";
    document.getElementById("detail-setting-room").value = selectedSensor.room || "";
    document.getElementById("detail-setting-profile").value = selectedSensor.profile_id || profiles.default_profile;
    selectReportingInterval(
      document.getElementById("detail-setting-reporting-interval"),
      selectedSensor.expected_interval_seconds
    );
    document.getElementById("sensor-settings-message").textContent = "";
  }
  const sensorInterval = selectedSensor.sensor_reporting_interval_seconds;
  document.getElementById("sensor-reporting-interval").value = sensorInterval == null
    ? "Not synced yet"
    : formatReportingInterval(sensorInterval);
  const delivery = document.getElementById("device-config-status");
  if (selectedSensor.device_config_status === "applied") {
    delivery.textContent = `Applied on sensor · revision ${selectedSensor.device_config_revision}`;
  } else if (selectedSensor.device_config_status === "retrying") {
    delivery.textContent = `Delivery will retry on the next report · ${selectedSensor.device_config_error}`;
  } else {
    delivery.textContent = "Waiting to send when the sensor next reports.";
  }

  const replacement = document.getElementById("replacement-sensor");
  replacement.replaceChildren(new Option("Choose an unclaimed sensor", ""));
  for (const candidate of unclaimedSensors) {
    replacement.add(new Option(candidate.sensor_id, candidate.sensor_id));
  }
  document.getElementById("replacement-control").hidden = replacement.options.length === 1;
}

function renderRawReports(items) {
  const rows = document.getElementById("raw-report-list");
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
    status.dataset.status = item.decode_status;
    const payload = Object.assign(document.createElement("code"), {
      textContent: item.service_data_hex || "unavailable",
      title: `SHA-256 ${item.payload_sha256}`
    });
    const values = [
      new Date(item.received_at).toLocaleString(),
      item.packet_id == null ? "—" : String(item.packet_id),
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
  document.getElementById("hub-health").replaceChildren(
    Object.assign(document.createElement("span"), { textContent: `Hub started ${new Date(health.started_at).toLocaleString()}` }),
    Object.assign(document.createElement("span"), { textContent: `Database: ${health.database.status}` }),
    Object.assign(document.createElement("span"), { textContent: `BLE scanner: ${scanner.status}` }),
    Object.assign(document.createElement("span"), { textContent: `Last BLE receive: ${scanner.last_receive_at ? new Date(scanner.last_receive_at).toLocaleString() : "none"}` })
  );
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
      document.getElementById("plant-profile-name").textContent = profile.name;
      document.getElementById("scientific-name").textContent = profile.scientific_name;
    }
  }
  renderFleet();
  renderInbox();
  renderSensorSettings();
  renderSettingsSensors();
  renderHouseholdNetwork();
  renderPage();
}

async function selectSensor(sensorId) {
  sensorSettingsDraftSensorId = null;
  selectedSensorId = sensorId;
  selectedSensor = fleetSensors.find((sensor) => sensor.sensor_id === sensorId) || null;
  history.pushState({}, "", `/sensors/${encodeURIComponent(sensorId)}`);
  latestReading = null;
  historyRequestKey = null;
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
  document.getElementById("settings-message").textContent = "";
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
    document.getElementById("settings-message").textContent = payload.error || "Could not save sensor";
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

function updateClockSource(payload) {
  const observedAt = Date.parse(payload.reading.observed_at || payload.received_at);
  const receivedAt = Date.parse(payload.received_at);
  if (!Number.isFinite(observedAt) || !Number.isFinite(receivedAt)) return;
  if (
    previousClockSample &&
    observedAt === previousClockSample.observedAt &&
    receivedAt === previousClockSample.receivedAt
  ) return;

  if (previousClockSample) {
    const observedDelta = observedAt - previousClockSample.observedAt;
    const receivedDelta = receivedAt - previousClockSample.receivedAt;
    if (observedDelta > 0 && receivedDelta > 0) {
      clockScale = Math.max(0.1, Math.min(10000, observedDelta / receivedDelta));
    }
  }
  const anchoredAt = performance.now();
  const projectedTime = clockObservedAt == null || clockAnchoredAt == null
    ? observedAt
    : clockObservedAt + (anchoredAt - clockAnchoredAt) * (clockScale || 1);
  const receivedAge = Math.max(0, Date.now() - receivedAt);
  const incomingTime = observedAt + (
    receivedAge <= sensorOfflineAfterMs ? receivedAge * (clockScale || 1) : 0
  );
  previousClockSample = { observedAt, receivedAt };
  clockObservedAt = Math.max(projectedTime, incomingTime);
  clockAnchoredAt = anchoredAt;
}

function renderSimulationClock() {
  if (clockObservedAt == null || clockAnchoredAt == null) return;
  const scale = clockScale || 1;
  const simulatedNow = new Date(clockObservedAt + (performance.now() - clockAnchoredAt) * scale);
  const seconds = simulatedNow.getSeconds() + simulatedNow.getMilliseconds() / 1000;
  const minutes = simulatedNow.getMinutes() + seconds / 60;
  const hours = simulatedNow.getHours() % 12 + minutes / 60;
  document.getElementById("clock-hour").style.transform = `rotate(${hours * 30}deg)`;
  document.getElementById("clock-minute").style.transform = `rotate(${minutes * 6}deg)`;
  document.getElementById("clock-second").style.transform = `rotate(${seconds * 6}deg)`;

  const scaleLabel = clockScale == null
    ? "Syncing"
    : `${clockScale >= 10 ? Math.round(clockScale) : clockScale.toFixed(1)}×`;
  document.getElementById("time-scale").textContent = scaleLabel;
  document.getElementById("simulation-clock").setAttribute(
    "aria-label",
    `Simulation time ${simulatedNow.toLocaleTimeString()}, running at ${scaleLabel}`
  );
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

  vessel.style.setProperty("--fill", `${clamp(value)}%`);
  vessel.style.setProperty("--target-low", `${clamp(targetLow)}%`);
  vessel.style.setProperty("--target-size", `${clamp(targetHigh) - clamp(targetLow)}%`);
  vessel.style.setProperty("--cycle-low", `${clamp(cycleLow)}%`);
  vessel.style.setProperty("--cycle-size", `${clamp(cycleHigh) - clamp(cycleLow)}%`);
  vessel.style.setProperty("--refill", `${clamp(watering.refill_below)}%`);
  vessel.setAttribute("aria-label", `Soil moisture ${value.toFixed(1)} percent. ${wateringPhase(watering, value)}.`);
  vessel.dataset.phase = wateringPhase(watering, value).toLowerCase().replaceAll(" ", "-");
  document.getElementById("moisture-target-label").textContent = `After watering ${targetLow}–${targetHigh}%`;
  document.getElementById("moisture-refill-label").textContent = `Water below ${watering.refill_below}%`;
  document.getElementById("moisture-phase").textContent = wateringPhase(watering, value);
  document.getElementById("moisture-strategy").textContent = watering.label;
}

function renderPlantAssessment(profile) {
  const metrics = ["root_zone", "climate", "chemistry"]
    .flatMap(section => Object.entries(profile[section] || {}))
    .filter(([metricName]) => metricName !== "moisture_percent");
  const assessments = metrics
    .map(([metricName, metric]) => assessMetric(metricName, metric, latestReading[metricName]))
    .filter(Boolean);
  const moistureAssessment = assessWatering(profile.watering, latestReading.moisture_percent);
  if (moistureAssessment) assessments.push(moistureAssessment);
  const metricCount = metrics.length + 1;
  const panel = document.getElementById("plant-assessment");
  const status = document.getElementById("plant-status");
  const reason = document.getElementById("plant-status-reason");

  if (!assessments.length) {
    panel.dataset.level = "waiting";
    status.textContent = "Unknown";
    reason.textContent = "No profiled measurements are available yet.";
    return;
  }

  const worst = assessments.reduce((current, item) => item.deviation > current.deviation ? item : current);
  const level = profiles.assessment_policy.levels.find(
    item => item.max_deviation == null || worst.deviation <= item.max_deviation
  );
  panel.dataset.level = level.status.toLowerCase();
  status.textContent = level.status;

  const coverage = `${assessments.length} of ${metricCount} readings assessed`;
  if (worst.deviation === 0) {
    reason.textContent = `All ${assessments.length} readings are within ${profile.name} healthy limits.`;
  } else if (worst.reason) {
    reason.textContent = `${worst.reason} ${coverage}.`;
  } else {
    const suffix = worst.unit ? ` ${worst.unit}` : "";
    reason.textContent = `${worst.label} is ${formatDifference(worst.distance, worst.metricName)}${suffix} ${worst.direction}. ${coverage}.`;
  }
}

function renderGauge(card, metricName, metric, value) {
  const [scaleLow, scaleHigh] = metric.scale;
  const [idealLow, idealHigh] = metric.ideal;
  const percentage = number => (number - scaleLow) / (scaleHigh - scaleLow) * 100;
  const marker = Math.max(0, Math.min(100, percentage(value)));
  card.querySelector(".ideal-range").style.left = `${percentage(idealLow)}%`;
  card.querySelector(".ideal-range").style.width = `${percentage(idealHigh) - percentage(idealLow)}%`;
  card.querySelector(".value-marker").style.left = `${marker}%`;
  card.querySelector(".scale-low").textContent = scaleLow;
  card.querySelector(".scale-high").textContent = scaleHigh;
  card.querySelector(".ideal-label").textContent = `Ideal ${idealLow}–${idealHigh}`;

  const status = card.querySelector(".range-status");
  status.className = "range-status";
  const suffix = metric.unit ? ` ${metric.unit}` : "";
  if (value < idealLow) {
    status.textContent = `${formatDifference(idealLow - value, metricName)}${suffix} below ideal`;
    status.classList.add("out-range");
  } else if (value > idealHigh) {
    status.textContent = `${formatDifference(value - idealHigh, metricName)}${suffix} above ideal`;
    status.classList.add("out-range");
  } else {
    const margin = Math.min(value - idealLow, idealHigh - value);
    status.textContent = `Inside ideal zone · ${formatValue(margin, metricName)}${suffix} to nearest edge`;
    status.classList.add("in-range");
  }
}

function renderProfileRanges() {
  if (!profiles || !latestReading) return;
  const profileId = selectedSensor?.profile_id || profiles.default_profile;
  const profile = profiles.profiles[profileId];
  document.getElementById("scientific-name").textContent = profile.scientific_name;
  document.getElementById("chemistry-profile").textContent = `${profile.name} starter targets`;
  renderMoistureVessel(profile);
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
  for (const card of document.querySelectorAll(".chemistry-gauge")) {
    const metricName = card.dataset.metric;
    document.getElementById(chemistryValueIds[metricName]).textContent = formatValue(latestReading[metricName], metricName);
    renderGauge(card, metricName, profile.chemistry[metricName], latestReading[metricName]);
  }
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
    interval.textContent = `Typical dry-down cycle: ${formattedDays} days`;
  } else {
    interval.textContent = "Two watering dates are needed to learn the usual interval";
  }

  if (sensorEvents.length < requiredCycles) {
    panel.dataset.level = "learning";
    status.textContent = "Learning";
    reason.textContent = `${sensorEvents.length} of ${requiredCycles} watering cycles learned. ${profile.drainage.label}.`;
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
    panel.dataset.level = "suitable";
    status.textContent = "Suitable";
    reason.textContent = `This pot appears suitable for ${profile.name}. It is consistently ${responseLabel} and settles within ${settleMinutes.toFixed(0)} minutes.`;
  } else {
    panel.dataset.level = "unsuitable";
    status.textContent = "Possibly unsuitable";
    const issue = classMatches
      ? `takes up to ${settleMinutes.toFixed(0)} minutes to settle`
      : `behaves as ${responseLabel}`;
    reason.textContent = `The pot ${issue}; ${profile.name} prefers ${profile.drainage.label.toLowerCase()}. Check substrate, drainage holes, and probe placement.`;
  }
}

function renderReading(payload) {
  const reading = payload.reading;
  latestReading = reading;
  updateClockSource(payload);
  for (const [id, [key, precision]] of Object.entries(fields)) {
    const value = reading[key];
    document.getElementById(id).textContent = value == null ? "--" : Number(value).toFixed(precision);
  }
  const receivedAt = Date.parse(payload.received_at);
  const staleAfterMs = selectedSensor
    ? (selectedSensor.sensor_reporting_interval_seconds
      ?? selectedSensor.expected_interval_seconds) * 2_000
    : sensorOfflineAfterMs;
  const isLive = Number.isFinite(receivedAt) && Date.now() - receivedAt <= staleAfterMs;
  document.getElementById("status").textContent = isLive ? "Sensor fresh" : "Sensor stale";
  document.getElementById("status-dot").classList.toggle("live", isLive);
  const displayedAt = reading.observed_at || payload.received_at;
  document.getElementById("updated").textContent = `Sample ${reading.sequence} · ${new Date(displayedAt).toLocaleTimeString()}`;
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
  chart.replaceChildren();
  chart.toggleAttribute("hidden", points.length === 0);
  empty.hidden = points.length !== 0;
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
  document.getElementById("history-temperature").textContent = temperatures.length
    ? temperatures[temperatures.length - 1].toFixed(1) : "--";
  document.getElementById("history-humidity").textContent = humidities.length
    ? humidities[humidities.length - 1].toFixed(1) : "--";
  renderHistoryChart(
    "temperature-history-chart", "temperature-history-empty", items,
    "air_temperature_c", "degrees Celsius", chartStartTime, chartEndTime
  );
  renderHistoryChart(
    "humidity-history-chart", "humidity-history-empty", items,
    "air_humidity_percent", "percent relative humidity", chartStartTime, chartEndTime
  );
  const status = document.getElementById("climate-history-status");
  status.classList.remove("error");
  const sampleStatus = items.length === 1
    ? "Collecting another reading to draw the line"
    : `${items.length} stored readings`;
  status.textContent = `${historyRanges[selectedHistoryRange].label} · ${sampleStatus} · Updates live`;
}

async function refreshClimateHistory() {
  if (!latestReading || !latestReading.observed_at || !selectedSensorId) return;
  const range = historyRanges[selectedHistoryRange];
  const endTime = Date.parse(latestReading.observed_at);
  if (!Number.isFinite(endTime)) return;
  const startTime = endTime - range.milliseconds;
  const requestKey = `${selectedSensorId}:${selectedHistoryRange}:${latestReading.observed_at}:${latestReading.sequence}`;
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
    status.classList.add("error");
    status.textContent = "Climate history is temporarily unavailable.";
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
  document.getElementById("chemistry-guidance").textContent = profiles.guidance;
  renderProfileRanges();
}

const plantLabel = document.getElementById("plant-label");

function renderCareLog(items) {
  const list = document.getElementById("care-log-list");
  list.replaceChildren();
  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "care-log-empty";
    empty.textContent = "No care events detected yet.";
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
    title.textContent = event.title;
    const confidence = document.createElement("span");
    confidence.textContent = event.kind === "drainage_assessment"
      ? "Estimated response"
      : event.confidence === "high" ? "High confidence" : "Confirm this event";
    heading.append(title, confidence);
    const summary = document.createElement("p");
    summary.textContent = event.summary;
    const timestamp = document.createElement("time");
    timestamp.dateTime = event.detected_at;
    timestamp.textContent = new Date(event.detected_at).toLocaleString([], {
      month: "short", day: "numeric", hour: "numeric", minute: "2-digit"
    });
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
  const activityByDay = new Map(items.map(item => [item.date, item]));

  const grid = document.getElementById("watering-calendar-grid");
  const months = document.getElementById("watering-calendar-months");
  const layout = document.querySelector(".watering-calendar-layout");
  const chartWidth = range.weekCount * 14 - 3;
  layout.style.width = `${24 + chartWidth}px`;
  layout.style.gridTemplateColumns = `24px ${chartWidth}px`;
  months.style.gridTemplateColumns = `repeat(${range.weekCount}, 11px)`;
  grid.style.gridTemplateColumns = `repeat(${range.weekCount}, 11px)`;
  grid.setAttribute("aria-label", `Watering events for ${range.year}`);
  document.getElementById("watering-calendar-title").textContent = `${range.year} watering history`;
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
    cell.dataset.state = count ? "watered" : activity.drying_level ? "drying" : moisture != null ? "moisture" : "none";
    cell.dataset.level = count ? Math.min(3, count) : activity.drying_level;
    cell.dataset.future = date > range.todayStart ? "true" : "false";
    cell.dataset.outsideYear = date < range.yearStart || date >= range.yearEnd ? "true" : "false";
    cell.setAttribute("role", "gridcell");
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
    cell.setAttribute("aria-label", `${formattedDate}: ${description}`);
    cell.title = cell.getAttribute("aria-label");
    grid.append(cell);

    if (date.getUTCFullYear() === range.year && date.getUTCMonth() !== previousMonth && date.getUTCDate() <= 7) {
      const label = document.createElement("span");
      label.textContent = date.toLocaleDateString([], { timeZone: "UTC", month: "short" });
      label.style.gridColumn = String(Math.floor(index / 7) + 1);
      months.append(label);
    }
    previousMonth = date.getUTCMonth();
  }

  const total = items.reduce((sum, item) => sum + item.watering_count, 0);
  document.getElementById("watering-calendar-summary").textContent =
    `${total} watering event${total === 1 ? "" : "s"} in ${range.year}`;
}

async function refreshWateringCalendar() {
  if (!latestReading || !latestReading.observed_at) return;
  const range = wateringCalendarRange(latestReading.observed_at);
  const profileId = selectedSensor?.profile_id || profiles.default_profile;
  const requestKey = [
    latestReading.sensor_id,
    latestReading.observed_at,
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
      document.getElementById("watering-calendar-summary").textContent = "History unavailable";
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
    document.getElementById("journey-days").textContent = journey.monitored_days;
    document.getElementById("journey-waterings").textContent = journey.watering_count;
    document.getElementById("journey-fertilizing").textContent = journey.fertilizing_count;
    document.getElementById("journey-missed").textContent = journey.missed_watering_count;
    document.getElementById("journey-started").textContent = journey.started_at
      ? `Since ${new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeZone: "UTC" }).format(new Date(journey.started_at))}`
      : "No sensor history yet";
  } catch (_error) {
    return;
  }
}

async function restoreClockScale() {
  if (!selectedSensorId) return;
  try {
    const response = await fetch(`/api/readings/history${sensorQuery()}`);
    if (!response.ok) return;
    const history = (await response.json()).items;
    history.slice(-2).forEach(updateClockSource);
  } catch (_error) {
    return;
  }
}

async function refresh() {
  try {
    await refreshFleet();
    if (pageFromLocation() !== "detail") return;
    if (!selectedSensorId || !selectedSensor) {
      document.getElementById("status").textContent = "Sensor not found";
      document.getElementById("status-dot").classList.remove("live");
      return;
    }
    const query = sensorQuery();
    const [latestResponse, careLogResponse, rawReportsResponse] = await Promise.all([
      fetch(`/api/readings/latest${query}`),
      fetch(`/api/care-log${query}`),
      fetch(`/api/raw-reports${query}`)
    ]);
    if (latestResponse.ok) renderReading(await latestResponse.json());
    if (careLogResponse.ok) renderCareLog((await careLogResponse.json()).items);
    if (rawReportsResponse.ok) renderRawReports((await rawReportsResponse.json()).items);
    await Promise.all([
      refreshClimateHistory(), refreshWateringCalendar(), refreshPotResponse(), refreshPlantJourney()
    ]);
  } catch (_error) {
    document.getElementById("status").textContent = "Hub unavailable";
    document.getElementById("status-dot").classList.remove("live");
  }
}

document.getElementById("open-inbox").addEventListener("click", () => document.getElementById("inbox-dialog").showModal());
document.getElementById("close-settings").addEventListener("click", () => document.getElementById("settings-dialog").close());
document.getElementById("settings-form").addEventListener("submit", saveManagedSensor);
document.querySelectorAll("[data-history-range]").forEach((button) => {
  button.addEventListener("click", () => {
    selectedHistoryRange = button.dataset.historyRange;
    historyRequestKey = null;
    document.querySelectorAll("[data-history-range]").forEach((option) => {
      option.setAttribute("aria-pressed", String(option === button));
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
      room: document.getElementById("detail-setting-room").value,
      profile_id: document.getElementById("detail-setting-profile").value,
      expected_interval_seconds: Number(
        document.getElementById("detail-setting-reporting-interval").value
      )
    })
  });
  const payload = await response.json();
  if (submitSequence !== sensorSettingsSubmitSequence || selectedSensorId !== sensorId) return;
  if (!response.ok) {
    message.textContent = payload.error || "Could not save sensor configuration";
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
  message.textContent = payload.device_config_status === "applied"
    ? "Sensor configuration is already applied."
    : "Configuration saved. It will be sent when the sensor next reports.";
});
document.getElementById("replace-sensor").addEventListener("click", async () => {
  if (!selectedSensorId) return;
  const replacementSensorId = document.getElementById("replacement-sensor").value;
  if (!replacementSensorId) return;
  const response = await fetch(`/api/sensors/${encodeURIComponent(selectedSensorId)}/replace`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      replacement_sensor_id: replacementSensorId,
      merge_history: document.getElementById("merge-history").checked
    })
  });
  const payload = await response.json();
  if (!response.ok) {
    document.getElementById("sensor-settings-message").textContent = payload.error || "Could not replace sensor";
    return;
  }
  renderedSettingsKey = null;
  await selectSensor(payload.sensor_id);
});
document.getElementById("archive-sensor").addEventListener("click", async () => {
  if (!selectedSensorId || !selectedSensor) return;
  const displayName = selectedSensor.display_name || selectedSensor.sensor_id;
  if (!window.confirm(`Archive ${displayName}? Its history will be retained.`)) return;
  const response = await fetch(`/api/sensors/${encodeURIComponent(selectedSensorId)}/archive`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ archived: true })
  });
  if (!response.ok) {
    const payload = await response.json();
    document.getElementById("sensor-settings-message").textContent = payload.error || "Could not archive sensor";
    return;
  }
  history.pushState({}, "", "/");
  renderedSettingsKey = null;
  await refresh();
});
document.getElementById("delete-sensor").addEventListener("click", async () => {
  if (!selectedSensorId || !selectedSensor) return;
  const displayName = selectedSensor.display_name || selectedSensor.sensor_id;
  if (!window.confirm(`Delete ${displayName} and all of its stored history?`)) return;
  const response = await fetch(`/api/sensors/${encodeURIComponent(selectedSensorId)}`, { method: "DELETE" });
  if (!response.ok) {
    const payload = await response.json();
    document.getElementById("sensor-settings-message").textContent = payload.error || "Could not delete sensor";
    return;
  }
  history.pushState({}, "", "/");
  renderedSettingsKey = null;
  await refresh();
});
document.getElementById("settings-link").addEventListener("click", (event) => {
  event.preventDefault();
  navigate("/settings");
});
document.getElementById("fleet-link").addEventListener("click", (event) => {
  event.preventDefault();
  navigate("/");
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
document.getElementById("settings-sensor-list").addEventListener("change", (event) => {
  const toggle = event.target.closest(".console-toggle");
  if (toggle) setSensorConsole(toggle.dataset.sensorId, toggle.checked);
});
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
  document.getElementById("onboarding-candidates").dataset.renderKey = "";
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
loadProfiles().then(refreshHouseholdNetwork).then(poll).then(restoreClockScale);
setInterval(renderSimulationClock, 100);