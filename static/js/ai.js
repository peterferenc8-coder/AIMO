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
let activeVisemes = null;  // viseme track of the line being spoken (for resume)
// Completion callback of the line being spoken. A video turn hangs its clip
// hand-off on this, so it must never be dropped silently: tearing the audio
// down calls it with aborted=true so the backend's hold gets released.
let currentAudioFinish = null;

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
  // The line will never reach its 'ended' event now — settle its completion
  // callback as aborted so a video turn doesn't strand the backend's hold.
  if (currentAudioFinish) {
    const abort = currentAudioFinish;
    currentAudioFinish = null;
    abort(true);
  }
  activeWords = [];
  activeWordIndex = -1;
  activeVisemes = null;
  isAudioPlaying = false;
  // Let the avatar's mouth close (guarded: avatar.js is a deferred module and
  // may not have loaded yet, and the model may still be downloading).
  if (window.Avatar) window.Avatar.stopSpeaking();
  // Clear any highlighted words in the DOM
  document.querySelectorAll('.word-highlight').forEach(el => {
    el.classList.remove('word-highlight');
  });
}

/**
 * Suspend the line currently being spoken without discarding it, so resume can
 * pick it up where it stopped. Unlike stopCurrentAudio() this keeps the audio
 * element (and therefore its 'ended' event and completion callback) alive.
 */
function pauseCurrentAudio() {
  if (currentAudio && !currentAudio.paused) currentAudio.pause();
  isAudioPlaying = false;
  // Let the mouth close while nothing is being said; the viseme track is
  // re-armed on resume.
  if (window.Avatar) window.Avatar.stopSpeaking();
}

/** Continue the suspended line, re-arming word highlighting and lip-sync. */
function resumeCurrentAudio() {
  // `ended` also reads as paused — resuming that would replay the whole line.
  if (!currentAudio || !currentAudio.paused || currentAudio.ended) return;
  isAudioPlaying = true;
  if (window.Avatar && activeVisemes && activeVisemes.length) {
    window.Avatar.speak(activeVisemes, () => (currentAudio ? currentAudio.currentTime * 1000 : null));
  }
  currentAudio.play().catch(err => {
    console.warn('Audio resume failed:', err);
    // Never leave a video turn waiting on a line that cannot play.
    stopCurrentAudio();
  });
}

/**
 * Play `audioUrl`, highlighting `words` in `speechEl` and driving the avatar's
 * mouth from `visemes`. `onFinished(aborted)` fires when the line has been
 * spoken — or with aborted=true if the line was torn down before finishing.
 * Video turns use it to hold the clip back until the announcement is done.
 */
function playAudioWithWordSync(audioUrl, words, speechEl, visemes, onFinished) {
  // Deregister before invoking: the callback may start a clip, which tears the
  // audio down again and would otherwise re-enter this.
  const finish = (aborted = false) => {
    if (currentAudioFinish === finish) currentAudioFinish = null;
    if (onFinished) { const f = onFinished; onFinished = null; f(aborted); }
  };

  if (!audioUrl || !words || words.length === 0) {
    // No audio or no timing data – just show the full text immediately
    finish();
    return;
  }

  stopCurrentAudio();

  activeWords = words;
  activeWordIndex = -1;
  activeVisemes = visemes;
  currentAudioFinish = finish;
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
    if (window.Avatar) window.Avatar.stopSpeaking();
    finish();   // never strand a video waiting on a line that cannot play
  });

  // Drive the avatar's mouth off the audio element's own clock — the same
  // clock the word highlighter below uses — so playback drift can never
  // desync the lips from the voice.
  if (window.Avatar && visemes && visemes.length) {
    window.Avatar.speak(visemes, () => (currentAudio ? currentAudio.currentTime * 1000 : null));
  }

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
    if (window.Avatar) window.Avatar.stopSpeaking();
    finish();
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

// ── AI video clip (play_video intent) ──────────────────────────────────────
let aiVideoFunscriptLoaded = false;

/**
 * Tell the backend a clip turn is over even though no clip ever played. The
 * display loop holds the whole stream while it waits for a video turn, so an
 * announcement that gets cut short (pause, stop, a new line) must release that
 * hold — otherwise nothing further is displayed until the clip's full length
 * has elapsed.
 */
async function releaseVideoHold() {
  try { await fetch('/api/video/ended', { method: 'POST' }); } catch (e) { /* ignore */ }
}

// `notifyEnded` tells the backend the clip stopped so AI device motion resumes
// right away. Pass false for silent teardown (e.g. before loading the next clip
// or when the whole session is stopping) so we don't release a hold prematurely.
async function stopAiVideo(notifyEnded = false) {
  const panel = document.getElementById('ai-video-panel');
  const video = document.getElementById('ai-video');
  if (video) {
    video.onended = null;  // avoid re-entrant end handling during teardown
    try { video.pause(); } catch (e) { /* ignore */ }
    video.removeAttribute('src');
    video.load();
  }
  // Hand the columns back to the transcript and avatar.
  const layout = document.querySelector('.ai-layout');
  if (layout) layout.classList.remove('video-mode');
  if (aiVideoFunscriptLoaded) {
    aiVideoFunscriptLoaded = false;
    try { await fetch('/api/funscript/stop', { method: 'POST' }); } catch (e) { /* ignore */ }
  }
  if (notifyEnded) {
    try { await fetch('/api/video/ended', { method: 'POST' }); } catch (e) { /* ignore */ }
  }
}

async function playAiVideo(info) {
  if (!info || !info.video_url) return;
  const panel = document.getElementById('ai-video-panel');
  const video = document.getElementById('ai-video');
  const title = document.getElementById('ai-video-title');
  if (!panel || !video) return;

  // Tear down any previous clip first (silently — a new clip is starting).
  await stopAiVideo(false);

  // The clip's own audio must not fight a voice-over: whatever the avatar was
  // saying is finished or abandoned by the time we get here.
  stopCurrentAudio();

  if (title) title.textContent = info.title || 'Video clip';
  // The clip takes over the transcript's and avatar's columns while it plays.
  const layout = document.querySelector('.ai-layout');
  if (layout) layout.classList.add('video-mode');

  // Pre-load the funscript (server-side) so it's ready when the video starts.
  aiVideoFunscriptLoaded = false;
  if (info.has_funscript && info.scene_id) {
    try {
      const res = await fetch(`/api/stash/funscript/${info.scene_id}`, { method: 'POST' });
      const data = await res.json();
      aiVideoFunscriptLoaded = Boolean(data.ok);
    } catch (e) {
      console.warn('Funscript load failed:', e);
    }
  }

  // Drive the funscript player from the video element's own clock.
  const syncStart = () => {
    if (!aiVideoFunscriptLoaded) return;
    fetch('/api/funscript/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ offset_ms: Math.floor(video.currentTime * 1000) }),
    }).catch(() => {});
  };
  const syncPause = () => {
    if (aiVideoFunscriptLoaded) fetch('/api/funscript/pause', { method: 'POST' }).catch(() => {});
  };
  // True once playback has reached (or been seeked to) the very end.
  const isAtEnd = () => video.duration && video.currentTime >= video.duration - 0.3;
  const syncSeek = () => {
    // Seeking to the end means the clip is over — end it so the AI resumes
    // instead of waiting out the clip's original length.
    if (isAtEnd()) {
      stopAiVideo(true);
      return;
    }
    if (aiVideoFunscriptLoaded) {
      fetch('/api/funscript/seek', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ position_ms: Math.floor(video.currentTime * 1000) }),
      }).catch(() => {});
    }
  };

  video.onplay = syncStart;
  video.onpause = syncPause;
  video.onseeked = syncSeek;
  video.onended = () => { stopAiVideo(true); };

  video.src = info.video_url;
  video.currentTime = 0;
  try {
    await video.play();
  } catch (e) {
    // Autoplay may be blocked; the user can press play (controls are shown).
    console.warn('Video autoplay blocked:', e);
  }
}

// ── Feedback (like / love / dislike / ban) ─────────────────────────────────
// like/love/dislike are mutually exclusive per card; ban is a separate sticky
// flag (a line can be both, e.g. disliked AND banned). Clicking an already-
// active button toggles it off (clear / unban). State is local to the card —
// cards are append-only and never re-rendered, so no server echo needed.
async function sendFeedback(index, reaction, fbBar, btn) {
  const wasActive = btn.classList.contains('active');
  let action;

  if (reaction === 'ban') {
    btn.classList.toggle('active', !wasActive);
    action = wasActive ? 'unban' : 'ban';
  } else {
    fbBar.querySelectorAll('.fb-btn:not(.fb-ban)').forEach(b => b.classList.remove('active'));
    if (wasActive) {
      action = 'clear';            // toggling off the active reaction
    } else {
      btn.classList.add('active');
      action = reaction;
    }
  }

  try {
    await fetch('/api/feedback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ index, reaction: action }),
    });
  } catch (err) {
    console.error('Feedback failed:', err);
  }
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
    <div class="turn-feedback" data-index="${item.index}">
      <button class="fb-btn" data-reaction="like" title="Like — more like this">👍</button>
      <button class="fb-btn" data-reaction="love" title="Love — stay in this register">❤️</button>
      <button class="fb-btn" data-reaction="dislike" title="Dislike — avoid this direction">👎</button>
      <button class="fb-btn fb-ban" data-reaction="ban" title="Ban — never say this again">🚫</button>
    </div>
  `;

  const speechEl = card.querySelector('.turn-speech');

  const fbBar = card.querySelector('.turn-feedback');
  fbBar.querySelectorAll('.fb-btn').forEach(btn => {
    btn.addEventListener('click', () => sendFeedback(item.index, btn.dataset.reaction, fbBar, btn));
  });

  // True once a video turn's spoken intro has taken responsibility for
  // starting the clip, so the block below does not also start it.
  let spokenIntroPending = false;

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

    // Start audio playback immediately with word sync + avatar lip-sync. On a
    // video turn the line introduces the clip, so the clip waits for it.
    playAudioWithWordSync(
      item.audio_url, item.words, speechEl, item.visemes,
      item.video
        ? (aborted) => (aborted ? releaseVideoHold() : playAiVideo(item.video))
        : null,
    );
    spokenIntroPending = Boolean(item.video);
  } else if (item.source === 'big' && item.speech) {
    // Fallback: typing animation when no TTS available
    enqueueTyping(speechEl, item.speech);
  } else {
    speechEl.textContent = item.speech || '';
  }

  // A video turn whose line is already playing starts the clip from that
  // line's completion callback instead — never both at once.
  if (item.video && !spokenIntroPending) {
    playAiVideo(item.video);
  } else if (item.source === 'big' && item.commands) {
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
  // Prefer pattern-level intensity (if present), otherwise fall back to commands.intensity
  let displayIntensity = null;
  if (cmds.pattern_intensity !== null && cmds.pattern_intensity !== undefined) displayIntensity = cmds.pattern_intensity;
  else if (cmds.intensity !== null && cmds.intensity !== undefined) displayIntensity = cmds.intensity;
  else if (cmds.ai_intensity !== null && cmds.ai_intensity !== undefined) displayIntensity = cmds.ai_intensity;
  if (displayIntensity !== null && displayIntensity !== undefined) document.getElementById('ai-param-intensity').textContent = displayIntensity;
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
  stopAiVideo();
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

const aiVideoCloseBtn = document.getElementById('ai-video-close');
if (aiVideoCloseBtn) aiVideoCloseBtn.addEventListener('click', () => { stopAiVideo(true); });

const aiVideoSkipBtn = document.getElementById('ai-video-skip');
if (aiVideoSkipBtn) aiVideoSkipBtn.addEventListener('click', () => { stopAiVideo(true); });

document.getElementById('ai-pause').addEventListener('click', async () => {
  try {
    const res = await fetch('/api/pause', { method: 'POST' });
    const data = await res.json();
    updateAIStatus(data.state);
    isRunning = data.state === 'running';
    stopTimer();
    // Suspend — don't tear down. Tearing the line down would kill the 'ended'
    // event a video turn's clip hand-off hangs on, stalling the stream.
    pauseCurrentAudio();
    const aiVideoEl = document.getElementById('ai-video');
    if (aiVideoEl && !aiVideoEl.paused) aiVideoEl.pause(); // also pause any clip

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

      // Pick the interrupted line back up, then the clip it was introducing
      // (or the clip that was already on screen when we paused).
      resumeCurrentAudio();
      const aiVideoEl = document.getElementById('ai-video');
      if (aiVideoEl && aiVideoEl.getAttribute('src') && aiVideoEl.paused && !isAudioPlaying) {
        aiVideoEl.play().catch(err => console.warn('Clip resume failed:', err));
      }

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
