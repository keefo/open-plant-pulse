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
  ["sensors", "rooms", "wifi"].forEach((name) => {
    document.getElementById("settings-" + name + "-panel").hidden = tab !== name;
    document.getElementById("settings-tab-" + name).classList.toggle("active", tab === name);
  });
}

const ROOM_ASPECT_TEXT = {
  unknown: "aspect not recorded",
  none: "no windows",
  several: "windows on several sides",
  north: "faces north",
  north_east: "faces north-east",
  east: "faces east",
  south_east: "faces south-east",
  south: "faces south",
  south_west: "faces south-west",
  west: "faces west",
  north_west: "faces north-west",
};
const ROOM_LIGHT_TEXT = {
  unknown: "daylight not recorded",
  low: "low daylight",
  medium: "medium daylight",
  bright: "bright",
};

function describeRoom(room) {
  return ROOM_ASPECT_TEXT[room.aspect] + " \u00b7 " + ROOM_LIGHT_TEXT[room.light];
}

async function refreshRooms() {
  const response = await fetch("/api/rooms");
  if (!response.ok) return;
  rooms = (await response.json()).items;
  renderRooms();
  renderRoomChoices();
}

function renderRooms() {
  const list = document.getElementById("room-list");
  if (list === null) return;
  const renderKey = JSON.stringify(rooms);
  if (list.dataset.renderKey === renderKey) return;
  list.dataset.renderKey = renderKey;
  list.textContent = "";
  if (!rooms.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "No rooms yet. Add the first one above.";
    list.append(empty);
    return;
  }
  rooms.forEach((room) => {
    const card = document.createElement("div");
    card.className = "settings-sensor";

    const heading = document.createElement("div");
    heading.className = "settings-sensor-heading";
    const name = document.createElement("strong");
    name.textContent = room.name;
    const count = document.createElement("small");
    count.textContent =
      room.sensor_count === 1 ? "1 sensor" : room.sensor_count + " sensors";
    heading.append(name, count);

    const detail = document.createElement("code");
    detail.textContent = describeRoom(room);

    card.append(heading, detail);
    if (room.notes) {
      const notes = document.createElement("span");
      notes.textContent = room.notes;
      card.append(notes);
    }

    const actions = document.createElement("div");
    actions.className = "inline-control";
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "edit-room";
    edit.dataset.roomId = room.room_id;
    edit.textContent = "Edit";
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "danger delete-room";
    remove.dataset.roomId = room.room_id;
    remove.textContent = "Delete";
    // A room still holding sensors cannot be deleted, and saying so before the
    // click is kinder than refusing after it.
    remove.disabled = room.sensor_count > 0;
    remove.title = room.sensor_count > 0 ? "Move its sensors out first" : "";
    actions.append(edit, remove);
    card.append(actions);
    list.append(card);
  });
}

/* The room is picked, never typed, so two sensors in one room always agree on
 * which room that is. */
function renderRoomChoices() {
  const select = document.getElementById("onboarding-room");
  if (select === null) return;
  const chosen = select.value;
  const renderKey = JSON.stringify(rooms.map((room) => [room.room_id, room.name]));
  if (select.dataset.renderKey !== renderKey) {
    select.dataset.renderKey = renderKey;
    select.textContent = "";
    rooms.forEach((room) => {
      const option = document.createElement("option");
      option.value = room.room_id;
      option.textContent = room.name;
      select.append(option);
    });
    if (chosen) select.value = chosen;
  }
  const note = document.getElementById("onboarding-room-note");
  const current = rooms.find((room) => String(room.room_id) === select.value);
  select.disabled = rooms.length === 0;
  note.textContent = rooms.length
    ? current
      ? describeRoom(current)
      : ""
    : "No rooms yet. Add one in Settings first.";
}

function startRoomEdit(roomId) {
  const room = rooms.find((candidate) => candidate.room_id === roomId);
  if (!room) return;
  document.getElementById("room-id").value = room.room_id;
  document.getElementById("room-name").value = room.name;
  document.getElementById("room-aspect").value = room.aspect;
  document.getElementById("room-light").value = room.light;
  document.getElementById("room-notes").value = room.notes || "";
  document.getElementById("room-form-title").textContent = "Edit " + room.name;
  document.getElementById("save-room").textContent = "Save room";
  document.getElementById("cancel-room-edit").hidden = false;
  document.getElementById("room-message").textContent = "";
}

function clearRoomForm() {
  document.getElementById("room-id").value = "";
  document.getElementById("room-name").value = "";
  document.getElementById("room-aspect").value = "unknown";
  document.getElementById("room-light").value = "unknown";
  document.getElementById("room-notes").value = "";
  document.getElementById("room-form-title").textContent = "Add a room";
  document.getElementById("save-room").textContent = "Add room";
  document.getElementById("cancel-room-edit").hidden = true;
}

async function saveRoom(event) {
  event.preventDefault();
  const message = document.getElementById("room-message");
  const roomId = document.getElementById("room-id").value;
  const body = JSON.stringify({
    name: document.getElementById("room-name").value.trim(),
    aspect: document.getElementById("room-aspect").value,
    light: document.getElementById("room-light").value,
    notes: document.getElementById("room-notes").value.trim(),
  });
  const response = await fetch(roomId ? "/api/rooms/" + roomId : "/api/rooms", {
    method: roomId ? "PUT" : "POST",
    headers: { "Content-Type": "application/json" },
    body,
  });
  const payload = await response.json();
  if (!response.ok) {
    message.textContent = payload.error || "Could not save the room";
    return;
  }
  clearRoomForm();
  message.textContent = "Saved.";
  await refreshRooms();
}

async function deleteRoom(roomId) {
  const response = await fetch("/api/rooms/" + roomId, { method: "DELETE" });
  if (!response.ok) {
    const payload = await response.json();
    document.getElementById("room-message").textContent =
      payload.error || "Could not delete the room";
    return;
  }
  await refreshRooms();
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
/* Ten seconds is the agreed ceiling for the list to react, and an unclaimed
 * sensor announces itself every three, so this is three missed beacons: long
 * enough to survive a dropped advertisement, short enough that plugging a sensor
 * in or pulling it out is reflected while somebody is still watching. */
const ONBOARDING_CANDIDATE_MAX_AGE_SECONDS = 10;

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
    pair: "Claimed — continue",
    details: "Finish",
    done: "Done",
  }[onboardingStep];
  next.disabled = onboardingStep === "find" && !onboardingSensorId;
  document.getElementById("onboarding-hint").textContent = {
    find: "Not seeing it? Move the sensor closer to this computer.",
    pair: "Nothing to enter. If this does not finish, check that Bluetooth is on.",
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

/* The last step keeps working after it is reached.
 *
 * Claiming finishes in a moment, but the console has to join a network and the
 * first reading has to arrive, and both take longer than a person takes to read
 * the page. Freezing the summary at the instant Finish was pressed made it
 * assert things that had not happened yet, so it re-reads the sensor on every
 * poll and each row says where it has actually got to. */
function renderOnboardingSummary() {
  const summary = document.getElementById("onboarding-summary");
  if (!onboardingResult) return;
  const live =
    fleetSensors.find((sensor) => sensor.sensor_id === onboardingResult.sensor_id) ||
    onboardingResult;

  document.getElementById("onboarding-done-title").textContent =
    (live.display_name || live.sensor_id) + " is set up";

  const rows = [
    { state: "done", label: "Claimed", text: "Encrypted Bluetooth link", meta: live.sensor_id },
  ];
  if (live.wifi_enabled) {
    rows.push(consoleRow(live));
  }
  rows.push(readingRow(live));

  const renderKey = JSON.stringify(rows);
  if (summary.dataset.renderKey === renderKey) return;
  summary.dataset.renderKey = renderKey;
  summary.textContent = "";
  rows.forEach((row) => {
    const line = document.createElement("div");
    line.className = "onboarding-summary-row " + row.state;
    const tag = document.createElement("small");
    tag.textContent = row.label;
    const text = document.createElement("span");
    text.textContent = row.text;
    const meta = document.createElement("code");
    meta.textContent = row.meta || "";
    if (row.state === "waiting") {
      const spinner = document.createElement("i");
      spinner.className = "row-spinner";
      spinner.setAttribute("aria-hidden", "true");
      line.append(tag, spinner, text, meta);
    } else {
      line.append(tag, text, meta);
    }
    summary.append(line);
  });
}

function consoleRow(sensor) {
  if (sensor.wifi_state === "joined") {
    return {
      state: "done",
      label: "Joined",
      text: "Web console reachable",
      meta: sensor.wifi_address || "",
    };
  }
  if (sensor.wifi_state === "failed") {
    return {
      state: "failed",
      label: "Failed",
      text: WIFI_FAILURE_TEXT[sensor.wifi_failure] || "The sensor could not join",
      meta: "",
    };
  }
  return {
    state: "waiting",
    label: "Pending",
    text: waitedTooLong()
      ? "Web console on \u00b7 the sensor has not confirmed yet"
      : "Web console on \u00b7 waiting for the sensor",
    meta: "",
  };
}

/* Onboarding proves the sensor reached the hub, not that its probe works.
 *
 * A sensor announcing itself with nothing to measure has still done everything
 * setup is about: it is claimed, it is in range, and the hub is hearing it. A
 * missing probe is a separate fault, reported as a note rather than as a failed
 * setup. */
function readingRow(sensor) {
  const heardAt =
    typeof sensor.seen_age_seconds === "number"
      ? Date.now() - sensor.seen_age_seconds * 1000
      : null;
  const reportedSinceSetup =
    heardAt !== null && onboardingFinishedAt !== null && heardAt >= onboardingFinishedAt - 2000;

  if (!reportedSinceSetup) {
    if (waitedTooLong()) {
      return {
        state: "failed",
        label: "Silent",
        text: "Nothing heard from the sensor \u00b7 check it is powered and in range",
        meta: "",
      };
    }
    return {
      state: "waiting",
      label: "Waiting",
      text: "Waiting for the sensor to report",
      meta: "",
    };
  }

  const latest = sensor.latest;
  const measured =
    latest &&
    latest.received_at &&
    onboardingFinishedAt !== null &&
    Date.parse(latest.received_at) >= onboardingFinishedAt;
  return {
    state: "done",
    label: "Reporting",
    text: measured
      ? describeReading(latest.reading)
      : "Reporting in \u00b7 no measurements yet, its probe is not reading",
    meta: describeLastHeard(sensor.seen_age_seconds),
  };
}

function describeReading(reading) {
  if (!reading) return "First reading stored";
  const parts = [];
  if (typeof reading.air_temperature_c === "number") {
    parts.push(reading.air_temperature_c.toFixed(1) + " \u00b0C");
  }
  if (typeof reading.air_humidity_percent === "number") {
    parts.push(Math.round(reading.air_humidity_percent) + "% humidity");
  }
  if (typeof reading.moisture_percent === "number") {
    parts.push(Math.round(reading.moisture_percent) + "% moisture");
  }
  return parts.length ? parts.join(" \u00b7 ") : "First reading stored";
}

// Long enough that a slow but working setup is not accused of failing.
const ONBOARDING_PATIENCE_MS = 60000;

function waitedTooLong() {
  return onboardingFinishedAt !== null && Date.now() - onboardingFinishedAt > ONBOARDING_PATIENCE_MS;
}

async function finishOnboarding() {
  const message = document.getElementById("onboarding-message");
  message.textContent = "";
  const response = await fetch("/api/sensors/" + encodeURIComponent(onboardingSensorId), {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      display_name: document.getElementById("onboarding-name").value.trim(),
      room_id: Number(document.getElementById("onboarding-room").value) || null,
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
  // Anything that arrives from here on is a result of this setup, which is what
  // lets the reading row tell a new reading from one stored days ago.
  onboardingFinishedAt = Date.now();
  return true;
}

function resetOnboarding() {
  onboardingStep = "find";
  onboardingSensorId = null;
  onboardingDraft = false;
  onboardingResult = null;
  onboardingFinishedAt = null;
  const summary = document.getElementById("onboarding-summary");
  if (summary) summary.dataset.renderKey = "";
  const container = document.getElementById("onboarding-candidates");
  if (container) container.dataset.renderKey = "";
}
