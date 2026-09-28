// Grow lights: the Settings › Lights tab, and a plant's lighting.
//
// Loaded before app.js, which owns the shared helpers and the page state these
// functions read, and which calls refreshLights() and refreshPlantLighting()
// from its refresh loop. How a light is switched is its driver's business; the
// Add light form is built from what each driver says it needs.

let lightDrivers = [];
let lightItems = [];
let plantLighting = null;
let plantLightingDraftSensorId = null;
// Lights being switched from the plant page, by ID, to the state asked for.
// A switch shows what was asked while the light is asked, not what the last
// poll said a moment before.
const plantLightRequests = new Map();
const plantLightNotes = new Map();
// Bumped when a switch finishes, so a poll that set off before it cannot put
// the old state back.
let plantLightingEpoch = 0;

async function refreshLightDrivers() {
  const response = await fetch("/api/light-drivers");
  if (!response.ok) return;
  lightDrivers = (await response.json()).items;
  const select = document.getElementById("light-driver");
  select.replaceChildren();
  lightDrivers.forEach((driver) => {
    const option = document.createElement("option");
    option.value = driver.kind;
    setText(option, driver.label);
    select.append(option);
  });
  renderLightFields(select.value, {});
}

async function refreshLights() {
  const response = await fetch("/api/lights");
  if (!response.ok) return;
  lightItems = (await response.json()).items;
  renderLights();
}

function describeLightState(light) {
  if (!light.state.online) return "Unreachable · " + (light.state.detail || "no answer");
  if (light.state.power === true) return "On";
  if (light.state.power === false) return "Off";
  // Only the light can say whether it is on; until it has, the hub does not guess.
  return "State unknown" + (light.state.detail ? " · " + light.state.detail : "");
}

function lightStateLevel(light) {
  if (light.alert || !light.state.online) return "alert";
  if (light.state.power === true) return "on";
  if (light.state.power === false) return "off";
  return "unknown";
}

function describeLightPlant(light) {
  if (!light.plant) return "Not used by any plant · switched by hand only";
  const name = light.plant.display_name || "a plant";
  if (light.plant.archived) return "Lights " + name + " (archived) · not scheduled";
  if (!light.schedule) return "Lights " + name + " · no schedule";
  return "Lights " + name + " · on " + light.schedule.on + ", off " + light.schedule.off;
}

function describeLightEvent(event) {
  const when = new Date(event.at).toLocaleString([], {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
  const what = { schedule: "Schedule", watchdog: "Correction", manual: "By hand" }[event.source];
  const outcome = event.outcome === "confirmed" ? "confirmed" : event.outcome;
  return `${when} · ${what} ${event.wanted ? "on" : "off"} · ${outcome}`;
}

function renderLights() {
  const list = document.getElementById("light-list");
  const renderKey = JSON.stringify(lightItems.map((light) => [
    light.light_id, light.display_name, light.driver_label, light.state, light.alert,
    light.plant, light.schedule, Math.ceil(light.override_seconds_left / 60),
    light.events[0] || null,
  ]));
  if (list.dataset.renderKey === renderKey) return;
  setData(list, "renderKey", renderKey);
  list.replaceChildren();
  if (!lightItems.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    setText(empty, "No lights yet. Add the first one above.");
    list.append(empty);
    return;
  }
  lightItems.forEach((light) => {
    const card = document.createElement("div");
    card.className = "settings-sensor light-card";

    const body = document.createElement("div");
    body.className = "settings-sensor-body";
    const heading = document.createElement("div");
    heading.className = "settings-sensor-heading";
    const name = document.createElement("strong");
    setText(name, light.display_name);
    const kind = document.createElement("small");
    setText(kind, light.driver_label);
    heading.append(name, kind);

    const state = document.createElement("span");
    state.className = "light-state";
    setData(state, "level", lightStateLevel(light));
    setText(state, describeLightState(light));

    const plant = document.createElement("code");
    setText(plant, describeLightPlant(light));
    body.append(heading, state, plant);

    if (light.alert) {
      const alert = document.createElement("span");
      alert.className = "light-alert";
      setText(alert, light.alert);
      body.append(alert);
    }
    // The hand-switched pause only means something to a light on a schedule.
    if (light.override_seconds_left > 0 && light.schedule) {
      const override = document.createElement("small");
      setText(override, "Switched by hand · the schedule will not correct it for "
        + Math.ceil(light.override_seconds_left / 60) + " more min");
      body.append(override);
    }
    if (light.events.length) {
      const last = document.createElement("small");
      setText(last, "Last: " + describeLightEvent(light.events[0]));
      body.append(last);
    }

    const actions = document.createElement("div");
    actions.className = "settings-sensor-actions light-actions";
    [["Turn on", "light-on"], ["Turn off", "light-off"], ["Edit", "edit-light"],
      ["Remove", "danger remove-light"]].forEach(([label, className]) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = className;
      setData(button, "lightId", light.light_id);
      setText(button, label);
      actions.append(button);
    });
    card.append(body, actions);
    list.append(card);
  });
}

function renderLightFields(kind, config) {
  const container = document.getElementById("light-fields");
  const driver = lightDrivers.find((candidate) => candidate.kind === kind);
  container.replaceChildren();
  if (!driver) return;
  driver.fields.forEach((field) => {
    const label = document.createElement("label");
    label.setAttribute("for", "light-field-" + field.name);
    setText(label, field.label + (field.required ? "" : " (optional)"));
    const input = document.createElement("input");
    input.id = "light-field-" + field.name;
    input.dataset.field = field.name;
    input.dataset.kind = field.kind;
    input.required = field.required;
    if (field.kind === "integer") {
      input.type = "number";
      input.step = "1";
      if (field.minimum != null) input.min = field.minimum;
      if (field.maximum != null) input.max = field.maximum;
    }
    const value = config[field.name] ?? field.default;
    input.value = value == null ? "" : value;
    container.append(label, input);
    if (field.help) {
      const help = document.createElement("small");
      setText(help, field.help);
      container.append(help);
    }
  });
}

function lightFormConfig() {
  const config = {};
  document.querySelectorAll("#light-fields input").forEach((input) => {
    const text = input.value.trim();
    if (text === "") return;
    config[input.dataset.field] = input.dataset.kind === "integer" ? Number(text) : text;
  });
  return config;
}

function startLightEdit(lightId) {
  const light = lightItems.find((candidate) => candidate.light_id === lightId);
  if (!light) return;
  document.getElementById("light-id").value = light.light_id;
  document.getElementById("light-name").value = light.display_name;
  const select = document.getElementById("light-driver");
  select.value = light.driver;
  // A light's type is what it is; changing it would be a different light.
  setDisabled(select, true);
  renderLightFields(light.driver, light.config);
  setText(document.getElementById("light-form-title"), "Edit " + light.display_name);
  setText(document.getElementById("save-light"), "Save light");
  setHidden(document.getElementById("cancel-light-edit"), false);
  setText(document.getElementById("light-message"), "");
}

function clearLightForm() {
  document.getElementById("light-id").value = "";
  document.getElementById("light-name").value = "";
  const select = document.getElementById("light-driver");
  setDisabled(select, false);
  renderLightFields(select.value, {});
  setText(document.getElementById("light-form-title"), "Add a light");
  setText(document.getElementById("save-light"), "Add light");
  setHidden(document.getElementById("cancel-light-edit"), true);
}

async function saveLight(event) {
  event.preventDefault();
  const message = document.getElementById("light-message");
  const lightId = document.getElementById("light-id").value;
  const body = {
    display_name: document.getElementById("light-name").value.trim(),
    config: lightFormConfig(),
  };
  if (!lightId) body.driver = document.getElementById("light-driver").value;
  const response = await fetch(lightId ? "/api/lights/" + encodeURIComponent(lightId) : "/api/lights", {
    method: lightId ? "PUT" : "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const payload = await response.json();
  if (!response.ok) {
    setText(message, payload.error || "Could not save the light");
    return;
  }
  clearLightForm();
  setText(message, "Saved.");
  await refreshLights();
}

async function switchLight(lightId, on, button) {
  const light = lightItems.find((candidate) => candidate.light_id === lightId);
  const message = document.getElementById("light-message");
  // A Wemo can take most of a minute to answer when its ports hang, so the
  // wait is shown rather than left looking like nothing happened.
  button.closest(".light-actions").querySelectorAll("button").forEach((each) => setDisabled(each, true));
  setText(message, `Turning ${light ? light.display_name : "the light"} ${on ? "on" : "off"}…`);
  const response = await fetch("/api/lights/" + encodeURIComponent(lightId) + "/power", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ on }),
  });
  const payload = await response.json();
  if (!response.ok) {
    setText(message, payload.error || "Could not switch the light");
  } else if (payload.result.outcome === "confirmed") {
    setText(message, `${payload.display_name} is ${on ? "on" : "off"}.`);
  } else {
    setText(message, `${payload.display_name} did not confirm: ${payload.result.detail || payload.result.outcome}`);
  }
  setData(document.getElementById("light-list"), "renderKey", "");
  await refreshLights();
}

async function removeLight(lightId) {
  const light = lightItems.find((candidate) => candidate.light_id === lightId);
  if (!light) return;
  const question = light.plant
    ? `Remove ${light.display_name}? ${light.plant.display_name} will no longer use it. The light stays as it is now.`
    : `Remove ${light.display_name}? The light stays as it is now.`;
  if (!window.confirm(question)) return;
  const response = await fetch("/api/lights/" + encodeURIComponent(lightId), { method: "DELETE" });
  if (!response.ok) {
    const payload = await response.json();
    setText(document.getElementById("light-message"), payload.error || "Could not remove the light");
    return;
  }
  if (document.getElementById("light-id").value === lightId) clearLightForm();
  await refreshLights();
}

/* A plant's lighting.
 *
 * The schedule belongs to the plant, not the light, so it is edited here, with
 * the choice of which lights the plant uses. A light already lighting another
 * plant is shown but cannot be taken: one light, one plant, one schedule. */
async function refreshPlantLighting() {
  if (!selectedSensorId) return;
  const sensorId = selectedSensorId;
  const epoch = plantLightingEpoch;
  const response = await fetch("/api/sensors/" + encodeURIComponent(sensorId) + "/lighting");
  if (selectedSensorId !== sensorId || epoch !== plantLightingEpoch) return;
  if (!response.ok) {
    plantLighting = null;
    renderPlantLighting();
    return;
  }
  plantLighting = await response.json();
  renderPlantLighting();
}

function plantLights() {
  if (!plantLighting) return [];
  return plantLighting.lights.filter((light) => plantLighting.light_ids.includes(light.light_id));
}

function renderPlantLighting() {
  const windowText = document.getElementById("plant-lighting-window");
  const lightsList = document.getElementById("plant-lighting-lights");
  const allToggle = document.getElementById("plant-lighting-all");
  if (!plantLighting) {
    setText(windowText, "Lighting is not available for this plant.");
    lightsList.replaceChildren();
    setHidden(allToggle.closest(".lighting-all"), true);
    return;
  }
  const chosen = plantLights();
  renderAllLightsToggle(allToggle, chosen);
  const schedule = plantLighting.schedule;
  if (!chosen.length) {
    setText(windowText, "No grow lights. Choose them on this plant's configuration page.");
  } else if (!plantLighting.schedule_saved || !schedule.enabled) {
    setText(windowText, "Not on a schedule · its lights are switched by hand.");
  } else {
    const lit = chosen.some((light) => light.schedule?.in_window);
    setText(windowText, `On ${schedule.on}, off ${schedule.off} · ${lit ? "lit now" : "dark now"}`);
  }
  const renderKey = JSON.stringify(chosen.map((light) => [
    light.light_id, light.display_name, light.state, light.alert, Boolean(light.schedule),
    Math.ceil(light.override_seconds_left / 60), plantLightRequests.get(light.light_id) ?? null,
    plantLightNotes.get(light.light_id) || null,
  ]));
  if (lightsList.dataset.renderKey !== renderKey) {
    setData(lightsList, "renderKey", renderKey);
    lightsList.replaceChildren();
    chosen.forEach((light) => lightsList.append(plantLightRow(light)));
  }
  renderPlantLightingForm();
}

/* One switch for all of a plant's lights: on when every light that can be
 * reached is on, or has been asked to be. Unreachable lights are left out,
 * since they cannot be switched either way. */
function reachablePlantLights(chosen) {
  return chosen.filter((light) => light.state.online);
}

function lightIsOn(light) {
  return plantLightRequests.get(light.light_id) ?? light.state.power === true;
}

function renderAllLightsToggle(toggle, chosen) {
  const reachable = reachablePlantLights(chosen);
  setHidden(toggle.closest(".lighting-all"), chosen.length === 0);
  const allOn = reachable.length > 0 && reachable.every(lightIsOn);
  if (toggle.checked !== allOn) toggle.checked = allOn;
  setDisabled(toggle, reachable.length === 0 || plantLightRequests.size > 0);
  setAttr(toggle, "aria-label", allOn ? "Turn all this plant's lights off" : "Turn all this plant's lights on");
}

async function toggleAllPlantLights(on) {
  const reachable = reachablePlantLights(plantLights());
  await Promise.all(reachable
    .filter((light) => lightIsOn(light) !== on)
    .map((light) => togglePlantLight(light.light_id, on)));
}

function plantLightRow(light) {
  const requested = plantLightRequests.get(light.light_id);
  const row = document.createElement("div");
  row.className = "lighting-light";

  const text = document.createElement("div");
  text.className = "lighting-light-text";
  const name = document.createElement("strong");
  setText(name, light.display_name);
  const state = document.createElement("span");
  state.className = "light-state";
  if (requested !== undefined) {
    setData(state, "level", "unknown");
    setText(state, `Turning ${requested ? "on" : "off"}…`);
  } else {
    setData(state, "level", lightStateLevel(light));
    setText(state, light.alert || describeLightState(light));
  }
  text.append(name, state);
  const note = plantLightNotes.get(light.light_id)
    || (light.override_seconds_left > 0 && light.schedule
      ? "Switched by hand · the schedule takes over again in "
        + Math.ceil(light.override_seconds_left / 60) + " min, or at its next on or off"
      : "");
  if (note) {
    const small = document.createElement("small");
    setText(small, note);
    text.append(small);
  }

  const toggle = document.createElement("input");
  toggle.type = "checkbox";
  toggle.className = "switch lighting-toggle";
  toggle.dataset.lightId = light.light_id;
  toggle.checked = requested ?? light.state.power === true;
  // An unreachable light cannot be switched, and a switch that seemed to work
  // would be a lie.
  toggle.disabled = requested !== undefined || !light.state.online;
  toggle.setAttribute("aria-label", "Turn " + light.display_name + (toggle.checked ? " off" : " on"));
  row.append(text, toggle);
  return row;
}

async function togglePlantLight(lightId, on) {
  plantLightRequests.set(lightId, on);
  plantLightNotes.delete(lightId);
  renderPlantLighting();
  let note = null;
  try {
    const response = await fetch("/api/lights/" + encodeURIComponent(lightId) + "/power", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ on }),
    });
    const payload = await response.json();
    if (!response.ok) {
      note = payload.error || "Could not switch the light";
    } else {
      const { result, ...light } = payload;
      if (plantLighting) {
        plantLighting.lights = plantLighting.lights.map((each) => each.light_id === lightId ? light : each);
      }
      if (result.outcome !== "confirmed") {
        note = "Did not confirm: " + (result.detail || result.outcome);
      }
    }
  } catch (_error) {
    note = "Hub unavailable";
  }
  plantLightingEpoch += 1;
  plantLightRequests.delete(lightId);
  if (note) plantLightNotes.set(lightId, note);
  renderPlantLighting();
}

function renderPlantLightingForm() {
  const choices = document.getElementById("lighting-light-choices");
  const note = document.getElementById("lighting-note");
  if (!plantLighting) return;
  if (plantLightingDraftSensorId === selectedSensorId) return;
  const legend = choices.querySelector("legend");
  choices.replaceChildren(legend);
  if (!plantLighting.lights.length) {
    const empty = document.createElement("p");
    empty.className = "lighting-empty";
    setText(empty, "The hub has no lights yet. Add them in Settings › Lights.");
    choices.append(empty);
  }
  plantLighting.lights.forEach((light) => {
    const label = document.createElement("label");
    label.className = "lighting-choice";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.value = light.light_id;
    box.checked = plantLighting.light_ids.includes(light.light_id);
    const elsewhere = light.plant && light.plant.plant_id !== plantLighting.plant_id;
    box.disabled = Boolean(elsewhere);
    const text = document.createElement("span");
    setText(text, light.display_name + (elsewhere ? " · lights " + light.plant.display_name : ""));
    label.append(box, text);
    choices.append(label);
  });
  document.getElementById("lighting-enabled").checked = plantLighting.schedule.enabled;
  document.getElementById("lighting-on").value = plantLighting.schedule.on;
  document.getElementById("lighting-off").value = plantLighting.schedule.off;
  setText(note, plantLighting.schedule_saved
    ? "Times are this hub's local time. A window may run past midnight."
    : "Not saved yet: this is the suggested window. Saving puts the lights on it.");
}

async function savePlantLighting(event) {
  event.preventDefault();
  if (!selectedSensorId) return;
  const sensorId = selectedSensorId;
  const message = document.getElementById("plant-lighting-message");
  const lightIds = [...document.querySelectorAll("#lighting-light-choices input:checked")]
    .map((box) => box.value);
  const response = await fetch("/api/sensors/" + encodeURIComponent(sensorId) + "/lighting", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      light_ids: lightIds,
      schedule: {
        enabled: document.getElementById("lighting-enabled").checked,
        on: document.getElementById("lighting-on").value,
        off: document.getElementById("lighting-off").value,
      },
    }),
  });
  const payload = await response.json();
  if (selectedSensorId !== sensorId) return;
  if (!response.ok) {
    setText(message, payload.error || "Could not save the lighting");
    return;
  }
  plantLightingDraftSensorId = null;
  plantLighting = payload;
  renderPlantLighting();
  setText(message, "Saved. The lights follow it from now.");
}

document.getElementById("light-form").addEventListener("submit", saveLight);
document.getElementById("cancel-light-edit").addEventListener("click", clearLightForm);
document.getElementById("light-driver").addEventListener("change", (event) => {
  renderLightFields(event.target.value, {});
});
document.getElementById("light-list").addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button || !button.dataset.lightId) return;
  const lightId = button.dataset.lightId;
  if (button.classList.contains("light-on")) switchLight(lightId, true, button);
  else if (button.classList.contains("light-off")) switchLight(lightId, false, button);
  else if (button.classList.contains("edit-light")) startLightEdit(lightId);
  else if (button.classList.contains("remove-light")) removeLight(lightId);
});
document.getElementById("plant-lighting-all").addEventListener("change", (event) => {
  toggleAllPlantLights(event.target.checked);
});
document.getElementById("plant-lighting-lights").addEventListener("change", (event) => {
  const toggle = event.target.closest(".lighting-toggle");
  if (toggle) togglePlantLight(toggle.dataset.lightId, toggle.checked);
});
const plantLightingForm = document.getElementById("plant-lighting-form");
plantLightingForm.addEventListener("input", () => {
  plantLightingDraftSensorId = selectedSensorId;
});
plantLightingForm.addEventListener("submit", savePlantLighting);
