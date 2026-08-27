// Service worker: owns the actual download lifecycle so it survives the
// popup closing. The popup is only a thin UI — all state that must persist
// (which video is downloading, at what quality/mode, filename, errors) lives
// here in chrome.storage.session, keyed by the YouTube video URL.

const BACKEND_URL = 'https://all-social-media-downloader-production.up.railway.app';
const DEV_MODE = true;
const STORAGE_KEY = 'downloads';

function log(...args) {
  if (DEV_MODE) console.log('[YT-DL/bg]', ...args);
}

// Every backend call must go through this instead of `fetch(...).then(r =>
// r.json())` directly. A Railway/gunicorn-level failure (worker timeout,
// deploy crash, edge 502/504) never reaches our Flask app at all — it comes
// back as an HTML error page from the platform's own proxy, and blindly
// calling .json() on that throws the opaque "Unexpected token '<'" error.
// This reads the body as text first, checks Content-Type, and only then
// parses JSON, so every caller gets either real data or a clear Error.
async function fetchBackendJson(url, options = {}) {
  let response;
  try {
    response = await fetch(url, options);
  } catch (e) {
    throw new Error('Could not reach the backend. Check your connection and try again.');
  }

  let text;
  try {
    text = await response.text();
  } catch (e) {
    throw new Error(`Backend returned HTTP ${response.status} and its body could not be read.`);
  }

  const contentType = response.headers.get('Content-Type') || '';
  const isJson = contentType.toLowerCase().includes('application/json');

  if (!isJson || (text && !looksLikeJsonText(text))) {
    if (DEV_MODE) {
      // Safe diagnostic only: endpoint, status, content-type, a short body
      // preview. Never logs request headers, so the device token/cookies
      // this call may have sent are never at risk of ending up here.
      log('non-JSON backend response', {
        url,
        status: response.status,
        contentType,
        bodyPreview: text.slice(0, 200),
      });
    }
    throw new Error(`Backend returned HTTP ${response.status} instead of JSON. Check Railway deployment logs.`);
  }

  if (!text) {
    return { status: response.status, ok: response.ok, data: {} };
  }

  try {
    return { status: response.status, ok: response.ok, data: JSON.parse(text) };
  } catch (e) {
    if (DEV_MODE) {
      log('backend claimed JSON but body failed to parse', {
        url, status: response.status, bodyPreview: text.slice(0, 200),
      });
    }
    throw new Error(`Backend returned HTTP ${response.status} instead of JSON. Check Railway deployment logs.`);
  }
}

function looksLikeJsonText(text) {
  const trimmed = text.trimStart();
  return trimmed.startsWith('{') || trimmed.startsWith('[');
}

// ---- Per-device identity ----
// Every extension installation gets its own device_id + secret device_token
// from POST /device/register, persisted in chrome.storage.local so it
// survives browser restarts. This is what lets the backend give each office
// PC its own YouTube session instead of one shared global cookie set — the
// user still has to explicitly connect that session (see popup.js), this
// step only establishes *which device* is asking.
async function ensureDevice() {
  const stored = await chrome.storage.local.get(['device_id', 'device_token']);
  if (stored.device_id && stored.device_token) {
    return { device_id: stored.device_id, device_token: stored.device_token };
  }
  log('no device identity yet, registering');
  const { data } = await fetchBackendJson(`${BACKEND_URL}/device/register`, { method: 'POST' });
  if (!data.success) throw new Error(data.message || 'Device registration failed.');
  await chrome.storage.local.set({ device_id: data.device_id, device_token: data.device_token });
  log('device registered', data.device_id);
  return { device_id: data.device_id, device_token: data.device_token };
}

async function authHeaders(extra = {}) {
  const { device_id, device_token } = await ensureDevice();
  return { 'X-Device-Id': device_id, 'Authorization': `Bearer ${device_token}`, ...extra };
}

async function checkQualities(url) {
  const headers = await authHeaders({ 'Content-Type': 'application/json' });
  const { ok, data } = await fetchBackendJson(`${BACKEND_URL}/youtube/qualities`, {
    method: 'POST',
    headers,
    body: JSON.stringify({ url }),
  });
  return { ok, data };
}

async function connectSession(cookiesText) {
  const headers = await authHeaders({ 'Content-Type': 'application/json' });
  const { data } = await fetchBackendJson(`${BACKEND_URL}/youtube/session`, {
    method: 'POST',
    headers,
    body: JSON.stringify({ cookies: cookiesText }),
  });
  return data;
}

async function disconnectSession() {
  const headers = await authHeaders();
  const { data } = await fetchBackendJson(`${BACKEND_URL}/youtube/session`, { method: 'DELETE', headers });
  return data;
}

async function getSessionStatus() {
  const headers = await authHeaders();
  const { data } = await fetchBackendJson(`${BACKEND_URL}/youtube/session/status`, { method: 'GET', headers });
  return data;
}

function basename(path) {
  if (!path) return '';
  return path.split(/[\\/]/).pop();
}

async function getAllState() {
  const data = await chrome.storage.session.get(STORAGE_KEY);
  return data[STORAGE_KEY] || {};
}

async function getState(videoUrl) {
  const all = await getAllState();
  return all[videoUrl] || null;
}

async function setState(videoUrl, entry) {
  const all = await getAllState();
  all[videoUrl] = entry;
  await chrome.storage.session.set({ [STORAGE_KEY]: all });
  return entry;
}

async function findTrackedByDownloadId(downloadId) {
  const all = await getAllState();
  for (const [videoUrl, entry] of Object.entries(all)) {
    if (entry && entry.downloadId === downloadId) return [videoUrl, entry];
  }
  return null;
}

function broadcast(videoUrl, state) {
  // No listener (popup closed) throws/rejects — that's expected, swallow it.
  chrome.runtime.sendMessage({ type: 'DOWNLOAD_STATE_UPDATE', videoUrl, state }).catch(() => {});
}

async function startDownload(videoUrl, payload) {
  const existing = await getState(videoUrl);
  if (existing && ['processing', 'downloading'].includes(existing.status)) {
    log('duplicate download blocked for', videoUrl);
    return { ok: false, message: 'A download for this video is already in progress.' };
  }

  const entry = {
    // 'processing': the POST /download call is running yt-dlp/FFmpeg on the
    // backend right now and won't resolve until that's fully done.
    status: 'processing',
    quality: payload.quality ?? null,
    mode: payload.quality_mode || 'original',
    contentType: payload.content_type || 'video',
    downloadId: null,
    filename: null,
    error: null,
    startedAt: Date.now(),
  };
  await setState(videoUrl, entry);
  broadcast(videoUrl, entry);
  log('POST /download started', { videoUrl, payload });

  let data;
  try {
    const headers = await authHeaders({ 'Content-Type': 'application/json' });
    const result = await fetchBackendJson(`${BACKEND_URL}/download`, {
      method: 'POST',
      headers,
      body: JSON.stringify(payload),
    });
    data = result.data;
  } catch (e) {
    const failed = await setState(videoUrl, { ...entry, status: 'failed', error: e.message });
    broadcast(videoUrl, failed);
    log('backend unreachable or non-JSON', e.message);
    return { ok: false, message: e.message };
  }

  log('backend response', data);
  if (!data.success) {
    // The backend's own JSON error text, verbatim — no more mime-sniffing
    // needed to guess what went wrong, since /download no longer streams
    // the file (or a same-shaped error) through chrome.downloads itself.
    const failed = await setState(videoUrl, { ...entry, status: 'failed', error: data.message });
    broadcast(videoUrl, failed);
    log('backend rejected download', data.message);
    return { ok: false, message: data.message };
  }

  // The backend already finished all yt-dlp/FFmpeg work; data.download_url
  // is a plain GET, range-capable resource. Chrome's own network stack
  // fetches it directly — the extension's JS is never in the data path.
  try {
    const downloadId = await new Promise((resolve, reject) => {
      chrome.downloads.download({ url: data.download_url, saveAs: false }, (id) => {
        if (chrome.runtime.lastError || id === undefined) {
          reject(new Error(chrome.runtime.lastError?.message || 'Chrome refused to start the download'));
        } else {
          resolve(id);
        }
      });
    });
    const updated = await setState(videoUrl, {
      ...entry,
      status: 'downloading',
      downloadId,
      filename: data.filename,
    });
    broadcast(videoUrl, updated);
    log('download registered with Chrome', { videoUrl, downloadId, url: data.download_url });
    return { ok: true, downloadId };
  } catch (e) {
    const failed = await setState(videoUrl, { ...entry, status: 'failed', error: e.message });
    broadcast(videoUrl, failed);
    log('failed to start chrome download', e.message);
    return { ok: false, message: e.message };
  }
}

// The backend's /file/<id> route can still (rarely) answer with a small
// JSON error instead of the file — e.g. a race where the registered download
// expired between POST /download and this GET. Chrome would otherwise
// silently save that JSON as a wrongly-named file; detect and clean it up.
chrome.downloads.onChanged.addListener(async (delta) => {
  const tracked = await findTrackedByDownloadId(delta.id);
  if (!tracked) return;
  const [videoUrl, entry] = tracked;

  if (delta.filename && delta.filename.current && entry.status !== 'completed' && entry.status !== 'failed') {
    const updated = await setState(videoUrl, {
      ...entry,
      filename: basename(delta.filename.current),
    });
    broadcast(videoUrl, updated);
  }

  if (delta.state && delta.state.current === 'complete') {
    const [item] = await chrome.downloads.search({ id: delta.id });
    const mime = item?.mime || '';
    const looksLikeErrorPayload = mime.includes('json') && item && item.fileSize < 2048;

    if (looksLikeErrorPayload) {
      log('GET /file returned an error payload, cleaning up', { videoUrl, mime, size: item?.fileSize });
      await chrome.downloads.removeFile(delta.id).catch(() => {});
      await chrome.downloads.erase({ id: delta.id }).catch(() => {});
      const updated = await setState(videoUrl, {
        ...entry,
        status: 'failed',
        error: 'The prepared file was no longer available (it may have expired). Try downloading again.',
      });
      broadcast(videoUrl, updated);
    } else {
      const updated = await setState(videoUrl, {
        ...entry,
        status: 'completed',
        filename: item?.filename ? basename(item.filename) : entry.filename,
      });
      log('download completed', { videoUrl, filename: updated.filename });
      broadcast(videoUrl, updated);
    }
  } else if (delta.state && delta.state.current === 'interrupted') {
    const [item] = await chrome.downloads.search({ id: delta.id });
    const updated = await setState(videoUrl, {
      ...entry,
      status: 'failed',
      error: `Download interrupted (${item?.error || 'network interruption or browser cancellation'}).`,
    });
    log('download interrupted', { videoUrl, reason: item?.error });
    broadcast(videoUrl, updated);
  }
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === 'GET_STATE') {
    getState(message.videoUrl).then((state) => sendResponse(state || { status: 'idle' }));
    return true; // async
  }
  if (message?.type === 'START_DOWNLOAD') {
    startDownload(message.videoUrl, message.payload).then(sendResponse);
    return true; // async
  }
  if (message?.type === 'CLEAR_STATE') {
    setState(message.videoUrl, { status: 'idle' }).then(() => sendResponse({ ok: true }));
    return true;
  }
  if (message?.type === 'CHECK_QUALITIES') {
    checkQualities(message.url)
      .then(sendResponse)
      .catch((e) => sendResponse({ ok: false, data: { success: false, message: e.message } }));
    return true;
  }
  if (message?.type === 'CONNECT_SESSION') {
    connectSession(message.cookies)
      .then(sendResponse)
      .catch((e) => sendResponse({ success: false, message: e.message }));
    return true;
  }
  if (message?.type === 'DISCONNECT_SESSION') {
    disconnectSession()
      .then(sendResponse)
      .catch((e) => sendResponse({ success: false, message: e.message }));
    return true;
  }
  if (message?.type === 'SESSION_STATUS') {
    getSessionStatus()
      .then(sendResponse)
      .catch((e) => sendResponse({ success: false, message: e.message }));
    return true;
  }
  return false;
});
