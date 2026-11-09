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

function renderSettingsTab() {
  const tab = settingsTabFromLocation();
  // Remembered as it is shown, so arriving at a bare /settings next
  // time lands back where the work was left. Written here rather than
  // on the click, so a tab reached by typing its address or following
  // a link is remembered too.
  rememberSettingsTab(tab);
  SETTINGS_TABS.forEach((name) => {
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
}

function tally(howMany, thing) {
  const count = howMany || 0;
  return `${count} ${thing}${count === 1 ? "" : "s"}`;
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
    setText(count, [tally(room.sensor_count, "sensor"),
                    tally(room.plant_count, "plant")].join(" \u00b7 "));
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
    const edit = document.createElement("a");
    edit.className = "open-room";
    edit.href = `/rooms/${room.room_id}`;
    setText(edit, "Edit");
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "danger delete-room";
    setData(remove, "roomId", room.room_id);
    setText(remove, "Delete");
    // Plants as well as sensors: both hold a room in place, and saying
    // so before the click is kinder than refusing after it.
    const holding = [room.plant_count ? tally(room.plant_count, "plant") : null,
                     room.sensor_count ? tally(room.sensor_count, "sensor") : null]
      .filter(Boolean);
    setDisabled(remove, holding.length > 0);
    remove.title = holding.length
      ? `Move its ${holding.join(" and ")} out first`
      : "";
    actions.append(edit, remove);
    card.append(actions);
    list.append(card);
  });
}







async function deleteRoom(roomId) {
  const message = document.getElementById("room-message");
  setText(message, "");
  let response;
  try {
    response = await fetch("/api/rooms/" + roomId, { method: "DELETE" });
  } catch (_error) {
    setText(message, "The hub could not be reached. The room was not deleted.");
    return;
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    setText(message, payload.error || "Could not delete the room");
    return;
  }
  await refreshRooms();
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
  /* Shown once it holds the network's name. An empty box reads as a
     network nobody has set, which is a different thing from one that
     has not arrived yet. */
  setHidden(document.getElementById("household-network-form"), householdNetwork === null);
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













