/* setup.js */

let currentDeviceType = 'none';

function setSetupStep(step) {
  for (let i = 1; i <= 3; i++) {
    const el = document.getElementById(`wizard-step-${i}`);
    if (!el) continue;
    el.classList.remove('completed', 'disabled');
    if (i < step) el.classList.add('completed');
    else if (i > step) el.classList.add('disabled');
  }
}

function markSetupComplete() {
  setSetupStep(4);
}

window.addEventListener('device-homed', () => {
  markSetupComplete();
});

async function waitForEngineReady(timeoutMs = 8000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    const res = await fetch('/api/device/state');
    const data = await res.json();
    if (data.ok && data.engineReady) return true;
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  return false;
}

// ── Device Type Switching ───────────────────────────────────────────────────

const deviceTypeSelect = document.getElementById('device-type-select');
const deviceTypeSetBtn = document.getElementById('device-type-set');
const deviceTypeStatus = document.getElementById('device-type-status');
const nonePanel = document.getElementById('setup-none-panel');
const ossmPanel = document.getElementById('setup-ossm-panel');
const ossmBlePanel = document.getElementById('setup-ossm-ble-panel');
const coyotePanel = document.getElementById('setup-coyote-panel');
const buttplugPanel = document.getElementById('setup-buttplug-panel');
const setupPanelTitle = document.getElementById('setup-panel-title');

const DEVICE_PANELS = {
  none: { panel: nonePanel, title: 'Position' },
  ossm: { panel: ossmPanel, title: 'Position' },
  ossm_ble: { panel: ossmBlePanel, title: 'Position (simulated)' },
  coyote: { panel: coyotePanel, title: 'Coyote Status' },
  buttplug: { panel: buttplugPanel, title: 'Position' },
};

async function loadDeviceType() {
  try {
    const res = await fetch('/api/device/types');
    const data = await res.json();
    if (data.ok) {
      currentDeviceType = data.active || 'none';
      deviceTypeSelect.value = currentDeviceType;
      deviceTypeStatus.textContent = currentDeviceType.toUpperCase();
      updateDevicePanels();
    }
  } catch (err) {
    console.warn('Failed to load device type', err);
  }
}

// Stock OSSM firmware has no usable funscript path — its streaming mode stalls
// on every direction reversal — so the driver drops stream commands and the tab
// stays shut rather than offering controls that do nothing.
const UNSUPPORTED_TABS = {
  ossm_ble: ['funscript'],
};

function updateDevicePanels() {
  window.App.setUnsupportedTabs(UNSUPPORTED_TABS[currentDeviceType] || []);
  const active = DEVICE_PANELS[currentDeviceType] || DEVICE_PANELS.none;
  Object.values(DEVICE_PANELS).forEach(({ panel }) => {
    if (panel) panel.style.display = 'none';
  });
  if (active.panel) active.panel.style.display = '';
  setupPanelTitle.textContent = active.title;

  if (currentDeviceType === 'buttplug') refreshButtplugDevices();
  // Nothing to connect or home without a toy, so hold nothing back. The stream
  // still runs: the driver echoes positions back so the gauge follows a
  // funscript or a pattern on screen.
  if (currentDeviceType === 'none') {
    markSetupComplete();
    window.App.unlockDeviceTabs();
    window.App.openDeviceStream();
  }
}

deviceTypeSetBtn.addEventListener('click', async () => {
  const type = deviceTypeSelect.value;
  try {
    const res = await fetch('/api/device/set', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ type }),
    });
    const data = await res.json();
    if (data.ok) {
      currentDeviceType = type;
      deviceTypeStatus.textContent = type.toUpperCase();
      // Real hardware starts disconnected, so gate the driving tabs again
      // until it connects (and, for OSSM, homes). updateDevicePanels reopens
      // them immediately for "none".
      if (type !== 'none') {
        window.App.closeDeviceStream();
        window.App.setDeviceStatus(false);
        window.App.lockDeviceTabs();
        setSetupStep(1);
      }
      updateDevicePanels();
      window.App.showInfo(`Switched to ${data.name}`);
      // Refresh manual tab visibility
      window.dispatchEvent(new CustomEvent('device-type-changed', { detail: type }));
    } else {
      window.App.showError(data.error || 'Failed to switch device');
    }
  } catch (err) {
    window.App.showError('Device switch error: ' + err.message);
  }
});

// ── OSSM Emulator ───────────────────────────────────────────────────────────

const launchLinuxEmuBtn = document.getElementById('setup-launch-linux-emulator');
if (launchLinuxEmuBtn) {
  launchLinuxEmuBtn.addEventListener('click', async () => {
    const statusEl = document.getElementById('setup-emulator-status');
    launchLinuxEmuBtn.disabled = true;
    if (statusEl) statusEl.textContent = 'Starting...';

    try {
      const res = await fetch('/api/device/serial_emulator/start', { method: 'POST' });
      const data = await res.json();
      if (!data.ok) {
        throw new Error(data.error || 'Could not launch emulator');
      }

      const controllerPort = (data.controller_port || '').trim();
      if (controllerPort) {
        document.getElementById('device-url').value = controllerPort;
      }

      if (statusEl) {
        statusEl.textContent = `Ready: emulator ${data.device_port} -> app ${data.controller_port}`;
      }
    } catch (err) {
      if (statusEl) statusEl.textContent = 'Failed';
      window.App.showError('Linux emulator start failed: ' + err.message);
    } finally {
      launchLinuxEmuBtn.disabled = false;
    }
  });
}

// ── Shared connect/disconnect wiring ────────────────────────────────────────

// All three device cards run the same button state machine: POST connect or
// disconnect, flip the label/class/dataset, and open or close the device
// stream. Only the status text, the indicator dot, and the post-connect extras
// differ, so those are the parameters.
//
// getUrl() returns the address to connect to, or null to abort silently once
// it has reported its own validation error.
function wireConnectToggle({ btn, label, statusEl, dotEl, getUrl, onConnect, onDisconnect }) {
  if (!btn) return;

  btn.addEventListener('click', async () => {
    if (btn.dataset.connected === 'true') {
      try {
        await fetch('/api/device/disconnect', { method: 'POST' });
        window.App.closeDeviceStream();
        btn.textContent = 'Connect';
        btn.classList.remove('btn-danger');
        btn.dataset.connected = 'false';
        if (statusEl) statusEl.textContent = 'Offline';
        if (dotEl) dotEl.className = 'dot';
        if (onDisconnect) onDisconnect();
      } catch (err) {
        window.App.showError('Disconnect failed: ' + err.message);
      }
      return;
    }

    const url = getUrl();
    if (url === null) return;

    try {
      const res = await fetch('/api/device/connect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url }),
      });
      const data = await res.json();
      if (data.ok) {
        btn.textContent = 'Disconnect';
        btn.classList.add('btn-danger');
        btn.dataset.connected = 'true';
        if (statusEl) statusEl.textContent = 'Connected';
        if (dotEl) dotEl.className = 'dot ok';
        window.App.openDeviceStream();
        if (onConnect) onConnect();
      } else {
        window.App.showError(`${label} connect failed: ` + (data.error || 'unknown'));
      }
    } catch (err) {
      window.App.showError(`${label} connect error: ` + err.message);
    }
  });
}

// ── OSSM Connection ──────────────────────────────────────────────────────────

// OSSM homes after connecting, so app.js unlocks the tabs on the homed event
// rather than here.
wireConnectToggle({
  btn: document.getElementById('setup-connect'),
  label: 'Device',
  statusEl: document.getElementById('device-conn-status'),
  getUrl: () => document.getElementById('device-url').value.trim() || null,
});

// ── Stock-firmware OSSM (BLE) ───────────────────────────────────────────────

const ossmBleScanBtn = document.getElementById('ossm-ble-scan');
const ossmBleConnectBtn = document.getElementById('ossm-ble-connect');
const ossmBleAddressInput = document.getElementById('ossm-ble-address');
const ossmBleScanResults = document.getElementById('ossm-ble-scan-results');
const ossmBleConnStatus = document.getElementById('ossm-ble-conn-status');
const ossmBleDot = document.getElementById('ossm-ble-dot');
const ossmBleFwState = document.getElementById('ossm-ble-fw-state');

// The address is remembered across restarts, so a returning user only has to
// press Connect.
async function loadOssmBleAddress() {
  if (!ossmBleAddressInput || ossmBleAddressInput.value.trim()) return;
  try {
    const res = await fetch('/api/settings');
    const data = await res.json();
    if (data.ok && data.ossm_ble_address) {
      ossmBleAddressInput.value = data.ossm_ble_address;
    }
  } catch (err) {
    console.warn('Failed to load OSSM BLE address', err);
  }
}
loadOssmBleAddress();

if (ossmBleScanBtn) {
  ossmBleScanBtn.addEventListener('click', async () => {
    ossmBleScanBtn.disabled = true;
    ossmBleScanResults.disabled = true;
    ossmBleScanResults.innerHTML = '<option>Scanning...</option>';

    try {
      const res = await fetch('/api/ossm_ble/scan');
      const data = await res.json();
      ossmBleScanResults.innerHTML = '';

      if (data.ok && data.devices && data.devices.length > 0) {
        data.devices.forEach((dev) => {
          const opt = document.createElement('option');
          opt.value = dev.address;
          opt.textContent = `${dev.name || 'OSSM'} (${dev.address})`;
          ossmBleScanResults.appendChild(opt);
        });
        ossmBleScanResults.disabled = false;
        ossmBleAddressInput.value = data.devices[0].address;
      } else {
        const opt = document.createElement('option');
        opt.textContent = data.ok ? 'No OSSM found' : (data.error || 'Scan failed');
        ossmBleScanResults.appendChild(opt);
      }
    } catch (err) {
      window.App.showError('OSSM BLE scan failed: ' + err.message);
    } finally {
      ossmBleScanBtn.disabled = false;
    }
  });
}

if (ossmBleScanResults) {
  ossmBleScanResults.addEventListener('change', () => {
    if (ossmBleScanResults.value) {
      ossmBleAddressInput.value = ossmBleScanResults.value;
    }
  });
}

wireConnectToggle({
  btn: ossmBleConnectBtn,
  label: 'OSSM',
  statusEl: ossmBleConnStatus,
  dotEl: ossmBleDot,
  getUrl: () => {
    const address = ossmBleAddressInput.value.trim();
    if (!address) {
      window.App.showError('Please enter a BLE address or scan first');
      return null;
    }
    return address;
  },
  onConnect: () => {
    window.App.setDeviceStatus(true);
    // The stock firmware homes on the way into a play mode rather than on
    // connect, so waiting for a homed event here would hang the wizard.
    window.App.unlockDeviceTabs();
  },
  onDisconnect: () => {
    if (ossmBleFwState) ossmBleFwState.textContent = '--';
  },
});

// Surface the firmware's own state name — it is the only way to see a
// preflight gate or a homing pass from the browser.
window.addEventListener('device-fw-state', (event) => {
  if (!ossmBleFwState || currentDeviceType !== 'ossm_ble') return;
  ossmBleFwState.textContent = event.detail || '--';
});

// ── Coyote BLE ──────────────────────────────────────────────────────────────

const coyoteScanBtn = document.getElementById('coyote-scan');
const coyoteConnectBtn = document.getElementById('coyote-connect');
const coyoteAddressInput = document.getElementById('coyote-ble-address');
const coyoteScanResults = document.getElementById('coyote-scan-results');
const coyoteConnStatus = document.getElementById('coyote-conn-status');
const coyoteDot = document.getElementById('coyote-dot');

if (coyoteScanBtn) {
  coyoteScanBtn.addEventListener('click', async () => {
    coyoteScanBtn.disabled = true;
    coyoteScanResults.disabled = true;
    coyoteScanResults.innerHTML = '<option>Scanning...</option>';

    try {
      const res = await fetch('/api/coyote/scan');
      const data = await res.json();
      coyoteScanResults.innerHTML = '';

      if (data.ok && data.devices && data.devices.length > 0) {
        data.devices.forEach(dev => {
          const opt = document.createElement('option');
          opt.value = dev.address;
          opt.textContent = `${dev.name || 'Unknown'} (${dev.address}) RSSI: ${dev.rssi}`;
          coyoteScanResults.appendChild(opt);
        });
        coyoteScanResults.disabled = false;
        // Auto-fill address from first result
        coyoteAddressInput.value = data.devices[0].address;
      } else {
        const opt = document.createElement('option');
        opt.value = '';
        opt.textContent = 'No devices found';
        coyoteScanResults.appendChild(opt);
      }
    } catch (err) {
      window.App.showError('BLE scan failed: ' + err.message);
    } finally {
      coyoteScanBtn.disabled = false;
    }
  });
}

if (coyoteScanResults) {
  coyoteScanResults.addEventListener('change', () => {
    if (coyoteScanResults.value) {
      coyoteAddressInput.value = coyoteScanResults.value;
    }
  });
}

wireConnectToggle({
  btn: coyoteConnectBtn,
  label: 'Coyote',
  statusEl: coyoteConnStatus,
  dotEl: coyoteDot,
  getUrl: () => {
    const address = coyoteAddressInput.value.trim();
    if (!address) {
      window.App.showError('Please enter a BLE address or scan first');
      return null;
    }
    return address;
  },
  onConnect: () => {
    window.App.setDeviceStatus(true);
    // Coyote has no homing step, so unlock immediately.
    window.App.unlockDeviceTabs();
  },
});

// ── Buttplug / Intiface ─────────────────────────────────────────────────────

const buttplugConnectBtn = document.getElementById('buttplug-connect');
const buttplugUrlInput = document.getElementById('buttplug-ws-url');
const buttplugConnStatus = document.getElementById('buttplug-conn-status');
const buttplugDot = document.getElementById('buttplug-dot');
const buttplugDeviceList = document.getElementById('buttplug-device-list');
const buttplugDeviceCount = document.getElementById('buttplug-device-count');

// Prefill from saved settings so the field matches the Settings tab.
async function loadButtplugUrl() {
  if (!buttplugUrlInput || buttplugUrlInput.value.trim()) return;
  try {
    const res = await fetch('/api/settings');
    const data = await res.json();
    if (data.ok && data.buttplug_ws_url) buttplugUrlInput.value = data.buttplug_ws_url;
  } catch (err) {
    console.warn('Failed to load Intiface URL', err);
  }
}

function renderButtplugDevices(devices) {
  if (!buttplugDeviceList) return;
  buttplugDeviceList.innerHTML = '';

  if (!devices || devices.length === 0) {
    buttplugDeviceCount.textContent = 'No toys found';
    const empty = document.createElement('p');
    empty.className = 'buttplug-empty';
    empty.textContent = 'Pair a toy in Intiface Central, then Scan.';
    buttplugDeviceList.appendChild(empty);
    return;
  }

  buttplugDeviceCount.textContent = `${devices.length} toy${devices.length === 1 ? '' : 's'}`;

  devices.forEach((dev) => {
    const row = document.createElement('label');
    row.className = 'buttplug-device';

    const box = document.createElement('input');
    box.type = 'checkbox';
    box.checked = dev.selected;
    box.dataset.index = dev.index;
    box.addEventListener('change', submitButtplugSelection);

    // Say what the toy will actually do, since the mapping isn't obvious.
    const kinds = [];
    if (dev.linear) kinds.push('stroker');
    if (dev.actuators && dev.actuators.length) kinds.push(dev.actuators.join('/').toLowerCase());

    const name = document.createElement('span');
    name.className = 'buttplug-device-name';
    name.textContent = dev.name;

    const meta = document.createElement('span');
    meta.className = 'buttplug-device-meta';
    meta.textContent = kinds.length ? kinds.join(' · ') : 'no drivable actuators';

    row.append(box, name, meta);
    buttplugDeviceList.appendChild(row);
  });
}

async function refreshButtplugDevices() {
  if (currentDeviceType !== 'buttplug') return;
  try {
    const res = await fetch('/api/device/buttplug/devices');
    const data = await res.json();
    if (!data.ok) return;

    const connected = data.connected;
    buttplugConnStatus.textContent = connected ? 'Connected' : 'Offline';
    buttplugDot.className = connected ? 'dot ok' : 'dot error';
    if (connected) {
      renderButtplugDevices(data.devices);
    } else {
      buttplugDeviceList.innerHTML = '';
      buttplugDeviceCount.textContent = 'Not connected';
    }
  } catch (err) {
    console.warn('Failed to list Buttplug devices', err);
  }
}

async function submitButtplugSelection() {
  const boxes = buttplugDeviceList.querySelectorAll('input[type=checkbox]');
  const checked = Array.from(boxes).filter((b) => b.checked).map((b) => Number(b.dataset.index));

  // All-checked is sent as an empty list, which the driver reads as "everything",
  // so newly discovered toys are picked up automatically.
  const allChecked = checked.length === boxes.length;

  try {
    await fetch('/api/device/buttplug/select', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ indices: allChecked ? [] : checked }),
    });
  } catch (err) {
    window.App.showError('Toy selection failed: ' + err.message);
  }
}

wireConnectToggle({
  btn: buttplugConnectBtn,
  label: 'Intiface',
  statusEl: buttplugConnStatus,
  dotEl: buttplugDot,
  getUrl: () => buttplugUrlInput.value.trim(),
  onConnect: () => {
    window.App.setDeviceStatus(true);
    // No homing step, so unlock the rest of the app immediately.
    window.App.unlockDeviceTabs();
    refreshButtplugDevices();
  },
  onDisconnect: () => {
    buttplugDeviceList.innerHTML = '';
    buttplugDeviceCount.textContent = 'Not connected';
  },
});

// Intiface scans on its own, so the inventory arrives unprompted over the
// device stream. The initial fetch covers toys already paired before we
// connected; everything after that is pushed.
window.addEventListener('buttplug-devices', (ev) => {
  if (currentDeviceType !== 'buttplug') return;
  renderButtplugDevices(ev.detail);
});

// ── Wizard actions (OSSM only) ───────────────────────────────────────────────

const jogBwd = document.getElementById('setup-jog-bwd');
if (jogBwd) jogBwd.addEventListener('click', () => window.App.sendDeviceCmd({ cmd: 'jogBwd' }));

const jogFwd = document.getElementById('setup-jog-fwd');
if (jogFwd) jogFwd.addEventListener('click', () => window.App.sendDeviceCmd({ cmd: 'jogFwd' }));

const jogBwd2 = document.getElementById('setup-jog-bwd-2');
if (jogBwd2) jogBwd2.addEventListener('click', () => window.App.sendDeviceCmd({ cmd: 'jogBwd' }));

const jogFwd2 = document.getElementById('setup-jog-fwd-2');
if (jogFwd2) jogFwd2.addEventListener('click', () => window.App.sendDeviceCmd({ cmd: 'jogFwd' }));

const setZero = document.getElementById('setup-set-zero');
if (setZero) {
  setZero.addEventListener('click', async () => {
    await window.App.sendDeviceCmd({ cmd: 'stop' });
    await window.App.sendDeviceCmd({ cmd: 'setZero' });
    setSetupStep(2);
  });
}

const setMax = document.getElementById('setup-set-max');
if (setMax) {
  setMax.addEventListener('click', async () => {
    await window.App.sendDeviceCmd({ cmd: 'stop' });
    await window.App.sendDeviceCmd({ cmd: 'setMax' });

    const ready = await waitForEngineReady(10000);
    if (!ready) {
      window.App.showError('Set Max timed out waiting for engine init. Try Set Max again.');
      return;
    }
    setSetupStep(3);
  });
}

const homeBtn = document.getElementById('setup-home');
if (homeBtn) {
  homeBtn.addEventListener('click', async () => {
    await window.App.sendDeviceCmd({ cmd: 'stop' });
    await window.App.sendDeviceCmd({ cmd: 'moveTo', pct: 0 });
  });
}

const setupStop = document.getElementById('setup-stop');
if (setupStop) {
  setupStop.addEventListener('click', () => window.App.sendDeviceCmd({ cmd: 'stop' }));
}

// ── Init ───────────────────────────────────────────────────────────────────

loadDeviceType();
loadButtplugUrl();
setSetupStep(1);
