const $settings = (id) => document.getElementById(id);
const $$settings = (sel) => Array.from(document.querySelectorAll(sel));

const settingsState = {
  promptNames: [],
  googleKeyPresent: false,
  groqKeyPresent: false,
  dirty: false,
};

// One-line description per setting, shown in the "?" tooltip beside its label.
const SETTING_HELP = {
  google_api_key: 'Your Google Generative AI (Gemini/Gemma) API key. Stored locally and used to generate session turns.',
  google_model: 'Which Google model generates turns. Unlocks once a valid key is saved and tested.',
  groq_api_key: 'Your Groq API key for Groq-hosted models (Llama, Qwen, GPT-OSS). Stored locally.',
  groq_model: 'Which Groq model generates turns. Unlocks once a valid key is saved and tested.',
  gen_temperature: 'Sampling temperature (0–2). Higher is more random/creative, lower is more focused.',
  gen_top_p: 'Nucleus sampling (0–1). Restricts choices to the most probable tokens by cumulative probability.',
  gen_top_k: 'Top-k sampling. Restricts choices to the k most likely tokens (0 disables it).',
  google_timeout: 'Seconds to wait for a Google API response before the request fails.',
  groq_timeout: 'Seconds to wait for a Groq API response before the request fails.',
  big_model_max_retries: 'How many times to retry the main model after a failed request.',
  big_model_retry_delay: 'Seconds to wait between retries of the main model.',
  default_turns: 'Default value for the Turns field on the AI Session page.',
  display_interval: 'Seconds between each displayed (and spoken) turn.',
  low_watermark: 'When the pending buffer falls to this size, a new batch of turns is generated.',
  high_watermark: 'How many turns the model generates per batch.',
  generator_sleep: 'Seconds the generator waits between buffer checks.',
  banned_phrase_window: 'How many recent lines are fed back to the model as "do not repeat".',
  tts_enabled: 'Synthesize spoken audio for each turn using local Kokoro TTS.',
  kokoro_voice: 'Kokoro voice name used for speech (e.g. af_heart).',
  kokoro_speed: 'Speech speed multiplier (1.0 = normal).',
  kokoro_device: 'Compute device for TTS: auto, cpu, or cuda (GPU).',
  stash_video_enabled: 'Allow the AI to cut to random video clips from Stash.',
  video_chance: 'Probability (0–1) that any given turn becomes a video interlude.',
  stash_url: 'Base URL of your Stash server, e.g. http://192.168.1.50:9999.',
  stash_api_key: 'Stash API key (Settings ▸ Security in Stash). Kept server-side, never sent to the browser.',
  stash_tag: 'Only scenes carrying this tag are considered playable.',
  stash_proxy_enabled: 'Route all Stash requests through a SOCKS5 proxy.',
  stash_proxy_address: 'SOCKS5 proxy address as host:port, e.g. 127.0.0.1:2080.',
  device_ws_url: 'Default WebSocket URL used when connecting to an OSSM device.',
  coyote_ble_name: 'Advertised BLE name used to find your Coyote device when scanning.',
  coyote_soft_limit_a: 'Maximum allowed strength for Coyote channel A (safety cap).',
  coyote_soft_limit_b: 'Maximum allowed strength for Coyote channel B (safety cap).',
  coyote_freq_ms: 'Coyote pulse frequency in milliseconds.',
};

function injectHelpIcons() {
  for (const el of $$settings('[data-setting]')) {
    const tip = SETTING_HELP[el.dataset.setting];
    if (!tip || !el.id) continue;
    const label = document.querySelector(`label[for="${el.id}"]`);
    if (!label || label.querySelector('.settings-help')) continue;
    const help = document.createElement('span');
    help.className = 'settings-help';
    help.textContent = '?';
    help.dataset.tip = tip;     // styled CSS tooltip
    help.title = tip;           // native fallback (never clipped near edges)
    // The icon lives inside a <label>, so swallow clicks to avoid toggling
    // the associated checkbox / focusing the field.
    help.addEventListener('click', (e) => { e.preventDefault(); e.stopPropagation(); });
    label.appendChild(help);
  }
}

function setSettingsMessage(message, isError = false) {
  const status = $settings('settings-prompt-status');
  if (!status) return;
  status.textContent = message;
  status.classList.toggle('error', Boolean(isError));
}

function setGlobalStatus(message, isError = false) {
  const status = $settings('settings-global-status');
  if (!status) return;
  status.textContent = message || '';
  status.classList.toggle('error', Boolean(isError));
  status.classList.toggle('ok', Boolean(message) && !isError);
}

function setDirty(dirty) {
  settingsState.dirty = dirty;
  const indicator = $settings('settings-dirty');
  const bar = $settings('settings-savebar');
  if (indicator) indicator.textContent = dirty ? 'Unsaved changes' : 'All changes saved';
  if (bar) bar.classList.toggle('dirty', dirty);
}

function setKeyState(provider, isPresent) {
  const state = $settings(`settings-${provider}-key-state`);
  const input = $settings(`settings-${provider}-key`);
  if (state) {
    state.textContent = isPresent ? 'Present' : 'Missing';
    state.classList.toggle('ok', Boolean(isPresent));
    state.classList.toggle('error', !isPresent);
  }
  if (input) {
    input.placeholder = isPresent
      ? 'Enter a new key to replace the saved one'
      : `Enter a key to enable ${provider} access`;
  }
}

function setValidationState(provider, stateText, isOk) {
  const validation = $settings(`settings-${provider}-validation-state`);
  if (!validation) return;
  validation.textContent = stateText;
  validation.classList.toggle('ok', Boolean(isOk));
  validation.classList.toggle('error', isOk === false);
}

function populateSelect(select, options, selectedValue) {
  if (!select) return;
  select.innerHTML = '';
  for (const optionValue of options) {
    const option = document.createElement('option');
    option.value = optionValue;
    option.textContent = optionValue;
    if (optionValue === selectedValue) option.selected = true;
    select.appendChild(option);
  }
}

function syncModelLock(provider) {
  const keyField = $settings(`settings-${provider}-key`);
  const modelSelect = $settings(`settings-${provider}-model`);
  const status = $settings(`settings-${provider}-model-status`);
  const stateKey = provider === 'google' ? 'googleKeyPresent' : 'groqKeyPresent';
  const hasKey = settingsState[stateKey] || Boolean(keyField && keyField.value.trim());
  if (modelSelect) modelSelect.disabled = !hasKey;
  if (status) {
    status.textContent = hasKey ? 'Unlocked' : 'Locked until a working key is present';
  }
}

// ── Generic field <-> settings mapping ──────────────────────────────────────

function fillSettingFields(data) {
  for (const el of $$settings('[data-setting]')) {
    const key = el.dataset.setting;
    if (el.dataset.secret) {
      el.value = '';  // secrets are never echoed back; status text shows the mask
      continue;
    }
    if (!(key in data) || data[key] === null || data[key] === undefined) continue;
    if (el.type === 'checkbox') {
      el.checked = Boolean(data[key]);
    } else {
      el.value = data[key];
    }
  }
}

function collectSettings() {
  const payload = {};
  for (const el of $$settings('[data-setting]')) {
    const key = el.dataset.setting;
    if (el.dataset.secret) {
      const v = el.value.trim();
      if (v) payload[key] = v;  // blank → omit so the saved secret is kept
    } else if (el.type === 'checkbox') {
      payload[key] = el.checked;
    } else if (el.dataset.type === 'number') {
      if (el.value !== '') payload[key] = Number(el.value);
    } else {
      payload[key] = el.value;
    }
  }
  return payload;
}

// ── Status panel refresh ─────────────────────────────────────────────────────

function applyStatusPanel(data) {
  for (const provider of ['google', 'groq']) {
    const present = Boolean(data[`${provider}_key_present`]);
    settingsState[provider === 'google' ? 'googleKeyPresent' : 'groqKeyPresent'] = present;
    setKeyState(provider, present);

    const status = $settings(`settings-${provider}-status`);
    const masked = data[`${provider}_api_key_masked`];
    if (status) status.textContent = masked ? `Saved key: ${masked}` : 'No saved key';

    const validation = data[`${provider}_validation`];
    setValidationState(
      provider,
      validation?.ok ? 'Valid' : (validation?.message || 'Not validated'),
      validation?.ok
    );
  }

  const stashStatus = $settings('settings-stash-status');
  if (stashStatus) {
    const masked = data.stash_api_key_masked;
    stashStatus.textContent = masked ? `Saved key: ${masked}` : 'No saved key';
  }
  const stashV = data.stash_validation;
  setValidationState(
    'stash',
    stashV?.ok ? (stashV.message || 'Connected') : (stashV?.message || 'Not validated'),
    stashV?.ok
  );

  syncModelLock('google');
  syncModelLock('groq');
}

// ── Load ──────────────────────────────────────────────────────────────────────

async function loadSettings() {
  const response = await fetch('/api/settings');
  const data = await response.json();

  settingsState.promptNames = data.prompt_names || [];

  // Model dropdown options must be populated before generic fill sets the value.
  populateSelect($settings('settings-google-model'), data.google_model_options || [], data.google_model || '');
  populateSelect($settings('settings-groq-model'), data.groq_model_options || [], data.groq_model || '');
  populateSelect($settings('settings-prompt-name'), settingsState.promptNames, settingsState.promptNames[0] || '');

  fillSettingFields(data);
  applyStatusPanel(data);

  $settings('settings-prompt-count').textContent = `${settingsState.promptNames.length} files`;

  setDirty(false);
  setGlobalStatus('');
  setSettingsMessage('Settings loaded.');
}

// ── Save all ────────────────────────────────────────────────────────────────

async function saveAllSettings() {
  const payload = collectSettings();
  setGlobalStatus('Saving…');

  const response = await fetch('/api/settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await response.json().catch(() => ({}));

  if (!response.ok || !data.ok) {
    setGlobalStatus(data.error || 'Save failed', true);
    return;
  }

  // Fully refresh from the server so clamped/normalized values are reflected.
  await loadSettings();
  setGlobalStatus('All settings saved.');
}

// ── Per-service connectivity test ─────────────────────────────────────────────

async function testService(provider) {
  // Save first so the test runs against the values currently on screen.
  if (settingsState.dirty) {
    await saveAllSettings();
  }
  setValidationState(provider, 'Testing…', undefined);
  setGlobalStatus(`Testing ${provider}…`);

  const response = await fetch(`/api/settings/test/${provider}`, { method: 'POST' });
  const data = await response.json().catch(() => ({}));

  if (!response.ok || !data.ok) {
    setValidationState(provider, data.error || 'Test failed', false);
    setGlobalStatus(`${provider} test failed.`, true);
    return;
  }

  const v = data.validation || {};
  setValidationState(provider, v.ok ? 'Valid' : (v.message || 'Failed'), v.ok);
  if (provider === 'google' || provider === 'groq') {
    settingsState[provider === 'google' ? 'googleKeyPresent' : 'groqKeyPresent'] =
      settingsState[provider === 'google' ? 'googleKeyPresent' : 'groqKeyPresent'] || v.ok;
    syncModelLock(provider);
  }
  setGlobalStatus(v.ok ? `${provider} connected.` : `${provider}: ${v.message || 'failed'}.`, !v.ok);
}

// ── Prompt files (unchanged behaviour) ────────────────────────────────────────

async function downloadSelectedPrompt() {
  const select = $settings('settings-prompt-name');
  if (!select || !select.value) {
    setSettingsMessage('Select a prompt file first.', true);
    return;
  }

  const response = await fetch(`/api/prompts/${encodeURIComponent(select.value)}`);
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    setSettingsMessage(data.error || 'Download failed', true);
    return;
  }

  const blob = await response.blob();
  const url = window.URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = select.value.split('/').pop();
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.URL.revokeObjectURL(url);
  setSettingsMessage(`Downloaded ${select.value}.`);
}

async function uploadSelectedPrompt() {
  const select = $settings('settings-prompt-name');
  const fileInput = $settings('settings-upload-file');

  if (!select || !select.value) {
    setSettingsMessage('Select a prompt file first.', true);
    return;
  }
  if (!fileInput || !fileInput.files.length) {
    setSettingsMessage('Choose a file to upload.', true);
    return;
  }

  const formData = new FormData();
  formData.append('file', fileInput.files[0]);

  const response = await fetch(`/api/prompts/${encodeURIComponent(select.value)}`, {
    method: 'POST',
    body: formData,
  });
  const data = await response.json();

  if (!response.ok || !data.ok) {
    setSettingsMessage(data.error || 'Upload failed', true);
    return;
  }

  fileInput.value = '';
  setSettingsMessage(`Uploaded ${data.name}.`);
}

async function revertPromptOverrides() {
  if (!window.confirm('Delete all files from prompts/current and revert to base prompts?')) {
    return;
  }

  const response = await fetch('/api/prompts/revert', { method: 'POST' });
  const data = await response.json();

  if (!response.ok || !data.ok) {
    setSettingsMessage(data.error || 'Revert failed', true);
    return;
  }

  setSettingsMessage(`Reverted ${data.removed} override file(s).`);
}

// ── Wiring ────────────────────────────────────────────────────────────────────

function wireSettingsEvents() {
  // Any tracked field marks the form dirty.
  for (const el of $$settings('[data-setting]')) {
    const evt = (el.type === 'checkbox' || el.tagName === 'SELECT') ? 'change' : 'input';
    el.addEventListener(evt, () => setDirty(true));
  }

  for (const provider of ['google', 'groq']) {
    const keyField = $settings(`settings-${provider}-key`);
    if (keyField) keyField.addEventListener('input', () => syncModelLock(provider));
  }

  for (const btn of $$settings('[data-test]')) {
    btn.addEventListener('click', () => testService(btn.dataset.test));
  }

  const saveBtn = $settings('settings-save-all');
  if (saveBtn) saveBtn.addEventListener('click', saveAllSettings);

  const downloadButton = $settings('settings-download');
  if (downloadButton) downloadButton.addEventListener('click', downloadSelectedPrompt);

  const uploadButton = $settings('settings-upload');
  if (uploadButton) uploadButton.addEventListener('click', uploadSelectedPrompt);

  const revertButton = $settings('settings-revert');
  if (revertButton) revertButton.addEventListener('click', revertPromptOverrides);

  const promptSelect = $settings('settings-prompt-name');
  if (promptSelect) {
    promptSelect.addEventListener('change', () => {
      setSettingsMessage(`Selected ${promptSelect.value}.`);
    });
  }
}

injectHelpIcons();
wireSettingsEvents();
loadSettings().catch((err) => {
  setSettingsMessage('Failed to load settings: ' + err.message, true);
});
