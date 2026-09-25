// Settings and the guided add-a-sensor flow.
//
// Loaded before app.js, which owns the shared state these functions read and the
// listeners that call them. Kept separate because the flow is a distinct surface
// from the monitoring dashboard, not because it is independent of it.

const WIFI_FAILURE_TEXT = {
  wrong_password: "Wrong password",
  network_not_found: "Network not found",
  association_timeout: "Timed out joining",
  no_address: "No address received",
  unsupported_band: "Network is not 2.4 GHz",
};
const ONBOARDING_STEPS = ["find", "pair", "details", "done"];

function renderSettingsTab() {
  const tab = settingsTabFromLocation();
  document.getElementById("settings-sensors-panel").hidden = tab !== "sensors";
  document.getElementById("settings-wifi-panel").hidden = tab !== "wifi";
  document.getElementById("settings-tab-sensors").classList.toggle("active", tab === "sensors");
  document.getElementById("settings-tab-wifi").classList.toggle("active", tab === "wifi");
}

function describeWifi(sensor) {
  if (!sensor.wifi_enabled) return "Web console off · radio idle";
  if (sensor.wifi_state === "joined") {
    return "Web console on · " + (sensor.wifi_address || "joined");
  }
  if (sensor.wifi_state === "failed") {
    return "Web console on · " + (WIFI_FAILURE_TEXT[sensor.wifi_failure] || "did not join");
  }
  return "Web console on · waiting for the sensor";
}

function renderSettingsSensors() {
  const list = document.getElementById("settings-sensor-list");
  const renderKey = JSON.stringify(
    fleetSensors.map((sensor) => [
      sensor.sensor_id,
      sensor.display_name,
      sensor.room,
      sensor.onboarding_state,
      sensor.wifi_enabled,
      sensor.wifi_state,
      sensor.wifi_failure,
      sensor.wifi_address,
    ])
  );
  if (renderedSettingsSensorsKey === renderKey) return;
  renderedSettingsSensorsKey = renderKey;
  list.textContent = "";
  if (!fleetSensors.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "No sensors yet. Power one on and add it.";
    list.append(empty);
    return;
  }
  fleetSensors.forEach((sensor) => {
    const card = document.createElement("div");
    card.className = "settings-sensor";
    card.dataset.sensorId = sensor.sensor_id;

    const heading = document.createElement("div");
    heading.className = "settings-sensor-heading";
    const name = document.createElement("strong");
    name.textContent = sensor.display_name || sensor.sensor_id;
    const state = document.createElement("small");
    state.textContent = sensor.onboarding_state === "onboarded" ? "Paired" : "Not paired";
    heading.append(name, state);

    const identity = document.createElement("code");
    identity.textContent = sensor.sensor_id + " · " + (sensor.room || "no room");

    const consoleRow = document.createElement("label");
    consoleRow.className = "checkbox";
    const toggle = document.createElement("input");
    toggle.type = "checkbox";
    toggle.checked = Boolean(sensor.wifi_enabled);
    toggle.dataset.sensorId = sensor.sensor_id;
    toggle.className = "console-toggle";
    consoleRow.append(toggle, document.createTextNode(" " + describeWifi(sensor)));

    const forget = document.createElement("button");
    forget.type = "button";
    forget.className = "danger forget-sensor";
    forget.dataset.sensorId = sensor.sensor_id;
    forget.textContent = "Forget sensor";

    card.append(heading, identity, consoleRow, forget);
    list.append(card);
  });
}

function renderHouseholdNetwork() {
  const ssidField = document.getElementById("wifi-ssid");
  if (!householdNetworkDraft && householdNetwork !== null) {
    ssidField.value = householdNetwork.wifi_ssid || "";
    // Mirror it onto the attribute as well, so the saved network is visible in
    // the serialised DOM rather than only as a live property.
    ssidField.setAttribute("value", ssidField.value);
  }
  const using = fleetSensors.filter((sensor) => sensor.wifi_enabled).length;
  document.getElementById("wifi-usage-count").textContent = using
    ? using + " of " + fleetSensors.length + " sensors is using it"
    : "No sensors are using it";
}

async function refreshHouseholdNetwork() {
  const response = await fetch("/api/settings/wifi");
  if (!response.ok) return;
  householdNetwork = await response.json();
  renderHouseholdNetwork();
}

async function saveHouseholdNetwork(event) {
  event.preventDefault();
  const message = document.getElementById("household-network-message");
  const ssid = document.getElementById("wifi-ssid").value.trim();
  const response = await fetch("/api/settings/wifi", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ wifi_ssid: ssid }),
  });
  const payload = await response.json();
  if (!response.ok) {
    message.textContent = payload.error || "Could not save the network";
    return;
  }
  householdNetwork = payload;
  householdNetworkDraft = false;
  message.textContent = "Saved. Sensors with their console on will report back.";
  renderedSettingsSensorsKey = null;
  await refreshFleet();
}

async function forgetHouseholdNetwork() {
  const response = await fetch("/api/settings/wifi", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ wifi_ssid: null }),
  });
  if (!response.ok) return;
  householdNetwork = await response.json();
  householdNetworkDraft = false;
  document.getElementById("wifi-ssid").value = "";
  document.getElementById("wifi-password").value = "";
  document.getElementById("household-network-message").textContent =
    "Network forgotten. Every console is now off.";
  renderedSettingsSensorsKey = null;
  await refreshFleet();
}

async function setSensorConsole(sensorId, enabled) {
  const response = await fetch("/api/sensors/" + encodeURIComponent(sensorId) + "/wifi", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
  const payload = await response.json();
  if (!response.ok) {
    document.getElementById("household-network-message").textContent =
      payload.error || "Could not change the console";
  }
  renderedSettingsSensorsKey = null;
  await refreshFleet();
  return response.ok;
}

async function forgetSensor(sensorId) {
  const sensor = fleetSensors.find((candidate) => candidate.sensor_id === sensorId);
  const name = (sensor && sensor.display_name) || sensorId;
  const confirmed = window.confirm(
    "Forget " +
      name +
      "?\n\nIt returns to the list of new sensors and has to be added again with " +
      "the code on its label. Its readings are kept."
  );
  if (!confirmed) return;
  await fetch("/api/sensors/" + encodeURIComponent(sensorId) + "/onboarding", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ onboarding_state: "onboarding" }),
  });
  renderedSettingsSensorsKey = null;
  await refreshFleet();
}

/* A sensor is only offered if it could actually be paired now.
 *
 * That rules out two kinds of entry the inbox legitimately holds. The simulator
 * reaches the hub over UDP and has no radio, so there is nothing to pair with.
 * And a sensor last heard from days ago is not in the room: offering it would
 * fail at the pairing step with no way for anyone to tell why. */
/* An unclaimed sensor beacons every ten seconds, so three missed beacons is
 * enough to conclude it is gone. Long enough to survive a lost advertisement,
 * short enough that switching a sensor off removes it from the list while
 * somebody is still looking at it. */
const ONBOARDING_CANDIDATE_MAX_AGE_SECONDS = 35;

/* Show the scanner's real state, not a decorative animation.
 *
 * An indicator that always spins would have hidden this morning's actual fault,
 * where the hub served pages happily while its scanner sat stopped and silent.
 * When nothing is arriving, the most useful thing this step can say is whether
 * the hub is even listening. */
function renderScanState() {
  const line = document.getElementById("onboarding-scan-state");
  const text = document.getElementById("onboarding-scan-text");
  if (line === null || text === null) return;
  const status = scannerHealth ? scannerHealth.status : "unknown";
  const scanning = status === "scanning";
  line.classList.toggle("scanning", scanning);
  line.classList.toggle("stalled", !scanning);
  if (scanning) {
    const count = onboardingCandidates().length;
    text.textContent = count
      ? "Scanning · " + count + (count === 1 ? " sensor found" : " sensors found")
      : "Scanning for sensors nearby…";
    return;
  }
  if (status === "disabled") {
    text.textContent = "Bluetooth collection is switched off, so no sensor can be found.";
    return;
  }
  text.textContent =
    "The Bluetooth scanner is not running (" +
    status +
    "), so no sensor can be found" +
    (scannerHealth && scannerHealth.last_error ? ": " + scannerHealth.last_error : ".");
}

function onboardingCandidates() {
  return unclaimedSensors.filter(
    (sensor) =>
      sensor.onboarding_state === "onboarding" &&
      sensor.transport === "bthome" &&
      typeof sensor.seen_age_seconds === "number" &&
      sensor.seen_age_seconds <= ONBOARDING_CANDIDATE_MAX_AGE_SECONDS
  );
}

function renderOnboarding() {
  const index = ONBOARDING_STEPS.indexOf(onboardingStep);
  document.getElementById("onboarding-step-label").textContent =
    "Step " + (index + 1) + " of 4";
  const progress = document.getElementById("onboarding-progress");
  progress.setAttribute("aria-label", "Step " + (index + 1) + " of 4");
  progress.textContent = "";
  ONBOARDING_STEPS.forEach((_, position) => {
    const bar = document.createElement("i");
    bar.className = position <= index ? "progress-bar done" : "progress-bar";
    progress.append(bar);
  });

  document.getElementById("onboarding-step-find").hidden = onboardingStep !== "find";
  document.getElementById("onboarding-step-pair").hidden = onboardingStep !== "pair";
  document.getElementById("onboarding-step-details").hidden = onboardingStep !== "details";
  document.getElementById("onboarding-step-done").hidden = onboardingStep !== "done";

  const back = document.getElementById("onboarding-back");
  const next = document.getElementById("onboarding-next");
  back.textContent = onboardingStep === "find" ? "Cancel" : "Back";
  back.hidden = onboardingStep === "done";
  next.textContent = {
    find: "Continue",
    pair: "Paired — continue",
    details: "Finish",
    done: "Done",
  }[onboardingStep];
  next.disabled = onboardingStep === "find" && !onboardingSensorId;
  document.getElementById("onboarding-hint").textContent = {
    find: "Not seeing it? Move the sensor closer to this computer.",
    pair: "No window appeared? Check that Bluetooth is on, then start again.",
    details: "Everything here can be changed later in Settings.",
    done: "Readings appear on the Plants page as they arrive.",
  }[onboardingStep];

  if (onboardingStep === "find") {
    renderScanState();
    renderOnboardingCandidates();
  }
  if (onboardingStep === "details") renderOnboardingDetails();
  if (onboardingStep === "done") renderOnboardingSummary();
}

function renderOnboardingCandidates() {
  const container = document.getElementById("onboarding-candidates");
  const candidates = onboardingCandidates();
  const renderKey = JSON.stringify([
    candidates.map((sensor) => [
      sensor.sensor_id,
      sensor.latest_rssi,
      sensor.seen_age_seconds,
    ]),
    onboardingSensorId,
  ]);
  if (container.dataset.renderKey === renderKey) return;
  container.dataset.renderKey = renderKey;
  container.textContent = "";
  if (!candidates.length) {
    const waiting = document.createElement("p");
    waiting.className = "empty-state";
    waiting.textContent =
      "Listening for a new sensor… Power one on and keep it near this computer.";
    container.append(waiting);
    return;
  }
  candidates.forEach((sensor) => {
    const choice = document.createElement("button");
    choice.type = "button";
    choice.className = "onboarding-candidate";
    choice.dataset.sensorId = sensor.sensor_id;
    if (sensor.sensor_id === onboardingSensorId) choice.classList.add("selected");
    const name = document.createElement("strong");
    name.textContent = "New sensor";
    const identity = document.createElement("code");
    identity.textContent = sensor.sensor_id;
    const signal = document.createElement("span");
    signal.textContent =
      describeSignal(sensor.latest_rssi) + " \u00b7 " + describeLastHeard(sensor.seen_age_seconds);
    choice.append(name, identity, signal);
    container.append(choice);
  });
}

// Proximity is not proof of ownership, so the wording says what the signal
// suggests rather than asserting whose sensor it is.
function describeSignal(rssi) {
  if (rssi === null || rssi === undefined) return "signal unknown";
  if (rssi >= -60) return "close by";
  return "far away — possibly a neighbour's";
}

function describeLastHeard(seconds) {
  if (typeof seconds !== "number") return "last heard unknown";
  if (seconds <= 2) return "heard just now";
  return "heard " + seconds + "s ago";
}

function renderOnboardingDetails() {
  const interval = document.getElementById("onboarding-interval");
  if (!interval.options.length) {
    [
      ["1800", "30 minutes"],
      ["3600", "1 hour"],
      ["14400", "4 hours"],
    ].forEach((entry) => {
      const option = document.createElement("option");
      option.value = entry[0];
      option.textContent = entry[1];
      interval.append(option);
    });
  }
  const configured = Boolean(householdNetwork && householdNetwork.wifi_ssid);
  const toggle = document.getElementById("onboarding-wifi");
  toggle.disabled = !configured;
  if (!configured) toggle.checked = false;
  document.getElementById("onboarding-wifi-note").textContent = configured
    ? "Joins " +
      householdNetwork.wifi_ssid +
      ", the network saved in Settings. Readings reach the hub over Bluetooth either way."
    : "No household network is saved yet, so the console cannot be switched on. Add one in Settings.";
}

function renderOnboardingSummary() {
  const summary = document.getElementById("onboarding-summary");
  if (!onboardingResult) return;
  document.getElementById("onboarding-done-title").textContent =
    (onboardingResult.display_name || onboardingResult.sensor_id) + " is set up";
  summary.textContent = "";
  const rows = [["Paired", "Encrypted Bluetooth link", onboardingResult.sensor_id]];
  if (onboardingResult.wifi_enabled) {
    rows.push([
      onboardingResult.wifi_state === "joined" ? "Joined" : "Pending",
      describeWifi(onboardingResult),
      onboardingResult.wifi_address || "",
    ]);
  }
  const latest = onboardingResult.latest;
  rows.push(
    latest && latest.reading
      ? ["Reading", "First reading stored", "just now"]
      : ["Waiting", "First reading", "not yet received"]
  );
  rows.forEach((row) => {
    const line = document.createElement("div");
    line.className = "onboarding-summary-row";
    const tag = document.createElement("small");
    tag.textContent = row[0];
    const text = document.createElement("span");
    text.textContent = row[1];
    const meta = document.createElement("code");
    meta.textContent = row[2];
    line.append(tag, text, meta);
    summary.append(line);
  });
}

async function finishOnboarding() {
  const message = document.getElementById("onboarding-message");
  message.textContent = "";
  const response = await fetch("/api/sensors/" + encodeURIComponent(onboardingSensorId), {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      display_name: document.getElementById("onboarding-name").value.trim(),
      room: document.getElementById("onboarding-room").value.trim(),
      profile_id: document.getElementById("onboarding-profile").value,
      expected_interval_seconds: Number(document.getElementById("onboarding-interval").value),
    }),
  });
  const payload = await response.json();
  if (!response.ok) {
    message.textContent = payload.error || "Could not finish setting up this sensor";
    return false;
  }
  if (document.getElementById("onboarding-wifi").checked) {
    await setSensorConsole(onboardingSensorId, true);
  }
  const refreshed = await fetch("/api/sensors/" + encodeURIComponent(onboardingSensorId));
  onboardingResult = refreshed.ok ? await refreshed.json() : payload;
  return true;
}

function resetOnboarding() {
  onboardingStep = "find";
  onboardingSensorId = null;
  onboardingDraft = false;
  onboardingResult = null;
  const container = document.getElementById("onboarding-candidates");
  if (container) container.dataset.renderKey = "";
}
