"""Optional admin password for MeshPulse's WebSocket API.

MeshPulse is built for a trusted LAN and the WebSocket has no login: anyone
who can open the map can also install a plugin (arbitrary Python), restore a
backup, read the channel keys, arm remote admin and transmit as your node.
Setting an admin password closes that: the map, statistics and messages stay
open to everyone, and everything that changes the system or goes out over
the air asks for the password first.

Without a password nothing changes; this is opt-in.

The password itself is never stored, only a PBKDF2-SHA256 hash in
config.json (which is 0600). A browser that logs in gets a token valid for
TOKEN_TTL. Tokens are stateless, an expiry signed with a key derived from
the password hash and a session epoch, so they survive a restart, changing
the password invalidates all of them, and "sign out everywhere" bumps the
epoch.

Forgot the password? Remove `admin_password_hash` from config.json over SSH
and restart the service.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

# Everything that changes the system, reads its secrets, or transmits.
PROTECTED = frozenset({
    # plugins: installing one runs its code
    'install_plugin', 'install_from_store', 'uninstall_plugin',
    'enable_plugin', 'disable_plugin', 'save_plugin_config',
    # the radio and this mapper's own settings
    'connect', 'get_config', 'set_config', 'save_channel',
    'set_fixed_position', 'clear_fixed_position', 'test_coverage',
    # data
    'create_backup', 'restore_backup', 'upload_begin', 'upload_chunk',
    'clear_all_stats', 'clear_node_stats',
    'set_favorite', 'remove_favorite', 'set_ignored_node', 'remove_ignored_node',
    # other nodes, over the air
    'remote_admin_get', 'remote_admin_set', 'remote_admin_reboot',
    'remote_admin_arm', 'remote_admin_disarm',
    'send_message', 'traceroute', 'request_position', 'ping_device',
})

ITERATIONS = 120_000          # ~0.5 s on a Raspberry Pi 2, once per login
TOKEN_TTL = 14 * 86400
MIN_LENGTH = 8
MAX_LENGTH = 256
MAX_FAILS = 5                 # per client address ...
FAIL_WINDOW = 600             # ... within this many seconds
LOCKOUT = 300                 # then this long before it may try again


def _b64(b):
    return base64.urlsafe_b64encode(b).decode('ascii').rstrip('=')


def _unb64(s):
    return base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))


def hash_password(password, iterations=ITERATIONS):
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, iterations)
    return f'pbkdf2_sha256${iterations}${_b64(salt)}${_b64(dk)}'


def verify_password(password, stored):
    try:
        algo, iterations, salt, digest = stored.split('$')
        if algo != 'pbkdf2_sha256':
            return False
        dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), _unb64(salt), int(iterations))
        return hmac.compare_digest(dk, _unb64(digest))
    except (ValueError, TypeError, AttributeError):
        return False


class AdminAuth:
    """Password checks, tokens and the failed-login lockout.

    `load_config` and `write_config` are the mapper's own config functions,
    passed in so this module never decides where config.json lives.
    """

    def __init__(self, load_config, write_config, clock=time.time):
        self._load = load_config
        self._write = write_config
        self._clock = clock
        self._fails = {}
        self._lock = threading.Lock()

    # ── state ───────────────────────────────────────────────────────────

    def _cfg(self):
        try:
            return self._load() or {}
        except Exception:
            return {}

    def required(self):
        return bool(self._cfg().get('admin_password_hash'))

    def fingerprint(self):
        """Changes whenever the password or the session epoch does; a live
        connection that logged in under another fingerprint is logged out."""
        return hashlib.sha256(self._key(self._cfg())).hexdigest()

    def _key(self, cfg):
        material = f"{cfg.get('admin_password_hash', '')}|{cfg.get('admin_session_epoch', 0)}"
        return hashlib.sha256(material.encode('utf-8')).digest()

    # ── tokens ──────────────────────────────────────────────────────────

    def issue_token(self, cfg=None):
        cfg = cfg or self._cfg()
        expiry = int(self._clock()) + TOKEN_TTL
        nonce = _b64(secrets.token_bytes(9))
        body = f'{expiry}.{nonce}'
        sig = hmac.new(self._key(cfg), body.encode('ascii'), hashlib.sha256).digest()
        return f'{body}.{_b64(sig)}'

    def check_token(self, token):
        cfg = self._cfg()
        if not cfg.get('admin_password_hash') or not isinstance(token, str):
            return False
        try:
            expiry, nonce, sig = token.split('.')
            if int(expiry) < self._clock():
                return False
            want = hmac.new(self._key(cfg), f'{expiry}.{nonce}'.encode('ascii'), hashlib.sha256).digest()
            return hmac.compare_digest(want, _unb64(sig))
        except (ValueError, TypeError):
            return False

    # ── lockout ─────────────────────────────────────────────────────────

    def _locked(self, client):
        now = self._clock()
        with self._lock:
            recent = [t for t in self._fails.get(client, []) if now - t < FAIL_WINDOW]
            self._fails[client] = recent
            if len(recent) >= MAX_FAILS:
                wait = int(LOCKOUT - (now - recent[-1]))
                if wait > 0:
                    return wait
                self._fails[client] = []
        return 0

    def _fail(self, client):
        with self._lock:
            self._fails.setdefault(client, []).append(self._clock())

    # ── requests ────────────────────────────────────────────────────────

    def login(self, client, password=None, token=None):
        """Returns {'ok': bool, 'token'?: str, 'error'?: str, 'retry_after'?: int}."""
        cfg = self._cfg()
        stored = cfg.get('admin_password_hash')
        if not stored:
            return {'ok': True, 'required': False}
        if token is not None and password is None:
            # A stale token is not a failed guess: browsers keep them for weeks.
            return {'ok': True} if self.check_token(token) else {'ok': False, 'error': 'expired'}
        wait = self._locked(client)
        if wait:
            return {'ok': False, 'error': 'locked', 'retry_after': wait}
        if not isinstance(password, str) or not verify_password(password[:MAX_LENGTH], stored):
            self._fail(client)
            return {'ok': False, 'error': 'wrong'}
        with self._lock:
            self._fails.pop(client, None)
        return {'ok': True, 'token': self.issue_token(cfg)}

    def set_password(self, authenticated, new_password):
        """Set, change or (with an empty string) remove the password.

        Setting the first one is open, as everything is while there is none.
        Changing or removing it needs an authenticated connection.
        """
        cfg = self._cfg()
        if cfg.get('admin_password_hash') and not authenticated:
            return {'ok': False, 'error': 'auth'}
        if new_password == '':
            if not cfg.get('admin_password_hash'):
                return {'ok': True, 'required': False}
            cfg.pop('admin_password_hash', None)
            cfg['admin_session_epoch'] = int(cfg.get('admin_session_epoch', 0)) + 1
            self._write(cfg)
            return {'ok': True, 'required': False}
        if not isinstance(new_password, str) or not (MIN_LENGTH <= len(new_password) <= MAX_LENGTH):
            return {'ok': False, 'error': 'length', 'min': MIN_LENGTH}
        cfg['admin_password_hash'] = hash_password(new_password)
        cfg['admin_session_epoch'] = int(cfg.get('admin_session_epoch', 0)) + 1
        self._write(cfg)
        return {'ok': True, 'required': True, 'token': self.issue_token(cfg)}

    def sign_out_everywhere(self, authenticated):
        cfg = self._cfg()
        if not cfg.get('admin_password_hash'):
            return {'ok': True}
        if not authenticated:
            return {'ok': False, 'error': 'auth'}
        cfg['admin_session_epoch'] = int(cfg.get('admin_session_epoch', 0)) + 1
        self._write(cfg)
        return {'ok': True}


def write_config_file(path, cfg):
    """Write config.json atomically, owner-only from the first byte."""
    tmp = path + '.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(cfg, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
