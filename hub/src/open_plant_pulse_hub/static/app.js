const fields = {
  "moisture": ["moisture_percent", 1], "soil-temp": ["soil_temperature_c", 1],
  "conductivity": ["conductivity_us_cm", 0]
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
  /* A figure carries its unit beside it, drawn by CSS from data-unit so a
     number and its unit cannot drift apart. A placeholder is not a
     measurement and gets none: "-- ml" reads as a reading that failed,
     rather than one not taken yet. */
  if (element.dataset && element.dataset.unit !== undefined) {
    if (value === "" || value === "--") element.dataset.unitHidden = "true";
    else delete element.dataset.unitHidden;
  }
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
// Whose comfortable range the bands are drawn from: the plant being looked
// at, not whichever sensor happened to take the reading.
let climateProfileId = null;
let drainageAssessments = [];
let renderedCareEventIds = new Set();
let careLogInitialized = false;
let wateringCalendarRequestKey = null;
let wateringIntervalSummary = null;
let selectedSensorId = null;
let selectedSensor = null;
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


/* The Zigbee control panel.
 *
 * Two questions, answered separately: is the gateway there, and what is on
 * it. They are not the same -- an unplugged gateway leaves a serial
 * device that opens and then says nothing forever, which from a device list
 * alone is indistinguishable from a network where nobody has reported yet.
 */
async function refreshZigbee() {
  const response = await fetch("/api/zigbee", { cache: "no-store" });
  if (!response.ok) return;
  const data = await response.json();
  zigbeeDevices = data.devices || [];
  renderZigbeeGateway(data.gateway || {});
  renderZigbeeDevices(zigbeeDevices);
  refreshZigbeeReports();
}

function renderZigbeeGateway(gateway) {
  // A one-line verdict beside the heading, because whether the gateway is
  // there is the first thing worth knowing; the rest is diagnostics and
  // lives in the folded section.
  const brief = document.getElementById("zigbee-gateway-brief");
  if (brief) {
    brief.textContent = gateway.released ? "Dongle released"
      : gateway.connected ? "Dongle connected"
      : "No dongle found";
    setClass(brief, "bad", !gateway.connected && !gateway.released);
  }
  setText(document.getElementById("zigbee-gw-state"),
          gateway.released ? "Released — the hub is leaving the port alone"
          : gateway.connected ? "Connected" : "Not found");
  setText(document.getElementById("zigbee-gw-pan"), gateway.pan || "--");
  setText(document.getElementById("zigbee-gw-channel"),
          gateway.channel === null || gateway.channel === undefined ? "--" : String(gateway.channel));
  setText(document.getElementById("zigbee-gw-count"), String(gateway.devices || 0));
  setText(document.getElementById("zigbee-gw-uptime"),
          gateway.uptime_s == null ? "--" : describeSeconds(gateway.uptime_s));
  renderGatewayRestarts(gateway.restarts || []);

  const table = document.getElementById("zigbee-neighbours");
  const empty = document.getElementById("zigbee-neighbours-empty");
  const neighbours = gateway.neighbours || [];
  if (table) {
    const body = table.querySelector("tbody");
    body.innerHTML = "";
    neighbours.forEach((n) => {
      const tr = document.createElement("tr");
      const role = n.type === 0 ? "dongle" : n.type === 1 ? "router" : "end device";
      [
        n.eui,
        role,
        n.rx_on_idle ? "always on" : "sleepy",
        `${n.rssi} dBm`,
        String(n.lqi),
      ].forEach((value) => {
        const td = document.createElement("td");
        td.textContent = value;
        tr.appendChild(td);
      });
      body.appendChild(tr);
    });
    setHidden(table, neighbours.length === 0);
    setHidden(empty, neighbours.length !== 0);
  }

  const release = document.getElementById("zigbee-release");
  if (release) {
    release.textContent = gateway.released ? "Reconnect the dongle" : "Release the dongle";
    release.dataset.action = gateway.released ? "reconnect" : "release";
  }

  const state = document.getElementById("zigbee-permit-state");
  const radar = document.getElementById("zigbee-radar");
  const button = document.getElementById("zigbee-permit");
  const scanning = Boolean(gateway.permit_s);
  if (state) {
    state.textContent = scanning ? `Listening for ${gateway.permit_s}s more` : "Not scanning";
  }
  if (radar) setHidden(radar, !scanning);
  if (button) {
    button.textContent = scanning ? "Stop scanning" : "Scan for new devices";
    button.dataset.scanning = scanning ? "yes" : "no";
  }
}

/* Two lists, because they answer two different questions.
 *
 * The one above the scan button is the devices this hub uses. The one
 * below it is whatever has turned up on the radio and is waiting to be
 * let in or turned away. They used to be one list with the waiting ones
 * sorted to the top, which put a thing needing a decision in among the
 * things already decided, and made joining look like enrolling.
 */
/* How much of the raw record to keep.
 *
 * The count is shown beside the choice because "for ever" is only a
 * comfortable answer for somebody who can see what for ever amounts to.
 */
async function refreshZigbeeReports() {
  try {
    const response = await fetch("/api/settings");
    if (!response.ok) return;
    const settings = await response.json();
    const choice = document.getElementById("zigbee-reports-keep");
    if (!choice || choice.matches(":focus")) return;
    const days = settings.zigbee_report_keep_days;
    choice.value = days === null || days === undefined ? "forever" : String(days);
    const kept = (settings.zigbee_reports || {}).reports || 0;
    const oldest = (settings.zigbee_reports || {}).oldest_at;
    setText(document.getElementById("zigbee-reports-count"),
            kept ? `${kept.toLocaleString()} kept${oldest ? `, back to ${new Date(oldest)
                     .toLocaleDateString(undefined, { month: "short", day: "numeric" })}` : ""}`
                 : "nothing kept yet");
  } catch (_error) {
    /* the panel is still readable without it */
  }
}

async function saveZigbeeReports() {
  const choice = document.getElementById("zigbee-reports-keep");
  const state = document.getElementById("zigbee-reports-state");
  setText(state, "Saving.");
  try {
    const response = await fetch("/api/settings/zigbee-reports", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ keep_days: choice.value }),
    });
    if (!response.ok) throw new Error(String(response.status));
    setText(state, choice.value === "forever"
      ? "Keeping every report." : `Keeping the last ${choice.value} days.`);
    await refreshZigbeeReports();
  } catch (_error) {
    setText(state, "That did not save.");
  }
}

function renderZigbeeDevices(devices) {
  const inUse = document.getElementById("zigbee-devices");
  const found = document.getElementById("zigbee-found");
  if (!inUse || !found) return;
  const waiting = devices.filter((d) => !d.accepted);
  const settled = devices.filter((d) => d.accepted);

  inUse.innerHTML = "";
  if (!settled.length) {
    const empty = document.createElement("p");
    empty.className = "settings-note";
    empty.textContent = "No devices in use yet. Scan below to find one.";
    inUse.appendChild(empty);
  } else {
    settled.forEach((device) => inUse.appendChild(zigbeeDeviceRow(device)));
  }

  found.innerHTML = "";
  waiting.forEach((device) => found.appendChild(zigbeeWaitingRow(device)));
}

/* What a device says it is. Manufacturer and model come from the Basic
   cluster, which every certified device answers; the device type is from
   its endpoint descriptor. An address identifies a device without
   describing it, and three rows of hexadecimal help nobody. */
/* What each kind of device is, said plainly. The hub's kinds are names
   for drivers -- "soil", "plug" -- and a page is read by somebody who
   thinks in terms of what they bought and where they put it. */
const ZIGBEE_PRODUCT_TYPES = {
  soil: "Soil probe (temperature and moisture)",
  leak: "Water leak sensor",
  thermometer: "Room thermometer (temperature and humidity)",
  plug: "Smart plug (mains switch)",
  scale: "Pot scale (weight)",
};

const ZIGBEE_DEVICE_TYPES = {
  0x0051: "smart plug",
  0x0100: "on/off light",
  0x0101: "dimmable light",
  0x0002: "on/off output",
  0x0006: "switch",
  0x000a: "door lock",
  0x0302: "temperature sensor",
  0x0402: "motion sensor",
  0x0403: "contact sensor",
  0x0770: "air quality sensor",
};

function describeZigbeeDevice(device) {
  const made = [device.manufacturer, device.model].filter(Boolean).join(" ");
  const type = device.kind && device.kind !== "unknown"
    ? device.kind
    : ZIGBEE_DEVICE_TYPES[device.device_type];
  if (made && type) return `${made} \u00b7 ${type}`;
  if (made) return made;
  if (type) return type;
  // Said plainly rather than guessed at. A device that has not been
  // asked yet, or that answered nothing, is not a device of unknown
  // make -- it is one the hub has not got round to interviewing.
  return "not identified yet";
}

function zigbeeWaitingRow(device) {
  const row = document.createElement("div");
  row.className = "settings-sensor zigbee-waiting";

  const body = document.createElement("div");
  body.className = "settings-sensor-body zigbee-row";
  row.appendChild(body);

  // What it is comes first, because that is the whole question being
  // asked. The hub has no name for it yet -- naming happens on the way
  // in -- so the make and model are the only identity there is.
  const name = document.createElement("div");
  name.className = "zigbee-row-name";
  name.textContent = describeZigbeeDevice(device);
  body.appendChild(name);

  const state = document.createElement("div");
  state.className = "zigbee-row-what";
  state.textContent = "Found on the radio. Not added, and not reporting.";
  body.appendChild(state);

  const detail = document.createElement("div");
  detail.className = "zigbee-row-eui";
  detail.textContent = device.readings
    ? `${device.eui} · ${describeSignal(device)}` : device.eui;
  body.appendChild(detail);

  const actions = document.createElement("div");
  actions.className = "zigbee-actions";
  // One way out, not two. "Not mine" archived the row to stop the hub
  // asking again, which made a refusal indistinguishable from a removal
  // and left a device that came back unaddable.
  [["Add it", "accept"], ["Remove", "remove"]].forEach(([label, action]) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    button.className = "zigbee-claim";
    button.dataset.eui = device.eui;
    button.dataset.action = action;
    actions.appendChild(button);
  });
  row.appendChild(actions);
  return row;
}

function zigbeeDeviceRow(device) {
  const row = document.createElement("div");
  row.className = "settings-sensor";

  const body = document.createElement("div");
  body.className = "settings-sensor-body zigbee-row";
  row.appendChild(body);

  const name = document.createElement("div");
  name.className = "zigbee-row-name";
  name.textContent = device.name
    || (device.kind === "unknown" ? "Device" : capitalise(device.kind));
  body.appendChild(name);

  const what = document.createElement("div");
  what.className = "zigbee-row-what";
  what.textContent = describeZigbeeDevice(device);
  body.appendChild(what);

  // Health in words, with a dot that carries the same verdict in colour.
  // "17 readings, 2 rejoins" is a number without a verdict: nothing on
  // the page said whether that was fine.
  const silent = zigbeeIsSilent(device);
  const health = document.createElement("div");
  health.className = silent ? "zigbee-row-status is-silent" : "zigbee-row-status";
  health.textContent = describeZigbeeHealth(device);
  body.appendChild(health);

  const detail = document.createElement("div");
  detail.className = "zigbee-row-eui";
  // The firmware each device is running, where a fleet is read at a
  // glance: one board left behind on an old version is the thing worth
  // spotting here, and opening every device page to find it is not
  // reading, it is searching. Signal joins it only once there has been a
  // packet to measure -- before that the line above has already said so,
  // and saying it twice is the panel repeating itself.
  const parts = [device.eui];
  if (!silent && device.last && device.last.fw) parts.unshift(`v${device.last.fw}`);
  if (device.readings) parts.push(describeSignal(device));
  detail.textContent = parts.join(" · ");
  // The short address is a diagnostic that changes on every rejoin, so it
  // lives in the tooltip rather than competing with the identity.
  detail.title = `short address ${device.short || "unknown"} · `
    + `${device.readings} readings · ${device.joins} joins`
    + (device.failed_commands ? ` · ${device.failed_commands} undelivered commands` : "");
  body.appendChild(detail);

  const actions = document.createElement("div");
  actions.className = "zigbee-actions";
  const control = document.createElement("a");
  control.className = "nav-button";
  control.href = `/zigbee/${device.eui}`;
  control.textContent = "Control";
  control.dataset.zigbeeLink = device.eui;
  actions.appendChild(control);

  const remove = document.createElement("button");
  remove.type = "button";
  remove.textContent = "Remove";
  remove.className = "zigbee-manage danger";
  remove.dataset.eui = device.eui;
  remove.dataset.action = "remove";
  actions.appendChild(remove);
  row.appendChild(actions);
  return row;
}

function capitalise(word) {
  return word.charAt(0).toUpperCase() + word.slice(1);
}

// When a Zigbee device has been quiet long enough to worry about.
//
// This was a flat fifteen minutes, which is the wrong shape: a scale
// reporting every ten seconds has missed ninety of them by then and is
// plainly off, while one set to report twice a day is perfectly healthy.
// Lateness is relative to what the device said it would do -- the reading
// carries its own interval, so there is no need to guess.
const ZIGBEE_SILENT_FLOOR_S = 90;    // below this, normal jitter looks like death
const ZIGBEE_MISSED_REPORTS = 3;     // forgiving, but not for ever
// Continuous sampling reports as fast as the link allows and carries an
// interval of 0, so judge it by the floor rather than by zero.
const ZIGBEE_CONTINUOUS_AS_S = 30;

const ZIGBEE_ASLEEP_AS_S = 1800;     // the check-in the hub asks a sleeper for

function zigbeeSilentAfter(device) {
  const stated = Number(device && device.last && device.last.interval);
  if (Number.isFinite(stated) && stated > 0) {
    return Math.max(ZIGBEE_SILENT_FLOOR_S, stated * ZIGBEE_MISSED_REPORTS);
  }
  // A device that states no interval is judged by how it is powered. A
  // battery end device is meant to go quiet between readings; holding
  // it to a mains device's standard calls every healthy sleeper a
  // fault, which is the one thing this figure must not do.
  const expected = device && device.power_source === "battery"
    ? ZIGBEE_ASLEEP_AS_S : ZIGBEE_CONTINUOUS_AS_S;
  return Math.max(ZIGBEE_SILENT_FLOOR_S, expected * ZIGBEE_MISSED_REPORTS);
}

function zigbeeIsSilent(device) {
  return !device || device.quiet_for_s === null || device.quiet_for_s === undefined
    || device.quiet_for_s > zigbeeSilentAfter(device);
}

function describeSignal(device) {
  // Signal strength is a property of a conversation, not of a device. A
  // device that has said nothing for twenty minutes has no current signal,
  // however well the last reading came through -- and "silent, check it is
  // powered" next to "signal strong" is the page arguing with itself.
  if (!device.readings && (device.rssi === null || device.rssi === undefined)) {
    return "not heard from yet";
  }
  if (zigbeeIsSilent(device)) return "signal unknown";
  if (device.rssi === null || device.rssi === undefined) return "signal unknown";
  // Said in words, with the numbers on hover. dBm and LQI mean nothing to
  // someone deciding whether a sensor is in a sensible place.
  const quality = device.rssi > -60 ? "strong" : device.rssi > -75 ? "fair" : "weak";
  return `signal ${quality}`;
}

function describeZigbeeHealth(device) {
  if (device.quiet_for_s === null || device.quiet_for_s === undefined) {
    return "Joined, but nothing heard from it yet";
  }
  if (zigbeeIsSilent(device)) {
    const advice = device.power_source === "battery"
      ? "check its battery, and that it is in range"
      : "check it is powered and in range";
    return `Silent for ${describeSeconds(device.quiet_for_s)} — ${advice}`;
  }
  const weight = device.last && device.last.grams !== undefined && device.last.grams !== null
    ? `, last ${device.last.grams} g` : "";
  return `Reporting, heard ${describeSeconds(device.quiet_for_s)} ago${weight}`;
}

function describeSeconds(seconds) {
  if (!Number.isFinite(seconds)) return "some time";
  if (seconds < 90) return `${Math.round(seconds)}s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min`;
  return `${Math.round(seconds / 3600)} h`;
}

/* Adding or refusing a device that has only joined the radio.
 *
 * No confirmation, unlike Remove on a device in use: refusing one that
 * was never added loses nothing -- no name, no plant, no readings --
 * and a dialogue in front of "is this yours?" makes the answer feel
 * heavier than it is.
 */
async function claimZigbeeDevice(eui, action) {
  await fetch(`/api/zigbee/${action}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ eui }),
  });
  await refreshZigbee();
}

async function manageZigbeeDevice(eui, action) {
  if (!confirm("Remove this device? It is asked to leave the network and will "
               + "stop reporting until it is paired again.")) return;
  await fetch(`/api/zigbee/${action}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ eui }),
  });
  await refreshZigbee();
}

async function sendZigbeeCommand(eui, command) {
  // No confirmation here. A dialog is for an action that destroys something,
  // and turning a radio off does not: it can be turned back on over Zigbee.
  // Asking anyway trains people to dismiss dialogs without reading them,
  // which is how the one that matters gets waved through.
  await fetch("/api/zigbee/command", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ eui, command }),
  });
  await refreshZigbee();
  if (pageFromLocation() === "zigbee-device") renderZigbeeDevicePage();
}

/* One device's own page.
 *
 * Which controls appear is decided by what the device is, the same way the
 * sensor pages pick a panel by sensor kind. A new kind of Zigbee device adds
 * a panel here and changes nothing in the list. */
let zigbeeDevices = [];

function zigbeeEuiFromLocation() {
  return decodeURIComponent(window.location.pathname.slice("/zigbee/".length));
}

/* A battery's charge over time, for any Zigbee device that reports one.
 *
 * Its own fetch rather than part of the device poll: the poll runs every
 * second and this is hundreds of points that change once an hour. */
let zigbeeBatteryRange = 30;
let zigbeeBatteryFor = null;

async function loadZigbeeBattery(eui, force = false) {
  if (!eui) return;
  if (!force && zigbeeBatteryFor === `${eui}:${zigbeeBatteryRange}`) return;
  zigbeeBatteryFor = `${eui}:${zigbeeBatteryRange}`;
  try {
    const response = await fetch(
      `/api/zigbee/${encodeURIComponent(eui)}/battery?days=${zigbeeBatteryRange}`);
    if (!response.ok) throw new Error(String(response.status));
    renderZigbeeBatteryChart((await response.json()).points || []);
  } catch (error) {
    zigbeeBatteryFor = null;          // so a later poll tries again
    renderZigbeeBatteryChart([]);
  }
}

function renderZigbeeBatteryChart(points) {
  const section = document.getElementById("zigbee-battery-history");
  const chart = document.getElementById("zigbee-battery-chart");
  const empty = document.getElementById("zigbee-battery-empty");
  const summary = document.getElementById("zigbee-battery-summary");
  const usable = points
    .map((p) => ({ at: Date.parse(p.at), percent: Number(p.percent), volts: p.volts }))
    .filter((p) => Number.isFinite(p.at) && Number.isFinite(p.percent));

  /* Any device that has ever reported a charge gets this chart,
     whatever kind of device it is and whether or not it happens to be
     saying so this minute. Gated on the live value instead, a sensor
     that reports its battery twice a day showed no history at all for
     the other eleven hours -- while the history sat in the database. */
  setHidden(section, usable.length === 0);
  setHidden(chart, usable.length < 2);
  setHidden(empty, usable.length >= 2);
  // One reading is not a chart, and an empty frame reads as a fault.
  // A cell at full never crosses the change threshold, so a new sensor
  // sits on its first point until the next twelve-hourly heartbeat.
  if (usable.length === 1) {
    setText(empty, `One reading so far, ${usable[0].percent}%. A line needs two, `
                   + "and this device reports its charge every few hours.");
  }
  if (usable.length < 2) {
    setText(summary, usable.length === 1 ? "One reading so far." : "");
    return;
  }
  // The range is part of what is drawn, so it belongs in the key.
  // Without it, choosing a different range with the same points in
  // it skipped the redraw and the selector did nothing at all.
  if (!renderKeyChanged(chart, [usable.length, usable[0].at,
                                usable[usable.length - 1].at,
                                usable[usable.length - 1].percent,
                                zigbeeBatteryRange])) return;
  chart.replaceChildren();

  const width = 720, height = 220;
  const plot = { left: 46, right: 16, top: 16, bottom: 34 };
  const plotWidth = width - plot.left - plot.right;
  const plotHeight = height - plot.top - plot.bottom;
  /* The axis spans the range that was asked for, not the data.
   *
   * Fitting it to the readings drew every range identically -- there
   * are only three days of history, so seven days and a year hold the
   * same points -- and the selector looked broken because nothing
   * moved when it was pressed. Spanning the chosen window shows the
   * history pushed to the right with empty space beside it, which is
   * the honest picture: the range changed, and there is nothing older
   * to put in it. */
  const last = usable[usable.length - 1].at;
  const end = Math.max(last, Date.now());
  const first = end - zigbeeBatteryRange * 86400000;
  const span = Math.max(1, end - first);
  // Always the full scale. A battery between 80 and 84 percent drawn edge
  // to edge looks like a cliff; against 0 to 100 it looks like what it is.
  const x = (at) => plot.left + ((at - first) / span) * plotWidth;
  const y = (percent) => plot.top + (1 - percent / 100) * plotHeight;

  // The same helper and the same class names the other charts use, so a
  // gridline looks like a gridline everywhere. Inventing history-gridline
  // and history-axis here produced a chart styled by nothing at all.
  for (const level of [0, 25, 50, 75, 100]) {
    chart.append(
      svgElement("line", { class: "grid-line", x1: plot.left, x2: width - plot.right,
                           y1: y(level), y2: y(level) }),
      svgElement("text", { class: "axis-label", x: plot.left - 8, y: y(level) + 4,
                           "text-anchor": "end" }, `${level}%`));
  }

  chart.append(svgElement("path", {
    class: "history-line",
    d: usable.map((p, i) => `${i ? "L" : "M"}${x(p.at).toFixed(1)} ${y(p.percent).toFixed(1)}`)
             .join(" "),
  }));

  for (const [at, anchor] of [[first, "start"], [end, "end"]]) {
    chart.append(svgElement("text", {
      class: "axis-label", x: x(at), y: height - 10, "text-anchor": anchor,
    }, new Date(at).toLocaleDateString(undefined, { month: "short", day: "numeric" })));
  }

  // The question a drain chart is asked is "how long have I got", so answer
  // it rather than leaving it to be eyeballed off the slope.
  const days = (last - first) / 86400000;
  const dropped = usable[0].percent - usable[usable.length - 1].percent;
  const now = usable[usable.length - 1].percent;
  if (days >= 1 && dropped > 0) {
    const perDay = dropped / days;
    const leftDays = Math.round(now / perDay);
    setText(summary, `${now}% now, falling about ${perDay.toFixed(1)}% a day`
      + ` — roughly ${leftDays} day${leftDays === 1 ? "" : "s"} left at this rate.`);
  } else if (days >= 1 && dropped <= 0) {
    setText(summary, `${now}% now, steady or charging over the last `
      + `${Math.round(days)} day${Math.round(days) === 1 ? "" : "s"}.`);
  } else {
    setText(summary, `${now}% now. A day or so of history is needed to judge the drain.`);
  }
}

/* What a scale has weighed, from its own reports.
 *
 * Deliberately not the weights kept against the plant: those are one every
 * few hours by design, and the questions asked here -- is it drifting, has
 * it settled after being moved -- live in the minutes between them. The
 * retained reports hold every one, so the curve is the real one.
 *
 * Unstable readings are drawn too, hollow rather than hidden. A scale that
 * never settles is exactly what somebody is trying to see, and a chart that
 * quietly dropped those points would show a flat line and a mystery. */
let zigbeeWeightHours = 6;
let zigbeeWeightFor = null;

async function loadZigbeeWeight(eui, force = false) {
  if (!eui) return;
  // A different device is a different question.
  if (weighsFor !== eui) {
    weighsSomething = false;
    weighsFor = eui;
  }
  if (!force && zigbeeWeightFor === `${eui}:${zigbeeWeightHours}`) return;
  zigbeeWeightFor = `${eui}:${zigbeeWeightHours}`;
  try {
    const response = await fetch(
      `/api/zigbee/${encodeURIComponent(eui)}/reports?hours=${zigbeeWeightHours}&limit=2000`);
    if (!response.ok) throw new Error(String(response.status));
    renderZigbeeWeightChart((await response.json()).items || []);
  } catch (_error) {
    zigbeeWeightFor = null;           // so a later poll tries again
    renderZigbeeWeightChart([]);
  }
}

/* Whether the device on screen has ever reported a weight.
 *
 * "Is this a scale?" is not a question whose answer changes while
 * somebody is choosing a range, so it is remembered for as long as
 * the page is showing that device. Without it an empty hour looked
 * exactly like a thermometer, and the section disappeared.
 */
let weighsSomething = false;
let weighsFor = null;

function renderZigbeeWeightChart(items) {
  const section = document.getElementById("zigbee-weight-history");
  const chart = document.getElementById("zigbee-weight-chart");
  const empty = document.getElementById("zigbee-weight-empty");
  const summary = document.getElementById("zigbee-weight-summary");
  if (chart === null || section === null) return;
  // Oldest first, which is the order a chart is read in.
  const usable = items
    .map((r) => ({
      at: Date.parse(r.at),
      grams: Number((r.values || {}).grams),
      stable: Boolean((r.values || {}).stable),
    }))
    .filter((p) => Number.isFinite(p.at) && Number.isFinite(p.grams))
    .sort((a, b) => a.at - b.at);

  // A device that has never reported a weight is not a scale and gets
  // no weight chart. A scale with nothing in the last hour is a
  // different fact, and hiding the section for that took the range
  // buttons away with it: the controls that chose "1 hour" vanished,
  // the buttons left on screen belonged to the card below, and the
  // only way back was to reload the page.
  if (usable.length) weighsSomething = true;
  setHidden(section, !weighsSomething && usable.length === 0);
  setHidden(chart, usable.length < 2);
  setHidden(empty, usable.length >= 2);
  if (usable.length < 2) {
    setText(empty, usable.length === 1
      ? "One report in this range. Two are needed to draw a line."
      : "Nothing weighed in this range.");
    setText(summary, "");
    chart.replaceChildren();
    return;
  }
  const last = usable[usable.length - 1];
  if (!renderKeyChanged(chart, [usable.length, usable[0].at, last.at, last.grams,
                                zigbeeWeightHours])) return;
  chart.replaceChildren();

  const width = 720, height = 220;
  const plot = { left: 60, right: 16, top: 16, bottom: 34 };
  const plotWidth = width - plot.left - plot.right;
  const plotHeight = height - plot.top - plot.bottom;
  const firstAt = usable[0].at;
  const span = Math.max(1, last.at - firstAt);
  /* Scaled to the pot, not to the times it was lifted off.
   *
   * Taring this scale produced real readings of 0 g and -15394 g, and
   * a plain minimum and maximum drew an axis from -20041 to 20232 --
   * flattening fifteen kilograms of plant, and the hundred grams of
   * drying that is the whole point, into one line across the middle.
   *
   * Percentiles do not help: on the day of a re-zero nearly half the
   * readings are of an empty pan, so the middle nine tenths still
   * straddle both. These are two populations, not a curve with a few
   * strays, and the one worth drawing is whichever the scale is in
   * now. So the axis follows the latest reading, and anything less
   * than half or more than double it is the other population -- the
   * same rule the hub already uses to decide a weight is the pot
   * being lifted rather than a plant losing water.
   */
  const near = Math.abs(last.grams) > 1
    ? usable.filter((p) => p.grams > last.grams * 0.5 && p.grams < last.grams * 2)
    : usable;
  const shown = near.length >= 2 ? near : usable;
  const values = shown.map((p) => p.grams);
  const lowest = Math.min(...values);
  const highest = Math.max(...values);
  /* A floor keeps a perfectly steady scale from being drawn as a cliff
   * of rounding noise. */
  const padding = Math.max(10, (highest - lowest) * 0.15);
  const bottom = lowest - padding;
  const top = highest + padding;
  const offScale = usable.filter((p) => p.grams < bottom || p.grams > top);
  const x = (at) => plot.left + ((at - firstAt) / span) * plotWidth;
  const y = (grams) => plot.top + ((top - grams) / Math.max(1, top - bottom)) * plotHeight;

  for (const grams of [bottom, (bottom + top) / 2, top]) {
    chart.append(
      svgElement("line", { class: "grid-line", x1: plot.left, x2: width - plot.right,
                           y1: y(grams), y2: y(grams) }),
      svgElement("text", { class: "axis-label", x: plot.left - 8, y: y(grams) + 4,
                           "text-anchor": "end" }, showWeight(Math.round(grams))));
  }

  /* Broken around anything off the scale, so the line does not dive to
   * the floor and back and read as a weight that went there. */
  let drawn = [];
  const runs = [];
  for (const p of usable) {
    if (p.grams < bottom || p.grams > top) {
      if (drawn.length) runs.push(drawn);
      drawn = [];
      continue;
    }
    drawn.push(p);
  }
  if (drawn.length) runs.push(drawn);
  for (const stretch of runs) {
    if (stretch.length < 2) continue;
    chart.append(svgElement("path", {
      class: "history-line",
      d: stretch.map((p, i) => `${i ? "L" : "M"}${x(p.at).toFixed(1)}`
                               + ` ${y(p.grams).toFixed(1)}`).join(" "),
    }));
  }

  // One dot per report where there are few enough to see, filled when the
  // scale called the reading settled and hollow when it did not.
  if (usable.length <= 240) {
    for (const p of usable) {
      if (p.grams < bottom || p.grams > top) continue;
      chart.append(svgElement("circle", {
        class: p.stable ? "weight-point settled" : "weight-point unsettled",
        cx: x(p.at).toFixed(1), cy: y(p.grams).toFixed(1), r: 2.5,
      }));
    }
  }

  for (const [at, anchor] of [[firstAt, "start"], [last.at, "end"]]) {
    chart.append(svgElement("text", {
      class: "axis-label", x: x(at), y: height - 10, "text-anchor": anchor,
    }, new Date(at).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })));
  }

  /* The question is how fast it is moving and whether that is slowing, so
   * answer it: a settling scale and a drying pot look the same for an hour
   * and nothing alike over six. */
  /* Measured across what is on the scale, not across the tare.
   *
   * First-to-last over a day containing a re-zero reported "up 30609 g
   * over 21.3 h", which is the difference between an empty pan and a
   * full pot stated as if the plant had done it. */
  const onScale = usable.filter((p) => p.grams >= bottom && p.grams <= top);
  const from = onScale.length >= 2 ? onScale[0] : usable[0];
  const to = onScale.length >= 2 ? onScale[onScale.length - 1] : last;
  const hours = (to.at - from.at) / 3600000;
  const moved = to.grams - from.grams;
  const parts = [`${showWeight(Math.round(last.grams))} g now`];
  if (hours >= 0.05) {
    const perHour = moved / hours;
    /* The weight is shown in whichever unit was chosen; the change is
     * always in grams. Drift is the thing being read here, and seventeen
     * grams an hour rendered as "0.02 kg an hour" says nothing at all. */
    parts.push(`${moved >= 0 ? "up" : "down"} ${Math.round(Math.abs(moved))} g`
      + ` over ${hours < 1 ? `${Math.round(hours * 60)} min` : `${hours.toFixed(1)} h`}`
      + ` (${Math.round(Math.abs(perHour))} g an hour)`);
    // Half against half: a rate that is falling is a scale settling down,
    // a rate that holds is the pot actually losing water.
    const middle = from.at + (to.at - from.at) / 2;
    const early = onScale.filter((p) => p.at <= middle);
    const late = onScale.filter((p) => p.at > middle);
    if (early.length >= 2 && late.length >= 2) {
      const rate = (rows) => (rows[rows.length - 1].grams - rows[0].grams)
        / Math.max(0.01, (rows[rows.length - 1].at - rows[0].at) / 3600000);
      const before = Math.abs(rate(early));
      const after = Math.abs(rate(late));
      if (before > 0 && after < before * 0.6) parts.push("slowing down — it is still settling");
      else if (before > 0 && after > before * 1.4) parts.push("speeding up");
      else parts.push("holding a steady rate");
    }
  }
  const unsettled = onScale.filter((p) => !p.stable).length;
  if (unsettled) parts.push(`${unsettled} of ${onScale.length} reports unsettled`);
  // Named rather than quietly dropped: a reading of nothing is still
  // something the scale said, usually that the pot was off it.
  if (offScale.length) {
    parts.push(`${offScale.length} reading${offScale.length === 1 ? "" : "s"}`
      + " off this scale, most likely the pot lifted off");
  }
  setText(summary, parts.join(" · ") + ".");
}

/* The gateway's restarts, which used to be invisible.
 *
 * Each one costs the coordinator its device table, and a device it can no
 * longer place has its readings discarded -- while still being
 * acknowledged, so the device believes all is well and never retries.
 * Three evenings of silence were this, and nothing anywhere said the
 * gateway had restarted at all. */
function renderGatewayRestarts(restarts) {
  const table = document.getElementById("zigbee-gw-restarts");
  const none = document.getElementById("zigbee-gw-restarts-none");
  if (!table) return;
  setHidden(table.parentElement, restarts.length === 0);
  setHidden(none, restarts.length > 0);
  const body = table.querySelector("tbody");
  if (!renderKeyChanged(body, [restarts])) return;
  body.replaceChildren();
  for (const restart of restarts) {
    const row = document.createElement("tr");
    for (const text of [
      new Date(restart.at).toLocaleString(),
      restart.reason || "unknown",
      restart.ran_for_s == null ? "--" : describeSeconds(restart.ran_for_s),
    ]) {
      const cell = document.createElement("td");
      setText(cell, text);
      row.append(cell);
    }
    body.append(row);
  }
}

function renderZigbeeDevicePage() {
  const eui = zigbeeEuiFromLocation();
  const device = zigbeeDevices.find((d) => d.eui === eui);
  const kind = device ? device.kind : "unknown";
  const last = (device && device.last) || {};

  setText(document.getElementById("zigbee-device-kind"),
          kind === "unknown" ? "ZIGBEE DEVICE" : `ZIGBEE ${kind.toUpperCase()}`);
  setText(document.getElementById("zigbee-device-title"),
          device ? (kind === "unknown" ? "Device" : capitalise(kind)) : "Device not found");
  setText(document.getElementById("zigbee-device-id"), eui);
  setText(document.getElementById("zigbee-device-health"),
          device ? describeZigbeeHealth(device) : "This device is no longer on the network.");

  const fault = document.getElementById("zigbee-device-fault");
  const trouble = last.saturated ? "The reading is maxed out, which usually means a load cell wire has come loose."
    : last.ok === false ? "The device could not take a reading."
    : "";
  setText(fault, trouble);
  setHidden(fault, !trouble);

  // A number nobody has confirmed for hours is not a reading, it is a
  // souvenir. The weight stayed on screen for two hours after the link
  // died, looking exactly like a scale that was working and reading zero.
  const stale = device && zigbeeIsSilent(device);
  const hasWeight = last.grams !== undefined && last.grams !== null && !stale;
  setHidden(document.getElementById("zigbee-reading"),
            kind !== "scale" || (!hasWeight && !stale));
  setText(document.getElementById("zigbee-scale-grams"),
          hasWeight ? String(Math.round(last.grams)) : "--");
  // Always a word, never an empty line. Showing nothing when the reading is
  // steady let the card change height every time it settled, which is a
  // flicker on a page somebody is reading a number off.
  setText(document.getElementById("zigbee-scale-settling"),
          stale ? `last heard ${describeSeconds(device.quiet_for_s)} ago`
          : !hasWeight ? "waiting for a reading"
          : last.stable ? "steady" : "settling");
  // What the device last said it was doing -- and, if it has been asked for
  // something else, that too. A rate change can only be confirmed by the
  // next reading, which at half an hour apart is half an hour away, so
  // without this the page flatly contradicts the menu above it for the
  // whole interval and looks like it ignored the change.
  const ratePending = zigbeePendingInterval !== null
    && String(zigbeePendingInterval) !== String(last.interval);
  setText(document.getElementById("zigbee-scale-rate"),
          last.interval === undefined || stale ? "--"
          : ratePending
            ? `${describeInterval(last.interval)}, changing to ${describeInterval(Number(zigbeePendingInterval))}`
            : describeInterval(last.interval));
  // The same gauge the sensor pages draw, not a second way of saying the
  // same thing. A battery looks like a battery wherever it is shown, and
  // one page spelling it out in text while another drew a glyph made the
  // two read as different kinds of device.
  //
  // battery_percent of null is what renderBattery treats as unknown, which
  // is exactly what the firmware means by -1: there is nothing measuring
  // the cell, as opposed to a cell that is nearly flat.
  const battKnown = !stale && last.batt_pct !== undefined
    && last.batt_pct !== null && Number(last.batt_pct) >= 0;
  renderBattery(document.getElementById("zigbee-scale-batt"), {
    battery_percent: battKnown ? Number(last.batt_pct) : null,
    battery_voltage_v: Number.isFinite(Number(last.batt)) ? Number(last.batt) : null,
    battery_charging: null,
  });
  // The version the device itself reports, which since it travels in
  // every Zigbee frame is known with the Wi-Fi switched off -- the
  // state the scale normally sits in.
  // Ours reports its build in every reading; a bought device says it
  // once, during the interview. Either is the firmware this is running.
  setText(document.getElementById("zigbee-scale-firmware"),
          (device && device.firmware) || last.fw || "--");
  setText(document.getElementById("zigbee-scale-signal"),
          device && !zigbeeIsSilent(device)
            && device.rssi !== null && device.rssi !== undefined
            ? `${describeSignal(device)} (${device.rssi} dBm)` : "--");
  setText(document.getElementById("zigbee-scale-heard"),
          device && device.quiet_for_s !== null && device.quiet_for_s !== undefined
            ? `${describeSeconds(device.quiet_for_s)} ago` : "--");
  // Any device that reports a charge gets the chart, whatever kind it is.
  const hasBattery = device && device.last
    && device.last.batt_pct !== undefined && device.last.batt_pct !== null
    && Number(device.last.batt_pct) >= 0;
  // Always asked for: the chart hides itself when nothing has been
  // kept, which is the only honest test of whether this device has a
  // battery worth charting.
  loadZigbeeBattery(eui);
  /* The weight chart decides for itself, from what has been kept, rather
   * than from the live device list: a gateway that is unplugged empties
   * that list, and an unplugged gateway is exactly when somebody wants to
   * look at what the scale said before it went quiet. */
  loadZigbeeWeight(eui);

  const nameField = document.getElementById("zigbee-name");
  if (nameField && document.activeElement !== nameField) {
    nameField.value = (device && device.name) || "";
  }

  renderZigbeeSubject(device);

  // A way back to the device's own page, offered only when the device says
  // its Wi-Fi is on and has told us what it answers to. Guessing an address
  // would produce a link that goes nowhere.
  const notes = (device && device.notes) || {};
  const host = notes.ip || notes.host;
  const ownFigure = document.getElementById("zigbee-own-page-figure");
  const own = document.getElementById("zigbee-own-page");
  // Only while Wi-Fi is on, which is a temporary state by design.
  const reachable = Boolean(host) && last.wifi === true;
  setHidden(ownFigure, !reachable);
  if (reachable) {
    const where = notes.ip ? notes.ip : `${host}.local`;
    own.href = `http://${where}/`;
    setText(own, where);
  }

  // The panel for this kind of device, or the "no controls yet" one when
  // the hub has nothing to offer for it. Asking each panel what it is for
  // rather than naming the kinds here is what lets a new driver bring its
  // own controls without this line being edited again.
  const kinds = new Set([...document.querySelectorAll("[data-device-kind]")]
    .map((panel) => panel.dataset.deviceKind));
  const shown = kinds.has(kind) ? kind : "unknown";
  document.querySelectorAll("[data-device-kind]").forEach((panel) => {
    setHidden(panel, !device || panel.dataset.deviceKind !== shown);
  });

  // Scale-shaped facts, hidden for devices that are not shaped that way.
  // A mains plug has no battery and no reporting rate, and a dash next to
  // "Battery" reads as a flat one rather than as a question that does not
  // apply.
  setHidden(document.getElementById("zigbee-rate-figure"), kind !== "scale");
  setHidden(document.getElementById("zigbee-batt-figure"), !hasBattery);

  // True of every Zigbee device, and shown only once the device has
  // said it. Make answers "which of these is it", power and role
  // answer most of "why has it gone quiet": a battery end device is
  // meant to sleep and a mains router never is.
  // What it is, in the words somebody would use buying one. The kind
  // is the hub's name for a driver and the make is a model number;
  // neither of them says "soil probe" to a person reading the page.
  const type = device ? ZIGBEE_PRODUCT_TYPES[device.kind] : null;
  setHidden(document.getElementById("zigbee-type-figure"), !type);
  setText(document.getElementById("zigbee-type"), type || "--");

  const make = device ? [device.manufacturer, device.model].filter(Boolean).join(" ") : "";
  setHidden(document.getElementById("zigbee-make-figure"), !make);
  setText(document.getElementById("zigbee-make"), make);
  const supply = device && device.power_source;
  const role = device && device.role;
  setHidden(document.getElementById("zigbee-power-figure"), !supply && !role);
  setText(document.getElementById("zigbee-power"),
          [supply, role].filter(Boolean).join(" \u00b7 "));

  renderZigbeeDetails(device);
  renderZigbeeReporting(device);

  if (kind === "plug") renderZigbeePlug(device);
  if (kind === "leak") renderZigbeeLeak(device);
  if (kind === "soil") renderZigbeeSoil(device);
  if (kind === "thermometer") renderZigbeeAir(device);
  loadZigbeeMeasurements(eui);

  if (!device || kind !== "scale") return;

  // One button showing the action, from what the device reports.
  //
  // Wi-Fi off is the normal state of a finished scale, not a fault to
  // recover from. This panel used to read the other way round -- off
  // was "Zigbee is the only way to reach this scale", as though
  // something had gone wrong -- which had it backwards: Zigbee is how
  // the scale is meant to be reached, and the web server exists for
  // bringing a board up and for flashing it. Left on it keeps a second
  // radio associated to a router all day, which is the largest single
  // draw on a battery device.
  const button = document.getElementById("zigbee-scale-wifi");
  const state = document.getElementById("zigbee-scale-wifi-state");
  if (last.wifi === true || last.wifi === false) {
    button.textContent = last.wifi ? "Turn Wi-Fi off" : "Turn Wi-Fi on for now";
    button.dataset.command = last.wifi ? "w 0" : "w 1";
    button.disabled = false;
    setText(state, last.wifi
      ? "Wi-Fi is on, which is for updates and debugging and costs battery."
        + " Turn it off when you have finished."
      : "Running on Zigbee alone, which is how it should sit."
        + " Turn Wi-Fi on when you need its own page or an update.");
  } else {
    button.textContent = "Wi-Fi";
    button.disabled = true;
    button.removeAttribute("data-command");
    setText(state, "Waiting for the device to report whether its Wi-Fi is on.");
  }

  const select = document.getElementById("zigbee-scale-interval");
  const intervalState = document.getElementById("zigbee-scale-interval-state");
  if (select && last.interval !== undefined) {
    // The rate it actually runs at may not be one of the offered ones: it can
    // be set from the scale's own page or over serial. Show it rather than
    // leaving the control blank and the page disagreeing with the device.
    const value = String(last.interval);
    if (![...select.options].some((option) => option.value === value)) {
      select.add(new Option(describeInterval(last.interval), value));
    }

    // A choice that has been made but not yet reached the device is still
    // the person's choice. Writing the device's current rate back over it
    // every second meant the menu sprang back before the scale had so much
    // as heard the command -- the setting looked broken while it was in
    // fact on its way. Guarding on focus alone was not enough: choosing
    // from a menu moves the focus off it.
    if (zigbeePendingInterval !== null) {
      if (value === String(zigbeePendingInterval)) {
        zigbeePendingInterval = null;          // the scale is running it
        setText(intervalState, "");
      } else {
        // Held, with no deadline. A sleepy device collects its commands on
        // its own schedule, so the wait is as long as its reporting rate --
        // which may be twelve hours. A timer here would call a setting
        // broken while it was merely in a queue, so the page says when it
        // will land instead of guessing that it never will.
        select.value = String(zigbeePendingInterval);
        setText(intervalState,
                `Sent. The scale changes rate at its next reading, up to ${describeInterval(last.interval)} away.`);
        return;
      }
    }
    select.value = value;
  }
}

/* The one picker, filled from whatever the device is about.
 *
 * Nothing here asks what kind of device it is. The driver says "plant" or
 * "room", and the right list is fetched for it; a kind that is about
 * nothing gets no picker at all. */
/* The plug's switch, drawn from what the plug last said.
 *
 * Three states, not two: on, off, and not yet known. A plug the hub has
 * never heard from is not off -- it is unasked -- and drawing it as off
 * would be the page inventing a fact. The switch stays disabled until an
 * answer arrives, so the only thing to press is the one button that can
 * produce one.
 */
/* The diagnostics, which used to live in the tooltip of an address
 * that did not look like it had one. Folded away rather than deleted:
 * a rejoin count is the first thing worth seeing when a device keeps
 * dropping, and the second thing worth hiding the rest of the time.
 */
/* How often this device is asked to speak.
 *
 * Offered only where the hub has something to configure: a device
 * whose reporting it cannot set would get a form that does nothing.
 */
const ZIGBEE_CONFIGURABLE = new Set(["thermometer", "soil", "plug"]);

function renderZigbeeReporting(device) {
  const panel = document.getElementById("zigbee-reporting");
  if (!panel) return;
  const offered = Boolean(device) && ZIGBEE_CONFIGURABLE.has(device.kind);
  setHidden(panel, !offered);
  if (!offered) return;
  const maximum = document.getElementById("zigbee-report-max");
  const sensitivity = document.getElementById("zigbee-report-sensitivity");
  // Left alone while somebody is choosing from it: the page polls every
  // second, and a menu that resets under the cursor cannot be used.
  if (!maximum.matches(":focus")) {
    maximum.value = String(device.report_max_seconds || 1800);
  }
  if (!sensitivity.matches(":focus")) {
    sensitivity.value = device.report_sensitivity || "normal";
  }
}

async function saveZigbeeReporting() {
  const eui = zigbeeEuiFromLocation();
  const state = document.getElementById("zigbee-report-state");
  setText(state, "Asking the device.");
  try {
    const response = await fetch("/api/zigbee/reporting", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        eui,
        max_seconds: Number(document.getElementById("zigbee-report-max").value),
        sensitivity: document.getElementById("zigbee-report-sensitivity").value,
      }),
    });
    if (!response.ok) throw new Error(String(response.status));
    const result = await response.json();
    // Saved either way; whether the device has heard yet is a separate
    // fact, and a sleeping sensor has not.
    setText(state, result.asked_now
      ? "Saved, and the device has been told."
      : "Saved. The device is asleep, so it will be told when it next speaks.");
    await refreshZigbee();
  } catch (_error) {
    setText(state, "That did not save.");
  }
}

function renderZigbeeDetails(device) {
  setText(document.getElementById("zigbee-short"), (device && device.short) || "--");
  setText(document.getElementById("zigbee-report-count"),
          device ? String(device.readings) : "--");
  setText(document.getElementById("zigbee-join-count"),
          device ? String(device.joins) : "--");
  const undelivered = device ? device.failed_commands : 0;
  setHidden(document.getElementById("zigbee-undelivered-figure"), !undelivered);
  setText(document.getElementById("zigbee-undelivered"), String(undelivered || 0));
  setText(document.getElementById("zigbee-clusters"), describeZigbeeClusters(device));
}

/* What a device can do, in words. The numbers are the specification's
   and mean nothing to anyone deciding whether a sensor is in a sensible
   place; the ones with no name are left out rather than printed raw. */
const ZIGBEE_CLUSTER_NAMES = {
  0x0001: "battery",
  0x0006: "on/off",
  0x0008: "dimming",
  0x0201: "thermostat",
  0x0402: "temperature",
  0x0405: "humidity",
  0x0406: "occupancy",
  0x0500: "alarms",
  0x0702: "metering",
  0x0b04: "electrical measurement",
  0xfc00: "weighing",
};

function describeZigbeeClusters(device) {
  const named = (device && device.clusters ? device.clusters : [])
    .map((cluster) => ZIGBEE_CLUSTER_NAMES[cluster])
    .filter(Boolean);
  return named.length ? named.join(", ") : "--";
}

/* The air, from the last thing the sensor said.
 *
 * Each number is drawn only if it has been reported: a thermometer that
 * has sent temperature but not yet humidity should show the one it has
 * rather than a pair of dashes, and a dash where a reading will appear
 * is not the same as a dash where one never will.
 */
function renderZigbeeAir(device) {
  const last = (device && device.last) || {};
  const temp = last.temp_c;
  const humidity = last.rh_pct;
  setText(document.getElementById("zigbee-air-temp"),
          Number.isFinite(temp) ? temp.toFixed(1) : "--");
  setText(document.getElementById("zigbee-air-rh"),
          Number.isFinite(humidity) ? Math.round(humidity) : "--");
  const heard = device && device.quiet_for_s !== null && device.quiet_for_s !== undefined
    ? `Last reported ${describeSeconds(device.quiet_for_s)} ago.` : "";
  setText(document.getElementById("zigbee-air-state"),
          !device ? "This device is no longer on the network."
          : !Number.isFinite(temp) && !Number.isFinite(humidity)
            ? "Nothing reported yet. It sends when a reading changes, so give it a few minutes."
            : heard);
}

/* Every measurement this device has reported, whatever it is.
 *
 * Keyed to the record rather than to the kind of device: a chart
 * appears because something was measured, not because somebody
 * remembered to add one for this sort of sensor. A new driver that
 * decodes a known measurement gets its history with no change here.
 */
/* A unit: what to show, what to say, and what its axis may do.
 *
 * This was one string of prose, and the chart read it for hints --
 * `unit.startsWith("degrees")` chose the padding, `unit.includes(
 * "percent")` clamped the axis to 0-100 -- while the readout quietly
 * translated it back to a symbol to draw it. So the heading said
 * "52.7 percent relative humidity" beside a card already titled
 * Relative humidity, and shortening that label would have unclamped
 * a percentage axis with nothing to say it had.
 *
 * A symbol to draw, words for a screen reader, and the axis rules
 * stated outright. Nothing has to be inferred from how a label reads.
 */
const CELSIUS = { symbol: "\u00b0C", spoken: "degrees Celsius", pad: 0.5 };
const PERCENT = { symbol: "%", spoken: "percent", pad: 2, lowest: 0, highest: 100 };
// A weight has no ceiling and moves in whole grams.
const GRAMS = { symbol: " g", spoken: "grams", pad: 2 };

const ZIGBEE_MEASUREMENTS = [
  { key: "moisture_percent", label: "Moisture", decimals: 0,
    unit: { ...PERCENT, spoken: "percent moisture" } },
  { key: "soil_temperature_c", label: "Soil temperature", decimals: 1,
    unit: CELSIUS },
  { key: "temp_c", label: "Air temperature", decimals: 1, unit: CELSIUS },
  { key: "rh_pct", label: "Relative humidity", decimals: 0,
    unit: { ...PERCENT, spoken: "percent relative humidity" } },
  { key: "grams", label: "Weight", decimals: 0, unit: GRAMS },
];

let zigbeeMeasureHours = 24;
let zigbeeMeasureFor = null;

async function loadZigbeeMeasurements(eui, force = false) {
  if (!eui) return;
  if (!force && zigbeeMeasureFor === `${eui}:${zigbeeMeasureHours}`) return;
  zigbeeMeasureFor = `${eui}:${zigbeeMeasureHours}`;
  try {
    const response = await fetch(
      `/api/zigbee/${encodeURIComponent(eui)}/reports?hours=${zigbeeMeasureHours}&limit=5000`);
    if (!response.ok) throw new Error(String(response.status));
    renderZigbeeMeasurements((await response.json()).items || []);
  } catch (_error) {
    zigbeeMeasureFor = null;          // so a later poll tries again
    renderZigbeeMeasurements([]);
  }
}

function renderZigbeeMeasurements(items) {
  const section = document.getElementById("zigbee-measurements");
  const grid = document.getElementById("zigbee-measurement-grid");
  if (!section || !grid) return;
  const drawn = ZIGBEE_MEASUREMENTS
    .map((measure) => ({ measure, points: seriesFromReports(items, measure.key) }))
    .filter((series) => series.points.length >= 2);
  // Nothing kept worth drawing is not an empty chart, it is no chart.
  setHidden(section, drawn.length === 0);
  if (!drawn.length) {
    grid.replaceChildren();
    return;
  }
  if (!renderKeyChanged(grid, [drawn.map((s) => [s.measure.key, s.points.length,
                                                 s.points[s.points.length - 1].value]),
                               zigbeeMeasureHours])) return;
  grid.replaceChildren();
  for (const { measure, points } of drawn) {
    const card = document.createElement("article");
    card.className = "climate-chart-card";
    const header = document.createElement("header");
    const name = document.createElement("span");
    setText(name, measure.label);
    const value = document.createElement("strong");
    setText(value, `${points[points.length - 1].value.toFixed(measure.decimals)}`
                    + `${measure.unit.symbol}`);
    header.append(name, value);
    const chart = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    chart.id = `zigbee-measure-${measure.key}`;
    chart.setAttribute("class", "history-chart");
    chart.setAttribute("viewBox", "0 0 720 220");
    chart.setAttribute("role", "img");
    chart.setAttribute("aria-label", `${measure.label} over time`);
    const empty = document.createElement("p");
    empty.id = `zigbee-measure-${measure.key}-empty`;
    empty.className = "history-chart-empty";
    setText(empty, `No ${measure.label.toLowerCase()} in this range.`);
    card.append(header, chart, empty);
    grid.append(card);
  }
  // After they are in the document: the renderer finds them by id.
  //
  // The same renderer as every other chart in the hub. This had a
  // drawer of its own once -- a bare line, no points, no colour,
  // nothing to hover -- so the device page carried two charts in two
  // styles, and the rule in AGENTS.md is that any chart with a line
  // in it answers the pointer.
  const eui = zigbeeEuiFromLocation() || "device";
  for (const { measure, points } of drawn) {
    renderHistoryChart(
      `zigbee-measure-${measure.key}`,
      `zigbee-measure-${measure.key}-empty`,
      [{
        sensorId: `${eui}:${measure.key}`,
        name: measure.label,
        colour: seriesColours([measure.key]).get(measure.key),
        points: points.map((point) => ({ timestamp: point.at,
                                         value: point.value })),
        latest: points[points.length - 1].value.toFixed(measure.decimals),
      }],
      measure.unit, points[0].at, points[points.length - 1].at);
  }
}

function seriesFromReports(items, key) {
  return items
    .map((report) => ({ at: Date.parse(report.at),
                        value: Number((report.values || {})[key]) }))
    .filter((point) => Number.isFinite(point.at) && Number.isFinite(point.value))
    .sort((a, b) => a.at - b.at);
}

/* One line, with the axis labelled at both ends and in the middle.
 *
 * Padded so a steady room is not drawn as a mountain range of rounding
 * noise: a tenth of a degree across a day is a still room, and an axis
 * fitted tightly to it says the opposite.
 */
function renderZigbeeLeak(device) {
  const state = document.getElementById("zigbee-leak-state");
  const detail = document.getElementById("zigbee-leak-detail");
  const trouble = document.getElementById("zigbee-leak-trouble");
  if (!state) return;
  const last = (device && device.last) || {};
  const wet = last.wet;
  const known = wet === true || wet === false;
  setData(state, "wet", known ? String(wet) : "unknown");
  setText(state, !known ? "Nothing reported yet"
          : wet ? "Water detected" : "Dry");
  setText(detail, !device ? "This device is no longer on the network."
          : known ? `Last reported ${describeSeconds(device.quiet_for_s)} ago.`
          : "It reports when something changes. Wetting the probe is the way to see it work.");
  // Worth saying separately: a flat battery or a prised-open case is
  // the sensor telling you it may stop being able to warn you.
  const worries = [last.battery_low ? "its battery is low" : null,
                   last.tamper ? "it has been opened or moved" : null].filter(Boolean);
  setText(trouble, worries.length ? `The sensor says ${worries.join(", and ")}.` : "");
  setHidden(trouble, !worries.length);
}

/* What the pot is doing, from the probe's last word.
 *
 * Moisture leads: it is the number somebody opens this page to see,
 * and soil temperature is context for it rather than the other way
 * round.
 */
function renderZigbeeSoil(device) {
  const last = (device && device.last) || {};
  const moisture = last.moisture_percent;
  const temperature = last.soil_temperature_c;
  setText(document.getElementById("zigbee-soil-moisture"),
          Number.isFinite(moisture) ? Math.round(moisture) : "--");
  setText(document.getElementById("zigbee-soil-temp"),
          Number.isFinite(temperature) ? temperature.toFixed(1) : "--");
  setText(document.getElementById("zigbee-soil-state"),
          !device ? "This device is no longer on the network."
          : !Number.isFinite(moisture) && !Number.isFinite(temperature)
            ? "Nothing reported yet. It sends when a reading changes, so give it a few minutes."
            : `Last reported ${describeSeconds(device.quiet_for_s)} ago.`);
}

function renderZigbeePlug(device) {
  const toggle = document.getElementById("zigbee-plug-toggle");
  const label = document.getElementById("zigbee-plug-label");
  const state = document.getElementById("zigbee-plug-state");
  if (!toggle) return;
  const on = device && device.last ? device.last.on : undefined;
  const known = on === true || on === false;
  const silent = Boolean(device) && zigbeeIsSilent(device);
  toggle.dataset.eui = device ? device.eui : "";
  toggle.setAttribute("aria-checked", known ? String(on) : "mixed");
  toggle.disabled = !device || !known;
  setText(label, known ? (on ? "On" : "Off") : "--");
  setText(state, !device ? "This device is no longer on the network."
    : !known ? "The plug has not said whether it is on. Ask it."
    : silent ? `Last answered ${describeSeconds(device.quiet_for_s)} ago, so this may be out of date.`
    : `Answered ${describeSeconds(device.quiet_for_s)} ago.`);
}

async function commandZigbeePlug(eui, command) {
  if (!eui) return;
  setText(document.getElementById("zigbee-plug-state"),
          command === "state" ? "Asking the plug."
          : `Telling the plug to turn ${command}.`);
  const say = (word) => fetch("/api/zigbee/command", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ eui, command: word }),
  });
  await say(command);
  // Asked rather than assumed. A command the radio accepted is not the
  // same as a relay that moved, and the plug is the only thing that
  // knows which of those happened.
  if (command !== "state") {
    await new Promise((resolve) => setTimeout(resolve, 400));
    await say("state");
  }
  await new Promise((resolve) => setTimeout(resolve, 500));
  await refreshZigbee();
  if (pageFromLocation() === "zigbee-device") renderZigbeeDevicePage();
}

async function renderZigbeeSubject(device) {
  const block = document.getElementById("zigbee-subject");
  const label = document.getElementById("zigbee-subject-label");
  const choice = document.getElementById("zigbee-subject-choice");
  const subject = device ? device.subject : null;
  setHidden(block, !subject);
  if (!subject) return;

  const response = await fetch(subject === "plant" ? "/api/plants" : "/api/rooms",
                               { cache: "no-store" });
  if (!response.ok) return;
  const items = (await response.json()).items || [];
  const idOf = (item) => (subject === "plant" ? item.plant_id : item.room_id);
  const current = subject === "plant" ? device.plant_id : device.room_id;

  // The name comes from the list that is being loaded anyway. A sensor row
  // carries the id of what it is attached to, not the name, and fetching
  // the same list twice to learn it would be the worse trade.
  const assigned = items.find((item) => idOf(item) === current);
  const assignedName = assigned
    ? assigned.display_name || assigned.name
    : subject === "room" ? device.room : null;
  setText(label, assignedName
    ? `This ${device.kind} is ${subject === "plant" ? "on" : "in"} ${assignedName}`
    : subject === "plant"
      ? "This device is not on a plant yet"
      : "This device is not in a room yet");

  if (document.activeElement === choice) return;
  choice.innerHTML = "";
  choice.add(new Option(subject === "plant" ? "No plant" : "No room", ""));
  items.forEach((item) => {
    choice.add(new Option(item.display_name || item.name || String(idOf(item)), String(idOf(item))));
  });
  choice.value = current ? String(current) : "";

}

function describeInterval(seconds) {
  if (!seconds) return "as fast as it can";
  // One of anything is not plural. The scale runs at a one second interval
  // and the page said "1 seconds".
  const of = (count, unit) => `${count} ${unit}${count === 1 ? "" : "s"}`;
  if (seconds < 90) return of(seconds, "second");
  if (seconds < 5400) return of(Math.round(seconds / 60), "minute");
  return of(Math.round(seconds / 3600), "hour");
}

/* A page of rooms.
 *
 * Air belongs to a room, not to a plant: one thermometer on a windowsill
 * answers for every pot beside it. Reading the rooms side by side is how a
 * room that is too dry, or colder than the rest of the house, becomes
 * obvious -- which a plant-at-a-time view can never show.
 */
let roomsOverview = [];
let roomHistory = new Map();        // "room_id:sensor_id" -> {items, fetchedAt}
const ROOM_HISTORY_MS = 24 * 60 * 60 * 1000;
const ROOM_HISTORY_REFETCH_MS = 60_000;

async function refreshRoomsPage() {
  try {
    const response = await fetch("/api/rooms/overview");
    if (!response.ok) return;
    roomsOverview = (await response.json()).items || [];
  } catch (_error) {
    return;
  }
  renderRoomsPage();
  // Histories after the cards, so the page is readable before the charts
  // arrive rather than blank until the last one does.
  for (const room of roomsOverview) {
    if ((room.air_sources || []).length) loadRoomHistory(room);
  }
}

/* One history per sensor, not one per room.
 *
 * A room with a probe at the window and a thermometer by the door has
 * two honest answers, and drawing only the one that reported most
 * recently made the other look broken. Averaging them would be worse:
 * it produces a third reading that is true nowhere in the room.
 */
async function loadRoomHistory(room) {
  for (const source of room.air_sources || []) {
    const key = `${room.room_id}:${source.sensor_id}`;
    const held = roomHistory.get(key);
    if (held && Date.now() - held.fetchedAt < ROOM_HISTORY_REFETCH_MS) continue;
    const end = Date.now();
    const query = new URLSearchParams({
      sensor_id: source.sensor_id,
      start: new Date(end - ROOM_HISTORY_MS).toISOString(),
      end: new Date(end).toISOString(),
    });
    try {
      const response = await fetch(`/api/readings/history?${query}`);
      if (!response.ok) continue;
      const items = (await response.json()).items || [];
      roomHistory.set(key, { items, fetchedAt: Date.now(), end });
      renderRoomsPage();
    } catch (_error) {
      /* The card still shows the current air. */
    }
  }
}

/* One room's own page.
 *
 * Settings could say a room held one plant; only this can say which,
 * and the thing that blocks a delete is exactly the thing worth
 * showing beside the delete button.
 */
let shownRoom = null;

function currentRoomId() {
  const match = window.location.pathname.match(/^\/rooms\/(\d+)/);
  return match ? Number(match[1]) : null;
}

function addingARoom() {
  return window.location.pathname === "/rooms/new";
}

async function refreshRoomPage() {
  if (addingARoom()) {
    renderNewRoomPage();
    return;
  }
  const roomId = currentRoomId();
  if (roomId === null) return;
  try {
    const response = await fetch(`/api/rooms/${roomId}`);
    if (!response.ok) {
      shownRoom = null;
      setText(document.getElementById("room-page-title"), "No such room");
      setText(document.getElementById("room-page-summary"),
              "It may have been deleted.");
      return;
    }
    shownRoom = await response.json();
  } catch (_error) {
    return;
  }
  renderRoomPage();
}

function renderNewRoomPage() {
  shownRoom = null;
  setText(document.getElementById("room-page-title"), "New room");
  setText(document.getElementById("room-page-summary"),
          "Where a plant lives shapes what it needs.");
  setText(document.getElementById("room-page-details-title"), "Details");
  setText(document.getElementById("room-page-save"), "Add room");
  document.title = "New room \u00b7 Open Plant Pulse";
  for (const id of ["room-page-plants-section", "room-page-sensors-section",
                    "room-page-danger-section"]) {
    setHidden(document.getElementById(id), true);
  }
  const form = document.getElementById("room-page-form");
  if (form.contains(document.activeElement)) return;
  document.getElementById("room-page-id").value = "";
  document.getElementById("room-page-name").value = "";
  document.getElementById("room-page-aspect").value = "unknown";
  document.getElementById("room-page-light").value = "unknown";
  document.getElementById("room-page-notes").value = "";
}

function renderRoomPage() {
  const room = shownRoom;
  if (!room) return;
  for (const id of ["room-page-plants-section", "room-page-sensors-section",
                    "room-page-danger-section"]) {
    setHidden(document.getElementById(id), false);
  }
  setText(document.getElementById("room-page-save"), "Save");
  setText(document.getElementById("room-page-title"), room.name);
  setText(document.getElementById("room-page-summary"),
          [tally(room.sensor_count, "sensor"),
           tally(room.plant_count, "plant")].join(" \u00b7 "));
  document.title = `${room.name} \u00b7 Open Plant Pulse`;

  // The form is only filled when it is not being typed into, so a
  // refresh underneath somebody mid-edit does not take the field back.
  const form = document.getElementById("room-page-form");
  if (!form.contains(document.activeElement)) {
    document.getElementById("room-page-id").value = room.room_id;
    document.getElementById("room-page-name").value = room.name;
    document.getElementById("room-page-aspect").value = room.aspect || "unknown";
    document.getElementById("room-page-light").value = room.light || "unknown";
    document.getElementById("room-page-notes").value = room.notes || "";
  }

  renderRoomOccupants("room-page-plants", room.plants || [],
                      (plant) => ({ href: `/plants/${plant.plant_id}`,
                                    name: plant.display_name,
                                    detail: plant.profile_id || "no profile" }),
                      "No plants in this room.");
  renderRoomOccupants("room-page-sensors", room.sensors || [],
                      (sensor) => ({ href: `/sensors/${sensor.sensor_id}`,
                                     name: sensor.display_name || sensor.sensor_id,
                                     detail: sensor.transport || "" }),
                      "No sensors in this room.");

  // Said before the button is pressed, not after: what is in the way
  // is on the page already.
  const held = [room.plant_count ? tally(room.plant_count, "plant") : null,
                room.sensor_count ? tally(room.sensor_count, "sensor") : null]
    .filter(Boolean);
  setText(document.getElementById("room-page-delete-note"),
          held.length
            ? `This room still holds ${held.join(" and ")}. Move `
              + (room.plant_count + room.sensor_count === 1 ? "it" : "them")
              + " out before deleting it."
            : "Nothing is in this room, so it can be deleted.");
  document.getElementById("room-page-delete").disabled = held.length > 0;
}

function renderRoomOccupants(listId, items, describe, emptyText) {
  const list = document.getElementById(listId);
  if (!list) return;
  list.replaceChildren();
  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    setText(empty, emptyText);
    list.append(empty);
    return;
  }
  for (const item of items) {
    const { href, name, detail } = describe(item);
    const card = document.createElement("div");
    card.className = "settings-sensor";
    const heading = document.createElement("div");
    heading.className = "settings-sensor-heading";
    const link = document.createElement("a");
    link.href = href;
    setText(link, name);
    heading.append(link);
    card.append(heading);
    if (detail) {
      const note = document.createElement("code");
      setText(note, detail);
      card.append(note);
    }
    list.append(card);
  }
}

async function saveRoomPage(event) {
  event.preventDefault();
  const adding = addingARoom();
  const roomId = currentRoomId();
  const message = document.getElementById("room-page-message");
  setText(message, "");
  let response;
  try {
    response = await fetch(adding ? "/api/rooms" : `/api/rooms/${roomId}`, {
      method: adding ? "POST" : "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: document.getElementById("room-page-name").value.trim(),
        aspect: document.getElementById("room-page-aspect").value,
        light: document.getElementById("room-page-light").value,
        notes: document.getElementById("room-page-notes").value.trim() || null,
      }),
    });
  } catch (_error) {
    setText(message, "The hub could not be reached. Nothing was saved.");
    return;
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    setText(message, payload.error || "Could not save the room.");
    return;
  }
  if (adding) {
    const created = await response.json().catch(() => ({}));
    if (created.room_id) {
      navigate(`/rooms/${created.room_id}`);
      return;
    }
  }
  setText(message, "Saved.");
  await refreshRoomPage();
}

async function deleteRoomFromItsPage() {
  const roomId = currentRoomId();
  const message = document.getElementById("room-page-delete-message");
  setText(message, "");
  let response;
  try {
    response = await fetch(`/api/rooms/${roomId}`, { method: "DELETE" });
  } catch (_error) {
    setText(message, "The hub could not be reached. The room was not deleted.");
    return;
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    setText(message, payload.error || "Could not delete the room.");
    return;
  }
  navigate("/settings/rooms");
}

/* Where an alarm goes, and whether the path works.
 *
 * The test button exists because the alternative way to find out
 * whether the address was typed correctly is to pour water on a floor.
 */
// No i, l, o, 0 or 1: the name is read off one screen and typed into
// a phone, and those are the characters people get wrong.
const TOPIC_LETTERS = "abcdefghjkmnpqrstuvwxyz23456789";
const TOPIC_LENGTH = 16;

/* A topic nobody could guess.
 *
 * The name is the only thing keeping anybody else off this channel --
 * ntfy has no accounts for a plain topic -- so it is generated rather
 * than typed. "leak", "home", a street name: those are the ones
 * somebody else arrives at by accident, and the alert is then readable
 * by them and forgeable by them.
 *
 * 16 characters of this alphabet is about 79 bits, which is not
 * guessable by anybody working through names.
 */
function generatedTopic() {
  const numbers = new Uint32Array(TOPIC_LENGTH);
  crypto.getRandomValues(numbers);
  const name = [...numbers]
    .map((n) => TOPIC_LETTERS[n % TOPIC_LETTERS.length]).join("");
  return `opp-${name}`;
}

function showAlertTopic(address) {
  const line = document.getElementById("alerts-topic");
  if (!line) return;
  let topic = "";
  try {
    topic = address ? new URL(address).pathname.replace(/^\//, "") : "";
  } catch (_error) {
    topic = "";
  }
  setText(line, topic
    ? `Subscribe the phone to the topic ${topic}`
    : "");
  setHidden(line, !topic);
}

function generateAlertAddress() {
  const field = document.getElementById("alerts-url");
  const message = document.getElementById("alerts-message");
  // Replacing an address already in use would leave the phone
  // subscribed to a topic the hub no longer sends to, which is a
  // silent way for a leak alarm to stop working.
  if (field.value.trim()
      && !window.confirm("Replace the address?\n\nThe phone is subscribed to "
                         + "the old topic and would stop receiving alerts "
                         + "until it is subscribed to the new one.")) {
    return;
  }
  field.value = `https://ntfy.sh/${generatedTopic()}`;
  showAlertTopic(field.value);
  setText(message, "Generated. Press Save, then subscribe the phone to it.");
}

/* The alert rules on one device.
 *
 * The measurements offered are the ones this device has actually
 * reported, read off its kept reports, so a kind of sensor nobody has
 * written code for still gets rules. The sounds offered are the ones
 * this computer can actually play.
 */
let alertChoices = { measurements: [], sounds: [], ringing: [] };
// The rule the form is editing, or null when it is adding one. One
// form either way: two forms describing a rule would be two places to
// keep saying the same thing.
let editingRule = null;
let knownRules = [];

// What a measurement is called in a sentence. Anything not here reads
// as its own key, which is honest for a device nobody has named yet.
const MEASUREMENT_WORDS = {
  wet: "water", air_temperature_c: "air temperature", rh_pct: "humidity",
  soil_temperature_c: "soil temperature", moisture_percent: "soil moisture",
  batt_pct: "battery", battery_low: "battery low", grams: "weight",
  tamper: "tamper", temp_c: "temperature", lqi: "signal",
};

function measurementWords(key) {
  return MEASUREMENT_WORDS[key] || String(key).replace(/_/g, " ");
}

function describeRule(rule) {
  const what = measurementWords(rule.measurement);
  if (rule.test === "true") return `When ${what} is reported`;
  return `When ${what} is ${rule.test} ${rule.threshold}`;
}

async function refreshDeviceAlerts() {
  const eui = zigbeeEuiFromLocation();
  if (!eui) return;
  try {
    const response = await fetch(`/api/alert-rules/zigbee-${eui}`);
    if (!response.ok) return;
    const data = await response.json();
    alertChoices = {
      measurements: data.measurements || [],
      sounds: data.sounds || [],
      ringing: data.ringing || [],
    };
    renderAlertChoices();
    renderDeviceAlertRules(data.items || []);
  } catch (_error) {
    /* the rest of the page still works */
  }
}

function renderAlertChoices() {
  const measurement = document.getElementById("alert-measurement");
  const sound = document.getElementById("alert-sound");
  if (!measurement || !sound) return;
  if (!renderKeyChanged(measurement, alertChoices.measurements)) return;
  measurement.replaceChildren();
  for (const key of alertChoices.measurements) {
    const option = document.createElement("option");
    option.value = key;
    setText(option, measurementWords(key));
    measurement.append(option);
  }
  // What was picked, kept across a rebuild: the list is rebuilt
  // whenever the measurements change, and resetting the box under
  // somebody choosing is how a selection gets lost.
  const chosen = sound.value;
  sound.replaceChildren();
  // A real sound first. With "no sound" at the top, a rule added in a
  // hurry made none, and Hear did nothing until the box was changed.
  for (const name of alertChoices.sounds) {
    const option = document.createElement("option");
    option.value = name;
    setText(option, name);
    sound.append(option);
  }
  const silent = document.createElement("option");
  silent.value = "";
  setText(silent, "no sound");
  sound.append(silent);
  if (chosen && [...sound.options].some((o) => o.value === chosen)) {
    sound.value = chosen;
  }
}

function renderDeviceAlertRules(rules) {
  const list = document.getElementById("zigbee-alert-rules");
  if (!list) return;
  knownRules = rules;
  if (!renderKeyChanged(list, [rules, alertChoices.ringing])) return;
  list.replaceChildren();
  if (!rules.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    setText(empty, "No rules on this device. It will not raise an alarm.");
    list.append(empty);
    return;
  }
  for (const rule of rules) {
    const card = document.createElement("div");
    card.className = "settings-sensor";
    const heading = document.createElement("div");
    heading.className = "settings-sensor-heading";
    const what = document.createElement("strong");
    setText(what, describeRule(rule));
    heading.append(what);
    if (alertChoices.ringing.includes(rule.rule_id)) {
      const stop = document.createElement("button");
      stop.type = "button";
      stop.className = "danger";
      setData(stop, "stopRule", rule.rule_id);
      setText(stop, "Stop the sound");
      heading.append(stop);
    }
    const how = document.createElement("code");
    setText(how, [rule.sound ? `${rule.sound}${rule.repeat_seconds
                    ? ` every ${rule.repeat_seconds}s` : ""}` : "silent",
                  rule.push ? "phone" : null,
                  rule.enabled ? null : "off"].filter(Boolean).join(" \u00b7 "));
    const actions = document.createElement("div");
    actions.className = "inline-control";
    const test = document.createElement("button");
    test.type = "button";
    setData(test, "testRule", rule.rule_id);
    setText(test, "Test");
    const edit = document.createElement("button");
    edit.type = "button";
    setData(edit, "editRule", rule.rule_id);
    setText(edit, "Edit");
    const toggle = document.createElement("button");
    toggle.type = "button";
    setData(toggle, "toggleRule", rule.rule_id);
    setData(toggle, "enabled", rule.enabled ? "1" : "0");
    setText(toggle, rule.enabled ? "Turn off" : "Turn on");
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "danger";
    setData(remove, "deleteRule", rule.rule_id);
    setText(remove, "Delete");
    actions.append(test, edit, toggle, remove);
    card.append(heading, how, actions);
    list.append(card);
  }
}

function startRuleEdit(ruleId) {
  const rule = knownRules.find((r) => r.rule_id === Number(ruleId));
  if (!rule) return;
  editingRule = rule.rule_id;
  document.getElementById("alert-measurement").value = rule.measurement;
  document.getElementById("alert-test").value = rule.test;
  document.getElementById("alert-threshold").value =
    rule.threshold == null ? "" : rule.threshold;
  document.getElementById("alert-sound").value = rule.sound || "";
  document.getElementById("alert-repeat").value = String(rule.repeat_seconds);
  document.getElementById("alert-push").checked = rule.push;
  setHidden(document.getElementById("alert-threshold-field"),
            rule.test === "true");
  setText(document.getElementById("alert-add"), "Save changes");
  setHidden(document.getElementById("alert-cancel"), false);
  setText(document.getElementById("alert-message"), `Editing: ${describeRule(rule)}`);
  document.getElementById("alert-threshold").focus();
}

function clearRuleForm() {
  editingRule = null;
  document.getElementById("alert-threshold").value = "";
  document.getElementById("alert-repeat").value = "0";
  document.getElementById("alert-push").checked = true;
  setText(document.getElementById("alert-add"), "Add rule");
  setHidden(document.getElementById("alert-cancel"), true);
  setText(document.getElementById("alert-message"), "");
}

async function addAlertRule(event) {
  event.preventDefault();
  const eui = zigbeeEuiFromLocation();
  const message = document.getElementById("alert-message");
  const test = document.getElementById("alert-test").value;
  const threshold = document.getElementById("alert-threshold").value;
  setText(message, "");
  const body = {
    measurement: document.getElementById("alert-measurement").value,
    test,
    threshold: test === "true" ? null : Number(threshold),
    sound: document.getElementById("alert-sound").value || null,
    repeat_seconds: Number(document.getElementById("alert-repeat").value),
    push: document.getElementById("alert-push").checked,
  };
  if (test !== "true" && !Number.isFinite(body.threshold)) {
    setText(message, "Type the number to compare with.");
    return;
  }
  let response;
  try {
    // The same form writes a new rule and corrects an old one; only
    // where it goes differs.
    response = await fetch(editingRule
        ? `/api/alert-rules/${editingRule}`
        : `/api/alert-rules/zigbee-${eui}`, {
      method: editingRule ? "PUT" : "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (_error) {
    setText(message, "The hub could not be reached.");
    return;
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    setText(message, payload.error || "That rule could not be added.");
    return;
  }
  const changed = Boolean(editingRule);
  clearRuleForm();
  setText(message, changed ? "Saved." : "Added.");
  await refreshDeviceAlerts();
}

async function changeAlertRule(event) {
  const test = event.target.closest("[data-test-rule]");
  if (test) {
    const message = document.getElementById("alert-message");
    test.disabled = true;
    setText(message, "Testing...");
    try {
      const response = await fetch(`/api/alert-rules/${test.dataset.testRule}/test`,
                                   { method: "POST" });
      const result = await response.json().catch(() => ({}));
      // Said in full, because "it worked" about two things when one
      // of them failed is the sort of answer that wastes an evening.
      const said = [
        result.sound ? (result.sounded ? `played ${result.sound}`
                                       : `could not play ${result.sound}`) : null,
        result.push ? (result.pushed ? "sent to the phone"
                                     : "could not reach the phone") : null,
      ].filter(Boolean);
      setText(message, result.error || (said.length ? said.join(", ")
                                                    : "This rule does nothing."));
    } catch (_error) {
      setText(message, "The hub could not be reached.");
    } finally {
      test.disabled = false;
    }
    return;
  }
  const edit = event.target.closest("[data-edit-rule]");
  if (edit) {
    startRuleEdit(edit.dataset.editRule);
    return;
  }
  const stop = event.target.closest("[data-stop-rule]");
  if (stop) {
    await fetch(`/api/alert-rules/${stop.dataset.stopRule}/stop`,
                { method: "POST" });
    await refreshDeviceAlerts();
    return;
  }
  const toggle = event.target.closest("[data-toggle-rule]");
  if (toggle) {
    await fetch(`/api/alert-rules/${toggle.dataset.toggleRule}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: toggle.dataset.enabled !== "1" }),
    });
    await refreshDeviceAlerts();
    return;
  }
  const remove = event.target.closest("[data-delete-rule]");
  if (remove) {
    await fetch(`/api/alert-rules/${remove.dataset.deleteRule}`,
                { method: "DELETE" });
    // The form cannot go on editing a rule that is gone.
    if (editingRule === Number(remove.dataset.deleteRule)) clearRuleForm();
    await refreshDeviceAlerts();
  }
}

async function hearAlertSound(event) {
  const button = event.currentTarget;
  const message = document.getElementById("alert-message");
  const sound = document.getElementById("alert-sound").value;
  if (!sound) {
    // It used to return here without a word, so a button pressed on a
    // fresh page -- where the box says "no sound" -- did nothing and
    // looked broken.
    setText(message, "Pick a sound first.");
    return;
  }
  // Held down briefly, because a second press cuts the first sound
  // off to start it again, which is heard as the sound breaking up
  // rather than as two presses.
  button.disabled = true;
  try {
    const response = await fetch("/api/alert-rules/hear", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sound }),
    });
    const result = await response.json().catch(() => ({}));
    setText(message, result.played ? `Played ${sound}.`
                                   : `The hub could not play ${sound}.`);
  } catch (_error) {
    setText(message, "The hub could not be reached.");
  } finally {
    // About as long as a system sound lasts.
    window.setTimeout(() => { button.disabled = false; }, 600);
  }
}

/* The plant box's fan.
 *
 * Self-contained: this page and /api/fan, and nothing else in the hub
 * knows a fan exists. See docs/proposals/vzbot-humidity-control.md.
 */
// Whether the form holds something not yet saved.
let boxEdited = false;

function showFanMode(mode) {
  // Only the controls that apply to what it is doing. A manual speed
  // box beside a humidity target invites setting both and wondering
  // which won.
  setHidden(document.getElementById("box-manual-row"), mode !== "manual");
  setHidden(document.getElementById("box-auto-rows"), mode !== "auto");
}

async function refreshBoxPage() {
  const form = document.getElementById("box-form");
  if (!form) return;
  let fan;
  try {
    const response = await fetch("/api/fan");
    if (!response.ok) return;
    fan = await response.json();
  } catch (_error) {
    return;
  }
  const state = fan.state || {};
  setText(document.getElementById("box-rh"),
          state.rh == null ? "--" : `${state.rh}%`);
  setText(document.getElementById("box-target"), `${fan.target_rh}%`);
  setText(document.getElementById("box-speed"),
          state.speed == null ? "--" : `${Math.round(state.speed * 100)}%`);
  setText(document.getElementById("box-printer"),
          state.why && state.why.includes("cannot be reached") ? "away"
            : state.why && state.why.includes("printer is") ? "printing" : "ready");
  setText(document.getElementById("box-why"), state.why || "");

  renderBoxSensors(fan.sensor_id);
  // Unsaved changes survive the poll.
  //
  // Leaving it alone only while it has focus was not enough: a mode
  // chosen and then a glance away is still an unsaved change, and a
  // poll a second later put it back without saying anything.
  if (boxEdited || form.contains(document.activeElement)) return;
  const fill = (id, value) => {
    const field = document.getElementById(id);
    if (field) field.value = value;
  };
  fill("box-mode", fan.mode);
  fill("box-manual-speed", fan.manual_speed);
  fill("box-target-rh", fan.target_rh);
  fill("box-kp", fan.kp);
  fill("box-ki", fan.ki);
  fill("box-kd", fan.kd);
  const named = presetMatching(fan.kp, fan.ki, fan.kd);
  fill("box-preset", named);
  setText(document.getElementById("box-preset-note"),
          (PID_PRESETS.find((p) => p.name === named) || {}).why || "");
  fill("box-min-speed", fan.min_speed);
  fill("box-max-speed", fan.max_speed);
  fill("box-pulse-window", fan.pulse_window_s);
  renderPidPresets();
  fill("box-url", fan.moonraker_url);
  fill("box-fan-name", fan.fan_name);
  showFanMode(fan.mode);
  drawPidPreview();
}

/* Anything that has reported humidity can hold the box.
 *
 * Read off what the devices have actually sent, the same way the
 * alert rules offer their measurements, so a thermometer bought next
 * year appears here without a code change.
 */
function renderBoxSensors(chosen) {
  const picker = document.getElementById("box-sensor");
  if (!picker) return;
  const able = (zigbeeDevices || []).filter(
    (device) => device.last && device.last.rh_pct != null);
  if (!renderKeyChanged(picker, [able.map((d) => [d.eui, d.name]), chosen])) return;
  picker.replaceChildren();
  if (!able.length) {
    const none = document.createElement("option");
    none.value = "";
    setText(none, "nothing is measuring humidity yet");
    picker.append(none);
    return;
  }
  for (const device of able) {
    const option = document.createElement("option");
    option.value = `zigbee-${device.eui}`;
    setText(option, `${device.name || device.eui} \u00b7 ${device.last.rh_pct}%`);
    picker.append(option);
  }
  if (chosen) picker.value = chosen;
}

/* Starting points, not tuned values.
 *
 * There is no "correct PID for an enclosure": the right gains depend
 * on how big the box is, how much air the fan moves and how humid the
 * outside is, and the only way to know is to watch the chart. These
 * are four sensible places to start, with the symptom that should
 * send you to another one.
 */
const PID_PRESETS = [
  { name: "Gentle", kp: 0.03, ki: 0.0008, kd: 0,
    why: "Slow and steady. Start here if the fan hunts up and down." },
  { name: "Balanced", kp: 0.05, ki: 0.002, kd: 0,
    why: "A sensible default for a sealed box on a slow sensor." },
  { name: "Firm", kp: 0.1, ki: 0.004, kd: 0,
    why: "Reacts harder. Use if it sits above target for hours." },
  { name: "Proportional only", kp: 0.06, ki: 0, kd: 0,
    why: "Never winds up, and never quite reaches the target. "
       + "Useful while you watch what the fan actually does." },
  { name: "With damping", kp: 0.05, ki: 0.002, kd: 0.2,
    why: "Kd pushes back on fast rises. Only worth it once the sensor "
       + "reports often; on a slow one it mostly amplifies noise." },
];

function renderPidPresets() {
  const picker = document.getElementById("box-preset");
  if (!picker || picker.options.length) return;
  const custom = document.createElement("option");
  custom.value = "";
  setText(custom, "my own numbers");
  picker.append(custom);
  for (const preset of PID_PRESETS) {
    const option = document.createElement("option");
    option.value = preset.name;
    setText(option, preset.name);
    picker.append(option);
  }
}

/* Which preset the saved gains amount to, if any.
 *
 * Worked out from the gains rather than remembered beside them.
 * Storing the chosen name as well would be a second thing to keep
 * true: nudge Kp by hand and the stored name would still say
 * "Balanced" while the loop ran something else. The gains are what
 * the controller uses, so they are what the picker reflects.
 */
function presetMatching(kp, ki, kd) {
  const same = (a, b) => Math.abs(Number(a) - Number(b)) < 1e-9;
  const found = PID_PRESETS.find((p) => same(p.kp, kp) && same(p.ki, ki)
                                     && same(p.kd, kd));
  return found ? found.name : "";
}

function applyPidPreset(name) {
  const preset = PID_PRESETS.find((p) => p.name === name);
  setText(document.getElementById("box-preset-note"), preset ? preset.why : "");
  if (!preset) return;
  for (const [id, value] of [["box-kp", preset.kp], ["box-ki", preset.ki],
                             ["box-kd", preset.kd]]) {
    document.getElementById(id).value = value;
  }
  boxEdited = true;
  drawPidPreview();
}

// What the preview assumes. Printed under the drawing, because a
// curve with hidden assumptions is a picture of nothing.
const BOX_DRIFT_PER_HOUR = 1.5;      // %RH the sealed box gains on its own
const FAN_CLEARS_PER_HOUR = 12;      // %RH a full-speed fan removes
const PREVIEW_MINUTES = 7;           // how often the sensor reports

/* How these gains would settle a box that drifts.
 *
 * A drawing of the arithmetic, not a prediction about this enclosure.
 * The box is modelled as rising on its own and falling in proportion
 * to the fan -- enough to show whether a set of gains overshoots,
 * hunts, or crawls, which is what somebody choosing between them
 * wants to see. The real answer is the chart above.
 */
function drawPidPreview() {
  const chart = document.getElementById("box-pid-preview");
  if (!chart) return;
  const number = (id) => Number(document.getElementById(id).value);
  const kp = number("box-kp"), ki = number("box-ki"), kd = number("box-kd");
  const target = number("box-target-rh") || 65;
  const highest = number("box-max-speed") || 1;
  if (![kp, ki, kd, target].every(Number.isFinite)) return;

  let rh = target + 15;                       // start well above, to watch it come down
  let integral = 0, lastRh = null;
  const points = [];
  const step = PREVIEW_MINUTES / 60;          // hours per reading
  for (let hour = 0; hour <= 8; hour += step) {
    const error = rh - target;
    let derivative = 0;
    if (kd && lastRh !== null) derivative = -kd * (rh - lastRh) / (step * 60);
    const trial = integral + ki * error * step * 60;
    let output = kp * error + trial + derivative;
    if ((output > 0 && output < highest) || (output <= 0 && trial > 0)
        || (output >= highest && trial < 0)) integral = trial;
    output = kp * error + integral + derivative;
    // The demand, delivered the way the fan can: below its floor it
    // pulses, so over a reading it averages out to what was asked.
    // Drawn as that average, since a pulse is shorter than a step
    // here and a sawtooth would say less than the line does.
    // Over a reading the pulse averages out to the demand, whether it
    // was delivered continuously or in bursts at the floor, so the
    // line is the demand itself.
    const speed = Math.max(0, Math.min(highest, output));
    lastRh = rh;
    points.push({ hour, rh, speed });
    rh += (BOX_DRIFT_PER_HOUR - FAN_CLEARS_PER_HOUR * speed) * step;
    rh = Math.max(0, Math.min(100, rh));
  }

  const width = 720, height = 200;
  const plot = { left: 46, right: 12, top: 12, bottom: 28 };
  const lowRh = Math.min(target - 5, ...points.map((p) => p.rh));
  const highRh = Math.max(target + 5, ...points.map((p) => p.rh));
  const x = (hour) => plot.left + (hour / 8) * (width - plot.left - plot.right);
  const y = (value) => plot.top + ((highRh - value) / Math.max(1, highRh - lowRh))
                               * (height - plot.top - plot.bottom);
  chart.replaceChildren();
  chart.append(svgElement("line", { class: "ideal-edge", x1: plot.left,
                                    x2: width - plot.right, y1: y(target), y2: y(target) }));
  chart.append(svgElement("text", { class: "axis-label", x: plot.left - 6,
                                    y: y(target) + 4, "text-anchor": "end" },
                          `${target}%`));
  chart.append(svgElement("polyline", {
    class: "history-line series-0",
    points: points.map((p) => `${x(p.hour).toFixed(1)},${y(p.rh).toFixed(1)}`).join(" "),
  }));
  chart.append(svgElement("polyline", {
    class: "history-line series-1",
    points: points.map((p) => `${x(p.hour).toFixed(1)},`
      + `${(height - plot.bottom - p.speed * (height - plot.top - plot.bottom) * 0.3)
           .toFixed(1)}`).join(" "),
  }));
  for (const hour of [0, 2, 4, 6, 8]) {
    chart.append(svgElement("text", { class: "axis-label", x: x(hour),
                                      y: height - 8, "text-anchor": "middle" },
                            `${hour}h`));
  }
  const settled = points.find((p) => Math.abs(p.rh - target) < 1);
  setText(document.getElementById("box-pid-caption"),
    `Starting 15 points high, on a box assumed to gain `
    + `${BOX_DRIFT_PER_HOUR}%/h on its own and lose ${FAN_CLEARS_PER_HOUR}%/h `
    + `at full fan, measured every ${PREVIEW_MINUTES} min. `
    + (settled ? `Within a point of target after ${settled.hour.toFixed(1)} h.`
               : "Does not reach the target in eight hours.")
    + " Those are assumed numbers, not yours -- the chart above is what really happened.");
}

let boxHours = 24;

/* Humidity and the fan, on one chart.
 *
 * Both are percentages, so they share an axis and the same renderer
 * every other chart here uses -- no second drawer, and the pointer
 * answers on both lines.
 */
async function refreshBoxHistory() {
  const chart = document.getElementById("box-chart");
  if (!chart) return;
  let data;
  try {
    const response = await fetch(`/api/fan/history?hours=${boxHours}`);
    if (!response.ok) return;
    data = await response.json();
  } catch (_error) {
    return;
  }
  const humidity = (data.humidity || [])
    .map((item) => ({
      timestamp: Date.parse(item.reading?.observed_at || item.received_at),
      value: item.reading?.air_humidity_percent,
    }))
    .filter((p) => Number.isFinite(p.timestamp) && Number.isFinite(p.value));

  // Drawn as steps: the fan holds whatever it was last told, so a
  // straight line between two changes would show it easing from one
  // speed to the other, which is not what happened.
  const fan = [];
  let held = null;
  for (const row of data.fan || []) {
    const at = Date.parse(row.at);
    if (!Number.isFinite(at)) continue;
    if (held !== null) fan.push({ timestamp: at, value: held });
    fan.push({ timestamp: at, value: row.speed * 100 });
    held = row.speed * 100;
  }
  // Carried to now, because the last thing it was told is still true.
  if (held !== null) fan.push({ timestamp: Date.now(), value: held });

  const series = [];
  if (humidity.length > 1) {
    series.push({ sensorId: "box:humidity", name: "Humidity", colour: 0,
                  points: humidity,
                  latest: humidity[humidity.length - 1].value.toFixed(1) });
  }
  if (fan.length > 1) {
    series.push({ sensorId: "box:fan", name: "Fan", colour: 1, points: fan,
                  latest: fan[fan.length - 1].value.toFixed(0) });
  }
  const began = Date.now() - boxHours * 3600 * 1000;
  renderChartLegend("box-legend", series);
  renderHistoryChart("box-chart", "box-chart-empty", series,
                     { ...PERCENT, spoken: "percent" }, began, Date.now());
}

async function saveBoxSettings(event) {
  event.preventDefault();
  const message = document.getElementById("box-message");
  const number = (id) => Number(document.getElementById(id).value);
  const mode = document.getElementById("box-mode").value;
  setText(message, "");
  let response;
  try {
    response = await fetch("/api/fan", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        // Enabled follows the mode: "off" is the way to switch it off,
        // so a second switch saying the same thing would be one too many.
        enabled: mode !== "off",
        mode,
        manual_speed: number("box-manual-speed"),
        target_rh: number("box-target-rh"),
        sensor_id: document.getElementById("box-sensor").value || null,
        kp: number("box-kp"), ki: number("box-ki"), kd: number("box-kd"),
        min_speed: number("box-min-speed"), max_speed: number("box-max-speed"),
        pulse_window_s: number("box-pulse-window"),
        moonraker_url: document.getElementById("box-url").value.trim(),
        fan_name: document.getElementById("box-fan-name").value.trim(),
      }),
    });
  } catch (_error) {
    setText(message, "The hub could not be reached. Nothing was saved.");
    return;
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    setText(message, payload.error || "That could not be saved.");
    return;
  }
  boxEdited = false;
  setText(message, mode === "off" ? "Saved. The hub will not touch the fan."
                                  : "Saved. It takes effect within a minute.");
  await refreshBoxPage();
}

async function refreshAlertSettings() {
  const field = document.getElementById("alerts-url");
  // Left alone while it is being typed into: a poll that puts the
  // saved value back under somebody mid-edit is how an address gets
  // half entered and saved.
  if (!field || field === document.activeElement) return;
  try {
    const response = await fetch("/api/settings");
    if (!response.ok) return;
    field.value = (await response.json()).ntfy_url || "";
    showAlertTopic(field.value);
  } catch (_error) {
    /* the panel is still usable without it */
  }
}

async function saveAlertSettings(event) {
  event.preventDefault();
  const message = document.getElementById("alerts-message");
  setText(message, "");
  let response;
  try {
    response = await fetch("/api/settings/alerts", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ntfy_url: document.getElementById("alerts-url").value.trim(),
      }),
    });
  } catch (_error) {
    setText(message, "The hub could not be reached. Nothing was saved.");
    return;
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    setText(message, payload.error || "That address could not be saved.");
    return;
  }
  const saved = await response.json();
  showAlertTopic(saved.ntfy_url);
  setText(message, saved.ntfy_url
    ? "Saved. Send a test to be sure it arrives."
    : "Saved. The hub will not send alerts.");
}

async function testAlert(event) {
  const button = event.currentTarget;
  const message = document.getElementById("alerts-message");
  button.disabled = true;
  setText(message, "Sending...");
  try {
    const response = await fetch("/api/settings/alerts/test", { method: "POST" });
    const result = await response.json().catch(() => ({}));
    // The hub waits for this one, unlike a real alarm, because
    // somebody is watching the button and wants to be told.
    setText(message, result.sent
      ? "Sent. It should be on the phone now."
      : result.error || "It did not go.");
  } catch (_error) {
    setText(message, "The hub could not be reached.");
  } finally {
    button.disabled = false;
  }
}

/* The devices that can raise an alarm, so the page is honest about
   there being none rather than implying the hub watches everything. */
function renderAlertSources() {
  const list = document.getElementById("alerts-sources");
  if (!list) return;
  const sources = (zigbeeDevices || []).filter((device) => device.kind === "leak");
  if (!renderKeyChanged(list, sources)) return;
  list.replaceChildren();
  if (!sources.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    setText(empty, "No leak sensors are enrolled, so nothing can raise an alarm.");
    list.append(empty);
    return;
  }
  for (const device of sources) {
    const card = document.createElement("div");
    card.className = "settings-sensor";
    const heading = document.createElement("div");
    heading.className = "settings-sensor-heading";
    const link = document.createElement("a");
    link.href = `/zigbee/${device.eui}`;
    setText(link, device.name || device.eui);
    heading.append(link);
    const state = document.createElement("small");
    setText(state, device.last?.wet ? "Wet now" : "Dry");
    heading.append(state);
    const note = document.createElement("code");
    setText(note, `${device.model || "leak sensor"} \u00b7 battery `
                  + `${device.last?.batt_pct ?? "--"}%`);
    card.append(heading, note);
    list.append(card);
  }
}

function renderRoomsPage() {
  const grid = document.getElementById("rooms-grid");
  if (!grid) return;
  setHidden(document.getElementById("rooms-empty"), roomsOverview.length > 0);
  if (!renderKeyChanged(grid, [roomsOverview, [...roomHistory.keys()].sort(),
                               [...roomHistory.values()].map((h) => h.items.length),
                               // Which series are hidden decides what is
                               // drawn, so it decides whether to redraw.
                               // Without it, switching a name back on did
                               // nothing: the readings had not changed, the
                               // grid saw no reason to repaint, and the room
                               // stayed empty until the page was reloaded.
                               [...hiddenSeries].sort()])) return;
  grid.replaceChildren();
  for (const room of roomsOverview) grid.append(roomCard(room));
  // After they are in the document: renderHistoryChart finds its
  // elements by id, which only works once they exist.
  for (const room of roomsOverview) {
    // One chart per measurement with a line per sensor, rather than a
    // chart pair per sensor: two thermometers in a room are worth
    // comparing, and stacked separately they could not be.
    const sources = room.air_sources || [];
    const held = new Map(sources
      .map((source) => [source.sensor_id,
                        (roomHistory.get(`${room.room_id}:${source.sensor_id}`) || {}).items || []]));
    const end = Math.max(...sources
      .map((s) => (roomHistory.get(`${room.room_id}:${s.sensor_id}`) || {}).end || 0), 0);
    if (!end) continue;
    for (const metric of ROOM_AIR_METRICS) {
      const series = historySeries(held, metric.key, sources, metric.decimals);
      renderChartLegend(`room-${room.room_id}-legend`, series);
      renderHistoryChart(`room-${room.room_id}-${metric.slug}-chart`,
                         `room-${room.room_id}-${metric.slug}-empty`,
                         series, metric.unit, end - ROOM_HISTORY_MS, end);
    }
  }
}

function roomCard(room) {
  const card = document.createElement("article");
  card.className = "room-card";

  const heading = document.createElement("div");
  heading.className = "room-card-heading";
  const name = document.createElement("h2");
  const link = document.createElement("a");
  link.href = `/rooms/${room.room_id}`;
  setText(link, room.name);
  name.append(link);
  heading.append(name);
  const what = document.createElement("p");
  what.className = "note mute";
  const bits = [
    tally(room.plant_count, "plant"),
    tally(room.sensor_count, "sensor"),
  ];
  setText(what, bits.join(" · "));
  heading.append(what);
  card.append(heading);

  if (!room.air) {
    const none = document.createElement("p");
    none.className = "history-chart-empty";
    setText(none, "Nothing is measuring the air in here yet.");
    card.append(none);
    return card;
  }

  const figures = document.createElement("dl");
  figures.className = "ph-figures room-figures";
  for (const [label, value] of [
    ["Temperature", `${room.air.air_temperature_c} °C`],
    ["Humidity", `${room.air.air_humidity_percent}%`],
    // How hard the air pulls water out of a leaf, which is what actually
    // drives watering -- warm and damp is gentler than cool and dry.
    ["Drying power", `${room.air.drying_kpa} kPa`],
  ]) {
    const pair = document.createElement("div");
    const term = document.createElement("dt");
    setText(term, label);
    const detail = document.createElement("dd");
    setText(detail, value);
    pair.append(term, detail);
    figures.append(pair);
  }
  card.append(figures);

  const measured = document.createElement("p");
  measured.className = "note mute";
  const age = (Date.now() - Date.parse(room.air.observed_at)) / 1000;
  setText(measured, `Measured by ${room.air.sensor_name}, `
    + (Number.isFinite(age) ? `${describeSeconds(age)} ago` : "at an unknown time"));
  card.append(measured);

  // The same Air conditions block the plant page uses, not a second
  // way of drawing the same thing. Identical markup means identical
  // styling, and calling the same renderer means the gridlines, the
  // axis labels and the breaks across outages all behave the same
  // here as they do there.
  // One block of charts per sensor. Where there is more than one, each
  // is named: "25.1 °C" means something different at the window than
  // by the door, and a card that shows two unlabelled pairs of charts
  // is a puzzle rather than a reading.
  const sources = room.air_sources || [];
  const anyHistory = sources.some((source) => {
    const held = roomHistory.get(`${room.room_id}:${source.sensor_id}`);
    return held && held.items.length > 1;
  });
  if (anyHistory) {
    const legend = document.createElement("div");
    legend.className = "chart-legend";
    legend.id = `room-${room.room_id}-legend`;
    card.append(legend);
    const grid = document.createElement("div");
    grid.className = "climate-chart-grid";
    for (const metric of ROOM_AIR_METRICS) grid.append(roomChartCard(room, metric));
    card.append(grid);
  }
  return card;
}


const ROOM_AIR_METRICS = [
  { key: "air_temperature_c", label: "Temperature",
    unit: CELSIUS, slug: "temperature", decimals: 1 },
  { key: "air_humidity_percent", label: "Relative humidity",
    unit: { ...PERCENT, spoken: "percent relative humidity" },
    slug: "humidity", decimals: 0 },
];

function roomChartCard(room, metric) {
  const article = document.createElement("article");
  article.className = "climate-chart-card";
  const header = document.createElement("header");
  const name = document.createElement("span");
  setText(name, metric.label);
  const value = document.createElement("strong");
  const number = document.createElement("b");
  setText(number, (room.air ?? {})[metric.key] ?? "--");
  const unit = document.createElement("small");
  setText(unit, metric.unit.symbol);
  value.append(number, unit);
  header.append(name, value);
  article.append(header);

  const chart = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  chart.id = `room-${room.room_id}-${metric.slug}-chart`;
  chart.setAttribute("class", "history-chart");
  chart.setAttribute("viewBox", "0 0 720 220");
  chart.setAttribute("role", "img");
  chart.setAttribute("aria-label",
                     `${metric.label} in ${metric.unit.spoken} over the last day`);
  const empty = document.createElement("p");
  empty.id = `room-${room.room_id}-${metric.slug}-empty`;
  empty.className = "history-chart-empty";
  setText(empty, `No ${metric.label.toLowerCase()} readings in this range.`);
  article.append(chart, empty);
  return article;
}

function pageFromLocation() {
  const path = window.location.pathname;
  /* A sensor's configuration is its own page.
   *
   * Plant care and device administration are different jobs done at different
   * times, and the forms for the second were crowding the first. */
  if (path.startsWith("/plants/") && path.endsWith("/config")) return "plant-config";
  /* A sensor's own configuration has its own address again.
   *
   * It was folded into the plant's page while every sensor had a plant. A
   * scale does not: it is adopted, it sits in the sensors list, and it may
   * never be pointed at anything. Administering a device through a plant it
   * might not have was the wrong shape. */
  if (path.startsWith("/sensors/") && path.endsWith("/settings")) return "sensor-config";
  if (path.startsWith("/plants/")) return "plant";
  /* A sensor's address shows the sensor.
   *
   * It used to show a plant, because a sensor was how a plant existed: the
   * readings, the chemistry, the care log all hung off the device. Plants
   * are their own records now and a scale is a sensor with no plant at
   * all, so what lives at a sensor's address is the sensor. */
  if (path.startsWith("/sensors/")) return "sensor-config";
  /* A Zigbee device's controls are its own page, and per kind.
   *
   * The list had tare and Wi-Fi buttons on every row, which only worked
   * while every device was a scale. What a device can do belongs with the
   * device, not spread across a table that has to know about all of them. */
  if (path.startsWith("/zigbee/")) return "zigbee-device";
  if (path === "/rooms") return "rooms";
  if (path.startsWith("/rooms/")) return "room";
  if (path === "/settings" || path.startsWith("/settings/")) return "settings";
  if (path === "/onboarding" || path.startsWith("/onboarding/")) return "onboarding";
  return "fleet";
}

const SETTINGS_TABS = ["sensors", "rooms", "lights", "wifi", "time",
                       "firmware", "zigbee", "alerts", "box"];
const SETTINGS_TAB_KEY = "opp.settings.tab";

function settingsTabFromLocation() {
  const path = window.location.pathname;
  const named = path.startsWith("/settings/") ? path.slice("/settings/".length) : "";
  if (SETTINGS_TABS.includes(named)) return named;
  // Bare /settings means "the one I was last on", which is remembered
  // per browser rather than on the hub: which tab somebody had open is
  // a convenience for them, not a setting of the house.
  return rememberedSettingsTab();
}

function rememberedSettingsTab() {
  try {
    const kept = window.localStorage.getItem(SETTINGS_TAB_KEY);
    if (SETTINGS_TABS.includes(kept)) return kept;
  } catch (_error) {
    // Private windows and blocked site data both throw here. Settings
    // opening on Sensors is a smaller loss than the page not opening.
  }
  return "sensors";
}

function rememberSettingsTab(tab) {
  if (!SETTINGS_TABS.includes(tab)) return;
  try {
    window.localStorage.setItem(SETTINGS_TAB_KEY, tab);
  } catch (_error) {
    /* nothing is lost that was not already unavailable */
  }
}

function sensorIdFromLocation() {
  const match = window.location.pathname.match(/^\/sensors\/([^/]+)(?:\/settings)?$/);
  return match ? decodeURIComponent(match[1]) : null;
}

function sensorQuery() {
  return selectedSensorId ? `?${new URLSearchParams({ sensor_id: selectedSensorId })}` : "";
}

/* The long explanatory notes, folded away.
 *
 * They earn their length the first time somebody meets a feature and
 * are in the way every time after, so each one collapses to its first
 * line and remembers whether it was opened. Kept per note rather than
 * one switch for all of them: the note about what a weighing records
 * is worth keeping open long after the one about pot weights is not.
 *
 * The key is the note's own words. Reordering the page keeps a note's
 * state, and rewriting a note resets it -- which is the right way
 * round, because a rewritten note is one worth reading again.
 */
const NOTE_STATE_PREFIX = "open-plant-pulse.note.";

function noteKey(note) {
  /* An id where the note has one, because some notes are written by the
     page and are empty when this runs -- keyed by their words they would
     answer to one key while blank and another once filled. */
  if (note.id) return NOTE_STATE_PREFIX + note.id;
  const words = (note.textContent || "").replace(/\s+/g, " ").trim().slice(0, 120);
  let hash = 0;
  for (let index = 0; index < words.length; index += 1) {
    hash = (hash * 31 + words.charCodeAt(index)) | 0;
  }
  return NOTE_STATE_PREFIX + (hash >>> 0).toString(36);
}

function noteIsOpen(note) {
  try {
    return window.localStorage.getItem(noteKey(note)) === "open";
  } catch (error) {
    // Private windows and blocked site data throw rather than return
    // null; a note that cannot remember simply starts closed.
    return false;
  }
}

function rememberNote(note, open) {
  try {
    window.localStorage.setItem(noteKey(note), open ? "open" : "shut");
  } catch (error) {
    /* Nothing to do: the fold still works for this visit. */
  }
}

/* Applied after renders as well as at load, because some notes are
   written by the page rather than sitting in the markup. */
function foldNotes() {
  /* Every note, including the ones still empty: marking one later, on
     the poll that fills it, is the page changing under somebody's
     hands. Done once each, at the first render. */
  for (const note of document.querySelectorAll(".settings-note")) {
    if (!note.dataset.foldable) {
      setData(note, "foldable", "true");
      setAttr(note, "role", "button");
      setAttr(note, "tabindex", "0");
      const open = noteIsOpen(note);
      if (!open) setData(note, "folded", "true");
      setAttr(note, "aria-expanded", String(open));
    }
  }
}

function toggleNote(note) {
  const open = "folded" in note.dataset;   // folded now means opening
  if (open) delete note.dataset.folded;
  else setData(note, "folded", "true");
  setAttr(note, "aria-expanded", String(open));
  rememberNote(note, open);
}

document.addEventListener("click", (event) => {
  const note = event.target.closest(".settings-note[data-foldable]");
  // A link or a bold word inside a note is still part of the note; only
  // a link needs to be left alone.
  if (!note || event.target.closest("a, button, input, select")) return;
  toggleNote(note);
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" && event.key !== " ") return;
  const note = event.target.closest?.(".settings-note[data-foldable]");
  if (!note) return;
  event.preventDefault();
  toggleNote(note);
});

function renderPage() {
  const page = pageFromLocation();
  /* A section may serve more than one page: the lighting form is the same
   * form whether the plant has a sensor or not. A section may also belong
   * to one kind of device, and that is decided here too rather than being
   * undone further down: showing every section and then hiding the ones
   * that do not belong flipped their hidden attribute on every render, and
   * an element that is focused when its section goes away loses the focus
   * and does not get it back. */
  const shownKind = sensorKind(selectedSensor);
  document.querySelectorAll("[data-page]").forEach((element) => {
    const rightPage = element.dataset.page.split(" ").includes(page);
    const rightKind = element.dataset.sensorKind === undefined
      || element.dataset.sensorKind.split(" ").includes(shownKind);
    /* A section that decides for itself is left alone once it is on the
       right page: showing it here and letting its renderer hide it again
       is the same flip this function was written to stop, and at one
       render a second it reads as the page blinking. Off the page it is
       hidden like anything else. */
    if (element.dataset.ownVisibility && rightPage && rightKind) return;
    setHidden(element, !(rightPage && rightKind));
  });
  foldNotes();
  setClass(document.getElementById("fleet-link"), "active", page === "fleet");
  setClass(document.getElementById("rooms-link"), "active",
           page === "rooms" || page === "room");
  setClass(document.getElementById("settings-link"), "active", page === "settings");
  if (selectedSensorId) {
    /* Configuring the device and configuring the plant are different jobs.
     *
     * This button used to divert to the plant's page whenever the sensor
     * had one, which left a sensor with no plant -- a scale, say -- with
     * nowhere to be configured, and buried the device's own settings under
     * a plant that merely happens to be in front of it. */
  }
  if (page === "plant" || page === "plant-config") {
    const plantId = plantIdFromLocation();
    if (plantId !== null) {
      setAttr(document.getElementById("plant-page-config"), "href", `/plants/${plantId}/config`);
    }
  }
  if (page === "room") {
    if (addingARoom()) {
      renderNewRoomPage();
    } else if (shownRoom && shownRoom.room_id === currentRoomId()) {
      renderRoomPage();
    } else {
      // Another room's details are not this room's. Blank until
      // its own arrive, rather than the last one under a new name.
      shownRoom = null;
      for (const id of ["room-page-plants-section", "room-page-sensors-section",
                        "room-page-danger-section"]) {
        setHidden(document.getElementById(id), true);
      }
    }
  }
  if (page === "plant-config") {
    // The device panels have their own page now. What is left here is the
    // plant's own business: which sensor it has, if any.
    const watched = plantSensor() !== null;
    setHidden(document.getElementById("plant-sensor-attach"), watched);
    if (!watched) renderFreeSensors();
  }
  if (page === "sensor-config") {
    /* Back to where a sensor was found. The plant is not it: this page is
     * reached from the sensors list, and a scale may have no plant at all. */
    const back = document.getElementById("plant-config-back");
    if (back !== null) {
      setAttr(back, "href", "/settings");
      setText(back, "Back to sensors");
    }
    /* Different instruments, different page.
     *
     * A probe in the soil and a scale under the pot share almost nothing:
     * one is configured over Bluetooth, reports on an interval the hub
     * asks for, has firmware the hub can push and a pH to calibrate; the
     * other is an address that answers with a number. Rather than one
     * page quietly hiding most of itself, each section says which kind of
     * sensor it belongs to and the page is composed from that. */
    // Which sections belong to this kind of device is settled above, in the
    // one pass that decides what is shown.
    if (sensorKind(selectedSensor) === "bthome") renderSensorFirmware();
  }
  /* A Zigbee device reached at a sensor address goes to the Zigbee page.
   *
   * It is a sensor row, so the address resolves; but it is administered
   * where its gateway is, and showing a second, emptier configuration page
   * for the same device was the parallel-system mistake one level up. */
  if (page === "sensor-config" && sensorKind(selectedSensor) === "zigbee") {
    const eui = selectedSensor?.identity_value;
    if (eui) {
      navigate(`/zigbee/${eui}`);
      return;
    }
  }
  if (page === "settings") renderSettingsTab();
  if (page === "zigbee-device") renderZigbeeDevicePage();
  if (page === "onboarding") renderOnboarding();
  document.title = titleForPage(page);
}

function titleForPage(page) {
  if (page === "sensor-config" && selectedSensor) {
    return `${selectedSensor.display_name || selectedSensor.sensor_id} · Sensor · Open Plant Pulse`;
  }
  if (page === "plant") {
    return `${selectedPlant?.display_name || "Plant"} · Open Plant Pulse`;
  }
  if (page === "rooms") return "Rooms · Open Plant Pulse";
  if (page === "room") return `${shownRoom?.name || "Room"} · Open Plant Pulse`;
  if (page === "settings") return "Settings · Open Plant Pulse";
  if (page === "zigbee-device") return "Device · Zigbee · Open Plant Pulse";
  if (page === "onboarding") return "Add a sensor · Open Plant Pulse";
  return "Plants · Open Plant Pulse";
}

function navigate(path) {
  window.history.pushState({}, "", path);
  renderPage();
  // The new page asks for what it shows straight away rather than waiting for
  // the next tick of the poll: a page a second behind looks like a page still
  // loading, and the page being arrived at is the one nobody has data for.
  refresh();
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


/* Every card opens with a cover, photograph or not.
 *
 * A plant without one gets the same band holding a leaf, so the tiles are a
 * grid rather than a ragged edge, and so the gap is plainly a photograph
 * waiting to be taken rather than something missing. */
function setFleetCardCover(card, photoId, digest) {
  const existing = card.querySelector(".fleet-card-cover");
  const wanted = photoId == null ? "" : photoUrl(photoId, digest);
  if (existing !== null && (existing.getAttribute("src") || "") === wanted) return;
  existing?.remove();
  if (photoId == null) {
    const blank = document.createElement("div");
    blank.className = "fleet-card-cover fleet-card-cover-blank";
    blank.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true">'
      + '<path d="M12 21c0-6 2-10 7-13-1 7-3 11-7 13z"/>'
      + '<path d="M12 21C8 18 5 14 5 9c4 2 6 6 7 12z"/>'
      + '<path d="M12 21v-3"/></svg>';
    card.prepend(blank);
    return;
  }
  const cover = document.createElement("img");
  cover.className = "fleet-card-cover";
  setAttr(cover, "src", wanted);
  cover.loading = "lazy";
  cover.alt = "";
  card.prepend(cover);
}



/* The household, as one grid.
 *
 * A plant with a sensor and a plant without are the same kind of thing and
 * get the same tile: the sensor changes what the tile can say, not how big
 * it is or which list it belongs to. The plants are the subject, so the list
 * comes from /api/plants and a sensor is looked up against it.
 */
function renderHousehold() {
  const grid = document.getElementById("fleet-grid");
  if (grid === null) return;
  const sensorsByPlant = new Map(
    fleetSensors.filter((sensor) => sensor.plant_id != null && !isScale(sensor))
      .map((sensor) => [sensor.plant_id, sensor])
  );
  /* Plants, and only plants.
   *
   * A sensor with no plant used to get a tile here so that enrolling one
   * appeared to do something. It is found in Settings now, which is where
   * a device belongs, and a page called Plants that lists two of them and
   * a scale is not a page about plants. */
  const tiles = sortHousehold(
    householdPlants.map((plant) => ({ plant, sensor: sensorsByPlant.get(plant.plant_id) }))
  );

  /* Cards are kept and updated rather than rebuilt. The page refreshes every
   * second, and replacing the grid would reload every cover photograph and
   * lose whatever the pointer was over. */
  const existing = new Map(
    Array.from(grid.querySelectorAll(".fleet-card"), (card) => [card.dataset.key, card])
  );
  const keys = new Set(tiles.map(tileKey));
  for (const [key, card] of existing) {
    if (!keys.has(key)) card.remove();
  }
  if (!tiles.length) {
    if (grid.querySelector(".empty-state") === null) {
      const empty = document.createElement("p");
      empty.className = "empty-state";
      setText(empty, "No plants yet. Add one, or add a sensor to start a plant.");
      grid.append(empty);
    }
    return;
  }
  grid.querySelector(".empty-state")?.remove();
  tiles.forEach((tile, index) => {
    const key = tileKey(tile);
    const card = existing.get(key) || createHouseholdCard(key);
    updateHouseholdCard(card, tile.plant, tile.sensor);
    const cardAtIndex = grid.children[index];
    if (cardAtIndex !== card) grid.insertBefore(card, cardAtIndex || null);
  });
}


function timeAtHomeParts(since) {
  if (!since) return null;
  const days = Math.floor((Date.now() - Date.parse(since)) / 86400000);
  if (!Number.isFinite(days) || days < 0) return null;
  if (days < 1) return { short: "today", long: "Came home today" };
  if (days < 60) {
    return { short: `${days}d`, long: `At home ${days} day${days === 1 ? "" : "s"}` };
  }
  const months = Math.round(days / 30.44);
  if (days < 730) return { short: `${months}mo`, long: `At home ${months} months` };
  const years = Math.floor(days / 365.25);
  const extra = Math.round((days - years * 365.25) / 30.44);
  const plural = `${years} year${years === 1 ? "" : "s"}`;
  return extra >= 1 && extra < 12
    ? { short: `${years}y ${extra}mo`, long: `At home ${plural} and ${extra} months` }
    : { short: `${years}y`, long: `At home ${plural}` };
}

/* When it was last watered and when it was last weighed: two different
 * facts, so both are said where both are known. Either on its own reads as
 * the whole story, which is how a card came to say "not watered yet" beside
 * a figure for how much water was left. */
/* Watered, fed and weighed, as marks rather than words.
 *
 * Three sentences will not fit across a card, and the words are the same
 * on every card anyway -- the dates are what differ. A drop for water is
 * about as unambiguous as a drawing gets; a feed bottle is less so, which
 * is why each mark carries its word for a pointer and for anyone
 * listening to the page. */
const CARE_MARKS = {
  // A drop, filled, because water is the one that should read instantly.
  watered: {
    word: "Watered",
    fill: true,
    paths: ["M8 1.5c0 0 5.2 6.1 5.2 8.8a5.2 5.2 0 0 1-10.4 0C2.8 7.6 8 1.5 8 1.5z"],
  },
  // A dropper: the bulb, the tube, and a drop leaving the tip. Liquid feed
  // is the thing measured out with one, and the silhouette is nothing like
  // the water drop beside it, which matters more at this size than detail.
  fed: {
    word: "Fed",
    // Filled, like the drop: at thirteen pixels an outline of this becomes
    // a thin vertical line and reads as a thermometer. A fat bulb over a
    // tapering barrel is a pipette even when it is this small.
    fill: true,
    paths: [
      "M5.6 1.3h4.8a1.3 1.3 0 0 1 1.3 1.3v1.7a1.3 1.3 0 0 1-1.3 1.3H5.6a1.3 1.3 0 0 1-1.3-1.3V2.6a1.3 1.3 0 0 1 1.3-1.3z",
      "M6.5 5.2h3v5.3L8 14.2l-1.5-3.7z",
    ],
  },
  // A balance: beam, pans, post and foot.
  weighed: {
    word: "Weighed",
    fill: false,
    paths: [
      "M3 4.6h10",
      "M8 3.2v9.4",
      "M5.6 13.8h4.8",
      "M1.4 4.8 3 8.2l1.6-3.4",
      "M11.4 4.8 13 8.2l1.6-3.4",
    ],
  },
};

function careMark(kind, since) {
  const mark = CARE_MARKS[kind];
  const wrapper = document.createElement("span");
  wrapper.className = "care-mark";
  setAttr(wrapper, "title", `${mark.word} ${since}`);
  const glyph = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  glyph.setAttribute("viewBox", "0 0 16 16");
  glyph.setAttribute("aria-hidden", "true");
  glyph.classList.toggle("filled", mark.fill);
  for (const drawing of mark.paths) {
    const shape = document.createElementNS("http://www.w3.org/2000/svg", "path");
    shape.setAttribute("d", drawing);
    glyph.append(shape);
  }
  // The word, once: the date beside it is already on the page, and saying
  // it twice reads as "Watered today today" to anything listening.
  const said = document.createElement("span");
  said.className = "visually-hidden";
  setText(said, `${mark.word} `);
  const when = document.createElement("i");
  setText(when, since);
  wrapper.append(glyph, said, when);
  return wrapper;
}

function renderCaredFor(into, plant) {
  const marks = [
    ["watered", daysSince(plant.last_watered_at)],
    ["fed", daysSince(plant.last_fed_at)],
    ["weighed", daysSince(plant.weight?.latest?.at)],
  ].filter(([, since]) => since);
  if (!renderKeyChanged(into, marks)) return;
  into.replaceChildren();
  if (!marks.length) {
    setText(into, "Not watered yet");
    return;
  }
  for (const [kind, since] of marks) into.append(careMark(kind, since));
}

function tileKey(tile) {
  return `plant-${tile.plant.plant_id}`;
}

function createHouseholdCard(key) {
  const card = document.createElement("a");
  card.className = "fleet-card";
  setData(card, "key", key);
  card.innerHTML = "<strong></strong><code></code><span></span>"
    // When it needs water first: that is what the list is scanned for.
    + '<div class="fleet-card-tags"><small class="fleet-card-due"></small>'
    + '<small></small><small class="fleet-card-rate" data-state="rate"></small></div>';
  return card;
}

const HOUSEHOLD_SORT_KEY = "open-plant-pulse.plant-sort";

function householdSort() {
  const chosen = document.getElementById("plant-sort");
  return chosen ? chosen.value : "name";
}

function tileName(tile) {
  return tile.plant.display_name.toLowerCase();
}

/* What kind of plant this is, for grouping.
 *
 * The profile's name rather than its id, because that is what the rest
 * of the page calls it: sorting by id would file Bird of paradise
 * under S. A plant with no profile chosen sorts last rather than under
 * whatever the default happens to be called.
 */
function tileProfile(tile) {
  const id = tile.plant.profile_id;
  if (!id) return "\uffff";
  return (profiles?.profiles?.[id]?.name || id).toLowerCase();
}

/* How much water is left, as a percentage, for ordering only.
 *
 * A weighed pot and a probe are not measuring the same quantity -- one is
 * the water in the whole pot, the other the water around two centimetres of
 * needle -- so this is a rough answer to "who needs me first", not a figure
 * to show. A plant that cannot say sorts last rather than sorting as zero
 * and jumping the queue. */
function tileThirst(tile) {
  const weight = tile.plant.weight;
  const left = weight?.remaining_percent;
  if (left != null) return left - (weight.thirsty_percent ?? 0);
  const moisture = tile.sensor?.latest?.reading?.moisture_percent;
  return moisture != null ? Number(moisture) : Infinity;
}

function sortHousehold(tiles) {
  const by = householdSort();
  const compareName = (a, b) => tileName(a).localeCompare(tileName(b));
  const sorted = [...tiles];
  if (by === "thirst") {
    sorted.sort((a, b) => tileThirst(a) - tileThirst(b) || compareName(a, b));
  } else if (by === "watered") {
    // Never watered is the longest of all, so it sorts to the front.
    const when = (tile) => tile.plant.last_watered_at
      ? Date.parse(tile.plant.last_watered_at) : -Infinity;
    sorted.sort((a, b) => when(a) - when(b) || compareName(a, b));
  } else if (by === "room") {
    const room = (tile) => (tile.plant.room || "\uffff").toLowerCase();
    sorted.sort((a, b) => room(a).localeCompare(room(b)) || compareName(a, b));
  } else if (by === "profile") {
    sorted.sort((a, b) => tileProfile(a).localeCompare(tileProfile(b))
                          || compareName(a, b));
  } else {
    sorted.sort(compareName);
  }
  return sorted;
}

function restoreHouseholdSort() {
  const chosen = document.getElementById("plant-sort");
  if (chosen === null) return;
  try {
    const remembered = window.localStorage.getItem(HOUSEHOLD_SORT_KEY);
    if (remembered && [...chosen.options].some((option) => option.value === remembered)) {
      chosen.value = remembered;
    }
  } catch (_error) {
    /* Name is the default either way. */
  }
  chosen.addEventListener("change", () => {
    try {
      window.localStorage.setItem(HOUSEHOLD_SORT_KEY, chosen.value);
    } catch (_error) {
      /* The order still changes; it just forgets. */
    }
    renderHousehold();
  });
}

function updateHouseholdCard(card, plant, sensor) {
  /* A plant's tile leads to the plant. This grid holds nothing else.
   *
   * It used to lead to the sensor when the plant had one, because that was
   * where the measurements lived, and it could draw a tile for a sensor
   * with no plant at all. Both were the old shape, where a sensor was how
   * a plant existed. */
  setAttr(card, "href", `/plants/${plant.plant_id}`);

  /* A wall of names is harder to read than a wall of plants, and the picture
   * is the quickest way to know which "marble queen small pot" this is. */
  setFleetCardCover(card,
    plant.cover_photo_id, plant.cover_photo_digest);

  const name = card.querySelector("strong");
  setText(name, plant.display_name);
  /* How long it has lived here, which is the thing about a houseplant that
   * a number can actually say. The room is the fallback, since a plant that
   * has never been asked should not be given a made-up answer. */
  const where = card.querySelector("code");
  const home = plant && timeAtHomeParts(plant.acquired_at || plant.created_at);
  /* How old it is, beside how long it has been here. They are different
     questions and often different answers -- a plant bought mature is
     old and newly arrived -- and the age is what decides how its light
     is read, so it is worth seeing without opening the plant. */
  const age = plant && timeAtHomeParts(plant.started_at);
  const parts = [
    home ? `Home ${home.short}` : null,
    age ? `${age.short} old` : null,
  ].filter(Boolean);
  setText(where, parts.length ? parts.join(" \u00b7 ")
    : (plant?.room || sensor?.room || "no room recorded"));
  setAttr(where, "title", [
    home ? home.long : null,
    age ? age.long.replace("At home", "Growing for") : null,
  ].filter(Boolean).join(" \u00b7 "));

  /* The last thing that happened to this plant, by whichever instrument
   * knows. A pot can be weighed without the watering being logged, and a
   * card that says "not watered yet" beside "47% water left" is two true
   * sentences that read as nonsense together. */
  /* Watered, fed and weighed are facts about the plant, not about how it
   * is measured, so every card carries them. A plant with a probe used to
   * show when the probe last spoke instead -- which is the sensor's health,
   * and the badge beside it already says that. */
  renderCaredFor(card.querySelector("span"), plant);

  const badge = card.querySelector("small:not(.fleet-card-rate):not(.fleet-card-due)");
  if (sensor) {
    const state = sensorState(sensor);
    setText(badge, state);
    setHidden(badge, false);
    setData(badge, "state", state.toLowerCase());
  } else {
    /* What the pot weighs answers the question the card is really asked:
     * does this plant need me today. */
    const weight = plant.weight || {};
    const left = weight.remaining_percent != null
      ? `${weight.remaining_percent}% water left`
      : weight.used_grams ? `${weight.used_grams} ml used` : "";
    setText(badge, left);
    setHidden(badge, left === "");
    setData(badge, "state", weight.remaining_percent == null ? "manual"
      : weight.remaining_percent <= (weight.thirsty_percent ?? 15)
        ? "thirsty" : "watered");
  }

  /* What it drinks, beside what it has left: one is the plant's habit and
   * the other where it has got to, and they read together. */
  const rate = card.querySelector(".fleet-card-rate");
  setText(rate, plant?.daily_grams != null ? `${plant.daily_grams} ml/d` : "");
  setHidden(rate, plant?.daily_grams == null);

  /* When it will want water, which is the question the list is scanned for.
   * Only where the hub can actually say; the plant's page explains why not. */
  const due = card.querySelector(".fleet-card-due");
  const days = plant?.water_in_days;
  setText(due, days == null ? ""
    : days < 1 ? "Water now" : `Water in ${Math.round(days)}d`);
  setHidden(due, days == null);
  setData(due, "state", days == null ? "unknown" : days < 1 ? "now" : "later");
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

/* The probe's pH, what it is shown as, and the one number that connects
 * them. Rendered from the sensor because the offset describes the piece of
 * hardware, not the plant that happens to be in front of it. */
function renderSoilPhCalibration() {
  const panel = document.getElementById("sensor-ph-panel");
  if (panel === null) return;
  const sensor = plantSensor() || selectedSensor;
  const status = sensor?.soil_chemistry;
  setHidden(panel, !sensor || isScale(sensor));
  if (!status || isScale(sensor)) return;

  setText(document.getElementById("ph-raw"),
    status.raw === null ? "--" : status.raw.toFixed(1));
  setText(document.getElementById("ph-corrected"),
    status.ph === null ? "--" : status.ph.toFixed(1));
  setText(document.getElementById("ph-offset"),
    status.calibrated ? `${status.offset >= 0 ? "+" : ""}${status.offset.toFixed(1)}`
                      : "none");
  setText(document.getElementById("ph-measured-at"),
    status.at === null ? "never" : `${describeAge(status.age_seconds)} ago`);

  const note = document.getElementById("ph-reference-note");
  setText(note, status.calibrated
    ? `Calibrated to ${status.reference} against a probe reading of `
      + `${status.reference_raw} at ${status.reference_ec ?? "unknown"} uS/cm.`
    : "Taken against the probe's last pH reading and the conductivity at the time.");

  // A correction found at one conductivity is not promised at another.
  const caution = document.getElementById("ph-caution");
  setHidden(caution, !status.far_from_calibration);
  if (status.far_from_calibration) {
    setText(caution, `This was calibrated at ${status.reference_ec} uS/cm and the `
      + `probe is now in ${status.conductivity_us_cm} uS/cm. The probe's pH moves `
      + `with conductivity, so the corrected figure is outside the conditions it `
      + `was set in. Calibrate again where it is now, if that is where it lives.`);
  }

  const field = document.getElementById("ph-reference");
  if (field !== null && field !== document.activeElement && field.value === "") {
    field.value = status.reference === null ? "" : String(status.reference);
  }
}

function describeAge(seconds) {
  if (seconds === null || seconds === undefined) return "never";
  if (seconds < 90) return `${Math.round(seconds)}s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min`;
  if (seconds < 172800) return `${Math.round(seconds / 3600)} h`;
  return `${Math.round(seconds / 86400)} days`;
}

async function saveSoilPhCalibration(reference) {
  const sensor = plantSensor() || selectedSensor;
  const message = document.getElementById("sensor-ph-message");
  if (!sensor) return;
  const response = await fetch(
    `/api/sensors/${encodeURIComponent(sensor.sensor_id)}/ph-calibration`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reference }),
    });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    setText(message, body.error || "That calibration could not be saved.");
    return;
  }
  setText(message, reference === null ? "Calibration cleared." : "Calibrated.");
  await refreshFleet();
  renderSoilPhCalibration();
}

/* What a scale is, and what it is doing.
 *
 * A scale is reached over HTTP at an address it already serves, so almost
 * nothing in the Bluetooth sensor's configuration applies to it: there is
 * no reporting interval to request, no configuration to deliver on the
 * next report, and no console to switch on -- the page at its address is
 * the console. What it does have is how often it weighs, and how much of
 * that is worth writing down.
 */
/* Whether this sensor has a plant, and the means to give it one.
 *
 * Kept out of the sensor settings render, which is memoised on the device's
 * own fields: the list of plants is not one of them, so the chooser was
 * built once while the plants were still loading and never again. */
function renderSensorAttach() {
  if (!selectedSensor) return;
  /* Either it has a plant or it is looking for one: attaching and
   * detaching are not two things to be offered at once. */
  const attach = document.getElementById("sensor-attach-plant");
  const detach = document.getElementById("plant-sensor-detach");
  const plantId = selectedSensor?.plant_id;
  if (detach !== null) setHidden(detach, plantId == null);
  /* The plant reads as one of the facts about this sensor, beside the
   * battery and the room, rather than as a button to press. What it is
   * watching is a property of it; going there is just following the name. */
  const figure = document.getElementById("sensor-plant-figure");
  if (figure !== null) {
    setHidden(figure, plantId == null);
    if (plantId != null) {
      const plant = householdPlants.find((p) => p.plant_id === plantId);
      const link = document.getElementById("sensor-plant-link");
      // Blank while the plants are still on their way, rather than an id
      // that is not what anybody calls it.
      setText(link, plant ? plant.display_name : "");
      setAttr(link, "href", `/plants/${plantId}`);
    }
  }
  /* Detaching left nothing here but a sensor with no job and no way to
   * give it one. The choice of plant belongs on the sensor's page as much
   * as the choice of sensor belongs on the plant's. */
  if (attach !== null) {
    setHidden(attach, plantId != null);
    if (plantId == null && attach !== document.activeElement) {
      const chooser = document.getElementById("sensor-attach-choice");
      const wanted = JSON.stringify(householdPlants.map((p) => [p.plant_id, p.display_name]));
      if (chooser.dataset.renderKey !== wanted) {
        setData(chooser, "renderKey", wanted);
        chooser.replaceChildren();
        const none = document.createElement("option");
        none.value = "";
        setText(none, "Choose a plant");
        chooser.append(none);
        for (const plant of householdPlants) {
          const option = document.createElement("option");
          option.value = String(plant.plant_id);
          setText(option, plant.display_name);
          chooser.append(option);
        }
      }
    }
  }
  /* A scale keeps its charge in its status rather than in its weight, and
   * reports volts beside the percentage. The gauge draws the same either
   * way; only where the numbers come from differs. */
  const gauge = document.getElementById("sensor-battery");
  if (gauge !== null) {
    const charge = isScale(selectedSensor)
      ? (selectedSensor.scale?.battery_percent == null ? null : {
          battery_percent: selectedSensor.scale.battery_percent,
          battery_voltage_v: selectedSensor.scale.battery_volts,
          battery_charging: null,
        })
      : selectedSensor.latest?.reading;
    setHidden(gauge.closest("div"), charge == null);
    if (charge) renderBattery(gauge, charge);
  }
  /* "4 seconds ago" becoming "5 seconds ago" is the one thing on this page
   * that changes every single second, and it tells nobody anything they are
   * waiting for. It is not worth repainting underneath a form somebody is
   * filling in, so while one is busy this waits. */
  if (!scaleFormBusy()) {
    setText(document.getElementById("sensor-last-heard"),
      selectedSensor.last_seen_at
        ? `${describeAge(selectedSensor.seen_age_seconds)} ago` : "never");
  }
  setText(document.getElementById("sensor-room"), selectedSensor.room || "no room");
}

function renderScalePanel() {
  const panel = document.getElementById("scale-panel");
  if (panel === null) return;
  const sensor = selectedSensor;
  const scale = isScale(sensor) ? sensor.scale : null;
  if (!isScale(sensor)) return;
  setHidden(panel, pageFromLocation() !== "sensor-config");

  /* While somebody is part way through this form, the panel holds still.
   *
   * Skipping only the fields being edited was not enough: the live weight
   * and the "last answered" counter are honest changes, so they are written
   * every second, and on a phone the menu's picker is a sheet that any
   * repaint underneath can dismiss. None of these readings is worth the
   * interruption -- the panel is seconds from being saved, and the numbers
   * resume the moment it is. */
  if (scaleFormBusy()) return;

  setText(document.getElementById("scale-address"), sensor.scale_address || "");
  const grams = scale?.grams;
  setText(document.getElementById("scale-live"),
    grams == null ? "--" : `${showWeight(grams)} g`);
  setText(document.getElementById("scale-interval-now"),
    scale?.interval_seconds == null ? "--"
      : scale.interval_seconds === 0 ? "continuously"
      : describeEvery(scale.interval_seconds));
  setText(document.getElementById("scale-firmware"),
    scale?.firmware ? `${scale.firmware}${scale.build ? ` · ${scale.build}` : ""}` : "--");
  setText(document.getElementById("scale-answered"),
    scale?.read_at ? `${describeAge((Date.now() - Date.parse(scale.read_at)) / 1000)} ago`
                   : "never");

  // A scale that cannot be reached, or that is not weighing, says so here
  // rather than quietly showing its last good number as though it were now.
  const fault = document.getElementById("scale-fault");
  const wrong = scale && scale.status && scale.status !== "ok";
  setHidden(fault, !wrong);
  if (wrong) {
    setText(fault, scale.status === "unreachable"
      ? `The hub cannot reach this scale at ${sensor.scale_address}. ${scale.error || ""}`
      : `The scale reports "${scale.status}", so it is not weighing. `
        + "Its own page at the address above has the detail.");
  }

  /* A half-made decision belongs to the person making it. The panel polls
   * while it is open, and a poll that wrote the device's answer back over a
   * changed menu took the choice away before Save could be reached: pick
   * "1 week", glance at the plant, and it is "30 minutes" again. Focus was
   * guarding this, but a menu stops being the focused element the moment
   * the pointer goes anywhere else, which is most of the time. So a field
   * that has been changed is left alone until it is saved or abandoned. */
  const movedOn = panel.dataset.sensorId !== sensor.sensor_id
    || pageFromLocation() !== "sensor-config";
  if (movedOn) {
    panel.dataset.sensorId = sensor.sensor_id;
    forgetScaleEdits();
  }
  for (const [id, value] of [
    ["scale-interval", scale?.interval_seconds],
    ["scale-keep-every", sensor.scale_keep_every_seconds],
  ]) {
    const field = document.getElementById(id);
    if (field === null || value == null) continue;
    if (field === document.activeElement || field.dataset.edited === "true") continue;
    selectNearest(field, value);
  }
}

/* Somebody has this form open in front of them: a control in it holds the
 * focus, which on a phone means its picker is on screen.
 *
 * Deliberately not "has been edited", which lasts until Save: a choice made
 * and then left sitting would have frozen the live weight for as long as the
 * page stayed open. The choice itself is safe either way -- each field
 * carries its own edited flag and the poll refuses to write over it -- so
 * this only has to cover the moment the picker is up. */
function scaleFormBusy() {
  const form = document.getElementById("scale-form");
  return form !== null && form.contains(document.activeElement);
}

/* Saved, or moved away from: either way the device's answer is the truth
 * again and the next poll may show it. */
function forgetScaleEdits() {
  for (const id of ["scale-interval", "scale-keep-every"]) {
    const field = document.getElementById(id);
    if (field !== null) delete field.dataset.edited;
  }
}

/* The listed choice closest to what the device actually says, so a value
 * set elsewhere does not read as one of ours. */
function selectNearest(field, value) {
  let best = null;
  for (const option of field.options) {
    const difference = Math.abs(Number(option.value) - Number(value));
    if (best === null || difference < best.difference) {
      best = { value: option.value, difference };
    }
  }
  if (best !== null && field.value !== best.value) field.value = best.value;
}

function describeEvery(seconds) {
  if (seconds < 60) return `${seconds} seconds`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} minutes`;
  return `${Math.round(seconds / 3600)} hours`;
}

async function saveScale() {
  const sensor = selectedSensor;
  const message = document.getElementById("scale-message");
  if (!isScale(sensor)) return;
  const seconds = Number(document.getElementById("scale-interval").value);
  setText(message, "Asking the scale...");
  if (seconds !== sensor.scale?.interval_seconds) {
    const response = await fetch(
      `/api/sensors/${encodeURIComponent(sensor.sensor_id)}/scale/interval`,
      { method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ seconds }) });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      setText(message, body.error || "The scale did not take that setting.");
      return;
    }
  }
  const kept = await fetch(`/api/sensors/${encodeURIComponent(sensor.sensor_id)}/scale`, {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      keep_every_seconds: Number(document.getElementById("scale-keep-every").value),
    }) });
  if (!kept.ok) {
    const body = await kept.json().catch(() => ({}));
    setText(message, body.error || "That could not be saved.");
    return;
  }
  setText(message, "Saved.");
  forgetScaleEdits();
  await refreshFleet();
  renderScalePanel();
}

/* A sensor's charge belongs to the sensor.
 *
 * It used to sit in the topbar of the page that stood in for the plant,
 * and went with it. A battery is a fact about the device, so it is shown
 * where the device is. */
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
  /* The plant is somewhere to go from here, not where here came from, so
   * it sits with the other things done to this sensor rather than in the
   * back button. A sensor with no plant is offered nothing. */
  const kind = SENSOR_KINDS[sensorKind(selectedSensor)];
  setText(document.getElementById("sensor-kind-label"), kind.label);
  setText(document.getElementById("sensor-kind-note"), kind.note);
  setHidden(document.getElementById("sensor-identity-panel"),
            pageFromLocation() !== "sensor-config");
  // A scale has no configuration the hub delivers, so it is not kept
  // waiting for one it will never be sent.
  setHidden(document.getElementById("settings-config-revision"), isScale(selectedSensor));
  if (!isScale(selectedSensor)) renderConfigRevision(selectedSensor);
  if (!preserveDraft) {
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
  const [fleetResponse, unclaimedResponse, healthResponse] = await Promise.all([
    fetch("/api/sensors?status=enrolled"),
    fetch("/api/sensors?status=unclaimed"),
    fetch("/api/health")
  ]);
  if (!fleetResponse.ok || !unclaimedResponse.ok || !healthResponse.ok) {
    throw new Error("fleet request failed");
  }
  const [fleetPayload, unclaimedPayload, healthPayload] = await Promise.all([
    fleetResponse.json(),
    unclaimedResponse.json(),
    healthResponse.json()
  ]);
  if (requestSequence !== fleetRequestSequence) return;
  fleetSensors = fleetPayload.items;
  unclaimedSensors = unclaimedPayload.items;
  renderHubHealth(healthPayload);
  /* Which sensor this page is about.
   *
   * A plant's configuration page carries its sensor's settings, and names
   * the plant rather than the sensor in its address. Taking the sensor
   * from the address alone set this to null on every poll, so the sensor
   * panel rendered nothing: no identity on its switches, and a console
   * toggle that called setSensorConsole with no sensor to act on. */
  selectedSensorId = sensorIdFromLocation() ?? plantSensor()?.sensor_id ?? null;
  selectedSensor = fleetSensors.find((sensor) => sensor.sensor_id === selectedSensorId) || null;
  renderHousehold();
  renderSensorSettings();
  renderSensorAttach();
  renderScalePanel();
  renderSoilPhCalibration();
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
  climateHistory = null;
  moistureTrendItems = [];
  moistureTrendSensorId = null;
  wateringCalendarRequestKey = null;
  renderedCareEventIds = new Set();
  careLogInitialized = false;
  renderPage();
  await refresh();
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

// Past this, a reading is worth labelling with its age rather than
// being shown as though it had just arrived.
const MOISTURE_STALE_AFTER_S = 45 * 60;

/* The soil moisture to show, and how old it is.
 *
 * The plant's own latest reading first; failing that, the newest
 * moisture any probe in its pot has reported, however long ago. A
 * probe that sends temperature and moisture in separate frames leaves
 * gaps in the latest row, and an empty field is worse than an old
 * number: soil is the slowest thing here, so yesterday's moisture
 * still tells you more than a dash. It is labelled with its age so it
 * cannot pass itself off as current.
 */
function plantMoisture() {
  const said = latestReading?.moisture_percent;
  if (said != null && Number.isFinite(Number(said))) {
    const at = Date.parse(latestReadingAt || latestReading?.observed_at || "");
    return { value: Number(said),
             ageSeconds: Number.isFinite(at) ? (Date.now() - at) / 1000 : null };
  }
  let best = null;
  for (const source of selectedPlant?.soil_sources || []) {
    if (source.moisture_percent == null) continue;
    const at = Date.parse(source.observed_at || "");
    if (!best || (Number.isFinite(at) && at > best.at)) {
      best = { value: Number(source.moisture_percent), at };
    }
  }
  if (!best) return { value: NaN, ageSeconds: null };
  return { value: best.value,
           ageSeconds: Number.isFinite(best.at) ? (Date.now() - best.at) / 1000 : null };
}

function renderMoistureVessel(profile) {
  const reading = plantMoisture();
  const value = reading.value;
  if (!Number.isFinite(value)) {
    // Say nothing rather than draw an empty pot: the last verdict left
    // on screen is as wrong as a made-up one.
    setText(document.getElementById("moisture-phase"), "Waiting for a reading");
    setText(document.getElementById("moisture-strategy"), "");
    setAttr(document.getElementById("moisture-vessel"), "aria-label",
            "Soil moisture not reported yet");
    return;
  }
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
  // The reading itself, riding the surface it describes. The big figure
  // above the pot is the one to read; this one says which line is the water.
  setText(document.getElementById("moisture-waterline"), `${Math.round(value)}%`);
  setAttr(vessel, "aria-label", `Soil moisture ${value.toFixed(1)} percent. ${wateringPhase(watering, value)}.`);
  setData(vessel, "phase", wateringPhase(watering, value).toLowerCase().replaceAll(" ", "-"));
  setText(document.getElementById("moisture-cycle-label"), `Comfortable ${cycleLow}–${cycleHigh}%`);
  setText(document.getElementById("moisture-target-label"), `After watering ${targetLow}–${targetHigh}%`);
  setText(document.getElementById("moisture-refill-label"), `Water below ${watering.refill_below}%`);
  setText(document.getElementById("moisture-phase"), wateringPhase(watering, value));
  setText(document.getElementById("moisture-strategy"),
          reading.ageSeconds != null && reading.ageSeconds > MOISTURE_STALE_AFTER_S
            ? `${watering.label} · last measured ${describeSeconds(reading.ageSeconds)} ago`
            : watering.label);
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

  const valueMarker = card.querySelector(".value-marker");
  // A value the sensor did not send is not a value of zero, which is where a
  // missing number would otherwise put the marker.
  setHidden(valueMarker, value == null);
  if (value != null) {
    setStyle(valueMarker, "left", `${Math.max(0, Math.min(100, percentage(value)))}%`);
  }
  renderRangeStatus(card.querySelector(".range-status"), metricName, metric, value);
}

// Where a value sits against a profile's ideal range, in words.
function renderRangeStatus(status, metricName, metric, value) {
  const [idealLow, idealHigh] = metric.ideal;
  const inRange = value != null && value >= idealLow && value <= idealHigh;
  setClass(status, "in-range", inRange);
  setClass(status, "out-range", value != null && !inRange);
  if (value == null) {
    setText(status, "Not in the latest report");
    return;
  }
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
  setText(document.getElementById("chemistry-profile"), `${profile.name} starter targets`);
  renderMoistureVessel(profile);
  renderMoistureTrend(profile);
  renderPlantAssessment(profile);
  renderDrainageAssessment(profile);

  for (const card of document.querySelectorAll(".root-zone-gauge")) {
    const metricName = card.dataset.metric;
    renderGauge(card, metricName, profile.root_zone[metricName], latestReading[metricName]);
  }
  // Air conditions are judged under their charts, which draw the same range.
  for (const [metricName, id] of [
    ["air_temperature_c", "temperature-history-range"], ["air_humidity_percent", "humidity-history-range"]
  ]) {
    renderRangeStatus(document.getElementById(id), metricName, profile.climate[metricName], latestReading[metricName]);
  }
  if (climateHistory) renderClimateHistory(...climateHistory);
  /* The probe's pH and N/P/K are shown as it reports them, with no target
   * band: they are labelled in the page as unverified and trend-only.
   *
   * They no longer arrive with every reading. Holding the probe powered
   * long enough for pH costs four minutes, so it is paid a couple of times
   * a day and after a watering, and the readings in between carry none of
   * these. Taking them from the newest reading left the whole panel blank,
   * so they come from the last reading that actually had them, and the
   * panel says when that was. */
  const chemistry = (plantSensor() || selectedSensor)?.soil_chemistry;
  for (const [metricName, id] of Object.entries(chemistryValueIds)) {
    const live = latestReading[metricName];
    const value = live != null ? live
      : metricName === "soil_ph" ? chemistry?.ph
      : chemistry?.[metricName];
    setText(document.getElementById(id), value == null ? "--" : formatValue(value, metricName));
  }
  const measured = document.getElementById("chemistry-measured");
  if (measured !== null) {
    const stale = latestReading.soil_ph == null && chemistry?.at;
    setHidden(measured, !stale);
    if (stale) {
      setText(measured, `pH and nutrients were last measured ${formatAge(chemistry.age_seconds)}`
        + `${chemistry.calibrated ? `, and are corrected by ${chemistry.offset >= 0 ? "+" : ""}${chemistry.offset}` : ""}`
        + ". They need four minutes with the probe powered, so they are taken a "
        + "couple of times a day and after a watering rather than every reading.");
    }
  }
  renderPoreWaterEc(profile);
  renderBulkEcChart();
  renderPoreEcChart(profile);
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
  latestReadingAt = reading.observed_at || payload.received_at;
  const takenAt = reading.observed_at
    ? new Date(reading.observed_at).toLocaleTimeString()
    : `time unknown, received ${new Date(payload.received_at).toLocaleTimeString()}`;
  renderProfileRanges();
}

function svgElement(name, attributes, text = null) {
  const element = document.createElementNS("http://www.w3.org/2000/svg", name);
  for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, value);
  if (text != null) element.textContent = text;
  return element;
}

/* Readings from one sensor turned into a drawable line.
 *
 * The colour is the sensor's position in the list rather than
 * anything about the sensor, so "white bird" is the same colour in the
 * air chart and the soil chart beneath it and the legend is read once.
 */
const SERIES_COLOURS = 6;

/* A sensor's colour, decided by the sensor and nothing else.
 *
 * Taken from its position in the list to begin with, which meant the
 * colours shuffled on every refresh: the list is ordered by which
 * sensor reported last, so a thermometer changed colour whenever the
 * other one spoke. A line that changes colour while you watch it
 * cannot be followed, and the legend has to be re-read every time.
 *
 * Hashed from the sensor's id, so it survives refreshes, pages and
 * restarts. Where two sensors would collide the one whose id sorts
 * first keeps the colour and the other takes the next free slot, so
 * the answer stays the same for the same set of sensors.
 */
function seriesColours(sensorIds) {
  const chosen = new Map();
  const taken = new Set();
  for (const sensorId of [...sensorIds].sort()) {
    let hash = 0;
    for (const character of sensorId) hash = (hash * 31 + character.charCodeAt(0)) >>> 0;
    let colour = hash % SERIES_COLOURS;
    while (taken.has(colour) && taken.size < SERIES_COLOURS) {
      colour = (colour + 1) % SERIES_COLOURS;
    }
    taken.add(colour);
    chosen.set(sensorId, colour);
  }
  return chosen;
}

function historySeries(items, metric, sources, decimals = 1) {
  const colours = seriesColours(sources.map((source) => source.sensor_id));
  // Named order, not reporting order: the legend should not rearrange
  // itself because a sensor spoke.
  sources = [...sources].sort((a, b) => a.sensor_name.localeCompare(b.sensor_name));
  return sources.map((source) => {
    const points = (items.get(source.sensor_id) || [])
      .map((item) => ({
        timestamp: Date.parse(item.reading.observed_at || item.received_at),
        value: item.reading[metric] == null ? null : Number(item.reading[metric]),
      }))
      .filter((point) => Number.isFinite(point.timestamp) && Number.isFinite(point.value))
      .sort((a, b) => a.timestamp - b.timestamp);
    return {
      sensorId: source.sensor_id,
      name: source.sensor_name,
      colour: colours.get(source.sensor_id) ?? 0,
      points,
      latest: points.length ? points[points.length - 1].value.toFixed(decimals) : null,
    };
  }).filter((entry) => entry.points.length);
}

/* Which sensors a reader has switched off, by sensor id.
 *
 * Per viewer rather than per hub: hiding a line is a way of looking at
 * a chart, not a decision about the house, and it should not follow
 * anybody to another screen or hide a line for somebody else.
 */
const hiddenSeries = new Set(readHiddenSeries());

function readHiddenSeries() {
  try {
    return JSON.parse(window.localStorage.getItem("opp.chart.hidden") || "[]");
  } catch (_error) {
    return [];   // blocked site data: every line shown is the safe answer
  }
}

function rememberHiddenSeries() {
  try {
    window.localStorage.setItem("opp.chart.hidden", JSON.stringify([...hiddenSeries]));
  } catch (_error) {
    /* the toggle still works for this visit */
  }
}

/* A legend for a set of charts, with a line somebody can switch off.
 *
 * One legend for the section rather than one per chart: the same
 * sensors draw the temperature and the humidity, and hiding a sensor
 * means hiding it from both -- two legends saying the same thing would
 * be two places to disagree.
 */
function renderChartLegend(legendId, series) {
  const legend = document.getElementById(legendId);
  if (!legend) return;
  // A legend of one says nothing worth the space -- unless that one
  // is switched off, in which case it is the only way back. Hiding is
  // remembered across pages, so a chart can arrive with its only
  // sensor already hidden, and without this there would be no line
  // and no chip to bring it back.
  const anyHidden = series.some((entry) => hiddenSeries.has(entry.sensorId));
  setHidden(legend, series.length < 2 && !anyHidden);
  if (!renderKeyChanged(legend, [series.map((s) => [s.sensorId, s.name, s.latest]),
                                 [...hiddenSeries].sort()])) return;
  legend.replaceChildren();
  for (const entry of series) {
    const off = hiddenSeries.has(entry.sensorId);
    const button = document.createElement("button");
    button.type = "button";
    button.className = `chart-legend-entry series-${entry.colour}`;
    button.setAttribute("aria-pressed", String(!off));
    button.dataset.sensorId = entry.sensorId;
    const swatch = document.createElement("span");
    swatch.className = "chart-legend-swatch";
    swatch.setAttribute("aria-hidden", "true");
    const name = document.createElement("span");
    setText(name, entry.name);
    button.append(swatch, name);
    if (entry.latest != null) {
      const value = document.createElement("b");
      setText(value, entry.latest);
      button.append(value);
    }
    legend.append(button);
  }
}

document.addEventListener("click", (event) => {
  const entry = event.target.closest(".chart-legend-entry");
  if (!entry) return;
  const sensorId = entry.dataset.sensorId;
  if (hiddenSeries.has(sensorId)) hiddenSeries.delete(sensorId);
  else hiddenSeries.add(sensorId);
  rememberHiddenSeries();
  renderPage();
});

/* One chart, one measurement, one line per sensor measuring it.
 *
 * A room with two thermometers has two honest answers and a pot can
 * have two probes in it; drawing only one of them makes the other
 * sensor look broken, and averaging them produces a reading that is
 * true nowhere. The ideal band stays the plant's, because what counts
 * as comfortable is the plant's business and not the thermometer's.
 */
function renderHistoryChart(chartId, emptyId, series, unit, startTime, endTime, ideal) {
  const chart = document.getElementById(chartId);
  const empty = document.getElementById(emptyId);
  if (!chart) return;
  const shown = series.filter((entry) => !hiddenSeries.has(entry.sensorId)
                                      && entry.points.length);
  const everything = shown.flatMap((entry) => entry.points);
  if (!renderKeyChanged(chart, [shown.map((s) => [s.sensorId, s.points.length,
                                                  s.points[s.points.length - 1].value]),
                                unit, startTime, endTime, selectedHistoryRange, ideal])) return;
  chart.replaceChildren();
  setHidden(chart, everything.length === 0);
  if (empty) {
    // What this chart says when it truly has nothing, kept before
    // anything overwrites it so it can be put back.
    if (!empty.dataset.saying) setData(empty, "saying", empty.textContent);
    setHidden(empty, everything.length !== 0);
    // Nothing recorded and everything hidden are different facts, and
    // only one of them is the reader's own doing. This said "no
    // readings in this range" about readings that were there all
    // along, because a name switched off on one page is switched off
    // on every page.
    if (series.length && !shown.length) {
      setText(empty, series.length > 1
        ? "Every sensor here is hidden. Tap a name above to show one again."
        : "This sensor is hidden. Tap its name above to show it again.");
    } else if (empty.dataset.saying) {
      setText(empty, empty.dataset.saying);
    }
  }
  if (!everything.length) return;

  const width = 720;
  const height = 220;
  const plot = { left: 52, right: 16, top: 16, bottom: 34 };
  const plotWidth = width - plot.left - plot.right;
  const plotHeight = height - plot.top - plot.bottom;
  const values = everything.map((point) => point.value);
  const observedMinimum = Math.min(...values);
  const observedMaximum = Math.max(...values);
  // The plant's ideal range is always in view, so the lines are read
  // against it; the scale covers every sensor shown, so two lines can
  // be compared rather than each being drawn to its own flattering axis.
  let minimum = ideal ? Math.min(observedMinimum, ideal[0]) : observedMinimum;
  let maximum = ideal ? Math.max(observedMaximum, ideal[1]) : observedMaximum;
  const padding = Math.max((maximum - minimum) * 0.12, unit.pad);
  minimum -= padding;
  maximum += padding;
  // A percentage cannot be 104 %, so padding is not allowed to invent it.
  if (unit.lowest != null) minimum = Math.max(unit.lowest, minimum);
  if (unit.highest != null) maximum = Math.min(unit.highest, maximum);
  const x = (timestamp) => plot.left + ((timestamp - startTime) / (endTime - startTime)) * plotWidth;
  const y = (value) => plot.top + ((maximum - value) / (maximum - minimum)) * plotHeight;

  if (ideal) {
    const [idealLow, idealHigh] = ideal;
    chart.append(
      svgElement("rect", {
        class: "ideal-band", x: plot.left, width: plotWidth,
        y: y(idealHigh), height: y(idealLow) - y(idealHigh)
      }),
      svgElement("line", { class: "ideal-edge", x1: plot.left, x2: width - plot.right, y1: y(idealHigh), y2: y(idealHigh) }),
      svgElement("line", { class: "ideal-edge", x1: plot.left, x2: width - plot.right, y1: y(idealLow), y2: y(idealLow) }),
      svgElement("text", { class: "ideal-band-label", x: plot.left + 6, y: y(idealHigh) + 14 },
                 `Ideal ${idealLow}\u2013${idealHigh}`)
    );
  }
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
      class: "axis-label", x: x(timestamp), y: height - 8,
      "text-anchor": index === 0 ? "start" : index === 2 ? "end" : "middle"
    }, new Date(timestamp).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })));
  });

  const marks = [];
  for (const entry of shown) {
    drawHistorySeries(chart, entry, x, y);
    for (const point of entry.points) {
      marks.push({
        cx: x(point.timestamp), cy: y(point.value),
        text: `${point.value.toFixed(1)} ${unit.symbol}`,
        when: shown.length > 1
          ? `${entry.name} \u00b7 ${new Date(point.timestamp).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`
          : new Date(point.timestamp).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }),
      });
    }
  }
  attachNearestReadout(chart, { width, height }, marks);

  chart.setAttribute("aria-label",
    `${historyRanges[selectedHistoryRange].label} ${unit.spoken} history. `
    + shown.map((entry) => `${entry.name}: ${entry.points.length} readings, latest `
        + `${entry.points[entry.points.length - 1].value.toFixed(1)}`).join("; ")
    + `. Range ${observedMinimum.toFixed(1)} to ${observedMaximum.toFixed(1)} ${unit.spoken}.`);
}

/* One sensor's line, broken wherever that sensor went quiet.
 *
 * A single polyline joins the last reading before an outage straight to
 * the first one after it, drawing a confident diagonal across a day
 * when the sensor was flat on its back. The gap is the thing worth
 * seeing, so the line stops at the edge of what was measured.
 *
 * What counts as a gap is this sensor's own rhythm, judged near the top
 * of its spacings rather than the middle: a device that reports on
 * change sends several frames in a minute and then nothing for half an
 * hour, and the median of that claims it reports every minute -- which
 * turned a thermometer's humidity into a scatter of dots while its
 * temperature, spread more evenly, stayed a line.
 */
function drawHistorySeries(chart, entry, x, y) {
  const points = entry.points;
  const spacings = points.slice(1)
    .map((point, index) => point.timestamp - points[index].timestamp)
    .sort((a, b) => a - b);
  const typical = spacings.length
    ? spacings[Math.min(spacings.length - 1, Math.floor(spacings.length * 0.9))] : 0;
  const gapAfter = Math.max(typical * 4, 15 * 60 * 1000);
  const runs = [];
  let run = [];
  for (const [index, point] of points.entries()) {
    if (index && point.timestamp - points[index - 1].timestamp > gapAfter) {
      runs.push(run);
      run = [];
    }
    run.push(point);
  }
  runs.push(run);
  for (const stretch of runs) {
    if (stretch.length < 2) continue;
    chart.append(svgElement("polyline", {
      class: `history-line series-${entry.colour}`,
      points: stretch.map((point) => (
        `${x(point.timestamp).toFixed(2)},${y(point.value).toFixed(2)}`
      )).join(" "),
    }));
  }
  for (const [index, point] of points.entries()) {
    const last = index === points.length - 1;
    chart.append(svgElement("circle", {
      class: `${last ? "history-point latest-point" : "history-point"} series-${entry.colour}`,
      cx: x(point.timestamp), cy: y(point.value), r: last ? 3 : 1.6
    }));
  }
}

// The air history last fetched, kept so a change of plant profile redraws its
// ideal bands without waiting for the next report.
let climateHistory = null;

/* The soil, drawn exactly as the air is.
 *
 * A pot can hold more than one probe and they will disagree -- a
 * capacitive probe reads on its own uncalibrated scale, so 28% on one
 * and 80% on another can both be honest about the same soil. Showing
 * both is the only way that is visible; averaging them would invent a
 * number true of neither.
 */
function renderSoilHistory(items, startTime, endTime, profile) {
  const section = document.querySelector('[data-section="soil"]');
  if (!section) return;
  const sources = selectedPlant?.soil_sources || [];
  const moisture = historySeries(items, "moisture_percent", sources, 0);
  const temperature = historySeries(items, "soil_temperature_c", sources, 1);
  // Nothing measured means no section, rather than a pair of empty
  // frames on the page of a plant with no probe in it.
  setHidden(section, !moisture.length && !temperature.length);
  if (!moisture.length && !temperature.length) return;

  const latest = (series) => series.length
    ? series[0].points[series[0].points.length - 1].value : null;
  setText(document.getElementById("soil-history-moisture"),
          moisture.length ? Math.round(latest(moisture)) : "--");
  setText(document.getElementById("soil-history-temperature"),
          temperature.length ? latest(temperature).toFixed(1) : "--");
  renderChartLegend("soil-legend", moisture.length ? moisture : temperature);
  renderHistoryChart("soil-moisture-chart", "soil-moisture-empty", moisture,
                     { ...PERCENT, spoken: "percent moisture" }, startTime, endTime,
                     profile?.root_zone?.moisture_percent?.ideal);
  renderHistoryChart("soil-temp-chart", "soil-temp-empty", temperature,
                     CELSIUS, startTime, endTime,
                     profile?.root_zone?.soil_temperature_c?.ideal);
  setText(document.getElementById("soil-history-status"),
          sources.length > 1
            ? `${sources.length} probes in this pot. Capacitive probes each read on `
              + "their own scale, so they will not agree."
            : "");
}

function renderClimateHistory(items, startTime, endTime) {
  climateHistory = [items, startTime, endTime];
  const profile = profiles?.profiles?.[climateProfileId || profiles?.default_profile];
  const everyItem = [...items.values()].flat();
  const sampleTimes = everyItem
    .map(item => Date.parse(item.reading.observed_at || item.received_at))
    .filter(Number.isFinite);
  let chartStartTime = startTime;
  let chartEndTime = endTime;
  /* Live zooms in on the readings it has, but only at the left.
   *
   * Fitting both ends meant the axis ended at the newest reading, so a
   * sensor that had said nothing for a day still drew a full width
   * line looking perfectly alive -- the chart literally could not
   * render silence, because its right hand edge was defined as the
   * last thing it heard. Holding the right edge at now is what makes a
   * gap appear and grow. */
  if (historyRanges[selectedHistoryRange].fitSamples && sampleTimes.length > 1) {
    const firstSampleTime = Math.min(...sampleTimes);
    if (firstSampleTime < chartEndTime) chartStartTime = firstSampleTime;
  }
  const temperatures = everyItem
    .map(item => item.reading.air_temperature_c)
    .filter(value => value != null && Number.isFinite(Number(value)))
    .map(Number);
  const humidities = everyItem
    .map(item => item.reading.air_humidity_percent)
    .filter(value => value != null && Number.isFinite(Number(value)))
    .map(Number);
  setText(document.getElementById("history-temperature"), temperatures.length
    ? temperatures[temperatures.length - 1].toFixed(1) : "--");
  setText(document.getElementById("history-humidity"), humidities.length
    ? humidities[humidities.length - 1].toFixed(1) : "--");
  renderSoilHistory(items, chartStartTime, chartEndTime, profile);
  const sources = selectedPlant?.air_sources || [];
  const temperatureSeries = historySeries(items, "air_temperature_c", sources, 1);
  const humiditySeries = historySeries(items, "air_humidity_percent", sources, 0);
  // One legend for the section: the same sensors draw both charts, and
  // switching one off means switching it off in both.
  renderChartLegend("climate-legend", temperatureSeries.length
    ? temperatureSeries : humiditySeries);
  renderHistoryChart(
    "temperature-history-chart", "temperature-history-empty", temperatureSeries,
    CELSIUS, chartStartTime, chartEndTime,
    profile?.climate.air_temperature_c.ideal
  );
  renderHistoryChart(
    "humidity-history-chart", "humidity-history-empty", humiditySeries,
    { ...PERCENT, spoken: "percent relative humidity" }, chartStartTime, chartEndTime,
    profile?.climate.air_humidity_percent.ideal
  );
  const status = document.getElementById("climate-history-status");
  setClass(status, "error", false);
  const sampleStatus = items.length === 1
    ? "Collecting another reading to draw the line"
    : `${items.length} stored readings`;
  /* Say when the line stops short, rather than leaving the blank to be
   * read as a chart that has not loaded. */
  const newest = items.length
    ? Date.parse(items[items.length - 1].reading?.observed_at
                 || items[items.length - 1].received_at)
    : NaN;
  const quietFor = Number.isFinite(newest) ? (Date.now() - newest) / 1000 : null;
  const silence = quietFor != null && quietFor > 3 * 60 * 60
    ? ` · nothing since ${describeSeconds(quietFor)} ago`
    : " · Updates live";
  setText(status, `${historyRanges[selectedHistoryRange].label} · ${sampleStatus}${silence}`);
  setClass(status, "bad", quietFor != null && quietFor > 3 * 60 * 60);
}

/* Where the air readings come from for whichever page is open.
 *
 * Its own probe, on a plant that has one. Otherwise the room's: air belongs
 * to the room, and a plant with nothing in its soil is still in air somebody
 * is measuring. The ideal bands come from the plant being looked at either
 * way, because what counts as comfortable is the plant's business and not
 * the thermometer's. */
function plantHistorySources() {
  // Every sensor either section will draw, fetched once. A Bluetooth
  // probe measures the air and the soil, so asking per section would
  // request the same readings twice.
  const air = selectedPlant?.air_sources || [];
  const soil = selectedPlant?.soil_sources || [];
  const seen = new Map();
  for (const source of [...air, ...soil]) seen.set(source.sensor_id, source);
  return [...seen.values()];
}

function airSources() {
  // Every sensor measuring the air this plant is in, which is every
  // air sensor in its room -- its own probe among them. Two
  // thermometers in a room are two honest answers about different
  // corners of it, and the reader is better served by both than by
  // whichever happened to report last.
  const sources = plantHistorySources();
  if (!sources.length) return null;
  const newest = sources
    .map((source) => Date.parse(source.observed_at))
    .filter(Number.isFinite);
  return {
    sources,
    at: new Date(Math.max(...newest)).toISOString(),
    stamp: sources.map((source) => source.observed_at).join(","),
    profileId: selectedSensor?.profile_id || selectedPlant?.profile_id,
  };
}

async function refreshClimateHistory() {
  const source = airSources();
  if (source === null) return;
  const range = historyRanges[selectedHistoryRange];
  const newest = Date.parse(source.at);
  if (!Number.isFinite(newest)) return;
  /* The window ends now, not at the last reading.
   *
   * Anchoring it to the newest reading meant the chart could never show
   * a sensor going quiet: when this probe's battery went flat the whole
   * window slid back in time with it and drew a full, healthy looking
   * day of yesterday's air. The right hand edge was the last reading by
   * construction, so the one thing worth seeing -- that nothing has
   * arrived for a day -- was the one thing it could not draw.
   *
   * Ending at now costs nothing when a sensor is reporting and leaves a
   * widening blank when it stops, which is what somebody glancing at it
   * needs to notice. */
  const endTime = Math.max(newest, Date.now());
  const startTime = endTime - range.milliseconds;
  /* Rounded to the minute so a key built from "now" does not refetch on
   * every poll; the newest reading still forces one as soon as it moves. */
  const windowKey = Math.floor(endTime / 60000);
  const requestKey = `${source.sources.map((s) => s.sensor_id).join(",")}`
    + `:${selectedHistoryRange}:${source.at}:${source.stamp}:${windowKey}`;
  if (historyRequestKey === requestKey) return;
  historyRequestKey = requestKey;
  climateProfileId = source.profileId;
  setText(document.getElementById("climate-history-eyebrow"), "AIR");
  try {
    const histories = new Map();
    for (const entry of source.sources) {
      const query = new URLSearchParams({
        sensor_id: entry.sensor_id,
        start: new Date(startTime).toISOString(),
        end: new Date(endTime).toISOString()
      });
      const response = await fetch(`/api/readings/history?${query}`);
      if (!response.ok) throw new Error("history request failed");
      histories.set(entry.sensor_id, (await response.json()).items || []);
    }
    if (historyRequestKey !== requestKey) return;
    renderClimateHistory(histories, startTime, endTime);
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

/* What the measured conductivity has been doing.
 *
 * The number on its own says little: this is the reading that leaps when a
 * pot is watered and sags as the water finds its way down, so the shape of
 * the last couple of days is the part worth seeing -- the pour, the drain,
 * and where it settles. Two days rather than thirty, because that is the
 * span a watering plays out over.
 *
 * It is the bulk reading, soil and water and air together, so most of the
 * rise after a pour is water arriving rather than salt. The pore-water
 * estimate above is the one with moisture divided out.
 */
const bulkEcChartMs = 2 * 24 * 60 * 60 * 1000;
// How much of the pot before the pour to keep, so the rise has something to
// rise from. The same 15 points the hub calls a watering.
const bulkEcLeadInMs = 30 * 60 * 1000;
const WATERING_RISE_PERCENT = 15;

function bulkEcPoints() {
  const points = moistureTrendItems.map((item) => ({
    timestamp: Date.parse(item.reading.observed_at || item.received_at),
    value: item.reading.conductivity_us_cm,
    moisture: item.reading.moisture_percent,
  }));
  if (latestReading && latestReadingAt) {
    points.push({
      timestamp: Date.parse(latestReadingAt),
      value: latestReading.conductivity_us_cm,
      moisture: latestReading.moisture_percent,
    });
  }
  const valid = points
    .filter((point) => point.value != null && Number.isFinite(point.timestamp)
      && Number.isFinite(Number(point.value))
      && point.timestamp >= Date.now() - bulkEcChartMs)
    .map((point) => ({ timestamp: point.timestamp, value: Number(point.value),
                       moisture: Number(point.moisture) }))
    .sort((a, b) => a.timestamp - b.timestamp);
  const ordered = valid.filter((point, index) =>
    index === 0 || point.timestamp > valid[index - 1].timestamp);

  /* Start a little before the last watering where there was one.
   *
   * Two days of a pot that was dry for most of them is a flat line at zero
   * with the whole story squeezed into the last few pixels. The shape worth
   * seeing runs from just before the pour to wherever it has settled, so
   * that is the window when a pour can be found in it. */
  let wateredAt = null;
  for (let index = 1; index < ordered.length; index += 1) {
    const rise = ordered[index].moisture - ordered[index - 1].moisture;
    if (Number.isFinite(rise) && rise >= WATERING_RISE_PERCENT) {
      wateredAt = ordered[index - 1].timestamp;
    }
  }
  if (wateredAt === null) return ordered;
  const from = wateredAt - bulkEcLeadInMs;
  const since = ordered.filter((point) => point.timestamp >= from);
  // Not worth re-anchoring on a pour so recent there is nothing after it.
  return since.length >= 4 ? since : ordered;
}

function renderBulkEcChart() {
  const chart = document.getElementById("bulk-ec-chart");
  const empty = document.getElementById("bulk-ec-chart-empty");
  const note = document.getElementById("bulk-ec-chart-note");
  if (chart === null) return;
  const points = bulkEcPoints();
  setHidden(chart, points.length < 2);
  setHidden(empty, points.length >= 2);
  if (points.length < 2) {
    setHidden(note, true);
    return;
  }
  const width = Math.max(260, Math.round(chart.getBoundingClientRect().width) || 640);
  if (!renderKeyChanged(chart, [moistureTrendVersion, points.length, points.at(-1), width])) return;
  chart.replaceChildren();

  const height = 150;
  setAttr(chart, "viewBox", `0 0 ${width} ${height}`);
  const plot = { left: 46, right: 10, top: 10, bottom: 20 };
  const plotWidth = width - plot.left - plot.right;
  const plotHeight = height - plot.top - plot.bottom;
  const startTime = points[0].timestamp;
  const endTime = points[points.length - 1].timestamp;
  const values = points.map((point) => point.value);
  const top = Math.max(...values) * 1.1 || 1;
  const x = (time) =>
    plot.left + ((time - startTime) / Math.max(1, endTime - startTime)) * plotWidth;
  const y = (value) => plot.top + (1 - value / top) * plotHeight;

  for (const value of [0, top / 2, top]) {
    chart.append(
      svgElement("line", { class: "grid-line", x1: plot.left, x2: width - plot.right,
                           y1: y(value), y2: y(value) }),
      svgElement("text", { class: "axis-label", x: plot.left - 6, y: y(value) + 4,
                           "text-anchor": "end" }, String(Math.round(value)))
    );
  }
  chart.append(svgElement("polyline", {
    class: "history-line",
    points: points.map((point) => `${x(point.timestamp)},${y(point.value)}`).join(" "),
  }));
  chart.append(svgElement("circle", {
    class: "history-point latest-point", r: 3.5,
    cx: x(points[points.length - 1].timestamp),
    cy: y(points[points.length - 1].value),
  }));

  const day = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric",
                                                   hour: "numeric" });
  chart.append(
    svgElement("text", { class: "axis-label", x: plot.left, y: height - 5,
                         "text-anchor": "start" }, day.format(new Date(startTime))),
    svgElement("text", { class: "axis-label", x: width - plot.right, y: height - 5,
                         "text-anchor": "end" }, day.format(new Date(endTime)))
  );

  // Said beside the line, because the shape invites the wrong reading.
  const lowest = Math.min(...values);
  const highest = Math.max(...values);
  setHidden(note, false);
  const hours = (endTime - startTime) / 3600000;
  setText(note, `${Math.round(lowest)} to ${Math.round(highest)} µS/cm over `
    + `${hours < 1 ? `${Math.round(hours * 60)} minutes` : `${hours.toFixed(hours < 10 ? 1 : 0)} hours`}. `
    + "Most of a rise after watering is water arriving between the needles rather "
    + "than salt; pore-water EC above has moisture divided out.");
}

/* Pore-water conductivity over the month: is salt building up?
 *
 * The slow companion to the measured-EC line. Watering moves that one by
 * tenfold and means little; this one moves when what the roots sit in
 * actually changes, which is the thing that eventually asks for a flush.
 *
 * Drawn with gaps. Below about a fifth moisture the model has nothing to
 * say and the hub sends no estimate, which is most of the time between
 * waterings -- joining across those would draw a line through soil nobody
 * measured.
 */
function renderPoreEcChart(profile) {
  const chart = document.getElementById("pore-ec-chart");
  const empty = document.getElementById("pore-ec-chart-empty");
  const note = document.getElementById("pore-ec-chart-note");
  if (chart === null) return;
  const runs = [];
  let run = [];
  for (const item of moistureTrendItems) {
    const value = item.pore_water_ec_us_cm;
    const timestamp = Date.parse(item.reading?.observed_at || item.received_at);
    if (value == null || !Number.isFinite(timestamp)) {
      if (run.length) runs.push(run);
      run = [];
      continue;
    }
    run.push({ timestamp, value: Number(value) });
  }
  if (run.length) runs.push(run);
  const all = runs.flat();
  setHidden(chart, all.length < 2);
  setHidden(empty, all.length >= 2);
  if (all.length < 2) {
    setHidden(note, true);
    return;
  }
  const width = Math.max(260, Math.round(chart.getBoundingClientRect().width) || 640);
  const ideal = profile?.root_zone?.conductivity_us_cm?.ideal;
  if (!renderKeyChanged(chart, [moistureTrendVersion, all.length, all.at(-1), width, ideal])) return;
  chart.replaceChildren();

  const height = 170;
  setAttr(chart, "viewBox", `0 0 ${width} ${height}`);
  const plot = { left: 52, right: 10, top: 10, bottom: 20 };
  const plotWidth = width - plot.left - plot.right;
  const plotHeight = height - plot.top - plot.bottom;
  const startTime = all[0].timestamp;
  const endTime = all[all.length - 1].timestamp;
  const highest = Math.max(...all.map((point) => point.value), ideal ? ideal[1] : 0);
  const top = highest * 1.1 || 1;
  const x = (time) =>
    plot.left + ((time - startTime) / Math.max(1, endTime - startTime)) * plotWidth;
  const y = (value) => plot.top + (1 - value / top) * plotHeight;

  // The plant's own band, so the line is read against something.
  if (ideal) {
    chart.append(svgElement("rect", {
      class: "ideal-band", x: plot.left, width: plotWidth,
      y: y(ideal[1]), height: Math.max(1, y(ideal[0]) - y(ideal[1])),
    }));
  }
  for (const value of [0, top / 2, top]) {
    chart.append(
      svgElement("line", { class: "grid-line", x1: plot.left, x2: width - plot.right,
                           y1: y(value), y2: y(value) }),
      svgElement("text", { class: "axis-label", x: plot.left - 6, y: y(value) + 4,
                           "text-anchor": "end" }, String(Math.round(value)))
    );
  }
  for (const stretch of runs) {
    if (stretch.length === 1) {
      chart.append(svgElement("circle", { class: "history-point", r: 2,
        cx: x(stretch[0].timestamp), cy: y(stretch[0].value) }));
      continue;
    }
    chart.append(svgElement("polyline", {
      class: "history-line",
      points: stretch.map((point) => `${x(point.timestamp)},${y(point.value)}`).join(" "),
    }));
  }
  chart.append(svgElement("circle", {
    class: "history-point latest-point", r: 3.5,
    cx: x(all[all.length - 1].timestamp), cy: y(all[all.length - 1].value),
  }));

  const day = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" });
  chart.append(
    svgElement("text", { class: "axis-label", x: plot.left, y: height - 5,
                         "text-anchor": "start" }, day.format(new Date(startTime))),
    svgElement("text", { class: "axis-label", x: width - plot.right, y: height - 5,
                         "text-anchor": "end" }, day.format(new Date(endTime)))
  );

  const first = all[0].value;
  const last = all[all.length - 1].value;
  const direction = Math.abs(last - first) < Math.max(50, first * 0.1)
    ? "steady" : last > first ? "rising" : "falling";
  setHidden(note, false);
  setText(note, `${Math.round(first)} to ${Math.round(last)} µS/cm across the readings `
    + `wet enough to estimate from -- ${direction}`
    + `${ideal ? `, against a ${ideal[0]}-${ideal[1]} band` : ""}. `
    + "Gaps are stretches where the pot was too dry for the estimate to mean "
    + "anything. Rising over weeks is salt building up, which a heavy flush clears.");
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
  // One profile list now. The other belonged to the enroll dialog, which
  // duplicated what /onboarding does and has gone with the sensor inbox.
  const onboardingSelect = document.getElementById("onboarding-profile");
  for (const [id, profile] of Object.entries(profiles.profiles)) {
    onboardingSelect.add(new Option(profile.name, id));
  }
  setText(document.getElementById("chemistry-guidance"), profiles.guidance);
  renderProfileRanges();
}


/* What the hub can say about an entry, which is not always confidence.
 *
 * Something a person recorded is not a guess to be confirmed, and a light the
 * hub switched either answered or it did not. Only what was inferred from
 * readings is a matter of confidence. */
function careEventNote(event) {
  if (event.changes?.source === "manual") return "Recorded by hand";
  // Not something that happened to the plant: something the weighings imply.
  if (event.changes?.source === "water-use") return "Read from the weighings";

  if (event.kind === "drainage_assessment") return "Estimated response";
  if (event.kind === "lighting") {
    return event.confidence === "high" ? "Light confirmed" : "No answer from the light";
  }
  return event.confidence === "high" ? "High confidence" : "Confirm this event";
}

/* What the care log shows, and whether it is showing at all.
 *
 * A plant under a lamp collects fifty lighting lines a fortnight, and
 * somebody looking for when they last fed it has to read past all of them.
 * Which kinds are worth seeing is a standing preference rather than a
 * plant's property, so the hub keeps it and every plant obeys it.
 *
 * Whether the log is folded away is not: that is where somebody is on a
 * page right now, and it belongs to the browser they are reading it in. */
const CARE_KIND_LABELS = {
  watering: "Waterings",
  watering_due: "Overdue reminders",
  fertilizing: "Feedings",
  repotting: "Repottings",
  lighting: "Lights on and off",
  drainage_assessment: "Drainage readings",
  observation: "Readings from the weighings",
  note: "Notes",
};
const CARE_LOG_FOLDED_KEY = "open-plant-pulse.care-log-folded";
let careLogHiddenKinds = [];
let careLogFolded = false;
let lastCareLogItems = [];

function visibleCareEvents(items) {
  return items.filter((event) => !careLogHiddenKinds.includes(event.kind));
}

/* Where each list has read up to, and what it is a log of. The hub leaves the
 * switched-off kinds out before it counts to fifty, so a plant under a lamp no
 * longer spends its whole window on the light going on and off -- and what is
 * left can be walked back a page at a time. */
const CARE_LOG_PAGE = 50;
const careLogPaging = new Map();

function careLogQuery(listId, before) {
  const source = careLogPaging.get(listId);
  if (!source) return null;
  const fields = new URLSearchParams(source);
  fields.set("limit", String(CARE_LOG_PAGE));
  if (before) fields.set("before", before);
  return `/api/care-log?${fields}`;
}

/* Offered only when the page just filled: a short log has nothing more to
 * fetch, and a button that always says Load more and never loads anything is
 * worse than no button. */
function renderCareLogMore(listId, arrived) {
  const button = document.getElementById(
    "plant-care-log-more");
  if (button === null) return;
  const more = careLogPaging.has(listId) && arrived >= CARE_LOG_PAGE;
  setHidden(button, !more || careLogFolded);
}

async function loadMoreCareLog(listId) {
  const oldest = (lastCareLogItems[lastCareLogItems.length - 1] || {}).detected_at;
  const where = careLogQuery(listId, oldest);
  if (!where || !oldest) return;
  const button = document.getElementById(
    "plant-care-log-more");
  setText(button, "Loading...");
  button.disabled = true;
  try {
    const response = await fetch(where);
    if (!response.ok) return;
    const older = (await response.json()).items;
    // Appended rather than re-fetched from the top: the page keeps its place,
    // and anything recorded meanwhile is not silently inserted mid-scroll.
    renderCareLog(lastCareLogItems.concat(older), listId, {page: older.length});
  } finally {
    setText(button, "Load more");
    button.disabled = false;
  }
}

/* A box per kind the log actually holds. Offering all seven to a plant
 * whose log is four waterings is a wall of controls for nothing. */
function renderCareKindChoices(items) {
  const counts = new Map();
  for (const event of items) counts.set(event.kind, (counts.get(event.kind) || 0) + 1);
  const present = Object.keys(CARE_KIND_LABELS);
  for (const fieldset of document.querySelectorAll(".care-log-kinds")) {
    const renderKey = JSON.stringify([[...counts], careLogHiddenKinds]);
    if (fieldset.dataset.renderKey === renderKey) continue;
    setData(fieldset, "renderKey", renderKey);
    const legend = fieldset.querySelector("legend");
    fieldset.replaceChildren(legend);
    // Shown even for a single kind: a log that is fifty lighting lines and
    // nothing else is precisely the one somebody wants to switch off.
    setHidden(fieldset, present.length === 0);
    for (const kind of present) {
      const label = document.createElement("label");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.value = kind;
      box.checked = !careLogHiddenKinds.includes(kind);
      box.addEventListener("change", () => setCareKindShown(kind, box.checked));
      const text = document.createElement("span");
      const count = counts.get(kind) || 0;
      setText(text, count ? `${CARE_KIND_LABELS[kind]} (${count})`
        : CARE_KIND_LABELS[kind]);
      // A kind this plant has none of is still worth a box -- the choice
      // is for every plant -- but it should not look like something to read.
      if (!count) setData(label, "empty", "true");
      label.append(box, text);
      fieldset.append(label);
    }
  }
}

async function setCareKindShown(kind, shown) {
  const hidden = careLogHiddenKinds.filter((name) => name !== kind);
  if (!shown) hidden.push(kind);
  careLogHiddenKinds = hidden;
  // Drawn at once rather than after the round trip: the hub agreeing is
  // not what the box being ticked means.
  renderCareLog(lastCareLogItems, "plant-care-log");
  const response = await fetch("/api/settings/care-log", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ hidden }),
  });
  if (!response.ok) return;
  careLogHiddenKinds = (await response.json()).care_log_hidden_kinds;
  // A kind switched back on is not in what the browser already holds -- the
  // hub left it out of the window -- so the list is asked for again rather
  // than filtered again.
  await reloadCareLog("plant-care-log");
}

async function reloadCareLog(listId) {
  const where = careLogQuery(listId);
  if (!where) return;
  const response = await fetch(where);
  if (response.ok) renderCareLog((await response.json()).items, listId);
}

function renderCareLogFolded() {
  for (const button of document.querySelectorAll(".care-log-toggle")) {
    setText(button, careLogFolded ? "Show log" : "Hide log");
    setAttr(button, "aria-expanded", String(!careLogFolded));
  }
  for (const list of document.querySelectorAll(".care-log-list")) {
    setHidden(list, careLogFolded);
  }
  for (const more of document.querySelectorAll(".care-log-more")) {
    setHidden(more, careLogFolded);
  }
}

function toggleCareLog() {
  careLogFolded = !careLogFolded;
  try {
    window.localStorage.setItem(CARE_LOG_FOLDED_KEY, careLogFolded ? "folded" : "open");
  } catch (_error) {
    /* It still folds; it just forgets. */
  }
  renderCareLogFolded();
}

function restoreCareLogFolded() {
  try {
    careLogFolded = window.localStorage.getItem(CARE_LOG_FOLDED_KEY) === "folded";
  } catch (_error) {
    careLogFolded = false;
  }
  renderCareLogFolded();
}

function renderCareLog(items, listId = "plant-care-log", options = {}) {
  const list = document.getElementById(listId);
  if (list === null) return;
  // The boxes are offered from everything the log holds; the list is drawn
  // from what is left after the hidden kinds are taken out.
  lastCareLogItems = items;
  renderCareKindChoices(items);
  renderCareLogMore(listId,
    options.page === undefined ? items.length : options.page);
  items = visibleCareEvents(items);
  if (!renderKeyChanged(list, items)) return;
  list.replaceChildren();
  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "care-log-empty";
    // An empty log and a log emptied by the boxes above it are different
    // things, and saying the first when it is the second sends somebody
    // looking for a fault.
    setText(empty, lastCareLogItems.length
      ? "Nothing of the kinds being shown. Tick another above."
      : "No care events detected yet.");
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
    setText(confidence, careEventNote(event));
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

let wateringCalendarSource = null;

function renderWateringCalendar(items, range, watering) {
  if (!renderKeyChanged(document.getElementById("watering-calendar-grid"), [
    items, range.year, range.todayStart, watering, wateringCalendarSource
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
  setAttr(grid, "aria-label", `Watering and feeding for ${range.year}`);
  setText(document.getElementById("watering-calendar-title"), `${range.year} care history`);
  grid.replaceChildren();
  months.replaceChildren();
  let previousMonth = -1;
  for (let index = 0; index < range.weekCount * 7; index += 1) {
    const date = new Date(range.start);
    date.setUTCDate(date.getUTCDate() + index);
    const dateKey = utcDateKey(date);
    const activity = activityByDay.get(dateKey) || { watering_count: 0, drying_level: 0 };
    const count = activity.watering_count;
    const fed = Boolean(activity.fertilizing_count);
    const moisture = activity.final_moisture_percent;
    const cell = document.createElement("span");
    const formattedDate = date.toLocaleDateString([], { timeZone: "UTC", month: "long", day: "numeric", year: "numeric" });
    cell.className = "watering-calendar-day";
    setData(cell, "state", count ? "watered" : activity.drying_level ? "drying" : moisture != null ? "moisture" : "none");
    setData(cell, "level", count ? Math.min(3, count) : activity.drying_level);
    /* Feeding is an event on a day, not a state of it: a day can be watered,
       drying, or simply wet, and fed as well. It is marked on top rather than
       taking the square's colour, so neither answer hides the other. */
    setData(cell, "fertilized", fed ? "true" : "false");
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
    setAttr(cell, "aria-label",
      `${formattedDate}: ${description}${fed ? "; fertilized" : ""}`);
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
  const fedDays = items.reduce((sum, item) => sum + (item.fertilizing_count ? 1 : 0), 0);
  // Name the evidence. The shading is soil a probe measured on one
  // plant and water inferred from the pot's weight on another, and a
  // green square that means two different things without saying so is
  // worse than no square.
  const from = wateringCalendarSource === "probe" ? ", soil read by the probe"
    : wateringCalendarSource === "weight" ? ", wetness from the pot's weight"
    : "";
  setText(document.getElementById("watering-calendar-summary"),
    `${total} watering${total === 1 ? "" : "s"} and ${fedDays} feeding${fedDays === 1 ? "" : "s"} in ${range.year}${from}`);
}

async function refreshWateringCalendar() {
  // Keyed on the plant, and drawn for a plant with no sensor at all.
  //
  // This returned here unless a sensor had reported, so nine of ten
  // plants showed an empty care calendar for ever. Watering and feeding
  // are recorded by hand against the plant; needing a probe to see them
  // was an accident of the calendar being written when every plant had
  // one. Today's date does for the year when nothing has reported.
  const plantId = selectedPlant?.plant_id;
  if (!plantId) return;
  const range = wateringCalendarRange(latestReadingAt || new Date().toISOString());
  const profileId = selectedSensor?.profile_id || profiles.default_profile;
  const requestKey = [
    plantId,
    range.year,
    profileId
  ].join(":");
  if (wateringCalendarRequestKey === requestKey) return;
  wateringCalendarRequestKey = requestKey;
  try {
    const query = new URLSearchParams({
      plant_id: String(plantId),
      start: range.yearStart.toISOString(),
      end: range.yearEnd.toISOString()
    });
    const response = await fetch(`/api/watering-calendar?${query}`);
    if (!response.ok) throw new Error("calendar request failed");
    const payload = await response.json();
    if (wateringCalendarRequestKey !== requestKey) return;
    wateringIntervalSummary = payload.watering_interval;
    wateringCalendarSource = payload.wetness_source || null;
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


async function refresh() {
  try {
    const page = pageFromLocation();
    // The household grid shows sensored and manual plants side by side, so it
    // asks for both at once. Asking in turn paints the sensors a round trip
    // early, which is invisible on this machine and plain from anywhere else.
    if (page === "fleet") {
      await Promise.all([refreshFleet(), refreshPlants()]);
    } else {
      await refreshFleet();
    }
    if (page === "rooms") await refreshRoomsPage();
    if (page === "settings" && settingsTabFromLocation() === "lights") {
      // The plug names live in the driver catalogue, and a plug
      // renamed on its own page should not still be the old name here.
      await refreshLightDrivers();
    }
    if (page === "settings" && settingsTabFromLocation() === "box") {
      await Promise.all([refreshZigbee(), refreshBoxPage(), refreshBoxHistory()]);
    }
    if (page === "settings" && settingsTabFromLocation() === "alerts") {
      // The Zigbee panel's refresh fills zigbeeDevices, and it does not
      // run on this tab. Without this the page said "no leak sensors
      // are enrolled" about a house with one, which is the worst thing
      // an alerts page could be wrong about.
      await Promise.all([refreshAlertSettings(), refreshZigbee()]);
      renderAlertSources();
    }
    if (page === "room") await refreshRoomPage();
    if (page === "zigbee-device") await refreshDeviceAlerts();
    if (page === "plant") {
      await Promise.all([refreshPlant(), refreshPlantPhotos()]);
      /* What the plant's probe says, asked for by the plant.
       *
       * The soil chemistry, the readings, the drying trend, the watering
       * calendar and the drainage reading all moved here from the sensor's
       * page; their markup came with them and their requests did not, so
       * the sections drew nothing. A plant with no probe simply skips them.
       */
      const sensor = plantSensor();
      selectedSensorId = sensor?.sensor_id ?? null;
      selectedSensor = sensor ?? null;
      if (sensor) {
        const query = sensorQuery();
        const latest = await fetch(`/api/readings/latest${query}`);
        if (latest.ok) renderReading(await latest.json());
        await Promise.all([
          refreshMoistureTrend(), refreshPotResponse(),
        ]);
      }
      // Outside the sensor check: a plant's care history is its own,
      // and the calendar was invisible on every plant without a probe
      // purely because this call sat inside that branch.
      await refreshWateringCalendar();
      // The room's air, drawn the same way as a sensor's own: by this point
      // the plant is loaded, so its room's reading is known.
      await Promise.all([refreshClimateHistory(), refreshPlantLighting()]);
      return;
    }
    if (page === "sensor-config") {
      selectedSensorId = sensorIdFromLocation();
      selectedSensor = fleetSensors.find(
        (sensor) => sensor.sensor_id === selectedSensorId) || null;
      // The plants, so a sensor with none can be given one and so detaching
      // can name the plant it is leaving.
      await refreshPlants();
      if (selectedSensorId) {
        const reports = await fetch(`/api/raw-reports${sensorQuery()}`);
        if (reports.ok) renderRawReports((await reports.json()).items);
        await refreshFirmwareImages();
      }
      return;
    }
    if (page === "plant-config") {
      // The sensor's own settings are a section of this page, so it loads
      // what a sensor page used to: its firmware and its raw reports.
      selectedSensorId = plantSensor()?.sensor_id ?? null;
      selectedSensor = plantSensor() ?? null;
      await refreshPlantConfig();
      if (selectedSensorId) {
        const reports = await fetch(`/api/raw-reports${sensorQuery()}`);
        if (reports.ok) renderRawReports((await reports.json()).items);
        await refreshFirmwareImages();
      }
      return;
    }
    if (page === "settings") {
      if (settingsTabFromLocation() === "firmware") await refreshFirmwareImages();
      if (settingsTabFromLocation() === "lights") await refreshLights();
      if (settingsTabFromLocation() === "zigbee") await refreshZigbee();
      if (settingsTabFromLocation() === "time") await refreshHubTimezone();
      return;
    }
    if (page === "zigbee-device") {
      await refreshZigbee();
      renderZigbeeDevicePage();
      return;
    }
    if (page !== "sensor-config" && page !== "config") return;
    if (!selectedSensorId || !selectedSensor) {
      return;
    }
    const query = sensorQuery();
  } catch (error) {
    // Said out loud as well as shown. This catch is here for a hub that has
    // gone away, but it will just as happily swallow a page that threw, and
    // a bug in the page should not pass for a network that is down.
    console.error("refresh failed", error);
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


/* Plants nothing is watching, beside the ones that are.
 *
 * A card cannot say "healthy" about a plant with no readings, so it says what
 * is actually known: when it was last watered, and how much water the pot has
 * left when the scale can answer that. */
let householdPlants = [];

async function refreshPlants() {
  try {
    const response = await fetch("/api/plants");
    if (!response.ok) return;
    householdPlants = (await response.json()).items;
  } catch (_error) {
    return;
  }
  renderHousehold();
}


async function addPlant() {
  const name = window.prompt("What is this plant called?");
  if (!name) return;
  const response = await fetch("/api/plants", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ display_name: name }),
  });
  if (!response.ok) return;
  const plant = await response.json();
  window.location.href = `/plants/${plant.plant_id}`;
}

/* A plant with no sensor.
 *
 * The same page as an instrumented plant's, with the sections that would have
 * nothing to say left out: no readings, no charts, no firmware. What is left
 * is what somebody records by hand, and what the pot weighs. */
let selectedPlant = null;

function plantIdFromLocation() {
  const match = window.location.pathname.match(/^\/plants\/(\d+)(?:\/config)?$/);
  if (match) return Number(match[1]);
  // Reached by way of a sensor: the plant is whichever one it watches.
  const bySensor = window.location.pathname.match(/^\/sensors\/(.+)\/settings$/);
  if (bySensor) {
    const sensor = fleetSensors.find((s) => s.sensor_id === decodeURIComponent(bySensor[1]));
    return sensor?.plant_id ?? null;
  }
  return null;
}

/* Grams, everywhere.
 *
 * Each weight box used to carry a kg/g selector, remembered per browser,
 * with the typed number converted when it changed. That is three selects,
 * two placeholders per field and a stored preference, to support a unit
 * nobody here types: the scale reports grams, the database stores grams,
 * and every anchor and reading on this page is a whole number of them.
 * The unit is now said once in each label and never asked about. */

function showWeight(grams) {
  return grams == null ? "--" : String(Math.round(grams));
}

/* Clears the remembered kg/g choices.
 *
 * They live in each browser rather than in the database, so removing the
 * selectors would otherwise leave them behind for ever -- harmless, but
 * they are the sort of thing that confuses whoever next goes looking for
 * why a unit was chosen. */
function forgetWeightUnits() {
  // By prefix, not by a list of the three selectors that existed last.
  // Naming them missed "open-plant-pulse.weight-unit.dry", left behind by
  // an earlier box that had already gone -- which is exactly the kind of
  // leftover this is meant to sweep up.
  try {
    const stale = Object.keys(window.localStorage)
      .filter((key) => key.startsWith("open-plant-pulse.weight-unit"));
    for (const key of stale) window.localStorage.removeItem(key);
  } catch (_error) {
    /* Nothing reads them any more either way. */
  }
}

/* A plant that is weighed by a scale does not need to be asked for its
 * weight. The form stays, dimmed, because somebody will still put the pot
 * on a kitchen scale occasionally and a form that vanished cannot be used
 * when they do. */
let plantScaleSensor = null;

function renderPlantScale(scale) {
  const panel = document.getElementById("plant-weight-scale");
  const entry = document.getElementById("plant-weight-entry");
  if (!panel || !entry) return;
  plantScaleSensor = scale || null;
  setHidden(panel, !scale);
  entry.classList.toggle("is-automatic", Boolean(scale) && !plantWeighByHand);
  if (!scale) return;

  const link = document.getElementById("plant-weight-scale-link");
  if (link) {
    link.textContent = scale.display_name || scale.sensor_id;
    link.href = scale.eui ? `/zigbee/${scale.eui}`
      : `/sensors/${encodeURIComponent(scale.sensor_id)}/settings`;
  }
  const select = document.getElementById("plant-weight-schedule");
  if (select) {
    select.value = scale.keep_at_hour != null
      ? `at:${scale.keep_at_hour}`
      : `every:${scale.keep_every_seconds || 1800}`;
    // A schedule the hub does not offer is still the schedule in force, so
    // say so rather than silently showing a neighbouring option.
    if (!select.value) {
      const kept = document.createElement("option");
      kept.value = `every:${scale.keep_every_seconds}`;
      kept.textContent = describeInterval(scale.keep_every_seconds);
      select.append(kept);
      select.value = kept.value;
    }
  }
}

let plantWeighByHand = false;


/* Days since something, in the words somebody would use. */
/* Counted in days on the calendar, where the reader lives.
 *
 * Counting elapsed 24-hour blocks instead called a quarter past eight
 * last night "today" until a quarter past eight tonight. "Today" is a
 * date, not a duration, and somebody deciding whether to water again
 * needs the word to mean what it says. */
function daysSince(at) {
  if (!at) return null;
  const then = new Date(Date.parse(at));
  if (Number.isNaN(then.getTime())) return null;
  const midnight = (date) => new Date(date.getFullYear(), date.getMonth(), date.getDate());
  const days = Math.round((midnight(new Date()) - midnight(then)) / 86400000);
  if (days <= 0) return "today";
  if (days === 1) return "yesterday";
  return `${days} days ago`;
}

/* Where a plant's light sits against what its kind wants.
 *
 * The daily total on its own says little: fourteen mol is generous for a
 * pothos and thin for a jasmine. So it is shown the way air temperature
 * and humidity are -- a marker on the profile's scale, with the healthy
 * span marked -- and the rest of the scale is coloured by band, because
 * "too little" and "scorching" are different kinds of wrong and a single
 * ideal range cannot say which one a plant is in.
 *
 * The figure quoted is the last *complete* day. At nine in the morning
 * today's total is a third of what it will be, and a marker sitting in
 * "too little" because the day is young would be alarming and wrong. */
function renderPlantLight() {
  const section = document.querySelector('[data-section="light"]');
  if (!section) return;
  const days = (selectedPlant && selectedPlant.light_days) || [];
  const profile = profiles?.profiles?.[
    selectedPlant?.profile_id || profiles?.default_profile];
  const metric = profile?.light?.dli_mol_m2_day;

  /* One number, and it is always a whole day.
   *
   * Either the last day the hub watched from end to end, or -- where no
   * lamp it switches lights this plant -- the day the typed hours
   * describe. Never a part of today: half a day's light is a true
   * figure that answers a question nobody asked, and sitting it beside
   * yesterday's it reads as a collapse rather than a morning.
   */
  const ppfd = selectedPlant?.ppfd_umol_m2s;
  const typedHours = selectedPlant?.light_hours_per_day;
  const complete = days.filter((day) => day.complete);
  const logged = complete.length ? complete[complete.length - 1] : null;
  const today = days.find((day) => !day.complete) || null;
  const assumed = (!logged && ppfd && typedHours)
    ? { dli: Math.round(ppfd * typedHours * 3.6) / 1000,
        lit_hours: typedHours }
    : null;
  const latest = logged || assumed;

  setHidden(section, !metric || !latest);
  if (!metric || !latest) return;

  const card = document.getElementById("plant-light-gauge");

  setText(document.getElementById("plant-light-latest"),
          latest ? latest.dli.toFixed(1) : "--");
  setText(document.getElementById("plant-light-period"),
    logged ? "Daily light \u00b7 last full day" : "Daily light \u00b7 from hours you entered");
  // = what you measured, times how long the lamps were on.
  setText(document.getElementById("plant-light-working"),
    latest && ppfd
      ? `= ${ppfd} \u00b5mol/m\u00b2/s \u00d7 ${latest.lit_hours} h`
        + (assumed ? " a day" : " lit")
      : "");
  const unsure = days.reduce((total, day) => total + (day.unsure_hours || 0), 0);
  setText(document.getElementById("plant-light-summary"), assumed
    ? "No full day logged yet \u00b7 from the hours you entered until there is one"
    : `${complete.length} full day${complete.length === 1 ? "" : "s"} recorded`
      + (unsure ? ` \u00b7 ${unsure.toFixed(1)} h the hub could not vouch for` : ""));

  renderLightBands(card, metric);
  wireLightTrack();
  renderGauge(card, "dli_mol_m2_day", metric, latest ? latest.dli : null);
  /* Not while somebody is reading it. The page refreshes on a timer, and
     a refresh that lands mid-sentence replaces the figure under the
     cursor with the plant's own -- which looks like the bar forgetting
     what was asked of it, two seconds after it was asked. */
  if (!pointerOnLightTrack) {
    setText(document.getElementById("plant-light-hint"), latest
      ? `${latest.dli.toFixed(1)} mol/m\u00b2/day `
        + `${logged ? "on its last full day" : "every day"} \u2014 `
        + `${bandFor(metric, latest.dli)} for ${profile.name}. ${lightEffect(metric, latest.dli)}`
      : "No complete day yet. Point along the bar to read what a day of any amount would do.");
  }
}

/* What a day of this much light would do to a plant in general.
 *
 * A fallback. Where a profile gives a band its own `effects` it wins,
 * because what too much light looks like is a fact about the species.
 *
 * The five bands say which box a figure falls in; they cannot say that
 * 12.1 and 19.9 are both "healthy" but not the same healthy. So each band
 * is read in thirds -- its bottom, middle and top -- which is as fine as
 * a starter figure honestly supports. Anything more precise would be
 * inventing certainty: these ranges are a sensible beginning for a kind
 * of plant, not a measurement of this one.
 *
 * Phrased about what the plant does, because "getting by" is a label and
 * "it will hold its leaves but put out few new ones" is an answer. */
const LIGHT_EFFECTS = {
  "too little": [
    "Deep shade for this kind. It holds what it has and grows almost nothing new.",
    "Too little. Growth stalls, new leaves come smaller, and stems stretch toward the window.",
    "Still short. It will tick over rather than fill out, and lower leaves may yellow and drop.",
  ],
  "getting by": [
    "Scraping by. Enough to keep it alive, not enough to replace what it loses.",
    "Modest. It grows slowly and stays thin; expect a new leaf now and then rather than steadily.",
    "Almost enough. Growth is real but unhurried, and it will take longer to dry its pot.",
  ],
  "healthy": [
    "Comfortable. Steady growth, good colour, and a pot that dries at a predictable rate.",
    "Where this kind is happiest. Leaves come regularly and hold their size and colour.",
    "Generously lit. Strong growth, and it will drink and feed noticeably harder.",
  ],
  "vigorous": [
    "More than it needs. Fast growth, and water and feed will run down quicker.",
    "Pushing it. Growth is rapid but leaves may come paler and thicker, and it dries fast.",
    "Near the limit. Watch for bleaching on the leaves that face the lamp most directly.",
  ],
  "scorching": [
    "Past what this kind can use. Expect bleached patches where the light lands hardest.",
    "Too much. Leaves scorch, especially in dry air or with the pot run dry.",
    "Far too much. This damages leaves rather than growing them.",
  ],
};

/* Which band a value is in, and how far through it -- 0 at the bottom
   edge, 1 at the top. The last band has no top, so it is read against
   the width of the one before it. */
function bandPosition(metric, value) {
  const bands = metric.bands || [];
  const [scaleLow, scaleHigh] = metric.scale;
  let from = scaleLow;
  for (let index = 0; index < bands.length; index += 1) {
    const band = bands[index];
    const last = band.upto == null;
    const to = last ? scaleHigh : band.upto;
    if (last || value < to) {
      const width = to - from || 1;
      return { band, share: Math.max(0, Math.min(1, (value - from) / width)), from, to };
    }
    from = to;
  }
  return null;
}

/* How old the plant is, in months, or null where nobody has said. */
function plantAgeMonths(plant) {
  const subject = plant || selectedPlant;
  if (!subject?.started_at) return null;
  const began = Date.parse(subject.started_at);
  if (!Number.isFinite(began)) return null;
  return Math.max(0, (Date.now() - began) / (1000 * 60 * 60 * 24 * 30.44));
}

function describePlantAge(months) {
  if (months < 1) return "under a month old";
  if (months < 24) return `${Math.round(months)} months old`;
  return `${(months / 12).toFixed(months < 120 ? 1 : 0)} years old`;
}

/* Young or established, against the age its kind grows up at.
 *
 * A plant nobody has dated is read as established. The alternative is to
 * call every undated plant a juvenile, which would quietly excuse a
 * grown plant that really is short of light -- the error that matters
 * more, because it is the one that changes nothing. */
function plantStage(plant) {
  const subject = plant || selectedPlant;
  const months = plantAgeMonths(subject);
  if (months == null) return "established";
  const profile = profiles?.profiles?.[
    subject?.profile_id || profiles?.default_profile];
  const grownUp = profile?.light?.dli_mol_m2_day?.established_after_months;
  if (!grownUp) return "established";
  return months < grownUp ? "young" : "established";
}

function lightEffect(metric, value) {
  const found = bandPosition(metric, value);
  if (!found) return "";
  /* The profile's own words where it has them: a bleached pothos goes
     cream and a scorched spider plant browns at the tips, and one
     sentence cannot be true of both. Where a profile says nothing, the
     wording above is true of plants in general, which is better than
     silence and honest about being general. */
  const staged = found.band.effects;
  const thirds = (staged && staged[plantStage()]) || LIGHT_EFFECTS[found.band.name];
  if (!thirds) return found.band.name;
  return thirds[found.share < 1 / 3 ? 0 : found.share < 2 / 3 ? 1 : 2];
}

/* Reading the bar with a pointer. Works with a finger as well as a mouse,
   because pointer events cover both, and leaves nothing behind when the
   pointer goes: a hint that outlives the cursor reads as a fact about the
   plant rather than a question somebody asked. */
// Whether a pointer is on the light bar right now, so a refresh does not
// talk over it.
let pointerOnLightTrack = false;

function wireLightTrack() {
  const track = document.getElementById("plant-light-track");
  const hint = document.getElementById("plant-light-hint");
  const probe = document.getElementById("plant-light-probe");
  if (!track || track.dataset.wired) return;
  setData(track, "wired", "true");

  const show = (event) => {
    const metric = lightMetric();
    if (!metric) return;
    pointerOnLightTrack = true;
    const box = track.getBoundingClientRect();
    const share = Math.max(0, Math.min(1, (event.clientX - box.left) / box.width));
    const [scaleLow, scaleHigh] = metric.scale;
    const value = scaleLow + share * (scaleHigh - scaleLow);
    setHidden(probe, false);
    probe.style.left = `${share * 100}%`;
    const latest = lastCompleteLightDay();
    const against = latest == null ? ""
      : ` \u00b7 ${Math.abs(value - latest.dli).toFixed(1)} ${value >= latest.dli ? "more" : "less"} than its last full day`;
    /* What it would take to land there. The hours are the ones this plant
       actually keeps, so the figure answers "how much brighter?" rather
       than describing some other plant's day -- and it is the number you
       would change, by moving the lamp closer or turning it up, since the
       hours are the schedule's business. */
    const hours = latest && latest.lit_hours ? latest.lit_hours : null;
    const needs = hours
      ? ` Over its usual ${hours} h that needs a PPFD of ${Math.round(value * 1e6 / (hours * 3600))}.`
      : "";
    setText(hint, `${value.toFixed(1)} mol/m\u00b2/day \u2014 ${bandFor(metric, value)}${against}.${needs} `
      + lightEffect(metric, value));
  };
  track.addEventListener("pointermove", show);
  track.addEventListener("pointerdown", show);
  // Back to the plant's own figure when the pointer goes: a sentence
  // about a value nobody is pointing at any more reads as a fact about
  // this plant rather than an answer to a question that has ended.
  const release = () => {
    pointerOnLightTrack = false;
    setHidden(probe, true);
    renderPlantLight();
  };
  track.addEventListener("pointerleave", release);
  // A pointer can be taken away without leaving -- a touch cancelled by a
  // scroll, a window losing focus mid-drag -- and the line would then stay
  // on a value nobody is pointing at.
  track.addEventListener("pointercancel", release);
  window.addEventListener("blur", release);
}

function lightMetric() {
  const profile = profiles?.profiles?.[
    selectedPlant?.profile_id || profiles?.default_profile];
  return profile?.light?.dli_mol_m2_day || null;
}

function lastCompleteLightDay() {
  const complete = ((selectedPlant && selectedPlant.light_days) || [])
    .filter((day) => day.complete);
  return complete.length ? complete[complete.length - 1] : null;
}

// Which named band a figure falls in. The last band has no top edge.
function bandFor(metric, value) {
  const bands = metric.bands || [];
  const found = bands.find((band) => band.upto == null || value < band.upto);
  return found ? found.name : "unknown";
}

/* The scale behind the marker, one block per band. Drawn once per profile:
   the bands are the plant's kind, not its readings, so they do not change
   when a day does. */
function renderLightBands(card, metric) {
  const host = card.querySelector(".light-bands");
  const renderKey = JSON.stringify(metric.bands || []);
  if (host.dataset.renderKey === renderKey) return;
  setData(host, "renderKey", renderKey);
  host.replaceChildren();
  const [scaleLow, scaleHigh] = metric.scale;
  let from = scaleLow;
  (metric.bands || []).forEach((band) => {
    const upto = band.upto == null ? scaleHigh : Math.min(band.upto, scaleHigh);
    const width = (upto - from) / (scaleHigh - scaleLow) * 100;
    if (width <= 0) { from = upto; return; }
    const block = document.createElement("i");
    block.className = "light-band";
    setData(block, "band", band.name.replace(/ /g, "-"));
    block.style.width = `${width}%`;
    block.title = `${band.name}: ${from}\u2013${band.upto == null ? "" : band.upto} mol/m\u00b2/day`;
    host.append(block);
    from = upto;
  });
}

function renderPlantPage() {
  if (!selectedPlant) return;
  const weight = selectedPlant.weight || {};
  const journey = selectedPlant.journey || {};

  setText(document.getElementById("plant-page-name"), selectedPlant.display_name);
  /* What it is and where it lives, in one line: "Pothos · Marble Queen · in
   * the Living room", with whichever parts are known. */
  const profile = profiles?.profiles?.[selectedPlant.profile_id];
  const variety = profile?.varieties?.find((kind) => kind.id === selectedPlant.variety);
  setText(document.getElementById("plant-page-room"), [
    profile?.name,
    variety?.name,
    selectedPlant.room ? "in the " + selectedPlant.room : "no room recorded",
  ].filter(Boolean).join(" · "));
  const cared = [
    ["Watered", daysSince(selectedPlant.last_watered_at)],
    ["fed", daysSince(selectedPlant.last_fed_at)],
    ["weighed", daysSince(selectedPlant.weight?.latest?.at)],
  ].filter(([, since]) => since).map(([word, since]) => `${word} ${since}`);
  setText(document.getElementById("plant-page-watered"),
    cared.length ? cared.join(" · ") : "Never watered");

  /* The headline says the most actionable true thing, and nothing more: how
   * long the water will last if that is known, what is left if only that is,
   * and otherwise simply that nothing has been weighed. */
  const state = weight.days_left != null
    ? `About ${weight.days_left} days of water left`
    : weight.remaining_percent != null
      ? `${weight.remaining_percent}% of its water left`
      : weight.used_grams != null
        ? `${weight.used_grams} ml used since watering`
        : "Nothing weighed yet";
  setText(document.getElementById("plant-page-state"), state);

  renderPlantWeightOffset();
  setText(document.getElementById("plant-weight-latest"),
    weight.latest ? showWeight(weight.latest.grams) : "--");
  setText(document.getElementById("plant-weight-latest-label"), "Weighs now");
  setText(document.getElementById("plant-weight-used"),
    weight.used_grams == null ? "--" : String(weight.used_grams));
  setText(document.getElementById("plant-weight-remaining"),
    weight.remaining_percent == null ? "--" : weight.remaining_percent + "%");
  setText(document.getElementById("plant-weight-remaining-label"),
    weight.thirsty_percent == null
      ? "Water left"
      : `Water left · wants water at ${weight.thirsty_percent}%`);
  setText(document.getElementById("plant-weight-daily"),
    weight.daily_grams == null ? "--" : String(weight.daily_grams));
  setText(document.getElementById("plant-weight-days"),
    weight.days_left == null ? "--" : String(weight.days_left));
  /* "weighed" only for a weighing: an anchor typed in from memory was set,
   * which is a different claim about a different moment. */
  setText(document.getElementById("plant-weight-summary"),
    weight.full
      ? `Full at ${showWeight(weight.full.grams)} g, set ${daysSince(weight.full.at)}`
      : "No watered weight yet");

  renderPlantScale(selectedPlant.scale);
  renderPlantLight();

  setText(document.getElementById("plant-journey-days"), journey.monitored_days ?? "--");
  setText(document.getElementById("plant-journey-waterings"), journey.watering_count ?? "--");
  setText(document.getElementById("plant-journey-fertilizing"), journey.fertilizing_count ?? "--");
  setText(document.getElementById("plant-journey-lighting"), journey.lighting_hours ?? "--");
  setText(document.getElementById("plant-journey-started"),
    selectedPlant.created_at
      ? "Since " + new Date(selectedPlant.created_at).toLocaleDateString(
          undefined, { dateStyle: "medium" })
      : "Waiting for its first day");

  // Today, until somebody picks another day: the poll a second later must
  // not take back the date they just chose.
  const dateField = document.getElementById("plant-weight-date");
  dateField.max = localDate(new Date());
  if (!dateField.value) resetWeighingDate();
  renderPlantWeightChart();
  renderWeighingLog();
  renderForecast();
  renderWaterUse();
  fillPlantAnchorFields();
  careLogPaging.set("plant-care-log", `plant_id=${selectedPlant.plant_id}`);
  renderCareLog(selectedPlant.care_log || [], "plant-care-log");

  // Watering is recorded by weighing the full pot, so the log keeps only the
  // buttons for what no weighing can tell: feeding, and whatever was noticed.
  const today = new Date().toDateString();
  for (const [kind, id, label] of [
    ["fertilizing", "plant-fertilized", "Fertilized today"],
    ["repotting", "plant-repotted", "Repotted"],
  ]) {
    const done = (selectedPlant.care_log || []).some((event) =>
      event.kind === kind && new Date(event.detected_at).toDateString() === today);
    const button = document.getElementById(id);
    setText(button, done ? `${label} ✓` : label);
    setDisabled(button, done);
  }
}

/* Photographs of a plant.
 *
 * A photograph off a phone is several megabytes of detail nobody looking at a
 * plant page needs, so it is shrunk here before it is sent: the hub keeps
 * what the browser would have had to download anyway. The original stays on
 * the camera, which is where the archive belongs. */
const PHOTO_MAX_EDGE = 1600;

/* Where to ask for a photograph.
 *
 * The id alone is not a stable name: SQLite hands a deleted row's id to the
 * next one, so /api/photos/4 can mean a different picture tomorrow -- and
 * the browser, told to keep it for a year, would go on showing yesterday's.
 * The digest makes the address name the bytes. */
function photoUrl(photoId, digest) {
  return `/api/photos/${photoId}?v=${(digest || "").slice(0, 16)}`;
}
const PHOTO_QUALITY = 0.85;
let plantPhotos = [];
let coverFollowsLatest = false;

/* When the camera says the photograph was taken.
 *
 * The downsizing below re-encodes the picture through a canvas, which keeps
 * none of the metadata -- a deliberate thing for the location tag, and a
 * loss for this one. So the date is read off the original here and sent
 * beside the bytes. A plant's record is meant to outlast the year it was
 * started in, and a picture from three summers ago belongs at that end of
 * it, not at today's.
 *
 * EXIF dates carry no time zone; they are local time where the shutter was
 * pressed, and are read as local time here. */
async function photographTakenAt(file) {
  const head = new DataView(await file.slice(0, 262144).arrayBuffer());
  let index = 2;
  try {
    if (head.getUint16(0) !== 0xFFD8) return fallbackTakenAt(file);
    while (index + 4 <= head.byteLength && head.getUint8(index) === 0xFF) {
      const marker = head.getUint8(index + 1);
      if (marker === 0xD8 || (marker >= 0xD0 && marker <= 0xD7)) { index += 2; continue; }
      if (marker === 0xDA) break;
      const length = head.getUint16(index + 2);
      if (marker === 0xE1) {
        const text = String.fromCharCode(
          head.getUint8(index + 4), head.getUint8(index + 5),
          head.getUint8(index + 6), head.getUint8(index + 7));
        if (text === "Exif") {
          const taken = readExifDate(head, index + 10);
          if (taken !== null) return taken;
        }
      }
      index += 2 + length;
    }
  } catch (_error) {
    /* A truncated or odd file simply has no date here. */
  }
  return fallbackTakenAt(file);
}

/* The file's own timestamp, which the Convert to JPEG service sets to the
 * capture time, so a converted photograph keeps its date. Anything from this
 * minute is the copy rather than the capture, and is not worth claiming. */
function fallbackTakenAt(file) {
  if (!file.lastModified) return null;
  const when = new Date(file.lastModified);
  return Date.now() - file.lastModified < 60_000 ? null : when.toISOString();
}

function readExifDate(view, start) {
  const big = view.getUint16(start) === 0x4D4D;
  const u16 = (at) => view.getUint16(at, !big);
  const u32 = (at) => view.getUint32(at, !big);
  const walk = (directory) => {
    const count = u16(directory);
    for (let entry = 0; entry < count; entry += 1) {
      const at = directory + 2 + entry * 12;
      const tag = u16(at);
      // 0x9003 is when the shutter was pressed; 0x0132 is when the file was
      // last written, which a camera sets to the same moment.
      if (tag === 0x9003 || tag === 0x0132) {
        const text = [];
        const offset = start + u32(at + 8);
        for (let i = 0; i < 19; i += 1) text.push(String.fromCharCode(view.getUint8(offset + i)));
        const stamp = text.join("");
        const match = stamp.match(/^(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})$/);
        if (match) {
          const [, y, mo, d, h, mi, s] = match.map(Number);
          return new Date(y, mo - 1, d, h, mi, s).toISOString();
        }
      }
      // The Exif sub-directory, where DateTimeOriginal actually lives.
      if (tag === 0x8769) {
        const nested = walk(start + u32(at + 8));
        if (nested !== null) return nested;
      }
    }
    return null;
  };
  return walk(start + u32(start + 4));
}

async function shrinkPhoto(file) {
  const bitmap = await createImageBitmap(file).catch(() => null);
  if (bitmap === null) return null;
  const scale = Math.min(1, PHOTO_MAX_EDGE / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(bitmap.width * scale);
  canvas.height = Math.round(bitmap.height * scale);
  canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  bitmap.close();
  const shrunk = await new Promise((resolve) =>
    canvas.toBlob(resolve, "image/jpeg", PHOTO_QUALITY));
  // A picture that grew in the shrinking -- a small PNG screenshot, say -- is
  // sent as it came rather than made worse.
  return shrunk !== null && shrunk.size < file.size ? shrunk : file;
}

/* Which plant is being looked at, however it was reached: a plant with a
 * sensor is still the plant, and its photographs are the plant's rather than
 * the instrument's. */
function currentPlantId() {
  return plantIdFromLocation() ?? selectedSensor?.plant_id ?? null;
}

/* A date typed beside the Add photos button, for pictures whose own date is
 * gone or was never there. It is the whole upload's date, since a batch
 * chosen together is usually one occasion. Midday local, so no time zone
 * moves it to the day before. */
function chosenPhotoDate() {
  const chosen = document.getElementById("plant-photo-date")?.value;
  if (!chosen) return null;
  const [year, month, day] = chosen.split("-").map(Number);
  return new Date(year, month - 1, day, 12, 0, 0).toISOString();
}

async function uploadPlantPhotos(files) {
  const plantId = currentPlantId();
  const message = document.getElementById("plant-photo-message");
  if (plantId === null || !files.length) return;
  let added = 0;
  for (const [index, file] of [...files].entries()) {
    setText(message, `Sending ${index + 1} of ${files.length}…`);
    const body = await shrinkPhoto(file);
    if (body === null) {
      // Worth naming: a photograph taken on an iPhone is HEIC, and Chrome
      // cannot decode one however willing the page is.
      setText(message, /\.hei[cf]$/i.test(file.name)
        ? `${file.name} is a HEIC photograph, which this browser cannot read. `
          + "Convert it to JPEG first, or open this page in Safari."
        : `${file.name} is not a picture this browser can read.`);
      continue;
    }
    // What somebody typed beats what the file says: a picture scanned
    // years later has a date, and it is not the day it was scanned.
    const takenAt = chosenPhotoDate() || await photographTakenAt(file);
    const where = `/api/plants/${plantId}/photos`
      + (takenAt ? `?taken_at=${encodeURIComponent(takenAt)}` : "");
    const response = await fetch(where, {
      method: "POST",
      headers: { "Content-Type": body.type || "application/octet-stream" },
      body,
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      setText(message, payload.error || `${file.name} could not be stored.`);
      continue;
    }
    added += 1;
  }
  // Cleared, so the next upload is not filed under the last one's date.
  const dated = document.getElementById("plant-photo-date");
  if (dated) dated.value = "";
  setText(message, added ? `Added ${added}.` : "");
  await refreshPlantPhotos();
  if (pageFromLocation() === "plant") await refreshPlant();
}

async function refreshPlantPhotos() {
  const plantId = currentPlantId();
  if (plantId === null) return;
  const response = await fetch(`/api/plants/${plantId}/photos`);
  if (!response.ok) return;
  const payload = await response.json();
  plantPhotos = payload.items;
  // Set from the same answer the pictures came in, so the box and the
  // photographs it is about appear together.
  coverFollowsLatest = Boolean(payload.cover_follows_latest);
  renderPlantPhotos();
}

/* On a phone the grid is one column, so a plant with a year of pictures
   buries every section under it. The newest is the one somebody came to
   see; the rest are a tap away. On a wider screen they sit side by side
   and nothing is hidden -- which is why the collapsing lives in a media
   query and the button is simply not shown there. */
let photosExpanded = false;

/* Looking through a plant's photographs.
 *
 * Newer is up, older is down. A plant's pictures are a time series and
 * the question is nearly always "what did it look like before", so the
 * axis is the one the dates already run along -- and a vertical swipe on
 * a phone means the same thing as the arrow keys on a desktop.
 *
 * The list is whatever the page is showing, so the order and the dates
 * are the ones on screen rather than a second copy that could drift.
 */
let viewedPhotoId = null;

function photoViewerList() {
  return plantPhotos || [];
}

function openPhotoViewer(photoId) {
  const viewer = document.getElementById("photo-viewer");
  if (!viewer || !photoViewerList().some((photo) => photo.photo_id === photoId)) return;
  viewedPhotoId = photoId;
  resetPhotoZoom();
  setHidden(viewer, false);
  // The page behind must not scroll under the viewer: on a phone a swipe
  // meant for the pictures would otherwise take the page with it.
  document.body.style.overflow = "hidden";
  renderPhotoViewer();
  document.getElementById("photo-viewer-close").focus();
}

function closePhotoViewer() {
  const viewer = document.getElementById("photo-viewer");
  if (!viewer || viewer.hidden) return;
  setHidden(viewer, true);
  document.body.style.overflow = "";
  // Back to the tile it was opened from, so the keyboard does not start
  // again at the top of the page.
  const tile = document.querySelector(`#plant-photo-grid a[data-photo-id="${viewedPhotoId}"]`);
  viewedPhotoId = null;
  if (tile) tile.focus();
}

function stepPhotoViewer(by) {
  const photos = photoViewerList();
  const at = photos.findIndex((photo) => photo.photo_id === viewedPhotoId);
  if (at < 0) return;
  const next = photos[at + by];
  if (!next) return;
  viewedPhotoId = next.photo_id;
  // A new picture is looked at whole first; carrying the old zoom over
  // would open it already cropped to somewhere that meant nothing here.
  resetPhotoZoom();
  renderPhotoViewer();
}

function renderPhotoViewer() {
  const viewer = document.getElementById("photo-viewer");
  if (!viewer || viewer.hidden) return;
  const photos = photoViewerList();
  const at = photos.findIndex((photo) => photo.photo_id === viewedPhotoId);
  if (at < 0) { closePhotoViewer(); return; }
  const photo = photos[at];
  const taken = photo.taken_at || photo.added_at;

  const image = document.getElementById("photo-viewer-image");
  image.src = photoUrl(photo.photo_id, photo.digest);
  setAttr(image, "alt", `${selectedPlant?.display_name || "This plant"}, ${daysSince(taken)}`);

  setText(document.getElementById("photo-viewer-when"),
    new Date(taken).toLocaleDateString([], {year: "numeric", month: "long", day: "numeric"}));
  /* How long before now, and how far from the picture above it -- the gap
     is what says whether a change took a fortnight or half a year. */
  const previous = photos[at + 1];
  const apart = previous
    ? ` \u00b7 ${describeSpan(Date.parse(taken) - Date.parse(previous.taken_at || previous.added_at))} after the one below`
    : "";
  setText(document.getElementById("photo-viewer-since"), `${daysSince(taken)}${apart}`);
  setText(document.getElementById("photo-viewer-count"), `${at + 1} of ${photos.length}`);

  setDisabled(document.getElementById("photo-viewer-newer"), at === 0);
  setDisabled(document.getElementById("photo-viewer-older"), at === photos.length - 1);

  renderPhotoTrack(photos, at);

  /* The neighbours are fetched now so a step is instant. Without this the
     next picture arrives blank and fills in, which reads as the viewer
     having lost its place. */
  for (const near of [photos[at - 1], photos[at + 1]]) {
    if (near) new Image().src = photoUrl(near.photo_id, near.digest);
  }
}

/* Where each photograph sits on the plant's history.
 *
 * Spaced by elapsed time, but with each gap compressed -- the width of a
 * gap grows with the logarithm of its length, plus a fixed amount per
 * photograph so that pictures taken minutes apart are still separately
 * reachable.
 *
 * Strictly proportional was the first attempt and it was unusable on any
 * plant with an old photograph: a spider plant here has one picture from
 * 2023 and the rest from this month, so a single gap took 98% of the
 * track and sixteen photographs shared the remaining six pixels. The
 * ordering and the sense of "this gap is much longer than that one"
 * survive compression; being able to reach a photograph does not survive
 * the alternative. A gap long enough to have been squeezed is marked, so
 * the track says where it has been economical with the truth.
 */
const PHOTO_TICK_ROOM = 0.6;   // every photograph gets a little width
const PHOTO_GAP_MARKED_DAYS = 45;  // longer than this and the gap is drawn as one

function photoAt(photo) {
  return Date.parse(photo.taken_at || photo.added_at);
}

/* Positions along the track, keyed by photo id, plus the gaps that were
   compressed enough to be worth marking. Photos arrive newest first and
   the track reads oldest to newest, so it walks them backwards. */
function photoTrackLayout(photos) {
  const oldestFirst = [...photos].reverse().filter((photo) => Number.isFinite(photoAt(photo)));
  if (!oldestFirst.length) return null;
  const steps = [0];
  for (let index = 1; index < oldestFirst.length; index += 1) {
    const days = Math.max(0,
      (photoAt(oldestFirst[index]) - photoAt(oldestFirst[index - 1])) / 86400000);
    steps.push(PHOTO_TICK_ROOM + Math.log1p(days));
  }
  const running = [];
  steps.reduce((total, step) => {
    running.push(total + step);
    return total + step;
  }, 0);
  const span = running[running.length - 1] || 1;
  const at = new Map();
  const marks = [];
  oldestFirst.forEach((photo, index) => {
    at.set(photo.photo_id, running[index] / span * 100);
    if (index === 0) return;
    const days = (photoAt(photo) - photoAt(oldestFirst[index - 1])) / 86400000;
    if (days >= PHOTO_GAP_MARKED_DAYS) {
      marks.push({
        middle: (running[index] + running[index - 1]) / 2 / span * 100,
        days: Math.round(days),
      });
    }
  });
  return {at, marks, first: photoAt(oldestFirst[0]),
          last: photoAt(oldestFirst[oldestFirst.length - 1])};
}

function renderPhotoTrack(photos, at) {
  const ticks = document.getElementById("photo-track-ticks");
  const track = document.getElementById("photo-track");
  if (!ticks || !track) return;
  const layout = photoTrackLayout(photos);
  const renderKey = JSON.stringify([photos.map((photo) => photo.photo_id), at]);
  if (ticks.dataset.renderKey !== renderKey) {
    setData(ticks, "renderKey", renderKey);
    ticks.replaceChildren();
    for (const mark of (layout ? layout.marks : [])) {
      const gap = document.createElement("i");
      gap.className = "photo-track-gap";
      gap.style.left = `${mark.middle}%`;
      gap.title = `${describeSpan(mark.days * 86400000)} with no photograph`;
      ticks.append(gap);
    }
    photos.forEach((photo, index) => {
      const tick = document.createElement("i");
      tick.className = "photo-track-tick";
      tick.style.left = `${layout ? layout.at.get(photo.photo_id) ?? 50 : 50}%`;
      if (index === at) setData(tick, "current", "true");
      ticks.append(tick);
    });
  }
  setAttr(track, "aria-valuemin", 0);
  setAttr(track, "aria-valuemax", photos.length - 1);
  setAttr(track, "aria-valuenow", photos.length - 1 - at);
  setAttr(track, "aria-valuetext",
    new Date(photoAt(photos[at])).toLocaleDateString([], {month: "long", day: "numeric", year: "numeric"}));
  const shortDate = (time) => new Date(time).toLocaleDateString([], {month: "short", year: "numeric"});
  setText(document.getElementById("photo-track-first"), layout ? shortDate(layout.first) : "");
  setText(document.getElementById("photo-track-last"), layout ? shortDate(layout.last) : "");
}

/* Pointing picks the photograph whose tick is nearest, which is now the
   same thing as nearest in pixels: the track is no longer linear in
   time, so comparing dates would send the finger somewhere else. */
function seekPhotoTrack(clientX) {
  const track = document.getElementById("photo-track");
  const photos = photoViewerList();
  const layout = photoTrackLayout(photos);
  if (!track || !layout) return;
  const box = track.getBoundingClientRect();
  const share = Math.max(0, Math.min(100, (clientX - box.left) / box.width * 100));
  let nearest = photos[0];
  for (const photo of photos) {
    const here = layout.at.get(photo.photo_id);
    if (here === undefined) continue;
    if (Math.abs(here - share) < Math.abs((layout.at.get(nearest.photo_id) ?? 0) - share)) {
      nearest = photo;
    }
  }
  if (nearest.photo_id === viewedPhotoId) return;
  viewedPhotoId = nearest.photo_id;
  resetPhotoZoom();
  renderPhotoViewer();
}

/* Zooming in on a leaf.
 *
 * Double tap, or double click, toward the thing you want bigger: the
 * picture grows about that point rather than about its middle, because
 * the whole reason to zoom is that something interesting is off to one
 * side. Double again to come back out.
 *
 * While zoomed, a drag pans instead of stepping to the next photograph.
 * The two gestures are the same movement, so one of them has to give
 * way, and panning is the one somebody zoomed in order to do.
 */
const PHOTO_ZOOM = 2.6;
let photoZoom = 1;
let photoPan = {x: 0, y: 0};

function applyPhotoZoom() {
  const image = document.getElementById("photo-viewer-image");
  if (!image) return;
  // Kept inside its own edges: panning past them shows backdrop where
  // the picture should be, which reads as the image having come loose.
  const box = image.getBoundingClientRect();
  const slackX = Math.max(0, (box.width * photoZoom - box.width) / 2);
  const slackY = Math.max(0, (box.height * photoZoom - box.height) / 2);
  photoPan = {
    x: Math.max(-slackX, Math.min(slackX, photoPan.x)),
    y: Math.max(-slackY, Math.min(slackY, photoPan.y)),
  };
  image.style.transform = photoZoom === 1
    ? ""
    : `translate(${photoPan.x}px, ${photoPan.y}px) scale(${photoZoom})`;
  setData(image, "zoomed", photoZoom === 1 ? "" : "true");
  if (photoZoom === 1) delete image.dataset.zoomed;
}

function resetPhotoZoom() {
  photoZoom = 1;
  photoPan = {x: 0, y: 0};
  applyPhotoZoom();
}

function togglePhotoZoom(clientX, clientY) {
  const image = document.getElementById("photo-viewer-image");
  if (!image) return;
  if (photoZoom !== 1) { resetPhotoZoom(); return; }
  /* Shift the point under the finger toward the middle, by the amount
     the extra scale would otherwise carry it outward. */
  const box = image.getBoundingClientRect();
  const fromMiddleX = clientX - (box.left + box.width / 2);
  const fromMiddleY = clientY - (box.top + box.height / 2);
  photoZoom = PHOTO_ZOOM;
  photoPan = {x: -fromMiddleX * (PHOTO_ZOOM - 1) / PHOTO_ZOOM * 1.0,
              y: -fromMiddleY * (PHOTO_ZOOM - 1) / PHOTO_ZOOM * 1.0};
  applyPhotoZoom();
}

// "3 days", "2 months" -- the gap between two photographs, said roughly.
function describeSpan(milliseconds) {
  const days = Math.max(0, Math.round(milliseconds / 86400000));
  if (days < 1) return "hours";
  if (days < 45) return `${days} day${days === 1 ? "" : "s"}`;
  const months = Math.round(days / 30.44);
  if (months < 24) return `${months} months`;
  return `${(days / 365.25).toFixed(1)} years`;
}

function renderPlantPhotos() {
  const grid = document.getElementById("plant-photo-grid");
  if (grid === null) return;
  setClass(grid, "is-collapsed", !photosExpanded);
  const more = document.getElementById("plant-photo-more");
  if (more) {
    setHidden(more, plantPhotos.length < 2);
    setText(more, photosExpanded ? "Show fewer"
      : `Show all ${plantPhotos.length} photographs`);
  }
  const follows = document.getElementById("plant-cover-follows");
  if (follows !== null && follows.checked !== coverFollowsLatest) {
    follows.checked = coverFollowsLatest;
  }
  if (!renderKeyChanged(grid, plantPhotos)) return;
  grid.replaceChildren();
  if (!plantPhotos.length) {
    const empty = document.createElement("p");
    empty.className = "care-log-empty";
    setText(empty, "No photographs yet.");
    grid.append(empty);
    return;
  }
  for (const photo of plantPhotos) {
    const tile = document.createElement("figure");
    tile.className = "photo-tile";
    if (photo.cover) tile.dataset.cover = "true";

    // When the camera says it was taken, or when it arrived if it said
    // nothing. Declared here because the picture's own description uses it.
    const taken = photo.taken_at || photo.added_at;

    /* Still a real link to the file, so the browser's own open-in-new-tab,
       save and copy-address all work. The viewer takes the plain click,
       which is the one that used to lose the page. */
    const link = document.createElement("a");
    link.href = photoUrl(photo.photo_id, photo.digest);
    link.target = "_blank";
    setData(link, "photoId", photo.photo_id);
    link.addEventListener("click", (event) => {
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
      event.preventDefault();
      openPhotoViewer(photo.photo_id);
    });
    link.rel = "noopener";
    const image = document.createElement("img");
    image.src = photoUrl(photo.photo_id, photo.digest);
    image.loading = "lazy";
    image.alt = `${selectedPlant?.display_name || "This plant"}, ${daysSince(taken)}`;
    link.append(image);

    const caption = document.createElement("figcaption");
    /* The date is the control that changes it. Most of these pictures will
     * outlast anybody's memory of when they were taken, and the ones whose
     * metadata was stripped before they arrived have nothing else to go on. */
    const when = document.createElement("input");
    when.type = "date";
    when.className = "photo-date";
    when.value = localDate(new Date(taken));
    when.max = localDate(new Date());
    setAttr(when, "aria-label", "Date this photograph was taken");
    setAttr(when, "title", photo.taken_at
      ? "Taken " + new Date(photo.taken_at).toLocaleString()
      : "The camera gave no date; this is when it was added. Set it if you know.");
    if (!photo.taken_at) setData(when, "undated", "true");
    when.addEventListener("change", () => setPhotoDate(photo.photo_id, when.value));
    const actions = document.createElement("div");
    actions.className = "photo-actions";
    if (photo.cover) {
      /* The same star, filled: the outline asks, the solid one answers.
         Which kind of cover it is -- chosen, or simply the newest and due
         to be replaced by the next photograph -- is in the label, because
         a filled star cannot say it and the difference matters when the
         next picture quietly takes over. */
      const mark = iconGlyph(
        coverFollowsLatest
          ? "Cover, because it is the newest photograph"
          : "Cover, chosen by hand",
        COVER_ICON, { filled: true });
      actions.append(mark);
    } else {
      const choose = iconButton("Make cover", COVER_ICON);
      choose.addEventListener("click", () => setPlantCover(photo.photo_id));
      actions.append(choose);
    }
    const remove = iconButton("Remove this photograph", BIN_ICON, { danger: true });
    remove.addEventListener("click", () => removePlantPhoto(photo));
    actions.append(remove);
    caption.append(when, actions);

    tile.append(link, caption);
    grid.append(tile);
  }
}

/* A button that is a picture.
 *
 * Only for actions whose icon is already understood -- a star for the
 * one on the cover, a bin for throwing away -- and never for anything a
 * person has to guess at. The word is still there for anybody who needs
 * it, as the label a screen reader reads and the tooltip a pointer
 * reveals; what goes is the width it took beside every photograph. */
function iconButton(label, paths, { danger = false } = {}) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "icon-button";
  if (danger) setData(button, "danger", "true");
  setAttr(button, "aria-label", label);
  setAttr(button, "title", label);
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("aria-hidden", "true");
  for (const definition of paths) {
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", definition);
    path.setAttribute("stroke", "currentColor");
    path.setAttribute("stroke-width", "1.7");
    path.setAttribute("stroke-linecap", "round");
    path.setAttribute("stroke-linejoin", "round");
    svg.append(path);
  }
  button.append(svg);
  return button;
}

/* The same picture as an icon button, standing for a state rather than an
   action. Not a button: there is nothing to press, and a control that
   does nothing when clicked is worse than a label. */
function iconGlyph(label, paths, { filled = false } = {}) {
  const span = document.createElement("span");
  span.className = "icon-glyph";
  if (filled) setData(span, "filled", "true");
  setAttr(span, "role", "img");
  setAttr(span, "aria-label", label);
  setAttr(span, "title", label);
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", filled ? "currentColor" : "none");
  svg.setAttribute("aria-hidden", "true");
  for (const definition of paths) {
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", definition);
    path.setAttribute("stroke", "currentColor");
    path.setAttribute("stroke-width", "1.7");
    path.setAttribute("stroke-linejoin", "round");
    svg.append(path);
  }
  span.append(svg);
  return span;
}

const COVER_ICON = ["M12 3.8l2.5 5.1 5.6.8-4.05 3.95.95 5.55L12 16.6l-5.05 2.6.95-5.55L3.85 9.7l5.6-.8z"];
const BIN_ICON = ["M4.5 6.8h15", "M10 6.8V4.6h4v2.2", "M6.6 6.8l.9 12.6h9l.9-12.6",
                  "M10.2 10.4v5.6", "M13.8 10.4v5.6"];

async function setPhotoDate(photoId, date) {
  const message = document.getElementById("plant-photo-message");
  const [year, month, day] = (date || "").split("-").map(Number);
  const takenAt = date ? new Date(year, month - 1, day, 12, 0, 0).toISOString() : null;
  const response = await fetch(`/api/photos/${photoId}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ taken_at: takenAt }),
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    setText(message, payload.error || "That date could not be saved.");
    return;
  }
  setText(message, "Date saved.");
  await refreshPlantPhotos();
}

/* Follow the newest, or stop following it. Choosing a picture by hand
 * turns this off on the hub's side, so the box simply reports what the
 * plant says rather than arguing with it. */
async function setCoverFollowsLatest(following) {
  const plantId = currentPlantId();
  if (plantId === null) return;
  const response = await fetch(`/api/plants/${plantId}/cover`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ follows_latest: following }),
  });
  if (!response.ok) return;
  await refreshPlantPhotos();
  if (pageFromLocation() === "plant") await refreshPlant();
}

async function setPlantCover(photoId) {
  const plantId = currentPlantId();
  if (plantId === null) return;
  const response = await fetch(`/api/plants/${plantId}/cover`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ photo_id: photoId }),
  });
  if (!response.ok) return;
  await refreshPlantPhotos();
  if (pageFromLocation() === "plant") await refreshPlant();
}

/* Asked before removing, because the file goes with the row and a photograph
 * is the one thing on this page that cannot be measured again. */
async function removePlantPhoto(photo) {
  const taken = new Date(photo.added_at).toLocaleDateString(undefined, { dateStyle: "medium" });
  if (!window.confirm(`Remove the photograph from ${taken}? This cannot be undone.`)) return;
  const response = await fetch(`/api/photos/${photo.photo_id}`, { method: "DELETE" });
  if (!response.ok) return;
  await refreshPlantPhotos();
  if (pageFromLocation() === "plant") await refreshPlant();
}

/* A plant's configuration: what it is called, where it is, and which lights
 * are its own -- the questions a sensor's configuration page asks about the
 * plant it watches, asked where there is no sensor to ask through. */
/* The sensor watching the plant whose page this is, if one is. */
function isScale(sensor) {
  /* A scale is a scale whatever radio it uses.
   *
   * This asked sensorKind(), which answers a different question: which
   * family of page a device belongs to. A Zigbee scale is administered
   * on the Zigbee page, so that function rightly calls it "zigbee" --
   * and the moment it did, this stopped recognising it as a scale.
   *
   * The household grid picks each plant's sensor with !isScale(), so a
   * plant with both a probe and a Zigbee scale started showing the
   * scale's card: "No measurements", from a device that measures
   * weight and was never asked about moisture.
   */
  const transport = sensor?.transport || "";
  return transport === "http-scale" || transport.endsWith("-scale");
}

/* What sort of instrument this is, which decides what its page is made of. */
function sensorKind(sensor) {
  /* Derived, not assumed.
   *
   * This read "anything that is not an http-scale is a bluetooth probe",
   * which was true while there were two kinds and wrong the moment a third
   * arrived: a Zigbee scale was offered pH calibration and moisture
   * thresholds. An unrecognised transport now gets no panels rather than
   * somebody else's. */
  const transport = sensor?.transport || "";
  if (transport === "http-scale") return "scale";
  if (transport === "bthome") return "bthome";
  if (transport === "zigbee-soil") return "soil";
  if (transport.startsWith("zigbee")) return "zigbee";
  return "unknown";
}

const SENSOR_KINDS = {
  zigbee: {
    label: "ZIGBEE DEVICE",
    note: "Administered on the Zigbee page, because what can be done with it "
      + "depends on the dongle being plugged in.",
  },
  unknown: {
    label: "DEVICE",
    note: "The hub does not recognise how this device is reached, so it offers "
      + "nothing rather than guessing at controls it may not have.",
  },
  bthome: {
    label: "BLUETOOTH PROBE",
    note: "A probe in the soil. It is configured over Bluetooth, so changes "
      + "reach it when it next reports, and the hub can send it firmware.",
  },
  scale: {
    label: "WI-FI SCALE",
    note: "A scale under the pot. The hub reaches it at its own address and "
      + "asks it what it weighs; there is nothing to deliver to it and no "
      + "console to switch on, because the page at that address is its console.",
  },
};


function plantSensor() {
  const plantId = plantIdFromLocation();
  return plantId === null
    ? null
    /* A scale is a sensor too, but it is not the plant's probe. A plant can
     * have both -- something in the soil and something under the pot -- and
     * the panels that say "the sensor" mean the one measuring the soil. */
    : fleetSensors.find((sensor) =>
        sensor.plant_id === plantId && !isScale(sensor)) || null;
}

/* The sensors this hub has adopted and pointed at nothing. A sensor it has
 * never met is not offered here: adopting one is the inbox's job, and the
 * two steps are deliberately separate. */
function renderFreeSensors() {
  const select = document.getElementById("plant-sensor-choice");
  if (select === null) return;
  const free = fleetSensors.filter((sensor) => sensor.plant_id == null);
  const renderKey = JSON.stringify(free.map((s) => [s.sensor_id, s.display_name]));
  if (select.dataset.renderKey !== renderKey) {
    setData(select, "renderKey", renderKey);
    select.replaceChildren();
    if (!free.length) {
      const none = document.createElement("option");
      none.value = "";
      setText(none, "No spare sensor — adopt one in Sensor inbox");
      select.append(none);
    }
    for (const sensor of free) {
      const option = document.createElement("option");
      option.value = sensor.sensor_id;
      setText(option, sensor.display_name
        ? `${sensor.display_name} · ${sensor.sensor_id}` : sensor.sensor_id);
      select.append(option);
    }
  }
  setDisabled(select, free.length === 0);
  setDisabled(document.getElementById("plant-sensor-attach-save"), free.length === 0);
}

async function pointSensorAtPlant(sensorId, plantId) {
  const message = document.getElementById("plant-sensor-message");
  const response = await fetch(`/api/sensors/${encodeURIComponent(sensorId)}/plant`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ plant_id: plantId }),
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    setText(message, payload.error || "That could not be done.");
    return;
  }
  setText(message, "");
  await refreshFleet();
  await refreshPlantConfig();
  renderPage();
}

async function refreshPlantConfig() {
  const plantId = plantIdFromLocation();
  if (plantId === null) return;
  if (plantConfigDraftPlantId !== null && plantConfigDraftPlantId !== plantId) {
    plantConfigDraftPlantId = null;
  }
  const [plantResponse] = await Promise.all([
    fetch(`/api/plants/${plantId}`),
    refreshPlantLighting(),
  ]);
  if (!plantResponse.ok) {
    setText(document.getElementById("plant-config-name"), "Plant not found");
    return;
  }
  selectedPlant = await plantResponse.json();
  renderPlantConfig();
}

/* A plant may be in no room at all, which a sensor may not be: it is a thing
 * somebody owns before it is a thing the hub is watching. */
function fillPlantRoomSelect(select, roomId) {
  const renderKey = JSON.stringify(rooms.map((room) => [room.room_id, room.name]));
  if (select.dataset.renderKey !== renderKey) {
    setData(select, "renderKey", renderKey);
    select.replaceChildren();
    const none = document.createElement("option");
    none.value = "";
    setText(none, rooms.length ? "No room recorded" : "No rooms yet · add one in Settings");
    select.append(none);
    rooms.forEach((room) => {
      const option = document.createElement("option");
      option.value = room.room_id;
      setText(option, room.name);
      select.append(option);
    });
  }
  select.value = roomId == null ? "" : String(roomId);
}

/* A plant may have no profile, the way it may have no room: the hub is told
 * what a plant is when somebody knows, not before. */
function fillPlantProfileSelect(select, profileId) {
  const entries = Object.entries(profiles?.profiles || {})
    .sort((a, b) => a[1].name.localeCompare(b[1].name));
  const renderKey = JSON.stringify(entries.map(([id, profile]) => [id, profile.name]));
  if (select.dataset.renderKey !== renderKey) {
    setData(select, "renderKey", renderKey);
    select.replaceChildren();
    const none = document.createElement("option");
    none.value = "";
    setText(none, "Not recorded");
    select.append(none);
    for (const [id, profile] of entries) {
      const option = document.createElement("option");
      option.value = id;
      setText(option, profile.scientific_name && profile.scientific_name !== "Starter profile"
        ? `${profile.name} · ${profile.scientific_name}`
        : profile.name);
      select.append(option);
    }
  }
  select.value = profileId ?? "";
}

/* The varieties of whichever profile is chosen in the form, not of whichever
 * is saved: picking Pothos should offer Marble Queen before anything is
 * saved. A variety belongs to its profile, so changing the profile empties
 * the list rather than leaving a name that refers to nothing. */
function fillPlantVarietySelect(variety) {
  const select = document.getElementById("plant-config-variety");
  const note = document.getElementById("plant-config-variety-note");
  const profileId = document.getElementById("plant-config-profile").value;
  const varieties = profiles?.profiles?.[profileId]?.varieties || [];
  const renderKey = JSON.stringify([profileId, varieties.map((kind) => kind.id)]);
  if (select.dataset.renderKey !== renderKey) {
    setData(select, "renderKey", renderKey);
    select.replaceChildren();
    const none = document.createElement("option");
    none.value = "";
    setText(none, varieties.length
      ? "Not recorded"
      : profileId ? "This profile lists no varieties" : "Choose a profile first");
    select.append(none);
    for (const kind of varieties) {
      const option = document.createElement("option");
      option.value = kind.id;
      setText(option, kind.name);
      select.append(option);
    }
  }
  setDisabled(select, varieties.length === 0);
  if (variety !== undefined) select.value = variety ?? "";
  const chosen = varieties.find((kind) => kind.id === select.value);
  setText(note, chosen?.note
    || "The named plant within that profile. It changes how fast it grows and "
       + "how much light it wants, not what the roots will take.");
}

/* Which plant's form somebody has started filling in.
 *
 * The page refreshes every second, and a form refilled underneath whoever is
 * using it loses what they were doing. Asking document.activeElement is not
 * enough: on a phone a date opens a sheet of its own and the field is no
 * longer the focused element, so a poll a second later would put the old
 * date back while the wheel was still being turned. So the form is left
 * alone from the first keystroke until it is saved, or until another plant's
 * page is opened. */
let plantConfigDraftPlantId = null;

function setValue(element, value) {
  // Assigning a date input the value it already holds still throws away a
  // part-entered date, so nothing is written unless it changed.
  const text = value == null ? "" : String(value);
  if (element.value !== text) element.value = text;
}

function renderPlantConfig() {
  if (!selectedPlant) return;
  setText(document.getElementById("plant-config-name"), selectedPlant.display_name);
  // The two pages share this button, so each sets it only on its own page.
  if (pageFromLocation() === "plant-config") {
    const back = document.getElementById("plant-config-back");
    setAttr(back, "href", `/plants/${selectedPlant.plant_id}`);
    setText(back, "Back to plant");
  }

  if (plantConfigDraftPlantId === selectedPlant.plant_id) return;
  setValue(document.getElementById("plant-config-display-name"), selectedPlant.display_name);
  fillPlantRoomSelect(document.getElementById("plant-config-room"), selectedPlant.room_id);
  setValue(document.getElementById("plant-config-watering-days"),
    selectedPlant.expected_watering_days ?? "");
  const started = document.getElementById("plant-config-started");
  setAttr(started, "max", localDate(new Date()));
  setValue(started, selectedPlant.started_at
    ? localDate(new Date(selectedPlant.started_at)) : "");
  const months = plantAgeMonths();
  setText(document.getElementById("plant-config-age-note"),
    months == null ? "" : `\u00b7 ${describePlantAge(months)}, read as ${plantStage()}`);
  setValue(document.getElementById("plant-config-ppfd"),
    selectedPlant.ppfd_umol_m2s ?? "");
  setValue(document.getElementById("plant-config-light-hours"),
    selectedPlant.light_hours_per_day ?? "");
  const acquired = document.getElementById("plant-config-acquired");
  setAttr(acquired, "max", localDate(new Date()));
  setValue(acquired, selectedPlant.acquired_at
    ? localDate(new Date(selectedPlant.acquired_at)) : "");
  setValue(document.getElementById("plant-config-purchase-place"),
    selectedPlant.purchase_place ?? "");
  // Cents in the database, whole currency in the field: nobody types a price
  // in cents, and nobody should have to read one back that way either.
  setValue(document.getElementById("plant-config-purchase-price"),
    selectedPlant.purchase_price_cents == null
      ? "" : (selectedPlant.purchase_price_cents / 100).toFixed(2));
  fillPlantProfileSelect(document.getElementById("plant-config-profile"), selectedPlant.profile_id);
  fillPlantVarietySelect(selectedPlant.variety);
}

async function savePlantConfig(event) {
  event.preventDefault();
  if (!selectedPlant) return;
  const message = document.getElementById("plant-config-message");
  const days = document.getElementById("plant-config-watering-days").value;
  // Blank means nobody has measured it, which is not zero.
  const light = document.getElementById("plant-config-ppfd").value;
  const lightHours = document.getElementById("plant-config-light-hours").value;
  const startedOn = document.getElementById("plant-config-started").value;
  const room = document.getElementById("plant-config-room").value;
  const response = await fetch(`/api/plants/${selectedPlant.plant_id}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      display_name: document.getElementById("plant-config-display-name").value,
      room_id: room === "" ? null : Number(room),
      expected_watering_days: days === "" ? null : Number(days),
      ppfd_umol_m2s: light === "" ? null : Number(light),
      light_hours_per_day: lightHours === "" ? null : Number(lightHours),
      // Midday local, like acquired_at, so no time zone moves the day.
      started_at: (() => {
        if (!startedOn) return null;
        const [y, m, d] = startedOn.split("-").map(Number);
        return new Date(y, m - 1, d, 12, 0, 0).toISOString();
      })(),
      profile_id: document.getElementById("plant-config-profile").value || null,
      variety: document.getElementById("plant-config-variety").value || null,
      // A date with no time of day is filed at midday local time, far enough
      // from either midnight that no time zone moves it to the day before.
      acquired_at: (() => {
        const chosen = document.getElementById("plant-config-acquired").value;
        if (!chosen) return null;
        const [year, month, day] = chosen.split("-").map(Number);
        return new Date(year, month - 1, day, 12, 0, 0).toISOString();
      })(),
      purchase_place: document.getElementById("plant-config-purchase-place").value || null,
      // Rounded here rather than left to the database, so a price typed as
      // 12.99 is 1299 cents and not 1298.9999999999998 truncated to 1298.
      purchase_price_cents: (() => {
        const typed = document.getElementById("plant-config-purchase-price").value;
        if (typed === "") return null;
        const amount = Number(typed);
        return Number.isFinite(amount) ? Math.round(amount * 100) : null;
      })(),
    }),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    setText(message, payload.error || "Could not save this plant.");
    return;
  }
  selectedPlant = payload;
  setText(message, "Saved.");
  // Saved, so the form is the hub's to refill again.
  plantConfigDraftPlantId = null;
  renderPlantConfig();
}

/* The day the pot was on the scale, which is today unless somebody says
 * otherwise: a number written down on Tuesday is still Tuesday's weighing.
 *
 * A date carries no time, so a backdated weighing is filed at midday local
 * time -- far enough from either midnight that no time zone files it under
 * the wrong day. Today is sent as the moment it actually is. */
function weighingTime() {
  const chosen = document.getElementById("plant-weight-date").value;
  const today = localDate(new Date());
  if (!chosen || chosen === today) return new Date().toISOString();
  if (chosen > today) return null;
  const [year, month, day] = chosen.split("-").map(Number);
  return new Date(year, month - 1, day, 12, 0, 0).toISOString();
}

function localDate(when) {
  return [
    when.getFullYear(),
    String(when.getMonth() + 1).padStart(2, "0"),
    String(when.getDate()).padStart(2, "0"),
  ].join("-");
}

function resetWeighingDate() {
  const field = document.getElementById("plant-weight-date");
  const today = localDate(new Date());
  field.max = today;
  field.value = today;
}

/* What the plant has been drinking.
 *
 * Each bar spans the time between two weighings and stands at the average
 * rate over that span. Drawn as spans rather than as a line of daily points
 * because the daily points do not exist: a pot weighed on Monday and Friday
 * knows the week's total and nothing about Wednesday. The gaps between bars
 * are honest -- they are the stretches nobody measured, or the ones with a
 * watering in them. */
/* What the change in rate suggests, shown where the rate is. It is also
 * written into the care log, so a plant that was off its water in March can
 * be found again in September. */
function renderWaterUseFindings(findings) {
  const list = document.getElementById("water-use-findings");
  if (list === null) return;
  if (!renderKeyChanged(list, findings)) return;
  list.replaceChildren();
  for (const finding of findings) {
    const note = document.createElement("div");
    note.className = "water-use-finding";
    setData(note, "code", finding.code);
    const title = document.createElement("strong");
    setText(title, finding.title);
    const summary = document.createElement("p");
    setText(summary, finding.summary);
    note.append(title, summary);
    list.append(note);
  }
}

/* When this plant will want water.
 *
 * Where it is now, where thirsty is, and how fast it is getting from one to
 * the other. The chart carries the weighings it was worked out from and the
 * line down to the thirsty weight, with a band between the fastest and
 * slowest the plant has drunk: a single line would claim a precision that
 * four weighings on a kitchen scale do not have.
 *
 * Where a piece is missing the hub says which, and what to do about it,
 * rather than producing a number from a guess. */
/* Arranging the sections of a plant's page.
 *
 * One order for the whole household, kept on the hub: somebody who wants
 * the next watering at the top wants it at the top of every plant, and from
 * the phone as well as the desk.
 *
 * Arranging happens in a mode of its own, with each section shrunk to a
 * tile bearing its name. A page of full-height panels cannot be dragged
 * into order -- the thing being moved and the place it is going are never
 * on screen together. */
let plantSectionOrder = [];
let arranging = false;
let orderBeforeArranging = null;

function orderedSections() {
  // Two sections can carry one name -- the care log and the journey exist
  // once per page -- and both move together, which is what somebody
  // arranging them means.
  return [...document.querySelectorAll("main [data-section]")];
}

function applySectionOrder() {
  const sections = orderedSections();
  if (!sections.length || !plantSectionOrder.length) return;
  const anchor = sections[0];
  const marker = document.createComment("sections");
  anchor.parentNode.insertBefore(marker, anchor);
  for (const name of plantSectionOrder) {
    for (const section of sections) {
      if (section.dataset.section === name) marker.parentNode.insertBefore(section, marker);
    }
  }
  // Anything the saved order has not heard of keeps its place at the end.
  for (const section of sections) {
    if (!plantSectionOrder.includes(section.dataset.section)) {
      marker.parentNode.insertBefore(section, marker);
    }
  }
  marker.remove();
}

/* Each tile needs a name to show once its contents are hidden, taken from
 * the heading the section already has rather than a second list of titles
 * to keep in step with the first. */
function labelSections() {
  for (const section of orderedSections()) {
    if (section.dataset.sectionTitle) continue;
    const heading = section.querySelector("h2, h3");
    const labelled = section.getAttribute("aria-label");
    setData(section, "sectionTitle",
      (heading?.textContent || labelled || section.dataset.section).trim());
  }
}

function startArranging() {
  orderBeforeArranging = [...plantSectionOrder];
  arranging = true;
  renderArranging();
}

function stopArranging(restore) {
  if (restore && orderBeforeArranging) {
    plantSectionOrder = orderBeforeArranging;
    applySectionOrder();
  }
  arranging = false;
  orderBeforeArranging = null;
  renderArranging();
}

function renderArranging() {
  labelSections();
  setClass(document.querySelector("main"), "arranging", arranging);
  setHidden(document.getElementById("arrange-bar"), !arranging);
  // One control, once per header: a plant with a sensor and one without
  // are the same page arranged the same way.
  for (const button of document.querySelectorAll(".arrange-sections")) {
    setText(button, arranging ? "Done" : "Arrange");
  }
  for (const section of orderedSections()) {
    setAttr(section, "draggable", String(arranging));
    if (arranging) {
      setAttr(section, "tabindex", "0");
      setAttr(section, "role", "button");
      setAttr(section, "aria-label",
        `${section.dataset.sectionTitle}. Drag to move, or use the arrow keys.`);
    } else {
      section.removeAttribute("tabindex");
      section.removeAttribute("role");
      section.removeAttribute("aria-label");
    }
  }
}

/* Dragging with pointer events rather than the drag-and-drop API, which
 * does not exist on a phone.
 *
 * The tile is lifted out of the column and follows the finger, and a
 * dashed gap of its own size stands where it would land. Taking it out of
 * the flow is what makes the rest of this honest: left in place it shifts
 * every tile below it as it moves, the pointer lands somewhere else than
 * it did a frame ago, and the next move undoes the last.
 */
function beginSectionDrag(event) {
  if (!arranging) return;
  const moving = event.target.closest("[data-section]");
  if (moving === null) return;
  event.preventDefault();
  moving.setPointerCapture(event.pointerId);

  const box = moving.getBoundingClientRect();
  const grabbedAt = { x: event.clientX - box.left, y: event.clientY - box.top };
  const gap = document.createElement("div");
  gap.className = "section-gap";
  gap.style.height = `${box.height}px`;
  moving.parentNode.insertBefore(gap, moving);

  setClass(moving, "dragging", true);
  moving.style.position = "fixed";
  moving.style.width = `${box.width}px`;
  moving.style.margin = "0";
  moving.style.pointerEvents = "none";
  moving.style.zIndex = "30";
  const lift = (pointer) => {
    moving.style.left = `${pointer.clientX - grabbedAt.x}px`;
    moving.style.top = `${pointer.clientY - grabbedAt.y}px`;
  };
  lift(event);

  const move = (pointer) => {
    lift(pointer);
    // Live positions are trustworthy now: the tile being dragged is out of
    // the column, and the gap standing in for it is exactly its size.
    const others = orderedSections()
      .filter((node) => node !== moving && !node.hasAttribute("hidden"));
    const above = others.find((node) => {
      const rect = node.getBoundingClientRect();
      return pointer.clientY < rect.top + rect.height / 2;
    });
    if (above) {
      if (gap.nextSibling !== above) above.parentNode.insertBefore(gap, above);
    } else if (others.length) {
      const last = others[others.length - 1];
      if (last.nextSibling !== gap) last.after(gap);
    }
  };
  const end = () => {
    moving.removeEventListener("pointermove", move);
    moving.removeEventListener("pointerup", end);
    moving.removeEventListener("pointercancel", end);
    setClass(moving, "dragging", false);
    for (const property of ["position", "width", "margin", "pointerEvents",
                            "zIndex", "left", "top"]) {
      moving.style[property] = "";
    }
    gap.parentNode.insertBefore(moving, gap);
    gap.remove();
    readSectionOrder();
  };
  moving.addEventListener("pointermove", move);
  moving.addEventListener("pointerup", end);
  moving.addEventListener("pointercancel", end);
}

function nudgeSection(event) {
  if (!arranging) return;
  const section = event.target.closest("[data-section]");
  if (section === null) return;
  const up = event.key === "ArrowUp";
  const down = event.key === "ArrowDown";
  if (!up && !down) return;
  event.preventDefault();
  const shown = orderedSections().filter((node) => !node.hasAttribute("hidden"));
  const at = shown.indexOf(section);
  const swap = shown[at + (up ? -1 : 1)];
  if (!swap) return;
  swap.parentNode.insertBefore(up ? section : swap, up ? swap : section);
  section.focus();
  readSectionOrder();
}

function readSectionOrder() {
  const seen = [];
  for (const section of orderedSections()) {
    if (!seen.includes(section.dataset.section)) seen.push(section.dataset.section);
  }
  plantSectionOrder = seen;
}

async function saveSectionOrder() {
  const message = document.getElementById("arrange-message");
  const response = await fetch("/api/settings/sections", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ order: plantSectionOrder }),
  });
  if (!response.ok) {
    setText(message, "That order could not be saved.");
    return;
  }
  plantSectionOrder = (await response.json()).plant_section_order;
  setText(message, "");
  stopArranging(false);
}

async function loadSectionOrder() {
  const response = await fetch("/api/settings");
  if (!response.ok) return;
  const settings = await response.json();
  const saved = settings.plant_section_order;
  if (Array.isArray(saved) && saved.join() !== plantSectionOrder.join()) {
    plantSectionOrder = saved;
    applySectionOrder();
  }
  if (Array.isArray(settings.care_log_hidden_kinds)) {
    careLogHiddenKinds = settings.care_log_hidden_kinds;
  }
}

function renderForecast() {
  const section = document.getElementById("forecast-known");
  if (section === null) return;
  const forecast = selectedPlant?.forecast;
  const missing = document.getElementById("forecast-missing");
  const headline = document.getElementById("forecast-headline");
  if (!forecast) {
    setHidden(section, true);
    setHidden(missing, true);
    setText(headline, "");
    return;
  }

  // Said whether or not there is a forecast: a thing worth knowing about
  // the figures is also worth knowing about their absence.
  renderForecastList("forecast-cautions", forecast.cautions || []);
  setHidden(section, !forecast.ready);
  setHidden(missing, forecast.ready);
  if (!forecast.ready) {
    setText(headline, "Not enough to go on yet");
    renderForecastMissing(forecast.missing);
    return;
  }
  renderForecastMissing([]);

  if (forecast.thirsty_now) {
    setText(headline, "It is at its thirsty weight now");
  } else {
    const when = new Date(forecast.water_on);
    setText(headline, `Around ${when.toLocaleDateString(undefined, { dateStyle: "medium" })}`);
  }
  setText(document.getElementById("forecast-days"),
    forecast.thirsty_now ? "now" : String(forecast.days));
  setText(document.getElementById("forecast-window"),
    forecast.thirsty_now ? "--" : `${forecast.days_soonest}–${forecast.days_latest}`);
  setText(document.getElementById("forecast-to-go"), String(forecast.grams_to_go));
  setText(document.getElementById("forecast-rate"), String(forecast.grams_per_day));

  /* Said in a sentence as well as in numbers: the figures are the working,
   * and the sentence is the answer somebody came for. */
  setText(document.getElementById("forecast-reason"), forecast.thirsty_now
    ? `The pot is at ${showWeight(forecast.grams_now)} g, which is the `
      + "weight you set as thirsty for this plant. Water it when you are ready, "
      + "and weigh it again afterwards to start the next cycle."
    : `The pot weighs ${showWeight(forecast.grams_now)} g and asks for `
      + `water at ${showWeight(forecast.thirsty_grams)} g, so it has `
      + `${forecast.grams_to_go} ml of its water left. It has been using `
      + `${forecast.grams_per_day} ml a day over ${forecast.spans_used} `
      + `${forecast.spans_used === 1 ? "span" : "spans"} of ${forecast.rate_from}, `
      + `which puts the next watering about ${forecast.days} days out. It has drunk `
      + `as fast as ${forecast.days_soonest} days' worth and as slowly as `
      + `${forecast.days_latest}, so that is the range rather than the day.`);

  renderForecastChart(forecast);
}

function renderForecastMissing(missing) {
  renderForecastList("forecast-missing-list", missing);
}

function renderForecastList(listId, missing) {
  const list = document.getElementById(listId);
  if (list === null || !renderKeyChanged(list, missing)) return;
  list.replaceChildren();
  for (const gap of missing) {
    const item = document.createElement("li");
    const what = document.createElement("strong");
    setText(what, gap.what);
    const how = document.createElement("span");
    setText(how, gap.how);
    item.append(what, how);
    list.append(item);
  }
}

function renderForecastChart(forecast) {
  const chart = document.getElementById("forecast-chart");
  if (chart === null) return;
  const weighings = (selectedPlant?.weights || [])
    .filter((weighing) => weighing.observed)
    .map((weighing) => ({ at: Date.parse(weighing.at), grams: weighing.grams }))
    .filter((point) => Number.isFinite(point.at))
    .sort((a, b) => a.at - b.at)
    .slice(-12);
  if (!renderKeyChanged(chart, [forecast, weighings])) return;
  chart.replaceChildren();

  const width = Math.max(320, Math.round(chart.getBoundingClientRect().width) || 640);
  const height = 190;
  setAttr(chart, "viewBox", `0 0 ${width} ${height}`);

  const now = Date.now();
  const latest = { at: now, grams: forecast.grams_now };
  const soonest = now + (forecast.days_soonest || 0) * 86400000;
  const latestDay = now + (forecast.days_latest || 0) * 86400000;
  const expected = now + (forecast.days || 0) * 86400000;
  const startTime = weighings.length ? Math.min(weighings[0].at, now) : now - 86400000;
  const endTime = Math.max(latestDay, now + 86400000);

  const values = weighings.map((p) => p.grams)
    .concat([forecast.grams_now, forecast.thirsty_grams]);
  const padding = Math.max(30, (Math.max(...values) - Math.min(...values)) * 0.2);
  const top = Math.max(...values) + padding;
  const bottom = Math.min(...values) - padding;

  const plot = { left: 46, right: 10, top: 10, bottom: 22 };
  const plotWidth = width - plot.left - plot.right;
  const plotHeight = height - plot.top - plot.bottom;
  const x = (time) =>
    plot.left + ((time - startTime) / Math.max(1, endTime - startTime)) * plotWidth;
  const y = (grams) => plot.top + ((top - grams) / Math.max(1, top - bottom)) * plotHeight;

  // The cone between the fastest and slowest it has drunk. Everything inside
  // it is a day this plant might ask for water.
  chart.append(svgElement("polygon", {
    class: "forecast-cone",
    points: [`${x(now)},${y(forecast.grams_now)}`,
             `${x(soonest)},${y(forecast.thirsty_grams)}`,
             `${x(latestDay)},${y(forecast.thirsty_grams)}`].join(" "),
  }));

  chart.append(
    svgElement("line", {
      class: "trend-refill", x1: plot.left, x2: width - plot.right,
      y1: y(forecast.thirsty_grams), y2: y(forecast.thirsty_grams),
    }),
    svgElement("text", {
      class: "trend-refill-label", x: plot.left + 4, y: y(forecast.thirsty_grams) - 4,
      "text-anchor": "start",
    }, `Thirsty at ${showWeight(forecast.thirsty_grams)} g`)
  );

  for (const grams of [bottom, (bottom + top) / 2, top]) {
    chart.append(
      svgElement("line", {
        class: "grid-line", x1: plot.left, x2: width - plot.right, y1: y(grams), y2: y(grams),
      }),
      svgElement("text", {
        class: "axis-label", x: plot.left - 6, y: y(grams) + 4, "text-anchor": "end",
      }, showWeight(Math.round(grams)))
    );
  }

  // What has happened, solid; what is expected, dashed. The eye should be
  // able to tell a measurement from a projection without reading a legend.
  if (weighings.length > 1) {
    chart.append(svgElement("polyline", {
      class: "history-line",
      points: weighings.map((p) => `${x(p.at)},${y(p.grams)}`).join(" "),
    }));
  }
  chart.append(svgElement("line", {
    class: "forecast-line",
    x1: x(latest.at), y1: y(latest.grams),
    x2: x(expected), y2: y(forecast.thirsty_grams),
  }));
  chart.append(svgElement("circle", {
    class: "history-point latest-point", cx: x(now), cy: y(forecast.grams_now), r: 3.5,
  }));

  const dayLabel = (time) => new Intl.DateTimeFormat(undefined, {
    month: "short", day: "numeric" }).format(new Date(time));
  chart.append(
    svgElement("text", {
      class: "axis-label", x: x(startTime), y: height - 5, "text-anchor": "start",
    }, dayLabel(startTime)),
    svgElement("text", {
      class: "axis-label", x: Math.min(x(expected), width - plot.right), y: height - 5,
      "text-anchor": "end",
    }, dayLabel(expected))
  );
}

function renderWaterUse() {
  const use = selectedPlant?.water_use;
  const chart = document.getElementById("water-use-chart");
  if (chart === null) return;
  const empty = document.getElementById("water-use-empty");
  const intervals = use?.intervals || [];

  setText(document.getElementById("water-use-daily"),
    use?.daily_grams == null ? "--" : String(use.daily_grams));
  setText(document.getElementById("water-use-range"),
    intervals.length ? `${use.lowest_daily_grams}–${use.highest_daily_grams}` : "--");
  setText(document.getElementById("water-use-total"),
    intervals.length ? String(use.used_grams) : "--");
  setText(document.getElementById("water-use-latest"),
    intervals.length ? String(intervals[intervals.length - 1].grams_per_day) : "--");
  /* Said rather than hidden: a mean over four measured days out of ninety is
   * a different claim from one over eighty. */
  const latestDrying = [...intervals].reverse().find((i) => i.drying_kpa != null);
  setText(document.getElementById("water-use-coverage"), intervals.length
    ? `Measured across ${use.measured_days} of the last ${use.window_days} days`
      + (latestDrying ? ` · room air ${latestDrying.drying_kpa} kPa` : "")
    : "");

  setHidden(chart, intervals.length === 0);
  setHidden(empty, intervals.length !== 0);
  renderWaterUseFindings(use?.findings || []);
  if (!intervals.length) {
    setText(document.getElementById("water-use-summary"), "");
    chart.replaceChildren();
    return;
  }

  const width = Math.max(320, Math.round(chart.getBoundingClientRect().width) || 640);
  const height = 170;
  if (!renderKeyChanged(chart, [intervals, width])) return;
  chart.replaceChildren();
  setAttr(chart, "viewBox", `0 0 ${width} ${height}`);

  const starts = intervals.map((i) => Date.parse(i.from));
  const ends = intervals.map((i) => Date.parse(i.to));
  const startTime = Math.min(...starts);
  const endTime = Math.max(...ends);
  const top = Math.max(...intervals.map((i) => i.grams_per_day)) * 1.15 || 1;

  const plot = { left: 44, right: 10, top: 10, bottom: 22 };
  const plotWidth = width - plot.left - plot.right;
  const plotHeight = height - plot.top - plot.bottom;
  const x = (time) =>
    plot.left + ((time - startTime) / Math.max(1, endTime - startTime)) * plotWidth;
  const y = (rate) => plot.top + (1 - rate / top) * plotHeight;

  for (const rate of [0, top / 2, top]) {
    chart.append(
      svgElement("line", {
        class: "grid-line", x1: plot.left, x2: width - plot.right, y1: y(rate), y2: y(rate),
      }),
      svgElement("text", {
        class: "axis-label", x: plot.left - 6, y: y(rate) + 4, "text-anchor": "end",
      }, String(Math.round(rate)))
    );
  }

  for (const interval of intervals) {
    const left = x(Date.parse(interval.from));
    const right = x(Date.parse(interval.to));
    const bar = svgElement("rect", {
      class: "water-use-bar",
      x: left + 0.5,
      // A span of a few hours would otherwise be invisible.
      width: Math.max(2, right - left - 1),
      y: y(interval.grams_per_day),
      height: Math.max(1, y(0) - y(interval.grams_per_day)),
    });
    bar.append(svgElement("title", {},
      `${interval.grams} ml over ${interval.days} days · ${interval.grams_per_day} ml a day`));
    chart.append(bar);
  }

  // The average across everything measured, for the bars to be read against.
  if (selectedPlant.water_use.daily_grams != null) {
    const mean = selectedPlant.water_use.daily_grams;
    chart.append(
      svgElement("line", {
        class: "trend-refill", x1: plot.left, x2: width - plot.right, y1: y(mean), y2: y(mean),
      }),
      svgElement("text", {
        class: "trend-refill-label", x: plot.left + 4, y: y(mean) - 4, "text-anchor": "start",
      }, `${mean} ml a day on average`)
    );
  }

  [startTime, endTime].forEach((time, index) => {
    chart.append(svgElement("text", {
      class: "axis-label", x: x(time), y: height - 5,
      "text-anchor": index === 0 ? "start" : "end",
    }, new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" })
      .format(new Date(time))));
  });

  renderWaterUseFindings(use.findings || []);
  const longest = intervals.reduce((a, b) => (b.days > a.days ? b : a));
  setText(document.getElementById("water-use-summary"),
    `${intervals.length} span${intervals.length === 1 ? "" : "s"} measured · `
    + `longest ${longest.days} days`
    + (use.drying_known
      ? " · rates held against the room's drying power before anything is read into them"
      : ""));
}

/* Every weighing, and a way to strike one.
 *
 * A number typed wrong is not a measurement: left in, it drags the drying
 * rate, the forecast and the chart along with it. Folded away by default,
 * because the list is for correcting a mistake rather than for reading. */
function renderWeighingLog() {
  const list = document.getElementById("weighing-log-list");
  if (list === null) return;
  const weighings = selectedPlant?.weights || [];
  setHidden(document.getElementById("weighing-log"), weighings.length === 0);
  setText(document.getElementById("weighing-log-summary"),
    `${weighings.length} weighing${weighings.length === 1 ? "" : "s"} recorded`);
  if (!renderKeyChanged(list, [weighings])) return;
  list.replaceChildren();
  for (const weighing of weighings) {
    const row = document.createElement("div");
    row.className = "weighing-row";
    const when = document.createElement("time");
    when.dateTime = weighing.at;
    setText(when, new Date(weighing.at).toLocaleString([],
      { dateStyle: "medium", timeStyle: "short" }));
    const what = document.createElement("strong");
    setText(what, `${showWeight(weighing.grams)} g`);
    const kind = document.createElement("span");
    // What it was taken as, where that is not just "this is what it weighs",
    // and who took it where that was not a person.
    setText(kind, [
      { watered: "full", thirsty: "thirsty", dry: "dry" }[weighing.kind] || "",
      weighing.observed ? "" : "typed, not weighed",
      weighing.note || "",
    ].filter(Boolean).join(" · "));
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "link-button";
    setText(remove, "Remove");
    remove.addEventListener("click", () => removeWeighing(weighing));
    row.append(when, what, kind, remove);
    list.append(row);
  }
}

async function removeWeighing(weighing) {
  const when = new Date(weighing.at).toLocaleString([],
    { dateStyle: "medium", timeStyle: "short" });
  if (!window.confirm(`Remove the ${showWeight(weighing.grams)} g `
    + `weighing from ${when}? Anything worked out from it is worked out again.`)) return;
  const response = await fetch(`/api/weights/${weighing.weight_id}`, { method: "DELETE" });
  if (!response.ok) {
    setText(document.getElementById("plant-weight-message"),
      "That weighing could not be removed.");
    return;
  }
  // A watering recorded at the same moment is a separate thing that still
  // happened, and stays in the care log for anyone who wants it gone too.
  setText(document.getElementById("plant-weight-message"), "Removed.");
  await refreshPlant();
}

/* The pot's weight over a month, which is this plant's moisture history.
 *
 * The sensored page plots what the probe read; here the pot itself is the
 * instrument. Full and dry are drawn as the band between them, so a glance
 * says where in its cycle the plant is without reading a number. */
const plantWeightTrendMs = 30 * 24 * 60 * 60 * 1000;

function plantWeightPoints() {
  const weights = (selectedPlant && selectedPlant.weights) || [];
  const since = Date.now() - plantWeightTrendMs;
  return weights
    .filter((weighing) => weighing.observed)
    .map((weighing) => ({ timestamp: Date.parse(weighing.at), grams: weighing.grams }))
    .filter((point) => Number.isFinite(point.timestamp) && point.timestamp >= since)
    .sort((a, b) => a.timestamp - b.timestamp);
}

/* A readout that follows the pointer across a chart's dots.
 *
 * Drawn inside the SVG rather than as an HTML overlay so it scales and
 * clips with the chart, and so nothing has to track the chart's position
 * on the page. It is the last thing appended, so it sits above the line.
 *
 * Touch gets it on a tap: there is no hover on a phone, and a number you
 * cannot reach is worse than no number.
 */
function createReadout(chart, size) {
  const readout = svgElement("g", { class: "point-readout", opacity: "0" });
  const bubble = svgElement("rect", { class: "point-readout-bubble", rx: 5, x: 0, y: 0,
                                      width: 10, height: 30 });
  const value = svgElement("text", { class: "point-readout-value", x: 0, y: 0 });
  const when = svgElement("text", { class: "point-readout-when", x: 0, y: 0 });
  readout.append(bubble, value, when);

  const show = (mark) => {
    setText(value, mark.text);
    setText(when, mark.when);
    // Measured after the text is in, because the bubble is sized to it.
    const widest = Math.max(value.getComputedTextLength(), when.getComputedTextLength());
    const boxWidth = widest + 14;  // seven pixels of air each side
    const boxHeight = 34;
    // Flipped to whichever side has room, so a dot near an edge still reads.
    const left = Math.min(Math.max(mark.cx - boxWidth / 2, 2), size.width - boxWidth - 2);
    const topEdge = mark.cy - boxHeight - 10 < 2 ? mark.cy + 12 : mark.cy - boxHeight - 10;
    setAttr(bubble, "x", left);
    setAttr(bubble, "y", topEdge);
    setAttr(bubble, "width", boxWidth);
    setAttr(bubble, "height", boxHeight);
    setAttr(value, "x", left + 7);
    setAttr(value, "y", topEdge + 15);
    setAttr(when, "x", left + 7);
    setAttr(when, "y", topEdge + 28);
    setAttr(readout, "opacity", "1");
  };
  const hide = () => setAttr(readout, "opacity", "0");
  chart.append(readout);
  return { show, hide, readout };
}

/* A dot you can ask what it is, one target per dot.
 *
 * Each dot gets a target ten times its size that draws nothing,
 * because a dot two and a half pixels across cannot be pointed at.
 * Suits a chart with tens of readings; a chart with hundreds uses the
 * nearest-point version below, which needs no targets at all.
 */
function attachPointReadout(chart, marks, size) {
  if (!marks.length) return;
  const { show, hide, readout } = createReadout(chart, size);
  for (const mark of marks) {
    const target = svgElement("circle", {
      class: "point-target", cx: mark.cx, cy: mark.cy, r: 14,
    });
    target.addEventListener("pointerenter", () => show(mark));
    target.addEventListener("pointerdown", () => show(mark));
    target.addEventListener("pointerleave", hide);
    chart.append(target);
  }
  chart.addEventListener("pointerleave", hide);
  chart.append(readout);   // last, so it sits above the targets
}

/* The reading nearest the pointer, anywhere over the chart.
 *
 * A line drawn from hundreds of readings cannot give each one a hit
 * target: that is hundreds of elements rebuilt on every poll, and the
 * dots are too close together to aim at anyway. One transparent sheet
 * over the plot and a search for the nearest reading does the same job
 * for any density, and means the number appears wherever the pointer
 * is rather than only when it finds a dot.
 */
function attachNearestReadout(chart, size, marks) {
  if (!marks.length) return;
  const { show, hide, readout } = createReadout(chart, size);
  const sheet = svgElement("rect", {
    class: "point-sheet", x: 0, y: 0, width: size.width, height: size.height,
  });
  const nearest = (event) => {
    const box = chart.getBoundingClientRect();
    if (!box.width) return;
    // Client pixels into the chart's own coordinates, so this works at
    // any width the page gives it.
    const at = ((event.clientX - box.left) / box.width) * size.width;
    let best = null;
    let bestGap = Infinity;
    for (const mark of marks) {
      const gap = Math.abs(mark.cx - at);
      if (gap < bestGap) {
        bestGap = gap;
        best = mark;
      }
    }
    if (best) show(best);
  };
  sheet.addEventListener("pointermove", nearest);
  sheet.addEventListener("pointerdown", nearest);
  sheet.addEventListener("pointerleave", hide);
  chart.append(sheet);
  chart.append(readout);
}

function renderPlantWeightChart() {
  const chart = document.getElementById("plant-weight-chart");
  const empty = document.getElementById("plant-weight-chart-empty");
  const summary = document.getElementById("plant-weight-chart-summary");
  if (chart === null) return;
  const points = plantWeightPoints();
  const weight = (selectedPlant && selectedPlant.weight) || {};
  setHidden(chart, points.length < 2);
  setHidden(empty, points.length >= 2);
  if (points.length < 2) {
    setText(summary, points.length === 1 ? "One weighing so far." : "");
    chart.replaceChildren();
    return;
  }

  const width = Math.max(320, Math.round(chart.getBoundingClientRect().width) || 640);
  const height = 170;
  if (!renderKeyChanged(chart,
      [points, weight.full, weight.thirsty, weight.dry, width])) return;
  chart.replaceChildren();
  setAttr(chart, "viewBox", `0 0 ${width} ${height}`);

  /* A month of history, or the time there is: a pot weighed for a week should
   * not be a sliver at the right edge. */
  const endTime = points[points.length - 1].timestamp;
  const firstTime = points[0].timestamp;
  const startTime = endTime - firstTime < plantWeightTrendMs
    ? Math.min(firstTime, endTime - 24 * 60 * 60 * 1000)
    : endTime - plantWeightTrendMs;

  /* The scale always holds both anchors, so the band they make is visible
   * even when every weighing sits above or below it. */
  const anchors = [weight.full?.grams, weight.thirsty?.grams, weight.dry?.grams]
    .filter((g) => g != null);
  const values = points.map((point) => point.grams).concat(anchors);
  const lowest = Math.min(...values);
  const highest = Math.max(...values);
  const padding = Math.max(40, (highest - lowest) * 0.15);
  const bottom = lowest - padding;
  const top = highest + padding;

  const plot = { left: 46, right: 10, top: 10, bottom: 22 };
  const plotWidth = width - plot.left - plot.right;
  const plotHeight = height - plot.top - plot.bottom;
  const x = (timestamp) =>
    plot.left + ((timestamp - startTime) / Math.max(1, endTime - startTime)) * plotWidth;
  const y = (grams) => plot.top + ((top - grams) / Math.max(1, top - bottom)) * plotHeight;

  /* The pot's whole range, full at the top and dry at the bottom, coloured by
   * what the weight means rather than by how far down the chart it is. Above
   * thirsty the plant has water it has not asked for, so that stretch is one
   * flat green: 90% full is no better than 70%. Below thirsty it is spending
   * a reserve, and how far in matters, so that stretch runs yellow-green to
   * red as it approaches the driest the pot has ever been. */
  const fullGrams = weight.full?.grams;
  const dryGrams = weight.dry?.grams;
  const thirstyGrams = weight.thirsty?.grams;
  const bandedRange = fullGrams != null && dryGrams != null && fullGrams > dryGrams;
  if (bandedRange) {
    // Thirsty outside the range would paint a band taller than the pot.
    const refill = thirstyGrams != null && thirstyGrams < fullGrams && thirstyGrams > dryGrams
      ? thirstyGrams : null;
    const gradientId = `pot-dry-${currentPlantId()}`;
    const gradient = svgElement("linearGradient", {
      id: gradientId, x1: 0, y1: 0, x2: 0, y2: 1,
    });
    gradient.append(
      svgElement("stop", { offset: "0%", "stop-color": "#cfe08f", "stop-opacity": "0.75" }),
      svgElement("stop", { offset: "55%", "stop-color": "#e8b45a", "stop-opacity": "0.78" }),
      svgElement("stop", { offset: "100%", "stop-color": "#d4503f", "stop-opacity": "0.82" })
    );
    const defs = svgElement("defs", {});
    defs.append(gradient);
    chart.append(defs);

    const comfortableTo = refill ?? dryGrams;
    chart.append(
      svgElement("rect", {
        class: "trend-cycle", x: plot.left, width: plotWidth,
        y: y(fullGrams), height: Math.max(0, y(comfortableTo) - y(fullGrams)),
      }),
      svgElement("line", {
        class: "trend-cycle-edge", x1: plot.left, x2: width - plot.right,
        y1: y(fullGrams), y2: y(fullGrams),
      }),
      svgElement("text", {
        class: "trend-cycle-label", x: plot.left + 4, y: y(fullGrams) + 12,
        "text-anchor": "start",
      }, `Full at ${showWeight(fullGrams)} g`)
    );
    if (refill != null) {
      chart.append(
        svgElement("rect", {
          class: "trend-reserve", x: plot.left, width: plotWidth,
          y: y(refill), height: Math.max(0, y(dryGrams) - y(refill)),
          fill: `url(#${gradientId})`,
        }),
        svgElement("line", {
          class: "trend-refill", x1: plot.left, x2: width - plot.right,
          y1: y(refill), y2: y(refill),
        }),
        svgElement("text", {
          class: "trend-refill-label", x: plot.left + 4, y: y(refill) - 4,
          "text-anchor": "start",
        }, `Thirsty at ${showWeight(refill)} g`)
      );
    }
    chart.append(
      svgElement("line", {
        class: "trend-dry", x1: plot.left, x2: width - plot.right,
        y1: y(dryGrams), y2: y(dryGrams),
      }),
      svgElement("text", {
        class: "trend-dry-label", x: plot.left + 4, y: y(dryGrams) - 4,
        "text-anchor": "start",
      }, weight.dry_stated
        ? `Dry at ${showWeight(dryGrams)} g`
          + (weight.lightest && weight.lightest.grams < dryGrams
            ? ` \u00b7 weighed ${showWeight(weight.lightest.grams)} g once`
            : "")
        : `Driest seen ${showWeight(dryGrams)} g`)
    );
  } else if (weight.full) {
    chart.append(
      svgElement("line", {
        class: "trend-cycle-edge", x1: plot.left, x2: width - plot.right,
        y1: y(weight.full.grams), y2: y(weight.full.grams),
      }),
      svgElement("text", {
        class: "trend-cycle-label", x: plot.left + 4, y: y(weight.full.grams) + 12,
        "text-anchor": "start",
      }, `Full at ${showWeight(weight.full.grams)} g`)
    );
  }

  for (const grams of [bottom, (bottom + top) / 2, top]) {
    chart.append(
      svgElement("line", {
        class: "grid-line", x1: plot.left, x2: width - plot.right, y1: y(grams), y2: y(grams),
      }),
      svgElement("text", {
        class: "axis-label", x: plot.left - 6, y: y(grams) + 4, "text-anchor": "end",
      }, showWeight(Math.round(grams)))
    );
  }
  [startTime, endTime].forEach((timestamp, index) => {
    chart.append(svgElement("text", {
      class: "axis-label", x: x(timestamp), y: height - 5,
      "text-anchor": index === 0 ? "start" : "end",
    }, new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" })
      .format(new Date(timestamp))));
  });

  chart.append(svgElement("polyline", {
    class: "history-line",
    points: points.map((point) => `${x(point.timestamp).toFixed(2)},${y(point.grams).toFixed(2)}`).join(" "),
  }));
  for (const point of points) {
    chart.append(svgElement("circle", {
      class: "history-point", cx: x(point.timestamp), cy: y(point.grams), r: 2.5,
    }));
  }
  const latest = points[points.length - 1];
  chart.append(svgElement("circle", {
    class: "history-point latest-point", cx: x(latest.timestamp), cy: y(latest.grams), r: 3.5,
  }));

  /* What a dot is worth, when the pointer is on it.
   *
   * The dots are two and a half pixels across, which is the right size to
   * read a shape from and far too small to hit, so each gets a target ten
   * times its size that draws nothing. */
  attachPointReadout(chart, points.map((point) => ({
    cx: x(point.timestamp), cy: y(point.grams),
    text: `${showWeight(point.grams)} g`,
    when: new Date(point.timestamp).toLocaleString([],
      { dateStyle: "medium", timeStyle: "short" }),
  })), { width, height });

  setText(summary,
    `${points.length} weighings · ${showWeight(Math.min(...points.map((p) => p.grams)))}` +
    `–${showWeight(Math.max(...points.map((p) => p.grams)))} g`);
}

/* Setting an anchor is a claim about the pot in a state it is not in: what it
 * weighs full, or dry. It is not a weighing taken now, so it does not become
 * what the pot weighs now, and setting the full weight is not watering it. */
/* The three weights a pot is read against, saved by one button.
 *
 * Only what changed is sent. A person correcting the thirsty weight
 * should not have the full weight rewritten underneath them with the
 * same number it already held -- it is a reference every other figure is
 * measured against, and rewriting it would move its date for nothing. */
async function setPlantAnchors() {
  const plantId = currentPlantId();
  const message = document.getElementById("plant-weight-anchor-message");
  const weight = (selectedPlant && selectedPlant.weight) || {};
  const wanted = [
    // The stated ceiling. This wrote kind "watered" once, which is
    // what a real watering writes, so pressing Set and watering the
    // plant moved the same number and the ceiling drifted with every
    // watering.
    ["full", "plant-weight-full", weight.full],
    ["thirsty", "plant-weight-thirsty", weight.thirsty],
    // Only what was stated counts as the current value here. Left empty,
    // dry falls back to the lightest weighing -- so comparing against
    // that would send a "change" every time somebody saves the other two.
    ["dry", "plant-weight-dry", weight.dry_stated ? weight.dry : null],
  ];
  const changed = [];
  for (const [kind, fieldId, anchor] of wanted) {
    const field = document.getElementById(fieldId);
    if (field === null || field.value === "") continue;
    const typed = Number(field.value);
    if (!Number.isFinite(typed) || typed <= 0) {
      setText(message, "Type the weight in grams.");
      return;
    }
    const grams = Math.round(typed);
    if (anchor && Math.round(anchor.grams) === grams) continue;
    changed.push([kind, grams]);
  }
  if (plantId === null) return;

  // What else sits on the scale is saved by the same button. It is
  // one of the four things somebody states about a pot, and a second
  // Set beside it was a second way to say the same kind of thing.
  const offsetField = document.getElementById("plant-weight-offset");
  const offsetNow = Number(offsetField.value || 0);
  const offsetWas = selectedPlant?.weight_offset_grams || 0;
  if (Number.isFinite(offsetNow) && offsetNow >= 0
      && Math.round(offsetNow) !== offsetWas) {
    const answered = await fetch(`/api/plants/${plantId}/weight-offset`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ grams: Math.round(offsetNow) }),
    }).catch(() => null);
    if (!answered || !answered.ok) {
      setText(message, "What else is on the scale could not be saved.");
      return;
    }
    if (!changed.length) {
      setText(message, "Saved.");
      await refreshPlant();
      return;
    }
  }
  if (!changed.length) {
    setText(message, "Both weights already say that.");
    return;
  }
  for (const [kind, grams] of changed) {
    const response = await fetch(`/api/plants/${plantId}/weight`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ grams, kind, observed: false }),
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      setText(message, payload.error || "That weight could not be recorded.");
      return;
    }
  }
  // The full weight is said plainly, because somebody who has just watered
  // the plant and typed its full weight will expect a watering in the log.
  const saved = changed.map(([kind]) => kind);
  const named = {watered: "Full", thirsty: "Thirsty", dry: "Dry"};
  const list = saved.map((kind) => named[kind]);
  const which = list.length > 1
    ? `${list.slice(0, -1).join(", ")} and ${list[list.length - 1]} weights set`
    : `${list[0]} weight set`;
  setText(message, saved.includes("watered")
    ? `${which} \u2014 a reference, not a watering. Weigh the pot after watering to record one.`
    : `${which}.`);
  await refreshPlant();
}

/* The anchor fields show what the plant currently believes, so correcting one
 * means editing a number rather than remembering it. */
function fillPlantAnchorFields() {
  const weight = (selectedPlant && selectedPlant.weight) || {};
  const fields = [
    ["plant-weight-full", weight.full],
    ["plant-weight-thirsty", weight.thirsty],
    // Empty where nobody has stated one, so the placeholder can say the
    // scale is answering it instead.
    ["plant-weight-dry", weight.dry_stated ? weight.dry : null],
  ];
  for (const [id, anchor] of fields) {
    const field = document.getElementById(id);
    if (field === null || field === document.activeElement) continue;
    const shown = anchor ? showWeight(anchor.grams) : "";
    if (field.value !== shown) field.value = shown;
  }
}

async function refreshPlant() {
  const plantId = currentPlantId();
  if (plantId === null) return;
  try {
    const response = await fetch(`/api/plants/${plantId}`);
    if (!response.ok) {
      setText(document.getElementById("plant-page-name"), "Plant not found");
      return;
    }
    selectedPlant = await response.json();
  } catch (_error) {
    return;
  }
  renderPlantPage();
}

async function recordPlantCare(kind) {
  const plantId = currentPlantId();
  if (plantId === null) return;
  let note = null;
  if (kind === "note") {
    note = window.prompt("What happened to this plant?");
    if (!note) return;
  }
  await fetch(`/api/plants/${plantId}/care`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ kind, note }),
  });
  await refreshPlant();
}

/* One button, and two ticks for what else happened at the pot.
 *
 * Both ticks only record. Watering used to also declare the weighing to
 * be the pot full, which moved the ceiling every other figure is measured
 * against -- so a pot weighed before it finished draining quietly
 * re-scaled its own history downward, and the water-left percentages with
 * it. The full weight is a deliberate claim now, made in the Full at
 * field, which is the one place that reads like a claim.
 */
/* A last look at a number before it goes in the log.
 *
 * The hub reads a sudden gain as a watering and files it as one, so
 * nobody has to remember a tick. The mistake that leaves behind is a
 * mistyped number, which is wrong in the log afterwards and makes the
 * weighing that follows it look like a refill. Cheap to ask once,
 * hard to spot later.
 *
 * Only for a change big enough to be one or the other: a pot losing
 * water a few grams at a time goes straight through.
 */
function weighingLooksRight(grams) {
  const previous = plantWeightPoints().at(-1)?.grams;
  if (previous == null) return true;
  const change = grams - previous;
  if (change >= Math.max(40, previous * 0.08)) {
    return window.confirm(
      `${showWeight(grams)} g is ${showWeight(change)} g heavier than the last `
      + `weighing of ${showWeight(previous)} g.\n\n`
      + "It will be recorded as a watering. Press Cancel if the number is wrong.");
  }
  if (change <= -Math.max(40, previous * 0.3)) {
    return window.confirm(
      `${showWeight(grams)} g is ${showWeight(-change)} g lighter than the last `
      + `weighing of ${showWeight(previous)} g.\n\n`
      + "A pot rarely loses that much at once. Press Cancel if the number is wrong.");
  }
  return true;
}

/* What else sits on the scale with the plant.
 *
 * A probe in the soil, a cache pot, a saucer. The hub takes it off a
 * weighing and stores the plant's own weight, so the drying rate and
 * the anchors keep meaning what they meant. Shown beside the weight
 * box, because a number quietly subtracted is a number nobody trusts.
 */
function renderPlantWeightOffset() {
  const field = document.getElementById("plant-weight-offset");
  const note = document.getElementById("plant-weight-offset-note");
  if (!field) return;
  const offset = selectedPlant?.weight_offset_grams || 0;
  if (field !== document.activeElement) field.value = offset || "";
  // Said plainly, because a number taken off without a word is the
  // kind of thing somebody finds months later and distrusts.
  setText(note, offset
    ? `Weigh the whole thing and type that. ${offset} g comes off, `
      + "and the plant's own weight is what is kept. Typed anchors are "
      + "not touched."
    : "If a probe or a cache pot is weighed with the plant, put its "
      + "weight here and the hub will take it off each weighing.");
}

async function recordPlantWeight() {
  const plantId = currentPlantId();
  const field = document.getElementById("plant-weight-input");
  const fed = document.getElementById("plant-weight-fed");
  const message = document.getElementById("plant-weight-message");
  // Always a weighing. What else happened is recorded beside it, not
  // folded into what the weighing is taken to mean.
  const kind = "reading";
  const at = weighingTime();
  if (at === null) {
    setText(message, "That date has not happened yet.");
    return;
  }
  const typed = Number(field.value);
  if (plantId === null || !field.value || !Number.isFinite(typed) || typed <= 0) {
    setText(message, "Type what the pot weighs, in grams.");
    return;
  }
  // Grams on the wire and grams in the database: the unit is a keypad choice,
  // not something the rest of the hub should have to know about.
  const grams = Math.round(typed);
  // Checked against the last weighing before it is written. A sudden
  // gain the hub files as a watering without asking; a sudden loss is
  // usually a mistyped number -- 240 for 2400 -- and that one is worth
  // stopping, because it is wrong in the log afterwards and makes the
  // next correct weighing look like a refill.
  if (!weighingLooksRight(grams)) {
    setText(message, "Not recorded.");
    field.focus();
    field.select();
    return;
  }
  const response = await fetch(`/api/plants/${plantId}/weight`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ grams, kind, at }),
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    setText(message, payload.error || "That weight could not be recorded.");
    return;
  }
  /* What else happened, each its own record, written at the weighing's
     own time so a back-dated weighing does not file today's watering.
     They go after the weight, because the weight is the thing that cannot
     be re-entered from memory: if one of these fails the weighing is
     still saved and the tick can be made again. */
  const alsoFed = fed.checked;
  const failed = [];
  // Only the feeding. The watering is the hub's own reading of the
  // weight and is recorded with the weighing itself.
  for (const [ticked, careKind, said] of [[alsoFed, "fertilizing", "feeding"]]) {
    if (!ticked) continue;
    const response = await fetch(`/api/plants/${plantId}/care`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind: careKind, at }),
    });
    if (!response.ok) failed.push(said);
  }

  field.value = "";
  // Cleared, so the next weighing is not a feeding by leftover tick, and
  // back to today, so a number typed tomorrow is not filed under yesterday.
  fed.checked = false;
  resetWeighingDate();
  const kept = [alsoFed && !failed.includes("feeding") ? "the feeding" : null]
    .filter(Boolean);
  setText(message, failed.length
    ? `Weight recorded, but the ${failed.join(" and ")} could not be saved. Tick again.`
    : kept.length ? `Recorded, with ${kept.join(" and ")}.`
      : "Recorded.");
  await refreshPlant();
}

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
      // The plant's name, room and kind are the plant's, and are left out
      // so that saving the device cannot quietly rewrite them.
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
  renderHousehold();
  renderSensorSettings();
  renderSoilPhCalibration();
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
document.getElementById("room-list").addEventListener("click", (event) => {
  const remove = event.target.closest(".delete-room");
  if (remove) deleteRoom(Number(remove.dataset.roomId));
});
document.querySelectorAll("[data-box-range]").forEach((button) => {
  button.addEventListener("click", () => {
    boxHours = Number(button.dataset.boxRange);
    document.querySelectorAll("[data-box-range]").forEach((other) => {
      other.setAttribute("aria-pressed", String(other === button));
    });
    refreshBoxHistory();
  });
});
document.getElementById("box-form").addEventListener("submit", saveBoxSettings);
document.getElementById("box-form").addEventListener("input", (event) => {
  boxEdited = true;
  // The drawing follows the numbers as they are typed.
  if (["box-kp", "box-ki", "box-kd", "box-target-rh", "box-min-speed",
       "box-max-speed"].includes(event.target.id)) drawPidPreview();
});
document.getElementById("box-preset").addEventListener("change", (event) => {
  applyPidPreset(event.target.value);
});
document.getElementById("box-mode").addEventListener("change", (event) => {
  showFanMode(event.target.value);
});
document.getElementById("alerts-form").addEventListener("submit", saveAlertSettings);
document.getElementById("alerts-test").addEventListener("click", testAlert);
document.getElementById("zigbee-alert-form")
  .addEventListener("submit", addAlertRule);
document.getElementById("zigbee-alert-rules")
  .addEventListener("click", changeAlertRule);
document.getElementById("alert-hear").addEventListener("click", hearAlertSound);
document.getElementById("alert-cancel").addEventListener("click", clearRuleForm);
// "reported" is for a thing that either happened or did not, so there
// is no number to compare it with.
document.getElementById("alert-test").addEventListener("change", (event) => {
  setHidden(document.getElementById("alert-threshold-field"),
            event.target.value === "true");
});
document.getElementById("alerts-generate")
  .addEventListener("click", generateAlertAddress);
document.getElementById("room-page-form").addEventListener("submit", saveRoomPage);
document.getElementById("room-page-delete")
  .addEventListener("click", deleteRoomFromItsPage);
document.getElementById("onboarding-room").addEventListener("change", renderRoomChoices);
document.getElementById("onboarding-plant").addEventListener("change", applyPlantChoice);
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
/* The hub's time zone.
 *
 * The menu is filled from the machine's own tzdata rather than a list baked
 * into the page, which would go stale and be wrong about the places that
 * change their rules. Filled once: six hundred options rebuilt on a poll
 * would be six hundred nodes replaced under somebody choosing from them. */
let hubTimezoneFilled = false;

async function refreshHubTimezone() {
  const field = document.getElementById("hub-timezone");
  if (field === null) return;
  let settings;
  try {
    const response = await fetch("/api/settings/timezones");
    if (!response.ok) return;
    settings = await response.json();
  } catch (_error) {
    return;
  }
  if (!hubTimezoneFilled) {
    const fragment = document.createDocumentFragment();
    for (const name of settings.items || []) {
      const option = document.createElement("option");
      option.value = name;
      setText(option, name);
      fragment.append(option);
    }
    field.append(fragment);
    hubTimezoneFilled = true;
  }
  // Chosen but not saved is theirs until they save it.
  if (field !== document.activeElement && field.dataset.edited !== "true") {
    const chosen = settings.timezone || "";
    if (field.value !== chosen) field.value = chosen;
  }
  const note = document.getElementById("hub-timezone-now");
  if (note !== null) {
    const zone = settings.timezone || settings.timezone_effective;
    let reading = "";
    try {
      reading = new Intl.DateTimeFormat(undefined, {
        timeZone: settings.timezone || undefined,
        dateStyle: "medium", timeStyle: "short",
      }).format(new Date());
    } catch (_error) {
      reading = new Date().toLocaleString();
    }
    setText(note, `It is ${reading} in ${zone}.`
      + (settings.timezone ? "" : " Nothing has been chosen, so this computer's zone is used."));
  }
}

async function saveHubTimezone(event) {
  event.preventDefault();
  const field = document.getElementById("hub-timezone");
  const message = document.getElementById("hub-timezone-message");
  const response = await fetch("/api/settings/timezone", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ timezone: field.value || null }),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    setText(message, body.error || "That zone could not be saved.");
    return;
  }
  delete field.dataset.edited;
  setText(message, field.value
    ? `Saved. The hub keeps time in ${field.value}.`
    : "Saved. The hub follows this computer's zone.");
  await refreshHubTimezone();
}

document.getElementById("hub-timezone").addEventListener("change", (event) => {
  event.currentTarget.dataset.edited = "true";
});
document.getElementById("hub-timezone-form").addEventListener("submit", saveHubTimezone);
document.getElementById("household-network-form").addEventListener("submit", saveHouseholdNetwork);
document.getElementById("wifi-ssid").addEventListener("input", () => {
  householdNetworkDraft = true;
});
document.getElementById("forget-household-network").addEventListener("click", forgetHouseholdNetwork);
document.getElementById("zigbee-permit").addEventListener("click", async (event) => {
  const state = document.getElementById("zigbee-permit-state");
  // Zero shuts the window immediately; the gateway treats it as "stop".
  const seconds = event.target.dataset.scanning === "yes" ? 0 : 180;
  await fetch("/api/zigbee/permit", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ seconds }),
  });
  // The countdown comes from the gateway on each refresh, so the page
  // cannot claim a window is open after it has shut.
  state.textContent = seconds ? "Starting…" : "Stopping…";
  await refreshZigbee();
});
document.getElementById("zigbee-release").addEventListener("click", async (event) => {
  const action = event.target.dataset.action === "reconnect" ? "reconnect" : "release";
  await fetch(`/api/zigbee/${action}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  await refreshZigbee();
});
document.getElementById("zigbee-subject-save").addEventListener("click", async () => {
  const value = document.getElementById("zigbee-subject-choice").value;
  await fetch("/api/zigbee/attach", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ eui: zigbeeEuiFromLocation(), target: value ? Number(value) : null }),
  });
  await refreshZigbee();
  renderZigbeeDevicePage();
});
document.getElementById("zigbee-name-save").addEventListener("click", async () => {
  const name = document.getElementById("zigbee-name").value.trim();
  if (!name) return;
  await fetch("/api/zigbee/rename", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ eui: zigbeeEuiFromLocation(), name }),
  });
  await refreshZigbee();
  renderZigbeeDevicePage();
});
document.getElementById("zigbee-scale-tare").addEventListener("click", () => {
  sendZigbeeCommand(zigbeeEuiFromLocation(), "t");
});
document.getElementById("zigbee-scale-wifi").addEventListener("click", (event) => {
  if (event.target.dataset.command) {
    sendZigbeeCommand(zigbeeEuiFromLocation(), event.target.dataset.command);
  }
});
// What the person chose, until the scale confirms it by reporting that rate.
// A sleepy device collects its commands on its own schedule, so "not yet"
// is a normal state and has to look different from "no".
let zigbeePendingInterval = null;

document.getElementById("zigbee-scale-interval").addEventListener("change", (event) => {
  zigbeePendingInterval = event.target.value;
  setText(document.getElementById("zigbee-scale-interval-state"), "Sending...");
  sendZigbeeCommand(zigbeeEuiFromLocation(), `i ${event.target.value}`);
});
document.querySelectorAll("[data-weight-range]").forEach((button) => {
  button.addEventListener("click", () => {
    zigbeeWeightHours = Number(button.dataset.weightRange);
    document.querySelectorAll("[data-weight-range]").forEach((other) => {
      setAttr(other, "aria-pressed", other === button ? "true" : "false");
    });
    loadZigbeeWeight(zigbeeEuiFromLocation(), true);
  });
});
document.querySelectorAll("[data-battery-range]").forEach((button) => {
  button.addEventListener("click", () => {
    zigbeeBatteryRange = Number(button.dataset.batteryRange);
    document.querySelectorAll("[data-battery-range]").forEach((other) => {
      setAttr(other, "aria-pressed", other === button ? "true" : "false");
    });
    loadZigbeeBattery(zigbeeEuiFromLocation(), true);
  });
});

document.getElementById("zigbee-device-back").addEventListener("click", (event) => {
  event.preventDefault();
  navigate("/settings/zigbee");
});
/* Both device lists, because the rows moved and the listener did not.
 *
 * Splitting the panel in two put the Add it and Remove buttons in a
 * second container while this was still bound to the first, so they
 * rendered perfectly and did nothing at all. Bound to each list rather
 * than to the panel around them: the panel also holds the scan button,
 * and a click handler that sees more than it needs is how a stray click
 * becomes an action.
 */
function onZigbeeListClick(event) {
  const command = event.target.closest(".zigbee-command");
  if (command) {
    sendZigbeeCommand(command.dataset.eui, command.dataset.command);
    return;
  }
  const link = event.target.closest("[data-zigbee-link]");
  if (link) {
    event.preventDefault();
    navigate(`/zigbee/${link.dataset.zigbeeLink}`);
    return;
  }
  const manage = event.target.closest(".zigbee-manage");
  if (manage) {
    manageZigbeeDevice(manage.dataset.eui, manage.dataset.action);
    return;
  }
  const claim = event.target.closest(".zigbee-claim");
  if (claim) claimZigbeeDevice(claim.dataset.eui, claim.dataset.action);
}
document.getElementById("zigbee-report-save")
  .addEventListener("click", saveZigbeeReporting);
document.getElementById("zigbee-plug-toggle").addEventListener("click", (event) => {
  const toggle = event.currentTarget;
  if (toggle.disabled) return;
  const on = toggle.getAttribute("aria-checked") === "true";
  commandZigbeePlug(toggle.dataset.eui, on ? "off" : "on");
});
document.getElementById("zigbee-plug-refresh").addEventListener("click", () => {
  commandZigbeePlug(zigbeeEuiFromLocation(), "state");
});
document.getElementById("zigbee-reports-keep")
  .addEventListener("change", saveZigbeeReports);
document.querySelectorAll("[data-measure-range]").forEach((button) => {
  button.addEventListener("click", () => {
    zigbeeMeasureHours = Number(button.dataset.measureRange);
    document.querySelectorAll("[data-measure-range]").forEach((other) => {
      other.setAttribute("aria-pressed", String(other === button));
    });
    loadZigbeeMeasurements(zigbeeEuiFromLocation(), true);
  });
});
for (const id of ["zigbee-devices", "zigbee-found"]) {
  document.getElementById(id).addEventListener("click", onZigbeeListClick);
}
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
  plantLightingDraftSubject = null;
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
document.getElementById("add-plant").addEventListener("click", addPlant);
document.getElementById("plant-fertilized").addEventListener("click", () => recordPlantCare("fertilizing"));
document.getElementById("plant-repotted").addEventListener("click", () => recordPlantCare("repotting"));
document.getElementById("plant-note").addEventListener("click", () => recordPlantCare("note"));
document.getElementById("plant-weight-record").addEventListener("click", () => recordPlantWeight());

document.getElementById("plant-weight-now").addEventListener("click", async (event) => {
  // Asks the scale for a reading rather than filing the last one it sent.
  // On a long interval the newest reading can be most of an interval old,
  // and a button called "now" that records a number from twenty minutes
  // ago is the sort of small lie that makes a log hard to trust.
  const button = event.currentTarget;
  const message = document.getElementById("plant-weight-now-message");
  const plantId = selectedPlant?.plant_id;
  if (!plantId) return;
  button.disabled = true;
  setText(message, "Asking the scale...");
  try {
    const response = await fetch(`/api/plants/${plantId}/weigh-now`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    if (!response.ok) throw new Error(await response.text() || "Could not weigh");
    const result = await response.json();
    // Said plainly when the hub refused it, because a button that looks
    // like it worked and quietly wrote nothing is worse than an error.
    setText(message, result.recorded
      ? `Recorded ${showWeight(result.grams)} g`
      : `Read ${showWeight(result.grams)} g, but it was not`
        + ` recorded: too far from the last weight to be this plant.`);
    await refreshPlant();
  } catch (error) {
    setText(message, error.message || "Could not reach the scale");
  } finally {
    button.disabled = false;
  }
});

document.getElementById("plant-weight-by-hand").addEventListener("click", (event) => {
  // No confirmation: typing a weight is not destructive, and the button
  // only un-dims a form.
  plantWeighByHand = true;
  document.getElementById("plant-weight-entry").classList.remove("is-automatic");
  setHidden(event.currentTarget, true);
  document.getElementById("plant-weight-input").focus();
});

document.getElementById("plant-weight-schedule").addEventListener("change", async (event) => {
  const message = document.getElementById("plant-weight-schedule-message");
  if (!plantScaleSensor) return;
  const [mode, value] = String(event.currentTarget.value).split(":");
  setText(message, "Saving...");
  try {
    const response = await fetch("/api/scale-schedule", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        sensor_id: plantScaleSensor.sensor_id,
        every_seconds: mode === "every" ? Number(value) : null,
        at_hour: mode === "at" ? Number(value) : null,
      }),
    });
    if (!response.ok) throw new Error(await response.text() || "Could not save");
    setText(message, mode === "at" ? "Saved. One weight a day." : "Saved.");
    if (mode === "at") plantScaleSensor.keep_at_hour = Number(value);
    else { plantScaleSensor.keep_at_hour = null; plantScaleSensor.keep_every_seconds = Number(value); }
  } catch (error) {
    setText(message, error.message || "Could not save");
  }
});
document.getElementById("plant-weight-set-anchors").addEventListener("click", setPlantAnchors);
/* The label opens the picker by itself, which is one less thing to go wrong
 * than forwarding a click; the keyboard needs the forwarding, though, since a
 * label is not a button. */
document.getElementById("plant-photo-add").addEventListener("keydown", (event) => {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    document.getElementById("plant-photo-file").click();
  }
});
document.getElementById("photo-viewer-close").addEventListener("click", closePhotoViewer);
document.getElementById("photo-viewer-newer").addEventListener("click", () => stepPhotoViewer(-1));
document.getElementById("photo-viewer-older").addEventListener("click", () => stepPhotoViewer(1));
// The backdrop closes; the picture and its controls do not, or every
// attempt to look closely would shut the thing being looked at.
document.getElementById("photo-viewer").addEventListener("click", (event) => {
  if (event.target === event.currentTarget) closePhotoViewer();
});
document.addEventListener("keydown", (event) => {
  if (document.getElementById("photo-viewer").hidden) return;
  /* Up and down are the photographs' own axis. Left and right do the
     same, because half of people reach for those in a picture viewer and
     being wrong about which should cost nothing. */
  const steps = {ArrowUp: -1, ArrowLeft: -1, ArrowDown: 1, ArrowRight: 1,
                 PageUp: -1, PageDown: 1};
  if (event.key === "Escape") { event.preventDefault(); closePhotoViewer(); return; }
  if (event.key in steps) { event.preventDefault(); stepPhotoViewer(steps[event.key]); }
});
/* One pointer, three meanings: a vertical swipe steps, a drag pans while
   zoomed, and two quick taps zoom in or out.
 *
 * A swipe is measured from where the finger went down to where it left
 * rather than followed as it moves: a photograph that slides under the
 * finger and snaps back when the swipe was too short is worse than one
 * that simply does not move. */
const DOUBLE_TAP_MS = 320;
let photoSwipeFrom = null;
let photoLastTap = 0;
const photoViewerFrame = document.getElementById("photo-viewer");

photoViewerFrame.addEventListener("pointerdown", (event) => {
  photoSwipeFrom = {x: event.clientX, y: event.clientY,
                    pan: {...photoPan}, moved: false};
});

photoViewerFrame.addEventListener("pointermove", (event) => {
  if (photoSwipeFrom === null || photoZoom === 1) return;
  // Dragging a zoomed picture moves the picture, not the page.
  event.preventDefault();
  photoSwipeFrom.moved = true;
  photoPan = {x: photoSwipeFrom.pan.x + (event.clientX - photoSwipeFrom.x),
              y: photoSwipeFrom.pan.y + (event.clientY - photoSwipeFrom.y)};
  applyPhotoZoom();
});

photoViewerFrame.addEventListener("pointerup", (event) => {
  const from = photoSwipeFrom;
  photoSwipeFrom = null;
  if (from === null) return;
  const down = event.clientY - from.y;
  const across = Math.abs(event.clientX - from.x);
  const still = Math.abs(down) < 10 && across < 10;

  /* Two taps in the same place, quickly. Only counted on the picture
     itself: double-tapping the backdrop means nothing, and the buttons
     have their own job. */
  if (still && event.target.id === "photo-viewer-image") {
    const now = Date.now();
    if (now - photoLastTap < DOUBLE_TAP_MS) {
      photoLastTap = 0;
      togglePhotoZoom(event.clientX, event.clientY);
      return;
    }
    photoLastTap = now;
    return;
  }
  // Zoomed in, a drag was a pan and has already happened.
  if (photoZoom !== 1 || from.moved) return;
  // Far enough to be meant, and more vertical than horizontal.
  if (Math.abs(down) < 60 || across > Math.abs(down)) return;
  stepPhotoViewer(down > 0 ? -1 : 1);
});

/* Dragging the scrubber. Its pointer events stop here rather than
   reaching the viewer, which would otherwise read the same drag as a
   swipe and step twice. */
const photoTrack = document.getElementById("photo-track");
let scrubbing = false;
photoTrack.addEventListener("pointerdown", (event) => {
  event.stopPropagation();
  event.preventDefault();
  scrubbing = true;
  photoTrack.setPointerCapture(event.pointerId);
  seekPhotoTrack(event.clientX);
});
photoTrack.addEventListener("pointermove", (event) => {
  event.stopPropagation();
  if (scrubbing) seekPhotoTrack(event.clientX);
});
for (const ending of ["pointerup", "pointercancel"]) {
  photoTrack.addEventListener(ending, (event) => {
    event.stopPropagation();
    scrubbing = false;
  });
}
// Left and right walk the track; the viewer's own arrows still work,
// and mean the same thing.
photoTrack.addEventListener("keydown", (event) => {
  const steps = {ArrowLeft: 1, ArrowRight: -1, Home: "first", End: "last"};
  if (!(event.key in steps)) return;
  event.preventDefault();
  event.stopPropagation();
  const photos = photoViewerList();
  if (steps[event.key] === "first") {
    viewedPhotoId = photos[photos.length - 1]?.photo_id ?? viewedPhotoId;
    resetPhotoZoom();
    renderPhotoViewer();
    return;
  }
  if (steps[event.key] === "last") {
    viewedPhotoId = photos[0]?.photo_id ?? viewedPhotoId;
    resetPhotoZoom();
    renderPhotoViewer();
    return;
  }
  stepPhotoViewer(steps[event.key]);
});

/* A mouse says double-click rather than double-tap, and says it plainly,
   so it does not need the timing above. */
document.getElementById("photo-viewer-image").addEventListener("dblclick", (event) => {
  event.preventDefault();
  togglePhotoZoom(event.clientX, event.clientY);
});
document.getElementById("plant-photo-more").addEventListener("click", () => {
  photosExpanded = !photosExpanded;
  renderPlantPhotos();
});
document.getElementById("sensor-attach-save").addEventListener("click", () => {
  const chosen = document.getElementById("sensor-attach-choice").value;
  if (!chosen || !selectedSensor) return;
  pointSensorAtPlant(selectedSensor.sensor_id, Number(chosen));
});
document.getElementById("scale-form").addEventListener("submit", (event) => {
  event.preventDefault();
  saveScale();
});
/* Changed means chosen but not yet saved, which is the state a poll must
 * not walk over. Marked here rather than inferred from the value, because a
 * choice that happens to match the device is still a choice somebody made. */
for (const id of ["scale-interval", "scale-keep-every"]) {
  document.getElementById(id).addEventListener("change", (event) => {
    event.currentTarget.dataset.edited = "true";
  });
}
document.getElementById("sensor-ph-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const raw = document.getElementById("ph-reference").value.trim();
  saveSoilPhCalibration(raw === "" ? null : Number(raw));
});
document.getElementById("ph-clear").addEventListener("click", () => {
  document.getElementById("ph-reference").value = "";
  saveSoilPhCalibration(null);
});
document.getElementById("plant-cover-follows").addEventListener("change", (event) => {
  setCoverFollowsLatest(event.target.checked);
});
document.getElementById("plant-photo-file").addEventListener("change", (event) => {
  // Copied out before the input is cleared: files is the input's own live
  // list, and emptying the input empties the list that was about to be read.
  const files = [...event.target.files];
  // Cleared so that choosing the same file twice in a row still counts.
  event.target.value = "";
  uploadPlantPhotos(files);
});
document.getElementById("plant-sensor-attach-save").addEventListener("click", () => {
  const chosen = document.getElementById("plant-sensor-choice").value;
  const plantId = plantIdFromLocation();
  if (chosen && plantId !== null) pointSensorAtPlant(chosen, plantId);
});
document.getElementById("plant-sensor-detach").addEventListener("click", () => {
  /* The sensor this page is about, which is not the same as the plant's
   * sensor. On a scale's page it detached the plant's probe instead --
   * the wrong instrument, from a plant nobody was looking at. */
  const sensor = pageFromLocation() === "sensor-config"
    ? selectedSensor
    : plantSensor();
  if (!sensor || sensor.plant_id == null) return;
  const plant = householdPlants.find((candidate) => candidate.plant_id === sensor.plant_id);
  const where = plant ? plant.display_name : "its plant";
  if (!window.confirm(`Take ${sensor.display_name || sensor.sensor_id} off ${where}? `
    + "Everything recorded about the plant stays; the sensor keeps its readings.")) return;
  pointSensorAtPlant(sensor.sensor_id, null);
});
for (const button of document.querySelectorAll(".arrange-sections")) {
  button.addEventListener("click", () => (arranging ? stopArranging(false) : startArranging()));
}
document.getElementById("arrange-save").addEventListener("click", saveSectionOrder);
document.getElementById("arrange-cancel").addEventListener("click", () => stopArranging(true));
document.querySelector("main").addEventListener("pointerdown", beginSectionDrag);
document.querySelector("main").addEventListener("keydown", nudgeSection);
document.getElementById("plant-config-form").addEventListener("submit", savePlantConfig);
// Touched by hand, and so no longer the hub's to rewrite. "input" covers a
// date sheet on a phone, which never focuses the field it belongs to.
for (const signal of ["input", "change", "focusin"]) {
  document.getElementById("plant-config-form").addEventListener(signal, () => {
    plantConfigDraftPlantId = plantIdFromLocation();
  });
}
// Choosing a profile offers its varieties at once, before anything is saved.
document.getElementById("plant-config-profile").addEventListener("change", () => fillPlantVarietySelect(null));
document.getElementById("plant-config-variety").addEventListener("change", () => fillPlantVarietySelect());
document.getElementById("firmware-install").addEventListener("click", installFirmware);
document.getElementById("firmware-cancel").addEventListener("click", cancelFirmwareUpdate);
forgetWeightUnits();
restoreHouseholdSort();
restoreCareLogFolded();
document.getElementById("plant-care-log-more")
  .addEventListener("click", () => loadMoreCareLog("plant-care-log"));
for (const button of document.querySelectorAll(".care-log-toggle")) {
  button.addEventListener("click", toggleCareLog);
}
trackRawReportsDisclosure();
/* The page shows itself before it has anything to say.
 *
 * Every section starts hidden and is revealed by renderPage, which until
 * now only ran inside the refresh -- so a plant page stayed blank through
 * four round trips, and on anything slower than this machine that is the
 * second or two before the headings, the buttons and the boxes appear.
 * They do not depend on data; they can be there while it arrives. */
renderPage();

/* The things the page needs before it can draw, none of which depends on
 * another: asked for together, they cost one round trip rather than seven.
 *
 * The plants are among them because other pages name them. A sensor's page
 * says which plant it is on, and fetching the list only once that page had
 * decided it was a sensor page meant the name arrived a second or two late,
 * after the box had already shown the bare id. */
Promise.all([
  loadProfiles(), refreshHouseholdNetwork(), refreshRooms(), refreshPlants(),
  refreshLightDrivers(), refreshFirmwareImages(), loadSectionOrder(),
]).then(poll);