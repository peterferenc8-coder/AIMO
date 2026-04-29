let pollHandle = null;
let displayIndex = 0;
let isRunning = false;
let startTime = null;
let totalElapsedTime = 0;
let timerInterval = null;
let lastDisplayedSpeech = '';
let currentPattern = null;

// ── Audio & word-sync state ───────────────────────────────────────────────
let currentAudio = null;
let currentWordTimer = null;
let activeWords = [];      // [{word, start_ms, end_ms}, ...]
let activeWordIndex = -1;
let isAudioPlaying = false;

const typingQueue = [];
let isTyping = false;

// ── Timer ──────────────────────────────────────────────────────────────────
function startTimer() {
  startTime = Date.now();
  if (timerInterval) clearInterval(timerInterval);
  timerInterval = setInterval(() => {
    const elapsed = totalElapsedTime + (Date.now() - startTime);
    document.getElementById('ai-timer').textContent = (elapsed / 1000).toFixed(1) + 's';
  }, 100);
}
function stopTimer() {
  if (timerInterval) clearInterval(timerInterval);
  timerInterval = null;
  if (startTime !== null) {
    totalElapsedTime += Date.now() - startTime;
    startTime = null;
  }
}
function resetTimer() {
  stopTimer();
  totalElapsedTime = 0;
  document.getElementById('ai-timer').textContent = '';
}

// ── Audio playback with word sync ─────────────────────────────────────────
function stopCurrentAudio() {
  if (currentAudio) {
    currentAudio.pause();
    currentAudio.currentTime = 0;
    currentAudio = null;
  }
  if (currentWordTimer) {
    clearInterval(currentWordTimer);
    currentWordTimer = null;
  }
  activeWords = [];
  activeWordIndex = -1;
  isAudioPlaying = false;
  // Clear any highlighted words in the DOM
  document.querySelectorAll('.word-highlight').forEach(el => {
    el.classList.remove('word-highlight');
  });
}

function playAudioWithWordSync(audioUrl, words, speechEl) {
  if (!audioUrl || !words || words.length === 0) {
    // No audio or no timing data – just show the full text immediately
    return;
  }

  stopCurrentAudio();

  activeWords = words;
  activeWordIndex = -1;
  isAudioPlaying = true;

  // Build word spans inside speechEl for highlighting
  speechEl.innerHTML = '';
  words.forEach((w, i) => {
    const span = document.createElement('span');
    span.className = 'word-token';
    span.dataset.index = i;
    span.textContent = w.word;
    speechEl.appendChild(span);
    // Add space after word (except last)
    if (i < words.length - 1) {
      speechEl.appendChild(document.createTextNode(' '));
    }
  });

  currentAudio = new Audio(audioUrl);
  currentAudio.play().catch(err => {
    console.warn('Audio play failed:', err);
    isAudioPlaying = false;
  });

  // Word highlight loop: check audio.currentTime against word timings
  currentWordTimer = setInterval(() => {
    if (!currentAudio || currentAudio.paused) {
      return;
    }
    const t = currentAudio.currentTime * 1000; // ms

    // Find current word
    let newIndex = -1;
    for (let i = 0; i < activeWords.length; i++) {
      if (t >= activeWords[i].start_ms && t < activeWords[i].end_ms) {
        newIndex = i;
        break;
      }
    }

    if (newIndex !== activeWordIndex) {
      activeWordIndex = newIndex;
      // Update DOM highlighting
      speechEl.querySelectorAll('.word-token').forEach((span, i) => {
        if (i === newIndex) {
          span.classList.add('word-highlight');
          span.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        } else {
          span.classList.remove('word-highlight');
        }
      });
    }

    // Auto-advance: if we're past the last word, stop the timer
    if (activeWords.length > 0 && t >= activeWords[activeWords.length - 1].end_ms + 200) {
      clearInterval(currentWordTimer);
      currentWordTimer = null;
    }
  }, 30); // 30ms refresh for smooth highlighting

  currentAudio.addEventListener('ended', () => {
    isAudioPlaying = false;
    if (currentWordTimer) {
      clearInterval(currentWordTimer);
      currentWordTimer = null;
    }
    // Leave last word highlighted briefly, then clear
    setTimeout(() => {
      speechEl.querySelectorAll('.word-token').forEach(span => {
        span.classList.remove('word-highlight');
      });
    }, 500);
  });
}

// ── Typing animation (fallback when no TTS) ───────────────────────────────
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

  // If we have TTS data, show words as spans and sync with audio
  if (item.audio_url && item.words && item.words.length > 0) {
    // Build placeholder spans (will be filled when audio starts)
    item.words.forEach((w, i) => {
      const span = document.createElement('span');
      span.className = 'word-token';
      span.dataset.index = i;
      span.textContent = w.word;
      speechEl.appendChild(span);
      if (i < item.words.length - 1) {
        speechEl.appendChild(document.createTextNode(' '));
      }
    });

    // Start audio playback immediately with word sync
    playAudioWithWordSync(item.audio_url, item.words, speechEl);
  } else if (item.source === 'big' && item.speech) {
    // Fallback: typing animation when no TTS available
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
  if (cmds.pattern) {
    document.getElementById('ai-param-pattern').textContent = cmds.pattern;
    currentPattern = cmds.pattern;
  }
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
  document.getElementById('ai-start').disabled = state !== 'idle';
  document.getElementById('ai-pause').disabled = state !== 'running';
  document.getElementById('ai-resume').disabled = state !== 'paused';
  document.getElementById('ai-stop').disabled = state === 'idle';
}

function setAILoading(on) {
  document.getElementById('ai-spinner').style.display = on ? 'block' : 'none';
  document.getElementById('ai-start').disabled = on;
  document.getElementById('ai-pause').disabled = on;
  document.getElementById('ai-resume').disabled = on;
  document.getElementById('ai-stop').disabled = on;
}

function clearAIStream() {
  stopCurrentAudio();
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
    currentPattern = null;
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
    stopTimer();
    stopCurrentAudio(); // Pause audio too

    // Send device stop command - don't block on this
    try {
      window.App.sendDeviceCmd({ cmd: 'stop' });
    } catch (deviceErr) {
      console.error('Device command failed:', deviceErr);
    }
  } catch (err) {
    window.App.showError('Pause failed: ' + err.message);
    updateAIStatus('idle');
  }
});

document.getElementById('ai-resume').addEventListener('click', async () => {
  console.log('Resume clicked');
  try {
    console.log('Fetching /api/resume');
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 5000);

    const res = await fetch('/api/resume', {
      method: 'POST',
      signal: controller.signal
    });

    clearTimeout(timeoutId);
    console.log('Resume response received:', res.status);

    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`);
    }

    const data = await res.json();
    console.log('Resume response data:', data);
    updateAIStatus(data.state);
    isRunning = data.state === 'running';
    if (isRunning) {
      startTimer();

      // Restart the pattern if one is active
      if (currentPattern) {
        try {
          window.App.sendDeviceCmd({ cmd: 'startPattern' });
        } catch (deviceErr) {
          console.error('Device command failed on resume:', deviceErr);
        }
      }
    }
  } catch (err) {
    console.error('Resume error:', err);
    window.App.showError('Resume failed: ' + err.message);
    updateAIStatus('idle');
  }
});

document.getElementById('ai-stop').addEventListener('click', async () => {
  try {
    await fetch('/api/clear', { method: 'POST' });
    isRunning = false;
    if (pollHandle) clearInterval(pollHandle);
    pollHandle = null;
    clearAIStream();
    resetTimer();
    window.App.hideError();
    displayIndex = 0;
    currentPattern = null;

    // Send device stop command
    try {
      window.App.sendDeviceCmd({ cmd: 'stop' });
    } catch (deviceErr) {
      console.error('Device command failed on stop:', deviceErr);
    }

    updateAIStatus('idle');
  } catch (err) {
    window.App.showError('Stop failed: ' + err.message);
    updateAIStatus('idle');
  }
});
