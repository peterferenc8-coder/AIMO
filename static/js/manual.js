const manualPattern = document.getElementById('manual-pattern');
const manualSpeed   = document.getElementById('manual-speed');
const manualDepth   = document.getElementById('manual-depth');
const manualBase    = document.getElementById('manual-base');
const manualInt     = document.getElementById('manual-intensity');

function updateLabels() {
  document.getElementById('val-speed').textContent = manualSpeed.value + '%';
  document.getElementById('val-depth').textContent = manualDepth.value + '%';
  document.getElementById('val-base').textContent = manualBase.value + '%';
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

[manualSpeed, manualDepth, manualBase, manualInt].forEach(el => {
  el.addEventListener('input', updateLabels);
});

manualSpeed.addEventListener('change', () => {
  window.App.sendDeviceCmd({ cmd: 'setSpeedPct', value: parseInt(manualSpeed.value) });
});

manualDepth.addEventListener('change', () => {
  enforceBaseDepth();
  const depth = parseInt(manualDepth.value);
  const base = parseInt(manualBase.value);
  window.App.sendDeviceCmd({ cmd: 'setDepthPct', value: depth });
  window.App.sendDeviceCmd({ cmd: 'setStrokePct', value: Math.max(0, depth - base) });
});

manualBase.addEventListener('change', () => {
  enforceBaseDepth();
  const depth = parseInt(manualDepth.value);
  const base = parseInt(manualBase.value);
  window.App.sendDeviceCmd({ cmd: 'setStrokePct', value: Math.max(0, depth - base) });
});

manualInt.addEventListener('change', () => {
  window.App.sendDeviceCmd({ cmd: 'setSensation', value: parseInt(manualInt.value) });
});

manualPattern.addEventListener('change', () => {
  window.App.sendDeviceCmd({ cmd: 'setPattern', value: parseInt(manualPattern.value) });
});

document.getElementById('manual-start').addEventListener('click', () => {
  window.App.sendDeviceCmd({ cmd: 'startPattern' });
});

document.getElementById('manual-stop').addEventListener('click', () => {
  window.App.sendDeviceCmd({ cmd: 'stopPattern' });
});

updateLabels();