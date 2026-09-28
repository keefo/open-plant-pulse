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
  ["sensors", "rooms", "wifi", "firmware"].forEach((name) => {
    setHidden(document.getElementById("settings-" + name + "-panel"), tab !== name);
    setClass(document.getElementById("settings-tab-" + name), "active", tab === name);
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
  setData(list, "renderKey", renderKey);
  list.replaceChildren();
  if (!rooms.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    setText(empty, "No rooms yet. Add the first one above.");
    list.append(empty);
    return;
  }
  rooms.forEach((room) => {
    const card = document.createElement("div");
    card.className = "settings-sensor";

    const heading = document.createElement("div");
    heading.className = "settings-sensor-heading";
    const name = document.createElement("strong");
    setText(name, room.name);
    const count = document.createElement("small");
    setText(count,
      room.sensor_count === 1 ? "1 sensor" : room.sensor_count + " sensors");
    heading.append(name, count);

    const detail = document.createElement("code");
    setText(detail, describeRoom(room));

    card.append(heading, detail);
    if (room.notes) {
      const notes = document.createElement("span");
      setText(notes, room.notes);
      card.append(notes);
    }

    const actions = document.createElement("div");
    actions.className = "inline-control";
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "edit-room";
    setData(edit, "roomId", room.room_id);
    setText(edit, "Edit");
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "danger delete-room";
    setData(remove, "roomId", room.room_id);
    setText(remove, "Delete");
    // A room still holding sensors cannot be deleted, and saying so before the
    // click is kinder than refusing after it.
    setDisabled(remove, room.sensor_count > 0);
    remove.title = room.sensor_count > 0 ? "Move its sensors out first" : "";
    actions.append(edit, remove);
    card.append(actions);
    list.append(card);
  });
}

/* The room is picked, never typed, so two sensors in one room always agree on
 * which room that is. */
function renderRoomChoices() {
  fillRoomSelect(document.getElementById("detail-setting-room"));
  const select = document.getElementById("onboarding-room");
  if (select === null) return;
  const chosen = select.value;
  const renderKey = JSON.stringify(rooms.map((room) => [room.room_id, room.name]));
  if (select.dataset.renderKey !== renderKey) {
    setData(select, "renderKey", renderKey);
    select.replaceChildren();
    rooms.forEach((room) => {
      const option = document.createElement("option");
      option.value = room.room_id;
      setText(option, room.name);
      select.append(option);
    });
    if (chosen) select.value = chosen;
  }
  const note = document.getElementById("onboarding-room-note");
  const current = rooms.find((room) => String(room.room_id) === select.value);
  setDisabled(select, rooms.length === 0);
  setText(note, rooms.length
    ? current
      ? describeRoom(current)
      : ""
    : "No rooms yet. Add one in Settings first.");
}

function fillRoomSelect(select) {
  if (select === null) return;
  const renderKey = JSON.stringify(rooms.map((room) => [room.room_id, room.name]));
  if (select.dataset.renderKey === renderKey) return;
  const chosen = select.value;
  setData(select, "renderKey", renderKey);
  select.replaceChildren();
  rooms.forEach((room) => {
    const option = document.createElement("option");
    option.value = room.room_id;
    setText(option, room.name);
    select.append(option);
  });
  if (chosen) select.value = chosen;
  setDisabled(select, rooms.length === 0);
}

function startRoomEdit(roomId) {
  const room = rooms.find((candidate) => candidate.room_id === roomId);
  if (!room) return;
  document.getElementById("room-id").value = room.room_id;
  document.getElementById("room-name").value = room.name;
  document.getElementById("room-aspect").value = room.aspect;
  document.getElementById("room-light").value = room.light;
  document.getElementById("room-notes").value = room.notes || "";
  setText(document.getElementById("room-form-title"), "Edit " + room.name);
  setText(document.getElementById("save-room"), "Save room");
  setHidden(document.getElementById("cancel-room-edit"), false);
  setText(document.getElementById("room-message"), "");
}

function clearRoomForm() {
  document.getElementById("room-id").value = "";
  document.getElementById("room-name").value = "";
  document.getElementById("room-aspect").value = "unknown";
  document.getElementById("room-light").value = "unknown";
  document.getElementById("room-notes").value = "";
  setText(document.getElementById("room-form-title"), "Add a room");
  setText(document.getElementById("save-room"), "Add room");
  setHidden(document.getElementById("cancel-room-edit"), true);
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
    setText(message, payload.error || "Could not save the room");
    return;
  }
  clearRoomForm();
  setText(message, "Saved.");
  await refreshRooms();
}

async function deleteRoom(roomId) {
  const response = await fetch("/api/rooms/" + roomId, { method: "DELETE" });
  if (!response.ok) {
    const payload = await response.json();
    setText(document.getElementById("room-message"),
      payload.error || "Could not delete the room");
    return;
  }
  await refreshRooms();
}

function describeWifi(sensor) {
  if (!sensor.wifi_enabled) return "Off · the sensor is not on the network";
  if (sensor.wifi_state === "joined") {
    return "On · reachable at " + (sensor.wifi_address || "its address");
  }
  if (sensor.wifi_state === "failed") {
    return "Failed · " + (WIFI_FAILURE_TEXT[sensor.wifi_failure] || "the sensor could not join");
  }
  // Only the sensor can say its console is on, and it says so by reporting the
  // address it joined at. Before that this is a request, and it says so rather
  // than claiming the switch did something it has not yet done.
  return "Requested · not confirmed by the sensor yet";
}

function renderSettingsSensors() {
  const list = document.getElementById("settings-sensor-list");
  const renderKey = JSON.stringify(
    fleetSensors.map((sensor) => [
      sensor.sensor_id,
      sensor.display_name,
      sensor.room,
      sensor.onboarding_state,
      sensor.firmware_version,
      sensor.station_checked_at,
    ])
  );
  if (renderedSettingsSensorsKey === renderKey) return;
  renderedSettingsSensorsKey = renderKey;
  list.replaceChildren();
  if (!fleetSensors.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    setText(empty, "No sensors yet. Power one on and add it.");
    list.append(empty);
    return;
  }
  fleetSensors.forEach((sensor) => {
    const card = document.createElement("div");
    card.className = "settings-sensor";
    setData(card, "sensorId", sensor.sensor_id);

    const heading = document.createElement("div");
    heading.className = "settings-sensor-heading";
    const name = document.createElement("strong");
    setText(name, sensor.display_name || sensor.sensor_id);
    const state = document.createElement("small");
    setText(state, sensor.onboarding_state === "onboarded" ? "Paired" : "Not paired");
    heading.append(name, state);

    const identity = document.createElement("code");
    setText(identity,
      sensor.sensor_id + " · " + (sensor.room || "no room") + " · " + describeFirmware(sensor));

    const forget = document.createElement("button");
    forget.type = "button";
    forget.className = "danger forget-sensor";
    setData(forget, "sensorId", sensor.sensor_id);
    setText(forget, "Forget sensor");

    const configure = document.createElement("a");
    configure.className = "secondary-action";
    setAttr(configure, "href", "/sensors/" + encodeURIComponent(sensor.sensor_id) + "/settings");
    setText(configure, "Configure");

    // This list says which sensors the hub has and lets one be opened or let
    // go. The console switch lives on the sensor's own page, with the rest of
    // what it can be told to do, rather than in two places.
    const actions = document.createElement("div");
    actions.className = "settings-sensor-actions";
    actions.append(configure, forget);
    const body = document.createElement("div");
    body.className = "settings-sensor-body";
    body.append(heading, identity);
    card.append(body, actions);
    list.append(card);
  });
}

/* The version is what the sensor last said, not what it is running now, and the
 * two differ the moment it is reflashed. Say which it is rather than presenting
 * a cached answer as current. */
function describeFirmware(sensor) {
  if (!sensor.firmware_version) return "firmware not reported yet";
  const age = sensor.station_checked_at
    ? Math.round((Date.now() - Date.parse(sensor.station_checked_at)) / 1000)
    : null;
  if (age === null) return "firmware " + sensor.firmware_version;
  if (age < 120) return "firmware " + sensor.firmware_version;
  if (age < 3600) return "firmware " + sensor.firmware_version + " as of " + Math.round(age / 60) + "m ago";
  return "firmware " + sensor.firmware_version + " as of " + Math.round(age / 3600) + "h ago";
}

function renderHouseholdNetwork() {
  const ssidField = document.getElementById("wifi-ssid");
  if (!householdNetworkDraft && householdNetwork !== null) {
    const saved = householdNetwork.wifi_ssid || "";
    if (ssidField.value !== saved) ssidField.value = saved;
    // Mirror it onto the attribute as well, so the saved network is visible in
    // the serialised DOM rather than only as a live property.
    setAttr(ssidField, "value", ssidField.value);
  }
  const using = fleetSensors.filter((sensor) => sensor.wifi_enabled).length;
  setText(document.getElementById("wifi-usage-count"), using
    ? using + " of " + fleetSensors.length + " sensors is using it"
    : "No sensors are using it");
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
    setText(message, payload.error || "Could not save the network");
    return;
  }
  householdNetwork = payload;
  householdNetworkDraft = false;
  setText(message, "Saved. Sensors with their console on will report back.");
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
  setText(document.getElementById("household-network-message"),
    "Network forgotten. Every console is now off.");
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
    setText(document.getElementById("household-network-message"),
      payload.error || "Could not change the console");
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
 * That rules out entries the inbox legitimately holds but nobody can pair: one
 * that reached the hub without a radio, and one last heard from days ago, which
 * is not in the room. Offering either would fail at the pairing step with no way
 * for anyone to tell why. */
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
  setClass(line, "scanning", scanning);
  setClass(line, "stalled", !scanning);
  if (scanning) {
    const count = onboardingCandidates().length;
    setText(text, count
      ? "Scanning · " + count + (count === 1 ? " sensor found" : " sensors found")
      : "Scanning for sensors nearby…");
    return;
  }
  if (status === "disabled") {
    setText(text, "Bluetooth collection is switched off, so no sensor can be found.");
    return;
  }
  setText(text,
    "The Bluetooth scanner is not running (" +
    status +
    "), so no sensor can be found" +
    (scannerHealth && scannerHealth.last_error ? ": " + scannerHealth.last_error : "."));
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
  setText(document.getElementById("onboarding-step-label"),
    "Step " + (index + 1) + " of 4");
  const progress = document.getElementById("onboarding-progress");
  setAttr(progress, "aria-label", "Step " + (index + 1) + " of 4");
  if (renderKeyChanged(progress, index)) {
    progress.replaceChildren();
    ONBOARDING_STEPS.forEach((_, position) => {
      const bar = document.createElement("i");
      bar.className = position <= index ? "progress-bar done" : "progress-bar";
      progress.append(bar);
    });
  }

  setHidden(document.getElementById("onboarding-step-find"), onboardingStep !== "find");
  setHidden(document.getElementById("onboarding-step-pair"), onboardingStep !== "pair");
  setHidden(document.getElementById("onboarding-step-details"), onboardingStep !== "details");
  setHidden(document.getElementById("onboarding-step-done"), onboardingStep !== "done");

  const back = document.getElementById("onboarding-back");
  const next = document.getElementById("onboarding-next");
  setText(back, onboardingStep === "find" ? "Cancel" : "Back");
  setHidden(back, onboardingStep === "done");
  setText(next, {
    find: "Continue",
    pair: "Claimed — continue",
    details: "Finish",
    done: "Done",
  }[onboardingStep]);
  setDisabled(next, onboardingStep === "find" && !onboardingSensorId);
  setText(document.getElementById("onboarding-hint"), {
    find: "Not seeing it? Move the sensor closer to this computer.",
    pair: "Nothing to enter. If this does not finish, check that Bluetooth is on.",
    details: "Everything here can be changed later in Settings.",
    done: "Readings appear on the Plants page as they arrive.",
  }[onboardingStep]);

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
  setData(container, "renderKey", renderKey);
  container.replaceChildren();
  if (!candidates.length) {
    const waiting = document.createElement("p");
    waiting.className = "empty-state";
    setText(waiting,
      "Listening for a new sensor… Power one on and keep it near this computer.");
    container.append(waiting);
    return;
  }
  candidates.forEach((sensor) => {
    const choice = document.createElement("button");
    choice.type = "button";
    choice.className = "onboarding-candidate";
    setData(choice, "sensorId", sensor.sensor_id);
    if (sensor.sensor_id === onboardingSensorId) choice.classList.add("selected");
    const name = document.createElement("strong");
    setText(name, "New sensor");
    const identity = document.createElement("code");
    setText(identity, sensor.sensor_id);
    const signal = document.createElement("span");
    setText(signal,
      describeSignal(sensor.latest_rssi) + " \u00b7 " + describeLastHeard(sensor.seen_age_seconds));
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
      setText(option, entry[1]);
      interval.append(option);
    });
  }
  const configured = Boolean(householdNetwork && householdNetwork.wifi_ssid);
  const toggle = document.getElementById("onboarding-wifi");
  setDisabled(toggle, !configured);
  if (!configured) toggle.checked = false;
  setText(document.getElementById("onboarding-wifi-note"), configured
    ? "Joins " +
      householdNetwork.wifi_ssid +
      ", the network saved in Settings. Readings reach the hub over Bluetooth either way."
    : "No household network is saved yet, so the console cannot be switched on. Add one in Settings.");
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

  setText(document.getElementById("onboarding-done-title"),
    (live.display_name || live.sensor_id) + " is set up");

  const rows = [
    { state: "done", label: "Claimed", text: "Encrypted Bluetooth link", meta: live.sensor_id },
  ];
  if (live.wifi_enabled) {
    rows.push(consoleRow(live));
  }
  rows.push(readingRow(live));

  const renderKey = JSON.stringify(rows);
  if (summary.dataset.renderKey === renderKey) return;
  setData(summary, "renderKey", renderKey);
  summary.replaceChildren();
  rows.forEach((row) => {
    const line = document.createElement("div");
    line.className = "onboarding-summary-row " + row.state;
    const tag = document.createElement("small");
    setText(tag, row.label);
    const text = document.createElement("span");
    setText(text, row.text);
    const meta = document.createElement("code");
    setText(meta, row.meta || "");
    if (row.state === "waiting") {
      const spinner = document.createElement("i");
      spinner.className = "row-spinner";
      setAttr(spinner, "aria-hidden", "true");
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
      ? "Requested \u00b7 the sensor has still not confirmed"
      : "Requested \u00b7 not confirmed by the sensor yet",
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
  setText(message, "");
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
    setText(message, payload.error || "Could not finish setting up this sensor");
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
