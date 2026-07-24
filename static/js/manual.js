/* manual.js */

let currentManualDevice = 'none';

// ── OSSM Elements ───────────────────────────────────────────────────────────

const manualPattern = document.getElementById('manual-pattern');
const manualSpeed   = document.getElementById('manual-speed');
const manualDepth   = document.getElementById('manual-depth');
const manualBase    = document.getElementById('manual-base');
const manualInt     = document.getElementById('manual-intensity');
const manualStart   = document.getElementById('manual-start');
const manualStop    = document.getElementById('manual-stop');

// ── Coyote Elements ─────────────────────────────────────────────────────────

const coyoteWaveform     = document.getElementById('coyote-waveform');
const coyoteChA          = document.getElementById('coyote-ch-a');
const coyoteChB          = document.getElementById('coyote-ch-b');
const coyoteFreqA        = document.getElementById('coyote-freq-a');
const coyoteFreqB        = document.getElementById('coyote-freq-b');
const coyoteWaveA        = document.getElementById('coyote-wave-a');
const coyoteWaveB        = document.getElementById('coyote-wave-b');
const coyoteSoftLimitA   = document.getElementById('coyote-soft-limit-a');
const coyoteSoftLimitB   = document.getElementById('coyote-soft-limit-b');
const coyoteZeroBtn      = document.getElementById('coyote-zero');
const coyoteStopBtn      = document.getElementById('coyote-stop');

const manualOssmPanel = document.getElementById('manual-ossm-panel');
const manualCoyotePanel = document.getElementById('manual-coyote-panel');

// ── Device Type Listener ──────────────────────────────────────────────────

window.addEventListener('device-type-changed', (e) => {
  currentManualDevice = e.detail || 'none';
  updateManualPanels();
});

async function syncManualDeviceType() {
  try {
    const res = await fetch('/api/device/types');
    const data = await res.json();
    if (data.ok) {
      currentManualDevice = data.active || 'none';
      updateManualPanels();
    }
  } catch (err) {
    console.warn('Failed to sync manual device type', err);
  }
}

function updateManualPanels() {
  // "No Toy" borrows the stroke controls: they drive the on-screen gauge and
  // the pattern engine, they just have nothing to move.
  if (currentManualDevice === 'ossm' || currentManualDevice === 'none') {
    if (manualOssmPanel) manualOssmPanel.style.display = '';
    if (manualCoyotePanel) manualCoyotePanel.style.display = 'none';
  } else {
    if (manualOssmPanel) manualOssmPanel.style.display = 'none';
    if (manualCoyotePanel) manualCoyotePanel.style.display = '';
  }
}

// ── OSSM Handlers ───────────────────────────────────────────────────────────

function updateLabels() {
  if (document.getElementById('val-speed'))
    document.getElementById('val-speed').textContent = manualSpeed.value + '%';
  if (document.getElementById('val-depth'))
    document.getElementById('val-depth').textContent = manualDepth.value + '%';
  if (document.getElementById('val-base'))
    document.getElementById('val-base').textContent = manualBase.value + '%';
  if (document.getElementById('val-intensity'))
    document.getElementById('val-intensity').textContent = manualInt.value;
}

function enforceBaseDepth() {
  let depth = parseInt(manualDepth.value);
  let base = parseInt(manualBase.value);
  if (base > depth) {
    manualBase.value = depth;
    updateLabels();
  }
}

if (manualSpeed) {
  manualSpeed.addEventListener('input', updateLabels);
  manualSpeed.addEventListener('change', () => {
    window.App.sendDeviceCmd({ cmd: 'setSpeedPct', value: parseInt(manualSpeed.value) });
  });
}

if (manualDepth) {
  manualDepth.addEventListener('input', updateLabels);
  manualDepth.addEventListener('change', () => {
    enforceBaseDepth();
    const depth = parseInt(manualDepth.value);
    const base = parseInt(manualBase.value);
    window.App.sendDeviceCmd({ cmd: 'setDepthPct', value: depth });
    window.App.sendDeviceCmd({ cmd: 'setStrokePct', value: Math.max(0, depth - base) });
  });
}

if (manualBase) {
  manualBase.addEventListener('input', updateLabels);
  manualBase.addEventListener('change', () => {
    enforceBaseDepth();
    const depth = parseInt(manualDepth.value);
    const base = parseInt(manualBase.value);
    window.App.sendDeviceCmd({ cmd: 'setStrokePct', value: Math.max(0, depth - base) });
  });
}

if (manualInt) {
  manualInt.addEventListener('input', updateLabels);
  manualInt.addEventListener('change', () => {
    window.App.sendDeviceCmd({ cmd: 'setSensation', value: parseInt(manualInt.value) });
  });
}

if (manualPattern) {
  manualPattern.addEventListener('change', () => {
    window.App.sendDeviceCmd({ cmd: 'setPattern', value: parseInt(manualPattern.value) });
  });
}

if (manualStart) {
  manualStart.addEventListener('click', () => {
    window.App.sendDeviceCmd({ cmd: 'startPattern' });
  });
}

if (manualStop) {
  manualStop.addEventListener('click', () => {
    window.App.sendDeviceCmd({ cmd: 'stopPattern' });
  });
}

// ── Coyote Handlers ───────────────────────────────────────────────────────────

function updateCoyoteLabels() {
  if (document.getElementById('val-coyote-a'))
    document.getElementById('val-coyote-a').textContent = coyoteChA.value;
  if (document.getElementById('val-coyote-b'))
    document.getElementById('val-coyote-b').textContent = coyoteChB.value;
  if (document.getElementById('val-coyote-freq-a'))
    document.getElementById('val-coyote-freq-a').textContent = coyoteFreqA.value + 'ms';
  if (document.getElementById('val-coyote-freq-b'))
    document.getElementById('val-coyote-freq-b').textContent = coyoteFreqB.value + 'ms';
  if (document.getElementById('val-coyote-wave-a'))
    document.getElementById('val-coyote-wave-a').textContent = coyoteWaveA.value;
  if (document.getElementById('val-coyote-wave-b'))
    document.getElementById('val-coyote-wave-b').textContent = coyoteWaveB.value;
}

function sendCoyoteCommand() {
  const cmd = {
    ch_a: parseInt(coyoteChA.value),
    ch_b: parseInt(coyoteChB.value),
    freq_a: parseInt(coyoteFreqA.value),
    freq_b: parseInt(coyoteFreqB.value),
    wave_a: parseInt(coyoteWaveA.value),
    wave_b: parseInt(coyoteWaveB.value),
    waveform: coyoteWaveform.value,
    soft_limit_a: parseInt(coyoteSoftLimitA.value),
    soft_limit_b: parseInt(coyoteSoftLimitB.value),
  };
  fetch('/api/coyote/command', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(cmd),
  }).catch(err => console.warn('Coyote command error:', err));
}

if (coyoteChA) {
  coyoteChA.addEventListener('input', updateCoyoteLabels);
  coyoteChA.addEventListener('change', sendCoyoteCommand);
}
if (coyoteChB) {
  coyoteChB.addEventListener('input', updateCoyoteLabels);
  coyoteChB.addEventListener('change', sendCoyoteCommand);
}
if (coyoteFreqA) {
  coyoteFreqA.addEventListener('input', updateCoyoteLabels);
  coyoteFreqA.addEventListener('change', sendCoyoteCommand);
}
if (coyoteFreqB) {
  coyoteFreqB.addEventListener('input', updateCoyoteLabels);
  coyoteFreqB.addEventListener('change', sendCoyoteCommand);
}
if (coyoteWaveA) {
  coyoteWaveA.addEventListener('input', updateCoyoteLabels);
  coyoteWaveA.addEventListener('change', sendCoyoteCommand);
}
if (coyoteWaveB) {
  coyoteWaveB.addEventListener('input', updateCoyoteLabels);
  coyoteWaveB.addEventListener('change', sendCoyoteCommand);
}
if (coyoteWaveform) {
  coyoteWaveform.addEventListener('change', sendCoyoteCommand);
}
if (coyoteSoftLimitA) {
  coyoteSoftLimitA.addEventListener('change', sendCoyoteCommand);
}
if (coyoteSoftLimitB) {
  coyoteSoftLimitB.addEventListener('change', sendCoyoteCommand);
}

if (coyoteZeroBtn) {
  coyoteZeroBtn.addEventListener('click', () => {
    coyoteChA.value = 0;
    coyoteChB.value = 0;
    updateCoyoteLabels();
    sendCoyoteCommand();
  });
}

if (coyoteStopBtn) {
  coyoteStopBtn.addEventListener('click', () => {
    coyoteChA.value = 0;
    coyoteChB.value = 0;
    updateCoyoteLabels();
    sendCoyoteCommand();
  });
}

// ── Init ────────────────────────────────────────────────────────────────────

syncManualDeviceType();
updateLabels();
updateCoyoteLabels();
