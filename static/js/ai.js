let pollHandle = null;
let displayIndex = 0;
let isRunning = false;
let startTime = null;
let timerInterval = null;
let lastDisplayedSpeech = '';

const typingQueue = [];
let isTyping = false;

// ── Timer ──────────────────────────────────────────────────────────────────
function startTimer() {
  startTime = Date.now();
  if (timerInterval) clearInterval(timerInterval);
  timerInterval = setInterval(() => {
    document.getElementById('ai-timer').textContent = ((Date.now() - startTime) / 1000).toFixed(1) + 's';
  }, 100);
}
function stopTimer() {
  if (timerInterval) clearInterval(timerInterval);
  timerInterval = null;
}
function resetTimer() {
  stopTimer();
  startTime = null;
  document.getElementById('ai-timer').textContent = '';
}

// ── Typing animation ───────────────────────────────────────────────────────
function typeText(el, text, speed = 20) {
  return new Promise(resolve => {
    let i = 0;
    el.classList.add('typing');
    const iv = setInterval(() => {
      el.textContent += text.charAt(i);
      i++;
      if (i >= text.length) {
        clearInterval(iv);
        el.classList.remove('typing');
        resolve();
      }
    }, speed);
  });
}

async function processTypingQueue() {
  if (isTyping || typingQueue.length === 0) return;
  isTyping = true;
  const { el, text } = typingQueue.shift();
  await typeText(el, text);
  isTyping = false;
  processTypingQueue();
}

function enqueueTyping(el, text) {
  typingQueue.push({ el, text });
  processTypingQueue();
}

// ── Stream cards ───────────────────────────────────────────────────────────
function makeAICard(item) {
  const card = document.createElement('div');
  card.className = 'turn-card';
  card.style.animationDelay = '0ms';

  const badge = item.source === 'big'
    ? '<span class="badge badge-big">BIG</span>'
    : '<span class="badge badge-small">SMALL</span>';
  const time = new Date().toLocaleTimeString();

  card.innerHTML = `
    <div class="turn-header">
      ${badge}
      <span>#${item.index + 1}</span>
      <span class="turn-time">${time}</span>
    </div>
    <div class="turn-speech"></div>
  `;

  const speechEl = card.querySelector('.turn-speech');
  if (item.source === 'big' && item.speech) {
    enqueueTyping(speechEl, item.speech);
  } else {
    speechEl.textContent = item.speech || '';
  }

  if (item.source === 'big' && item.commands) {
    updateParamChips(item.commands);
  }
  return card;
}

function updateParamChips(cmds) {
  if (cmds.pattern) document.getElementById('ai-param-pattern').textContent = cmds.pattern;
  if (cmds.speed !== null && cmds.speed !== undefined) document.getElementById('ai-param-speed').textContent = cmds.speed + '%';
  if (cmds.depth !== null && cmds.depth !== undefined) document.getElementById('ai-param-depth').textContent = cmds.depth + '%';
  if (cmds.base !== null && cmds.base !== undefined) document.getElementById('ai-param-base').textContent = cmds.base + '%';
  if (cmds.intensity !== null && cmds.intensity !== undefined) document.getElementById('ai-param-intensity').textContent = cmds.intensity;
}

// ── Poll loop ──────────────────────────────────────────────────────────────
async function poll() {
  if (!isRunning) return;
  try {
    const res = await fetch(`/api/poll?since=${displayIndex}`);
    const data = await res.json();
    if (!data.ok) {
      window.App.showError(data.error || 'Poll failed');
      return;
    }
    updateAIStatus(data.state);

    if (data.items && data.items.length) {
      const list = document.getElementById('ai-list-stream');
      if (list.querySelector('.empty-state')) list.innerHTML = '';
      data.items.forEach(item => {
        const speech = item.speech || (item.raw && item.raw.speech);
        if (speech && speech === lastDisplayedSpeech) return;
        list.appendChild(makeAICard(item));
        if (speech) lastDisplayedSpeech = speech;
      });
      list.scrollTop = list.scrollHeight;
      displayIndex = data.total;
      document.getElementById('ai-count-stream').textContent = displayIndex;
    }
  } catch (err) {
    console.error('Poll error:', err);
  }
}

function updateAIStatus(state) {
  const el = document.getElementById('ai-status');
  el.className = '';
  el.textContent = state;
  el.classList.add(`status-${state}`);
  document.getElementById('ai-start').disabled = state === 'running' || state === 'buffering';
  document.getElementById('ai-pause').disabled = state !== 'running';
  document.getElementById('ai-resume').disabled = state !== 'paused';
}

function setAILoading(on) {
  document.getElementById('ai-spinner').style.display = on ? 'block' : 'none';
  document.getElementById('ai-start').disabled = on;
}

function clearAIStream() {
  document.getElementById('ai-list-stream').innerHTML = `
    <div class="empty-state">
      <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
        <rect x="3" y="3" width="18" height="18" rx="2"/>
        <path d="M8 12h8M12 8v8"/>
      </svg>
      Start a session to see the stream
    </div>`;
  document.getElementById('ai-count-stream').textContent = '0';
  lastDisplayedSpeech = '';
}

function setSelectToValue(selectEl, value) {
  const hasMatch = Array.from(selectEl.options).some(opt => opt.value === value);
  if (hasMatch) selectEl.value = value;
}

// ── Event listeners ────────────────────────────────────────────────────────
document.getElementById('ai-start').addEventListener('click', async () => {
  const nTurns = parseInt(document.getElementById('ai-n-turns').value) || 20;
  const modelSelect = document.getElementById('ai-model');
  const selectedModel = modelSelect ? modelSelect.value : '';

  if (!selectedModel) {
    window.App.showError('No validated provider/model available. Set and validate an API key in Settings first.');
    return;
  }

  window.App.hideError();
  setAILoading(true);

  try {
    const res = await fetch('/api/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        n_turns: nTurns,
        persona: document.getElementById('ai-persona').value === 'random' ? null : document.getElementById('ai-persona').value,
        pacing: document.getElementById('ai-pacing').value === 'random' ? null : document.getElementById('ai-pacing').value,
        model: selectedModel,
      }),
    });
    const data = await res.json();
    if (!data.ok) {
      window.App.showError(data.error || 'Start failed');
      setAILoading(false);
      return;
    }
    if (data.persona) setSelectToValue(document.getElementById('ai-persona'), data.persona);
    if (data.pacing) setSelectToValue(document.getElementById('ai-pacing'), data.pacing);

    isRunning = true;
    displayIndex = 0;
    startTimer();
    updateAIStatus(data.state || 'running');
    clearAIStream();
    pollHandle = setInterval(poll, 1000);
  } catch (err) {
    window.App.showError('Network error: ' + err.message);
  } finally {
    setAILoading(false);
  }
});

document.getElementById('ai-pause').addEventListener('click', async () => {
  try {
    const res = await fetch('/api/pause', { method: 'POST' });
    const data = await res.json();
    updateAIStatus(data.state);
    isRunning = data.state === 'running';
  } catch (err) {
    window.App.showError('Pause failed: ' + err.message);
  }
});

document.getElementById('ai-resume').addEventListener('click', async () => {
  try {
    const res = await fetch('/api/resume', { method: 'POST' });
    const data = await res.json();
    updateAIStatus(data.state);
    isRunning = data.state === 'running';
  } catch (err) {
    window.App.showError('Resume failed: ' + err.message);
  }
});

document.getElementById('ai-clear').addEventListener('click', async () => {
  await fetch('/api/clear', { method: 'POST' });
  isRunning = false;
  if (pollHandle) clearInterval(pollHandle);
  pollHandle = null;
  clearAIStream();
  resetTimer();
  window.App.hideError();
  updateAIStatus('idle');
  displayIndex = 0;
});