const $ = (id) => document.getElementById(id);

// ── Tab switching ───────────────────────────────────────────────────────────
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    if (btn.disabled) return;
    const tab = btn.dataset.tab;
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    $(`tab-${tab}`).classList.add('active');
  });
});

// ── Health check ───────────────────────────────────────────────────────────
async function checkHealth() {
  try {
    const res = await fetch('/api/health');
    const data = await res.json();
    const dot = $('health-dot');
    const lbl = $('health-label');
    dot.className = data.ok ? 'ok' : 'error';
    lbl.textContent = data.ok ? `${data.big_model} — Connected` : `Offline: ${data.message}`;
  } catch {
    $('health-dot').className = 'error';
    $('health-label').textContent = 'Cannot reach server';
  }
}
checkHealth();
setInterval(checkHealth, 15000);

// ── Error banner ───────────────────────────────────────────────────────────
function showError(msg) {
  const banner = $('error-banner');
  banner.style.display = 'block';
  banner.textContent = '⚠ ' + msg;
}
function hideError() {
  $('error-banner').style.display = 'none';
}

// ── Device stream (global, updates all gauge instances) ────────────────────
let deviceEventSource = null;

function setDeviceStatus(ok, reconnecting) {
  const dot = $('device-dot');
  const lbl = $('device-label');
  if (!dot || !lbl) return;
  if (reconnecting) {
    dot.className = 'dot';
    dot.style.background = '#ffe66d';
    dot.style.boxShadow = '0 0 6px #ffe66d';
    lbl.textContent = 'Reconnecting...';
    return;
  }
  dot.className = 'dot ' + (ok ? 'ok' : 'error');
  dot.style.background = '';
  dot.style.boxShadow = '';
  lbl.textContent = ok ? 'Connected' : 'Offline';
}

function updateAllGauges(data) {
  const pct = data.pct ?? 0;
  const clamped = Math.max(0, Math.min(100, pct));
  document.querySelectorAll('.gauge-fill').forEach(el => el.style.height = clamped + '%');
  document.querySelectorAll('.gauge-marker').forEach(el => el.style.bottom = clamped + '%');
  document.querySelectorAll('.device-pct').forEach(el => el.textContent = clamped.toFixed(1) + '%');
  document.querySelectorAll('.readout-steps').forEach(el => el.textContent = data.steps ?? 0);
  document.querySelectorAll('.readout-running').forEach(el => el.textContent = data.running ? 'Yes' : 'No');
  document.querySelectorAll('.readout-homed').forEach(el => el.textContent = data.homed ? 'Yes' : 'No');

  if (data.homed) {
    const manualBtn = $('tab-btn-manual');
    const aiBtn = $('tab-btn-ai');
    if (manualBtn && manualBtn.disabled) manualBtn.disabled = false;
    if (aiBtn && aiBtn.disabled) aiBtn.disabled = false;
    window.dispatchEvent(new CustomEvent('device-homed'));
  }
}

function openDeviceStream() {
  closeDeviceStream();
  deviceEventSource = new EventSource('/api/device/stream');
  deviceEventSource.onmessage = (ev) => {
    try {
      const data = JSON.parse(ev.data);
      if (data.type === 'position') updateAllGauges(data);
    } catch (e) {
      console.error('SSE parse error:', e, ev.data);
    }
  };
  deviceEventSource.onerror = () => setDeviceStatus(false, true);
  deviceEventSource.onopen = () => setDeviceStatus(true, false);
}

function closeDeviceStream() {
  if (deviceEventSource) {
    deviceEventSource.close();
    deviceEventSource = null;
  }
}

// ── Shared API helper ──────────────────────────────────────────────────────
async function sendDeviceCmd(payload) {
  try {
    const res = await fetch('/api/device/command', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!data.ok) showError(data.error || 'Device command failed');
  } catch (err) {
    showError('Device command error: ' + err.message);
  }
}

// ── Global emergency stop ──────────────────────────────────────────────────
$('btn-global-stop').addEventListener('click', () => {
  sendDeviceCmd({ cmd: 'stop' });
});

// ── Expose globals ─────────────────────────────────────────────────────────
window.App = { showError, hideError, setDeviceStatus, openDeviceStream, closeDeviceStream, sendDeviceCmd };