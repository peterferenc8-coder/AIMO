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
  await window.App.sendDeviceCmd({ cmd: 'setZero' });
  setSetupStep(2);
});

document.getElementById('setup-set-max').addEventListener('click', async () => {
  await window.App.sendDeviceCmd({ cmd: 'setMax' });
  setSetupStep(3);
});

document.getElementById('setup-home').addEventListener('click', async () => {
  await window.App.sendDeviceCmd({ cmd: 'moveTo', pct: 0 });
});

document.getElementById('setup-stop').addEventListener('click', () => window.App.sendDeviceCmd({ cmd: 'stop' }));

setSetupStep(1);