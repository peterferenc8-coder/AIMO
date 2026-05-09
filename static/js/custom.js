let customPoints = [
  { pct: 50, duration: 2000 },
  { pct: 55, duration: 1000 },
  { pct: 0, duration: 2000 },
  { pct: 75, duration: 750 },
  { pct: 0, duration: 750 }
];

let isCustomPlaying = false;
let customRunToken = 0;

const canvas = document.getElementById('custom-canvas');
const ctx = canvas.getContext('2d');
const listContainer = document.getElementById('custom-points-list');
const selectEl = document.getElementById('custom-pattern-select');
const nameInput = document.getElementById('custom-pattern-name');

// ============================================================
//  Position tracking for arrival-based sequencing
// ============================================================
let lastKnownPct = null;
let lastPositionTime = 0;

window.CustomTab = window.CustomTab || {};
window.CustomTab.onDevicePositionUpdate = function(data) {
  if (data && typeof data.pct === 'number') {
    lastKnownPct = data.pct;
    lastPositionTime = Date.now();
  }
};

function waitForPosition(targetPct, tolerance = 0.5, budgetMs = 1000) {
  return new Promise((resolve) => {
    if (lastKnownPct === null) {
      console.warn('[Custom] No position data yet; using timer fallback');
      setTimeout(resolve, budgetMs);
      return;
    }

    if (Math.abs(lastKnownPct - targetPct) <= tolerance) {
      resolve();
      return;
    }

    const startTime = Date.now();
    const checkInterval = 20;
    const safetyTimeoutMs = Math.max(budgetMs * 3, 2000);
    let settleCount = 0;
    const requiredSettle = 3;

    const timer = setInterval(() => {
      const elapsed = Date.now() - startTime;
      const dataStale = (Date.now() - lastPositionTime) > 2000;

      if (lastKnownPct !== null && Math.abs(lastKnownPct - targetPct) <= tolerance) {
        settleCount++;
        if (settleCount >= requiredSettle) {
          clearInterval(timer);
          console.log(`[Custom] Arrived at ${targetPct}% (actual=${lastKnownPct.toFixed(2)}%), settle=${settleCount}`);
          resolve();
          return;
        }
      } else {
        settleCount = 0;
      }

      if (elapsed > safetyTimeoutMs || dataStale) {
        clearInterval(timer);
        console.warn(
          `[Custom] Arrival wait aborted (target=${targetPct.toFixed(1)}%, ` +
          `last=${lastKnownPct !== null ? lastKnownPct.toFixed(1) : 'null'}%, ` +
          `elapsed=${elapsed}ms)`
        );
        resolve();
      }
    }, checkInterval);
  });
}

// --- UI & Rendering ---

function renderList() {
  listContainer.innerHTML = '';
  customPoints.forEach((pt, idx) => {
    const row = document.createElement('div');
    row.className = 'custom-point-row';
    row.draggable = true;
    row.dataset.idx = idx;
    row.style.cssText = `
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-bottom: 8px;
      align-items: center;
      padding: 6px 8px;
      background: #252535;
      border-radius: 6px;
      border: 1px solid transparent;
      cursor: grab;
    `;

    row.innerHTML = `
      <span class="drag-handle" style="cursor: grab; user-select: none; line-height: 32px; color: #888; font-size: 16px;">≡</span>
      <span style="line-height: 32px; min-width: 22px; font-variant-numeric: tabular-nums;">${idx + 1}.</span>
      <input type="number" min="0" max="100" class="pt-pct" value="${pt.pct}" placeholder="Target %" style="flex: 1 1 50px; min-width: 50px; max-width: 80px;">
      <span style="line-height: 32px;">%</span>
      <span style="line-height: 32px; color: #888;">in</span>
      <input type="number" min="10" step="10" class="pt-dur" value="${pt.duration}" placeholder="ms" style="flex: 1 1 60px; min-width: 60px; max-width: 100px;">
      <span style="line-height: 32px; color: #888;">ms</span>
      <button class="btn btn-sm btn-ghost rm-btn" data-idx="${idx}" style="margin-left: auto;">X</button>
    `;
    listContainer.appendChild(row);
  });

  // --- Drag & Drop ---
  let dragSrcIdx = null;

  listContainer.querySelectorAll('.custom-point-row').forEach(row => {
    row.addEventListener('dragstart', (e) => {
      dragSrcIdx = parseInt(row.dataset.idx);
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', dragSrcIdx);
      row.style.opacity = '0.5';
    });

    row.addEventListener('dragend', () => {
      row.style.opacity = '1';
      listContainer.querySelectorAll('.custom-point-row').forEach(r => {
        r.style.borderColor = 'transparent';
      });
    });

    row.addEventListener('dragover', (e) => {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      return false;
    });

    row.addEventListener('dragenter', (e) => {
      const target = e.currentTarget;
      if (parseInt(target.dataset.idx) !== dragSrcIdx) {
        target.style.borderColor = '#00ffcc';
      }
    });

    row.addEventListener('dragleave', (e) => {
      e.currentTarget.style.borderColor = 'transparent';
    });

    row.addEventListener('drop', (e) => {
      e.stopPropagation();
      e.preventDefault();
      const targetIdx = parseInt(e.currentTarget.dataset.idx);
      if (dragSrcIdx === null || dragSrcIdx === targetIdx) return false;

      const [moved] = customPoints.splice(dragSrcIdx, 1);
      customPoints.splice(targetIdx, 0, moved);

      renderList();
      drawGraph();
      return false;
    });
  });

  // --- Input listeners ---
  listContainer.querySelectorAll('.pt-pct').forEach((el, idx) => {
    el.addEventListener('change', (e) => {
      customPoints[idx].pct = parseInt(e.target.value);
      drawGraph();
    });
  });
  listContainer.querySelectorAll('.pt-dur').forEach((el, idx) => {
    el.addEventListener('change', (e) => {
      customPoints[idx].duration = parseInt(e.target.value);
      drawGraph();
    });
  });
  listContainer.querySelectorAll('.rm-btn').forEach(el => {
    el.addEventListener('click', (e) => {
      customPoints.splice(parseInt(e.target.dataset.idx), 1);
      renderList();
      drawGraph();
    });
  });
}

function drawGraph() {
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (customPoints.length === 0) return;

  const totalTime = customPoints.reduce((sum, pt) => sum + pt.duration, 0);
  if (totalTime === 0) return;

  ctx.beginPath();
  ctx.strokeStyle = '#00ffcc';
  ctx.lineWidth = 3;
  ctx.lineJoin = 'round';

  let currentTime = 0;
  const startY = canvas.height - (customPoints[customPoints.length - 1].pct / 100) * canvas.height;
  ctx.moveTo(0, startY);

  customPoints.forEach((pt) => {
    currentTime += pt.duration;
    const x = (currentTime / totalTime) * canvas.width;
    const y = canvas.height - (pt.pct / 100) * canvas.height;
    ctx.lineTo(x, y);
    ctx.fillStyle = '#fff';
    ctx.fillRect(x - 3, y - 3, 6, 6);
  });

  ctx.stroke();
}

// --- Sequence Engine ---

async function startSequence() {
  if (customPoints.length === 0) return;

  isCustomPlaying = true;
  customRunToken++;
  const myToken = customRunToken;

  console.log("[Custom] Sequence Started. Pattern:", JSON.parse(JSON.stringify(customPoints)));

  let index = 0;
  let prevTargetPct = null;

  while (isCustomPlaying && customRunToken === myToken) {
    const targetPoint = customPoints[index];

    const safePct = (isNaN(targetPoint.pct)) ? 0 : Math.max(0, Math.min(100, targetPoint.pct));
    const safeDuration = (isNaN(targetPoint.duration) || targetPoint.duration <= 0) ? 1000 : targetPoint.duration;

    console.log(`[Custom] -> Node ${index}: target ${safePct}% (budget ${safeDuration}ms)`);

    window.App.sendDeviceCmd({
      cmd: 'stream',
      pct: safePct,
      duration: safeDuration
    });

    // FIX: if target is same as previous, dwell for the full duration
    if (prevTargetPct !== null && Math.abs(prevTargetPct - safePct) < 0.5) {
      console.log(`[Custom] Dwell at ${safePct}% for ${safeDuration}ms`);
      await new Promise(r => setTimeout(r, safeDuration));
    } else {
      await waitForPosition(safePct, 0.5, safeDuration);
    }

    prevTargetPct = safePct;
    index = (index + 1) % customPoints.length;
  }
}

function stopSequence() {
  isCustomPlaying = false;
  customRunToken++;
  window.App.sendDeviceCmd({ cmd: 'stop' });
}

// --- API & Persistence ---

async function loadPatternList() {
  const res = await fetch('/api/custom_patterns');
  const data = await res.json();
  selectEl.innerHTML = '<option value="">-- Select Saved Pattern --</option>';
  data.patterns.forEach(name => {
    const opt = document.createElement('option');
    opt.value = name;
    opt.textContent = name;
    selectEl.appendChild(opt);
  });
}

document.getElementById('custom-btn-add').addEventListener('click', () => {
  customPoints.push({ pct: 50, duration: 1000 });
  renderList();
  drawGraph();
});

document.getElementById('custom-btn-start').addEventListener('click', startSequence);
document.getElementById('custom-btn-stop').addEventListener('click', stopSequence);

selectEl.addEventListener('change', async (e) => {
  const name = e.target.value;
  if (!name) return;
  const res = await fetch(`/api/custom_patterns/${name}`);
  const data = await res.json();
  if (data.ok && data.points) {
    customPoints = data.points;
    nameInput.value = name;
    renderList();
    drawGraph();
  }
});

document.getElementById('custom-btn-save').addEventListener('click', async () => {
  const name = nameInput.value.trim();
  if (!name) return alert('Enter a pattern name');
  await fetch(`/api/custom_patterns/${name}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ points: customPoints })
  });
  loadPatternList();
});

// Initialization
renderList();
drawGraph();
loadPatternList();