/* setup.js */

let currentDeviceType = 'ossm';

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
const ossmPanel = document.getElementById('setup-ossm-panel');
const coyotePanel = document.getElementById('setup-coyote-panel');
const setupPanelTitle = document.getElementById('setup-panel-title');

async function loadDeviceType() {
  try {
    const res = await fetch('/api/device/types');
    const data = await res.json();
    if (data.ok) {
      currentDeviceType = data.active || 'ossm';
      deviceTypeSelect.value = currentDeviceType;
      deviceTypeStatus.textContent = currentDeviceType.toUpperCase();
      updateDevicePanels();
    }
  } catch (err) {
    console.warn('Failed to load device type', err);
  }
}

function updateDevicePanels() {
  if (currentDeviceType === 'ossm') {
    ossmPanel.style.display = '';
    coyotePanel.style.display = 'none';
    setupPanelTitle.textContent = 'Position';
  } else {
    ossmPanel.style.display = 'none';
    coyotePanel.style.display = '';
    setupPanelTitle.textContent = 'Coyote Status';
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

// ── OSSM Connection ──────────────────────────────────────────────────────────

const setupConnectBtn = document.getElementById('setup-connect');
if (setupConnectBtn) {
  setupConnectBtn.addEventListener('click', async () => {
    const btn = setupConnectBtn;
    if (btn.dataset.connected === 'true') {
      try {
        await fetch('/api/device/disconnect', { method: 'POST' });
        window.App.closeDeviceStream();
        btn.textContent = 'Connect';
        btn.classList.remove('btn-danger');
        btn.dataset.connected = 'false';
        document.getElementById('device-conn-status').textContent = 'Offline';
      } catch (err) {
        window.App.showError('Disconnect failed: ' + err.message);
      }
      return;
    }

    const url = document.getElementById('device-url').value.trim();
    if (!url) return;

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
        document.getElementById('device-conn-status').textContent = 'Connected';
        window.App.openDeviceStream();
      } else {
        window.App.showError('Device connect failed: ' + (data.error || 'unknown'));
      }
    } catch (err) {
      window.App.showError('Device connect error: ' + err.message);
    }
  });
}

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

if (coyoteConnectBtn) {
  coyoteConnectBtn.addEventListener('click', async () => {
    const btn = coyoteConnectBtn;
    if (btn.dataset.connected === 'true') {
      try {
        await fetch('/api/device/disconnect', { method: 'POST' });
        window.App.closeDeviceStream();
        btn.textContent = 'Connect';
        btn.classList.remove('btn-danger');
        btn.dataset.connected = 'false';
        coyoteConnStatus.textContent = 'Offline';
        coyoteDot.className = 'dot';
      } catch (err) {
        window.App.showError('Disconnect failed: ' + err.message);
      }
      return;
    }

    const address = coyoteAddressInput.value.trim();
    if (!address) {
      window.App.showError('Please enter a BLE address or scan first');
      return;
    }

    try {
      const res = await fetch('/api/device/connect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: address }),
      });
      const data = await res.json();
      if (data.ok) {
        btn.textContent = 'Disconnect';
        btn.classList.add('btn-danger');
        btn.dataset.connected = 'true';
        coyoteConnStatus.textContent = 'Connected';
        coyoteDot.className = 'dot connected';
        // Also update the shared device-dot so app.js sees it
        const sharedDot = document.getElementById('device-dot');
        if (sharedDot) sharedDot.className = 'dot connected';
        const sharedLabel = document.getElementById('device-label');
        if (sharedLabel) sharedLabel.textContent = 'Connected';
        window.App.openDeviceStream();
        // Unlock tabs (Coyote has no homing, so unlock immediately)
        document.getElementById('tab-btn-manual').disabled = false;
        document.getElementById('tab-btn-ai').disabled = false;
        document.getElementById('tab-btn-custom').disabled = false;
      } else {
        window.App.showError('Coyote connect failed: ' + (data.error || 'unknown'));
      }
    } catch (err) {
      window.App.showError('Coyote connect error: ' + err.message);
    }
  });
}

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
setSetupStep(1);
