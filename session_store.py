"""Per-device YouTube session storage for the Chrome extension.

Each extension installation registers itself once (device_id + a secret
bearer token), then can upload its own YouTube cookies, which are encrypted
at rest and looked up by device_id on every /youtube/qualities and /download
call. This replaces one shared global YOUTUBE_COOKIES session with one
independent session per office PC.

Storage: Redis (via the REDIS_URL Railway provides once its Redis plugin is
attached), so sessions survive redeploys — the container filesystem doesn't.
Falls back to an in-memory dict if REDIS_URL isn't set. That fallback is only
for local development: it isn't durable and isn't shared across multiple
gunicorn worker processes.

Encryption: Fernet (symmetric, authenticated) keyed by SESSION_ENCRYPTION_KEY,
a Railway secret. Cookie plaintext only ever exists in memory for the
duration of one request and inside a short-lived 0600 temp file handed to
yt-dlp — never logged, never returned in any API response.
"""
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger('downloader')

DEVICE_TTL_SECONDS = 180 * 24 * 3600   # a device registration expires after 180 days of inactivity
SESSION_TTL_SECONDS = 90 * 24 * 3600   # an uploaded session expires after 90 days of inactivity
MAX_COOKIE_BYTES = 512 * 1024

DEVICE_ID_RE = re.compile(r'^[0-9a-f]{32}$')

_redis = None
_redis_checked = False
_memory_store = {}  # ponytail: dev-only fallback (no REDIS_URL) - single-process, not durable


def _get_redis():
    global _redis, _redis_checked
    if _redis_checked:
        return _redis
    _redis_checked = True
    url = os.environ.get('REDIS_URL')
    if not url:
        logger.warning('REDIS_URL not set - per-device sessions use an in-memory store '
                        '(lost on restart, not shared across workers). Fine for local dev only.')
        return None
    import redis
    _redis = redis.from_url(url)
    return _redis


def _store_set(key, obj, ttl):
    payload = json.dumps(obj)
    r = _get_redis()
    if r:
        r.set(key, payload, ex=ttl)
    else:
        _memory_store[key] = (payload, time.time() + ttl)


def _store_get(key):
    r = _get_redis()
    if r:
        raw = r.get(key)
        return json.loads(raw) if raw else None
    entry = _memory_store.get(key)
    if not entry:
        return None
    payload, expires_at = entry
    if time.time() > expires_at:
        _memory_store.pop(key, None)
        return None
    return json.loads(payload)


def _store_delete(key):
    r = _get_redis()
    if r:
        r.delete(key)
    else:
        _memory_store.pop(key, None)


# --- Device registration + bearer-token auth --------------------------------

def register_device():
    """Creates a new device_id + secret token pair. Only the token's hash is
    stored server-side — the raw token is returned once and never persisted."""
    device_id = secrets.token_hex(16)
    token = secrets.token_urlsafe(32)
    _store_set(f'device:{device_id}', {
        'token_hash': hashlib.sha256(token.encode()).hexdigest(),
        'created_at': time.time(),
        'last_seen': time.time(),
    }, DEVICE_TTL_SECONDS)
    return device_id, token


def verify_device(device_id, token):
    """True if device_id+token are a valid, currently-registered pair. Refreshes
    the device's inactivity TTL on success (sliding expiry)."""
    if not device_id or not token or not DEVICE_ID_RE.match(device_id):
        return False
    record = _store_get(f'device:{device_id}')
    if not record:
        return False
    supplied_hash = hashlib.sha256(token.encode()).hexdigest()
    if not hmac.compare_digest(record.get('token_hash', ''), supplied_hash):
        return False
    record['last_seen'] = time.time()
    _store_set(f'device:{device_id}', record, DEVICE_TTL_SECONDS)
    return True


# --- Encrypted per-device YouTube session storage ---------------------------

def _fernet():
    key = os.environ.get('SESSION_ENCRYPTION_KEY')
    if not key:
        return None
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError) as e:
        logger.error(f'SESSION_ENCRYPTION_KEY is set but invalid: {e}')
        return None


_COOKIE_LINE_RE = re.compile(
    r'^[^\t\r\n]+\t(TRUE|FALSE)\t[^\t\r\n]*\t(TRUE|FALSE)\t\d+\t[^\t\r\n]+\t[^\t\r\n]*$',
    re.IGNORECASE,
)


def looks_like_netscape_cookiefile(text):
    """Loose structural validation: every non-blank, non-comment line must be a
    well-formed 7-tab-field Netscape cookie line, and at least one must exist.
    Rejects arbitrary pasted text without requiring an exact byte-for-byte
    format (real exports vary slightly in header wording)."""
    if not text or not text.strip():
        return False
    found_cookie_line = False
    for raw_line in text.splitlines():
        line = raw_line.rstrip('\r')
        if not line.strip():
            continue
        if _COOKIE_LINE_RE.match(line):
            found_cookie_line = True
            continue
        if line.startswith('#'):
            continue  # header/comment line ("#HttpOnly_..." cookie lines are matched above, not here)
        return False
    return found_cookie_line


def save_session(device_id, cookie_text):
    """Encrypts and stores cookie_text for device_id. Raises RuntimeError if
    SESSION_ENCRYPTION_KEY isn't configured (fail closed, never store plaintext)."""
    fernet = _fernet()
    if fernet is None:
        raise RuntimeError('SESSION_ENCRYPTION_KEY is not configured on the server')
    ciphertext = fernet.encrypt(cookie_text.encode('utf-8')).decode('ascii')
    _store_set(f'session:{device_id}', {
        'ciphertext': ciphertext,
        'created_at': time.time(),
        'last_used': time.time(),
    }, SESSION_TTL_SECONDS)


def load_session_cookies(device_id):
    """Returns the decrypted cookie text for device_id, or None if there is no
    stored session, the key is misconfigured, or the ciphertext fails to
    decrypt (e.g. the encryption key changed)."""
    record = _store_get(f'session:{device_id}')
    if not record:
        return None
    fernet = _fernet()
    if fernet is None:
        return None
    try:
        plaintext = fernet.decrypt(record['ciphertext'].encode('ascii')).decode('utf-8')
    except (InvalidToken, KeyError, ValueError):
        return None
    record['last_used'] = time.time()
    _store_set(f'session:{device_id}', record, SESSION_TTL_SECONDS)
    return plaintext


def session_status(device_id):
    record = _store_get(f'session:{device_id}')
    if not record:
        return {'connected': False}
    return {
        'connected': True,
        'connected_at': record.get('created_at'),
        'last_used': record.get('last_used'),
    }


def delete_session(device_id):
    _store_delete(f'session:{device_id}')
