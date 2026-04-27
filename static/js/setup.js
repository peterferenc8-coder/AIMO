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

      // Device emulator binds to device_port; controller should connect to the peer port.
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

// Connection toggle
document.getElementById('setup-connect').addEventListener('click', async () => {
  const btn = document.getElementById('setup-connect');
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

// Wizard actions
document.getElementById('setup-jog-bwd').addEventListener('click', () => window.App.sendDeviceCmd({ cmd: 'jogBwd' }));
document.getElementById('setup-jog-fwd').addEventListener('click', () => window.App.sendDeviceCmd({ cmd: 'jogFwd' }));
document.getElementById('setup-jog-bwd-2').addEventListener('click', () => window.App.sendDeviceCmd({ cmd: 'jogBwd' }));
document.getElementById('setup-jog-fwd-2').addEventListener('click', () => window.App.sendDeviceCmd({ cmd: 'jogFwd' }));

document.getElementById('setup-set-zero').addEventListener('click', async () => {
  await window.App.sendDeviceCmd({ cmd: 'stop' });
  await window.App.sendDeviceCmd({ cmd: 'setZero' });
  setSetupStep(2);
});

document.getElementById('setup-set-max').addEventListener('click', async () => {
  await window.App.sendDeviceCmd({ cmd: 'stop' });
  await window.App.sendDeviceCmd({ cmd: 'setMax' });

  const ready = await waitForEngineReady(10000);
  if (!ready) {
    window.App.showError('Set Max timed out waiting for engine init. Try Set Max again.');
    return;
  }

  setSetupStep(3);
});

document.getElementById('setup-home').addEventListener('click', async () => {
  await window.App.sendDeviceCmd({ cmd: 'stop' });
  await window.App.sendDeviceCmd({ cmd: 'moveTo', pct: 0 });
});

document.getElementById('setup-stop').addEventListener('click', () => window.App.sendDeviceCmd({ cmd: 'stop' }));

setSetupStep(1);