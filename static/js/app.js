const $ = (id) => document.getElementById(id);

// ── Tab switching ───────────────────────────────────────────────────────────
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    if (btn.disabled) return;
    const tab = btn.dataset.tab;
    // Stop funscript when leaving its tab
    if (window.FunscriptTab && window.FunscriptTab.isPlaying() && tab !== 'funscript') {
      window.FunscriptTab.stop();
    }
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
function showBanner(msg, kind = 'error') {
  const banner = $('error-banner');
  if (!banner) return;
  banner.style.display = 'flex';
  banner.classList.remove('banner-error', 'banner-info');
  banner.classList.add(kind === 'info' ? 'banner-info' : 'banner-error');
  const msgEl = $('error-banner-msg');
  if (msgEl) msgEl.textContent = (kind === 'info' ? 'ℹ ' : '⚠ ') + msg;
}

function showError(msg) {
  showBanner(msg, 'error');
}

function showInfo(msg) {
  showBanner(msg, 'info');
}

function hideError() {
  const banner = $('error-banner');
  if (!banner) return;
  banner.style.display = 'none';
  banner.classList.remove('banner-error', 'banner-info');
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
    unlockDeviceTabs();
    window.dispatchEvent(new CustomEvent('device-homed'));
  }
}

// Enable every tab that needs a live device. Shared with setup.js, where
// devices without a homing step (Coyote, Intiface) unlock on connect instead.
// Keep this the single list: it previously drifted, leaving Funscript locked
// for anything that never homes.
const DEVICE_TABS = ['manual', 'ai', 'custom', 'funscript'];

function unlockDeviceTabs() {
  DEVICE_TABS.forEach(tab => {
    const btn = $(`tab-btn-${tab}`);
    if (btn && btn.disabled) btn.disabled = false;
  });
}

// Put the gate back when the user switches to real hardware that has not been
// connected (or homed) yet. Anyone standing on a locked tab is walked back to
// Setup, since that is where the work to reopen it happens.
function lockDeviceTabs() {
  DEVICE_TABS.forEach(tab => {
    const btn = $(`tab-btn-${tab}`);
    if (!btn) return;
    btn.disabled = true;
    if (btn.classList.contains('active')) $('tab-btn-setup').click();
  });
}

function openDeviceStream() {
  closeDeviceStream();
  deviceEventSource = new EventSource('/api/device/stream');
  deviceEventSource.onmessage = (ev) => {
    try {
      const data = JSON.parse(ev.data);
      if (data.type === 'position') updateAllGauges(data);
      if (window.CustomTab && window.CustomTab.onDevicePositionUpdate) {
          window.CustomTab.onDevicePositionUpdate(data);
        }
      // Buttplug pushes its toy inventory whenever Intiface reports a device
      // added or removed, so the setup list stays live without polling.
      if (data.devices) {
        window.dispatchEvent(new CustomEvent('buttplug-devices', { detail: data.devices }));
      }
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

// ── Dismissable error banner ────────────────────────────────────────────────
const errorBannerClose = $('error-banner-close');
if (errorBannerClose) errorBannerClose.addEventListener('click', hideError);

// ── Expose globals ─────────────────────────────────────────────────────────
window.App = { showError, showInfo, hideError, setDeviceStatus, openDeviceStream, closeDeviceStream, sendDeviceCmd, unlockDeviceTabs, lockDeviceTabs };