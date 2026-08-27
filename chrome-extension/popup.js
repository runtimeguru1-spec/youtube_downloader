const BACKEND_URL = 'https://all-social-media-downloader-production.up.railway.app';
const DEV_MODE = true;
const YOUTUBE_RE = /^https?:\/\/(?:www\.)?youtube\.com\/watch\?[^#]*\bv=([\w-]+)|^https?:\/\/youtu\.be\/([\w-]+)/;

function log(...args) {
  if (DEV_MODE) console.log('[YT-DL/popup]', ...args);
}

function extractVideoId(url) {
  const m = url && url.match(YOUTUBE_RE);
  return m ? (m[1] || m[2]) : null;
}

// ---- DOM ----
const el = {
  notYoutube: document.getElementById('not-youtube'),
  backendOffline: document.getElementById('backend-offline'),
  retryBackendBtn: document.getElementById('retry-backend-btn'),
  main: document.getElementById('main-content'),
  videoTitle: document.getElementById('video-title'),
  videoUrl: document.getElementById('video-url'),
  checkBtn: document.getElementById('check-btn'),
  results: document.getElementById('results'),
  typeSelector: document.getElementById('type-selector'),
  qualityField: document.getElementById('quality-field'),
  qualityOptions: document.getElementById('quality-options'),
  modeField: document.getElementById('mode-field'),
  downloadBtn: document.getElementById('download-btn'),
  spinner: document.getElementById('spinner'),
  statusText: document.getElementById('status-text'),
  errorBox: document.getElementById('error-box'),
  sessionStatus: document.getElementById('session-status'),
  connectSessionBtn: document.getElementById('connect-session-btn'),
  disconnectSessionBtn: document.getElementById('disconnect-session-btn'),
  connectSessionForm: document.getElementById('connect-session-form'),
  sessionCookiesInput: document.getElementById('session-cookies-input'),
  submitSessionBtn: document.getElementById('submit-session-btn'),
  cancelSessionBtn: document.getElementById('cancel-session-btn'),
};

// ---- UI helpers ----
function setStatus(text, { spinning = false } = {}) {
  el.statusText.textContent = text;
  el.spinner.classList.toggle('hidden', !spinning);
}

function showError(message) {
  el.errorBox.textContent = message;
  el.errorBox.classList.remove('hidden');
}

function clearError() {
  el.errorBox.classList.add('hidden');
  el.errorBox.textContent = '';
}

// The backend already classifies YouTube errors (bot-check, rate limit,
// expired cookies, etc.) into a plain-language `message` — just show it.
function friendlyCheckError(rawMessage) {
  return rawMessage || 'Could not check this video.';
}

// ---- YouTube session (per-device, explicitly connected by the user) ----
function renderSessionUI(status) {
  if (status && status.connected) {
    el.sessionStatus.textContent = '✓ Connected — using your own YouTube session';
    el.sessionStatus.classList.remove('notice-error');
    el.sessionStatus.classList.add('notice-success');
    el.connectSessionBtn.classList.add('hidden');
    el.disconnectSessionBtn.classList.remove('hidden');
  } else {
    el.sessionStatus.textContent = 'Not connected — using the shared server session, if any (may hit YouTube sign-in checks).';
    el.sessionStatus.classList.remove('notice-success', 'notice-error');
    el.connectSessionBtn.classList.remove('hidden');
    el.disconnectSessionBtn.classList.add('hidden');
  }
  el.connectSessionForm.classList.add('hidden');
}

async function refreshSessionStatus() {
  try {
    const data = await chrome.runtime.sendMessage({ type: 'SESSION_STATUS' });
    renderSessionUI(data);
  } catch (e) {
    el.sessionStatus.textContent = 'Could not check session status.';
    el.sessionStatus.classList.add('notice-error');
  }
}

el.connectSessionBtn.addEventListener('click', () => {
  el.connectSessionForm.classList.remove('hidden');
  el.sessionCookiesInput.focus();
});

el.cancelSessionBtn.addEventListener('click', () => {
  el.connectSessionForm.classList.add('hidden');
  el.sessionCookiesInput.value = '';
});

el.submitSessionBtn.addEventListener('click', async () => {
  const cookies = el.sessionCookiesInput.value;
  if (!cookies.trim()) {
    showError('Paste your cookies.txt contents first.');
    return;
  }
  el.submitSessionBtn.disabled = true;
  el.submitSessionBtn.textContent = 'Saving...';
  clearError();
  try {
    const data = await chrome.runtime.sendMessage({ type: 'CONNECT_SESSION', cookies });
    if (!data.success) {
      showError(data.message || 'Could not save the session.');
    } else {
      el.sessionCookiesInput.value = '';
      await refreshSessionStatus();
    }
  } catch (e) {
    showError('Could not reach the backend to save the session.');
  } finally {
    el.submitSessionBtn.disabled = false;
    el.submitSessionBtn.textContent = 'Save Session';
  }
});

el.disconnectSessionBtn.addEventListener('click', async () => {
  el.disconnectSessionBtn.disabled = true;
  try {
    await chrome.runtime.sendMessage({ type: 'DISCONNECT_SESSION' });
    await refreshSessionStatus();
  } finally {
    el.disconnectSessionBtn.disabled = false;
  }
});

async function checkBackendReachable() {
  try {
    await fetch(`${BACKEND_URL}/`, { method: 'GET', signal: AbortSignal.timeout(3000) });
    return true;
  } catch (e) {
    log('backend unreachable:', e.message);
    return false;
  }
}

// ---- App state (reset whenever the video changes) ----
let state = {
  tabId: null,
  videoUrl: null,
  videoId: null,
  isChecking: false,
  qualities: [], // [{height, label}], height=null means "Best available"
  selectedQuality: null, // null = best available
  selectedMode: 'original',
  selectedType: 'video',
};

function resetForNewVideo(videoUrl, videoId) {
  state = {
    ...state,
    videoUrl,
    videoId,
    isChecking: false,
    qualities: [],
    selectedQuality: null,
    selectedMode: 'original',
    selectedType: 'video',
  };
  el.notYoutube.classList.add('hidden');
  el.main.classList.remove('hidden');
  el.videoUrl.textContent = videoUrl;
  el.videoTitle.textContent = '—';
  el.results.classList.add('hidden');
  el.downloadBtn.disabled = true;
  el.checkBtn.disabled = false;
  clearError();
  setStatus('Ready');
  log('reset for new video', videoId);
}

function renderQualityChips() {
  el.qualityOptions.innerHTML = '';
  const options = [{ height: null, label: 'Best Available' }, ...state.qualities];
  for (const q of options) {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'chip' + (state.selectedQuality === q.height ? ' active' : '');
    chip.textContent = q.label;
    chip.addEventListener('click', () => {
      state.selectedQuality = q.height;
      log('quality selected', q.height);
      renderQualityChips();
    });
    el.qualityOptions.appendChild(chip);
  }
}

function applyTypeVisibility() {
  const isAudio = state.selectedType === 'audio';
  el.qualityField.classList.toggle('hidden', isAudio);
  el.modeField.classList.toggle('hidden', isAudio);
}

// ---- Check Video ----
async function onCheckVideoClick() {
  if (state.isChecking) return;
  state.isChecking = true;
  el.checkBtn.disabled = true;
  clearError();
  setStatus('Checking video...', { spinning: true });
  log('quality check started', state.videoUrl);

  const reachable = await checkBackendReachable();
  if (!reachable) {
    el.backendOffline.classList.remove('hidden');
    setStatus('Failed', { spinning: false });
    state.isChecking = false;
    el.checkBtn.disabled = false;
    return;
  }
  el.backendOffline.classList.add('hidden');

  try {
    const { ok, data } = await chrome.runtime.sendMessage({ type: 'CHECK_QUALITIES', url: state.videoUrl });
    log('quality response received', { ok, success: data.success });

    if (!ok || !data.success) {
      showError(friendlyCheckError(data.message));
      setStatus('Failed');
      return;
    }

    state.qualities = data.qualities || [];
    el.videoTitle.textContent = data.title || state.videoUrl;
    renderQualityChips();
    applyTypeVisibility();
    el.results.classList.remove('hidden');
    el.downloadBtn.disabled = false;
    setStatus('Qualities loaded');
    setTimeout(() => setStatus('Waiting for selection'), 700);
  } catch (e) {
    log('quality check error', e.message);
    showError('Could not reach the backend to check this video.');
    setStatus('Failed');
  } finally {
    state.isChecking = false;
    el.checkBtn.disabled = false;
  }
}

// ---- Download ----
function buildPayload() {
  const payload = {
    url: state.videoUrl,
    platform: 'youtube',
    content_type: state.selectedType,
  };
  if (state.selectedType === 'video') {
    if (state.selectedQuality) payload.quality = state.selectedQuality;
    payload.quality_mode = state.selectedMode;
  }
  return payload;
}

async function onDownloadClick() {
  el.downloadBtn.disabled = true; // synchronous guard against double-click
  clearError();
  setStatus('Starting download...', { spinning: true });
  const payload = buildPayload();
  log('download request started', payload);

  const response = await chrome.runtime.sendMessage({
    type: 'START_DOWNLOAD',
    // Keyed by video ID, not the raw URL string — incidental differences
    // like a `&t=42s` timestamp shouldn't be treated as a different video.
    videoUrl: state.videoId,
    payload,
  });
  log('backend response status', response);

  if (!response.ok) {
    showError(response.message || 'Could not start the download.');
    setStatus('Failed');
    el.downloadBtn.disabled = false;
  }
  // On success we wait for DOWNLOAD_STATE_UPDATE messages from background.js
  // to drive the status text through processing -> downloading -> completed.
}

function applyDownloadState(s) {
  if (!s) return;
  switch (s.status) {
    case 'processing':
      setStatus('Processing...', { spinning: true });
      el.downloadBtn.disabled = true;
      break;
    case 'downloading':
      setStatus(s.filename ? `Downloading... (${s.filename})` : 'Downloading...', { spinning: true });
      el.downloadBtn.disabled = true;
      break;
    case 'completed':
      setStatus(s.filename ? `Completed — ${s.filename}` : 'Completed', { spinning: false });
      log('download completed', s.filename);
      el.downloadBtn.disabled = false;
      break;
    case 'failed':
      setStatus('Failed', { spinning: false });
      if (s.error) showError(s.error);
      log('download failed', s.error);
      el.downloadBtn.disabled = false;
      break;
    default:
      break;
  }
}

chrome.runtime.onMessage.addListener((message) => {
  if (message?.type === 'DOWNLOAD_STATE_UPDATE' && message.videoUrl === state.videoId) {
    applyDownloadState(message.state);
  }
});

// ---- Type / quality / mode wiring ----
el.typeSelector.addEventListener('click', (e) => {
  const btn = e.target.closest('.seg');
  if (!btn) return;
  state.selectedType = btn.dataset.value;
  [...el.typeSelector.children].forEach((c) => c.classList.toggle('active', c === btn));
  applyTypeVisibility();
});

document.querySelectorAll('input[name="mode"]').forEach((radio) => {
  radio.addEventListener('change', (e) => {
    state.selectedMode = e.target.value;
    log('mode selected', state.selectedMode);
  });
});

el.checkBtn.addEventListener('click', onCheckVideoClick);
el.downloadBtn.addEventListener('click', onDownloadClick);
el.retryBackendBtn.addEventListener('click', async () => {
  if (await checkBackendReachable()) {
    el.backendOffline.classList.add('hidden');
  }
});

// ---- SPA navigation: same tab, URL changes without a full reload ----
chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (tabId !== state.tabId || !changeInfo.url) return;
  const newVideoId = extractVideoId(changeInfo.url);
  if (newVideoId && newVideoId !== state.videoId) {
    log('SPA navigation detected, resetting', newVideoId);
    resetForNewVideo(changeInfo.url, newVideoId);
  } else if (!newVideoId) {
    // Navigated away from a video entirely.
    el.main.classList.add('hidden');
    el.notYoutube.classList.remove('hidden');
  }
});

// ---- Init ----
async function init() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const videoId = tab?.url ? extractVideoId(tab.url) : null;
  log('current tab url', tab?.url);

  if (!tab?.url || !videoId) {
    el.notYoutube.classList.remove('hidden');
    el.main.classList.add('hidden');
    return;
  }

  el.notYoutube.classList.add('hidden');
  el.main.classList.remove('hidden');
  state.tabId = tab.id;
  resetForNewVideo(tab.url, videoId);

  const reachable = await checkBackendReachable();
  el.backendOffline.classList.toggle('hidden', reachable);
  if (reachable) refreshSessionStatus();

  // Restore any in-progress/completed/failed download state for this exact
  // video so reopening the popup mid-download shows the real status instead
  // of resetting to "Ready".
  const existing = await chrome.runtime.sendMessage({ type: 'GET_STATE', videoUrl: state.videoId });
  if (existing && existing.status !== 'idle') {
    applyDownloadState(existing);
  }
}

init();
