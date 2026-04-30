const $settings = (id) => document.getElementById(id);

const settingsState = {
  promptNames: [],
  googleKeyPresent: false,
  groqKeyPresent: false,
};

function setSettingsMessage(message, isError = false) {
  const status = $settings('settings-prompt-status');
  if (!status) return;
  status.textContent = message;
  status.classList.toggle('error', Boolean(isError));
}

function setKeyState(provider, isPresent) {
  const state = $settings(`settings-${provider}-key-state`);
  const input = $settings(`settings-${provider}-key`);
  if (!state) return;
  state.textContent = isPresent ? 'Present' : 'Missing';
  state.classList.toggle('ok', Boolean(isPresent));
  state.classList.toggle('error', !isPresent);
  if (input) {
    input.placeholder = isPresent
      ? 'Enter a new key to replace the saved one'
      : `Enter a key to enable ${provider === 'google' ? 'Google' : 'Groq'} models`;
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

async function loadSettings() {
  const response = await fetch('/api/settings');
  const data = await response.json();

  settingsState.promptNames = data.prompt_names || [];
  settingsState.googleKeyPresent = Boolean(data.google_key_present);
  settingsState.groqKeyPresent = Boolean(data.groq_key_present);

  for (const provider of ['google', 'groq']) {
    const keyField = $settings(`settings-${provider}-key`);
    if (keyField) keyField.value = '';
    const status = $settings(`settings-${provider}-status`);
    const masked = data[`${provider}_api_key_masked`];
    if (status) status.textContent = masked ? `Saved key: ${masked}` : 'No saved key';

    const validation = data[`${provider}_validation`];
    setKeyState(provider, Boolean(data[`${provider}_key_present`]));
    setValidationState(
      provider,
      validation?.ok ? 'Valid' : (validation?.message || 'Not validated'),
      validation?.ok
    );
  }

  populateSelect($settings('settings-google-model'), data.google_model_options || [], data.google_model || '');
  populateSelect($settings('settings-groq-model'), data.groq_model_options || [], data.groq_model || '');
  populateSelect($settings('settings-prompt-name'), settingsState.promptNames, settingsState.promptNames[0] || '');

  $settings('settings-prompt-count').textContent = `${settingsState.promptNames.length} files`;

  const ttsCheckbox = $settings('settings-tts-enabled');
  if (ttsCheckbox) ttsCheckbox.checked = Boolean(data.tts_enabled);

  syncModelLock('google');
  syncModelLock('groq');
  setSettingsMessage('Settings loaded.');
}

function applySavedStates(data) {
  for (const provider of ['google', 'groq']) {
    const keyPresent = Boolean(data.saved?.[`${provider}_key_present`]);
    settingsState[provider === 'google' ? 'googleKeyPresent' : 'groqKeyPresent'] = keyPresent;
    setKeyState(provider, keyPresent);

    const validation = data[`${provider}_validation`];
    setValidationState(
      provider,
      validation?.ok ? 'Valid' : (validation?.message || 'Not validated'),
      validation?.ok
    );

    const status = $settings(`settings-${provider}-status`);
    const masked = data.saved?.[`${provider}_api_key_masked`];
    if (status) {
      status.textContent = masked ? `Saved key: ${masked}` : 'No saved key';
    }
  }

  syncModelLock('google');
  syncModelLock('groq');
}

function showValidationSummary(data) {
  const googleOk = Boolean(data.google_validation?.ok);
  const groqOk = Boolean(data.groq_validation?.ok);
  const anyFailed = !googleOk || !groqOk;

  if (googleOk && groqOk) {
    setSettingsMessage('Settings saved and both keys validated.');
  } else {
    setSettingsMessage('Settings saved, but one or more provider validations failed.', anyFailed);
  }

  const ttsCheckbox = $settings('settings-tts-enabled');
  if (ttsCheckbox) {
    ttsCheckbox.checked = Boolean(data.tts_enabled);
  }
}

async function updateProviderSettings(provider) {
  const keyField = $settings(`settings-${provider}-key`);
  const modelSelect = $settings(`settings-${provider}-model`);

  const payload = {
    [`${provider}_api_key`]: keyField ? keyField.value.trim() : '',
    [`${provider}_model`]: modelSelect ? modelSelect.value : '',
  };

  const response = await fetch('/api/settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await response.json();

  if (!response.ok || !data.ok) {
    setSettingsMessage(data.error || 'Failed to save settings', true);
    return;
  }

  applySavedStates(data);
  showValidationSummary(data);

  if (keyField) keyField.value = '';
}

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

async function updateTTSSetting() {
  const ttsCheckbox = $settings('settings-tts-enabled');
  if (!ttsCheckbox) return;

  const payload = {
    tts_enabled: ttsCheckbox.checked,
  };

  const response = await fetch('/api/settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await response.json();

  if (!response.ok || !data.ok) {
    setSettingsMessage(data.error || 'Failed to save TTS setting', true);
    return;
  }

  applySavedStates(data);
  setSettingsMessage(`TTS ${data.tts_enabled ? 'enabled' : 'disabled'}.`);
}

function wireSettingsEvents() {
  for (const provider of ['google', 'groq']) {
    const keyField = $settings(`settings-${provider}-key`);
    if (keyField) {
      keyField.addEventListener('input', () => syncModelLock(provider));
    }

    const updateBtn = $settings(`settings-update-${provider}`);
    if (updateBtn) updateBtn.addEventListener('click', () => updateProviderSettings(provider));
  }

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

  const updateTtsBtn = $settings('settings-update-tts');
  if (updateTtsBtn) updateTtsBtn.addEventListener('click', updateTTSSetting);
}

wireSettingsEvents();
loadSettings().catch((err) => {
  setSettingsMessage('Failed to load settings: ' + err.message, true);
});
