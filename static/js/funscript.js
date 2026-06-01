/* funscript.js */
(function() {
  'use strict';

  let funscriptData = null;
  let isPlaying = false;
  let isPaused = false;
  let statusInterval = null;
  let latencyMs = 0;
  let invert = false;
  let lastStatusElapsedMs = 0;
  let animFrameId = null;
  let isUserSeeking = false;

  const WINDOW_MS = 5000; // 5-second rolling window

  const canvas = document.getElementById('funscript-canvas');
  const ctx = canvas ? canvas.getContext('2d') : null;
  const fileInput = document.getElementById('funscript-file-input');
  const btnLoad = document.getElementById('funscript-btn-load');
  const btnUpload = document.getElementById('funscript-btn-upload');
  const selectSaved = document.getElementById('funscript-saved-select');
  const btnPlay = document.getElementById('funscript-btn-play');
  const btnPause = document.getElementById('funscript-btn-pause');
  const btnStop = document.getElementById('funscript-btn-stop');
  const seekSlider = document.getElementById('funscript-seek');
  const timeDisplay = document.getElementById('funscript-time');
  const metaDisplay = document.getElementById('funscript-meta');
  const statusDisplay = document.getElementById('funscript-status');
  const targetPctDisplay = document.getElementById('funscript-target-pct');
  const latencyInput = document.getElementById('funscript-latency');
  const invertCheck = document.getElementById('funscript-invert');

  // Video elements
  const videoFileInput = document.getElementById('video-file-input');
  const videoBtnLoad = document.getElementById('video-btn-load');
  const videoBtnUpload = document.getElementById('video-btn-upload');
  const videoElement = document.getElementById('funscript-video');
  const videoOverlay = document.getElementById('video-play-overlay');
  const videoFilenameDisplay = document.getElementById('video-filename');
  const videoSavedSelect = document.getElementById('video-saved-select');
  let videoObjectUrl = null;
  let lastVideoFile = null;

  function formatTime(ms) {
    const s = Math.floor(ms / 1000);
    const m = Math.floor(s / 60);
    return `${m}:${(s % 60).toString().padStart(2, '0')}`;
  }

  function extractActions(data) {
    if (data.actions && data.actions.length > 0) return data.actions;
    if (data.axes && data.axes.length > 0) {
      for (const axis of data.axes) {
        if (axis.actions && axis.actions.length > 0) return axis.actions;
      }
    }
    return [];
  }

  // ── Time source for drawing ──────────────────────────────────────────────

  function getDrawTime() {
    if (videoElement && videoElement.readyState >= 2 && videoElement.duration > 0) {
      return videoElement.currentTime * 1000;
    }
    return lastStatusElapsedMs;
  }

  // ── Rolling-window heatmap ───────────────────────────────────────────────

  function drawRollingWindow(centerMs) {
    if (!ctx || !funscriptData) return;
    const actions = extractActions(funscriptData);
    if (actions.length === 0) return;
    const totalTime = actions[actions.length - 1].at;
    if (totalTime === 0) return;

    centerMs = Math.max(0, Math.min(centerMs, totalTime));

    let windowStart, windowEnd, playheadX;

    if (totalTime <= WINDOW_MS) {
      windowStart = 0;
      windowEnd = totalTime;
      playheadX = (centerMs / totalTime) * canvas.width;
    } else {
      const half = WINDOW_MS / 2;
      if (centerMs <= half) {
        windowStart = 0;
        windowEnd = WINDOW_MS;
        playheadX = (centerMs / WINDOW_MS) * canvas.width;
      } else if (centerMs >= totalTime - half) {
        windowEnd = totalTime;
        windowStart = totalTime - WINDOW_MS;
        playheadX = ((centerMs - windowStart) / WINDOW_MS) * canvas.width;
      } else {
        windowStart = centerMs - half;
        windowEnd = centerMs + half;
        playheadX = canvas.width * 0.5;
      }
    }

    const duration = windowEnd - windowStart || 1;

    // Background
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = '#1e1e2e';
    ctx.fillRect(0, 0, canvas.width, canvas.height);

    // Second grid lines
    const firstSec = Math.ceil(windowStart / 1000) * 1000;
    for (let t = firstSec; t <= windowEnd; t += 1000) {
      const x = ((t - windowStart) / duration) * canvas.width;
      ctx.strokeStyle = '#2a2a3a';
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, canvas.height); ctx.stroke();
    }

    // Find first visible action
    let startIdx = 0;
    for (let i = 0; i < actions.length; i++) {
      if (actions[i].at >= windowStart - 200) { startIdx = i; break; }
    }

    // Draw segments
    for (let i = startIdx; i < actions.length - 1; i++) {
      const a1 = actions[i], a2 = actions[i + 1];
      if (a1.at > windowEnd + 200) break;

      const x1 = ((a1.at - windowStart) / duration) * canvas.width;
      const x2 = ((a2.at - windowStart) / duration) * canvas.width;

      const p1 = invert ? 100 - a1.pos : a1.pos;
      const p2 = invert ? 100 - a2.pos : a2.pos;
      const y1 = canvas.height - (p1 / 100) * canvas.height;
      const y2 = canvas.height - (p2 / 100) * canvas.height;

      const speed = Math.abs(a2.pos - a1.pos) / (a2.at - a1.at || 1);
      const norm = Math.min(speed / 0.5, 1);
      ctx.strokeStyle = `rgb(${Math.floor(255 * norm)}, ${Math.floor(255 * (1 - norm))}, 100)`;
      ctx.lineWidth = 3;
      ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();

      if (x1 >= -5 && x1 <= canvas.width + 5) {
        ctx.fillStyle = '#fff';
        ctx.beginPath(); ctx.arc(x1, y1, 3, 0, Math.PI * 2); ctx.fill();
      }
    }

    // Playhead
    ctx.strokeStyle = '#00ffcc';
    ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(playheadX, 0); ctx.lineTo(playheadX, canvas.height); ctx.stroke();

    // Triangle marker at bottom
    ctx.fillStyle = '#00ffcc';
    ctx.beginPath();
    ctx.moveTo(playheadX, canvas.height - 2);
    ctx.lineTo(playheadX - 5, canvas.height - 10);
    ctx.lineTo(playheadX + 5, canvas.height - 10);
    ctx.fill();

    // Time labels
    ctx.fillStyle = '#888';
    ctx.font = '10px monospace';
    ctx.textAlign = 'left';
    ctx.fillText(formatTime(windowStart), 4, 12);
    ctx.textAlign = 'right';
    ctx.fillText(formatTime(windowEnd), canvas.width - 4, 12);
    ctx.textAlign = 'center';
    ctx.fillStyle = '#00ffcc';
    ctx.fillText(formatTime(centerMs), playheadX, 24);
  }

  // ── Animation loop (drives UI at 60 fps) ─────────────────────────────────

  function startAnimLoop() {
    if (animFrameId) return;
    function loop() {
      if (funscriptData) {
        const actions = extractActions(funscriptData);
        const totalTime = actions[actions.length - 1].at || 1;
        const currentMs = getDrawTime();

        // Update slider and time (unless user is actively dragging it)
        if (!isUserSeeking) {
          seekSlider.value = (currentMs / totalTime) * 100;
          timeDisplay.textContent = `${formatTime(currentMs)} / ${formatTime(totalTime)}`;
        }

        drawRollingWindow(currentMs);
      }
      animFrameId = requestAnimationFrame(loop);
    }
    animFrameId = requestAnimationFrame(loop);
  }

  // ── Video sync helpers (only on explicit state changes) ───────────────────

  function syncVideoPlay() {
    if (videoElement && videoElement.src && videoElement.paused && videoElement.readyState >= 2) {
      videoElement.play().catch(() => {});
    }
    if (videoOverlay) videoOverlay.style.display = 'none';
  }

  function syncVideoPause() {
    if (videoElement && !videoElement.paused) {
      videoElement.pause();
    }
    if (videoOverlay) videoOverlay.style.display = 'flex';
  }

  function syncVideoStop() {
    if (videoElement) {
      videoElement.pause();
      if (videoElement.readyState >= 1) {
        videoElement.currentTime = 0;
      }
    }
    if (videoOverlay) videoOverlay.style.display = 'flex';
  }

  function syncVideoSeek(ms) {
    if (videoElement && videoElement.readyState >= 1) {
      videoElement.currentTime = ms / 1000;
    }
  }

  // ── Funscript file loading ─────────────────────────────────────────────

  async function loadFile(file) {
    const text = await file.text();
    try {
      funscriptData = JSON.parse(text);
      const actions = extractActions(funscriptData);
      if (actions.length === 0) throw new Error('No actions found (check if multi-axis format)');
      actions.sort((a, b) => a.at - b.at);
      drawRollingWindow(0);
      startAnimLoop();
      metaDisplay.textContent = `${actions.length} points`;
      statusDisplay.textContent = 'Loaded (not saved)';
      isPaused = false;
      return funscriptData;
    } catch (e) {
      window.App.showError('Invalid funscript: ' + e.message);
      return null;
    }
  }

  async function uploadAndSave() {
    if (!funscriptData) { window.App.showError('Load a funscript first'); return; }
    const filename = prompt('Save as:', 'myscript.funscript');
    if (!filename) return;
    try {
      const res = await fetch('/api/funscript/upload', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ data: funscriptData, filename })
      });
      const data = await res.json();
      if (data.ok) {
        window.App.showInfo(`Saved: ${filename}`);
        statusDisplay.textContent = `Saved: ${filename} (${data.duration_str})`;
        isPaused = false;
        loadSavedList();
      } else {
        window.App.showError(data.error || 'Upload failed');
      }
    } catch (err) {
      window.App.showError('Upload error: ' + err.message);
    }
  }

  async function loadSavedList() {
    try {
      const res = await fetch('/api/funscript/list');
      const data = await res.json();
      if (data.ok) {
        selectSaved.innerHTML = '<option value="">-- Saved Funscripts --</option>';
        data.files.forEach(f => {
          const opt = document.createElement('option');
          opt.value = f; opt.textContent = f;
          selectSaved.appendChild(opt);
        });
      }
    } catch (e) { console.warn('Failed to list funscripts', e); }
  }

  async function loadSavedFile() {
    const filename = selectSaved.value;
    if (!filename) return;
    try {
      const dl = await fetch(`/api/funscript/download/${encodeURIComponent(filename)}`);
      const dlData = await dl.json();
      if (dlData.ok) {
        funscriptData = dlData.data;
        const actions = extractActions(funscriptData);
        actions.sort((a, b) => a.at - b.at);
        drawRollingWindow(0);
        startAnimLoop();
      }
      const res = await fetch('/api/funscript/load', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filename })
      });
      const data = await res.json();
      if (data.ok) {
        statusDisplay.textContent = `Loaded: ${filename} (${data.duration_str})`;
        isPaused = false;
        window.App.showInfo(`Loaded ${filename}`);
      } else {
        window.App.showError(data.error || 'Load failed');
      }
    } catch (err) {
      window.App.showError('Load error: ' + err.message);
    }
  }

  // ── Video loading ────────────────────────────────────────────────────────

  async function loadVideoList() {
    try {
      const res = await fetch('/api/funscript/videos');
      const data = await res.json();
      if (data.ok) {
        videoSavedSelect.innerHTML = '<option value="">-- Saved Videos --</option>';
        data.files.forEach(f => {
          const opt = document.createElement('option');
          opt.value = f; opt.textContent = f;
          videoSavedSelect.appendChild(opt);
        });
      }
    } catch (e) { console.warn('Failed to list videos', e); }
  }

  async function loadSavedVideo() {
    const filename = videoSavedSelect.value;
    if (!filename) return;
    if (videoObjectUrl) { URL.revokeObjectURL(videoObjectUrl); videoObjectUrl = null; }
    videoElement.src = `/api/funscript/video/${encodeURIComponent(filename)}`;
    videoFilenameDisplay.textContent = filename;
  }

  async function uploadVideo() {
    if (!lastVideoFile) { window.App.showError('Load a video file first'); return; }
    const formData = new FormData();
    formData.append('file', lastVideoFile);
    try {
      const res = await fetch('/api/funscript/video/upload', {
        method: 'POST',
        body: formData
      });
      const data = await res.json();
      if (data.ok) {
        window.App.showInfo(`Video saved: ${data.filename}`);
        loadVideoList();
      } else {
        window.App.showError(data.error || 'Upload failed');
      }
    } catch (err) {
      window.App.showError('Video upload error: ' + err.message);
    }
  }

  // ── API helpers ─────────────────────────────────────────────────────────

  async function apiCall(url, body) {
    try {
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: body ? JSON.stringify(body) : undefined
      });
      const text = await res.text();
      let data;
      try {
        data = JSON.parse(text);
      } catch (e) {
        console.error('Non-JSON response:', text.substring(0, 200));
        throw new Error('Server returned non-JSON (check console)');
      }
      return data;
    } catch (err) {
      throw err;
    }
  }

  async function updateConfig() {
    try {
      await apiCall('/api/funscript/config', { latency_ms: latencyMs, invert: invert });
    } catch (err) {
      console.warn('Config update failed', err);
    }
  }

  // ── Playback control ─────────────────────────────────────────────────────

  async function startPlayback() {
    if (isPlaying) return;

    if (!funscriptData && !selectSaved.value) {
      window.App.showError('Load a funscript first'); return;
    }

    if (isPaused) {
      try {
        const data = await apiCall('/api/funscript/resume', null);
        if (data.ok) {
          beginPlayState();
          isPaused = false;
        } else {
          window.App.showError(data.error || 'Resume failed');
        }
      } catch (err) {
        window.App.showError('Resume error: ' + err.message);
      }
      return;
    }

    if (funscriptData && statusDisplay.textContent.includes('not saved')) {
      try {
        const data = await apiCall('/api/funscript/play', { data: funscriptData, offset_ms: 0 });
        if (data.ok) {
          beginPlayState();
        } else {
          window.App.showError(data.error || 'Play failed');
        }
      } catch (err) {
        window.App.showError('Play error: ' + err.message);
      }
      return;
    }

    try {
      const data = await apiCall('/api/funscript/start', { offset_ms: 0 });
      if (data.ok) {
        beginPlayState();
      } else {
        window.App.showError(data.error || 'Start failed');
      }
    } catch (err) {
      window.App.showError('Start error: ' + err.message);
    }
  }

  function beginPlayState() {
    isPlaying = true;
    isPaused = false;
    btnPlay.textContent = '⏯ Resume';
    statusDisplay.textContent = 'Playing...';
    statusInterval = setInterval(pollStatus, 200);
    syncVideoPlay();
  }

  async function pausePlayback() {
    if (!isPlaying) return;
    try {
      const data = await apiCall('/api/funscript/pause', null);
      if (data.ok) {
        isPlaying = false;
        isPaused = true;
        btnPlay.textContent = '▶ Resume';
        statusDisplay.textContent = 'Paused';
        clearInterval(statusInterval);
        syncVideoPause();
      } else {
        window.App.showError(data.error || 'Pause failed');
      }
    } catch (err) {
      window.App.showError('Pause error: ' + err.message);
    }
  }

  async function stopPlayback() {
    try {
      const data = await apiCall('/api/funscript/stop', null);
      if (data.ok) {
        isPlaying = false;
        isPaused = false;
        btnPlay.textContent = '▶ Play';
        statusDisplay.textContent = 'Stopped';
        clearInterval(statusInterval);
        seekSlider.value = 0;
        targetPctDisplay.textContent = '--';
        timeDisplay.textContent = '0:00 / 0:00';
        lastStatusElapsedMs = 0;
        drawRollingWindow(0);
        syncVideoStop();
      } else {
        window.App.showError(data.error || 'Stop failed');
      }
    } catch (err) {
      window.App.showError('Stop error: ' + err.message);
    }
  }

  async function seekPlayback() {
    const pct = parseInt(seekSlider.value);
    const actions = funscriptData ? extractActions(funscriptData) : [];
    if (actions.length === 0) return;
    const totalTime = actions[actions.length - 1].at;
    const targetMs = Math.floor((pct / 100) * totalTime);
    try {
      const data = await apiCall('/api/funscript/seek', { position_ms: targetMs });
      if (data.ok) {
        statusDisplay.textContent = `Seeked to ${formatTime(targetMs)}`;
        syncVideoSeek(targetMs);
      }
    } catch (err) {
      window.App.showError('Seek error: ' + err.message);
    }
  }

  async function pollStatus() {
    try {
      const res = await fetch('/api/funscript/status');
      const text = await res.text();
      let data;
      try { data = JSON.parse(text); } catch (e) { return; }
      if (!data.ok) return;

      lastStatusElapsedMs = data.elapsed_ms;

      if (data.next_action) {
        targetPctDisplay.textContent = Math.round(data.next_action.pos) + '%';
      }

      // Detect natural finish from server side
      if (!data.running && data.elapsed_ms >= (data.total_ms * 0.99) && data.total_ms > 0) {
        isPlaying = false;
        isPaused = false;
        btnPlay.textContent = '▶ Play';
        statusDisplay.textContent = 'Finished';
        clearInterval(statusInterval);
        syncVideoPause();
      }
    } catch (e) {
      console.warn('Status poll failed', e);
    }
  }

  // ── Event listeners ──────────────────────────────────────────────────────

  if (btnLoad) btnLoad.addEventListener('click', () => fileInput.click());
  if (fileInput) {
    fileInput.addEventListener('change', async (e) => {
      const file = e.target.files[0];
      if (!file) return;
      await loadFile(file);
      e.target.value = '';
    });
  }
  if (btnUpload) btnUpload.addEventListener('click', uploadAndSave);
  if (selectSaved) selectSaved.addEventListener('change', loadSavedFile);
  if (btnPlay) btnPlay.addEventListener('click', startPlayback);
  if (btnPause) btnPause.addEventListener('click', pausePlayback);
  if (btnStop) btnStop.addEventListener('click', stopPlayback);

  if (seekSlider) {
    seekSlider.addEventListener('change', seekPlayback);

    // While dragging, preview the window and time without fighting the rAF loop
    seekSlider.addEventListener('mousedown', () => { isUserSeeking = true; });
    seekSlider.addEventListener('touchstart', () => { isUserSeeking = true; });
    seekSlider.addEventListener('input', () => {
      const actions = funscriptData ? extractActions(funscriptData) : [];
      if (actions.length === 0) return;
      const totalTime = actions[actions.length - 1].at;
      const targetMs = Math.floor((seekSlider.value / 100) * totalTime);
      timeDisplay.textContent = `${formatTime(targetMs)} / ${formatTime(totalTime)}`;
      drawRollingWindow(targetMs);
    });
  }

  if (latencyInput) {
    latencyInput.addEventListener('change', async () => {
      latencyMs = parseInt(latencyInput.value) || 0;
      await updateConfig();
    });
  }
  if (invertCheck) {
    invertCheck.addEventListener('change', async () => {
      invert = invertCheck.checked;
      drawRollingWindow(getDrawTime());
      await updateConfig();
    });
  }

  // Video events
  if (videoBtnLoad) videoBtnLoad.addEventListener('click', () => videoFileInput.click());
  if (videoFileInput) {
    videoFileInput.addEventListener('change', async (e) => {
      const file = e.target.files[0];
      if (!file) return;
      lastVideoFile = file;
      if (videoObjectUrl) URL.revokeObjectURL(videoObjectUrl);
      videoObjectUrl = URL.createObjectURL(file);
      videoElement.src = videoObjectUrl;
      videoFilenameDisplay.textContent = file.name;
      e.target.value = '';
    });
  }
  if (videoBtnUpload) videoBtnUpload.addEventListener('click', uploadVideo);
  if (videoSavedSelect) videoSavedSelect.addEventListener('change', loadSavedVideo);

  if (videoElement) {
    videoElement.addEventListener('click', () => {
      if (isPlaying) pausePlayback();
      else startPlayback();
    });
    videoElement.addEventListener('ended', () => {
      if (isPlaying) pausePlayback();
    });
  }

  window.FunscriptTab = {
    isPlaying: function() { return isPlaying; },
    stop: stopPlayback
  };

  loadSavedList();
  loadVideoList();
})();