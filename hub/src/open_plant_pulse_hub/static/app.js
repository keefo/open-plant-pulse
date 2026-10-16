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
  if (path === "/settings" || path.startsWith("/settings/")) return "settings";
  if (path === "/onboarding" || path.startsWith("/onboarding/")) return "onboarding";
  return "fleet";
}

function settingsTabFromLocation() {
  const path = window.location.pathname;
  if (path === "/settings/wifi") return "wifi";
  if (path === "/settings/rooms") return "rooms";
  if (path === "/settings/lights") return "lights";
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
      || element.dataset.sensorKind === shownKind;
    setHidden(element, !(rightPage && rightKind));
  });
  setClass(document.getElementById("fleet-link"), "active", page === "fleet");
  setClass(document.getElementById("settings-link"), "active", page === "settings");
  if (selectedSensorId) {
    /* Configuring the device and configuring the plant are different jobs.
     *
     * This button used to divert to the plant's page whenever the sensor
     * had one, which left a sensor with no plant -- a scale, say -- with
     * nowhere to be configured, and buried the device's own settings under
     * a plant that merely happens to be in front of it. */
    const plantId = selectedSensor?.plant_id ?? null;
    /* Back to where a sensor is found, which is the sensors list. The
     * plant is not where this page was reached from, and a scale has no
     * plant to go back to. */
    setAttr(document.getElementById("config-back-link"), "href", "/settings");
    // Lighting is the plant's, so that one still goes to the plant.
    setAttr(document.getElementById("plant-lighting-change"), "href",
            plantId === null ? sensorPath(selectedSensorId, "/settings")
                             : `/plants/${plantId}/config`);
  }
  if (page === "plant" || page === "plant-config") {
    const plantId = plantIdFromLocation();
    if (plantId !== null) {
      setAttr(document.getElementById("plant-page-config"), "href", `/plants/${plantId}/config`);
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
  if (page === "sensor-config" && selectedSensor) {
    return `${selectedSensor.display_name || selectedSensor.sensor_id} · Sensor · Open Plant Pulse`;
  }
  if (page === "config") {
    const name = selectedSensor?.display_name || selectedSensorId || "Sensor";
    return `${name} · Configuration · Open Plant Pulse`;
  }
  if (page === "plant") {
    return `${selectedPlant?.display_name || "Plant"} · Open Plant Pulse`;
  }
  if (page === "settings") return "Settings · Open Plant Pulse";
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
  setText(document.getElementById("inbox-count"), unclaimedSensors.length);
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
      setText(empty, "No plants yet. Add one, or open the sensor inbox to enrol a sensor.");
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

/* How long it has lived here, short enough for a card.
 *
 * The unit a plant's age is worth saying in changes as it gets older --
 * nobody counts a three-year-old plant in days -- so the number stays small
 * and only the unit changes: 22d, 7mo, 2y, 2y 3mo. The whole phrase goes in
 * the tooltip for anyone who wants it spelled out.
 */
function timeAtHome(since) {
  const spans = timeAtHomeParts(since);
  return spans === null ? null : `Home ${spans.short}`;
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

/* How much water is left, as a percentage, for ordering only.
 *
 * A weighed pot and a probe are not measuring the same quantity -- one is
 * the water in the whole pot, the other the water around two centimetres of
 * needle -- so this is a rough answer to "who needs me first", not a figure
 * to show. A plant that cannot say sorts last rather than sorting as zero
 * and jumping the queue. */
function tileThirst(tile) {
  const left = tile.plant.weight?.remaining_percent;
  if (left != null) return left;
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
  setText(where, home ? `Home ${home.short}`
    : (plant?.room || sensor?.room || "no room recorded"));
  setAttr(where, "title", home ? home.long : "");

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
      : weight.remaining_percent <= 15 ? "thirsty" : "watered");
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
  setData(due, "state", days == null ? "unknown" : days <= 2 ? "soon" : "later");
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
  const reading = selectedSensor.latest?.reading;
  const gauge = document.getElementById("sensor-battery");
  if (gauge !== null) {
    setHidden(gauge.closest("div"), isScale(selectedSensor));
    if (reading) renderBattery(gauge, reading);
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
    grams == null ? "--" : `${(grams / 1000).toFixed(3)} kg`);
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
  renderInbox();
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
  // The reading itself, riding the surface it describes. The big figure
  // above the pot is the one to read; this one says which line is the water.
  setText(document.getElementById("moisture-waterline"), `${Math.round(value)}%`);
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

function historyTickLabel(timestamp) {
  const options = selectedHistoryRange === "live" || selectedHistoryRange === "24h"
    ? { hour: "numeric", minute: "2-digit" }
    : { month: "short", day: "numeric" };
  return new Intl.DateTimeFormat(undefined, options).format(new Date(timestamp));
}

function renderHistoryChart(chartId, emptyId, items, metric, unit, startTime, endTime, ideal) {
  const chart = document.getElementById(chartId);
  const empty = document.getElementById(emptyId);
  const points = items.map((item) => ({
    timestamp: Date.parse(item.reading.observed_at || item.received_at),
    value: item.reading[metric] == null ? null : Number(item.reading[metric])
  })).filter((point) => Number.isFinite(point.timestamp) && Number.isFinite(point.value));
  if (!renderKeyChanged(chart, [points, unit, startTime, endTime, selectedHistoryRange, ideal])) return;
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
  // The plant's ideal range is always in view, so the line is read against it.
  let minimum = ideal ? Math.min(observedMinimum, ideal[0]) : observedMinimum;
  let maximum = ideal ? Math.max(observedMaximum, ideal[1]) : observedMaximum;
  const padding = Math.max((maximum - minimum) * 0.12, metric === "air_temperature_c" ? 0.5 : 2);
  minimum -= padding;
  maximum += padding;
  if (metric === "air_humidity_percent") {
    minimum = Math.max(0, minimum);
    maximum = Math.min(100, maximum);
  }
  const x = timestamp => plot.left + ((timestamp - startTime) / (endTime - startTime)) * plotWidth;
  const y = value => plot.top + ((maximum - value) / (maximum - minimum)) * plotHeight;

  if (ideal) {
    const [idealLow, idealHigh] = ideal;
    chart.append(
      svgElement("rect", {
        class: "ideal-band", x: plot.left, width: plotWidth, y: y(idealHigh), height: y(idealLow) - y(idealHigh)
      }),
      svgElement("line", { class: "ideal-edge", x1: plot.left, x2: width - plot.right, y1: y(idealHigh), y2: y(idealHigh) }),
      svgElement("line", { class: "ideal-edge", x1: plot.left, x2: width - plot.right, y1: y(idealLow), y2: y(idealLow) }),
      svgElement("text", {
        class: "ideal-band-label", x: plot.left + 6, y: y(idealHigh) + 14
      }, `Ideal ${idealLow}–${idealHigh}`)
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

// The air history last fetched, kept so a change of plant profile redraws its
// ideal bands without waiting for the next report.
let climateHistory = null;

function renderClimateHistory(items, startTime, endTime) {
  climateHistory = [items, startTime, endTime];
  const profile = profiles?.profiles?.[climateProfileId || profiles?.default_profile];
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
    "air_temperature_c", "degrees Celsius", chartStartTime, chartEndTime,
    profile?.climate.air_temperature_c.ideal
  );
  renderHistoryChart(
    "humidity-history-chart", "humidity-history-empty", items,
    "air_humidity_percent", "percent relative humidity", chartStartTime, chartEndTime,
    profile?.climate.air_humidity_percent.ideal
  );
  const status = document.getElementById("climate-history-status");
  setClass(status, "error", false);
  const sampleStatus = items.length === 1
    ? "Collecting another reading to draw the line"
    : `${items.length} stored readings`;
  setText(status, `${historyRanges[selectedHistoryRange].label} · ${sampleStatus} · Updates live`);
}

/* Where the air readings come from for whichever page is open.
 *
 * Its own probe, on a plant that has one. Otherwise the room's: air belongs
 * to the room, and a plant with nothing in its soil is still in air somebody
 * is measuring. The ideal bands come from the plant being looked at either
 * way, because what counts as comfortable is the plant's business and not
 * the thermometer's. */
function airSource() {
  if (selectedSensorId && latestReading && latestReadingAt) {
    return {
      sensorId: selectedSensorId,
      at: latestReadingAt,
      stamp: latestReading.report_id,
      profileId: selectedSensor?.profile_id,
      borrowedFrom: null,
    };
  }
  const air = selectedPlant?.room_air;
  return air
    ? {
        sensorId: air.sensor_id,
        at: air.observed_at,
        stamp: air.observed_at,
        profileId: selectedPlant?.profile_id,
        borrowedFrom: air.sensor_name,
      }
    : null;
}

async function refreshClimateHistory() {
  const source = airSource();
  if (source === null) return;
  const range = historyRanges[selectedHistoryRange];
  const endTime = Date.parse(source.at);
  if (!Number.isFinite(endTime)) return;
  const startTime = endTime - range.milliseconds;
  const requestKey = `${source.sensorId}:${selectedHistoryRange}:${source.at}:${source.stamp}`;
  if (historyRequestKey === requestKey) return;
  historyRequestKey = requestKey;
  climateProfileId = source.profileId;
  setText(document.getElementById("climate-history-eyebrow"), source.borrowedFrom
    ? `ROOM AIR · MEASURED BY ${source.borrowedFrom.toUpperCase()}`
    : "SHT45 · LIVE HISTORY");
  const query = new URLSearchParams({
    sensor_id: source.sensorId,
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
  const settingSelect = document.getElementById("setting-profile");
  const onboardingSelect = document.getElementById("onboarding-profile");
  for (const [id, profile] of Object.entries(profiles.profiles)) {
    settingSelect.add(new Option(profile.name, id));
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
  setText(document.getElementById("watering-calendar-summary"),
    `${total} watering${total === 1 ? "" : "s"} and ${fedDays} feeding${fedDays === 1 ? "" : "s"} in ${range.year}`);
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
          refreshMoistureTrend(), refreshWateringCalendar(), refreshPotResponse(),
        ]);
      }
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
      return;
    }
    if (page !== "sensor-config" && page !== "config") return;
    if (!selectedSensorId || !selectedSensor) {
      return;
    }
    const query = sensorQuery();
    // Each page asks only for what it shows. The configuration page has the raw
    // report log and nothing that plots a reading; the plant page is the other
    // way round, and neither should pay for the other's requests every second.
    if (page === "config") {
      const rawReportsResponse = await fetch(`/api/raw-reports${query}`);
      if (rawReportsResponse.ok) renderRawReports((await rawReportsResponse.json()).items);
      await Promise.all([refreshFirmwareImages(), refreshPlantLighting()]);
      return;
    }
    const [latestResponse, careLogResponse] = await Promise.all([
      fetch(`/api/readings/latest${query}`),
      fetch(`/api/care-log${query}`)
    ]);
    if (latestResponse.ok) renderReading(await latestResponse.json());
    if (careLogResponse.ok) {
      careLogPaging.set("plant-care-log", query.replace(/^[?]/, ""));
      renderCareLog((await careLogResponse.json()).items);
    }
    await Promise.all([
      refreshClimateHistory(), refreshMoistureTrend(), refreshWateringCalendar(),
      refreshPotResponse(), refreshPlantLighting(),
      refreshPlantPhotos(), refreshPlant()
    ]);
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

/* Kilograms or grams, per field, as whoever is holding the scale prefers.
 *
 * A 17 kg pot typed in grams is five digits to get wrong; a seedling in
 * kilograms is 0.42 of one. Each box says which it is reading, because a
 * number typed into an unlabelled box is a guess about what the page meant.
 * Grams are stored either way, so this is only about the keypad.
 *
 * Remembered per browser, per box: if storage refuses, the choice simply does
 * not outlive the page. */
const WEIGHT_UNIT_FIELDS = {
  // The weighing field is left blank: a number already in the box is a
  // number to be read past, and the unit beside it says what to type.
  "plant-weight-unit": {
    key: "open-plant-pulse.weight-unit",
    field: "plant-weight-input",
    placeholder: { kg: "", g: "" },
  },
  "plant-weight-full-unit": {
    key: "open-plant-pulse.weight-unit.full",
    field: "plant-weight-full",
    placeholder: { kg: "17.40", g: "17400" },
  },
  "plant-weight-thirsty-unit": {
    key: "open-plant-pulse.weight-unit.thirsty",
    field: "plant-weight-thirsty",
    placeholder: { kg: "16.20", g: "16200" },
  },
};

function unitOf(selectId) {
  const chosen = document.getElementById(selectId);
  return chosen && chosen.value === "g" ? "g" : "kg";
}

/* The unit the panel reads in: the one beside the weighing it is about. */
function weightUnit() {
  return unitOf("plant-weight-unit");
}

function gramsFrom(typed, unit) {
  return unit === "g" ? Math.round(typed) : Math.round(typed * 1000);
}

function showWeight(grams, unit = weightUnit()) {
  if (grams == null) return "--";
  return unit === "g" ? String(grams) : (grams / 1000).toFixed(2);
}

/* The field asks for the unit it will be read in, and a number already in it
 * is converted rather than silently reinterpreted. */
function applyWeightUnit(selectId, previous) {
  const spec = WEIGHT_UNIT_FIELDS[selectId];
  const field = document.getElementById(spec.field);
  if (field === null) return;
  const unit = unitOf(selectId);
  const grams = unit === "g";
  field.step = grams ? "1" : "0.01";
  field.max = grams ? "500000" : "500";
  field.placeholder = spec.placeholder[unit];
  if (previous && previous !== unit && field.value) {
    const typed = Number(field.value);
    if (Number.isFinite(typed)) field.value = showWeight(gramsFrom(typed, previous), unit);
  }
}

function restoreWeightUnits() {
  for (const [selectId, spec] of Object.entries(WEIGHT_UNIT_FIELDS)) {
    const chosen = document.getElementById(selectId);
    if (chosen === null) continue;
    try {
      const remembered = window.localStorage.getItem(spec.key);
      if (remembered === "g" || remembered === "kg") chosen.value = remembered;
    } catch (_error) {
      /* Kilograms is the default either way. */
    }
    chosen.dataset.unit = unitOf(selectId);
    applyWeightUnit(selectId);
    chosen.addEventListener("change", () => {
      const previous = chosen.dataset.unit;
      chosen.dataset.unit = unitOf(selectId);
      try {
        window.localStorage.setItem(spec.key, chosen.value);
      } catch (_error) {
        /* The selector still works; it just forgets. */
      }
      applyWeightUnit(selectId, previous);
      renderPlantPage();
    });
  }
}

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

  setText(document.getElementById("plant-weight-latest"),
    weight.latest ? showWeight(weight.latest.grams) : "--");
  setText(document.getElementById("plant-weight-latest-label"),
    weightUnit() === "g" ? "Weighs now (g)" : "Weighs now (kg)");
  setText(document.getElementById("plant-weight-used"),
    weight.used_grams == null ? "--" : String(weight.used_grams));
  setText(document.getElementById("plant-weight-remaining"),
    weight.remaining_percent == null ? "--" : weight.remaining_percent + "%");
  setText(document.getElementById("plant-weight-daily"),
    weight.daily_grams == null ? "--" : String(weight.daily_grams));
  setText(document.getElementById("plant-weight-days"),
    weight.days_left == null ? "--" : String(weight.days_left));
  /* "weighed" only for a weighing: an anchor typed in from memory was set,
   * which is a different claim about a different moment. */
  setText(document.getElementById("plant-weight-summary"),
    weight.watered
      ? `Full at ${showWeight(weight.watered.grams)} ${weightUnit()}, ` +
        `${weight.watered.observed ? "weighed" : "set"} ${daysSince(weight.watered.at)}`
      : "No watered weight yet");

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

function renderPlantPhotos() {
  const grid = document.getElementById("plant-photo-grid");
  if (grid === null) return;
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

    /* Opening the picture at its own address rather than in a viewer of our
     * own: the browser already has one, with zoom and save. */
    const link = document.createElement("a");
    link.href = photoUrl(photo.photo_id, photo.digest);
    link.target = "_blank";
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
    if (photo.cover) {
      const mark = document.createElement("strong");
      // Said plainly when it is the cover because it is the newest rather
      // than because anybody chose it: the next photograph will take it.
      setText(mark, coverFollowsLatest ? "Cover · newest" : "Cover");
      actions.append(mark);
    } else {
      const choose = document.createElement("button");
      choose.type = "button";
      choose.className = "link-button";
      setText(choose, "Make cover");
      choose.addEventListener("click", () => setPlantCover(photo.photo_id));
      actions.append(choose);
    }
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "link-button";
    setText(remove, "Remove");
    remove.addEventListener("click", () => removePlantPhoto(photo));
    actions.append(remove);
    caption.append(when, actions);

    tile.append(link, caption);
    grid.append(tile);
  }
}

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
  return sensorKind(sensor) === "scale";
}

/* What sort of instrument this is, which decides what its page is made of. */
function sensorKind(sensor) {
  return sensor?.transport === "http-scale" ? "scale" : "bthome";
}

const SENSOR_KINDS = {
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

/* The scale under this plant's pot, if it has one. */
function plantScale() {
  const plantId = plantIdFromLocation();
  return plantId === null
    ? null
    : fleetSensors.find((sensor) => sensor.plant_id === plantId && isScale(sensor)) || null;
}

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
  const room = document.getElementById("plant-config-room").value;
  const response = await fetch(`/api/plants/${selectedPlant.plant_id}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      display_name: document.getElementById("plant-config-display-name").value,
      room_id: room === "" ? null : Number(room),
      expected_watering_days: days === "" ? null : Number(days),
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
    ? `The pot is at ${showWeight(forecast.grams_now)} ${weightUnit()}, which is the `
      + "weight you set as thirsty for this plant. Water it when you are ready, "
      + "and weigh it again afterwards to start the next cycle."
    : `The pot weighs ${showWeight(forecast.grams_now)} ${weightUnit()} and asks for `
      + `water at ${showWeight(forecast.thirsty_grams)} ${weightUnit()}, so it has `
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
    }, `Thirsty at ${showWeight(forecast.thirsty_grams)} ${weightUnit()}`)
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
  if (!renderKeyChanged(list, [weighings, weightUnit()])) return;
  list.replaceChildren();
  for (const weighing of weighings) {
    const row = document.createElement("div");
    row.className = "weighing-row";
    const when = document.createElement("time");
    when.dateTime = weighing.at;
    setText(when, new Date(weighing.at).toLocaleString([],
      { dateStyle: "medium", timeStyle: "short" }));
    const what = document.createElement("strong");
    setText(what, `${showWeight(weighing.grams)} ${weightUnit()}`);
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
  if (!window.confirm(`Remove the ${showWeight(weighing.grams)} ${weightUnit()} `
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
      [points, weight.watered, weight.thirsty, weight.dry, width, weightUnit()])) return;
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
  const anchors = [weight.watered?.grams, weight.thirsty?.grams, weight.dry?.grams]
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
  const fullGrams = weight.watered?.grams;
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
      }, `Full at ${showWeight(fullGrams)} ${weightUnit()}`)
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
        }, `Thirsty at ${showWeight(refill)} ${weightUnit()}`)
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
      }, `Driest seen ${showWeight(dryGrams)} ${weightUnit()}`)
    );
  } else if (weight.watered) {
    chart.append(
      svgElement("line", {
        class: "trend-cycle-edge", x1: plot.left, x2: width - plot.right,
        y1: y(weight.watered.grams), y2: y(weight.watered.grams),
      }),
      svgElement("text", {
        class: "trend-cycle-label", x: plot.left + 4, y: y(weight.watered.grams) + 12,
        "text-anchor": "start",
      }, `Full at ${showWeight(weight.watered.grams)} ${weightUnit()}`)
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

  const unit = weightUnit();
  setText(summary,
    `${points.length} weighings · ${showWeight(Math.min(...points.map((p) => p.grams)))}` +
    `–${showWeight(Math.max(...points.map((p) => p.grams)))} ${unit}`);
}

/* Setting an anchor is a claim about the pot in a state it is not in: what it
 * weighs full, or dry. It is not a weighing taken now, so it does not become
 * what the pot weighs now, and setting the full weight is not watering it. */
const PLANT_ANCHOR_FIELDS = {
  watered: ["plant-weight-full", "plant-weight-full-unit"],
  thirsty: ["plant-weight-thirsty", "plant-weight-thirsty-unit"],
};

async function setPlantAnchor(kind) {
  const plantId = currentPlantId();
  const [fieldId, unitId] = PLANT_ANCHOR_FIELDS[kind];
  const field = document.getElementById(fieldId);
  const unit = unitOf(unitId);
  const message = document.getElementById("plant-weight-anchor-message");
  const typed = Number(field.value);
  if (plantId === null || !field.value || !Number.isFinite(typed) || typed <= 0) {
    setText(message, `Type the weight in ${unit === "g" ? "grams" : "kilograms"}.`);
    return;
  }
  const grams = gramsFrom(typed, unit);
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
  // Said plainly, because somebody who has just watered the plant and typed
  // its full weight will otherwise expect the watering to be in the log.
  setText(message, kind === "watered"
    ? "Full weight set — a reference, not a watering. Tick Just watered on a weighing to record one."
    : "Thirsty weight set.");
  await refreshPlant();
}

/* The anchor fields show what the plant currently believes, so correcting one
 * means editing a number rather than remembering it. */
function fillPlantAnchorFields() {
  const weight = (selectedPlant && selectedPlant.weight) || {};
  const fields = [
    ["plant-weight-full", "plant-weight-full-unit", weight.watered],
    ["plant-weight-thirsty", "plant-weight-thirsty-unit", weight.thirsty],
  ];
  for (const [id, selectId, anchor] of fields) {
    const field = document.getElementById(id);
    if (field === null || field === document.activeElement) continue;
    const shown = anchor ? showWeight(anchor.grams, unitOf(selectId)) : "";
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

/* One button. The checkbox beside it says whether this weighing is the pot
 * full -- the ceiling everything else is measured against, and a watering in
 * the care log with it. */
async function recordPlantWeight() {
  const plantId = currentPlantId();
  const field = document.getElementById("plant-weight-input");
  const watered = document.getElementById("plant-weight-watered");
  const message = document.getElementById("plant-weight-message");
  const kind = watered.checked ? "watered" : "reading";
  const at = weighingTime();
  if (at === null) {
    setText(message, "That date has not happened yet.");
    return;
  }
  const typed = Number(field.value);
  if (plantId === null || !field.value || !Number.isFinite(typed) || typed <= 0) {
    setText(message, `Type what the pot weighs, in ${weightUnit() === "g" ? "grams" : "kilograms"}.`);
    return;
  }
  // Grams on the wire and grams in the database: the unit is a keypad choice,
  // not something the rest of the hub should have to know about.
  const grams = gramsFrom(typed, weightUnit());
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
  field.value = "";
  // Cleared, so the next weighing is not a watering by leftover tick, and
  // back to today, so a number typed tomorrow is not filed under yesterday.
  watered.checked = false;
  resetWeighingDate();
  setText(message, kind === "watered"
    ? "Recorded, and the watering with it."
    : "Recorded.");
  await refreshPlant();
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
document.getElementById("plant-weight-set-full").addEventListener("click", () => setPlantAnchor("watered"));
document.getElementById("plant-weight-set-thirsty").addEventListener("click", () => setPlantAnchor("thirsty"));
/* The label opens the picker by itself, which is one less thing to go wrong
 * than forwarding a click; the keyboard needs the forwarding, though, since a
 * label is not a button. */
document.getElementById("plant-photo-add").addEventListener("keydown", (event) => {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    document.getElementById("plant-photo-file").click();
  }
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
restoreWeightUnits();
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