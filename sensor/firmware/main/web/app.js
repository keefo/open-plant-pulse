const views = document.querySelectorAll('.view');
const navItems = document.querySelectorAll('.nav-item');
const connection = document.getElementById('connection');
const connectionLabel = document.getElementById('connection-label');
const logConsole = document.getElementById('log-console');
const logPreview = document.getElementById('log-preview');
const configForm = document.getElementById('ui-config-form');
const configState = document.getElementById('config-state');
const sensorConfigState = document.getElementById('sensor-config-state');
const sht45Enabled = document.getElementById('sht45-enabled');
const soilProbeEnabled = document.getElementById('soil-probe-enabled');
const restartButton = document.getElementById('restart-device');
const restartState = document.getElementById('restart-state');
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
let activeView = 'overview';
let uiConfig;
let configRevision = 0;
let configSaveTimer;
let configOperation = Promise.resolve();
let latestStatus;
let clockAnchorUnixMs;
let clockAnchorMonotonicMs;
let clockRenderTimer;

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
document.querySelectorAll('[data-open-view]').forEach((item) => {
  item.addEventListener('click', () => showView(item.dataset.openView));
});

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
  const soilProbeState = document.getElementById('soil-probe-state');
  soilProbeState.className = `state ${soilProbeEnabled.checked ? 'unavailable' : 'disabled'}`;
  soilProbeState.textContent = soilProbeEnabled.checked ? 'Not implemented' : 'Disabled';
  if (latestStatus) {
    renderClock(latestStatus);
    renderSht45(latestStatus);
  }
}

function showConfigState(message, state = '') {
  [configState, sensorConfigState].forEach((element) => {
    element.className = element === configState ? `config-state ${state}`.trim() : `empty-note ${state}`.trim();
    element.textContent = message;
  });
}

function renderConfigForm() {
  if (!uiConfig) return;
  configForm.elements['theme-mode'].value = uiConfig.theme.mode;
  configForm.elements.accent.value = uiConfig.theme.accent;
  configForm.elements.density.value = uiConfig.dashboard.density;
  populateTimezoneSelect(uiConfig.clock.timezone);

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
  if (!confirm('Reset appearance and dashboard layout?')) return;
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

function renderSht45(status) {
  const enabled = uiConfig?.sensors?.sht45?.enabled ?? Boolean(status?.sht45_enabled);
  const available = enabled && Boolean(status?.sht45_available)
    && Number.isFinite(status.air_temperature_c)
    && Number.isFinite(status.air_humidity_percent);
  const overviewSection = document.getElementById('overview-sht45');
  const sensorState = document.getElementById('sht45-state');

  overviewSection.hidden = !available;
  sensorState.className = `state ${!enabled ? 'disabled' : (available ? 'available' : 'unavailable')}`;
  sensorState.textContent = !enabled ? 'Disabled' : (available ? 'Online' : 'Unavailable');

  if (!available) return;

  document.getElementById('overview-air-temperature').textContent =
    `${status.air_temperature_c.toFixed(2)} C`;
  document.getElementById('overview-air-humidity').textContent =
    `${status.air_humidity_percent.toFixed(2)}% RH`;
  document.getElementById('overview-sht45-age').textContent =
    Number.isFinite(status.air_sample_age_ms)
      ? `Sampled ${Math.max(0, Math.floor(status.air_sample_age_ms / 1000))}s ago`
      : 'Latest sample';
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
  if (status.clock_valid
      && Number.isFinite(clockAnchorUnixMs)
      && Number.isFinite(clockAnchorMonotonicMs)) {
    dateTime = new Date(clockAnchorUnixMs + performance.now() - clockAnchorMonotonicMs);
  }

  document.getElementById('date-time').textContent = status.clock_valid
    ? formatDateTime(dateTime, '\n')
    : 'Unsynchronized';
}

function renderClock(status) {
  const timezone = uiConfig?.clock?.timezone || 'UTC';
  renderDateTime(status);
  document.getElementById('date-time-zone').textContent =
    `System wall clock · ${timezoneName(timezone)}`;
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

    document.title = `Open Plant Pulse · v${version}`;
    firmwareVersion.textContent = `Sensor console · Firmware v${version}`;
    connection.className = `connection ${online ? 'online' : 'offline'}`;
    connectionLabel.textContent = online ? 'Device online' : 'Wi-Fi disconnected';
    document.getElementById('device-state').textContent = online
      ? (status.ssid || 'Wi-Fi connected')
      : 'Disconnected';
    document.getElementById('device-address').textContent = status.ip || 'No address yet';
    document.getElementById('device-mac').textContent = status.mac || 'MAC unavailable';
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
    renderSht45(status);
    document.getElementById('last-updated').textContent = `Updated ${new Date().toLocaleTimeString()}`;
  } catch (error) {
    connection.className = 'connection offline';
    connectionLabel.textContent = 'Device unreachable';
    document.getElementById('device-state').textContent = 'Unreachable';
    document.getElementById('power-source').textContent = 'Unavailable';
    document.getElementById('power-detail').textContent = 'Device unreachable';
    document.getElementById('last-updated').textContent = 'Update failed';
    renderSht45(null);
  }
}

async function refreshLogs() {
  try {
    const response = await fetch('/logs', { cache: 'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const text = await response.text();
    const lines = text.trimEnd().split('\n');
    logPreview.textContent = lines.slice(-8).join('\n') || 'No logs captured yet.';
    logConsole.textContent = text || 'No logs captured yet.';
    if (activeView === 'logs') logConsole.scrollTop = logConsole.scrollHeight;
  } catch (error) {
    logPreview.textContent = 'Log stream unavailable.';
    logConsole.textContent = 'Log stream unavailable.';
  }
}

refreshStatus();
refreshLogs();
loadUiConfig();
setInterval(refreshStatus, 2000);
setInterval(refreshLogs, 1000);
