const views = document.querySelectorAll('.view');
const navItems = document.querySelectorAll('.nav-item');
const connection = document.getElementById('connection');
const connectionLabel = document.getElementById('connection-label');
const logConsole = document.getElementById('log-console');
const configForm = document.getElementById('ui-config-form');
const configState = document.getElementById('config-state');
const sht45Enabled = document.getElementById('sht45-enabled');
const soilProbeEnabled = document.getElementById('soil-probe-enabled');
const restartButton = document.getElementById('restart-device');
const restartState = document.getElementById('restart-state');
const forceReportButton = document.getElementById('force-report');
const forceReportState = document.getElementById('force-report-state');
const firmwareVersion = document.getElementById('firmware-version');
const cardLabels = {
  device: 'Device',
  power: 'Power',
  uptime: 'Uptime',
  heap: 'Free heap',
  chip_temperature: 'Chip temperature',
  date_time: 'Date & time',
  rtc: 'RTC',
};
const CONFIG_SAVE_DELAY_MS = 300;
const CLOCK_RENDER_INTERVAL_MS = 1000;
const CLOCK_REANCHOR_THRESHOLD_MS = 1500;
const DEFAULT_STATUS_REFRESH_INTERVAL_MS = 2000;
let activeView = 'overview';
let uiConfig;
let configRevision = 0;
let configSaveTimer;
let configOperation = Promise.resolve();
let latestStatus;
let clockAnchorUnixMs;
let clockAnchorMonotonicMs;
let clockRenderTimer;
let statusRefreshTimer;
let activeStatusRefreshIntervalMs;

function browserTimezones() {
  const zones = typeof Intl.supportedValuesOf === 'function'
    ? Intl.supportedValuesOf('timeZone')
    : [];
  return [...new Set(['UTC', ...zones])];
}

function populateTimezoneSelect(selectedTimezone) {
  const timezoneSelect = configForm.elements.timezone;
  const timezones = browserTimezones();
  if (!timezones.includes(selectedTimezone)) timezones.push(selectedTimezone);
  timezoneSelect.replaceChildren(...timezones.sort().map((timezone) => {
    const option = document.createElement('option');
    option.value = timezone;
    option.textContent = timezone.replaceAll('_', ' ');
    return option;
  }));
  timezoneSelect.value = selectedTimezone;
}

function showView(name) {
  activeView = name;
  views.forEach((view) => {
    const active = view.id === name;
    view.hidden = !active;
    view.classList.toggle('active', active);
  });
  navItems.forEach((item) => item.classList.toggle('active', item.dataset.view === name));
}

navItems.forEach((item) => item.addEventListener('click', () => showView(item.dataset.view)));

function applyUiConfig(config) {
  const preferredTheme = config.theme.mode === 'system'
    ? (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark')
    : config.theme.mode;
  document.documentElement.dataset.theme = preferredTheme;
  document.documentElement.dataset.accent = config.theme.accent;
  document.documentElement.dataset.density = config.dashboard.density;
  document.querySelector('meta[name="theme-color"]').content =
    preferredTheme === 'light' ? '#f1f4f0' : '#18221c';

  const metricGrid = document.querySelector('.metric-grid');
  config.dashboard.cards.forEach((card) => {
    const element = metricGrid.querySelector(`[data-card-id="${card.id}"]`);
    if (!element) return;
    element.hidden = !card.visible;
    element.style.setProperty('--card-span', card.span);
    metricGrid.appendChild(element);
  });
  sht45Enabled.checked = config.sensors.sht45.enabled;
  sht45Enabled.disabled = false;
  soilProbeEnabled.checked = config.sensors.soil_probe.enabled;
  soilProbeEnabled.disabled = false;
  renderSoil(latestStatus);
  setStatusRefreshInterval(config.polling.status_refresh_interval_ms);
  if (latestStatus) {
    renderClock(latestStatus);
    renderSht45(latestStatus);
  }
}

function showConfigState(message, state = '') {
  configState.className = `config-state ${state}`.trim();
  configState.textContent = message;
}

function renderConfigForm() {
  if (!uiConfig) return;
  configForm.elements['theme-mode'].value = uiConfig.theme.mode;
  configForm.elements.accent.value = uiConfig.theme.accent;
  configForm.elements.density.value = uiConfig.dashboard.density;
  populateTimezoneSelect(uiConfig.clock.timezone);
  configForm.elements['status-refresh-interval'].value =
    String(uiConfig.polling.status_refresh_interval_ms);

  const cardSettings = document.getElementById('card-settings');
  cardSettings.replaceChildren();
  uiConfig.dashboard.cards.forEach((card, index) => {
    const row = document.createElement('div');
    row.className = 'card-setting';
    row.innerHTML = `
      <label class="card-toggle"><input type="checkbox" ${card.visible ? 'checked' : ''}><span>${cardLabels[card.id]}</span></label>
      <select aria-label="${cardLabels[card.id]} width"><option value="1">1 column</option><option value="2">2 columns</option></select>
      <div class="order-actions">
        <button class="icon-action move-up" type="button" title="Move up" aria-label="Move ${cardLabels[card.id]} up" ${index === 0 ? 'disabled' : ''}>&uarr;</button>
        <button class="icon-action move-down" type="button" title="Move down" aria-label="Move ${cardLabels[card.id]} down" ${index === uiConfig.dashboard.cards.length - 1 ? 'disabled' : ''}>&darr;</button>
      </div>`;
    const visible = row.querySelector('input');
    const span = row.querySelector('select');
    span.value = String(card.span);
    visible.addEventListener('change', (event) => {
      const visibleCount = uiConfig.dashboard.cards.filter((item) => item.visible).length;
      if (!visible.checked && visibleCount === 1) {
        visible.checked = true;
        event.stopPropagation();
        showConfigState('Keep at least one card visible', 'error');
        return;
      }
      card.visible = visible.checked;
      applyUiConfig(uiConfig);
    });
    span.addEventListener('change', () => {
      card.span = Number(span.value);
      applyUiConfig(uiConfig);
    });
    row.querySelector('.move-up').addEventListener('click', () => moveCard(index, -1));
    row.querySelector('.move-down').addEventListener('click', () => moveCard(index, 1));
    cardSettings.appendChild(row);
  });
}

function moveCard(index, direction) {
  const target = index + direction;
  if (target < 0 || target >= uiConfig.dashboard.cards.length) return;
  [uiConfig.dashboard.cards[index], uiConfig.dashboard.cards[target]] =
    [uiConfig.dashboard.cards[target], uiConfig.dashboard.cards[index]];
  applyUiConfig(uiConfig);
  renderConfigForm();
  scheduleUiConfigSave();
}

function readConfigForm() {
  uiConfig.theme.mode = configForm.elements['theme-mode'].value;
  uiConfig.theme.accent = configForm.elements.accent.value;
  uiConfig.dashboard.density = configForm.elements.density.value;
  uiConfig.clock.timezone = configForm.elements.timezone.value;
  uiConfig.polling.status_refresh_interval_ms =
    Number(configForm.elements['status-refresh-interval'].value);
  return uiConfig;
}

async function writeUiConfig(config) {
  const response = await fetch('/api/v1/config/ui', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(config),
  });
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

function queueConfigOperation(revision, operation, successMessage, failureMessage) {
  const result = configOperation.then(async () => {
    if (revision !== configRevision) return;
    showConfigState('Saving');
    const savedConfig = await operation();
    if (revision !== configRevision) return;
    uiConfig = savedConfig;
    applyUiConfig(uiConfig);
    renderConfigForm();
    showConfigState(successMessage);
  }).catch((error) => {
    if (revision === configRevision) {
      showConfigState(failureMessage, 'error');
    }
    throw error;
  });
  configOperation = result.catch(() => {});
  return result;
}

function scheduleUiConfigSave() {
  clearTimeout(configSaveTimer);
  const revision = ++configRevision;
  showConfigState('Saving');
  configSaveTimer = setTimeout(() => {
    configSaveTimer = undefined;
    const config = JSON.parse(JSON.stringify(uiConfig));
    queueConfigOperation(
      revision,
      () => writeUiConfig(config),
      'Saved on device',
      'Save failed',
    ).catch(() => {});
  }, CONFIG_SAVE_DELAY_MS);
}

function saveUiConfig(config, successMessage = 'Saved on device', failureMessage = 'Save failed') {
  clearTimeout(configSaveTimer);
  configSaveTimer = undefined;
  const revision = ++configRevision;
  const snapshot = JSON.parse(JSON.stringify(config));
  return queueConfigOperation(
    revision,
    () => writeUiConfig(snapshot),
    successMessage,
    failureMessage,
  );
}

async function loadUiConfig() {
  try {
    const response = await fetch('/api/v1/config/ui', { cache: 'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    uiConfig = await response.json();
    applyUiConfig(uiConfig);
    renderConfigForm();
    showConfigState('Stored on device');
  } catch (error) {
    showConfigState('Configuration unavailable', 'error');
    setStatusRefreshInterval(DEFAULT_STATUS_REFRESH_INTERVAL_MS);
  }
}

configForm.addEventListener('change', () => {
  if (!uiConfig) return;
  applyUiConfig(readConfigForm());
  scheduleUiConfigSave();
});

sht45Enabled.addEventListener('change', () => {
  if (!uiConfig) return;
  uiConfig.sensors.sht45.enabled = sht45Enabled.checked;
  applyUiConfig(uiConfig);
  scheduleUiConfigSave();
});

soilProbeEnabled.addEventListener('change', () => {
  if (!uiConfig) return;
  uiConfig.sensors.soil_probe.enabled = soilProbeEnabled.checked;
  applyUiConfig(uiConfig);
  scheduleUiConfigSave();
});

document.getElementById('reset-config').addEventListener('click', async () => {
  if (!confirm('Reset all saved settings to defaults?')) return;
  clearTimeout(configSaveTimer);
  configSaveTimer = undefined;
  const revision = ++configRevision;
  try {
    await queueConfigOperation(revision, async () => {
      const response = await fetch('/api/v1/config/ui/reset', { method: 'POST' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return response.json();
    }, 'Defaults restored', 'Reset failed');
  } catch (error) {}
});

document.getElementById('export-config').addEventListener('click', () => {
  if (!uiConfig) return;
  const link = document.createElement('a');
  link.href = URL.createObjectURL(new Blob([JSON.stringify(readConfigForm(), null, 2)], { type: 'application/json' }));
  link.download = 'open-plant-pulse-ui.json';
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(link.href), 0);
});

document.getElementById('import-config').addEventListener('change', async (event) => {
  const [file] = event.target.files;
  if (!file) return;
  try {
    await saveUiConfig(JSON.parse(await file.text()), 'Imported and saved', 'Import failed');
  } catch (error) {
    if (error instanceof SyntaxError) {
      showConfigState('Import failed', 'error');
    }
  } finally {
    event.target.value = '';
  }
});

restartButton.addEventListener('click', async () => {
  if (!window.confirm('Restart the device now? The console will be unavailable briefly.')) return;

  restartButton.disabled = true;
  restartState.className = 'action-state pending';
  restartState.textContent = 'Scheduling';
  try {
    const response = await fetch('/api/v1/restart', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirm: 'restart' }),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    restartState.textContent = 'Restarting';

    const deadline = Date.now() + 30000;
    let observedDisconnect = false;
    while (Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 1000));
      try {
        const statusResponse = await fetch('/status', { cache: 'no-store' });
        if (!statusResponse.ok) throw new Error(`HTTP ${statusResponse.status}`);
        const status = await statusResponse.json();
        if (observedDisconnect || status.uptime_s < 10) {
          restartState.className = 'action-state success';
          restartState.textContent = 'Restarted';
          restartButton.disabled = false;
          await refreshStatus();
          await refreshLogs();
          return;
        }
      } catch (error) {
        observedDisconnect = true;
        restartState.textContent = 'Reconnecting';
      }
    }
    throw new Error('Restart timed out');
  } catch (error) {
    restartState.className = 'action-state error';
    restartState.textContent = 'Restart failed';
    restartButton.disabled = false;
  }
});

function renderForceReport(status) {
  const state = status?.force_report_state || 'idle';
  const request = Number.isInteger(status?.force_report_request_id) && status.force_report_request_id > 0
    ? `Request ${status.force_report_request_id}`
    : 'Report';
  const packet = Number.isInteger(status?.force_report_report_id)
    ? ` · report ${status.force_report_report_id}`
    : '';
  forceReportButton.disabled = state === 'queued' || state === 'reporting';
  if (state === 'queued') {
    forceReportState.className = 'action-state pending';
    forceReportState.textContent = `${request} queued`;
  } else if (state === 'reporting') {
    forceReportState.className = 'action-state pending';
    forceReportState.textContent = `${request}${packet} sent · waiting for Hub`;
  } else if (state === 'acknowledged') {
    forceReportState.className = 'action-state success';
    forceReportState.textContent = `${request}${packet} · report stored and Hub acknowledged`;
  } else if (state === 'unacknowledged') {
    forceReportState.className = 'action-state error';
    forceReportState.textContent = `${request}${packet} · no Hub acknowledgement`;
  } else if (state === 'failed') {
    forceReportState.className = 'action-state error';
    forceReportState.textContent = `${request} failed · ${status.force_report_error || 'unknown error'}`;
  } else {
    forceReportState.className = 'action-state';
    forceReportState.textContent = 'Ready';
  }
}

forceReportButton.addEventListener('click', async () => {
  forceReportButton.disabled = true;
  forceReportState.className = 'action-state pending';
  forceReportState.textContent = 'Queuing report';
  try {
    const response = await fetch('/api/v1/reports/force', { method: 'POST' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const request = await response.json();
    const deadline = Date.now() + 40000;
    while (Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 400));
      const statusResponse = await fetch('/status', { cache: 'no-store' });
      if (!statusResponse.ok) throw new Error(`HTTP ${statusResponse.status}`);
      const status = await statusResponse.json();
      renderForceReport(status);
      if (status.force_report_request_id === request.request_id
          && ['acknowledged', 'unacknowledged', 'failed'].includes(status.force_report_state)) {
        forceReportButton.disabled = false;
        return;
      }
    }
    throw new Error('Acknowledgement timed out');
  } catch (error) {
    forceReportState.className = 'action-state error';
    forceReportState.textContent = 'Report request failed';
    forceReportButton.disabled = false;
  }
});

matchMedia('(prefers-color-scheme: light)').addEventListener('change', () => {
  if (uiConfig?.theme.mode === 'system') applyUiConfig(uiConfig);
});

function formatDuration(seconds) {
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days) return `${days}d ${hours}h`;
  if (hours) return `${hours}h ${minutes}m`;
  return `${minutes}m ${seconds % 60}s`;
}

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  return `${(bytes / 1024).toFixed(1)} KiB`;
}

function calculateAbsoluteHumidity(temperatureC, relativeHumidityPercent) {
  if (!Number.isFinite(temperatureC) || !Number.isFinite(relativeHumidityPercent)
      || temperatureC <= -243.5) return null;
  const humidityFraction = Math.min(100, Math.max(0, relativeHumidityPercent)) / 100;
  const saturationVaporPressureHpa =
    6.112 * Math.exp((17.67 * temperatureC) / (temperatureC + 243.5));
  return 216.7 * humidityFraction * saturationVaporPressureHpa / (temperatureC + 273.15);
}

function renderSht45(status) {
  const enabled = uiConfig?.sensors?.sht45?.enabled ?? Boolean(status?.sht45_enabled);
  const available = enabled && Boolean(status?.sht45_available)
    && Number.isFinite(status.air_temperature_c)
    && Number.isFinite(status.air_humidity_percent);
  const stateClass = `state ${!enabled ? 'disabled' : (available ? 'available' : 'unavailable')}`;
  const stateText = !enabled ? 'Disabled' : (available ? 'Online' : 'Unavailable');
  // The section stays put when the sensor drops out, so a missed sample
  // reads as "Unavailable" rather than as the whole sensor vanishing.
  const sensorState = document.getElementById('overview-sht45-state');
  sensorState.className = stateClass;
  sensorState.textContent = stateText;

  if (!available) {
    for (const id of ['overview-air-temperature', 'overview-air-humidity',
      'overview-absolute-humidity']) {
      document.getElementById(id).textContent = '--';
    }
    document.getElementById('overview-sht45-age').textContent = 'No current sample';
    return;
  }

  document.getElementById('overview-air-temperature').textContent =
    `${status.air_temperature_c.toFixed(2)} C`;
  document.getElementById('overview-air-humidity').textContent =
    `${status.air_humidity_percent.toFixed(2)}% RH`;
  const absoluteHumidity = calculateAbsoluteHumidity(
    status.air_temperature_c,
    status.air_humidity_percent,
  );
  document.getElementById('overview-absolute-humidity').textContent =
    Number.isFinite(absoluteHumidity) ? `${absoluteHumidity.toFixed(2)} g/m³` : 'Unavailable';
  document.getElementById('overview-sht45-age').textContent =
    Number.isFinite(status.air_sample_age_ms)
      ? `Sampled ${Math.max(0, Math.floor(status.air_sample_age_ms / 1000))}s ago`
      : 'Latest sample';
}

function renderSoil(status) {
  // The switch decides as soon as it moves; the device's own report catches up
  // on the next status poll.
  const enabled = uiConfig?.sensors?.soil_probe?.enabled ?? Boolean(status?.soil_probe_enabled);
  const available = enabled && Boolean(status?.soil_available);
  const state = document.getElementById('overview-soil-state');
  state.className = `state ${!enabled ? 'disabled' : (available ? 'available' : 'unavailable')}`;
  state.textContent = !enabled ? 'Disabled' : (available ? 'Online' : 'Unavailable');
  const ids = ['overview-soil-moisture', 'overview-soil-temperature',
    'overview-soil-conductivity', 'overview-soil-ph', 'overview-soil-nitrogen',
    'overview-soil-phosphorus', 'overview-soil-potassium'];
  if (!available) {
    for (const id of ids) document.getElementById(id).textContent = '--';
    document.getElementById('overview-soil-age').textContent =
      !enabled ? (status?.soil_probe_powered === false ? 'Probe powered off' : 'Sampling switched off')
        : (status?.soil_probe_error ? `Last read failed: ${status.soil_probe_error}` : 'No current sample');
    return;
  }

  const values = [
    ['overview-soil-moisture', `${status.soil_moisture_percent.toFixed(1)}%`],
    ['overview-soil-temperature', `${status.soil_temperature_c.toFixed(1)} C`],
    ['overview-soil-conductivity', `${status.soil_conductivity_us_cm} µS/cm`],
    ['overview-soil-ph', status.soil_ph.toFixed(1)],
    ['overview-soil-nitrogen', `${status.soil_nitrogen_mg_kg} mg/kg`],
    ['overview-soil-phosphorus', `${status.soil_phosphorus_mg_kg} mg/kg`],
    ['overview-soil-potassium', `${status.soil_potassium_mg_kg} mg/kg`],
  ];
  for (const [id, text] of values) document.getElementById(id).textContent = text;
  document.getElementById('overview-soil-age').textContent =
    Number.isFinite(status.soil_sample_age_ms)
      ? `Sampled ${Math.max(0, Math.floor(status.soil_sample_age_ms / 1000))}s ago`
      : 'Latest sample';
}

function formatHours(hours) {
  if (!Number.isFinite(hours)) return '--';
  if (hours < 1) return `${Math.round(hours * 60)} min`;
  if (hours < 48) return `${hours.toFixed(1)} h`;
  return `${(hours / 24).toFixed(1)} days`;
}

function renderBattery(status) {
  const available = Boolean(status?.battery_available);
  const stateText = available
    ? status.battery_state.charAt(0).toUpperCase() + status.battery_state.slice(1)
    : 'Unavailable';
  const state = document.getElementById('overview-battery-state');
  state.className = `state ${available ? 'available' : 'unavailable'}`;
  state.textContent = stateText;
  const text = (id, value) => { document.getElementById(id).textContent = value; };
  if (!available) {
    for (const id of ['overview-battery-percent', 'overview-battery-voltage',
      'overview-battery-current', 'overview-battery-power', 'overview-battery-time']) {
      text(id, '--');
    }
    for (const id of ['overview-battery-percent-note', 'overview-battery-voltage-note',
      'overview-battery-current-note', 'overview-battery-time-note']) {
      text(id, '');
    }
    text('overview-battery-time-label', 'Time left');
    text('overview-battery-age', status?.battery_error
      ? `Last read failed: ${status.battery_error}` : 'No current sample');
    return;
  }

  text('overview-battery-percent', `${status.battery_percent.toFixed(0)}%`);
  // Until a full charge has pinned it, the level began as a voltage guess.
  text('overview-battery-percent-note', status.battery_calibrated
    ? 'Counted since last full charge'
    : 'Estimate; exact after a full charge');
  text('overview-battery-voltage', `${status.battery_voltage_v.toFixed(3)} V`);
  text('overview-battery-voltage-note',
    `Voltage alone suggests ${status.battery_voltage_percent.toFixed(0)}%`);
  const current = status.battery_current_ma;
  text('overview-battery-current', `${current > 0 ? '+' : ''}${current.toFixed(1)} mA`);
  text('overview-battery-current-note', status.battery_overflow
    ? 'Over the shunt range'
    : `5 min average ${status.battery_average_current_ma.toFixed(1)} mA`);
  text('overview-battery-power', `${Math.abs(status.battery_power_mw).toFixed(0)} mW`);
  if (Number.isFinite(status.battery_hours_to_empty)) {
    text('overview-battery-time-label', 'Until empty');
    text('overview-battery-time', formatHours(status.battery_hours_to_empty));
    text('overview-battery-time-note', 'At the last 5 minutes\' draw');
  } else if (Number.isFinite(status.battery_hours_to_full)) {
    text('overview-battery-time-label', 'Until full');
    text('overview-battery-time', formatHours(status.battery_hours_to_full));
    text('overview-battery-time-note', 'Optimistic near the end');
  } else {
    text('overview-battery-time-label', 'Time left');
    text('overview-battery-time', '--');
    text('overview-battery-time-note', 'Neither charging nor discharging');
  }
  text('overview-battery-age',
    `Sampled ${Math.max(0, Math.floor(status.battery_sample_age_ms / 1000))}s ago`);
}

function renderDeviceConfiguration(status) {
  const configured = Number.isInteger(status.device_config_revision)
    && status.device_config_revision > 0;
  const state = document.getElementById('device-config-state');
  state.className = `state ${configured ? 'available' : 'disabled'}`;
  state.textContent = configured ? `Revision ${status.device_config_revision}` : 'Not configured';
  document.getElementById('configured-plant-name').textContent =
    configured ? status.plant_name : 'Not assigned';
  document.getElementById('configured-room').textContent =
    configured && status.room ? status.room : 'Not assigned';
  const intervalSeconds = status.reporting_interval_seconds;
  const interval = intervalSeconds < 60
    ? `${intervalSeconds} second${intervalSeconds === 1 ? '' : 's'}`
    : `${intervalSeconds / 60} minute${intervalSeconds === 60 ? '' : 's'}`;
  document.getElementById('configured-reporting-interval').textContent =
    interval;
}

function timezoneName(timezone) {
  try {
    return new Intl.DateTimeFormat('en-US', {
      timeZone: timezone,
      timeZoneName: 'short',
    }).formatToParts(new Date()).find((part) => part.type === 'timeZoneName')?.value || timezone;
  } catch (error) {
    return timezone;
  }
}

function formatDateTime(value, separator = ' ') {
  if (typeof value !== 'string' && !(value instanceof Date)) return 'Unsynchronized';
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return 'Unsynchronized';
  const timezone = uiConfig?.clock?.timezone || 'UTC';
  try {
    const parts = Object.fromEntries(new Intl.DateTimeFormat('en-CA', {
      timeZone: timezone,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hourCycle: 'h23',
    }).formatToParts(date).map((part) => [part.type, part.value]));
    return `${parts.year}-${parts.month}-${parts.day}${separator}${parts.hour}:${parts.minute}:${parts.second}`;
  } catch (error) {
    return `${date.toISOString().slice(0, 10)}${separator}${date.toISOString().slice(11, 19)}`;
  }
}

function updateClockAnchor(status) {
  if (!status.clock_valid || !Number.isFinite(status.unix_time_s)) {
    clockAnchorUnixMs = undefined;
    clockAnchorMonotonicMs = undefined;
    return;
  }

  const receivedMonotonicMs = performance.now();
  const receivedUnixMs = status.unix_time_s * 1000;
  const projectedUnixMs = clockAnchorUnixMs
    + receivedMonotonicMs
    - clockAnchorMonotonicMs;
  if (!Number.isFinite(projectedUnixMs)
      || Math.abs(receivedUnixMs - projectedUnixMs) >= CLOCK_REANCHOR_THRESHOLD_MS) {
    clockAnchorUnixMs = receivedUnixMs;
    clockAnchorMonotonicMs = receivedMonotonicMs;
  }
  if (clockRenderTimer === undefined) {
    clockRenderTimer = setInterval(() => {
      if (latestStatus) renderDateTime(latestStatus);
    }, CLOCK_RENDER_INTERVAL_MS);
  }
}

function renderDateTime(status) {
  let dateTime = status.date_time_utc;
  const timezone = uiConfig?.clock?.timezone || 'UTC';
  if (status.clock_valid
      && Number.isFinite(clockAnchorUnixMs)
      && Number.isFinite(clockAnchorMonotonicMs)) {
    dateTime = new Date(clockAnchorUnixMs + performance.now() - clockAnchorMonotonicMs);
  }

  document.getElementById('date-time').textContent = status.clock_valid
    ? `${formatDateTime(dateTime, '\n')} ${timezoneName(timezone)}`
    : 'Unsynchronized';
}

function renderClock(status) {
  const timezone = uiConfig?.clock?.timezone || 'UTC';
  renderDateTime(status);
  document.getElementById('rtc-status').textContent = status.clock_sync_state || 'Unknown';

  const details = [];
  if (typeof status.last_clock_sync_utc === 'string') {
    details.push(`last sync ${formatDateTime(status.last_clock_sync_utc)} ${timezoneName(timezone)}`);
  } else if (Number.isFinite(status.clock_sync_failures) && status.clock_sync_failures > 0) {
    details.push(`${status.clock_sync_failures} failed sync attempt${status.clock_sync_failures === 1 ? '' : 's'}`);
  } else {
    details.push('awaiting first sync');
  }
  document.getElementById('rtc-detail').textContent = details.join(' · ');
}

async function refreshStatus() {
  try {
    const response = await fetch('/status', { cache: 'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const status = await response.json();
    latestStatus = status;
    updateClockAnchor(status);
    const online = Boolean(status.connected);
    const version = typeof status.firmware_version === 'string' && status.firmware_version
      ? status.firmware_version
      : 'unknown';

    // Name the tab after the plant, because somebody with several consoles open
    // is trying to tell them apart, and the product name is the same on all of
    // them. An unclaimed sensor has no plant yet, so it falls back to itself.
    const plant = typeof status.plant_name === 'string' ? status.plant_name.trim() : '';
    document.title = plant
      ? `sensor - ${plant}`
      : `sensor - ${status.mac || 'unclaimed'}`;
    firmwareVersion.textContent = `Sensor console · Firmware v${version}`;
    connection.className = `connection ${online ? 'online' : 'offline'}`;
    connectionLabel.textContent = online ? 'Device online' : 'Wi-Fi disconnected';
    document.getElementById('device-state').textContent = online
      ? (status.ssid || 'Wi-Fi connected')
      : 'Disconnected';
    document.getElementById('device-address').textContent = status.ip || 'No address yet';
    document.getElementById('device-mac').textContent = status.mac || 'MAC unavailable';
    // Present only while this sensor belongs to nobody. The firmware stops
    // sending it once a hub is bonded, so the card goes with it.
    const pairingCard = document.getElementById('pairing-card');
    const pairingCode = typeof status.pairing_code === 'string' ? status.pairing_code : '';
    pairingCard.hidden = pairingCode === '';
    if (pairingCode !== '') {
      document.getElementById('pairing-code').textContent = pairingCode;
    }
    const usbPower = status.power_source === 'usb' || status.usb_connected === true;
    document.getElementById('power-source').textContent = usbPower ? 'USB' : 'Battery (inferred)';
    document.getElementById('power-detail').textContent = usbPower
      ? 'USB data host detected'
      : 'No USB data host detected';
    document.getElementById('uptime').textContent = formatDuration(status.uptime_s);
    document.getElementById('heap').textContent = formatBytes(status.free_heap);
    document.getElementById('chip-temperature').textContent =
      Number.isFinite(status.chip_temperature_c) ? `${status.chip_temperature_c.toFixed(1)} C` : 'Unavailable';
    renderClock(status);
    renderDeviceConfiguration(status);
    renderSht45(status);
    renderSoil(status);
    renderBattery(status);
    renderForceReport(status);
    document.getElementById('last-updated').textContent = `Updated ${new Date().toLocaleTimeString()}`;
  } catch (error) {
    connection.className = 'connection offline';
    connectionLabel.textContent = 'Device unreachable';
    document.getElementById('device-state').textContent = 'Unreachable';
    document.getElementById('power-source').textContent = 'Unavailable';
    document.getElementById('power-detail').textContent = 'Device unreachable';
    document.getElementById('last-updated').textContent = 'Update failed';
    renderSht45(null);
    renderSoil(null);
    renderBattery(null);
  }
}

function setStatusRefreshInterval(intervalMs) {
  if (intervalMs === activeStatusRefreshIntervalMs) return;
  clearInterval(statusRefreshTimer);
  statusRefreshTimer = undefined;
  activeStatusRefreshIntervalMs = intervalMs;
  if (intervalMs > 0) {
    statusRefreshTimer = setInterval(refreshStatus, intervalMs);
  }
}

async function refreshLogs() {
  try {
    const response = await fetch('/logs', { cache: 'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const text = await response.text();
    logConsole.textContent = text || 'No logs captured yet.';
    if (activeView === 'overview') logConsole.scrollTop = logConsole.scrollHeight;
  } catch (error) {
    logConsole.textContent = 'Log stream unavailable.';
  }
}

refreshStatus();
refreshLogs();
loadUiConfig();
setInterval(refreshLogs, 1000);
