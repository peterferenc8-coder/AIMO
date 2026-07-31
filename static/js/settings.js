const $settings = (id) => document.getElementById(id);
const $$settings = (sel) => Array.from(document.querySelectorAll(sel));

// Every AI back end that follows the key → Test → unlock model dropdown flow.
const AI_PROVIDERS = ['google', 'groq', 'openrouter'];

// The local OpenAI-compatible endpoint sits outside that flow: it is gated on a
// base URL rather than a key, and its model list is discovered from the server
// at Test time instead of being curated in config.
const LOCAL_PROVIDER = 'ollama';

const settingsState = {
  promptNames: [],
  keyPresent: Object.fromEntries(AI_PROVIDERS.map((p) => [p, false])),
  avatarModels: [],
  dirty: false,
};

// One-line description per setting, shown in the "?" tooltip beside its label.
const SETTING_HELP = {
  google_api_key: 'Your Google Generative AI (Gemini/Gemma) API key. Stored locally and used to generate session turns.',
  google_model: 'Which Google model generates turns. Unlocks once a valid key is saved and tested.',
  groq_api_key: 'Your Groq API key for Groq-hosted models (Llama, Qwen, GPT-OSS). Stored locally.',
  groq_model: 'Which Groq model generates turns. Unlocks once a valid key is saved and tested.',
  openrouter_api_key: 'Your OpenRouter API key. Stored locally. Only zero-cost ":free" models are offered.',
  openrouter_model: 'Which OpenRouter model generates turns. Unlocks once a valid key is saved and tested. Free models are capped at 20 requests/minute and 50/day (1000/day once the account has purchased $10 of credit).',
  openrouter_timeout: 'Seconds to wait for an OpenRouter response. Large reasoning models are far slower than Groq — leave this generous.',
  ollama_base_url: 'Address of a local OpenAI-compatible server — Ollama (http://localhost:11434), LM Studio (http://localhost:1234), llama.cpp or vLLM. A trailing "/v1" is optional.',
  ollama_api_key: 'Optional. A stock Ollama install needs no key; set one only if the endpoint sits behind an authenticating gateway or vLLM --api-key.',
  ollama_model: 'Which locally hosted model generates turns. The list is read from the server itself — press Test Connection to refresh it after pulling a new model.',
  ollama_timeout: 'Seconds to wait for a local response. One call generates a whole batch of turns, which a local model can spend minutes on — keep this well above the hosted timeouts.',
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
  buttplug_ws_url: 'WebSocket address of Intiface Central. Start Intiface and enable its server before connecting.',
  buttplug_vibe_floor: 'Minimum vibration for a non-zero stroke position. Motors need 50-100ms to spin up, so fast patterns can feel indistinct at 0; raise this to ~0.15 to keep pulses crisp.',
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

// Options are either plain strings (model names, prompt files) or
// {id, label} objects where the stored value and the display text differ —
// avatar models are ids like "user:foo.vrm" but should read as "foo.vrm".
function populateSelect(select, options, selectedValue) {
  if (!select) return;
  select.innerHTML = '';
  for (const entry of options) {
    const value = (entry && typeof entry === 'object') ? entry.id : entry;
    const label = (entry && typeof entry === 'object') ? entry.label : entry;
    const option = document.createElement('option');
    option.value = value;
    option.textContent = label;
    if (value === selectedValue) option.selected = true;
    select.appendChild(option);
  }
}

function syncModelLock(provider) {
  const keyField = $settings(`settings-${provider}-key`);
  const modelSelect = $settings(`settings-${provider}-model`);
  const status = $settings(`settings-${provider}-model-status`);
  const hasKey = settingsState.keyPresent[provider] || Boolean(keyField && keyField.value.trim());
  if (modelSelect) modelSelect.disabled = !hasKey;
  if (status) {
    status.textContent = hasKey ? 'Unlocked' : 'Locked until a working key is present';
  }
}

// ── Local endpoint (no key, discovered model list) ───────────────────────────

// The saved model is kept in the list even when it is absent from the last
// discovery, so a Save All before the first successful Test cannot silently
// wipe it (a <select> with no matching option reports an empty value).
function localModelOptions(discovered, saved) {
  const options = Array.from(discovered || []);
  if (saved && !options.includes(saved)) options.unshift(saved);
  return options;
}

function syncLocalModelLock(count) {
  const select = $settings(`settings-${LOCAL_PROVIDER}-model`);
  const status = $settings(`settings-${LOCAL_PROVIDER}-model-status`);
  if (select) select.disabled = count === 0;
  if (status) {
    status.textContent = count
      ? `${count} model${count === 1 ? '' : 's'} available`
      : 'Press Test Connection to load the models installed on the server';
  }
  const sideCount = $settings(`settings-${LOCAL_PROVIDER}-model-count`);
  if (sideCount) sideCount.textContent = count ? String(count) : 'None discovered';
}

function applyLocalPanel(data) {
  const discovered = data[`${LOCAL_PROVIDER}_model_options`] || [];
  const status = $settings(`settings-${LOCAL_PROVIDER}-status`);
  if (status) {
    status.textContent = data[`${LOCAL_PROVIDER}_endpoint_present`]
      ? (discovered.length ? 'Endpoint saved' : 'Endpoint saved — not tested yet')
      : 'No endpoint configured';
  }

  const validation = data[`${LOCAL_PROVIDER}_validation`];
  setValidationState(
    LOCAL_PROVIDER,
    validation?.ok ? 'Reachable' : (validation?.message || 'Not validated'),
    validation?.ok
  );

  syncLocalModelLock(localModelOptions(discovered, data[`${LOCAL_PROVIDER}_model`]).length);
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
  for (const provider of AI_PROVIDERS) {
    const present = Boolean(data[`${provider}_key_present`]);
    settingsState.keyPresent[provider] = present;
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

  for (const provider of AI_PROVIDERS) syncModelLock(provider);
  applyLocalPanel(data);
}

// ── Load ──────────────────────────────────────────────────────────────────────

async function loadSettings() {
  const response = await fetch('/api/settings');
  const data = await response.json();

  settingsState.promptNames = data.prompt_names || [];

  // Model dropdown options must be populated before generic fill sets the value.
  populateSelect($settings('settings-google-model'), data.google_model_options || [], data.google_model || '');
  populateSelect($settings('settings-groq-model'), data.groq_model_options || [], data.groq_model || '');
  populateSelect($settings('settings-openrouter-model'), data.openrouter_model_options || [], data.openrouter_model || '');
  populateSelect(
    $settings('settings-ollama-model'),
    localModelOptions(data.ollama_model_options, data.ollama_model),
    data.ollama_model || ''
  );
  populateSelect($settings('settings-prompt-name'), settingsState.promptNames, settingsState.promptNames[0] || '');

  settingsState.avatarModels = data.avatar_model_options || [];
  populateSelect($settings('settings-avatar-model'), settingsState.avatarModels, data.avatar_model || '');

  fillSettingFields(data);
  applyStatusPanel(data);

  // A saved id whose file has since been deleted leaves the select blank
  // (fillSettingFields cannot select a missing option). Fall back to the first
  // model so the dropdown always shows what the AI tab is actually rendering.
  const avatarSelect = $settings('settings-avatar-model');
  if (avatarSelect && !avatarSelect.value && settingsState.avatarModels.length) {
    avatarSelect.value = settingsState.avatarModels[0].id;
  }

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
  if (AI_PROVIDERS.includes(provider)) {
    settingsState.keyPresent[provider] = settingsState.keyPresent[provider] || v.ok;
    syncModelLock(provider);
  }
  if (provider === LOCAL_PROVIDER) {
    // The test doubles as model discovery. Repopulate even on failure — the
    // usual failure is "that model isn't installed", and the list is what tells
    // the user which ones are.
    const select = $settings(`settings-${LOCAL_PROVIDER}-model`);
    const current = select ? select.value : '';
    const options = localModelOptions(data.models, current);
    populateSelect(select, options, current);
    syncLocalModelLock(options.length);
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

// ── Avatar model ─────────────────────────────────────────────────────────────

function avatarModelUrl(modelId) {
  const match = settingsState.avatarModels.find((m) => m.id === modelId);
  return match ? match.url : '';
}

/** Swap the live avatar to the currently selected model, if it is mounted. */
function previewSelectedAvatar() {
  const select = $settings('settings-avatar-model');
  if (!select || !select.value) return;
  const url = avatarModelUrl(select.value);
  if (url && window.Avatar) window.Avatar.setModel(url);
}

function setAvatarMessage(message, isError = false) {
  const status = $settings('settings-avatar-status');
  if (!status) return;
  status.textContent = message;
  status.classList.toggle('error', Boolean(isError));
}

async function uploadAvatarModel() {
  const fileInput = $settings('settings-avatar-file');
  if (!fileInput || !fileInput.files.length) {
    setAvatarMessage('Choose a .vrm or .glb file to upload.', true);
    return;
  }

  const formData = new FormData();
  formData.append('file', fileInput.files[0]);

  setAvatarMessage('Uploading…');
  const response = await fetch('/api/avatar/model/upload', {
    method: 'POST',
    body: formData,
  });
  const data = await response.json();

  if (!response.ok || !data.ok) {
    setAvatarMessage(data.error || 'Upload failed', true);
    return;
  }

  // Rebuild the list so the new file is selectable, then select and preview it.
  settingsState.avatarModels = data.models || [];
  populateSelect($settings('settings-avatar-model'), settingsState.avatarModels, data.id);
  fileInput.value = '';
  previewSelectedAvatar();
  setDirty(true);
  setAvatarMessage(`Uploaded ${data.label} — press Save All to keep it.`);
}

// ── Wiring ────────────────────────────────────────────────────────────────────

function wireSettingsEvents() {
  // Any tracked field marks the form dirty.
  for (const el of $$settings('[data-setting]')) {
    const evt = (el.type === 'checkbox' || el.tagName === 'SELECT') ? 'change' : 'input';
    el.addEventListener(evt, () => setDirty(true));
  }

  for (const provider of AI_PROVIDERS) {
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

  const avatarUpload = $settings('settings-avatar-upload');
  if (avatarUpload) avatarUpload.addEventListener('click', uploadAvatarModel);

  // Preview on selection. The generic [data-setting] handler above already
  // marks the form dirty, so this only has to do the swap.
  const avatarSelect = $settings('settings-avatar-model');
  if (avatarSelect) avatarSelect.addEventListener('change', previewSelectedAvatar);
}

injectHelpIcons();
wireSettingsEvents();
loadSettings().catch((err) => {
  setSettingsMessage('Failed to load settings: ' + err.message, true);
});
