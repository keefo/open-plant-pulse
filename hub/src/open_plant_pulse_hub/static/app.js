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
let profiles = null;
let latestReading = null;
let drainageAssessments = [];
let renderedCareEventIds = new Set();
let careLogInitialized = false;
let clockObservedAt = null;
let clockAnchoredAt = null;
let clockScale = null;
let previousClockSample = null;
let syncedProfileKey = null;
let wateringCalendarRequestKey = null;
let wateringIntervalSummary = null;

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
  const profileId = document.getElementById("plant-profile").value;
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
  const isLive = Number.isFinite(receivedAt) && Date.now() - receivedAt <= sensorOfflineAfterMs;
  document.getElementById("status").textContent = isLive ? "Sensor live" : "Sensor offline";
  document.getElementById("status-dot").classList.toggle("live", isLive);
  const displayedAt = reading.observed_at || payload.received_at;
  document.getElementById("updated").textContent = `Sample ${reading.sequence} · ${new Date(displayedAt).toLocaleTimeString()}`;
  syncSensorProfile();
  renderProfileRanges();
}

async function syncSensorProfile() {
  if (!profiles || !latestReading) return;
  const profileId = document.getElementById("plant-profile").value;
  const key = `${latestReading.sensor_id}:${profileId}`;
  if (syncedProfileKey === key) return;
  syncedProfileKey = key;
  try {
    const response = await fetch("/api/sensors/profile", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sensor_id: latestReading.sensor_id, profile_id: profileId })
    });
    if (!response.ok) throw new Error("profile sync failed");
  } catch (_error) {
    syncedProfileKey = null;
  }
}

async function loadProfiles() {
  const response = await fetch("/api/plant-profiles");
  profiles = await response.json();
  const select = document.getElementById("plant-profile");
  for (const [id, profile] of Object.entries(profiles.profiles)) {
    select.add(new Option(profile.name, id));
  }
  select.value = localStorage.getItem("plant-profile") || profiles.default_profile;
  if (!profiles.profiles[select.value]) select.value = profiles.default_profile;
  document.getElementById("chemistry-guidance").textContent = profiles.guidance;
  select.addEventListener("change", () => {
    localStorage.setItem("plant-profile", select.value);
    syncedProfileKey = null;
    syncSensorProfile();
    renderProfileRanges();
  });
  renderProfileRanges();
}

const plantLabel = document.getElementById("plant-label");
plantLabel.value = localStorage.getItem("plant-label") || plantLabel.value;
plantLabel.addEventListener("change", () => {
  const label = plantLabel.value.trim() || "White bird of paradise";
  plantLabel.value = label;
  localStorage.setItem("plant-label", label);
});

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
  const requestKey = `${latestReading.sensor_id}:${range.year}:${utcDateKey(range.todayStart)}`;
  try {
    const query = new URLSearchParams({
      sensor_id: latestReading.sensor_id,
      start: range.yearStart.toISOString(),
      end: range.yearEnd.toISOString()
    });
    const response = await fetch(`/api/watering-calendar?${query}`);
    if (!response.ok) throw new Error("calendar request failed");
    const payload = await response.json();
    wateringIntervalSummary = payload.watering_interval;
    const profileId = document.getElementById("plant-profile").value;
    renderWateringCalendar(payload.items, range, profiles.profiles[profileId].watering);
    if (profiles) {
      renderDrainageAssessment(profiles.profiles[profileId]);
    }
    wateringCalendarRequestKey = requestKey;
  } catch (_error) {
    if (wateringCalendarRequestKey !== requestKey) {
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
      const profileId = document.getElementById("plant-profile").value;
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
  try {
    const response = await fetch("/api/readings/history");
    if (!response.ok) return;
    const history = (await response.json()).items;
    history.slice(-2).forEach(updateClockSource);
  } catch (_error) {
    return;
  }
}

async function refresh() {
  try {
    const [latestResponse, careLogResponse] = await Promise.all([
      fetch("/api/readings/latest"), fetch("/api/care-log")
    ]);
    if (latestResponse.ok) renderReading(await latestResponse.json());
    if (careLogResponse.ok) renderCareLog((await careLogResponse.json()).items);
    await Promise.all([refreshWateringCalendar(), refreshPotResponse(), refreshPlantJourney()]);
  } catch (_error) {
    document.getElementById("status").textContent = "Hub unavailable";
    document.getElementById("status-dot").classList.remove("live");
  }
}

window.addEventListener("resize", refresh);
Promise.all([loadProfiles(), restoreClockScale()]).then(refresh);
setInterval(refresh, 1000);
setInterval(renderSimulationClock, 100);