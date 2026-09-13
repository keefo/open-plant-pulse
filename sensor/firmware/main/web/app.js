const views = document.querySelectorAll('.view');
const navItems = document.querySelectorAll('.nav-item');
const connection = document.getElementById('connection');
const connectionLabel = document.getElementById('connection-label');
const logConsole = document.getElementById('log-console');
const logPreview = document.getElementById('log-preview');
const configForm = document.getElementById('ui-config-form');
const configState = document.getElementById('config-state');
const restartButton = document.getElementById('restart-device');
const restartState = document.getElementById('restart-state');
const firmwareVersion = document.getElementById('firmware-version');
const cardLabels = {
  device: 'Device',
  uptime: 'Uptime',
  heap: 'Free heap',
  mac: 'Station MAC',
  chip_temperature: 'Chip temperature',
};
let activeView = 'overview';
let uiConfig;

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
}

function renderConfigForm() {
  if (!uiConfig) return;
  configForm.elements['theme-mode'].value = uiConfig.theme.mode;
  configForm.elements.accent.value = uiConfig.theme.accent;
  configForm.elements.density.value = uiConfig.dashboard.density;

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
    visible.addEventListener('change', () => {
      const visibleCount = uiConfig.dashboard.cards.filter((item) => item.visible).length;
      if (!visible.checked && visibleCount === 1) {
        visible.checked = true;
        configState.className = 'config-state error';
        configState.textContent = 'Keep at least one card visible';
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
}

function readConfigForm() {
  uiConfig.theme.mode = configForm.elements['theme-mode'].value;
  uiConfig.theme.accent = configForm.elements.accent.value;
  uiConfig.dashboard.density = configForm.elements.density.value;
  return uiConfig;
}

async function saveUiConfig(config, successMessage = 'Saved on device') {
  configState.className = 'config-state';
  configState.textContent = 'Saving';
  const response = await fetch('/api/v1/config/ui', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(config),
  });
  if (!response.ok) throw new Error(await response.text());
  uiConfig = await response.json();
  applyUiConfig(uiConfig);
  renderConfigForm();
  configState.textContent = successMessage;
}

async function loadUiConfig() {
  try {
    const response = await fetch('/api/v1/config/ui', { cache: 'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    uiConfig = await response.json();
    applyUiConfig(uiConfig);
    renderConfigForm();
    configState.textContent = 'Stored on device';
  } catch (error) {
    configState.className = 'config-state error';
    configState.textContent = 'Configuration unavailable';
  }
}

configForm.addEventListener('change', () => {
  if (!uiConfig) return;
  applyUiConfig(readConfigForm());
  configState.textContent = 'Unsaved changes';
});

configForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    await saveUiConfig(readConfigForm());
  } catch (error) {
    configState.className = 'config-state error';
    configState.textContent = 'Save failed';
  }
});

document.getElementById('reset-config').addEventListener('click', async () => {
  if (!confirm('Reset appearance and dashboard layout?')) return;
  try {
    const response = await fetch('/api/v1/config/ui/reset', { method: 'POST' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    uiConfig = await response.json();
    applyUiConfig(uiConfig);
    renderConfigForm();
    configState.textContent = 'Defaults restored';
  } catch (error) {
    configState.className = 'config-state error';
    configState.textContent = 'Reset failed';
  }
});

document.getElementById('export-config').addEventListener('click', () => {
  if (!uiConfig) return;
  const link = document.createElement('a');
  link.href = URL.createObjectURL(new Blob([JSON.stringify(readConfigForm(), null, 2)], { type: 'application/json' }));
  link.download = 'open-plant-pulse-ui.json';
  link.click();
  URL.revokeObjectURL(link.href);
});

document.getElementById('import-config').addEventListener('change', async (event) => {
  const [file] = event.target.files;
  if (!file) return;
  try {
    await saveUiConfig(JSON.parse(await file.text()), 'Imported and saved');
  } catch (error) {
    configState.className = 'config-state error';
    configState.textContent = 'Import failed';
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

async function refreshStatus() {
  try {
    const response = await fetch('/status', { cache: 'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const status = await response.json();
    const online = Boolean(status.connected);
    const version = typeof status.firmware_version === 'string' && status.firmware_version
      ? status.firmware_version
      : 'unknown';

    document.title = `Open Plant Pulse · v${version}`;
    firmwareVersion.textContent = `Sensor console · Firmware v${version}`;
    connection.className = `connection ${online ? 'online' : 'offline'}`;
    connectionLabel.textContent = online ? 'Device online' : 'Wi-Fi disconnected';
    document.getElementById('device-state').textContent = online ? 'Online' : 'Disconnected';
    document.getElementById('device-address').textContent = status.ip;
    document.getElementById('uptime').textContent = formatDuration(status.uptime_s);
    document.getElementById('heap').textContent = formatBytes(status.free_heap);
    document.getElementById('mac').textContent = status.mac;
    document.getElementById('chip-temperature').textContent =
      Number.isFinite(status.chip_temperature_c) ? `${status.chip_temperature_c.toFixed(1)} C` : 'Unavailable';
    document.getElementById('last-updated').textContent = `Updated ${new Date().toLocaleTimeString()}`;
  } catch (error) {
    connection.className = 'connection offline';
    connectionLabel.textContent = 'Device unreachable';
    document.getElementById('device-state').textContent = 'Unreachable';
    document.getElementById('last-updated').textContent = 'Update failed';
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
