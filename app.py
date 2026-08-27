import os
import sys
import json
import logging
import shutil
import socket
import uuid
import tempfile
import traceback
import yt_dlp
import instaloader
import requests
import unicodedata
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from fake_useragent import UserAgent
import time
import random
import mimetypes
from flask import Flask, request, jsonify, send_file, g
from werkzeug.exceptions import HTTPException
from urllib.parse import urlparse
from functools import wraps
import threading
import subprocess
from bs4 import BeautifulSoup
import re

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024  # 2MB - generous for a cookies.txt upload, tight enough to block abuse

# CORS for the companion Chrome extension. Extension pages (popup/background)
# send an `Origin: chrome-extension://<32-char-id>` header on cross-origin
# fetch(); POST + Content-Type: application/json is a "non-simple" request,
# so the browser preflights it with OPTIONS before the real call. Scoped to
# chrome-extension origins (any installed extension) plus the backend's own
# origin for direct same-machine testing — not a blanket `*`.
_CORS_ORIGIN_RE = re.compile(r'^chrome-extension://[a-p]{32}$')
_CORS_ALLOWED_ORIGINS = {'http://127.0.0.1:5000', 'http://localhost:5000'}


@app.after_request
def _add_cors_headers(response):
    origin = request.headers.get('Origin', '')
    if origin and (_CORS_ORIGIN_RE.match(origin) or origin in _CORS_ALLOWED_ORIGINS):
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, DELETE, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-Device-Id'
        response.headers['Vary'] = 'Origin'
    return response


# Force line-buffered stdout: when this process's output is captured by an IDE
# run window, a pipe, or `python app.py > log.txt`, Python defaults to full
# block buffering (only flushes when the buffer fills or the process exits),
# which looks exactly like "no logs ever appear" while a request is in flight.
try:
    sys.stdout.reconfigure(line_buffering=True)
except (AttributeError, ValueError):
    pass

# A dedicated, non-propagating handler so our logs always print regardless of
# whatever Flask/Werkzeug did to the root logger (logging.basicConfig() is a
# no-op if the root logger already has handlers, which would otherwise make
# every _stage_log call below silently disappear).
logger = logging.getLogger('downloader')
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
    logger.addHandler(_handler)
    logger.propagate = False

# Imported only after the 'downloader' logger above is fully configured —
# session_store logs its Redis/SESSION_ENCRYPTION_KEY startup diagnostics at
# import time, and importing it any earlier means those lines print
# unformatted (or get silently dropped) because the logger has no handler yet.
import session_store


def _stage_log(download_id, stage, message=''):
    logger.info(f'[{download_id}] {stage}' + (f' - {message}' if message else ''))


# Configure temporary directory
TEMP_DIR = tempfile.mkdtemp()

FFMPEG_BIN = shutil.which('ffmpeg') or 'ffmpeg'
FFPROBE_BIN = shutil.which('ffprobe') or 'ffprobe'

# --- Prepared-download registry -------------------------------------------
# /download does all the (slow) yt-dlp/FFmpeg work and registers the result
# here; /file/<download_id> serves the already-finished file as a normal GET
# resource. This is what lets IDM (or Chrome, or curl) intercept a real,
# range-capable download URL instead of the POST processing endpoint.
DOWNLOAD_FILES = {}
DOWNLOAD_FILES_LOCK = threading.Lock()
DOWNLOAD_EXPIRY_SECONDS = 45 * 60  # 45 minutes
_DOWNLOAD_ID_RE = re.compile(r'^[0-9a-f]{32}$')

# --- Progress registry ------------------------------------------------------
# Keyed by the same download_id used throughout download_youtube(). The
# frontend polls GET /progress/<id> while the blocking POST /download is in
# flight so a slow FFmpeg transcode doesn't look like a frozen app.
PROGRESS = {}
PROGRESS_LOCK = threading.Lock()


def _set_progress(download_id, stage, percent=None, speed=None, message=''):
    with PROGRESS_LOCK:
        PROGRESS[download_id] = {
            'stage': stage,
            'percent': percent,
            'speed': speed,
            'message': message,
            'updated_at': time.time(),
        }


def register_download_file(path, download_name, temp_dir):
    """Registers a completed file for GET retrieval and returns its download_id.
    The id is the only thing ever exposed in a URL — the real path never is."""
    download_id = uuid.uuid4().hex
    with DOWNLOAD_FILES_LOCK:
        DOWNLOAD_FILES[download_id] = {
            'path': path,
            'download_name': download_name,
            'temp_dir': temp_dir,
            'created_at': time.time(),
        }
    logger.info(f'REGISTERED DOWNLOAD FILE - id={download_id} path={path}')
    return download_id


def _cleanup_expired_downloads():
    now = time.time()
    with DOWNLOAD_FILES_LOCK:
        expired_ids = [did for did, entry in DOWNLOAD_FILES.items()
                       if now - entry['created_at'] > DOWNLOAD_EXPIRY_SECONDS]
        expired_entries = [DOWNLOAD_FILES.pop(did) for did in expired_ids]
    for entry in expired_entries:
        logger.info(f"EXPIRED DOWNLOAD CLEANUP - removing {entry['temp_dir']}")
        shutil.rmtree(entry['temp_dir'], ignore_errors=True)
    with PROGRESS_LOCK:
        stale_ids = [did for did, entry in PROGRESS.items()
                     if now - entry['updated_at'] > DOWNLOAD_EXPIRY_SECONDS]
        for did in stale_ids:
            PROGRESS.pop(did, None)


def _download_cleanup_loop():
    while True:
        time.sleep(300)  # sweep every 5 minutes
        try:
            _cleanup_expired_downloads()
        except Exception as e:
            logger.error(f'DOWNLOAD CLEANUP ERROR - {e}')

# Network stalls (slow/broken connections) must raise, never hang the request forever.
YT_SOCKET_TIMEOUT = 30
YT_RETRIES = 5

# Player clients that return progressive/adaptive formats without requiring
# YouTube login cookies (the 'web' client needs a PO token and raises
# "Sign in to confirm you're not a bot" otherwise).
YT_PLAYER_CLIENTS = ['android', 'ios', 'tv']


_MAX_COOKIE_CHUNKS = 20


def _read_chunked_cookies():
    """Reads YOUTUBE_COOKIES_1, YOUTUBE_COOKIES_2, ... in order and joins them
    back into one Netscape cookie file. Used when the cookies don't fit in a
    single YOUTUBE_COOKIES variable (Railway caps a variable at 32768 chars).
    Stops at the first missing index, so a gap never silently drops the rest
    of a longer sequence. Each chunk is expected to end on a complete cookie
    line, so only its own leading/trailing newlines are trimmed (never
    interior whitespace) before rejoining with '\\n'.
    Returns (reconstructed_text_or_None, chunk_count)."""
    chunks = []
    for i in range(1, _MAX_COOKIE_CHUNKS + 1):
        chunk = os.environ.get(f'YOUTUBE_COOKIES_{i}')
        if not chunk:
            break
        chunks.append(chunk.strip('\n'))
    return ('\n'.join(chunks) if chunks else None), len(chunks)


def _setup_youtube_cookiefile():
    """Writes YOUTUBE_COOKIES — or, if that's unset, the reconstructed
    YOUTUBE_COOKIES_1.._N chunks — to a private temp file yt-dlp can use as
    `cookiefile`. Never logs the content. Returns None if no cookies are
    configured — cookie-less requests still work for videos that don't need
    sign-in verification."""
    raw = os.environ.get('YOUTUBE_COOKIES')
    if raw:
        source = 'YOUTUBE_COOKIES'
    else:
        raw, chunk_count = _read_chunked_cookies()
        source = f'{chunk_count} chunked YOUTUBE_COOKIES_1..{chunk_count} vars' if raw else None

    if not raw:
        logger.info('No YOUTUBE_COOKIES or YOUTUBE_COOKIES_1..N env vars set - continuing without cookies')
        return None
    try:
        fd, path = tempfile.mkstemp(prefix='ytcookies_', suffix='.txt', dir=TEMP_DIR)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(raw)
        os.chmod(path, 0o600)
        logger.info(f'YouTube cookiefile created from {source} ({len(raw)} chars)')
        return path
    except OSError as e:
        logger.error(f'Failed to write YouTube cookiefile: {e}')
        return None


YOUTUBE_COOKIE_FILE = _setup_youtube_cookiefile()
_DENO_AVAILABLE = shutil.which('deno') is not None
if not _DENO_AVAILABLE:
    logger.warning('deno binary not found - YouTube JS-challenge solving (EJS) is disabled')

# Substrings of yt-dlp/requests error messages mapped to a machine-readable
# code + a message safe to show the extension (never the raw exception, which
# can otherwise leak file paths or other internals).
_YT_ERROR_PATTERNS = [
    (('sign in to confirm', 'not a bot'), 'YOUTUBE_BOT_CHECK',
     "YouTube is asking for bot/sign-in verification. The server's YouTube cookies may be missing or expired."),
    (('429', 'too many requests'), 'YOUTUBE_RATE_LIMITED',
     'YouTube is rate-limiting requests from this server right now. Please wait a bit and try again.'),
    (('cookies are no longer valid', 'has expired'), 'YOUTUBE_COOKIES_EXPIRED',
     "The server's YouTube cookies have expired. An administrator needs to refresh them."),
    (('private video', 'sign in to view'), 'YOUTUBE_AUTH_REQUIRED',
     'This video is private or requires an account that has access to it.'),
    (('video unavailable',), 'YOUTUBE_VIDEO_UNAVAILABLE',
     'This video is unavailable (it may be deleted, private, or region-locked).'),
    (('unsupported url', 'is not a valid url'), 'INVALID_URL',
     'That does not look like a valid YouTube video URL.'),
    (('unable to download webpage',), 'YOUTUBE_UNREACHABLE',
     'Could not reach YouTube to fetch this video. Try again shortly.'),
]


def classify_youtube_error(exc):
    """Maps a yt-dlp exception to (error_code, safe_message) for the API response."""
    msg = str(exc).lower()
    for keywords, code, friendly in _YT_ERROR_PATTERNS:
        if any(k in msg for k in keywords):
            return code, friendly
    return 'YOUTUBE_EXTRACTION_FAILED', 'Could not process this YouTube video. It may be unavailable or YouTube may be blocking the request.'


# --- Simple per-IP rate limiting for the expensive yt-dlp routes -----------
# ponytail: in-memory sliding window, correct only for a single gunicorn
# worker process (the current deployment). Swap for Redis if scaled to
# multiple workers/dynos.
_RATE_LIMIT_WINDOW_SECONDS = 60
_RATE_LIMIT_MAX_REQUESTS = 12
_rate_limit_hits = {}
_rate_limit_lock = threading.Lock()


def _is_rate_limited(key):
    now = time.time()
    with _rate_limit_lock:
        hits = [t for t in _rate_limit_hits.get(key, []) if now - t < _RATE_LIMIT_WINDOW_SECONDS]
        hits.append(now)
        _rate_limit_hits[key] = hits
        return len(hits) > _RATE_LIMIT_MAX_REQUESTS


# --- Per-device auth (device_id + bearer token) for the multi-office-PC setup
# YOUTUBE_COOKIES/_N above remains as an optional fallback/admin session used
# whenever a request has no device auth, or its device hasn't connected its
# own session yet. -----------------------------------------------------------

class DeviceAuthError(Exception):
    """Raised when X-Device-Id/Authorization headers were sent but are invalid."""


def _extract_bearer_token():
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        return auth[len('Bearer '):].strip()
    return None


def _resolve_authenticated_device():
    """Returns the verified device_id for this request, or None if no device
    auth headers were sent at all (caller should fall back to the shared
    global/admin cookiefile). Raises DeviceAuthError if headers were sent but
    don't verify."""
    device_id = request.headers.get('X-Device-Id', '').strip()
    token = _extract_bearer_token()
    if not device_id and not token:
        return None
    if not session_store.verify_device(device_id, token):
        raise DeviceAuthError()
    return device_id


def require_device_auth(view):
    """Hard-requires valid device auth (unlike _resolve_authenticated_device,
    used where an anonymous/global fallback makes no sense — managing a
    specific device's own session)."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        device_id = request.headers.get('X-Device-Id', '').strip()
        token = _extract_bearer_token()
        if not session_store.verify_device(device_id, token):
            return jsonify({'success': False, 'message': 'Invalid or missing device authentication.',
                             'error_code': 'DEVICE_AUTH_FAILED'}), 401
        g.device_id = device_id
        return view(*args, **kwargs)
    return wrapper


def _prepare_request_cookiefile(device_id):
    """Returns (cookiefile_path_or_None, is_device_specific, cleanup_fn). The
    returned cleanup_fn must always be called (in a finally:) once the yt-dlp
    call is done — it deletes the per-request temp file for a device session,
    and is a no-op when falling back to the shared global cookiefile (which
    is reused across requests and must not be deleted here)."""
    if device_id:
        cookie_text = session_store.load_session_cookies(device_id)
        if cookie_text:
            fd, path = tempfile.mkstemp(prefix='ytcookies_dev_', suffix='.txt', dir=TEMP_DIR)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(cookie_text)
            os.chmod(path, 0o600)

            def _cleanup():
                try:
                    os.remove(path)
                except OSError:
                    pass
            return path, True, _cleanup
    return YOUTUBE_COOKIE_FILE, False, (lambda: None)


# Bot-check/auth-ish errors are only actionable by the affected user when a
# device-specific session was in play — remap those to a distinct code so the
# extension can say "reconnect your session" instead of a generic failure.
_SESSION_EXPIRY_CODES = {'YOUTUBE_BOT_CHECK', 'YOUTUBE_COOKIES_EXPIRED', 'YOUTUBE_AUTH_REQUIRED'}


def _remap_session_error(code, friendly, is_device_specific):
    if is_device_specific and code in _SESSION_EXPIRY_CODES:
        return 'YOUTUBE_SESSION_EXPIRED', 'Your YouTube session needs to be reconnected.'
    return code, friendly


_QUALITY_LABELS = {2160: '2160p / 4K', 1440: '1440p / 2K', 1080: '1080p', 720: '720p', 480: '480p', 360: '360p', 240: '240p', 144: '144p'}
_ILLEGAL_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class DownloadError(Exception):
    """Carries a machine-readable stage code alongside the message."""
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(message)


def sanitize_filename(name, max_len=150):
    name = _ILLEGAL_FILENAME_CHARS.sub('_', name or '').strip(' .')
    name = re.sub(r'\s+', ' ', name)
    return name[:max_len] if name else 'download'


def _yt_base_opts(cookiefile=None):
    """cookiefile: per-request override (a device's own session). Defaults to
    the shared global/admin cookiefile (YOUTUBE_COOKIE_FILE) when omitted, so
    every yt-dlp call site keeps working unchanged unless it opts in."""
    opts = {
        'quiet': False,
        'no_warnings': False,
        'noplaylist': True,

        # Timeout / retry protection
        'socket_timeout': YT_SOCKET_TIMEOUT,
        'retries': YT_RETRIES,
        'fragment_retries': YT_RETRIES,
    }
    cf = cookiefile if cookiefile is not None else YOUTUBE_COOKIE_FILE
    if cf:
        opts['cookiefile'] = cf
    if _DENO_AVAILABLE:
        # EJS solver for YouTube's JS challenges (needs the deno binary on PATH)
        opts['remote_components'] = {'ejs:github'}
        opts['js_runtimes'] = {'deno': {}}
    return opts

def get_youtube_qualities(url, download_id=None, cookiefile=None):
    """Return (title, [{height, label}]) using only resolutions that really exist."""
    tag = download_id or uuid.uuid4().hex[:8]
    logger.info(f'[{tag}] STARTING YT-DLP - metadata-only extract_info for {url}')
    opts = {**_yt_base_opts(cookiefile=cookiefile), 'logger': _YtDlpLogBridge(tag)}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        logger.error(f'[{tag}] YT-DLP EXIT CODE - metadata fetch failed: {e}')
        raise
    logger.info(f'[{tag}] YT-DLP EXIT CODE - 0 (metadata fetched)')
    heights = sorted({f['height'] for f in info.get('formats', [])
                       if f.get('height') and f.get('vcodec') not in (None, 'none')}, reverse=True)
    qualities = [{'height': h, 'label': _QUALITY_LABELS.get(h, f'{h}p')} for h in heights]
    return info.get('title', 'video'), qualities


def ffprobe_json(path):
    try:
        result = subprocess.run(
            [FFPROBE_BIN, '-v', 'error', '-print_format', 'json', '-show_format', '-show_streams', path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise DownloadError('FFPROBE_VALIDATION_FAILED', f'ffprobe could not run: {e}')
    if result.returncode != 0:
        raise DownloadError('FFPROBE_VALIDATION_FAILED', f'ffprobe exited {result.returncode}: {result.stderr.strip()}')
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise DownloadError('FFPROBE_VALIDATION_FAILED', f'ffprobe returned invalid JSON: {e}')


def validate_media_file(path, require_audio=True, require_video=True):
    """Raises DownloadError with a stage code if the file is not a genuinely playable media file."""
    if not os.path.exists(path):
        raise DownloadError('OUTPUT_FILE_MISSING', f'Output file was not created: {path}')
    if os.path.getsize(path) == 0:
        raise DownloadError('OUTPUT_FILE_EMPTY', f'Output file is empty: {path}')

    probe = ffprobe_json(path)
    streams = probe.get('streams', [])
    video_streams = [s for s in streams if s.get('codec_type') == 'video']
    audio_streams = [s for s in streams if s.get('codec_type') == 'audio']

    if require_video and not video_streams:
        raise DownloadError('VIDEO_STREAM_MISSING', 'No video stream found in output file')
    if require_audio and not audio_streams:
        raise DownloadError('AUDIO_STREAM_MISSING', 'No audio stream found in output file')

    duration = float(probe.get('format', {}).get('duration') or 0)
    if duration <= 0 and streams:
        durations = [float(s.get('duration') or 0) for s in streams]
        duration = max(durations) if durations else 0
    if duration <= 0:
        raise DownloadError('FFPROBE_VALIDATION_FAILED', 'Output file has zero duration')

    # Playability check: -v error means anything on stderr is a real decode/container problem.
    try:
        decode = subprocess.run(
            [FFMPEG_BIN, '-nostdin', '-v', 'error', '-i', path, '-f', 'null', '-'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True, timeout=180
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise DownloadError('FFPROBE_VALIDATION_FAILED', f'ffmpeg decode check could not run: {e}')
    if decode.returncode != 0 or decode.stderr.strip():
        raise DownloadError('FFPROBE_VALIDATION_FAILED', f'Decode check failed: {decode.stderr.strip()[:500]}')

    v = video_streams[0] if video_streams else {}
    a = audio_streams[0] if audio_streams else {}
    return {
        'container': probe.get('format', {}).get('format_name'),
        'video_codec': v.get('codec_name'),
        'width': v.get('width'),
        'height': v.get('height'),
        'pix_fmt': v.get('pix_fmt'),
        'audio_codec': a.get('codec_name'),
        'sample_rate': a.get('sample_rate'),
        'duration': duration,
    }


FFMPEG_PRESET = os.environ.get('FFMPEG_PRESET', 'veryfast')
FFMPEG_STALL_SECONDS = int(os.environ.get('FFMPEG_STALL_SECONDS', '180'))
FFMPEG_MAX_SECONDS = int(os.environ.get('FFMPEG_MAX_SECONDS', str(6 * 3600)))
_HW_ENCODERS = ('h264_nvenc', 'h264_qsv', 'h264_amf')
_NVENC_PRESET_MAP = {'ultrafast': 'p1', 'superfast': 'p2', 'veryfast': 'p3', 'faster': 'p4', 'fast': 'p4', 'medium': 'p5', 'slow': 'p6', 'slower': 'p6', 'veryslow': 'p7'}
_hw_encoder_cache = None


def _detect_hw_encoders():
    """Returns the list of H.264 hardware encoders this FFmpeg binary supports (cached)."""
    global _hw_encoder_cache
    if _hw_encoder_cache is not None:
        return _hw_encoder_cache
    try:
        result = subprocess.run(
            [FFMPEG_BIN, '-hide_banner', '-encoders'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True, timeout=15
        )
        output = result.stdout
    except (OSError, subprocess.TimeoutExpired):
        output = ''
    _hw_encoder_cache = [enc for enc in _HW_ENCODERS if enc in output]
    return _hw_encoder_cache


def _video_encode_args(encoder, preset):
    if encoder == 'h264_nvenc':
        return ['-c:v', 'h264_nvenc', '-preset', _NVENC_PRESET_MAP.get(preset, 'p4'), '-rc', 'vbr', '-cq', '20', '-b:v', '0']
    if encoder == 'h264_qsv':
        return ['-c:v', 'h264_qsv', '-preset', preset if preset in ('veryfast', 'faster', 'fast', 'medium', 'slow', 'slower', 'veryslow') else 'veryfast', '-global_quality', '20']
    if encoder == 'h264_amf':
        return ['-c:v', 'h264_amf', '-quality', 'speed', '-rc', 'cqp', '-qp_i', '20', '-qp_p', '20', '-qp_b', '20']
    return ['-c:v', 'copy', '-preset', preset, '-crf', '20']


def _run_ffmpeg_with_progress(cmd, download_id, duration, stall_seconds=FFMPEG_STALL_SECONDS, max_seconds=FFMPEG_MAX_SECONDS):
    """Runs ffmpeg via Popen, streaming `-progress pipe:1` lines into logs/PROGRESS as they
    arrive instead of buffering everything until exit. Kills the process if `out_time` stops
    advancing for `stall_seconds` (a genuinely slow encode is fine; a stuck one is not)."""
    logger.info(f'[{download_id}] STARTING FFMPEG - {" ".join(cmd)}')
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True, bufsize=1)

    stderr_tail = []

    def _read_stderr():
        for line in proc.stderr:
            stderr_tail.append(line.rstrip())
            del stderr_tail[:-200]

    threading.Thread(target=_read_stderr, daemon=True).start()

    watchdog_state = {'last_progress_at': time.time(), 'last_out_seconds': 0.0, 'started_at': time.time()}

    def _watchdog():
        while proc.poll() is None:
            time.sleep(5)
            now = time.time()
            if now - watchdog_state['last_progress_at'] > stall_seconds:
                logger.error(f'[{download_id}] FFMPEG STALL DETECTED - no progress for {stall_seconds}s, killing')
                proc.kill()
                return
            if now - watchdog_state['started_at'] > max_seconds:
                logger.error(f'[{download_id}] FFMPEG MAX DURATION EXCEEDED - {max_seconds}s, killing')
                proc.kill()
                return

    threading.Thread(target=_watchdog, daemon=True).start()

    block = {}
    for raw_line in proc.stdout:
        line = raw_line.strip()
        if not line or '=' not in line:
            continue
        key, _, val = line.partition('=')
        block[key] = val
        if key != 'progress':
            continue

        out_time = block.get('out_time', '')
        out_seconds = _parse_hms_to_seconds(out_time)
        if out_seconds > watchdog_state['last_out_seconds']:
            watchdog_state['last_out_seconds'] = out_seconds
            watchdog_state['last_progress_at'] = time.time()

        percent = round(min(100.0, out_seconds / duration * 100), 1) if duration > 0 else None
        speed = block.get('speed')
        frame = block.get('frame')
        fps = block.get('fps')

        logger.info(f'[{download_id}] FFMPEG PROGRESS - frame={frame} fps={fps} time={out_time} speed={speed}'
                    + (f' percent={percent}%' if percent is not None else ''))
        _set_progress(download_id, 'transcoding', percent=percent, speed=speed, message='Converting to MP4...')

        block = {}
        if val == 'end':
            break

    proc.wait(timeout=30)
    return proc.returncode, '\n'.join(stderr_tail)


def _parse_hms_to_seconds(hms):
    try:
        h, m, s = hms.split(':')
        return int(h) * 3600 + int(m) * 60 + float(s)
    except (ValueError, AttributeError):
        return 0.0


def _transcode_for_compatibility(src_path, dst_path, download_id, preset=None):
    """Force H.264/AAC/yuv420p/faststart MP4, remuxing instead of re-encoding when already
    compatible. Tries an available hardware encoder first (much faster on 4K), falling back
    to libx264 on failure so the app never crashes just because a GPU encoder is missing."""
    preset = preset or FFMPEG_PRESET
    probe = ffprobe_json(src_path)
    streams = probe.get('streams', [])
    v = next((s for s in streams if s.get('codec_type') == 'video'), {})
    a = next((s for s in streams if s.get('codec_type') == 'audio'), {})
    duration = float(probe.get('format', {}).get('duration') or 0)
    already_compatible = (
        v.get('codec_name') == 'h264' and a.get('codec_name') == 'aac' and v.get('pix_fmt') == 'yuv420p'
    )

    base_io_args = ['-nostdin', '-y', '-i', src_path, '-map', '0:v:0', '-map', '0:a:0']
    progress_args = ['-progress', 'pipe:1', '-nostats']

    if already_compatible:
        logger.info(f'[{download_id}] ENCODER SELECTED - remux (already H.264/AAC/yuv420p)')
        cmd = [FFMPEG_BIN, '-nostdin', '-y', '-i', src_path, '-c', 'copy', '-movflags', '+faststart'] + progress_args + [dst_path]
        returncode, stderr_tail = _run_ffmpeg_with_progress(cmd, download_id, duration)
        if returncode != 0:
            raise DownloadError('FFMPEG_TRANSCODE_FAILED', f'ffmpeg exited {returncode}: {stderr_tail[-800:]}')
        return

    use_hw = os.environ.get('FFMPEG_USE_HW', '1') != '0'
    candidates = ([e for e in _detect_hw_encoders() if use_hw]) + ['libx264']

    last_error = None
    for encoder in candidates:
        logger.info(f'[{download_id}] ENCODER SELECTED - {encoder}')
        cmd = (
            [FFMPEG_BIN] + base_io_args
            + _video_encode_args(encoder, preset)
            + ['-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '192k', '-ar', '48000', '-movflags', '+faststart']
            + progress_args + [dst_path]
        )
        try:
            returncode, stderr_tail = _run_ffmpeg_with_progress(cmd, download_id, duration)
        except (OSError, subprocess.TimeoutExpired) as e:
            logger.error(f'[{download_id}] FFMPEG EXIT CODE - {encoder} could not run: {e}')
            returncode, stderr_tail = -1, str(e)

        logger.info(f'[{download_id}] FFMPEG EXIT CODE - {returncode} (encoder={encoder})')
        if returncode == 0:
            return

        last_error = f'{encoder} exited {returncode}: {stderr_tail[-800:]}'
        if encoder != 'libx264':
            logger.warning(f'[{download_id}] HARDWARE ENCODER FAILED - {encoder}, falling back to next candidate')

    raise DownloadError('FFMPEG_TRANSCODE_FAILED', last_error or 'ffmpeg failed with no candidates available')


def _make_stage_hooks(download_id, audio_only=False):
    """progress/postprocessor hooks that log video-download / audio-download / ffmpeg-merge
    transitions as they actually happen inside a single yt-dlp extract_info() call."""
    seen_download_stages = set()

    def progress_hook(d):
        info = d.get('info_dict') or {}
        # audio-only mode: every fragment IS the audio track, no per-format guessing needed.
        # video mode: bestvideo+bestaudio downloads two formats back to back — tell them
        # apart from the format actually being fetched right now.
        is_audio_only = audio_only or (
            info.get('vcodec') in (None, 'none') and info.get('acodec') not in (None, 'none')
        )
        stage = 'audio download' if is_audio_only else 'video download'
        progress_stage = 'downloading_audio' if is_audio_only else 'downloading_video'
        progress_message = 'Downloading audio...' if is_audio_only else 'Downloading video...'
        status = d.get('status')

        if status == 'downloading':
            downloaded = d.get('downloaded_bytes') or 0
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            percent = round(downloaded / total * 100, 1) if total else None
            speed_bps = d.get('speed')
            speed = f'{speed_bps / 1024 / 1024:.2f}MB/s' if speed_bps else None
            _set_progress(download_id, progress_stage, percent=percent, speed=speed, message=progress_message)
        elif status == 'finished':
            _set_progress(download_id, progress_stage, percent=100, message=progress_message)

        key = (stage, status)
        if status in ('downloading', 'finished') and key not in seen_download_stages:
            seen_download_stages.add(key)
            _stage_log(download_id, stage, status)
        elif status == 'error':
            _stage_log(download_id, stage, 'error')

    def postprocessor_hook(d):
        name = d.get('postprocessor')
        status = d.get('status')
        if name == 'Merger':
            _set_progress(download_id, 'merging', message='Merging...')
            stage = 'ffmpeg merge'
        else:
            stage = f'postprocess:{name}'
        _stage_log(download_id, stage, status)

    return progress_hook, postprocessor_hook


class _YtDlpLogBridge:
    """yt-dlp's expected logger interface (debug/warning/error methods). yt-dlp routes
    its own internal messages here instead of stdout/stderr — this is the equivalent of
    capturing a subprocess's stdout/stderr when yt-dlp is used as a library, not a CLI."""
    def __init__(self, download_id):
        self.download_id = download_id

    def debug(self, msg):
        logger.info(f'[{self.download_id}] YT-DLP STDOUT - {msg}')

    def info(self, msg):
        logger.info(f'[{self.download_id}] YT-DLP STDOUT - {msg}')

    def warning(self, msg):
        logger.warning(f'[{self.download_id}] YT-DLP STDERR - {msg}')

    def error(self, msg):
        logger.error(f'[{self.download_id}] YT-DLP STDERR - {msg}')


# --- YouTube Downloader ---
def download_youtube(url, mode="video", quality=None, compatibility=False, download_id=None, cookiefile=None):
    """
    quality: requested max height (e.g. 1080, 2160) or None for best available.
    compatibility: True forces MP4 + H.264 + AAC output; False preserves source codecs.
    download_id: caller-supplied id (so the frontend can poll GET /progress/<id> for this
    run before this function returns) — generated here if the caller didn't supply one.
    cookiefile: per-request cookie file override (a device's own session), or None to use
    the shared global/admin cookiefile.
    Returns (success, dict) — see call sites for the two possible dict shapes.
    """
    download_id = download_id or uuid.uuid4().hex
    logger.info(
        f'[{download_id}] DOWNLOAD SETTINGS - '
        f'mode={mode}, quality={quality}, compatibility={compatibility}'
    )
    work_dir = os.path.join(TEMP_DIR, download_id)
    os.makedirs(work_dir, exist_ok=True)
    progress_hook, postprocessor_hook = _make_stage_hooks(download_id, audio_only=(mode == "audio"))

    yt_logger = _YtDlpLogBridge(download_id)

    try:
        if mode == "audio":
            logger.info(f'[{download_id}] FETCHING FORMATS - {url}')
            ydl_opts = {
                **_yt_base_opts(cookiefile=cookiefile),
                'format': 'bestaudio/best',
                'outtmpl': os.path.join(work_dir, 'audio.%(ext)s'),
                'progress_hooks': [progress_hook],
                'postprocessor_hooks': [postprocessor_hook],
                'logger': yt_logger,
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
            }
            logger.info(f'[{download_id}] FORMAT SELECTED - bestaudio/best')
            logger.info(f'[{download_id}] STARTING YT-DLP - mode=audio')
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
            except yt_dlp.utils.DownloadError as e:
                logger.error(f'[{download_id}] YT-DLP EXIT CODE - failed: {e}')
                code, friendly = classify_youtube_error(e)
                raise DownloadError(code, friendly)
            logger.info(f'[{download_id}] YT-DLP EXIT CODE - 0 (success)')

            final_path = os.path.join(work_dir, 'audio.mp3')
            logger.info(f'[{download_id}] VALIDATING FILE - {final_path}')
            validation = validate_media_file(final_path, require_audio=True, require_video=False) if os.path.exists(final_path) else None
            if not validation:
                raise DownloadError('OUTPUT_FILE_MISSING', f'Expected mp3 not found: {final_path}')

            title = info.get('title', 'audio')
            download_name = make_safe_filename(f'{title}.mp3')

            return True, {
                'path': final_path,
                'temp_dir': work_dir,
                'download_name': download_name,
                'validation': validation,
            }

        # --- video mode ---
        logger.info(f'[{download_id}] FETCHING FORMATS - {url}')
        title, qualities = get_youtube_qualities(url, download_id=download_id, cookiefile=cookiefile)
        if not qualities:
            raise DownloadError('VIDEO_STREAM_MISSING', 'No video formats available for this URL')

        available_heights = [q['height'] for q in qualities]
        fallback = False
        if quality:
            usable = [h for h in available_heights if h <= quality]
            if not usable:
                raise DownloadError(
                    'REQUESTED_QUALITY_UNAVAILABLE',
                    f'Requested {quality}p is unavailable; highest available is {max(available_heights)}p'
                )
            chosen_height = max(usable)
            fallback = chosen_height != quality
        else:
            chosen_height = max(available_heights)

        format_selector = f'bestvideo[height<={chosen_height}]+bestaudio/best[height<={chosen_height}]'
        logger.info(f'[{download_id}] FORMAT SELECTED - {format_selector} (compatibility={compatibility})')

        ydl_opts = {
            **_yt_base_opts(cookiefile=cookiefile),
            'format': format_selector,
            'outtmpl': os.path.join(work_dir, 'source.%(ext)s'),
            'progress_hooks': [progress_hook],
            'postprocessor_hooks': [postprocessor_hook],
            'logger': yt_logger,
        }
        if compatibility:
            # Let yt-dlp's own ffmpeg merger produce MP4 directly when the codecs allow it.
            ydl_opts['merge_output_format'] = 'mp4'

        logger.info(f'[{download_id}] STARTING YT-DLP - mode=video format={format_selector}')
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except yt_dlp.utils.PostProcessingError as e:
            logger.error(f'[{download_id}] YT-DLP EXIT CODE - ffmpeg merge failed: {e}')
            raise DownloadError('FFMPEG_MERGE_FAILED', str(e))
        except yt_dlp.utils.DownloadError as e:
            logger.error(f'[{download_id}] YT-DLP EXIT CODE - download failed: {e}')
            code, friendly = classify_youtube_error(e)
            raise DownloadError(code, friendly)
        logger.info(f'[{download_id}] YT-DLP EXIT CODE - 0 (success)')

        requested_downloads = info.get('requested_downloads') or []
        if not requested_downloads or not requested_downloads[0].get('filepath'):
            raise DownloadError('OUTPUT_FILE_MISSING', 'yt-dlp did not report an output file path')
        source_path = requested_downloads[0]['filepath']

        logger.info(f'[{download_id}] VALIDATING FILE - {source_path}')
        _set_progress(download_id, 'validating', message='Validating...')
        source_validation = validate_media_file(source_path, require_audio=True)

        if compatibility:
            logger.info(f'[{download_id}] ENTERING COMPATIBILITY TRANSCODE')
            _set_progress(download_id, 'transcoding', percent=0, message='Converting to MP4...')
            final_path = os.path.join(work_dir, 'final.mp4')
            _transcode_for_compatibility(source_path, final_path, download_id)
            logger.info(f'[{download_id}] VALIDATING FILE - {final_path}')
            _set_progress(download_id, 'validating', message='Validating final file...')
            validation = validate_media_file(final_path, require_audio=True)
        else:
            final_path = source_path
            validation = source_validation

        ext = os.path.splitext(final_path)[1]
        download_name = make_safe_filename(f'{title}{ext}')

        logger.info(f'[{download_id}] DOWNLOAD COMPLETE - {final_path}')
        _set_progress(download_id, 'preparing', percent=100, message='Preparing download...')
        return True, {
            'path': final_path, 'temp_dir': work_dir, 'download_name': download_name,
            'quality_requested': quality, 'quality_used': chosen_height, 'fallback': fallback,
            'validation': validation,
        }

    except DownloadError as e:
        logger.error(f'[{download_id}] FAILED - {e.code}: {e.message}\n{traceback.format_exc()}')
        _set_progress(download_id, 'failed', message=e.message)
        shutil.rmtree(work_dir, ignore_errors=True)
        return False, {'code': e.code, 'message': e.message}
    except Exception as e:
        logger.error(f'[{download_id}] FAILED - UNEXPECTED: {e}\n{traceback.format_exc()}')
        code, friendly = classify_youtube_error(e)
        _set_progress(download_id, 'failed', message=friendly)
        shutil.rmtree(work_dir, ignore_errors=True)
        return False, {'code': code, 'message': friendly}




def make_safe_filename(filename):
    # Replace common Unicode punctuation first
    filename = filename.replace("…", "...")
    filename = filename.replace("–", "-")
    filename = filename.replace("—", "-")
    filename = filename.replace("’", "'")
    filename = filename.replace("“", '"')
    filename = filename.replace("”", '"')

    # Force ASCII for HTTP header compatibility
    filename = unicodedata.normalize("NFKD", filename)
    filename = filename.encode("ascii", "ignore").decode("ascii")

    # Remove invalid Windows filename characters
    filename = re.sub(r'[<>:"/\\|?*]', '_', filename)

    filename = filename.strip().rstrip(".")

    if not filename:
        filename = "video.mp4"

    return filename
# --- Facebook Downloader ---
def download_facebook(url, content_type="video"):
    if content_type == "image":
        ua = UserAgent()
        chrome_options = Options()
        chrome_options.add_argument("--headless=new")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument(f"user-agent={ua.random}")

        try:
            driver = webdriver.Chrome(options=chrome_options)
            driver.get(url)
            time.sleep(3)

            # Try to find image element
            img_element = None
            selectors = [
                'img[src*="scontent"]',
                'div[data-visualcompletion="media-vc-image"] img',
                'img.x1ey2m1c.xds687c.x5yr21d.x10l6tqk.x17qophe.x13vifvy.xh8yej3'
            ]

            for selector in selectors:
                try:
                    img_element = driver.find_element(By.CSS_SELECTOR, selector)
                    if img_element:
                        break
                except:
                    continue

            if not img_element:
                return False, "Could not find image element"

            img_url = img_element.get_attribute('src')
            if not img_url:
                return False, "Image URL not found"

            response = requests.get(img_url, headers={'User-Agent': ua.random})
            if response.status_code == 200:
                filename = f"fb_image_{int(time.time())}.jpg"
                filepath = os.path.join(TEMP_DIR, filename)
                with open(filepath, 'wb') as f:
                    f.write(response.content)
                return True, filepath
            return False, f"Failed to download image: HTTP {response.status_code}"
        except Exception as e:
            return False, str(e)
        finally:
            driver.quit()
    else:  # video
        try:
            filename = f"fb_video_{int(time.time())}.mp4"
            filepath = os.path.join(TEMP_DIR, filename)
            ydl_opts = {
                'outtmpl': filepath,
                'quiet': True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            return True, filepath
        except Exception as e:
            return False, str(e)

# --- Instagram Downloader ---
def download_instagram(url, content_type="image"):
    try:
        L = instaloader.Instaloader(
            download_pictures=(content_type == "image"),
            download_videos=(content_type == "video"),
            download_video_thumbnails=False,
            download_geotags=False,
            download_comments=False,
            save_metadata=False,
            filename_pattern=os.path.join(TEMP_DIR, "{date_utc:%Y-%m-%d}_{profile}_{mediaid}"),
        )

        shortcode = url.split("/")[-2]
        post = instaloader.Post.from_shortcode(L.context, shortcode)
        
        if (content_type == "video" and not post.is_video) or (content_type == "image" and post.is_video):
            return False, "Content type mismatch"

        L.download_post(post, target="temp_download")
        
        # Find the downloaded file
        for file in os.listdir(TEMP_DIR):
            if file.startswith(f"{post.date_utc:%Y-%m-%d}_{post.owner_username}_{post.mediaid}"):
                if (content_type == "video" and file.endswith(('.mp4', '.mkv'))) or \
                   (content_type == "image" and file.endswith(('.jpg', '.jpeg', '.png'))):
                    return True, os.path.join(TEMP_DIR, file)
        
        return False, "File not found after download"
    except Exception as e:
        return False, str(e)

# --- Twitter Downloader ---

def download_twitter(url, content_type="video"):
    try:
        # Method 1: Try gallery-dl first (most reliable)
        result = _download_with_gallery_dl(url, content_type)
        if result[0]:
            return result

        # Method 2: Fallback to API method if gallery-dl fails
        result = _download_with_twitter_api(url, content_type)
        if result[0]:
            return result

        # Method 3: Final fallback to browser automation
        return _download_with_selenium(url, content_type)

    except Exception as e:
        return False, f"Error: {str(e)}"

def _download_with_gallery_dl(url, content_type):
    """Method 1: Using gallery-dl"""
    try:
        temp_download_dir = os.path.join(TEMP_DIR, f"twitter_{int(time.time())}")
        os.makedirs(temp_download_dir, exist_ok=True)

        cmd = [
            "gallery-dl",
            "--directory", temp_download_dir,
            "--write-metadata",
            url
        ]

        if content_type == "video":
            cmd.extend(["--filter", "type=video"])
        else:
            cmd.extend(["--filter", "type=image"])

        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=300
        )

        if result.returncode != 0:
            return False, f"gallery-dl failed: {result.stderr}"

        # Find downloaded file
        for root, _, files in os.walk(temp_download_dir):
            for file in files:
                if (content_type == "image" and file.lower().endswith(('.jpg', '.jpeg', '.png'))) or \
                   (content_type == "video" and file.lower().endswith(('.mp4', '.mkv'))):
                    return True, os.path.join(root, file)

        return False, "No media found after download"

    except Exception as e:
        return False, str(e)

def _download_with_twitter_api(url, content_type):
    """Method 2: Using Twitter API"""
    try:
        # Extract tweet ID
        tweet_id = re.search(r'(?:twitter\.com|x\.com)\/\w+\/status\/(\d+)', url)
        if not tweet_id:
            return False, "Invalid Twitter URL"
        tweet_id = tweet_id.group(1)

        # Use unofficial API
        api_url = f"https://cdn.syndication.twimg.com/tweet-result?id={tweet_id}"
        response = requests.get(api_url, headers={
            'User-Agent': 'Mozilla/5.0',
            'Referer': 'https://twitter.com/'
        }, timeout=10)

        if response.status_code != 200:
            return False, f"API request failed (HTTP {response.status_code})"

        data = response.json()
        media_url = None

        if content_type == "image" and ('photos' in data or 'media' in data):
            media = data.get('photos', data.get('media', []))
            if media:
                media_url = media[-1]['url'] + "?format=jpg&name=orig"
        elif content_type == "video" and ('video' in data or 'media' in data):
            variants = data.get('video', data.get('media', {})).get('variants', [])
            if variants:
                media_url = max(
                    [v for v in variants if v.get('content_type', '').startswith('video/')],
                    key=lambda x: x.get('bitrate', 0)
                )['url']

        if not media_url:
            return False, "No media URL found"

        # Download the media
        response = requests.get(media_url, stream=True, timeout=30)
        if response.status_code != 200:
            return False, f"Media download failed (HTTP {response.status_code})"

        ext = 'mp4' if content_type == "video" else 'jpg'
        filename = f"twitter_{content_type}_{tweet_id}.{ext}"
        filepath = os.path.join(TEMP_DIR, filename)

        with open(filepath, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)

        return True, filepath

    except Exception as e:
        return False, str(e)

def _download_with_selenium(url, content_type):
    """Method 3: Using browser automation (fallback)"""
    try:
        options = Options()
        options.add_argument("--headless")
        options.add_argument("--disable-gpu")
        driver = webdriver.Chrome(options=options)
        
        driver.get(url)
        time.sleep(5)  # Wait for page to load

        if content_type == "image":
            # Try to find image element
            img = driver.find_element(By.CSS_SELECTOR, 'img[src*="media"]')
            media_url = img.get_attribute('src')
        else:
            # Try to find video element
            video = driver.find_element(By.CSS_SELECTOR, 'video')
            media_url = video.get_attribute('src')

        if not media_url:
            return False, "Could not find media element"

        # Download the media
        response = requests.get(media_url, stream=True)
        if response.status_code != 200:
            return False, f"Media download failed (HTTP {response.status_code})"

        ext = 'mp4' if content_type == "video" else 'jpg'
        filename = f"twitter_selenium_{content_type}_{int(time.time())}.{ext}"
        filepath = os.path.join(TEMP_DIR, filename)

        with open(filepath, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)

        return True, filepath

    except Exception as e:
        return False, str(e)
    finally:
        try:
            driver.quit()
        except:
            pass

@app.route('/file/<download_id>', methods=['GET'])
def serve_prepared_file(download_id):
    """
    Serves an already-completed file registered by /download. Never calls
    yt-dlp/FFmpeg again — this route only reads a file that already exists
    on disk. `download_id` is looked up in DOWNLOAD_FILES; nothing here ever
    accepts or builds a filesystem path from request input, so there is no
    directory-traversal surface.

    Delegates Range/conditional handling entirely to Flask's send_file()
    (conditional=True), which already implements correct 200/206/416 +
    Content-Range/Accept-Ranges/Content-Length behavior — exactly what IDM,
    Chrome, and curl expect from a normal downloadable resource.
    """
    if not _DOWNLOAD_ID_RE.match(download_id or ''):
        return jsonify({'success': False, 'message': 'Invalid download id'}), 400

    with DOWNLOAD_FILES_LOCK:
        entry = DOWNLOAD_FILES.get(download_id)

    if not entry:
        return jsonify({'success': False, 'message': 'Download not found or expired'}), 404
    if not os.path.exists(entry['path']):
        return jsonify({'success': False, 'message': 'File is no longer available on disk'}), 410

    safe_name = make_safe_filename(entry['download_name'])
    mime = mimetypes.guess_type(safe_name)[0] or 'application/octet-stream'

    logger.info(f'[{download_id}] SERVING FILE - {entry["path"]} as {safe_name!r} '
                f'range={request.headers.get("Range")}')

    return send_file(
        entry['path'],
        mimetype=mime,
        as_attachment=True,
        download_name=safe_name,
        conditional=True,   # enables Range / If-Range / 206 / 416 support
        max_age=0,
    )


@app.route('/progress/<download_id>', methods=['GET'])
def get_progress(download_id):
    """Polled by the frontend while POST /download is in flight so a slow 4K transcode
    shows live stage/percent instead of a generic 'Downloading...' spinner."""
    if not _DOWNLOAD_ID_RE.match(download_id or ''):
        return jsonify({'success': False, 'message': 'Invalid id'}), 400
    with PROGRESS_LOCK:
        entry = PROGRESS.get(download_id)
    if not entry:
        return jsonify({'success': False, 'message': 'Unknown or expired id'}), 404
    return jsonify({
        'success': True,
        'stage': entry['stage'],
        'percent': entry['percent'],
        'speed': entry['speed'],
        'message': entry['message'],
    })


@app.errorhandler(Exception)
def handle_unexpected_error(e):
    if isinstance(e, HTTPException):
        # e.g. the 413 from MAX_CONTENT_LENGTH on an oversized /youtube/session upload —
        # keep every error response JSON, never Werkzeug's default HTML error page.
        return jsonify({'success': False, 'message': e.description or e.name,
                         'error_code': f'HTTP_{e.code}'}), e.code
    logger.error(f'UNHANDLED EXCEPTION - {e}\n{traceback.format_exc()}')
    return jsonify({'success': False, 'message': 'An unexpected server error occurred.', 'error_code': 'INTERNAL_ERROR'}), 500


@app.route('/')
def index():
    return 'Backend is running', 200


@app.route('/device/register', methods=['POST'])
def device_register():
    """First-run bootstrap: the extension calls this once per installation and
    persists the returned device_id/device_token in chrome.storage.local. No
    auth required here — this endpoint's whole job is to hand out a new
    identity. Rate-limited like everything else so it can't be used to spam
    device records."""
    if _is_rate_limited(request.remote_addr):
        return jsonify({'success': False, 'message': 'Too many requests, please slow down.', 'error_code': 'RATE_LIMITED'}), 429
    device_id, token = session_store.register_device()
    logger.info(f'DEVICE REGISTERED - device_id={device_id}')
    return jsonify({'success': True, 'device_id': device_id, 'device_token': token})


@app.route('/youtube/session', methods=['POST'])
@require_device_auth
def youtube_session_connect():
    """Stores this device's own YouTube cookies (Netscape format), encrypted at
    rest. Only ever called from the extension's explicit "Connect YouTube
    Session" flow — never triggered automatically."""
    if _is_rate_limited(request.remote_addr):
        return jsonify({'success': False, 'message': 'Too many requests, please slow down.', 'error_code': 'RATE_LIMITED'}), 429

    data = request.get_json(silent=True) or {}
    cookie_text = data.get('cookies')
    if not isinstance(cookie_text, str) or not cookie_text.strip():
        return jsonify({'success': False, 'message': 'A "cookies" field with your Netscape-format cookies.txt contents is required.',
                         'error_code': 'INVALID_SESSION_DATA'}), 400
    if len(cookie_text.encode('utf-8')) > session_store.MAX_COOKIE_BYTES:
        return jsonify({'success': False, 'message': 'Cookie data is too large.', 'error_code': 'SESSION_TOO_LARGE'}), 413
    if not session_store.looks_like_netscape_cookiefile(cookie_text):
        return jsonify({'success': False, 'message': 'That does not look like a valid Netscape-format cookies.txt file.',
                         'error_code': 'INVALID_SESSION_FORMAT'}), 400

    try:
        session_store.save_session(g.device_id, cookie_text)
    except RuntimeError:
        logger.error(f'YOUTUBE SESSION SAVE FAILED - device_id={g.device_id} - server misconfigured (SESSION_ENCRYPTION_KEY)')
        return jsonify({'success': False, 'message': 'The server is not configured to store sessions right now.',
                         'error_code': 'SERVER_MISCONFIGURED'}), 500

    logger.info(f'YOUTUBE SESSION CONNECTED - device_id={g.device_id}')
    return jsonify({'success': True, 'message': 'YouTube session connected.'})


@app.route('/youtube/session/status', methods=['GET'])
@require_device_auth
def youtube_session_status():
    return jsonify({'success': True, **session_store.session_status(g.device_id)})


@app.route('/youtube/session', methods=['DELETE'])
@require_device_auth
def youtube_session_disconnect():
    session_store.delete_session(g.device_id)
    logger.info(f'YOUTUBE SESSION DISCONNECTED - device_id={g.device_id}')
    return jsonify({'success': True, 'message': 'YouTube session disconnected.'})


@app.route('/youtube/qualities', methods=['POST'])
def youtube_qualities():
    if _is_rate_limited(request.remote_addr):
        return jsonify({'success': False, 'message': 'Too many requests, please slow down.', 'error_code': 'RATE_LIMITED'}), 429

    try:
        device_id = _resolve_authenticated_device()
    except DeviceAuthError:
        return jsonify({'success': False, 'message': 'Invalid or missing device authentication.',
                         'error_code': 'DEVICE_AUTH_FAILED'}), 401

    data = request.get_json() or {}
    url = data.get('url')
    if not url:
        return jsonify({'success': False, 'message': 'URL is required'})

    cookiefile, is_device_specific, cleanup = _prepare_request_cookiefile(device_id)
    try:
        title, qualities = get_youtube_qualities(url, cookiefile=cookiefile)
        return jsonify({'success': True, 'title': title, 'qualities': qualities})
    except Exception as e:
        code, friendly = classify_youtube_error(e)
        code, friendly = _remap_session_error(code, friendly, is_device_specific)
        logger.error(f'YOUTUBE QUALITIES FAILED - {code}: {e}')
        return jsonify({'success': False, 'message': friendly, 'error_code': code}), 422
    finally:
        cleanup()

@app.route('/download', methods=['POST'])
def download():
    logger.info(f'DOWNLOAD REQUEST RECEIVED - method={request.method} path={request.path} '
                f'content_type={request.content_type} remote_addr={request.remote_addr}')

    if _is_rate_limited(request.remote_addr):
        return jsonify({'success': False, 'message': 'Too many requests, please slow down.', 'error_code': 'RATE_LIMITED'}), 429

    data = request.get_json(silent=True) or {}
    url = data.get('url')
    platform = data.get('platform')
    content_type = data.get('content_type', 'video')
    logger.info(f'URL RECEIVED - platform={platform} content_type={content_type} url={url}')

    if not url:
        logger.info('DOWNLOAD REQUEST REJECTED - no url in request body')
        return jsonify({'success': False, 'message': 'URL is required'})

    try:
        if platform == 'youtube':
            try:
                device_id = _resolve_authenticated_device()
            except DeviceAuthError:
                return jsonify({'success': False, 'message': 'Invalid or missing device authentication.',
                                 'error_code': 'DEVICE_AUTH_FAILED'}), 401

            mode = 'audio' if content_type == 'audio' else 'video'
            quality = data.get('quality')
            quality = int(quality) if quality else None
            quality_mode = str(data.get('quality_mode', '')).strip().lower()

            logger.info(f"RAW QUALITY MODE = {quality_mode!r}")

            compatibility = quality_mode in (
                'compatibility',
                'max compatibility',
                'maximum compatibility',
                'max_compatibility',
                'maximum_compatibility',
                'compatible'
            )

            logger.info(f"COMPATIBILITY FLAG = {compatibility}")

            logger.info(
                f"QUALITY MODE RAW={quality_mode!r} "
                f"COMPATIBILITY={compatibility}"
)
            logger.info(
                f'YOUTUBE DOWNLOAD OPTIONS - quality={quality} '
                f'quality_mode={data.get("quality_mode")} '
                f'compatibility={compatibility}'
            )

            requested_id = data.get('download_id')
            progress_id = requested_id if requested_id and _DOWNLOAD_ID_RE.match(requested_id) else uuid.uuid4().hex
            _set_progress(progress_id, 'starting', percent=0, message='Starting download...')

            cookiefile, is_device_specific, cleanup = _prepare_request_cookiefile(device_id)
            try:
                success, result = download_youtube(
                    url,
                    mode,
                    quality=quality,
                    compatibility=compatibility,
                    download_id=progress_id,
                    cookiefile=cookiefile,
                )
            finally:
                cleanup()

            if not success:
                code, message = _remap_session_error(result['code'], result['message'], is_device_specific)
                return jsonify({'success': False, 'message': message, 'error_code': code}), 422

            safe_download_name = make_safe_filename(result['download_name'])
            download_id = register_download_file(result['path'], safe_download_name, result['temp_dir'])
            download_url = f"{request.host_url.rstrip('/')}/file/{download_id}"
            _set_progress(progress_id, 'ready', percent=100, message='Ready')

            return jsonify({
                'success': True,
                'download_id': download_id,
                'download_url': download_url,
                'filename': safe_download_name,
                'quality_used': result.get('quality_used'),
                'fallback': result.get('fallback', False),
            })

        elif platform == 'facebook':
            success, result = download_facebook(url, content_type)
        elif platform == 'instagram':
            success, result = download_instagram(url, content_type)
        elif platform == 'twitter':
            success, result = download_twitter(url, content_type)
        else:
            return jsonify({'success': False, 'message': 'Unsupported platform'})

        if success:
            if isinstance(result, str) and os.path.exists(result):
                original_filename = os.path.basename(result)
                safe_filename = make_safe_filename(original_filename)

                logger.info(f"Sending file: {safe_filename!r}")

                return send_file(
                    result,
                    as_attachment=True,
                    download_name=safe_filename
                )

            return jsonify({
                'success': True,
                'message': result
            })

        return jsonify({'success': False, 'message': result})
    except Exception as e:
        logger.error(f'DOWNLOAD REQUEST FAILED - unhandled exception: {e}\n{traceback.format_exc()}')
        return jsonify({'success': False, 'message': str(e)})

def _port_already_serving(host, port):
    """
    On Windows, Werkzeug's dev server sets SO_REUSEADDR, which (unlike on
    Linux/Mac) lets a NEW process bind the same port while an OLD one is
    still listening — both silently coexist and the OS picks one at random
    per incoming connection. A forgotten `python app.py` from an earlier run
    then keeps answering requests while you watch a different terminal that
    never receives them and never logs anything. Refuse to start rather than
    become a second silent listener.
    """
    check_host = '127.0.0.1' if host in ('0.0.0.0', '') else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex((check_host, port)) == 0


if __name__ == "__main__":
    os.makedirs('templates', exist_ok=True)
    _port = 5000
    # debug=True's reloader re-execs this file in a child process that inherits
    # the already-bound listening socket via WERKZEUG_RUN_MAIN/WERKZEUG_SERVER_FD
    # — the port is legitimately "already serving" there by design, so only check
    # on the original invocation (before the reloader has done anything).
    if os.environ.get('WERKZEUG_RUN_MAIN') != 'true' and _port_already_serving('0.0.0.0', _port):
        print(f'ERROR: port {_port} is already being served by another process '
              f'(likely a `python app.py` left running from an earlier session).')
        print(f'Find and stop it first: netstat -ano | findstr :{_port}   then   taskkill /F /PID <pid>')
        sys.exit(1)
    threading.Thread(target=_download_cleanup_loop, daemon=True).start()
    app.run(debug=True, host='0.0.0.0', port=5000, threaded=True)