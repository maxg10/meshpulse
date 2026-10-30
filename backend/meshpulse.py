#!/usr/bin/env python3
# MeshPulse
# Copyright (C) 2025-2026 Mariusz Gieparda (MG Group — mg-group.ltd)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""
MeshPulse - Listen Mode with TTL + WebSocket
Works on slow Raspberry Pi Model B+
Real-time updates via WebSocket
"""
import logging
import subprocess
import json
import time
import re
import collections
from datetime import datetime
import sys
import os
import asyncio
import websockets
import threading
import base64
import sqlite3
import shutil
import tempfile
import uuid
import admin_auth   # backend/admin_auth.py, next to this file
import platform
import meshtastic
import meshtastic.tcp_interface
import meshtastic.serial_interface
from meshtastic import mesh_pb2, portnums_pb2
from meshtastic.protobuf import config_pb2

# Suppress noisy websockets handshake errors (browser reconnect after sleep)
logging.getLogger('websockets.server').setLevel(logging.CRITICAL)

def sanitize_str(s, max_len=100):
    """Sanitize string from radio: valid UTF-8, max length, strip control chars."""
    if not isinstance(s, str):
        s = str(s) if s is not None else ''
    try:
        s = s.encode('utf-8', errors='replace').decode('utf-8', errors='replace')
    except Exception:
        s = ''
    s = ''.join(c for c in s if ord(c) >= 32 or c in '\n\t')
    return s[:max_len]

def sanitize_message(s, max_len=512):
    """Sanitize message text — allow more chars but still limit length."""
    if not isinstance(s, str):
        s = str(s) if s is not None else ''
    try:
        s = s.encode('utf-8', errors='replace').decode('utf-8', errors='replace')
    except Exception:
        s = ''
    return s[:max_len]

def repr_flag_true(line, key):
    """True when `'key': True` appears in a packet printed as a Python repr,
    outside any string literal.

    `meshtastic --listen` prints each packet on one line, message text
    included, so a plain regex would also match a message whose text is
    "'pkiEncrypted': True". String literals ('...', "...", b'...') are
    skipped whole, honouring backslash escapes.
    """
    needle = f"'{key}':"
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        if c in ('"', "'"):
            if line.startswith(needle, i):
                j = i + len(needle)
                while j < n and line[j].isspace():
                    j += 1
                if line.startswith('True', j) and not line[j + 4:j + 5].isalnum():
                    return True
            i += 1
            while i < n and line[i] != c:
                i += 2 if line[i] == '\\' else 1
            i += 1
            continue
        i += 1
    return False


def sanitize_node_id(s):
    """Sanitize node ID — must be !hex format."""
    if not isinstance(s, str):
        return None
    s = s.strip()
    if not s.startswith('!') or len(s) < 3 or len(s) > 12:
        return s[:12] if s else None
    return s

def safe_json(obj):
    """JSON serialize with UTF-8 support, fallback to ASCII if needed."""
    try:
        result = json.dumps(obj, ensure_ascii=False)
        # Validate UTF-8 encoding before sending
        result.encode('utf-8')
        return result
    except (UnicodeEncodeError, ValueError) as e:
        print(f"[WS] UTF-8 encode error: {e}, falling back to ASCII")
        try:
            return json.dumps(obj, ensure_ascii=True)
        except Exception as e2:
            print(f"[WS] JSON encode error: {e2}")
            return json.dumps({'type': 'error', 'message': 'encode_error'})

MAPPER_VERSION = '2.9.2'

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Plugin system
try:
    from mapper.plugin_manager import PluginManager
    PLUGINS_AVAILABLE = True
except ImportError:
    PLUGINS_AVAILABLE = False
    print("[PLUGINS] Plugin system not available (mapper module not found)")

# Global set of connected WebSocket clients
# A GPS with no fix reports coordinates at or beside (0, 0) — open Atlantic,
# 600 km off Ghana, where no mesh node has ever stood. Half a degree is about
# 55 km, wide enough to catch the near-zero junk a stalled receiver emits and
# still narrower than the nearest land.
NULL_ISLAND_RADIUS_DEG = 0.5


def is_placeholder_position(lat, lon):
    """True when coordinates are the absence of a fix rather than a location."""
    try:
        return abs(float(lat)) < NULL_ISLAND_RADIUS_DEG and abs(float(lon)) < NULL_ISLAND_RADIUS_DEG
    except (TypeError, ValueError):
        return True


connected_clients = set()
# {websocket: {'plugin:<id>:<channel>', ...}} — which plugin channels each browser
# asked for. The plugin API has always promised that broadcast_ws(channel=...)
# reaches only subscribers; without this the backend had nowhere to record who
# subscribed, so it answered 'Unknown message type' and broadcast to everyone.
plugin_subscriptions = {}

# Config path (shared with frontend)
CONFIG_PATH = '/var/www/html/meshpulse/config.json'

# Runtime restart support
mapper = None
plugin_manager = None
_ADMIN_AUTH = None


def _admin_auth():
    """The optional admin password (see backend/admin_auth.py)."""
    global _ADMIN_AUTH
    if _ADMIN_AUTH is None:
        _ADMIN_AUTH = admin_auth.AdminAuth(load_config, lambda cfg: admin_auth.write_config_file(CONFIG_PATH, cfg))
    return _ADMIN_AUTH


def _auth_status(authenticated):
    return {'type': 'auth_status', 'required': _admin_auth().required(),
            'authenticated': bool(authenticated), 'protected': sorted(admin_auth.PROTECTED)}


async def _broadcast_auth_status():
    for ws in list(connected_clients):
        try:
            await ws.send(json.dumps(_auth_status(False)))
        except Exception:
            pass


async def _handle_auth_message(websocket, data, client_addr, authed_key):
    """auth, set_admin_password, admin_sign_out_everywhere. Returns the
    connection's new auth fingerprint (None = not authenticated)."""
    auth = _admin_auth()
    t = data.get('type')
    ip = client_addr[0] if client_addr else '?'
    if t == 'auth':
        res = await asyncio.to_thread(auth.login, ip, data.get('password'), data.get('token'))
        if res.get('ok'):
            authed_key = auth.fingerprint()
        if data.get('password') is not None:
            print(f"[AUTH] Login from {ip}: {'ok' if res.get('ok') else res.get('error')}")
        await websocket.send(json.dumps({'type': 'auth_result', 'required': auth.required(), **res}))
        return authed_key
    is_authed = authed_key is not None and authed_key == auth.fingerprint()
    if t == 'set_admin_password':
        res = await asyncio.to_thread(auth.set_password, is_authed, data.get('password'))
        if res.get('ok'):
            print(f"[AUTH] Admin password {'set' if res.get('required') else 'removed'} from {ip}")
            authed_key = auth.fingerprint() if res.get('required') else None
            await _broadcast_auth_status()
        await websocket.send(json.dumps({'type': 'admin_password_result', **res}))
    elif t == 'admin_sign_out_everywhere':
        res = auth.sign_out_everywhere(is_authed)
        if res.get('ok'):
            print(f"[AUTH] Signed out every browser, requested from {ip}")
            authed_key = None
            await _broadcast_auth_status()
        await websocket.send(json.dumps({'type': 'admin_sign_out_result', **res}))
    return authed_key
restart_event = threading.Event()
restart_config = {}
traceroute_restart = False      # Legacy flag - kept for compatibility, no longer used with Python API
send_restart = False            # Legacy flag - kept for compatibility, no longer used with Python API
send_restart_no_nodes = False   # Legacy flag - kept for compatibility, no longer used with Python API


def parse_tcp_target(raw):
    """Parse 'host', 'host:port', or '[ipv6]:port' into (host, port).
    Port defaults to 4403 (Meshtastic standard).
    Returns (host, None) when the port part is present but invalid."""
    raw = (raw or '').strip()
    default_port = 4403
    if raw.startswith('['):            # [ipv6]:port or [ipv6]
        host, _, rest = raw[1:].partition(']')
        port = rest[1:] if rest.startswith(':') else ''
    elif raw.count(':') == 1:          # host:port
        host, _, port = raw.partition(':')
    else:                              # bare host OR bare ipv6 (multiple colons)
        host, port = raw, ''
    if port:
        try:
            p = int(port)
            if not (1 <= p <= 65535):
                raise ValueError
        except ValueError:
            return host, None          # None signals invalid port to caller
        return host, p
    return host, default_port


class TCPMeshtasticInterface:
    """Wraps meshtastic.tcp_interface.TCPInterface for use as a TCP listener/sender."""

    def __init__(self, host, port=4403):
        self.host = host
        self.port = port
        self.interface = None
        self._on_receive_ref = None
        self._on_mqtt_proxy_ref = None

    def connect(self, on_receive=None):
        """Create TCPInterface and subscribe to incoming packets."""
        import concurrent.futures
        from pubsub import pub

        iface_ref = [None]  # mutable cell so inner func captures final value

        def _on_receive(packet, interface):
            if interface is iface_ref[0] and on_receive is not None:
                try:
                    on_receive(packet)
                except Exception as e:
                    print(f"[TCP] Packet callback error: {e}")

        def _on_mqtt_proxy(proxymessage, interface):
            if proxymessage and plugin_manager and plugin_manager.plugins:
                plugin_manager.dispatch_hook_sync('on_mqtt_proxy',
                                                  proxymessage.topic, proxymessage.data)

        self._on_receive_ref = _on_receive
        self._on_mqtt_proxy_ref = _on_mqtt_proxy
        pub.subscribe(_on_receive, "meshtastic.receive")
        pub.subscribe(_on_mqtt_proxy, "meshtastic.mqttclientproxymessage")

        def _create_interface():
            self.interface = meshtastic.tcp_interface.TCPInterface(
                self.host,
                portNumber=self.port,
                noNodes=True,
                debugOut=None,
                timeout=15
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_create_interface)
            try:
                future.result(timeout=5)
            except concurrent.futures.TimeoutError:
                raise Exception("Connection timeout")

        iface_ref[0] = self.interface

    @property
    def localNode(self):
        return self.interface.localNode

    @property
    def nodes(self):
        return self.interface.nodes

    def getMyNodeInfo(self):
        return self.interface.getMyNodeInfo()

    def disconnect(self):
        """Unsubscribe and close the interface."""
        from pubsub import pub
        if self._on_receive_ref is not None:
            try:
                pub.unsubscribe(self._on_receive_ref, "meshtastic.receive")
            except Exception:
                pass
            self._on_receive_ref = None
        if self._on_mqtt_proxy_ref is not None:
            try:
                pub.unsubscribe(self._on_mqtt_proxy_ref, "meshtastic.mqttclientproxymessage")
            except Exception:
                pass
            self._on_mqtt_proxy_ref = None
        if self.interface is not None:
            try:
                self.interface.close()
            except Exception as e:
                print(f"[TCP] Close error: {e}")
            self.interface = None

    def sendText(self, text, channelIndex=0, destinationId='^all'):
        """Send a text message via the TCP interface."""
        if self.interface is None:
            raise RuntimeError("TCPMeshtasticInterface not connected")
        self.interface.sendText(text, channelIndex=channelIndex, destinationId=destinationId)


class SerialMeshtasticInterface:
    """Wraps meshtastic SerialInterface for USB/serial connections."""

    def __init__(self, port=None):
        self.port = port  # None = auto-detect
        self.iface = None
        self._on_receive_ref = None
        self._on_mqtt_proxy_ref = None

    def connect(self, on_receive, on_connection_established=None):
        """Connect to serial device and start listening."""
        from pubsub import pub

        iface_ref = [None]  # mutable cell so inner func captures final value

        def _on_receive(packet, interface):
            if interface is iface_ref[0] and on_receive is not None:
                try:
                    on_receive(packet)
                except Exception as e:
                    print(f"[SERIAL] Packet callback error: {e}")

        def _on_mqtt_proxy(proxymessage, interface):
            if proxymessage and plugin_manager and plugin_manager.plugins:
                plugin_manager.dispatch_hook_sync('on_mqtt_proxy',
                                                  proxymessage.topic, proxymessage.data)

        self._on_receive_ref = _on_receive
        self._on_mqtt_proxy_ref = _on_mqtt_proxy
        pub.subscribe(_on_receive, "meshtastic.receive")
        pub.subscribe(_on_mqtt_proxy, "meshtastic.mqttclientproxymessage")

        self.iface = meshtastic.serial_interface.SerialInterface(devPath=self.port)
        iface_ref[0] = self.iface

        if on_connection_established:
            on_connection_established(self.iface)

        return self.iface

    def disconnect(self):
        """Disconnect from serial device."""
        from pubsub import pub
        if self._on_receive_ref is not None:
            try:
                pub.unsubscribe(self._on_receive_ref, "meshtastic.receive")
            except Exception:
                pass
            self._on_receive_ref = None
        if self._on_mqtt_proxy_ref is not None:
            try:
                pub.unsubscribe(self._on_mqtt_proxy_ref, "meshtastic.mqttclientproxymessage")
            except Exception:
                pass
            self._on_mqtt_proxy_ref = None
        if self.iface is not None:
            try:
                self.iface.close()
            except Exception as e:
                print(f"[SERIAL] Close error: {e}")
            self.iface = None

    def sendText(self, text, channelIndex=0, destinationId='^all'):
        """Send a text message."""
        if not self.iface:
            raise Exception("Not connected")
        self.iface.sendText(text, channelIndex=channelIndex, destinationId=destinationId)

    def getNode(self):
        """Get local node for config access."""
        if not self.iface:
            raise Exception("Not connected")
        return self.iface.localNode


_PLUGIN_PART = re.compile(r'^[A-Za-z0-9_.-]+$')


def _restorable_plugin_file(name):
    """True for the plugin files a backup writes: plugins/enabled.json and
    plugins/<author>/<name>/config.json, with plain path segments only."""
    if name == 'plugins/enabled.json':
        return True
    parts = name.split('/')
    return (len(parts) == 4 and parts[0] == 'plugins' and parts[3] == 'config.json'
            and all(_PLUGIN_PART.match(x) and x not in ('.', '..') for x in parts[1:3]))


class StatsDB:
    """SQLite database for network statistics.

    The raw tables (packets, anomalies, neighbors, radio_stats_history) keep
    `retention_days` of data: 30 by default, `stats_retention_days` in config.json.
    The hourly rollup (traffic_hourly, traffic_hourly_port) holds about 9k rows a
    year and is never pruned, so long-term trends survive the raw retention.
    """

    DB_PATH = '/var/www/html/meshpulse/stats.db'
    DEFAULT_RETENTION_DAYS = 30
    MAX_RETENTION_DAYS = 365
    # Two radio samples further apart than this are a gap, not a delta: pinning
    # hours of counter growth on a single hour would invent a traffic spike.
    RADIO_MAX_GAP = 7200
    # Upper bounds on work per call. A backfill, or a retention lowered from 30
    # days to 3, is spread over several minutes instead of stalling the listener.
    ROLLUP_MAX_HOURS = 48
    CLEANUP_CHUNK = 500
    CLEANUP_MAX_CHUNKS = 4   # 500 rows take ~0.5 s on a Pi 2 with every index updated

    def __init__(self, retention_days=DEFAULT_RETENTION_DAYS):
        self.lock = threading.Lock()
        self.retention_days = retention_days
        self._init_db()
        # Every query opens and closes its own connection, and SQLite checkpoints
        # and deletes the WAL whenever the last connection to a file closes: an
        # fsync per packet again. One idle connection held for the life of the
        # process keeps the WAL in place; SQLite checkpoints it every ~1000 pages.
        self._wal_anchor = self._open_anchor()

    def _open_anchor(self):
        # A connection only counts once it has read the file; an idle one that
        # never ran a query does not keep the WAL alive. It is opened at start
        # and closed by a restore, which runs in a worker thread, so it must
        # not be tied to one thread; nothing else ever uses it.
        conn = sqlite3.connect(self.DB_PATH, timeout=10, check_same_thread=False)
        conn.execute('SELECT 1 FROM sqlite_master LIMIT 1').fetchone()
        return conn

    def _connect(self):
        """Open stats.db. Every query goes through here.

        The database runs in WAL mode (set once in _init_db; it is stored in
        the file). With synchronous=NORMAL a commit appends to the WAL without
        an fsync, and the fsync happens at checkpoint time: on an SD card that
        is the difference between a write per packet and a write every few
        hundred. A power cut can lose the last seconds of statistics, never
        the database.
        """
        conn = sqlite3.connect(self.DB_PATH, timeout=10)
        conn.execute('PRAGMA synchronous=NORMAL')
        # The WAL is reused rather than shrunk; cap what it keeps on disk.
        conn.execute('PRAGMA journal_size_limit=4194304')
        return conn

    def snapshot(self, dest_path):
        """Write a consistent, self-contained copy of the database to dest_path.

        Copying stats.db as a file misses whatever is still in the WAL and can
        catch a write half done. The backup API copies a consistent state, and
        the copy is switched to a rollback journal so it is a single file.
        """
        with self.lock:
            src = self._connect()
            try:
                dst = sqlite3.connect(dest_path)
                try:
                    src.backup(dst)
                    dst.execute('PRAGMA journal_mode=DELETE')
                finally:
                    dst.close()
            finally:
                src.close()

    def replace_with(self, data):
        """Swap in a database restored from a backup.

        `data` is bytes or a binary file object (streamed, so a large
        database never sits in memory whole). The old -wal and -shm files
        go first: SQLite would otherwise replay the old WAL on top of the
        new file. The new file is checked to be a SQLite database before
        anything is removed.
        """
        tmp = self.DB_PATH + '.restore'
        with self.lock:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'wb') as f:
                if isinstance(data, (bytes, bytearray)):
                    f.write(data)
                else:
                    shutil.copyfileobj(data, f, 1024 * 1024)
            try:
                check = sqlite3.connect(tmp)
                try:
                    check.execute('PRAGMA journal_mode=DELETE')
                    check.execute('SELECT count(*) FROM sqlite_master').fetchone()
                finally:
                    check.close()
            except sqlite3.DatabaseError:
                os.remove(tmp)
                raise ValueError('stats.db in the backup is not a SQLite database')
            try:
                # The anchor holds the old file open; let it go, and anchor the new one.
                anchor = getattr(self, '_wal_anchor', None)
                if anchor is not None:
                    self._wal_anchor = None
                    anchor.close()
                for suffix in ('-wal', '-shm'):
                    try:
                        os.remove(self.DB_PATH + suffix)
                    except FileNotFoundError:
                        pass
                os.replace(tmp, self.DB_PATH)
            except BaseException:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise
            conn = self._connect()
            conn.execute('PRAGMA journal_mode=WAL')
            conn.close()
            self._wal_anchor = self._open_anchor()
            for suffix in ('', '-wal', '-shm'):
                try:
                    os.chmod(self.DB_PATH + suffix, 0o600)
                except OSError:
                    pass

    @classmethod
    def clamp_retention(cls, value):
        """config.json is edited by hand: anything that is not a whole number of
        days falls back to the default, and the rest is clamped to 1..365."""
        try:
            days = int(value)
        except (TypeError, ValueError):
            return cls.DEFAULT_RETENTION_DAYS
        return max(1, min(cls.MAX_RETENTION_DAYS, days))

    def _init_db(self):
        with self.lock:
            conn = self._connect()
            c = conn.cursor()
            # Persistent: once set, every later connection opens the file in WAL.
            # Readers (the Stats page, Ground Truth) then no longer wait for the
            # packet logger, and the logger no longer waits for them.
            mode = c.execute('PRAGMA journal_mode=WAL').fetchone()[0]
            if str(mode).lower() != 'wal':
                print(f"[STATS] journal_mode is {mode}, WAL not available on this filesystem")
            # stats.db lives in the web root, so the web server would hand it to
            # anyone on the network (admin audit with client addresses, every
            # remote admin change). Owner-only, like config.json; SQLite gives
            # the -wal and -shm files the database file's permissions.
            for suffix in ('', '-wal', '-shm'):
                try:
                    os.chmod(self.DB_PATH + suffix, 0o600)
                except OSError:
                    pass
            c.execute('''CREATE TABLE IF NOT EXISTS packets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                from_id TEXT NOT NULL,
                from_name TEXT,
                portnum TEXT NOT NULL,
                hops INTEGER,
                snr REAL,
                rssi INTEGER,
                via_mqtt INTEGER DEFAULT 0,
                relay_node_id TEXT,
                relayed_by_us INTEGER DEFAULT 0
            )''')
            c.execute('''CREATE TABLE IF NOT EXISTS node_activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                node_id TEXT NOT NULL,
                node_name TEXT,
                packet_count INTEGER DEFAULT 0,
                portnum TEXT,
                avg_snr REAL,
                avg_rssi REAL,
                min_hops INTEGER,
                max_hops INTEGER
            )''')
            c.execute('''CREATE TABLE IF NOT EXISTS anomalies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                node_id TEXT NOT NULL,
                node_name TEXT,
                anomaly_type TEXT NOT NULL,
                details TEXT,
                severity TEXT DEFAULT 'warning'
            )''')
            c.execute('''CREATE TABLE IF NOT EXISTS neighbors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    from_id TEXT NOT NULL,
    from_name TEXT,
    neighbor_id TEXT NOT NULL,
    neighbor_name TEXT,
    snr REAL
)''')
            c.execute('CREATE INDEX IF NOT EXISTS idx_packets_ts ON packets(ts)')
            # (from_id, portnum, ts) serves every from_id lookup the old single-column
            # index did, and turns the "last relay" subquery in get_stats_summary from
            # a scan of each node's whole history into a range seek. Measured on a
            # Pi 2 with 30 days of traffic: 4.95 s -> 0.47 s, run every minute while
            # holding the lock that packet logging waits on.
            c.execute('DROP INDEX IF EXISTS idx_packets_from')
            c.execute('CREATE INDEX IF NOT EXISTS idx_packets_from_port_ts ON packets(from_id, portnum, ts)')
            # relayed_by_us was never true for a real packet: relayNode carries one
            # byte, and a client never sees its own node's rebroadcasts. The column
            # stays (old databases have it), the index it cost on every insert goes.
            c.execute('DROP INDEX IF EXISTS idx_packets_relay')
            c.execute('CREATE INDEX IF NOT EXISTS idx_packets_mqtt ON packets(via_mqtt, ts)')
            c.execute('CREATE INDEX IF NOT EXISTS idx_anomalies_ts ON anomalies(ts)')
            c.execute('CREATE INDEX IF NOT EXISTS idx_neighbors_ts ON neighbors(ts)')
            c.execute('CREATE INDEX IF NOT EXISTS idx_neighbors_from ON neighbors(from_id)')
            c.execute('''CREATE TABLE IF NOT EXISTS radio_stats_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                channel_utilization REAL,
                air_util_tx REAL,
                num_packets_tx INTEGER,
                num_packets_rx INTEGER,
                num_packets_rx_bad INTEGER,
                num_rx_dupe INTEGER,
                num_tx_relay INTEGER,
                num_online_nodes INTEGER,
                num_total_nodes INTEGER
            )''')
            c.execute('CREATE INDEX IF NOT EXISTS idx_radio_stats_ts ON radio_stats_history(ts)')
            # Hourly rollup, kept indefinitely. Packet columns count what this mapper
            # logged (RF and MQTT apart; SNR/RSSI/hops averaged over RF only, since an
            # MQTT packet carries somebody else's radio conditions). radio_* columns
            # are growth of the node's own firmware counters within the hour.
            c.execute('''CREATE TABLE IF NOT EXISTS traffic_hourly (
                hour_ts INTEGER PRIMARY KEY,
                pkts_rf INTEGER NOT NULL DEFAULT 0,
                pkts_mqtt INTEGER NOT NULL DEFAULT 0,
                senders INTEGER NOT NULL DEFAULT 0,
                avg_snr REAL,
                avg_rssi REAL,
                avg_hops REAL,
                radio_rx INTEGER,
                radio_dupe INTEGER,
                radio_bad INTEGER,
                radio_tx INTEGER,
                radio_relay INTEGER,
                cu_avg REAL,
                cu_max REAL,
                air_tx_avg REAL,
                nodes_online INTEGER,
                samples INTEGER NOT NULL DEFAULT 0
            )''')
            # Every public key MeshPulse has seen, by node. The radio's own NodeDB holds
            # far fewer nodes than a city mesh (80 on a Wio Tracker L1), so keys it has
            # dropped can be handed back with add_contact before a PKC admin request.
            # first_seen / changes are the seed of key-change detection.
            c.execute('''CREATE TABLE IF NOT EXISTS node_keys (
                node_id TEXT PRIMARY KEY,
                public_key TEXT NOT NULL,
                first_seen INTEGER NOT NULL,
                last_seen INTEGER NOT NULL,
                via_mqtt INTEGER NOT NULL DEFAULT 0,
                changes INTEGER NOT NULL DEFAULT 0
            )''')
            # Every remote admin write: who was changed, what, and whether it took
            c.execute('''CREATE TABLE IF NOT EXISTS admin_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                dest TEXT NOT NULL,
                action TEXT NOT NULL,
                section TEXT,
                changes TEXT,
                outcome TEXT,
                rtt REAL
            )''')
            # One row per (hour, portnum): a port the mapper starts logging later is a
            # new row, not a schema migration.
            c.execute('''CREATE TABLE IF NOT EXISTS traffic_hourly_port (
                hour_ts INTEGER NOT NULL,
                portnum TEXT NOT NULL,
                rf INTEGER NOT NULL DEFAULT 0,
                mqtt INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (hour_ts, portnum)
            )''')
            # One-time cleanup: remove bad rows inserted by deviceMetrics (no localStats data)
            c.execute('''DELETE FROM radio_stats_history
                WHERE num_tx_relay IS NULL
                AND num_packets_tx IS NULL
                AND channel_utilization IS NULL''')
            conn.commit()
            conn.close()

    def log_packet(self, from_id, from_name, portnum, hops, snr, rssi, via_mqtt, relay_node_id, relayed_by_us):
        with self.lock:
            conn = self._connect()
            conn.execute('''INSERT INTO packets
                (ts, from_id, from_name, portnum, hops, snr, rssi, via_mqtt, relay_node_id, relayed_by_us)
                VALUES (?,?,?,?,?,?,?,?,?,?)''',
                (int(time.time()), from_id, from_name, portnum, hops, snr, rssi,
                 1 if via_mqtt else 0, relay_node_id, 1 if relayed_by_us else 0))
            conn.commit()
            conn.close()

    def log_anomaly(self, node_id, node_name, anomaly_type, details, severity='warning'):
        with self.lock:
            conn = self._connect()
            conn.execute('''INSERT INTO anomalies (ts, node_id, node_name, anomaly_type, details, severity)
                VALUES (?,?,?,?,?,?)''',
                (int(time.time()), node_id, node_name, anomaly_type, details, severity))
            conn.commit()
            conn.close()

    def log_radio_stats(self, radio_stats: dict):
        """Store localStats snapshot. Only insert if localStats-specific fields are present."""
        if not any([
            radio_stats.get('numPacketsTx'),
            radio_stats.get('numPacketsRx'),
            radio_stats.get('numTxRelay'),
            radio_stats.get('channelUtilization'),
        ]):
            return
        with self.lock:
            conn = self._connect()
            conn.execute('''INSERT INTO radio_stats_history
                (ts, channel_utilization, air_util_tx, num_packets_tx,
                 num_packets_rx, num_packets_rx_bad, num_rx_dupe,
                 num_tx_relay, num_online_nodes, num_total_nodes)
                VALUES (?,?,?,?,?,?,?,?,?,?)''', (
                int(time.time()),
                radio_stats.get('channelUtilization'),
                radio_stats.get('airUtilTx'),
                radio_stats.get('numPacketsTx'),
                radio_stats.get('numPacketsRx'),
                radio_stats.get('numPacketsRxBad'),
                radio_stats.get('numRxDupe'),
                radio_stats.get('numTxRelay'),
                radio_stats.get('numOnlineNodes'),
                radio_stats.get('numTotalNodes'),
            ))
            conn.commit()
            conn.close()

    def cleanup_old_data(self):
        """Prune the raw tables to retention_days. The rollup tables are never pruned.

        Runs every minute, so normally it removes a minute's worth of rows. packets
        is the one table that grows large, and a lowered retention can leave weeks
        to delete at once (one day of a busy mesh took 29 s in a single DELETE on a
        Pi 2). So it goes in chunks, releasing the lock in between, and stops after
        CLEANUP_MAX_CHUNKS; the rest waits for the next call.
        """
        cutoff = int(time.time()) - (self.retention_days * 86400)
        for _ in range(self.CLEANUP_MAX_CHUNKS):
            with self.lock:
                conn = self._connect()
                try:
                    deleted = conn.execute(
                        'DELETE FROM packets WHERE id IN '
                        '(SELECT id FROM packets WHERE ts < ? LIMIT ?)',
                        (cutoff, self.CLEANUP_CHUNK)).rowcount
                    conn.commit()
                finally:
                    conn.close()
            if deleted < self.CLEANUP_CHUNK:
                break
        with self.lock:
            conn = self._connect()
            conn.execute('DELETE FROM node_activity WHERE ts < ?', (cutoff,))
            conn.execute('DELETE FROM anomalies WHERE ts < ?', (cutoff,))
            conn.execute('DELETE FROM neighbors WHERE ts < ?', (cutoff,))
            conn.execute('DELETE FROM radio_stats_history WHERE ts < ?', (cutoff,))
            conn.commit()
            conn.close()

    @staticmethod
    def _counter_delta(prev, cur):
        """Growth of a firmware counter between two samples. The counters run from
        the node's boot, so a drop means it rebooted: everything since is new."""
        if prev is None or cur is None:
            return None
        return cur - prev if cur >= prev else cur

    def _next_data_ts(self, since):
        """Earliest raw timestamp at or after `since`, in either source table."""
        with self.lock:
            conn = self._connect()
            try:
                return conn.execute('''SELECT MIN(t) FROM (
                    SELECT MIN(ts) AS t FROM packets WHERE ts >= ?
                    UNION ALL
                    SELECT MIN(ts) FROM radio_stats_history WHERE ts >= ?)''',
                    (since, since)).fetchone()[0]
            finally:
                conn.close()

    def _rollup_hour(self, hour):
        """Rewrite the rollup rows for [hour, hour + 3600). False if it holds no data.

        A pair of consecutive radio samples is credited to the hour of the later
        one, using the last sample before the hour as the baseline. Summed over
        hours, that reproduces the counters' total growth, minus gaps longer than
        RADIO_MAX_GAP, which are dropped rather than guessed.
        """
        end = hour + 3600
        cols = ('ts, channel_utilization, air_util_tx, num_packets_rx, num_rx_dupe, '
                'num_packets_rx_bad, num_packets_tx, num_tx_relay, num_online_nodes')
        with self.lock:
            conn = self._connect()
            try:
                pk = conn.execute('''SELECT
                        COALESCE(SUM(via_mqtt = 0), 0),
                        COALESCE(SUM(via_mqtt = 1), 0),
                        COUNT(DISTINCT from_id),
                        AVG(CASE WHEN via_mqtt = 0 THEN snr END),
                        AVG(CASE WHEN via_mqtt = 0 THEN rssi END),
                        AVG(CASE WHEN via_mqtt = 0 AND hops >= 0 THEN hops END)
                    FROM packets WHERE ts >= ? AND ts < ?''', (hour, end)).fetchone()
                samples = conn.execute(
                    f'SELECT {cols} FROM radio_stats_history '
                    'WHERE ts >= ? AND ts < ? ORDER BY ts', (hour, end)).fetchall()
                if pk[0] + pk[1] == 0 and not samples:
                    return False
                ports = conn.execute('''SELECT portnum, SUM(via_mqtt = 0), SUM(via_mqtt = 1)
                    FROM packets WHERE ts >= ? AND ts < ? GROUP BY portnum''',
                    (hour, end)).fetchall()
                prev = conn.execute(
                    f'SELECT {cols} FROM radio_stats_history '
                    'WHERE ts < ? AND ts >= ? ORDER BY ts DESC LIMIT 1',
                    (hour, hour - self.RADIO_MAX_GAP)).fetchone()

                # rx, dupe, bad, tx, relay: columns 3..7 of `cols`
                deltas = [None] * 5
                for s in samples:
                    if prev is not None and s[0] - prev[0] <= self.RADIO_MAX_GAP:
                        for i in range(5):
                            d = self._counter_delta(prev[3 + i], s[3 + i])
                            if d is not None:
                                deltas[i] = (deltas[i] or 0) + d
                    prev = s
                cu = [s[1] for s in samples if s[1] is not None]
                air = [s[2] for s in samples if s[2] is not None]
                online = [s[8] for s in samples if s[8] is not None]

                conn.execute('''INSERT OR REPLACE INTO traffic_hourly
                    (hour_ts, pkts_rf, pkts_mqtt, senders, avg_snr, avg_rssi, avg_hops,
                     radio_rx, radio_dupe, radio_bad, radio_tx, radio_relay,
                     cu_avg, cu_max, air_tx_avg, nodes_online, samples)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (
                    hour, pk[0], pk[1], pk[2], pk[3], pk[4], pk[5],
                    *deltas,
                    sum(cu) / len(cu) if cu else None,
                    max(cu) if cu else None,
                    sum(air) / len(air) if air else None,
                    max(online) if online else None,
                    len(samples)))
                conn.execute('DELETE FROM traffic_hourly_port WHERE hour_ts = ?', (hour,))
                conn.executemany(
                    'INSERT INTO traffic_hourly_port (hour_ts, portnum, rf, mqtt) VALUES (?,?,?,?)',
                    [(hour, p[0], p[1], p[2]) for p in ports])
                conn.commit()
                return True
            finally:
                conn.close()

    def rollup_traffic(self, now=None):
        """Fold every complete hour not rolled up yet into the hourly tables.

        Idempotent: each hour is rewritten whole, so a second call, a restart
        halfway through, or two mapper instances racing after a reconnect cannot
        double-count. The first run after an upgrade backfills whatever the raw
        tables still hold, skipping the oldest hour if retention already cut into
        it. Hours with no data at all get no row: a gap stays a gap, it does not
        turn into a quiet hour. Returns the number of hours written.
        """
        now = int(time.time() if now is None else now)
        current = (now // 3600) * 3600  # the hour in progress is never rolled up
        with self.lock:
            conn = self._connect()
            try:
                last = conn.execute('SELECT MAX(hour_ts) FROM traffic_hourly').fetchone()[0]
            finally:
                conn.close()
        if last is not None:
            hour = last + 3600
        else:
            first = self._next_data_ts(0)
            if first is None:
                return 0
            hour = (first // 3600) * 3600
            if first > hour:
                hour += 3600
        written = 0
        for _ in range(self.ROLLUP_MAX_HOURS):
            if hour >= current:
                break
            if self._rollup_hour(hour):
                written += 1
                hour += 3600
            else:
                nxt = self._next_data_ts(hour + 3600)
                if nxt is None:
                    break
                hour = (nxt // 3600) * 3600
        return written

    def remember_public_key(self, node_id, public_key_b64, via_mqtt=False):
        """Store a node's public key. Returns 'new', 'same' or 'changed'.
        An MQTT sighting never replaces a key heard over radio."""
        now = int(time.time())
        with self.lock:
            conn = self._connect()
            try:
                row = conn.execute('SELECT public_key, via_mqtt FROM node_keys WHERE node_id = ?',
                                   (node_id,)).fetchone()
                if row is None:
                    conn.execute('INSERT INTO node_keys (node_id, public_key, first_seen, last_seen, via_mqtt) '
                                 'VALUES (?,?,?,?,?)', (node_id, public_key_b64, now, now, 1 if via_mqtt else 0))
                    outcome = 'new'
                elif row[0] == public_key_b64:
                    conn.execute('UPDATE node_keys SET last_seen = ?, via_mqtt = via_mqtt AND ? WHERE node_id = ?',
                                 (now, 1 if via_mqtt else 0, node_id))
                    outcome = 'same'
                elif via_mqtt and not row[1]:
                    outcome = 'same'   # do not let a broker overwrite what the radio heard
                else:
                    conn.execute('UPDATE node_keys SET public_key = ?, last_seen = ?, via_mqtt = ?, '
                                 'changes = changes + 1 WHERE node_id = ?',
                                 (public_key_b64, now, 1 if via_mqtt else 0, node_id))
                    outcome = 'changed'
                conn.commit()
                return outcome
            finally:
                conn.close()

    def log_admin_action(self, dest, action, section, changes, outcome, rtt=None):
        with self.lock:
            conn = self._connect()
            try:
                conn.execute('INSERT INTO admin_audit (ts, dest, action, section, changes, outcome, rtt) '
                             'VALUES (?,?,?,?,?,?,?)', (int(time.time()), dest, action, section, changes, outcome, rtt))
                conn.commit()
            finally:
                conn.close()

    def recent_admin_actions(self, limit=10):
        with self.lock:
            conn = self._connect()
            try:
                rows = conn.execute('SELECT ts, dest, action, section, changes, outcome FROM admin_audit '
                                    'ORDER BY id DESC LIMIT ?', (int(limit),)).fetchall()
            finally:
                conn.close()
        return [{'ts': r[0], 'dest': r[1], 'action': r[2], 'section': r[3], 'changes': r[4], 'outcome': r[5]}
                for r in rows]

    def get_public_key(self, node_id):
        with self.lock:
            conn = self._connect()
            try:
                row = conn.execute('SELECT public_key FROM node_keys WHERE node_id = ?', (node_id,)).fetchone()
                return row[0] if row else None
            finally:
                conn.close()

    # range key -> (span in seconds, bucket in seconds)
    HISTORY_RANGES = {
        '24h': (86400, 3600),
        '7d': (7 * 86400, 3600),
        '30d': (30 * 86400, 86400),
        '1y': (365 * 86400, 86400),
    }

    def get_traffic_history(self, range_key='7d', now=None):
        """Rollup rows for the Stats history charts.

        24h and 7d come back per hour, 30d and 1y per local calendar day. Only
        complete hours exist in the rollup, so the newest bucket is the last full
        hour. Daily rows aggregate the hourly ones: counters are summed, channel
        utilisation is averaged weighted by radio samples, and `senders` becomes
        the busiest hour of the day (distinct senders do not add up across hours).
        """
        if range_key not in self.HISTORY_RANGES:
            range_key = '7d'
        span, bucket = self.HISTORY_RANGES[range_key]
        now = int(time.time() if now is None else now)
        since = (now // 3600) * 3600 - span
        key = 'hour_ts' if bucket == 3600 else "date(hour_ts, 'unixepoch', 'localtime')"
        with self.lock:
            conn = self._connect()
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(f'''SELECT {key} AS bucket_key, MIN(hour_ts) AS ts,
                        COUNT(*) AS hours,
                        SUM(pkts_rf) AS pkts_rf, SUM(pkts_mqtt) AS pkts_mqtt,
                        MAX(senders) AS senders,
                        SUM(radio_rx) AS radio_rx, SUM(radio_dupe) AS radio_dupe,
                        SUM(radio_bad) AS radio_bad, SUM(radio_tx) AS radio_tx,
                        SUM(radio_relay) AS radio_relay,
                        SUM(cu_avg * samples) / NULLIF(SUM(CASE WHEN cu_avg IS NOT NULL THEN samples END), 0) AS cu_avg,
                        MAX(cu_max) AS cu_max,
                        SUM(air_tx_avg * samples) / NULLIF(SUM(CASE WHEN air_tx_avg IS NOT NULL THEN samples END), 0) AS air_tx_avg,
                        MAX(nodes_online) AS nodes_online,
                        SUM(avg_snr * pkts_rf) / NULLIF(SUM(CASE WHEN avg_snr IS NOT NULL THEN pkts_rf END), 0) AS avg_snr
                    FROM traffic_hourly WHERE hour_ts >= ?
                    GROUP BY bucket_key ORDER BY ts''', (since,)).fetchall()
                ports = conn.execute('''SELECT portnum, SUM(rf) AS rf, SUM(mqtt) AS mqtt
                    FROM traffic_hourly_port WHERE hour_ts >= ?
                    GROUP BY portnum ORDER BY SUM(rf + mqtt) DESC''', (since,)).fetchall()
                first_ts = conn.execute('SELECT MIN(hour_ts) FROM traffic_hourly').fetchone()[0]
            finally:
                conn.close()
        return {
            'range': range_key,
            'bucket': bucket,
            'since': since,
            'now': now,
            'first_ts': first_ts,
            'retention_days': self.retention_days,
            # Floats rounded: a year of daily rows goes over the WebSocket as is.
            'rows': [{k: (round(v, 2) if isinstance(v, float) else v) for k, v in dict(r).items()}
                     for r in rows],
            'ports': [dict(r) for r in ports],
        }

    def clear_node_packets(self, node_id):
        """Delete all packet history for a specific node from stats.db."""
        with self.lock:
            conn = self._connect()
            conn.execute('DELETE FROM packets WHERE from_id = ?', (node_id,))
            conn.execute('DELETE FROM anomalies WHERE node_id = ?', (node_id,))
            conn.commit()
            conn.close()

    def clear_all_stats(self):
        """Delete ALL data from all stats tables."""
        with self.lock:
            conn = self._connect()
            conn.execute('DELETE FROM packets')
            conn.execute('DELETE FROM node_activity')
            conn.execute('DELETE FROM anomalies')
            conn.execute('DELETE FROM neighbors')
            conn.execute('DELETE FROM traffic_hourly')
            conn.execute('DELETE FROM traffic_hourly_port')
            conn.commit()
            conn.close()

    def update_node_name(self, node_id, name):
        """Update from_name for all packets from this node where name was unknown (= node_id)."""
        if not name or name == node_id:
            return
        with self.lock:
            conn = self._connect()
            conn.execute('''UPDATE packets SET from_name = ?
                           WHERE from_id = ? AND (from_name = ? OR from_name IS NULL)''',
                        (name, node_id, node_id))
            conn.execute('''UPDATE anomalies SET node_name = ?
                           WHERE node_id = ? AND (node_name = ? OR node_name IS NULL)''',
                        (name, node_id, node_id))
            conn.commit()
            conn.close()

    def log_neighbor_info(self, from_id, from_name, neighbors):
        """Store neighbor info packet data."""
        now = int(time.time())
        with self.lock:
            conn = self._connect()
            conn.execute('DELETE FROM neighbors WHERE from_id = ?', (from_id,))
            for neighbor in neighbors:
                conn.execute('''INSERT INTO neighbors (ts, from_id, from_name, neighbor_id, neighbor_name, snr)
                    VALUES (?,?,?,?,?,?)''',
                    (now, from_id, from_name, neighbor['id'], neighbor.get('name', neighbor['id']), neighbor.get('snr')))
            conn.commit()
            conn.close()

    def get_relay_nodes_for_map(self, local_node_id):
        """Get nodes whose packets relayed through our node (we were relay_node_id)."""
        if not local_node_id:
            return []
        since = int(time.time()) - 86400
        own_variants = [
            local_node_id,
            local_node_id.lstrip('!'),
            '!' + local_node_id.lstrip('!')
        ]
        last_byte_hex = ''
        try:
            last_byte_hex = f"relay_{int(local_node_id.replace('!',''), 16) & 0xFF:02x}"
        except Exception:
            last_byte_hex = ''
        with self.lock:
            conn = self._connect()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            rows = c.execute('''
                SELECT from_id, MAX(from_name) as from_name, COUNT(*) as cnt
                FROM packets
                WHERE ts > ?
                AND (
                    relay_node_id IN (?, ?, ?)
                    OR relay_node_id = ?
                )
                AND from_id NOT IN (?, ?, ?)
                GROUP BY from_id
                ORDER BY cnt DESC
                LIMIT 50
            ''', (since, *own_variants, last_byte_hex, *own_variants)).fetchall()
            conn.close()
            return [{'id': r['from_id'], 'name': r['from_name'], 'count': r['cnt']} for r in rows]

    def get_neighbor_graph(self):
        """Get neighbor graph for topology visualization. Only returns data from last 6 hours."""
        since = int(time.time()) - 21600
        with self.lock:
            conn = self._connect()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            edges = c.execute('''SELECT from_id, from_name, neighbor_id, neighbor_name, snr, MAX(ts) as last_seen
                FROM neighbors WHERE ts > ?
                GROUP BY from_id, neighbor_id
                ORDER BY last_seen DESC''', (since,)).fetchall()
            conn.close()
            return [dict(e) for e in edges]

    def backfill_names(self, nodes_dict):
        """Update from_name for nodes where name = node_id (unknown at time of logging)."""
        with self.lock:
            conn = self._connect()
            for node_id, node in nodes_dict.items():
                name = node.get('name')
                if name and name != node_id:
                    conn.execute('''UPDATE packets SET from_name = ?
                        WHERE from_id = ? AND from_name = ?''',
                        (name, node_id, node_id))
                    conn.execute('''UPDATE anomalies SET node_name = ?
                        WHERE node_id = ? AND node_name = ?''',
                        (name, node_id, node_id))
            conn.commit()
            conn.close()

    def get_stats_summary(self, local_node_id=None):
        """Get stats summary for the last 24h plus 7-day trend."""
        now = int(time.time())
        since_24h = now - 86400
        since_7d = now - 7 * 86400
        with self.lock:
            conn = self._connect()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()

            total = c.execute('SELECT COUNT(*) as cnt FROM packets WHERE ts > ?', (since_24h,)).fetchone()['cnt']
            own_variants_relay = [
                local_node_id or '',
                (local_node_id or '').lstrip('!'),
                '!' + (local_node_id or '').lstrip('!')
            ]
            last_byte_hex = ''
            if local_node_id:
                try:
                    last_byte_hex = f"relay_{int(local_node_id.replace('!',''), 16) & 0xFF:02x}"
                except Exception:
                    last_byte_hex = ''
            relayed = c.execute(
                '''SELECT COUNT(*) as cnt FROM packets
                   WHERE ts > ? AND (relay_node_id IN (?,?,?) OR relay_node_id = ?)''',
                (since_24h, *own_variants_relay, last_byte_hex)
            ).fetchone()['cnt']
            radio_total = c.execute(
                'SELECT COUNT(*) as cnt FROM packets WHERE ts > ? AND via_mqtt = 0',
                (since_24h,)).fetchone()['cnt']
            mqtt_total = c.execute(
                'SELECT COUNT(*) as cnt FROM packets WHERE ts > ? AND via_mqtt = 1',
                (since_24h,)).fetchone()['cnt']
            top_senders = c.execute('''SELECT from_id, MAX(from_name) as from_name, COUNT(*) as cnt,
                AVG(snr) as avg_snr, AVG(rssi) as avg_rssi, MAX(ts) as last_seen, portnum,
                (SELECT relay_node_id FROM packets p2
                 WHERE p2.from_id = packets.from_id AND p2.portnum = packets.portnum
                 AND p2.ts > ? AND p2.relay_node_id IS NOT NULL
                 AND p2.relay_node_id NOT LIKE 'relay_%'
                 ORDER BY p2.ts DESC LIMIT 1) as last_relay
                FROM packets WHERE ts > ? AND via_mqtt = 0
                GROUP BY from_id, portnum ORDER BY cnt DESC LIMIT 30''', (since_24h, since_24h)).fetchall()
            active_node_count = c.execute('''SELECT COUNT(DISTINCT from_id) as cnt
                FROM packets WHERE ts > ? AND via_mqtt = 0''', (since_24h,)).fetchone()['cnt']
            hourly = c.execute('''SELECT (ts/3600)*3600 as hour, COUNT(*) as cnt
                FROM packets WHERE ts > ?
                GROUP BY hour ORDER BY hour''', (since_24h,)).fetchall()
            anomalies = c.execute('''SELECT ts, node_id, node_name, anomaly_type, details, severity
                FROM anomalies WHERE ts > ? ORDER BY ts DESC LIMIT 50''', (since_24h,)).fetchall()
            by_type = c.execute('''SELECT portnum, COUNT(*) as cnt
                FROM packets WHERE ts > ?
                GROUP BY portnum ORDER BY cnt DESC''', (since_24h,)).fetchall()
            relay_flow = c.execute('''
                SELECT from_id, MAX(from_name) as from_name, COUNT(*) as cnt
                FROM packets
                WHERE ts > ?
                AND (
                    relay_node_id IN (?,?,?)
                    OR relay_node_id = ?
                )
                AND from_id NOT IN (?,?,?)
                GROUP BY from_id
                ORDER BY cnt DESC
                LIMIT 50
            ''', (since_24h,
                  *own_variants_relay,
                  last_byte_hex,
                  *own_variants_relay)).fetchall()

            # SNR distribution (5 dB buckets)
            snr_dist = c.execute('''SELECT CAST(ROUND(snr/5.0)*5 AS INTEGER) as bucket, COUNT(*) as cnt
                FROM packets WHERE ts > ? AND snr IS NOT NULL
                GROUP BY bucket ORDER BY bucket''', (since_24h,)).fetchall()

            # RSSI distribution (10 dB buckets)
            rssi_dist = c.execute('''SELECT CAST(ROUND(rssi/10.0)*10 AS INTEGER) as bucket, COUNT(*) as cnt
                FROM packets WHERE ts > ? AND rssi IS NOT NULL
                GROUP BY bucket ORDER BY bucket''', (since_24h,)).fetchall()

            # Hop count distribution
            hop_dist = c.execute('''SELECT hops, COUNT(*) as cnt
                FROM packets WHERE ts > ? AND hops IS NOT NULL AND hops >= 0
                GROUP BY hops ORDER BY hops''', (since_24h,)).fetchall()

            # 7-day daily trend
            daily_7d = c.execute('''SELECT (ts/86400)*86400 as day, COUNT(*) as cnt
                FROM packets WHERE ts > ?
                GROUP BY day ORDER BY day''', (since_7d,)).fetchall()

            # Data window: how long we've been recording within the 24h window
            first_ts = c.execute('SELECT MIN(ts) FROM packets WHERE ts > ?', (since_24h,)).fetchone()[0]
            data_window_minutes = int((now - first_ts) // 60) if first_ts else 0

            radio_history = c.execute('''
                SELECT ts, channel_utilization, air_util_tx,
                       num_packets_tx, num_packets_rx, num_packets_rx_bad,
                       num_rx_dupe, num_tx_relay
                FROM radio_stats_history
                WHERE ts > ?
                ORDER BY ts ASC
            ''', (since_24h,)).fetchall()

            conn.close()

            return {
                'total_packets': total,
                'radio_packets': radio_total,
                'mqtt_packets': mqtt_total,
                'relayed_packets': relayed,
                'relay_percentage': round(relayed / total * 100, 1) if total > 0 else 0,
                'top_senders': [dict(r) for r in top_senders],
                'active_node_count': active_node_count,
                'hourly_activity': [{'hour': r['hour'], 'count': r['cnt']} for r in hourly],
                'anomalies': [dict(r) for r in anomalies],
                'packet_types': [dict(r) for r in by_type],
                'relay_flow': [dict(r) for r in relay_flow],
                'snr_distribution': [{'bucket': r['bucket'], 'cnt': r['cnt']} for r in snr_dist],
                'rssi_distribution': [{'bucket': r['bucket'], 'cnt': r['cnt']} for r in rssi_dist],
                'hop_distribution': [{'hops': r['hops'], 'cnt': r['cnt']} for r in hop_dist],
                'daily_7d': [{'day': r['day'], 'count': r['cnt']} for r in daily_7d],
                'data_window_minutes': data_window_minutes,
                'radio_stats_history': [dict(r) for r in radio_history]
            }

    def get_topology_graph(self):
        """Get node connections for D3.js graph."""
        since = int(time.time()) - 86400
        with self.lock:
            conn = self._connect()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            nodes_data = c.execute('''SELECT DISTINCT from_id, MAX(from_name) as from_name, COUNT(*) as packet_count
                FROM packets WHERE ts > ? GROUP BY from_id''', (since,)).fetchall()
            conn.close()
            return [dict(r) for r in nodes_data]


def config_defaults():
    """Connection settings used when there is no config file to read."""
    return {'connection_type': 'serial', 'port': None, 'host': None}


def report_config_source():
    """Say which config file is in use, once, at startup.

    The live config lives in the web root (CONFIG_PATH) while config.json.example
    ships next to the source, so editing a copy in the repo is the natural
    mistake — and one that otherwise fails in complete silence: the app simply
    falls back to defaults and never mentions the file it did not read.
    """
    if os.path.exists(CONFIG_PATH):
        print(f"[CONFIG] Using {CONFIG_PATH}")
    else:
        print(f"[CONFIG] No {CONFIG_PATH} yet — starting with defaults (serial, auto-detect)")

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    stray = os.path.join(repo_root, 'config.json')
    if os.path.exists(stray) and os.path.realpath(stray) != os.path.realpath(CONFIG_PATH):
        print(f"[CONFIG] WARNING: {stray} exists but is NOT read by MeshPulse.")
        print(f"[CONFIG] WARNING: settings are only read from {CONFIG_PATH} — copy them there.")


def load_config():
    """Load connection config from CONFIG_PATH, falling back to defaults.

    A missing file is normal on a fresh install and stays quiet. A malformed one
    is reported every time: silently treating a broken edit as "no config" is how
    a wrong port survives three restarts while the logs say nothing.
    """
    try:
        with open(CONFIG_PATH, 'r') as f:
            config = json.load(f)
    except FileNotFoundError:
        config = config_defaults()
    except json.JSONDecodeError as e:
        print(f"[CONFIG] ERROR: {CONFIG_PATH} is not valid JSON ({e})")
        print("[CONFIG] ERROR: falling back to defaults — your settings are being ignored")
        config = config_defaults()
    except Exception as e:
        print(f"[CONFIG] ERROR reading {CONFIG_PATH}: {e} — falling back to defaults")
        config = config_defaults()

    if not isinstance(config, dict):
        print(f"[CONFIG] ERROR: {CONFIG_PATH} must hold a JSON object — falling back to defaults")
        config = config_defaults()
    config.setdefault('coverage_server_url', '')
    config.setdefault('coverage_api_key', '')
    config.setdefault('coverage_antenna_gain', 2.0)
    config.setdefault('coverage_antenna_height', 10.0)
    config.setdefault('coverage_max_range_km', 50)
    config.setdefault('plugin_store_url', 'https://meshpulse.app/plugins/plugins.json')
    config.setdefault('stats_retention_days', StatsDB.DEFAULT_RETENTION_DAYS)
    config.setdefault('remote_admin_write', False)
    return config


def _version_newer(a, b):
    try:
        return tuple(int(x) for x in a.split('.')) > \
               tuple(int(x) for x in b.split('.'))
    except Exception:
        return False


def _fetch_store_versions():
    """Return {plugin_id: version} from the plugin store."""
    import urllib.request
    cfg = load_config()
    store_url = cfg.get('plugin_store_url',
        'https://meshpulse.app/plugins/plugins.json')
    with urllib.request.urlopen(store_url, timeout=10) as r:
        store_data = json.loads(r.read().decode())
    return {p['id']: p['version'] for p in store_data.get('plugins', [])}


def compute_plugin_updates():
    """Which installed plugins are behind the last store snapshot.

    Derived on demand, not cached: the badge has to clear the moment a plugin
    is updated. Reads list_plugins() — a directory scan — rather than the
    loaded plugin instances, so frontend-only plugins are checked too.
    """
    if plugin_manager is None:
        return []
    store_versions = getattr(plugin_manager, '_store_versions', None) or {}
    if not store_versions:
        return []
    try:
        installed = plugin_manager.list_plugins()
    except Exception as e:
        print(f"[PLUGINS] Update comparison failed: {e}")
        return []
    updates = []
    for p in installed:
        local_ver = p.get('version', '0.0.0')
        store_ver = store_versions.get(p.get('id'))
        if store_ver and _version_newer(store_ver, local_ver):
            updates.append({'id': p['id'], 'local': local_ver,
                'available': store_ver})
    return updates


def _plugin_update_watcher():
    """Refresh the store snapshot 60 s after startup, then every 6 h.

    An instance that runs for months used to check exactly once and never hear
    about a new plugin release again.
    """
    import time as _time
    _time.sleep(60)
    while True:
        try:
            versions = _fetch_store_versions()
            if plugin_manager is not None:
                plugin_manager._store_versions = versions
                pending = compute_plugin_updates()
                if pending:
                    print(f"[PLUGINS] Updates available: {[u['id'] for u in pending]}")
        except Exception as e:
            print(f"[PLUGINS] Update check failed: {e}")
        _time.sleep(6 * 3600)


def save_config(connection_type, host=None, port=None):
    """Save connection config to JSON file"""
    config = {'connection_type': connection_type, 'host': host, 'port': port}
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, 'w') as f:
            json.dump(config, f, indent=2)
        # Owner-only: this file shares a directory with the served frontend and
        # can hold a broker host, credentials and the coverage API key. The web
        # server runs as its own user and has no business reading it.
        try:
            os.chmod(CONFIG_PATH, 0o600)
        except OSError:
            pass
        print(f"[CONFIG] Saved: {connection_type} {host or port or ''}")
    except Exception as e:
        print(f"[CONFIG] Save error: {e}")

class ListenBasedMapper:
    def __init__(self, connection_type='serial', port=None, host=None, max_age=86400):
        self.connection_type = connection_type
        self.port = port
        self.host = host
        self.current_process = None
        self._serial_iface = None
        self._tcp_iface = None
        self._cli_reboot_pending = False
        self._pending_traceroute_result = None
        self._traceroute_event = None      # asyncio.Event do wybudzania z callbacka
        self._traceroute_loop = None       # event loop do call_soon_threadsafe
        self._traceroute_expected_from = None  # node_id którego traceroute oczekujemy
        self.config = {}  # Populated with load_config() after construction
        self.json_path = '/var/www/html/meshpulse/nodes.json'
        self.meshtastic_cmd = shutil.which('meshtastic') or os.path.expanduser('~/.local/bin/meshtastic')
        self.max_age = max_age

        # Load existing nodes or start fresh
        self._loaded_radio_stats = None  # Populated by load_existing_nodes if available
        self.nodes_no_position = {}  # Nodes without GPS position
        self.ignored_nodes = {}  # node_id -> {'id': node_id, 'name': name, 'ts': timestamp}
        self.nodes = self.load_existing_nodes()
        self.local_node_id = self.get_local_node_id()

        # Restore radio_stats from previous run (live data will overwrite once received)
        if self._loaded_radio_stats and 'radio_stats' not in self.tracker_info:
            self.tracker_info['radio_stats'] = self._loaded_radio_stats
            print(f"[LOAD] Restored radio_stats from previous run")

        # Store text messages per channel (dict: {channel_index: [messages]})
        self.messages = getattr(self, '_loaded_messages', {})

        # Cache of all known node names from nodeinfo packets
        self.known_names = getattr(self, '_loaded_known_names', {})

        # Stats database
        self.stats_db = StatsDB()
        self._last_packet_times = {}  # for anomaly detection
        self._last_radio_packet_time = time.time()  # watchdog: last time we got any packet from radio
        self._packet_event_queue = collections.deque(maxlen=50)

        # Stats cache
        self._stats_cache = None
        self._stats_cache_ts = 0
        self._stats_cache_lock = threading.Lock()
        self._stats_computing = False
        t = threading.Thread(target=self._stats_refresh_loop, daemon=True)
        t.start()

        # Telemetry
        self._start_time = time.time()
        self._last_telemetry_ping = 0

        # Broadcast connection status to any already-connected WS clients
        if self.local_node_id:
            asyncio.run(self.broadcast_connection_status('connected'))
        else:
            asyncio.run(self.broadcast_connection_status('failed', f'Could not connect via {self.connection_type}'))

        # Note: do NOT save here - listener hasn't started yet, nodes are empty
    
    def get_local_node_id(self):
        """Get local node info using meshtastic --info"""
        try:
            print("[INFO] Getting local node info...")
            if self.connection_type == 'tcp':
                cmd_info = [self.meshtastic_cmd, '--host', self.host, '--info']
            else:
                cmd_info = [self.meshtastic_cmd, '--port', self.port, '--info']
            result = subprocess.run(
                cmd_info,
                capture_output=True,
                text=True,
                timeout=60
            )
            
            output = result.stdout
            
            # Parse myNodeNum
            match = re.search(r'"myNodeNum":\s*(\d+)', output)
            if match:
                node_num = int(match.group(1))
                node_id = f"!{node_num:08x}"
            else:
                node_id = None
            
            # Parse hwModel
            hw_match = re.search(r'"hwModel":\s*"([^"]+)"', output)
            hw_model = hw_match.group(1) if hw_match else "Unknown"
            
            # Parse firmwareVersion
            fw_match = re.search(r'"firmwareVersion":\s*"([^"]+)"', output)
            firmware = fw_match.group(1) if fw_match else "Unknown"
            
            # Parse channel names
            # Join wrapped lines (meshtastic CLI wraps long lines)
            output_joined = re.sub(r'\n\s*"', '"', output)
            channels = []
            for line in output_joined.split('\n'):
                idx_match = re.match(r'\s*Index (\d+): \w+ psk=\w+', line)
                if idx_match:
                    index = int(idx_match.group(1))
                    name_match = re.search(r'"name":\s*"([^"]*)"', line)
                    name = name_match.group(1).strip() if name_match else ''
                    name = 'Primary' if (index == 0 and not name) else (name if name else f'Channel {index}')
                    channels.append({'index': index, 'name': name})

            # Store tracker info
            self.tracker_info = {
                'node_id': node_id,
                'connection_type': self.connection_type,
                'port': self.port,
                'host': self.host,
                'hw_model': hw_model,
                'firmware': firmware,
                'channels': channels,
                'tx_power': 27
            }

            print(f"[INFO] Local node ID: {node_id}")
            print(f"[INFO] Hardware: {hw_model}, Firmware: {firmware}")
            print(f"[INFO] Channels: {[(c['index'], c['name']) for c in channels]}")

            return node_id
            
        except subprocess.TimeoutExpired:
            print("[WARN] Timeout getting local node info")
        except Exception as e:
            print(f"[WARN] Error getting local node info: {e}")
        
        self.tracker_info = {
            'node_id': None,
            'connection_type': self.connection_type,
            'port': self.port,
            'host': self.host,
            'hw_model': 'Unknown',
            'firmware': 'Unknown',
            'tx_power': 27
        }
        return None

    def calculate_distance(self, lat1, lon1, lat2, lon2):
        """Calculate distance between two points using Haversine formula (returns km)"""
        import math
        R = 6371  # Earth radius in km
        
        lat1_rad = math.radians(lat1)
        lat2_rad = math.radians(lat2)
        delta_lat = math.radians(lat2 - lat1)
        delta_lon = math.radians(lon2 - lon1)
        
        a = math.sin(delta_lat/2)**2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lon/2)**2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
        
        return R * c
    
    def notify_plugins_connected(self):
        """Tell plugins the radio link is up and usable.

        on_connect has been part of the documented plugin API all along but was
        never dispatched, so a plugin that needs the radio had no choice but to
        do its setup in on_enable — which runs while the interface is still
        connecting. MQTT Proxy failed there on every cold start, reading a
        tracker config that did not exist yet and reporting it as a user error.

        Dispatched after set_interface(), so get_tracker_config() works by the
        time a plugin's hook runs. Fires again after each reconnect: a plugin
        that lost the radio gets to set itself up once it is back.
        """
        if not plugin_manager:
            return
        try:
            plugin_manager.dispatch_hook_sync('on_connect', {
                'connection_type': self.connection_type,
                'host_or_port': self.host or self.port or '',
                'local_node_id': self.local_node_id,
            })
        except Exception as e:
            print(f"[PLUGINS] on_connect dispatch error: {e}")

    def notify_plugins_disconnected(self, reason):
        """Tell plugins the radio link is gone. Counterpart of on_connect."""
        if not plugin_manager:
            return
        try:
            plugin_manager.dispatch_hook_sync('on_disconnect', {'reason': reason})
        except Exception as e:
            print(f"[PLUGINS] on_disconnect dispatch error: {e}")

    def get_max_distance(self):
        """Find maximum distance to directly reachable node (hops=0)"""
        if not self.local_node_id or self.local_node_id not in self.nodes:
            return None, None
        
        local = self.nodes[self.local_node_id]
        local_lat = local.get('lat')
        local_lon = local.get('lon')
        
        if not local_lat or not local_lon:
            return None, None
        
        max_dist = 0
        farthest_id = None
        
        for node_id, node in self.nodes.items():
            if node_id == self.local_node_id:
                continue
            if node.get('hops') is None or node.get('hops') != 0:
                continue
            if node.get('via_mqtt', False):
                continue
            
            lat = node.get('lat')
            lon = node.get('lon')
            if not lat or not lon:
                continue
            
            dist = self.calculate_distance(local_lat, local_lon, lat, lon)
            if dist > max_dist:
                max_dist = dist
                farthest_id = node_id
        
        return round(max_dist, 2), farthest_id 

    # ── Per-network range ───────────────────────────────────────
    # Networks other than Meshtastic reach the map through plugin inject_node().
    # Two optional conventions make them measurable the same way:
    #   net_self  — marks that network's own local node (its point of origin)
    #   hops_away — radio hops from that local node; 0 = direct neighbour
    # Meshtastic uses its native 'hops'. A node without the field is "unknown",
    # never "direct", so an unmeasurable network simply gets no row.

    NET_LABELS = {'MT': 'Meshtastic', 'MC': 'Meshcore'}

    @staticmethod
    def net_of(node):
        """Network tag of a node. Absent/empty means Meshtastic."""
        return (node.get('net') or 'MT').upper()

    def local_node_id_for_net(self, net):
        """The node a given network measures distances from."""
        if net == 'MT':
            return self.local_node_id
        for node_id, node in self.nodes.items():
            # 'mc_self' is what meshcore plugin <= 1.0.0 sends; keep accepting it
            if self.net_of(node) == net and (node.get('net_self') or node.get('mc_self')):
                return node_id
        return None

    def get_max_distance_for_net(self, net):
        """Max distance to a node this network hears with zero radio hops,
        measured from that network's own local node. (km, node_id) or (None, None)."""
        local_id = self.local_node_id_for_net(net)
        if not local_id or local_id not in self.nodes:
            return None, None

        local = self.nodes[local_id]
        local_lat, local_lon = local.get('lat'), local.get('lon')
        if not local_lat or not local_lon:
            return None, None

        max_dist = 0
        farthest_id = None

        for node_id, node in self.nodes.items():
            if node_id == local_id or self.net_of(node) != net:
                continue
            hops = node.get('hops') if net == 'MT' else node.get('hops_away')
            if hops != 0:
                continue
            if net == 'MT' and node.get('via_mqtt', False):
                continue

            lat, lon = node.get('lat'), node.get('lon')
            if not lat or not lon:
                continue

            dist = self.calculate_distance(local_lat, local_lon, lat, lon)
            if dist > max_dist:
                max_dist = dist
                farthest_id = node_id

        if not farthest_id:
            return None, None
        return round(max_dist, 2), farthest_id

    def get_network_reach_for_net(self, net):
        """Distance to the farthest node of a network that is known at all.

        Deliberately not a radio measurement: it counts nodes reached through any
        number of repeaters. Meshcore needs this because its companion API cannot
        report whether an advert arrived directly — every contact looks the same —
        so the honest thing to show is how far the network extends, under a name
        that does not claim otherwise. MQTT-sourced nodes stay excluded: those
        arrive over the internet and would measure nothing about the mesh.

        Returns:
            tuple: (km, node_id) or (None, None).
        """
        local_id = self.local_node_id_for_net(net)
        if not local_id or local_id not in self.nodes:
            return None, None

        local = self.nodes[local_id]
        local_lat, local_lon = local.get('lat'), local.get('lon')
        if not local_lat or not local_lon:
            return None, None

        max_dist = 0
        farthest_id = None

        for node_id, node in self.nodes.items():
            if node_id == local_id or self.net_of(node) != net:
                continue
            if node.get('via_mqtt', False):
                continue

            lat, lon = node.get('lat'), node.get('lon')
            if not lat or not lon:
                continue

            dist = self.calculate_distance(local_lat, local_lon, lat, lon)
            if dist > max_dist:
                max_dist = dist
                farthest_id = node_id

        if not farthest_id:
            return None, None
        return round(max_dist, 2), farthest_id

    def _net_entry(self, net, dist, farthest_id):
        """Shape one per-network row for the UI."""
        farthest = self.nodes.get(farthest_id) or {}
        return {
            'label': self.NET_LABELS.get(net, net),
            'km': dist,
            'node_id': farthest_id,
            'node_name': farthest.get('name') or farthest_id,
            'from_id': self.local_node_id_for_net(net),
        }

    def get_network_reach_by_net(self):
        """Per-network reach — farthest known node, any number of hops."""
        result = {}
        for net in sorted({self.net_of(n) for n in self.nodes.values()}):
            dist, farthest_id = self.get_network_reach_for_net(net)
            if dist is not None:
                result[net] = self._net_entry(net, dist, farthest_id)
        return result

    def get_max_distance_by_net(self):
        """Per-network max range for the Mesh Info panel. Only networks that have
        a positioned local node AND at least one direct neighbour show up."""
        result = {}
        for net in sorted({self.net_of(n) for n in self.nodes.values()}):
            dist, farthest_id = self.get_max_distance_for_net(net)
            if dist is None:
                continue
            result[net] = self._net_entry(net, dist, farthest_id)
        return result

    def load_existing_nodes(self):
        """Load nodes from existing JSON file"""
        try:
            if os.path.exists(self.json_path):
                with open(self.json_path, 'r') as f:
                    data = json.load(f)
                    existing_count = data.get('cnt', 0)
                    existing_no_pos = data.get('cnt_no_pos', 0)
                    if existing_count > 0 or existing_no_pos > 0:
                        print(f"[LOAD] Found {existing_count} nodes + {existing_no_pos} no-GPS from previous run")
                        # Convert lists to dicts with id as key
                        nodes = {node['id']: node for node in data.get('nodes', [])}
                        nodes_no_pos = {node['id']: node for node in data.get('nodes_no_pos', [])}
                        for n in nodes.values(): n['source'] = 'memory'
                        for n in nodes_no_pos.values(): n['source'] = 'memory'
                    
                        # Clean old nodes immediately
                        self.clean_old_nodes_from_dict(nodes)
                        self.clean_old_nodes_from_dict(nodes_no_pos)

                        # A node saved before the Null Island filter existed keeps
                        # its bogus coordinates through restarts, so re-check here
                        # too: strip the position and move it to the no-GPS list,
                        # which is what it should have been all along.
                        bogus = [nid for nid, n in nodes.items()
                                 if is_placeholder_position(n.get('lat'), n.get('lon'))]
                        for nid in bogus:
                            node = nodes.pop(nid)
                            node.pop('lat', None)
                            node.pop('lon', None)
                            nodes_no_pos.setdefault(nid, node)
                        if bogus:
                            print(f"[LOAD] Dropped bogus (0,0)-area position from {len(bogus)} node(s): "
                                  + ', '.join(sorted(bogus)[:5]))

                        # Remove from no-position any node that already has GPS position
                        duplicates = [nid for nid in nodes_no_pos if nid in nodes]
                        for nid in duplicates:
                            del nodes_no_pos[nid]
                        if duplicates:
                            print(f"[LOAD] Removed {len(duplicates)} duplicate nodes from no-GPS list")

                        # Store no-GPS nodes
                        self.nodes_no_position = nodes_no_pos

                        # Restore names from known_names cache if node name = node_id
                        known = data.get('known_names', {})
                        for node_id, node in nodes.items():
                            if node.get('name') == node_id and node_id in known:
                                node['name'] = known[node_id]
                        for node_id, node in nodes_no_pos.items():
                            if node.get('name') == node_id and node_id in known:
                                node['name'] = known[node_id]

                        loaded_msgs = data.get('messages', {})
                        if isinstance(loaded_msgs, list):
                            self._loaded_messages = {0: loaded_msgs} if loaded_msgs else {}
                        elif isinstance(loaded_msgs, dict):
                            self._loaded_messages = {int(k): v for k, v in loaded_msgs.items()}
                        else:
                            self._loaded_messages = {}
                        total_msgs = sum(len(v) for v in self._loaded_messages.values())
                        print(f"[LOAD] Loaded {len(nodes)} nodes + {len(nodes_no_pos)} no-GPS after cleanup, {total_msgs} messages")

                    self._loaded_known_names = data.get('known_names', {})
                    self.ignored_nodes = {n['id']: n for n in data.get('ignored_nodes', [])}

                    # Always restore radio_stats regardless of node count
                    saved_radio_stats = data.get('tracker', {}).get('radio_stats')
                    if saved_radio_stats:
                        self._loaded_radio_stats = saved_radio_stats

                    return nodes if (existing_count > 0 or existing_no_pos > 0) else {}
        except Exception as e:
            import traceback
            print(f"[LOAD] Starting fresh (no existing data): {e}")
            traceback.print_exc()

        return {}
    
    def clean_old_nodes_from_dict(self, nodes_dict):
        """Remove nodes older than max_age seconds"""
        now = int(time.time())
        removed = []

        for node_id, node in list(nodes_dict.items()):
            age = now - node.get('ts', now)
            if age > self.max_age:
                removed.append(node_id)
                del nodes_dict[node_id]
                # Broadcast deletion to WebSocket clients
                # Broadcast deletion to WebSocket clients (only if nodes is already initialized)
                if hasattr(self, 'nodes'):
                    asyncio.run(self.broadcast_node_deleted(node_id))

        if removed:
            hours = self.max_age // 3600
            print(f"[CLEAN] Removed {len(removed)} old nodes (>{hours}h old)")
            for node_id in removed[:5]:  # Show first 5
                print(f"  - {node_id}")
            # Remove expired nodes from name cache — only if fully initialized
            if hasattr(self, 'nodes') and hasattr(self, 'nodes_no_position') and hasattr(self, 'known_names'):
                for node_id in removed:
                    if node_id not in self.nodes and node_id not in self.nodes_no_position:
                        self.known_names.pop(node_id, None)
    
    def get_device_config(self):
        """Read current config from connected device via Python API."""
        iface = None
        if self.connection_type == 'serial' and self._serial_iface:
            iface = self._serial_iface.iface
        elif self.connection_type == 'tcp' and self._tcp_iface:
            iface = self._tcp_iface

        if not iface:
            raise Exception('No active connection to device')

        # For TCP, check that the underlying interface is actually connected
        if self.connection_type == 'tcp' and hasattr(iface, 'interface') and iface.interface is None:
            raise Exception('TCP connection not yet established — please wait and retry')

        node = iface.localNode
        config = {}

        try:
            d = node.localConfig.device
            config['device'] = {
                'role': d.role,
                'node_info_broadcast_secs': d.node_info_broadcast_secs,
                'serial_enabled': d.serial_enabled,
            }
        except Exception as e:
            config['device'] = {'error': str(e)}

        try:
            p = node.localConfig.position
            config['position'] = {
                'position_broadcast_secs': p.position_broadcast_secs,
                'gps_enabled': p.gps_mode != 0,
                'gps_update_interval': p.gps_update_interval,
                'position_broadcast_smart_enabled': p.position_broadcast_smart_enabled,
            }
        except Exception as e:
            config['position'] = {'error': str(e)}

        try:
            l = node.localConfig.lora
            config['lora'] = {
                'region': l.region,
                'region_name': config_pb2.Config.LoRaConfig.RegionCode.Name(l.region) if l.region else 'UNSET',
                'hop_limit': l.hop_limit,
                'modem_preset': l.modem_preset,
                'tx_power': l.tx_power,
                'use_preset': l.use_preset,
            }
        except Exception as e:
            config['lora'] = {'error': str(e)}

        try:
            t = node.moduleConfig.telemetry
            config['telemetry'] = {
                'device_update_interval': t.device_update_interval,
                'environment_update_interval': t.environment_update_interval,
                'environment_measurement_enabled': t.environment_measurement_enabled,
            }
        except Exception as e:
            config['telemetry'] = {'error': str(e)}

        try:
            # Network config
            n = node.localConfig.network
            config['network'] = {
                'wifi_enabled': n.wifi_enabled,
                'wifi_ssid': n.wifi_ssid,
                'wifi_psk': n.wifi_psk,
                'address_mode': n.address_mode,
                'ntp_server': n.ntp_server,
            }
        except Exception as e:
            config['network'] = {'error': str(e)}

        try:
            # Add connection info to network config
            config['network']['current_ip'] = self.config.get('host', None) if self.connection_type == 'tcp' else None
            config['network']['connection_type'] = self.connection_type
        except:
            pass

        try:
            # Bluetooth config
            b = node.localConfig.bluetooth
            config['bluetooth'] = {
                'enabled': b.enabled,
                'mode': b.mode,
                'fixed_pin': b.fixed_pin,
            }
        except Exception as e:
            config['bluetooth'] = {'error': str(e)}

        try:
            ni = node.moduleConfig.neighbor_info
            config['neighborinfo'] = {
                'neighbor_info_enabled': ni.enabled,
                'update_interval': ni.update_interval,
            }
        except:
            config['neighborinfo'] = {'neighbor_info_enabled': False, 'update_interval': 14400}

        try:
            m = node.moduleConfig.mqtt
            mrs = getattr(m, 'map_report_settings', None)
            config['mqtt'] = {
                'enabled': getattr(m, 'enabled', False),
                'address': getattr(m, 'address', ''),
                'username': getattr(m, 'username', ''),
                'password': getattr(m, 'password', ''),
                'encryption_enabled': getattr(m, 'encryption_enabled', True),
                'json_enabled': getattr(m, 'json_enabled', False),
                'tls_enabled': getattr(m, 'tls_enabled', False),
                'proxy_to_client_enabled': getattr(m, 'proxy_to_client_enabled', False),
                'okay_to_mqtt': getattr(m, 'okay_to_mqtt', True),
                'root': getattr(m, 'root', 'msh'),
                'map_reporting_enabled': mrs.publish_interval_secs > 0 if mrs else False,
                'map_publish_interval_secs': getattr(mrs, 'publish_interval_secs', 3600) if mrs else 3600,
                'position_precision': getattr(mrs, 'position_precision', 32) if mrs else 32,
            }
        except Exception as e:
            config['mqtt'] = {'error': str(e)}

        try:
            p = node.localConfig.power
            config['power'] = {
                'is_power_saving': getattr(p, 'is_power_saving', False),
                'light_sleep_enabled': getattr(p, 'light_sleep_enabled', False),
                'wait_bluetooth_secs': getattr(p, 'wait_bluetooth_secs', 60),
                'min_wake_secs': getattr(p, 'min_wake_secs', 10),
                'sds_secs': getattr(p, 'sds_secs', 0),
                'adc_multiplier_override': getattr(p, 'adc_multiplier_override', 0),
            }
        except Exception as e:
            config['power'] = {
                'is_power_saving': False,
                'light_sleep_enabled': False,
                'wait_bluetooth_secs': 60,
                'min_wake_secs': 10,
                'sds_secs': 0,
                'adc_multiplier_override': 0,
                'error': str(e)
            }

        try:
            d = node.localConfig.display
            config['display'] = {
                'screen_on_secs': getattr(d, 'screen_on_secs', 0),
                'gps_format': getattr(d, 'gps_format', 0),
                'auto_screen_carousel_secs': getattr(d, 'auto_screen_carousel_secs', 0),
                'compass_north_top': getattr(d, 'compass_north_top', False),
                'wake_on_tap_or_motion': getattr(d, 'wake_on_tap_or_motion', False),
                'flip_screen': getattr(d, 'flip_screen', False),
                'units': getattr(d, 'units', 0),
                'oled': getattr(d, 'oled', 0),
                'displaymode': getattr(d, 'displaymode', 0),
                'heading_bold': getattr(d, 'heading_bold', False),
            }
        except Exception as e:
            config['display'] = {
                'screen_on_secs': 0, 'gps_format': 0, 'auto_screen_carousel_secs': 0,
                'compass_north_top': False, 'wake_on_tap_or_motion': False, 'flip_screen': False,
                'units': 0, 'oled': 0, 'displaymode': 0, 'heading_bold': False, 'error': str(e)
            }

        try:
            sf = node.moduleConfig.store_forward
            config['store_forward'] = {
                'enabled': getattr(sf, 'enabled', False),
                'heartbeat': getattr(sf, 'heartbeat', False),
                'records': getattr(sf, 'records', 0),
                'history_return_max': getattr(sf, 'history_return_max', 0),
                'history_return_window': getattr(sf, 'history_return_window', 0),
            }
        except Exception as e:
            config['store_forward'] = {'enabled': False, 'heartbeat': False, 'records': 0, 'history_return_max': 0, 'history_return_window': 0, 'error': str(e)}

        try:
            en = node.moduleConfig.external_notification
            config['ext_notification'] = {
                'enabled': getattr(en, 'enabled', False),
                'active': getattr(en, 'active', False),
                'alert_message': getattr(en, 'alert_message', False),
                'alert_message_buzzer': getattr(en, 'alert_message_buzzer', False),
                'alert_message_vibra': getattr(en, 'alert_message_vibra', False),
                'alert_bell': getattr(en, 'alert_bell', False),
                'alert_bell_buzzer': getattr(en, 'alert_bell_buzzer', False),
                'alert_bell_vibra': getattr(en, 'alert_bell_vibra', False),
                'output': getattr(en, 'output', 0),
                'output_buzzer': getattr(en, 'output_buzzer', 0),
                'output_vibra': getattr(en, 'output_vibra', 0),
                'output_ms': getattr(en, 'output_ms', 0),
                'nag_timeout': getattr(en, 'nag_timeout', 0),
                'use_pwm': getattr(en, 'use_pwm', False),
                'use_i2s_as_buzzer': getattr(en, 'use_i2s_as_buzzer', False),
            }
        except Exception as e:
            config['ext_notification'] = {
                'enabled': False, 'active': False,
                'alert_message': False, 'alert_message_buzzer': False, 'alert_message_vibra': False,
                'alert_bell': False, 'alert_bell_buzzer': False, 'alert_bell_vibra': False,
                'output': 0, 'output_buzzer': 0, 'output_vibra': 0,
                'output_ms': 0, 'nag_timeout': 0,
                'use_pwm': False, 'use_i2s_as_buzzer': False,
                'error': str(e)
            }

        try:
            rt = node.moduleConfig.range_test
            config['range_test'] = {
                'enabled': getattr(rt, 'enabled', False),
                'sender': getattr(rt, 'sender', 0),
                'save': getattr(rt, 'save', False),
            }
        except Exception as e:
            config['range_test'] = {'enabled': False, 'sender': 0, 'save': False, 'error': str(e)}

        try:
            cm = node.moduleConfig.canned_message
            config['canned_message'] = {
                'enabled': getattr(cm, 'enabled', False),
                'send_bell': getattr(cm, 'send_bell', False),
                'messages': getattr(cm, 'messages', ''),
                'allow_input_source': getattr(cm, 'allow_input_source', ''),
            }
        except Exception as e:
            config['canned_message'] = {'enabled': False, 'send_bell': False, 'messages': '', 'allow_input_source': '', 'error': str(e)}

        try:
            px = node.moduleConfig.paxcounter
            config['paxcounter'] = {
                'enabled': getattr(px, 'enabled', False),
                'paxcounter_update_interval': getattr(px, 'paxcounter_update_interval', 0),
            }
        except Exception as e:
            config['paxcounter'] = {'enabled': False, 'paxcounter_update_interval': 0, 'error': str(e)}

        try:
            sr = node.moduleConfig.serial
            config['serial_module'] = {
                'enabled': getattr(sr, 'enabled', False),
                'echo': getattr(sr, 'echo', False),
                'rxd': getattr(sr, 'rxd', 0),
                'txd': getattr(sr, 'txd', 0),
                'baud': getattr(sr, 'baud', 0),
                'timeout': getattr(sr, 'timeout', 0),
                'mode': getattr(sr, 'mode', 0),
                'override_console_serial_port': getattr(sr, 'override_console_serial_port', False),
            }
        except Exception as e:
            config['serial_module'] = {'enabled': False, 'echo': False, 'rxd': 0, 'txd': 0, 'baud': 0, 'timeout': 0, 'mode': 0, 'override_console_serial_port': False, 'error': str(e)}

        try:
            # Channels
            from meshtastic.protobuf import channel_pb2
            channels = []
            for ch in node.channels:
                channels.append({
                    'index': ch.index,
                    'name': ch.settings.name,
                    'role': ch.role,
                    'role_name': channel_pb2.Channel.Role.Name(ch.role),
                    'psk': base64.b64encode(ch.settings.psk).decode('utf-8') if ch.settings.psk else '',
                    'uplink_enabled': getattr(ch.settings, 'uplink_enabled', False),
                    'downlink_enabled': getattr(ch.settings, 'downlink_enabled', False),
                })
            config['channels'] = channels
        except Exception as e:
            config['channels'] = []

        try:
            # Extra device fields
            d = node.localConfig.device
            config['device']['rebroadcast_mode'] = d.rebroadcast_mode
            config['device']['serial_enabled'] = d.serial_enabled
            config['device']['led_heartbeat_disabled'] = d.led_heartbeat_disabled
        except:
            pass

        try:
            # Extra position fields
            p = node.localConfig.position
            config['position']['broadcast_smart_minimum_distance'] = p.broadcast_smart_minimum_distance
            config['position']['broadcast_smart_minimum_interval_secs'] = p.broadcast_smart_minimum_interval_secs
        except:
            pass

        try:
            # Extra telemetry fields
            t = node.moduleConfig.telemetry
            config['telemetry']['device_telemetry_enabled'] = t.device_telemetry_enabled
            config['telemetry']['power_measurement_enabled'] = t.power_measurement_enabled
            config['telemetry']['power_update_interval'] = t.power_update_interval
            config['telemetry']['environment_screen_enabled'] = t.environment_screen_enabled
            config['telemetry']['health_measurement_enabled'] = t.health_measurement_enabled
            config['telemetry']['health_update_interval'] = t.health_update_interval
        except:
            pass

        try:
            # Current position for fixed position pre-fill
            my_info = iface.getMyNodeInfo()
            pos = my_info.get('position', {})
            if pos:
                config['current_position'] = {
                    'lat': pos.get('latitude', 0),
                    'lon': pos.get('longitude', 0),
                    'alt': pos.get('altitude', 0),
                }
        except:
            pass

        try:
            u = iface.getMyNodeInfo().get('user', {})
            config['user'] = {
                'long_name': u.get('longName', ''),
                'short_name': u.get('shortName', ''),
            }
        except Exception as e:
            config['user'] = {'error': str(e)}

        config['coverage'] = {
            'server_url': self.config.get('coverage_server_url', ''),
            'api_key': self.config.get('coverage_api_key', ''),
            'antenna_gain': self.config.get('coverage_antenna_gain', 2.0),
            'antenna_height': self.config.get('coverage_antenna_height', 10.0),
            'max_range_km': self.config.get('coverage_max_range_km', 50),
        }

        return config

    def apply_device_config(self, changes, reboot=False):
        """Apply config changes to device via Python API."""
        iface = None
        if self.connection_type == 'serial' and self._serial_iface:
            iface = self._serial_iface.iface
        elif self.connection_type == 'tcp' and self._tcp_iface:
            iface = self._tcp_iface

        if not iface:
            raise Exception('No active connection to device')

        if self.connection_type == 'tcp' and hasattr(iface, 'interface') and iface.interface is None:
            raise Exception('TCP connection not yet established — please wait and retry')

        node = iface.localNode
        applied = []

        if 'user' in changes:
            u = changes['user']
            if 'long_name' in u:
                node.setOwner(longName=u['long_name'], shortName=u.get('short_name'))
                applied.append('user.long_name')
            elif 'short_name' in u:
                node.setOwner(shortName=u['short_name'])
                applied.append('user.short_name')

        if 'device' in changes:
            for key, val in changes['device'].items():
                if key == 'role' or key == 'rebroadcast_mode':
                    setattr(node.localConfig.device, key, int(val))
                else:
                    setattr(node.localConfig.device, key, val)
                applied.append(f'device.{key}')
            node.writeConfig('device')

        if 'position' in changes:
            for key, val in changes['position'].items():
                if key == 'gps_enabled':
                    node.localConfig.position.gps_mode = 1 if val else 0
                    applied.append('position.gps_mode')
                else:
                    setattr(node.localConfig.position, key, val)
                    applied.append(f'position.{key}')
            node.writeConfig('position')

        if 'lora' in changes:
            for key, val in changes['lora'].items():
                if key in ('region', 'modem_preset'):
                    setattr(node.localConfig.lora, key, int(val))
                else:
                    setattr(node.localConfig.lora, key, val)
                applied.append(f'lora.{key}')
            node.writeConfig('lora')

        if 'telemetry' in changes:
            for key, val in changes['telemetry'].items():
                setattr(node.moduleConfig.telemetry, key, val)
                applied.append(f'telemetry.{key}')
            node.writeConfig('telemetry')

        # Network config changes
        if 'network' in changes:
            for key, val in changes['network'].items():
                if not key.startswith('ipv4_config'):
                    if key == 'address_mode':
                        setattr(node.localConfig.network, key, int(val))
                    else:
                        setattr(node.localConfig.network, key, val)
                    applied.append(f'network.{key}')
            node.writeConfig('network')

        # Bluetooth config changes
        if 'bluetooth' in changes:
            for key, val in changes['bluetooth'].items():
                if key == 'mode':
                    setattr(node.localConfig.bluetooth, key, int(val))
                else:
                    setattr(node.localConfig.bluetooth, key, val)
                applied.append(f'bluetooth.{key}')
            node.writeConfig('bluetooth')

        if 'neighborinfo' in changes:
            for key, val in changes['neighborinfo'].items():
                if key == 'neighbor_info_enabled':
                    node.moduleConfig.neighbor_info.enabled = bool(val)
                elif key == 'update_interval':
                    node.moduleConfig.neighbor_info.update_interval = int(val)
            node.writeConfig('neighbor_info')
            applied.append('neighborinfo')

        if 'mqtt' in changes:
            m = node.moduleConfig.mqtt
            mqtt_changes = changes['mqtt']
            simple_fields = ['enabled', 'address', 'username', 'password',
                             'encryption_enabled', 'json_enabled', 'tls_enabled',
                             'proxy_to_client_enabled', 'root', 'okay_to_mqtt']
            for key in simple_fields:
                if key in mqtt_changes:
                    setattr(m, key, mqtt_changes[key])
            try:
                if 'map_reporting_enabled' in mqtt_changes or 'map_publish_interval_secs' in mqtt_changes:
                    if hasattr(m, 'map_report_settings'):
                        if mqtt_changes.get('map_reporting_enabled'):
                            m.map_report_settings.publish_interval_secs = int(mqtt_changes.get('map_publish_interval_secs', 3600))
                        else:
                            m.map_report_settings.publish_interval_secs = 0
                if 'position_precision' in mqtt_changes:
                    if hasattr(m, 'map_report_settings'):
                        m.map_report_settings.position_precision = int(mqtt_changes['position_precision'])
            except Exception as e:
                print(f"[CONFIG] map_report_settings not supported: {e}")
            time.sleep(0.5)   # Allow protobuf message to be queued
            node.writeConfig('mqtt')
            time.sleep(2.5)   # Wait for device to ACK and begin flash write before reboot
            applied.append('mqtt')

        if 'power' in changes:
            p = node.localConfig.power
            for key, val in changes['power'].items():
                if key == 'adc_multiplier_override':
                    setattr(p, key, float(val))
                elif key in ('is_power_saving', 'light_sleep_enabled'):
                    setattr(p, key, bool(val))
                else:
                    setattr(p, key, int(val))
            node.writeConfig('power')
            applied.append('power')

        if 'display' in changes:
            d = node.localConfig.display
            for key, val in changes['display'].items():
                if key in ('gps_format', 'units', 'oled', 'displaymode'):
                    setattr(d, key, int(val))
                elif key in ('compass_north_top', 'wake_on_tap_or_motion', 'flip_screen', 'heading_bold'):
                    setattr(d, key, bool(val))
                else:
                    setattr(d, key, int(val))
            node.writeConfig('display')
            applied.append('display')

        if 'store_forward' in changes:
            sf = node.moduleConfig.store_forward
            for key, val in changes['store_forward'].items():
                if key in ('enabled', 'heartbeat'):
                    setattr(sf, key, bool(val))
                else:
                    setattr(sf, key, int(val))
            node.writeConfig('store_forward')
            applied.append('store_forward')

        if 'ext_notification' in changes:
            en = node.moduleConfig.external_notification
            bool_fields = ('enabled', 'active', 'alert_message', 'alert_bell',
                           'alert_message_buzzer', 'alert_message_vibra',
                           'alert_bell_buzzer', 'alert_bell_vibra',
                           'use_pwm', 'use_i2s_as_buzzer')
            for key, val in changes['ext_notification'].items():
                if key in bool_fields:
                    setattr(en, key, bool(val))
                else:
                    setattr(en, key, int(val))
            node.writeConfig('external_notification')
            applied.append('ext_notification')

        if 'range_test' in changes:
            rt = node.moduleConfig.range_test
            for key, val in changes['range_test'].items():
                if key in ('enabled', 'save'):
                    setattr(rt, key, bool(val))
                else:
                    setattr(rt, key, int(val))
            node.writeConfig('range_test')
            applied.append('range_test')

        if 'canned_message' in changes:
            cm = node.moduleConfig.canned_message
            for key, val in changes['canned_message'].items():
                if key in ('enabled', 'send_bell'):
                    setattr(cm, key, bool(val))
                else:
                    setattr(cm, key, str(val))
            node.writeConfig('canned_message')
            applied.append('canned_message')

        if 'paxcounter' in changes:
            px = node.moduleConfig.paxcounter
            for key, val in changes['paxcounter'].items():
                if key == 'enabled':
                    setattr(px, key, bool(val))
                else:
                    setattr(px, key, int(val))
            node.writeConfig('paxcounter')
            applied.append('paxcounter')

        if 'serial_module' in changes:
            sr = node.moduleConfig.serial
            for key, val in changes['serial_module'].items():
                if key in ('enabled', 'echo', 'override_console_serial_port'):
                    setattr(sr, key, bool(val))
                else:
                    setattr(sr, key, int(val))
            node.writeConfig('serial')
            applied.append('serial_module')

        if 'coverage' in changes:
            cov = changes['coverage']
            try:
                with open(CONFIG_PATH, 'r') as f:
                    full_config = json.load(f)
            except Exception:
                full_config = {}
            if 'server_url' in cov:
                full_config['coverage_server_url'] = cov['server_url']
                self.config['coverage_server_url'] = cov['server_url']
            if 'api_key' in cov:
                full_config['coverage_api_key'] = cov['api_key']
                self.config['coverage_api_key'] = cov['api_key']
            if 'antenna_gain' in cov:
                full_config['coverage_antenna_gain'] = float(cov['antenna_gain'])
                self.config['coverage_antenna_gain'] = float(cov['antenna_gain'])
            if 'antenna_height' in cov:
                full_config['coverage_antenna_height'] = float(cov['antenna_height'])
                self.config['coverage_antenna_height'] = float(cov['antenna_height'])
            if 'max_range_km' in cov:
                full_config['coverage_max_range_km'] = int(cov['max_range_km'])
                self.config['coverage_max_range_km'] = int(cov['max_range_km'])
            try:
                with open(CONFIG_PATH, 'w') as f:
                    json.dump(full_config, f, indent=2)
                print(f"[CONFIG] Coverage config saved")
            except Exception as e:
                print(f"[CONFIG] Coverage save error: {e}")
            applied.append('coverage')

        if 'telemetry_mapper' in changes:
            tel = changes['telemetry_mapper']
            try:
                with open(CONFIG_PATH, 'r') as f:
                    full_config = json.load(f)
            except Exception:
                full_config = {}
            full_config['telemetry_opt_out'] = bool(tel.get('opt_out', False))
            self.config['telemetry_opt_out'] = bool(tel.get('opt_out', False))
            try:
                with open(CONFIG_PATH, 'w') as f:
                    json.dump(full_config, f, indent=2)
                print(f"[CONFIG] Telemetry opt-out saved: {full_config['telemetry_opt_out']}")
            except Exception as e:
                print(f"[CONFIG] Telemetry save error: {e}")
            applied.append('telemetry_mapper')

        if reboot:
            try:
                node.reboot()
                print(f"[CONFIG] Device reboot requested")
            except (BrokenPipeError, OSError):
                print(f"[CONFIG] Connection dropped during reboot (expected)")

        print(f"[CONFIG] Applied: {applied}")
        return applied

    def _get_or_create_anonymous_id(self):
        """Get or create a persistent anonymous ID for telemetry."""
        anon_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '.anonymous_id')
        try:
            if os.path.exists(anon_file):
                with open(anon_file, 'r') as f:
                    anon_id = f.read().strip()
                    if len(anon_id) >= 8:
                        return anon_id
        except Exception:
            pass
        anon_id = uuid.uuid4().hex
        try:
            with open(anon_file, 'w') as f:
                f.write(anon_id)
        except Exception:
            pass
        return anon_id

    def _detect_platform(self):
        """Detect if running on Raspberry Pi, Linux, or Docker."""
        if os.path.exists('/.dockerenv') or os.environ.get('DOCKER_CONTAINER'):
            return 'docker'
        try:
            with open('/proc/cpuinfo', 'r') as f:
                if 'Raspberry Pi' in f.read() or 'BCM' in f.read():
                    return 'raspberry_pi'
        except Exception:
            pass
        return 'linux'

    def _get_os_info(self):
        """Get OS description."""
        try:
            import distro
            return f"{distro.name()} {distro.version()}"
        except Exception:
            pass
        try:
            with open('/etc/os-release', 'r') as f:
                for line in f:
                    if line.startswith('PRETTY_NAME='):
                        return line.split('=', 1)[1].strip().strip('"')
        except Exception:
            pass
        return platform.platform()

    def _send_telemetry_ping(self):
        """Send anonymous telemetry ping. Called once per 24h."""
        if self.config.get('telemetry_opt_out', False):
            return
        try:
            import urllib.request
            anon_id = self._get_or_create_anonymous_id()
            uptime_hours = int((time.time() - self._start_time) / 3600)
            payload = json.dumps({
                'anonymous_id': anon_id,
                'version': MAPPER_VERSION,
                'connection_type': self.connection_type or 'unknown',
                'platform': self._detect_platform(),
                'os_info': self._get_os_info(),
                'arch': platform.machine(),
                'uptime_hours': uptime_hours,
            }).encode('utf-8')
            req = urllib.request.Request(
                'https://meshpulse.app/api/ping',
                data=payload,
                method='POST',
                headers={'Content-Type': 'application/json', 'User-Agent': f'MeshtasticMapper/{MAPPER_VERSION}'}
            )
            urllib.request.urlopen(req, timeout=10)
            print(f"[TELEMETRY] Ping sent (v{MAPPER_VERSION}, {self.connection_type})")
        except Exception as e:
            print(f"[TELEMETRY] Ping failed (non-critical): {e}")

    def _watchdog_loop(self):
        """Restart meshtastic --listen subprocess if no packets received for WATCHDOG_TIMEOUT seconds."""
        WATCHDOG_TIMEOUT = 600  # 10 minutes of silence = restart subprocess
        CHECK_INTERVAL = 60     # check every minute
        # Give extra time on startup before watchdog activates
        time.sleep(120)
        while True:
            time.sleep(CHECK_INTERVAL)
            if self.connection_type != 'serial':
                continue  # watchdog only for serial mode
            silence = time.time() - self._last_radio_packet_time
            if silence > WATCHDOG_TIMEOUT:
                print(f"[WATCHDOG] No packets for {int(silence)}s — restarting meshtastic listener...")
                self._last_radio_packet_time = time.time()  # reset before restart
                try:
                    if self._serial_iface:
                        self._serial_iface.disconnect()
                        time.sleep(2)
                except Exception as e:
                    print(f"[WATCHDOG] Error disconnecting: {e}")

    def clean_old_nodes(self):
        """Clean old nodes from self.nodes and self.nodes_no_position"""
        self.clean_old_nodes_from_dict(self.nodes)
        self.clean_old_nodes_from_dict(self.nodes_no_position)

    def _parse_neighbor_info(self, packet):
        """Parse NEIGHBORINFO_APP packet."""
        try:
            from_id = sanitize_node_id(packet.get('fromId'))
            if not from_id:
                return
            from_name = (
                self.nodes.get(from_id, {}).get('name') or
                self.nodes_no_position.get(from_id, {}).get('name') or
                self.known_names.get(from_id) or
                from_id
            )
            decoded = packet.get('decoded', {})
            neighbor_info = decoded.get('neighborinfo', {})
            neighbors_raw = neighbor_info.get('neighbors', [])
            neighbors = []
            for n in neighbors_raw:
                neighbor_num = n.get('nodeId')
                if not neighbor_num:
                    continue
                neighbor_id = sanitize_node_id(f"!{neighbor_num:08x}" if isinstance(neighbor_num, int) else str(neighbor_num))
                if not neighbor_id:
                    continue
                neighbor_name = sanitize_str(
                    self.nodes.get(neighbor_id, {}).get('name') or
                    self.nodes_no_position.get(neighbor_id, {}).get('name') or
                    self.known_names.get(neighbor_id) or
                    neighbor_id
                )
                neighbors.append({
                    'id': neighbor_id,
                    'name': neighbor_name,
                    'snr': n.get('snr', 0) / 4.0
                })
            if neighbors:
                self.stats_db.log_neighbor_info(from_id, from_name, neighbors)
                print(f"[NEIGHBOR] {from_name} ({from_id}): {len(neighbors)} neighbors")
                # Broadcast to frontend
                asyncio.run(self.broadcast_neighbor_update(from_id, from_name, neighbors))
                if plugin_manager:
                    plugin_manager.dispatch_hook_sync('on_neighborinfo', {
                        'node_id': from_id,
                        'neighbors': neighbors
                    })
        except Exception as e:
            print(f"[NEIGHBOR] Parse error: {e}")

    def _update_message_names(self, node_id, name):
        """Update from_name in stored messages when a node's name becomes known"""
        self.known_names[node_id] = sanitize_str(name)
        for ch_msgs in self.messages.values():
            for msg in ch_msgs:
                if msg.get('from_id') == node_id and msg.get('from_name') == node_id:
                    msg['from_name'] = name
        self.stats_db.update_node_name(node_id, name)

    def _refresh_all_message_names(self):
        """Update from_name in all stored messages using current known_names and nodes."""
        updated = 0
        all_names = {}
        for nid, node in self.nodes.items():
            if node.get('name') and node['name'] != nid:
                all_names[nid] = node['name']
        for nid, node in self.nodes_no_position.items():
            if node.get('name') and node['name'] != nid:
                all_names[nid] = node['name']
        all_names.update(self.known_names)

        for ch_msgs in self.messages.values():
            for msg in ch_msgs:
                from_id = msg.get('from_id')
                if from_id and from_id in all_names:
                    if msg.get('from_name') == from_id:
                        msg['from_name'] = all_names[from_id]
                        updated += 1
        if updated:
            print(f"[MSGS] Refreshed {updated} message names from known nodes")

    def log_packet_to_stats(self, from_id, portnum, hops, snr, rssi, via_mqtt, relay_node_raw):
        """Log packet to stats DB and detect anomalies."""
        from_name = (
            self.nodes.get(from_id, {}).get('name') or
            self.nodes_no_position.get(from_id, {}).get('name') or
            self.known_names.get(from_id) or
            from_id
        )

        relay_node_id = None
        if relay_node_raw is not None:
            if relay_node_raw > 0xFF:
                # A full node number: node ids are '!%08x', so this is the id itself.
                relay_node_id = f"!{relay_node_raw:08x}"
            else:
                # Last byte only, which is what the firmware sends. Resolving it to
                # a node would guess: with 100+ nodes a last-byte collision is ~30%
                # likely, so it stays an opaque relay_XX.
                relay_node_id = f"relay_{relay_node_raw:02x}"

        self._last_radio_packet_time = time.time()  # watchdog reset
        self.stats_db.log_packet(from_id, from_name, portnum, hops, snr, rssi, via_mqtt, relay_node_id, False)

        if not via_mqtt:
            self._packet_event_queue.append({
                'type': 'packet_event',
                'ts': int(time.time() * 1000),
                'from_id': from_id,
                'portnum': portnum,
                'snr': snr,
                'rssi': rssi,
                'hops': hops,
                # What the stats get: '!xxxxxxxx', or 'relay_XX' when the firmware
                # only sent the last byte (it usually does). None: heard directly
                # or the firmware is too old to say.
                'relay': relay_node_id,
            })

        # Anomaly detection - check all packet types
        now = int(time.time())
        key = (from_id, portnum)
        if key in self._last_packet_times:
            interval = now - self._last_packet_times[key]

            if portnum == 'POSITION_APP' and not via_mqtt:
                if interval < 30:
                    self.stats_db.log_anomaly(from_id, from_name, 'HIGH_FREQUENCY_POSITION',
                        f'⚠️ Very aggressive! Position every {interval}s (< 30s threshold). '
                        f'For stationary nodes use ≥1800s, for moving ≥30s.',
                        'warning')
                elif interval < 60:
                    self.stats_db.log_anomaly(from_id, from_name, 'FREQUENT_POSITION',
                        f'ℹ️ Slightly too frequent. Position every {interval}s. '
                        f'Consider ≥300s for moving nodes, ≥1800s for stationary.',
                        'info')

            elif portnum == 'NODEINFO_APP' and not via_mqtt:
                if interval < 60:
                    self.stats_db.log_anomaly(from_id, from_name, 'HIGH_FREQUENCY_NODEINFO',
                        f'⚠️ Very aggressive! NodeInfo every {interval}s (< 60s threshold). '
                        f'Default is 900s (15min). Causes serious channel congestion.',
                        'warning')
                elif interval < 300:
                    self.stats_db.log_anomaly(from_id, from_name, 'FREQUENT_NODEINFO',
                        f'ℹ️ Slightly too frequent. NodeInfo every {interval}s (recommended ≥900s). '
                        f'Not critical but contributes to channel load.',
                        'info')

            elif portnum == 'TELEMETRY_APP' and not via_mqtt:
                if interval < 60:
                    self.stats_db.log_anomaly(from_id, from_name, 'HIGH_FREQUENCY_TELEMETRY',
                        f'⚠️ Very aggressive! Telemetry every {interval}s (should be ≥1800s). '
                        f'Wastes airtime and drains battery fast.',
                        'warning')
                elif interval < 300:
                    self.stats_db.log_anomaly(from_id, from_name, 'FREQUENT_TELEMETRY',
                        f'ℹ️ Slightly too frequent. Telemetry every {interval}s (recommended ≥1800s). '
                        f'Not critical but consider increasing the interval.',
                        'info')

            elif portnum == 'TEXT_MESSAGE_APP':
                if interval < 5:
                    self.stats_db.log_anomaly(from_id, from_name, 'SPAM_MESSAGES',
                        f'Messages sent every {interval}s (< 5s threshold). '
                        f'Possible automated script or misconfigured device.',
                        'error')
                elif interval < 30:
                    self.stats_db.log_anomaly(from_id, from_name, 'FREQUENT_MESSAGES',
                        f'Messages sent every {interval}s. '
                        f'High message rate causes channel congestion.',
                        'warning')

        self._last_packet_times[key] = now

    def parse_node_info(self, line):
        """Parse nodeinfo from --listen output"""
        if 'Received nodeinfo:' in line:
            try:
                # Extract the dict
                dict_str = line.split('Received nodeinfo:')[1].strip()
                # Safe eval
                import ast
                node_data = ast.literal_eval(dict_str)
                
                node_id = node_data.get('user', {}).get('id')
                name = sanitize_str(node_data.get('user', {}).get('longName', node_id), max_len=50)
                pos = node_data.get('position', {})
                
                if not node_id:
                    return False

                if 'latitudeI' in pos and 'longitudeI' in pos:
                    lat = pos['latitudeI'] / 1e7
                    lon = pos['longitudeI'] / 1e7
                    alt = pos.get('altitude', 0)
                    snr = node_data.get('snr', 0)
                    role = node_data.get('user', {}).get('role', 'CLIENT')
                    hops = node_data.get('hopsAway', None)

                    # Check if node exists and show update message
                    is_new = node_id not in self.nodes
                    
                    # Use lastHeard from packet if available, otherwise current time
                    last_heard = node_data.get('lastHeard', int(time.time()))

                    via_mqtt = node_data.get('viaMqtt', False)

                    self.nodes[node_id] = {
                        'id': node_id,
                        'name': name,
                        'lat': round(lat, 6),
                        'lon': round(lon, 6),
                        'alt': alt,
                        'snr': round(snr, 1),
                        'role': role,
                        'hops': hops,
                        'ts': int(time.time()),
                        'seen_at': int(time.time()),
                        'via_mqtt': via_mqtt,
                        'source': 'live'
                    }
                    # Remove from no-position dict if node now has GPS
                    if node_id in self.nodes_no_position:
                        del self.nodes_no_position[node_id]
                        print(f"[GPS] {node_id} moved from no-GPS to GPS list")

                    marker = "✚" if is_new else "↻"
                    print(f"{marker} {node_id} {name[:20]} @ {lat:.4f},{lon:.4f}")

                    self._update_message_names(node_id, name)
                    relay_node_raw = node_data.get('relayNode') or node_data.get('relay_node')
                    self.log_packet_to_stats(node_id, 'NODEINFO_APP', hops, snr, None, via_mqtt, relay_node_raw)

                    # Broadcast to WebSocket clients
                    asyncio.run(self.broadcast_node_update(self.nodes[node_id]))
                    asyncio.run(self.broadcast_stats_update())
                    if plugin_manager:
                        plugin_manager.dispatch_hook_sync('on_node_update', self.nodes.get(node_id))

                    return True

                else:
                    # Node without position - save to separate dict
                    name = sanitize_str(node_data.get('user', {}).get('longName', node_id), max_len=50)
                    snr = node_data.get('snr', 0)
                    role = node_data.get('user', {}).get('role', 'CLIENT')
                    last_heard = node_data.get('lastHeard', int(time.time()))
                    hops = node_data.get('hopsAway', None)
                    via_mqtt = node_data.get('viaMqtt', False)

                    is_new = node_id not in self.nodes_no_position

                    self.nodes_no_position[node_id] = {
                        'id': node_id,
                        'name': name,
                        'snr': round(snr, 1),
                        'role': role,
                        'hops': hops,
                        'via_mqtt': via_mqtt,
                        'ts': int(time.time()),
                        'seen_at': int(time.time()),
                        'source': 'live'
                    }

                    marker = "✚" if is_new else "↻"
                    print(f"{marker} {node_id} {name[:20]} (no GPS)")

                    self._update_message_names(node_id, name)
                    relay_node_raw = node_data.get('relayNode') or node_data.get('relay_node')
                    self.log_packet_to_stats(node_id, 'NODEINFO_APP', hops, snr, None, via_mqtt, relay_node_raw)

                    # Broadcast to WebSocket clients
                    asyncio.run(self.broadcast_node_update(self.nodes_no_position[node_id]))
                    asyncio.run(self.broadcast_stats_update())
                    if plugin_manager:
                        plugin_manager.dispatch_hook_sync('on_node_update', self.nodes_no_position.get(node_id))

                    return True

            except Exception as e:
                print(f"Parse error: {e}")
        
        return False
    
    def parse_position_update(self, line):
        """Parse position update from --listen output"""
        if 'Publishing meshtastic.receive.position:' not in line:
            return False
        
        try:
            # Extract fromId
            from_match = re.search(r"'fromId':\s*'([^']+)'", line)
            if not from_match:
                return False
            
            node_id = from_match.group(1)
            
            # Extract latitude and longitude
            lat_match = re.search(r"'latitude':\s*([-\d.]+)", line)
            lon_match = re.search(r"'longitude':\s*([-\d.]+)", line)
            
            if not lat_match or not lon_match:
                return False
            
            lat = float(lat_match.group(1))
            lon = float(lon_match.group(1))
            
            # Skip invalid coordinates
            if is_placeholder_position(lat, lon):
                return False
            
            # Extract SNR if available
            snr_match = re.search(r"'rxSnr':\s*([-\d.]+)", line)
            snr = float(snr_match.group(1)) if snr_match else 0

            rssi_match = re.search(r"'rxRssi':\s*([-\d]+)", line)
            rssi = int(rssi_match.group(1)) if rssi_match else None

            # Extract hops (hopStart - hopLimit)
            hop_start_match = re.search(r"'hopStart':\s*(\d+)", line)
            hop_limit_match = re.search(r"'hopLimit':\s*(\d+)", line)
            if hop_start_match and hop_limit_match:
                hops = int(hop_start_match.group(1)) - int(hop_limit_match.group(1))
            else:
                hops = None  # Unknown hops - don't assume direct
            
            # Extract transport mechanism (detect MQTT)
            transport_match = re.search(r"'transportMechanism':\s*'([^']+)'", line)
            via_mqtt = transport_match and transport_match.group(1) == 'TRANSPORT_MQTT'

            relay_match = re.search(r"'relayNode':\s*(\d+)", line)
            relay_node_raw = int(relay_match.group(1)) if relay_match else None
            
            # Update existing node or create minimal entry
            if node_id in self.nodes:
                # Update existing node
                self.nodes[node_id]['lat'] = round(lat, 6)
                self.nodes[node_id]['lon'] = round(lon, 6)
                self.nodes[node_id]['snr'] = round(snr, 1)
                self.nodes[node_id]['rssi'] = rssi
                self.nodes[node_id]['hops'] = hops
                self.nodes[node_id]['via_mqtt'] = via_mqtt
                self.nodes[node_id]['ts'] = int(time.time())
                self.nodes[node_id]['seen_at'] = int(time.time())
                self.nodes[node_id]['source'] = 'live'
                print(f"↻ {node_id} position update @ {lat:.4f},{lon:.4f} hops={hops}{' MQTT' if via_mqtt else ''}")
            else:
                # New node from position packet (minimal info)
                self.nodes[node_id] = {
                    'id': node_id,
                    'name': (
                        self.nodes_no_position.get(node_id, {}).get('name') or
                        self.known_names.get(node_id) or
                        node_id
                    ),  # Will be updated when nodeinfo arrives
                    'lat': round(lat, 6),
                    'lon': round(lon, 6),
                    'alt': 0,
                    'snr': round(snr, 1),
                    'rssi': rssi,
                    'role': 'CLIENT',
                    'hops': hops,
                    'via_mqtt': via_mqtt,
                    'ts': int(time.time()),
                    'seen_at': int(time.time()),
                    'source': 'live'
                }
                # Remove from no-position dict if node now has GPS
                if node_id in self.nodes_no_position:
                    del self.nodes_no_position[node_id]
                    print(f"[GPS] {node_id} moved from no-GPS to GPS list")
                print(f"✚ {node_id} NEW from position @ {lat:.4f},{lon:.4f} hops={hops}{' MQTT' if via_mqtt else ''}")
            
            self.log_packet_to_stats(node_id, 'POSITION_APP', hops, snr, rssi, via_mqtt, relay_node_raw)

            # Broadcast to WebSocket clients
            asyncio.run(self.broadcast_node_update(self.nodes[node_id]))
            asyncio.run(self.broadcast_stats_update())
            if plugin_manager:
                plugin_manager.dispatch_hook_sync('on_position', {
                    'node_id': node_id, 'lat': lat, 'lon': lon,
                    'alt': 0, 'snr': snr, 'rssi': rssi, 'hops': hops,
                    'timestamp': int(time.time()),
                    'via_mqtt': bool(via_mqtt),
                    'speed': None, 'heading': None,
                    'precision_bits': None, 'pos_time': None,
                })

            return True

        except Exception as e:
            print(f"Position parse error: {e}")
        
        return False

    def parse_telemetry_update(self, line):
        """Parse telemetry to refresh node timestamp (keeps nodes 'alive')"""
        if 'Publishing meshtastic.receive.telemetry:' not in line:
            return False
        
        try:
            # Extract fromId
            from_match = re.search(r"'fromId':\s*'([^']+)'", line)
            if not from_match:
                return False
            
            node_id = from_match.group(1)

            # Extract device uptime and radio stats for local/tracker node
            if node_id == self.local_node_id:
                tracker_updated = False

                uptime_match = re.search(r"'uptimeSeconds':\s*(\d+)", line)
                if uptime_match:
                    self.tracker_info['uptime_seconds'] = int(uptime_match.group(1))
                    tracker_updated = True

                # Parse radio stats from localStats
                radio_fields = ['channelUtilization', 'airUtilTx', 'numPacketsTx', 'numPacketsRx',
                                 'numPacketsRxBad', 'numRxDupe', 'numTxRelay', 'numTxRelayCanceled',
                                 'numOnlineNodes', 'numTotalNodes', 'heapFreeBytes', 'heapTotalBytes']
                radio_stats = {}
                for field in radio_fields:
                    m = re.search(rf"'{field}':\s*([\d.]+)", line)
                    if m:
                        val = m.group(1)
                        radio_stats[field] = float(val) if '.' in val else int(val)
                if radio_stats:
                    self.tracker_info['radio_stats'] = radio_stats
                    tracker_updated = True
                    # Only log to DB when this is a localStats packet (has TX/RX counters).
                    # deviceMetrics packets share some field names (channelUtilization, airUtilTx)
                    # and arrive every ~60s — logging them would flood radio_stats_history.
                    if 'numTxRelay' in radio_stats or 'numPacketsTx' in radio_stats:
                        self.stats_db.log_radio_stats(radio_stats)

                if tracker_updated:
                    asyncio.run(self.broadcast_connection_status('connected'))

            # Parse RSSI if present
            rssi_match = re.search(r"'rxRssi':\s*([-\d]+)", line)
            rssi = int(rssi_match.group(1)) if rssi_match else None
            if rssi is not None:
                if node_id in self.nodes:
                    self.nodes[node_id]['rssi'] = rssi
                elif node_id in self.nodes_no_position:
                    self.nodes_no_position[node_id]['rssi'] = rssi

            if node_id != self.local_node_id:
                relay_match_t = re.search(r"'relayNode':\s*(\d+)", line)
                relay_node_raw_t = int(relay_match_t.group(1)) if relay_match_t else None
                self.log_packet_to_stats(node_id, 'TELEMETRY_APP', None, None, rssi, False, relay_node_raw_t)

            # Only update timestamp if node already exists
            if node_id in self.nodes:
                self.nodes[node_id]['ts'] = int(time.time())
                self.nodes[node_id]['seen_at'] = int(time.time())
                self.nodes[node_id]['source'] = 'live'
                print(f"♡ {node_id} telemetry heartbeat")

                # Broadcast to WebSocket clients
                asyncio.run(self.broadcast_node_update(self.nodes[node_id]))
                asyncio.run(self.broadcast_stats_update())
                if plugin_manager:
                    plugin_manager.dispatch_hook_sync('on_telemetry', {
                        'node_id': node_id, 'timestamp': int(time.time())
                    })

                return True
            elif node_id in self.nodes_no_position:
                self.nodes_no_position[node_id]['ts'] = int(time.time())
                self.nodes_no_position[node_id]['seen_at'] = int(time.time())
                self.nodes_no_position[node_id]['source'] = 'live'
                print(f"♡ {node_id} telemetry heartbeat (no GPS)")

                # Broadcast to WebSocket clients
                asyncio.run(self.broadcast_node_update(self.nodes_no_position[node_id]))
                asyncio.run(self.broadcast_stats_update())
                if plugin_manager:
                    plugin_manager.dispatch_hook_sync('on_telemetry', {
                        'node_id': node_id, 'timestamp': int(time.time())
                    })

            return True

        except Exception as e:
            print(f"Telemetry parse error: {e}")
        
        return False

    def parse_text_message(self, line):
        """Parse text message from --listen output"""
        if 'Publishing meshtastic.receive.text:' not in line:
            return False

        try:
            # Extract fromId
            from_match = re.search(r"'fromId':\s*'([^']+)'", line)
            if not from_match:
                return False
            from_id = from_match.group(1)

            # Extract toId
            to_match = re.search(r"'toId':\s*'([^']+)'", line)
            to_id = to_match.group(1) if to_match else '^all'

            # Extract text - try 'text' field first, then payload
            text_match = re.search(r"'text':\s*'([^']*)'", line)
            if not text_match:
                # Try to get from payload bytes
                payload_match = re.search(r"'payload':\s*b'([^']*)'", line)
                if payload_match:
                    text = payload_match.group(1)
                else:
                    return False
            else:
                text = text_match.group(1)

            if not text:
                return False

            # Extract channel index
            ch_match = re.search(r"'channel':\s*(\d+)", line)
            channel_index = int(ch_match.group(1)) if ch_match else 0

            # Get sender name from nodes
            sender_name = from_id
            if from_id in self.nodes:
                sender_name = self.nodes[from_id].get('name', from_id)
            elif from_id in self.nodes_no_position:
                sender_name = self.nodes_no_position[from_id].get('name', from_id)
            elif from_id in self.known_names:
                sender_name = self.known_names[from_id]

            # Create message object
            message = {
                'from_id': from_id,
                'from_name': sender_name,
                'to_id': to_id,
                'text': text,
                'timestamp': int(time.time()),
                'is_dm': to_id != '^all',
                'channel_index': channel_index,
                # True only for a DM encrypted and signed with the sender's PKI key
                # (firmware 2.5+); only then is from_id more than a claim.
                'pki': repr_flag_true(line, 'pkiEncrypted'),
            }

            # Add to channel dict (newest first, max 50 per channel)
            if channel_index not in self.messages:
                self.messages[channel_index] = []
            self.messages[channel_index].insert(0, message)
            if len(self.messages[channel_index]) > 50:
                self.messages[channel_index] = self.messages[channel_index][:50]

            # Log
            dm_marker = " [DM]" if message['is_dm'] else ""
            print(f"💬 [ch{channel_index}] {sender_name}: {text}{dm_marker}")

            relay_match_msg = re.search(r"'relayNode':\s*(\d+)", line)
            relay_node_raw_msg = int(relay_match_msg.group(1)) if relay_match_msg else None
            self.log_packet_to_stats(from_id, 'TEXT_MESSAGE_APP', None, None, None, False, relay_node_raw_msg)

            # Broadcast to WebSocket clients
            asyncio.run(self.broadcast_message(message))
            if plugin_manager:
                plugin_manager.dispatch_hook_sync('on_message', message)

            return True

        except Exception as e:
            print(f"Text message parse error: {e}")

        return False

    # ------------------------------------------------------------------
    # Python API packet parsers (TCP mode — receive already-decoded dicts)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Remote administration (read-only, v1)
    # ------------------------------------------------------------------
    # Admin messages travel over the live interface, the same way
    # request_position does: no serial release, no CLI subprocess. Measured on
    # Pimesh1 against a direct neighbour: 1.2 to 1.5 s per PKC round trip,
    # against 7 s for `meshtastic --dest ... --get` plus the reconnect.

    # section -> (oneof field of AdminMessage, enum container, enum value name)
    REMOTE_ADMIN_SECTIONS = {
        'owner': ('get_owner_request', None, None),
        'metadata': ('get_device_metadata_request', None, None),
        'device': ('get_config_request', 'ConfigType', 'DEVICE_CONFIG'),
        'position': ('get_config_request', 'ConfigType', 'POSITION_CONFIG'),
        'power': ('get_config_request', 'ConfigType', 'POWER_CONFIG'),
        'network': ('get_config_request', 'ConfigType', 'NETWORK_CONFIG'),
        'display': ('get_config_request', 'ConfigType', 'DISPLAY_CONFIG'),
        'lora': ('get_config_request', 'ConfigType', 'LORA_CONFIG'),
        'bluetooth': ('get_config_request', 'ConfigType', 'BLUETOOTH_CONFIG'),
        'security': ('get_config_request', 'ConfigType', 'SECURITY_CONFIG'),
        'mqtt': ('get_module_config_request', 'ModuleConfigType', 'MQTT_CONFIG'),
        'telemetry': ('get_module_config_request', 'ModuleConfigType', 'TELEMETRY_CONFIG'),
        'neighborinfo': ('get_module_config_request', 'ModuleConfigType', 'NEIGHBORINFO_CONFIG'),
        'store_forward': ('get_module_config_request', 'ModuleConfigType', 'STOREFORWARD_CONFIG'),
        'range_test': ('get_module_config_request', 'ModuleConfigType', 'RANGETEST_CONFIG'),
    }

    # Routing errors an admin request can come back with, in words a user can act on
    REMOTE_ADMIN_HINTS = {
        'ADMIN_PUBLIC_KEY_UNAUTHORIZED': "The node answered, but your tracker's public key is not one of its admin keys. "
                                         "Add it on that node under Security > Admin Key (up to three keys).",
        'NOT_AUTHORIZED': "The node refused the request: your tracker is not authorised to administer it.",
        'PKI_FAILED': "Your tracker could not encrypt for this node: it does not hold the node's public key.",
        'PKI_UNKNOWN_PUBKEY': "The node does not know your tracker's public key yet.",
        'NO_RESPONSE': "The request was delivered, but nothing answered it.",
        'MAX_RETRANSMIT': "No acknowledgement after all retries: the node is out of reach or asleep.",
        'NO_ROUTE': "No route to the node.",
        'NO_CHANNEL': "The node is not on a channel your tracker can use for admin.",
        'TOO_LARGE': "The answer did not fit in one packet.",
        'PKI_SEND_FAIL_PUBLIC_KEY': "Your radio does not hold this node's public key, so it refused to encrypt for it.",
        'ADMIN_BAD_SESSION_KEY': "The node rejected the write: the session key from the read had expired. Try again.",
    }

    def _remember_key(self, node_id, public_key_b64, via_mqtt=False):
        """Keep every public key we see, so keys the radio's small NodeDB drops can be
        handed back to it later. A changed key is logged: that is either a reflashed
        node or somebody impersonating it."""
        if not node_id or not public_key_b64:
            return
        try:
            outcome = self.stats_db.remember_public_key(node_id, public_key_b64, via_mqtt)
            if outcome == 'changed':
                print(f"[KEYS] ⚠️ {node_id} now announces a different public key ({public_key_b64[:8]}…)"
                      f"{' via MQTT' if via_mqtt else ''}")
        except Exception as e:
            print(f"[KEYS] could not store key for {node_id}: {e}")

    def _mesh_iface(self):
        """The live MeshInterface of the current connection, or None."""
        if self.connection_type == 'serial' and self._serial_iface and self._serial_iface.iface:
            return self._serial_iface.iface
        if self.connection_type == 'tcp' and self._tcp_iface and self._tcp_iface.interface:
            return self._tcp_iface.interface
        return None

    @staticmethod
    def _send_and_wait(iface, payload, dest, port, timeout, want_response=True, **kw):
        """sendData with onResponse, without the library's blocking waitForAckNak,
        which fights the listener on serial. Returns (rtt_seconds, packet) or (None, None)."""
        done, box = threading.Event(), {}

        def _on_response(packet):
            box['p'], box['t'] = packet, time.time()
            done.set()

        t0 = time.time()
        iface.sendData(payload, dest, portNum=port, wantResponse=want_response, onResponse=_on_response, **kw)
        if not done.wait(timeout):
            return None, None
        return box['t'] - t0, box['p']

    def _ensure_remote_pubkey(self, iface, dest, steps, timeout=15):
        """PKC needs the target's public key in the radio's own NodeDB, which holds far
        fewer nodes than the mesh has (80 on a Wio Tracker L1 against 300+ around
        Poznań), so it drops keys all the time. In order:
          1. the radio still has it: done;
          2. MeshPulse has it in node_keys: hand it back with add_contact, the same
             thing the phone apps do before every DM (firmware also pins the entry
             as a favourite, so it is not evicted again);
          3. ask the node with a NodeInfo request. Last, because firmware ignores a
             requester it heard less than 12 h ago (NODEINFO_REPLY_SUPPRESS_SECS)."""
        from meshtastic.protobuf import admin_pb2

        if ((iface.nodes or {}).get(dest) or {}).get('user', {}).get('publicKey'):
            return True
        cached = self.stats_db.get_public_key(dest)
        if cached:
            known = (iface.nodes or {}).get(dest, {}).get('user', {})
            contact = admin_pb2.AdminMessage()
            contact.add_contact.node_num = int(dest[1:], 16)
            contact.add_contact.user.id = dest
            contact.add_contact.user.long_name = known.get('longName') or self._display_name(dest)
            contact.add_contact.user.short_name = known.get('shortName') or ''
            contact.add_contact.user.public_key = base64.b64decode(cached)
            iface.localNode._sendAdmin(contact, wantResponse=False)
            steps.append('Radio had dropped this node\'s public key; handed it back from MeshPulse\'s key store')
            time.sleep(0.5)  # let the radio file it before we ask it to encrypt
            return True

        def _has_key():
            return bool(((iface.nodes or {}).get(dest) or {}).get('user', {}).get('publicKey'))
        me = (iface.getMyNodeInfo() or {}).get('user', {})
        user = mesh_pb2.User(id=me.get('id', ''), long_name=me.get('longName', ''),
                             short_name=me.get('shortName', ''),
                             public_key=iface.localNode.localConfig.security.public_key)
        steps.append('No public key for this node anywhere yet, asking it for its node info')
        rtt, packet = self._send_and_wait(iface, user, dest, portnums_pb2.PortNum.NODEINFO_APP, timeout, wantAck=False)
        if packet is None:
            steps.append(f'No node info answer within {timeout} s (a node ignores requesters it heard in the last 12 h)')
            return False
        for _ in range(30):  # the library files the key a moment after the callback
            if _has_key():
                self._remember_key(dest, iface.nodes[dest]['user']['publicKey'])
                steps.append(f'Got its public key in {rtt:.1f} s')
                return True
            time.sleep(0.1)
        steps.append('Node info answered, but without a public key (firmware older than 2.5?)')
        return False

    def _display_name(self, node_id):
        n = self.nodes.get(node_id) or self.nodes_no_position.get(node_id) or {}
        return (n.get('name') or self.known_names.get(node_id) or node_id)[:39]

    # Writes are allowed when config.json says so permanently, or for a limited time
    # after someone arms them from the Config page. Shared by every mapper instance
    # (the mapper object is rebuilt on reconnect; an armed window must survive that).
    REMOTE_ADMIN_ARM = {'until': 0.0, 'by': None}
    REMOTE_ADMIN_ARM_MINUTES = (15, 30, 60, 120)

    def remote_admin_write_enabled(self):
        return bool(self.config.get('remote_admin_write', False)) or time.time() < self.REMOTE_ADMIN_ARM['until']

    def remote_admin_state(self):
        now = time.time()
        until = self.REMOTE_ADMIN_ARM['until']
        try:
            recent = self.stats_db.recent_admin_actions(10)
        except Exception:
            recent = []
        return {
            'permanent': bool(self.config.get('remote_admin_write', False)),
            'armed': until > now,
            'armed_until': int(until) if until > now else 0,
            'remaining': max(0, int(until - now)),
            'armed_by': self.REMOTE_ADMIN_ARM['by'] if until > now else None,
            'enabled': self.remote_admin_write_enabled(),
            'minutes_options': list(self.REMOTE_ADMIN_ARM_MINUTES),
            'recent': recent,
        }

    def remote_admin_arm(self, minutes, client):
        minutes = int(minutes) if str(minutes).isdigit() else 60
        if minutes not in self.REMOTE_ADMIN_ARM_MINUTES:
            minutes = 60
        self.REMOTE_ADMIN_ARM['until'] = time.time() + minutes * 60
        self.REMOTE_ADMIN_ARM['by'] = client
        try:
            self.stats_db.log_admin_action('-', 'arm', None, json.dumps({'minutes': minutes, 'client': client}), 'armed')
        except Exception as e:
            print(f"[ADMIN] audit log failed: {e}")
        print(f"[ADMIN] remote writes armed for {minutes} min by {client}")
        return self.remote_admin_state()

    def remote_admin_disarm(self, client):
        was = self.REMOTE_ADMIN_ARM['until'] > time.time()
        self.REMOTE_ADMIN_ARM['until'] = 0.0
        self.REMOTE_ADMIN_ARM['by'] = None
        if was:
            try:
                self.stats_db.log_admin_action('-', 'disarm', None, json.dumps({'client': client}), 'disarmed')
            except Exception as e:
                print(f"[ADMIN] audit log failed: {e}")
            print(f"[ADMIN] remote writes disarmed by {client}")
        return self.remote_admin_state()

    # Sections that may be written remotely, and why the others may not (yet).
    # Full remote configuration is the goal; these guards are a policy layer, so
    # opening a section or field later is a one-line change here, not a rewrite.
    REMOTE_ADMIN_WRITABLE = {'owner', 'device', 'position', 'power', 'display', 'telemetry',
                             'neighborinfo', 'range_test', 'store_forward'}
    REMOTE_ADMIN_GUARDED_SECTIONS = {
        'lora': 'Radio parameters. A wrong region, preset or channel cuts the node off the mesh, '
                'and only a cable brings it back.',
        'security': 'Keys and admin settings. One wrong value locks remote admin out for good.',
        'network': 'Network settings can take away the interfaces you would use to rescue the node.',
        'bluetooth': 'Bluetooth settings can take away the interfaces you would use to rescue the node.',
        'mqtt': 'Not writable yet.',
        'metadata': 'Read-only by nature.',
    }
    REMOTE_ADMIN_GUARDED_FIELDS = {
        'device.role': 'The role decides how the node relays. Not remotely yet.',
        'owner.is_licensed': 'Licensed (ham) mode switches encryption off, PKC included: remote admin would stop working.',
        'power.is_power_saving': 'A sleeping node may stop answering admin requests.',
    }
    REMOTE_ADMIN_OWNER_EDITABLE = {'long_name', 'short_name', 'is_unmessagable'}
    # field name of the section inside Config / ModuleConfig
    REMOTE_ADMIN_ONEOF = {
        'device': 'device', 'position': 'position', 'power': 'power', 'network': 'network',
        'display': 'display', 'lora': 'lora', 'bluetooth': 'bluetooth', 'security': 'security',
        'mqtt': 'mqtt', 'telemetry': 'telemetry', 'neighborinfo': 'neighbor_info',
        'store_forward': 'store_forward', 'range_test': 'range_test',
    }

    def _ra_prepare(self, dest, section, result):
        """Validate the request and return the live interface, or None with result filled."""
        if section is not None and section not in self.REMOTE_ADMIN_SECTIONS:
            result['error'] = 'UNKNOWN_SECTION'
            return None
        if not dest or not re.fullmatch(r'![0-9a-f]{8}', dest):
            result['error'] = 'BAD_NODE_ID'
            return None
        iface = self._mesh_iface()
        if iface is None:
            result['error'] = 'NOT_CONNECTED'
            result['hint'] = 'MeshPulse is not connected to its radio right now.'
            return None
        if not hasattr(self, '_remote_admin_lock'):
            self._remote_admin_lock = threading.Lock()
            self._remote_admin_sessions = {}
        result['tracker_public_key'] = base64.b64encode(iface.localNode.localConfig.security.public_key).decode()
        return iface

    def _ra_send(self, iface, dest, msg, timeout, steps, result, expect_reply=True):
        """One PKC admin round trip. With expect_reply the answer is an admin message;
        without it (set_*, reboot) the node only acknowledges, so the ACK is the answer.
        Returns the reply packet, or None with result['error'] filled."""
        kw = dict(wantAck=True, pkiEncrypted=True, channelIndex=iface.localNode._getAdminChannelIndex())
        if not expect_reply:
            kw.update(want_response=False, onResponseAckPermitted=True)
        rtt, packet = self._send_and_wait(iface, msg, dest, portnums_pb2.PortNum.ADMIN_APP, timeout, **kw)
        if packet is None:
            result['error'] = 'TIMEOUT'
            result['hint'] = f'No answer within {timeout} s. The node may be out of reach, asleep or busy.'
            steps.append(result['hint'])
            return None
        result['rtt'] = round(rtt, 2)
        decoded = packet.get('decoded', {})
        if 'routing' in decoded:
            reason = decoded['routing'].get('errorReason', 'NONE')
            if reason == 'NONE' and not expect_reply:
                steps.append(f'Acknowledged in {rtt:.1f} s')
                return packet
            result['error'] = reason
            result['hint'] = self.REMOTE_ADMIN_HINTS.get(reason, '')
            steps.append(f'{reason} after {rtt:.1f} s')
            return None
        return packet

    def _ra_read(self, iface, dest, section, timeout, steps, result):
        """Read one section. Returns its protobuf message (Config.XConfig, ModuleConfig.X,
        User or DeviceMetadata), or None with result filled. Remembers the session key."""
        from meshtastic.protobuf import admin_pb2
        field, enum_name, value = self.REMOTE_ADMIN_SECTIONS[section]
        msg = admin_pb2.AdminMessage()
        if enum_name is None:
            setattr(msg, field, True)
        else:
            setattr(msg, field, getattr(admin_pb2.AdminMessage, enum_name).Value(value))
        steps.append(f'Sent {field} ({section})')
        packet = self._ra_send(iface, dest, msg, timeout, steps, result)
        if packet is None:
            return None
        raw = packet.get('decoded', {}).get('admin', {}).get('raw')
        if raw is None:
            result['error'] = 'UNEXPECTED_REPLY'
            return None
        if raw.session_passkey:
            self._remote_admin_sessions[dest] = (raw.session_passkey, time.time())
        which = raw.WhichOneof('payload_variant')
        body = getattr(raw, which)
        if which in ('get_config_response', 'get_module_config_response'):
            body = getattr(body, body.WhichOneof('payload_variant'))
        result['reply'] = which
        steps.append(f'Answered in {result["rtt"]:.1f} s')
        return body

    def _ra_schema(self, section, body):
        """Every scalar field of the section with its type, enum options, current value
        (defaults included, which MessageToDict would drop) and whether it may be written."""
        from google.protobuf.descriptor import FieldDescriptor as FD
        ints = {FD.TYPE_INT32, FD.TYPE_INT64, FD.TYPE_UINT32, FD.TYPE_UINT64, FD.TYPE_SINT32,
                FD.TYPE_SINT64, FD.TYPE_FIXED32, FD.TYPE_FIXED64, FD.TYPE_SFIXED32, FD.TYPE_SFIXED64}
        unsigned = {FD.TYPE_UINT32, FD.TYPE_UINT64, FD.TYPE_FIXED32, FD.TYPE_FIXED64}
        section_ok = section in self.REMOTE_ADMIN_WRITABLE
        out = []

        def walk(msg, prefix):
            for f in msg.DESCRIPTOR.fields:
                if f.GetOptions().deprecated:
                    continue
                path = prefix + f.name
                repeated = getattr(f, 'is_repeated', None)
                if repeated is None:
                    repeated = f.label == FD.LABEL_REPEATED
                if f.type == FD.TYPE_MESSAGE and not repeated:
                    walk(getattr(msg, f.name), path + '.')
                    continue
                v = getattr(msg, f.name)
                e = {'path': path}
                if repeated:
                    e['type'] = 'list'
                    e['value'] = [base64.b64encode(x).decode() if isinstance(x, bytes) else str(x) for x in v]
                elif f.type == FD.TYPE_BYTES:
                    e['type'], e['value'] = 'bytes', base64.b64encode(v).decode()
                elif f.type == FD.TYPE_ENUM:
                    e['type'] = 'enum'
                    e['options'] = [ev.name for ev in f.enum_type.values]
                    ev = f.enum_type.values_by_number.get(v)
                    e['value'] = ev.name if ev else str(v)
                elif f.type == FD.TYPE_BOOL:
                    e['type'], e['value'] = 'bool', bool(v)
                elif f.type in (FD.TYPE_FLOAT, FD.TYPE_DOUBLE):
                    e['type'], e['value'] = 'float', round(float(v), 6)
                elif f.type in ints:
                    e['type'], e['value'] = 'int', int(v)
                    if f.type in unsigned:
                        e['min'] = 0
                elif f.type == FD.TYPE_STRING:
                    e['type'], e['value'] = 'string', v
                else:
                    continue
                full = f'{section}.{path}'
                if path == 'private_key':
                    e['value'] = '(hidden)'
                if not section_ok:
                    e['editable'], e['reason'] = False, self.REMOTE_ADMIN_GUARDED_SECTIONS.get(section, 'Not writable.')
                elif full in self.REMOTE_ADMIN_GUARDED_FIELDS:
                    e['editable'], e['reason'] = False, self.REMOTE_ADMIN_GUARDED_FIELDS[full]
                elif section == 'owner' and path not in self.REMOTE_ADMIN_OWNER_EDITABLE:
                    e['editable'], e['reason'] = False, 'Identity field, set by the firmware.'
                elif e['type'] in ('list', 'bytes'):
                    e['editable'], e['reason'] = False, 'Lists and binary fields are not editable yet.'
                else:
                    e['editable'] = True
                if section == 'owner' and path == 'long_name':
                    e['max_bytes'] = 39
                elif section == 'owner' and path == 'short_name':
                    e['max_bytes'] = 4
                out.append(e)

        walk(body, '')
        return out

    def remote_admin_get(self, dest, section, timeout=45):
        """Read one config section from a remote node over PKC admin."""
        from google.protobuf.json_format import MessageToDict
        steps = []
        result = {'dest': dest, 'section': section, 'ok': False, 'steps': steps}
        iface = self._ra_prepare(dest, section, result)
        if iface is None:
            return result
        with self._remote_admin_lock:  # one admin conversation at a time
            if not self._ensure_remote_pubkey(iface, dest, steps):
                result['error'] = 'PKI_UNKNOWN_PUBKEY'
                result['hint'] = ("MeshPulse has not seen this node's public key yet and the node did not answer a "
                                  "node info request. It is stored automatically the next time the node broadcasts "
                                  "its node info (every few hours); try again after that.")
                return result
            body = self._ra_read(iface, dest, section, timeout, steps, result)
            if body is None:
                return result
        values = MessageToDict(body, preserving_proto_field_name=True)
        if 'private_key' in values:  # never let a private key reach a browser
            values['private_key'] = '(hidden)'
        write_enabled = self.remote_admin_write_enabled()
        result.update(ok=True, values=values, schema=self._ra_schema(section, body),
                      write_enabled=write_enabled,
                      section_writable=section in self.REMOTE_ADMIN_WRITABLE,
                      section_reason=self.REMOTE_ADMIN_GUARDED_SECTIONS.get(section))
        return result

    def _ra_apply_changes(self, section, body, changes):
        """Apply {path: value} to a copy of the section. Returns (new_body, diff, error)."""
        schema = {e['path']: e for e in self._ra_schema(section, body)}
        new = type(body)()
        new.CopyFrom(body)
        diff = []
        if not isinstance(changes, dict) or not changes:
            return None, None, 'No changes given.'
        for path, value in changes.items():
            e = schema.get(path)
            if e is None:
                return None, None, f'Unknown field: {path}'
            if not e.get('editable'):
                return None, None, f'{path} is locked: {e.get("reason", "")}'
            try:
                if e['type'] == 'bool':
                    if not isinstance(value, bool):
                        raise ValueError('expected true or false')
                    new_v = value
                elif e['type'] == 'int':
                    if isinstance(value, bool) or float(value) != int(float(value)):
                        raise ValueError('expected a whole number')
                    new_v = int(float(value))
                    if e.get('min') is not None and new_v < e['min']:
                        raise ValueError(f'must be at least {e["min"]}')
                elif e['type'] == 'float':
                    new_v = float(value)
                elif e['type'] == 'string':
                    new_v = str(value)
                    if e.get('max_bytes') and len(new_v.encode('utf-8')) > e['max_bytes']:
                        raise ValueError(f'at most {e["max_bytes"]} bytes')
                    if path in ('long_name', 'short_name') and not new_v.strip():
                        raise ValueError('cannot be empty')
                elif e['type'] == 'enum':
                    if value not in e['options']:
                        raise ValueError('not one of ' + ', '.join(e['options']))
                    new_v = value
                else:
                    raise ValueError('not editable')
            except (TypeError, ValueError) as err:
                return None, None, f'{path}: {err}'
            parent = new
            parts = path.split('.')
            for p in parts[:-1]:
                parent = getattr(parent, p)
            leaf = parts[-1]
            try:
                if e['type'] == 'enum':
                    fd = parent.DESCRIPTOR.fields_by_name[leaf]
                    setattr(parent, leaf, fd.enum_type.values_by_name[new_v].number)
                else:
                    setattr(parent, leaf, new_v)
            except (TypeError, ValueError) as err:   # e.g. out of range for uint32
                return None, None, f'{path}: {err}'
            if new_v != e['value']:
                diff.append({'path': path, 'old': e['value'], 'new': new_v})
        if not diff:
            return None, None, 'Nothing changed.'
        return new, diff, None

    def remote_admin_set(self, dest, section, changes, timeout=45):
        """Write changed fields of one section to a remote node. Reads the section first
        (current values plus a fresh session key), applies only the given fields, sends
        the whole section back. The caller verifies by reading again after the node has
        had time to restart."""
        from meshtastic.protobuf import admin_pb2
        steps = []
        result = {'dest': dest, 'section': section, 'ok': False, 'steps': steps}
        if not self.remote_admin_write_enabled():
            result['error'] = 'WRITE_DISABLED'
            result['hint'] = ('Remote writing is not armed. Arm it for a limited time in Config > Remote admin, '
                              'or set "remote_admin_write": true in /var/www/html/meshpulse/config.json.')
            return result
        if section not in self.REMOTE_ADMIN_WRITABLE:
            result['error'] = 'SECTION_LOCKED'
            result['hint'] = self.REMOTE_ADMIN_GUARDED_SECTIONS.get(section, 'This section is not writable.')
            return result
        iface = self._ra_prepare(dest, section, result)
        if iface is None:
            return result
        with self._remote_admin_lock:
            if not self._ensure_remote_pubkey(iface, dest, steps):
                result['error'] = 'PKI_UNKNOWN_PUBKEY'
                return result
            body = self._ra_read(iface, dest, section, timeout, steps, result)
            if body is None:
                return result
            new_body, diff, err = self._ra_apply_changes(section, body, changes)
            if err:
                result['error'], result['hint'] = 'INVALID_CHANGE', err
                steps.append(err)
                return result
            passkey = self._remote_admin_sessions.get(dest, (b'', 0))[0]
            msg = admin_pb2.AdminMessage()
            if section == 'owner':
                msg.set_owner.CopyFrom(new_body)
                what = 'set_owner'
            elif self.REMOTE_ADMIN_SECTIONS[section][0] == 'get_config_request':
                getattr(msg.set_config, self.REMOTE_ADMIN_ONEOF[section]).CopyFrom(new_body)
                what = 'set_config'
            else:
                getattr(msg.set_module_config, self.REMOTE_ADMIN_ONEOF[section]).CopyFrom(new_body)
                what = 'set_module_config'
            if passkey:
                msg.session_passkey = passkey
            steps.append(f'Sent {what} ({section}): ' + ', '.join(f"{d['path']} {d['old']} -> {d['new']}" for d in diff))
            packet = self._ra_send(iface, dest, msg, timeout, steps, result, expect_reply=False)
        outcome = 'applied' if packet is not None else result.get('error', 'FAILED')
        try:
            self.stats_db.log_admin_action(dest, 'set', section, json.dumps(diff), outcome, result.get('rtt'))
        except Exception as e:
            print(f"[ADMIN] audit log failed: {e}")
        print(f"[ADMIN] set {section} on {dest}: {outcome} " + json.dumps(diff))
        if packet is None:
            return result
        result.update(ok=True, diff=diff)
        return result

    def remote_admin_reboot(self, dest, seconds=5, timeout=45):
        """Ask a remote node to reboot. Needs a session key, so it reads metadata first."""
        from meshtastic.protobuf import admin_pb2
        steps = []
        result = {'dest': dest, 'ok': False, 'steps': steps}
        if not self.remote_admin_write_enabled():
            result['error'] = 'WRITE_DISABLED'
            result['hint'] = 'Remote writing is not armed (Config > Remote admin, or remote_admin_write in config.json).'
            return result
        iface = self._ra_prepare(dest, 'metadata', result)
        if iface is None:
            return result
        seconds = max(1, min(60, int(seconds or 5)))
        with self._remote_admin_lock:
            if not self._ensure_remote_pubkey(iface, dest, steps):
                result['error'] = 'PKI_UNKNOWN_PUBKEY'
                return result
            if self._ra_read(iface, dest, 'metadata', timeout, steps, result) is None:
                return result
            msg = admin_pb2.AdminMessage()
            msg.reboot_seconds = seconds
            passkey = self._remote_admin_sessions.get(dest, (b'', 0))[0]
            if passkey:
                msg.session_passkey = passkey
            steps.append(f'Sent reboot_seconds={seconds}')
            packet = self._ra_send(iface, dest, msg, timeout, steps, result, expect_reply=False)
        outcome = 'applied' if packet is not None else result.get('error', 'FAILED')
        try:
            self.stats_db.log_admin_action(dest, 'reboot', None, json.dumps({'seconds': seconds}), outcome, result.get('rtt'))
        except Exception as e:
            print(f"[ADMIN] audit log failed: {e}")
        print(f"[ADMIN] reboot {dest} in {seconds} s: {outcome}")
        if packet is not None:
            result.update(ok=True, seconds=seconds)
        return result

    # Ports whose Python API parsers call log_packet_to_stats themselves.
    # Every other packet is counted once, at the router, by _log_unparsed_packet.
    STATS_PARSED_PORTS = frozenset(('NODEINFO_APP', 'POSITION_APP', 'TELEMETRY_APP', 'TEXT_MESSAGE_APP'))

    @staticmethod
    def _packet_radio_meta(packet):
        """(hops, snr, rssi, via_mqtt, relay_node_raw) of a Python API packet dict.

        MessageToDict leaves out zero-valued fields: a packet whose hop limit was
        used up arrives without hopLimit, which means 0, not unknown. A missing
        hopStart is unknown (firmware too old to send it).
        """
        hop_start = packet.get('hopStart')
        hops = (hop_start - (packet.get('hopLimit') or 0)) if hop_start is not None else None
        return (hops, packet.get('rxSnr'), packet.get('rxRssi') or None,
                bool(packet.get('viaMqtt', False)), packet.get('relayNode'))

    def _log_unparsed_packet(self, packet, portnum):
        """Count a packet no parser logs: ROUTING, TRACEROUTE, NEIGHBORINFO, RANGE_TEST,
        ADMIN, ..., and packets on a channel this node has no key for (ENCRYPTED).

        Until 2.9 only four ports reached stats.db, so Stats showed roughly a
        third of the traffic the radio actually decoded.
        """
        try:
            if isinstance(portnum, int):
                try:
                    portnum = portnums_pb2.PortNum.Name(portnum)
                except ValueError:
                    portnum = f'PORT_{portnum}'
            if not portnum:
                portnum = 'ENCRYPTED' if packet.get('encrypted') else 'UNKNOWN'
            if portnum in self.STATS_PARSED_PORTS:
                return
            from_id = sanitize_node_id(packet.get('fromId'))
            if not from_id and isinstance(packet.get('from'), int):
                from_id = f"!{packet['from']:08x}"
            if not from_id:
                return
            hops, snr, rssi, via_mqtt, relay_node_raw = self._packet_radio_meta(packet)
            self.log_packet_to_stats(from_id, portnum, hops, snr, rssi, via_mqtt, relay_node_raw)
        except Exception as e:
            print(f"[STATS] Could not log {portnum} packet: {e}")

    def _on_tcp_packet(self, packet):
        """Route an incoming TCP API packet to the appropriate parser."""
        portnum = packet.get('decoded', {}).get('portnum', '')
        self._log_unparsed_packet(packet, portnum)
        if portnum in ('NODEINFO_APP', portnums_pb2.PortNum.Value('NODEINFO_APP')):
            self.parse_node_info_from_packet(packet)
        elif portnum in ('POSITION_APP', portnums_pb2.PortNum.Value('POSITION_APP')):
            self.parse_position_from_packet(packet)
        elif portnum in ('TELEMETRY_APP', portnums_pb2.PortNum.Value('TELEMETRY_APP')):
            self.parse_telemetry_from_packet(packet)
        elif portnum in ('TEXT_MESSAGE_APP', portnums_pb2.PortNum.Value('TEXT_MESSAGE_APP')):
            self.parse_text_from_packet(packet)
        elif portnum in ('TRACEROUTE_APP', portnums_pb2.PortNum.Value('TRACEROUTE_APP')):
            self._handle_traceroute_packet(packet)
        elif portnum in ('NEIGHBORINFO_APP',):
            self._parse_neighbor_info(packet)

    # Fields learned from telemetry/position that must survive a NodeInfo rebuild
    NODE_STICKY_FIELDS = (
        'hw_model', 'short_name', 'is_licensed',
        'channel_util', 'air_util_tx', 'sats_in_view', 'env',
        'battery_level', 'voltage', 'uptime_seconds', 'temperature', 'rssi',
    )

    def _load_nodedb_from_device(self, iface):
        """Read NodeDB from the connected device and backfill node entries with
        hw_model, position, and device metrics. Only fills fields not already
        present from live radio packets. Called once after connect/reconnect."""
        try:
            db = iface.nodes
            if not db:
                print('[NODEDB] No nodes in device NodeDB')
                return
            added = updated = 0
            now = int(time.time())
            for raw in db.values():
                user = raw.get('user', {})
                node_id = user.get('id')
                if not node_id or node_id == self.local_node_id:
                    continue  # skip self

                name = sanitize_str(user.get('longName') or node_id, max_len=60)
                hw_model = sanitize_str(user.get('hwModel') or '', max_len=40) or None
                short_name = sanitize_str(user.get('shortName') or '', max_len=8) or None
                is_licensed = bool(user.get('isLicensed', False))
                self._remember_key(node_id, user.get('publicKey'), bool(raw.get('viaMqtt', False)))
                role_raw = user.get('role', 'CLIENT')
                role = role_raw if isinstance(role_raw, str) else 'CLIENT'

                pos = raw.get('position', {})
                lat = pos.get('latitude')
                lon = pos.get('longitude')
                alt = pos.get('altitude', 0)
                # The tracker's NodeDB remembers whatever a node once broadcast,
                # bogus coordinates included, and replays it on every connect —
                # so a position rejected on the packet path walks back in here
                # unless it is checked again.
                has_pos = (lat is not None and lon is not None
                           and not is_placeholder_position(lat, lon))

                dm = raw.get('deviceMetrics', {})
                battery = dm.get('batteryLevel')
                voltage = dm.get('voltage')
                ch_util = dm.get('channelUtilization')
                air_tx = dm.get('airUtilTx')
                uptime = dm.get('uptimeSeconds')

                if has_pos and node_id not in self.nodes:
                    # New node with GPS — add to nodes dict
                    entry = {
                        'id': node_id,
                        'name': name,
                        'lat': round(lat, 6),
                        'lon': round(lon, 6),
                        'alt': alt or 0,
                        'snr': None,
                        'role': role,
                        'hops': None,
                        'ts': now,
                        'seen_at': now,
                        'via_mqtt': False,
                        'hw_model': hw_model,
                        'short_name': short_name,
                        'is_licensed': is_licensed,
                        'source': 'nodedb'
                    }
                    if battery is not None: entry['battery_level'] = round(battery, 1)
                    if voltage is not None: entry['voltage'] = round(voltage, 2)
                    if ch_util is not None: entry['channel_util'] = round(ch_util, 1)
                    if air_tx is not None: entry['air_util_tx'] = round(air_tx, 2)
                    if uptime is not None: entry['uptime_seconds'] = uptime
                    self.nodes[node_id] = entry
                    added += 1

                elif has_pos and node_id in self.nodes:
                    # Existing GPS node — backfill missing sticky fields only
                    existing = self.nodes[node_id]
                    changed = False
                    for field, val in [
                        ('hw_model', hw_model), ('short_name', short_name),
                        ('is_licensed', is_licensed),
                        ('battery_level', round(battery, 1) if battery is not None else None),
                        ('voltage', round(voltage, 2) if voltage is not None else None),
                        ('channel_util', round(ch_util, 1) if ch_util is not None else None),
                        ('air_util_tx', round(air_tx, 2) if air_tx is not None else None),
                        ('uptime_seconds', uptime),
                    ]:
                        if val is not None and existing.get(field) is None:
                            existing[field] = val
                            changed = True
                    if changed:
                        updated += 1

                elif not has_pos and node_id not in self.nodes \
                        and node_id not in self.nodes_no_position:
                    # No GPS, not yet seen — add to no-position dict
                    entry = {
                        'id': node_id,
                        'name': name,
                        'snr': None,
                        'role': role,
                        'hops': None,
                        'via_mqtt': False,
                        'ts': now,
                        'seen_at': now,
                        'hw_model': hw_model,
                        'short_name': short_name,
                        'is_licensed': is_licensed,
                        'source': 'nodedb'
                    }
                    if battery is not None: entry['battery_level'] = round(battery, 1)
                    self.nodes_no_position[node_id] = entry
                    added += 1

                elif not has_pos and node_id in self.nodes_no_position:
                    # Existing no-GPS node — backfill hw fields only
                    existing = self.nodes_no_position[node_id]
                    for field, val in [
                        ('hw_model', hw_model), ('short_name', short_name),
                        ('is_licensed', is_licensed),
                    ]:
                        if val is not None and existing.get(field) is None:
                            existing[field] = val

            print(f'[NODEDB] Backfilled from device: {added} new, {updated} updated')
        except Exception as e:
            print(f'[NODEDB] Error reading NodeDB: {e}')

    def _carry_sticky_fields(self, target, prev):
        """Carry forward sticky fields from a node's previous entry into a freshly
        rebuilt entry, so a NodeInfo packet (which rebuilds the whole dict) does not
        wipe values learned from telemetry/position packets."""
        if not prev or not target:
            return
        for f in self.NODE_STICKY_FIELDS:
            if f not in target and prev.get(f) is not None:
                target[f] = prev[f]

    def inject_external_node(self, node_data):
        """Insert or update a node from an external source (plugin).
        Used by plugins (e.g. Meshcore) to add non-Meshtastic nodes to the
        main node store. Node becomes a first-class citizen: TTL cleanup,
        JSON persistence and WebSocket broadcast all apply automatically.
        Required fields: id, net. Optional: name, lat, lon, alt, role,
        snr, rssi and any extra fields (passed through as-is)."""
        try:
            if not isinstance(node_data, dict):
                print('[INJECT] rejected: node_data is not a dict')
                return False
            node_id = sanitize_str(node_data.get('id'), max_len=40) if node_data.get('id') else ''
            if not node_id:
                print('[INJECT] rejected: missing or empty id')
                return False
            net = sanitize_str(node_data.get('net'), max_len=8).upper() if node_data.get('net') else ''
            if not net:
                print(f'[INJECT] rejected: missing or empty net for {node_id}')
                return False

            name = sanitize_str(node_data.get('name'), max_len=50) if node_data.get('name') else node_id
            role = sanitize_str(node_data.get('role', 'CLIENT'), max_len=30) or 'CLIENT'

            lat = node_data.get('lat')
            lon = node_data.get('lon')
            has_pos = False
            if lat is not None and lon is not None:
                if (isinstance(lat, (int, float)) and not isinstance(lat, bool)
                        and isinstance(lon, (int, float)) and not isinstance(lon, bool)
                        and -90 <= lat <= 90 and -180 <= lon <= 180
                        and not (is_placeholder_position(lat, lon))):
                    lat = round(float(lat), 6)
                    lon = round(float(lon), 6)
                    has_pos = True
                else:
                    print(f'[INJECT] invalid coords for {node_id}, treating as no-position')

            now = int(time.time())
            entry = {
                'id': node_id,
                'name': name,
                'net': net,
                'role': role,
                'snr': node_data.get('snr'),
                'hops': None,
                'via_mqtt': False,
                'ts': now,
                'seen_at': now,
                'source': 'plugin'
            }
            if has_pos:
                alt = node_data.get('alt')
                entry['lat'] = lat
                entry['lon'] = lon
                entry['alt'] = alt if isinstance(alt, (int, float)) and not isinstance(alt, bool) else 0
            # Pass through extra fields (pubkey etc.) not already handled
            for k, v in node_data.items():
                if k in entry or k in ('lat', 'lon', 'alt') or v is None:
                    continue
                entry[k] = v

            prev = self.nodes.get(node_id) or self.nodes_no_position.get(node_id)
            is_new = prev is None
            self._carry_sticky_fields(entry, prev)
            # lat/lon/alt are not in NODE_STICKY_FIELDS — carry a previously
            # known position forward so a position-less update does not wipe it
            if not has_pos and prev and prev.get('lat') is not None and prev.get('lon') is not None:
                entry['lat'] = prev['lat']
                entry['lon'] = prev['lon']
                entry['alt'] = prev.get('alt', 0)
                has_pos = True

            if has_pos:
                self.nodes[node_id] = entry
                if node_id in self.nodes_no_position:
                    del self.nodes_no_position[node_id]
            else:
                self.nodes_no_position[node_id] = entry

            marker = '✚' if is_new else '↻'
            coords = f" @ {entry['lat']:.4f},{entry['lon']:.4f}" if has_pos else ''
            print(f"[INJECT] {marker} {node_id} ({net}) {name[:20]}{coords}")

            if has_pos:
                try:
                    asyncio.get_running_loop()
                    asyncio.create_task(self.broadcast_node_update(entry))
                except RuntimeError:
                    asyncio.run(self.broadcast_node_update(entry))
            return True
        except Exception as e:
            print(f'[INJECT] rejected: error injecting node: {e}')
        return False

    def parse_node_info_from_packet(self, packet):
        """Parse a NODEINFO_APP packet received via the Python API."""
        try:
            decoded = packet.get('decoded', {})
            user = decoded.get('user', {})
            node_id = sanitize_node_id(user.get('id') or packet.get('fromId'))
            if not node_id:
                return False

            name = sanitize_str(user.get('longName') or node_id, max_len=50)
            role = sanitize_str(user.get('role', 'CLIENT'), max_len=30)
            snr = packet.get('rxSnr', 0)
            via_mqtt = packet.get('viaMqtt', False)

            hop_start = packet.get('hopStart')
            hop_limit = packet.get('hopLimit')
            hops = (hop_start - (hop_limit or 0)) if hop_start is not None else None

            # Identity fields from NodeInfo user submessage
            hw_model = sanitize_str(user.get('hwModel') or '', max_len=40) or None
            short_name = sanitize_str(user.get('shortName') or '', max_len=8) or None
            is_licensed = bool(user.get('isLicensed', False))
            self._remember_key(node_id, user.get('publicKey'), via_mqtt)
            # Snapshot previous entry to preserve telemetry/position-injected fields
            prev = self.nodes.get(node_id) or self.nodes_no_position.get(node_id)

            pos = decoded.get('position', {})
            lat_i = pos.get('latitudeI')
            lon_i = pos.get('longitudeI')
            lat = pos.get('latitude') or (lat_i / 1e7 if lat_i is not None else None)
            lon = pos.get('longitude') or (lon_i / 1e7 if lon_i is not None else None)

            if lat is not None and lon is not None and not (is_placeholder_position(lat, lon)):
                alt = pos.get('altitude', 0)
                is_new = node_id not in self.nodes
                self.nodes[node_id] = {
                    'id': node_id,
                    'name': name,
                    'lat': round(lat, 6),
                    'lon': round(lon, 6),
                    'alt': alt,
                    'snr': round(snr, 1),
                    'role': role,
                    'hops': hops,
                    'ts': int(time.time()),
                    'seen_at': int(time.time()),
                    'via_mqtt': via_mqtt,
                    'hw_model': hw_model,
                    'short_name': short_name,
                    'is_licensed': is_licensed,
                    'source': 'live'
                }
                self._carry_sticky_fields(self.nodes[node_id], prev)
                # Remove from no-position dict if node now has GPS
                if node_id in self.nodes_no_position:
                    del self.nodes_no_position[node_id]
                    print(f"[GPS] {node_id} moved from no-GPS to GPS list")
                if node_id == self.local_node_id:
                    self.tracker_info['lat'] = round(lat, 6)
                    self.tracker_info['lon'] = round(lon, 6)
                    self.tracker_info['alt'] = alt or 0
                marker = "✚" if is_new else "↻"
                print(f"{marker} {node_id} {name[:20]} @ {lat:.4f},{lon:.4f} {self._packet_route_tag(packet)}")
                self._update_message_names(node_id, name)
                relay_node_raw = packet.get('relayNode')
                self.log_packet_to_stats(node_id, 'NODEINFO_APP', hops, snr, None, via_mqtt, relay_node_raw)
                asyncio.run(self.broadcast_node_update(self.nodes[node_id]))
                asyncio.run(self.broadcast_stats_update())
                if plugin_manager:
                    plugin_manager.dispatch_hook_sync('on_node_update', self.nodes.get(node_id))
            else:
                is_new = node_id not in self.nodes_no_position
                self.nodes_no_position[node_id] = {
                    'id': node_id,
                    'name': name,
                    'snr': round(snr, 1),
                    'role': role,
                    'hops': hops,
                    'via_mqtt': via_mqtt,
                    'ts': int(time.time()),
                    'seen_at': int(time.time()),
                    'hw_model': hw_model,
                    'short_name': short_name,
                    'is_licensed': is_licensed,
                    'source': 'live'
                }
                self._carry_sticky_fields(self.nodes_no_position[node_id], prev)
                marker = "✚" if is_new else "↻"
                print(f"{marker} {node_id} {name[:20]} (no GPS) {self._packet_route_tag(packet)}")
                self._update_message_names(node_id, name)
                relay_node_raw = packet.get('relayNode')
                self.log_packet_to_stats(node_id, 'NODEINFO_APP', hops, snr, None, via_mqtt, relay_node_raw)
                asyncio.run(self.broadcast_node_update(self.nodes_no_position[node_id]))
                asyncio.run(self.broadcast_stats_update())
                if plugin_manager:
                    plugin_manager.dispatch_hook_sync('on_node_update', self.nodes_no_position.get(node_id))
            return True
        except Exception as e:
            print(f"[ERROR] nodeinfo parse: {e}")
        return False

    def parse_position_from_packet(self, packet):
        """Parse a POSITION_APP packet received via the Python API."""
        try:
            node_id = sanitize_node_id(packet.get('fromId'))
            if not node_id:
                return False

            decoded = packet.get('decoded', {})
            pos = decoded.get('position', {})
            lat_i = pos.get('latitudeI')
            lon_i = pos.get('longitudeI')
            lat = pos.get('latitude') or (lat_i / 1e7 if lat_i is not None else None)
            lon = pos.get('longitude') or (lon_i / 1e7 if lon_i is not None else None)

            if lat is None or lon is None or (is_placeholder_position(lat, lon)):
                return False

            snr = packet.get('rxSnr', 0)
            rssi = packet.get('rxRssi') or None
            via_mqtt = packet.get('viaMqtt', False)
            hop_start = packet.get('hopStart')
            hop_limit = packet.get('hopLimit')
            hops = (hop_start - (hop_limit or 0)) if hop_start is not None else None
            relay_node_raw = packet.get('relayNode')
            sats = pos.get('satsInView')

            if node_id in self.nodes:
                self.nodes[node_id].update({
                    'lat': round(lat, 6),
                    'lon': round(lon, 6),
                    'snr': round(snr, 1),
                    'rssi': rssi,
                    'hops': hops,
                    'via_mqtt': via_mqtt,
                    'ts': int(time.time()),
                    'seen_at': int(time.time()),
                    'source': 'live'
                })
                if sats is not None:
                    self.nodes[node_id]['sats_in_view'] = sats
                print(f"↻ {node_id} position update @ {lat:.4f},{lon:.4f} hops={hops} {self._packet_route_tag(packet)}")
            else:
                self.nodes[node_id] = {
                    'id': node_id,
                    'name': (
                        self.nodes_no_position.get(node_id, {}).get('name') or
                        self.known_names.get(node_id) or
                        node_id
                    ),
                    'lat': round(lat, 6),
                    'lon': round(lon, 6),
                    'alt': pos.get('altitude', 0),
                    'snr': round(snr, 1),
                    'rssi': rssi,
                    'role': 'CLIENT',
                    'hops': hops,
                    'via_mqtt': via_mqtt,
                    'ts': int(time.time()),
                    'seen_at': int(time.time()),
                    'source': 'live'
                }
                # Remove from no-position dict if node now has GPS
                if node_id in self.nodes_no_position:
                    del self.nodes_no_position[node_id]
                    print(f"[GPS] {node_id} moved from no-GPS to GPS list")
                print(f"✚ {node_id} NEW from position @ {lat:.4f},{lon:.4f} hops={hops} {self._packet_route_tag(packet)}")

            if node_id == self.local_node_id:
                self.tracker_info['lat'] = round(lat, 6)
                self.tracker_info['lon'] = round(lon, 6)
                self.tracker_info['alt'] = pos.get('altitude', 0)

            self.log_packet_to_stats(node_id, 'POSITION_APP', hops, snr, rssi, via_mqtt, relay_node_raw)
            asyncio.run(self.broadcast_node_update(self.nodes[node_id]))
            asyncio.run(self.broadcast_stats_update())
            if plugin_manager:
                plugin_manager.dispatch_hook_sync('on_position', {
                    'node_id': node_id, 'lat': lat, 'lon': lon,
                    'alt': pos.get('altitude', 0),
                    'snr': snr, 'rssi': rssi, 'hops': hops,
                    'timestamp': int(time.time()),
                    'via_mqtt': bool(via_mqtt),
                    # Only present when the sender's position flags include them.
                    'speed': pos.get('groundSpeed'),                      # m/s
                    'heading': (pos['groundTrack'] / 1e5                  # degrees, true north
                                if pos.get('groundTrack') is not None else None),
                    # Position precision the sender chose for this channel (32 = full).
                    'precision_bits': pos.get('precisionBits'),
                    'pos_time': pos.get('time'),                          # sender's GPS time
                })
            return True
        except Exception as e:
            print(f"[ERROR] position parse: {e}")
        return False

    def parse_telemetry_from_packet(self, packet):
        """Parse a TELEMETRY_APP packet received via the Python API."""
        try:
            node_id = sanitize_node_id(packet.get('fromId'))
            if not node_id:
                return False

            decoded = packet.get('decoded', {})
            telemetry = decoded.get('telemetry', {})

            if node_id == self.local_node_id:
                tracker_updated = False

                device_metrics = telemetry.get('deviceMetrics', {})
                uptime = device_metrics.get('uptimeSeconds')
                if uptime is not None:
                    self.tracker_info['uptime_seconds'] = uptime
                    tracker_updated = True

                local_stats = telemetry.get('localStats', {})
                radio_fields = ['channelUtilization', 'airUtilTx', 'numPacketsTx', 'numPacketsRx',
                                 'numPacketsRxBad', 'numRxDupe', 'numTxRelay', 'numTxRelayCanceled',
                                 'numOnlineNodes', 'numTotalNodes', 'heapFreeBytes', 'heapTotalBytes']
                radio_stats = {f: local_stats[f] for f in radio_fields if f in local_stats}
                if not radio_stats:
                    # Fallback: some firmware reports these in deviceMetrics
                    radio_stats = {f: device_metrics[f] for f in radio_fields if f in device_metrics}
                if radio_stats:
                    self.tracker_info['radio_stats'] = radio_stats
                    # Only log localStats (has numTxRelay/numPacketsTx), not deviceMetrics every 60s
                    if 'numTxRelay' in radio_stats or 'numPacketsTx' in radio_stats:
                        self.stats_db.log_radio_stats(radio_stats)
                    tracker_updated = True

                if tracker_updated:
                    asyncio.run(self.broadcast_connection_status('connected'))

                # Also save battery/telemetry to nodes dict for popup display
                battery_level = device_metrics.get('batteryLevel')
                voltage = device_metrics.get('voltage')
                uptime_seconds = device_metrics.get('uptimeSeconds')
                env = telemetry.get('environmentMetrics', {})
                temperature = env.get('temperature')

                telemetry_update = {}
                if battery_level is not None:
                    telemetry_update['battery_level'] = round(battery_level, 1)
                    self.tracker_info['battery_level'] = round(battery_level, 1)
                if voltage is not None:
                    telemetry_update['voltage'] = round(voltage, 2)
                    self.tracker_info['voltage'] = round(voltage, 2)
                if uptime_seconds is not None:
                    telemetry_update['uptime_seconds'] = uptime_seconds
                if temperature is not None:
                    telemetry_update['temperature'] = round(temperature, 1)
                    self.tracker_info['temperature'] = round(temperature, 1)

                if telemetry_update and self.local_node_id in self.nodes:
                    self.nodes[self.local_node_id].update(telemetry_update)

            rssi = packet.get('rxRssi') or None
            if rssi is not None:
                if node_id in self.nodes:
                    self.nodes[node_id]['rssi'] = rssi
                elif node_id in self.nodes_no_position:
                    self.nodes_no_position[node_id]['rssi'] = rssi

            if node_id != self.local_node_id:
                hops, snr, _, via_mqtt, relay_node_raw = self._packet_radio_meta(packet)
                self.log_packet_to_stats(node_id, 'TELEMETRY_APP', hops, snr, rssi, via_mqtt, relay_node_raw)

            # Save device metrics for all nodes (not just tracker)
            device_metrics = telemetry.get('deviceMetrics', {})
            battery_level = device_metrics.get('batteryLevel')
            voltage = device_metrics.get('voltage')
            uptime_seconds = device_metrics.get('uptimeSeconds')

            env = telemetry.get('environmentMetrics', {})
            temperature = env.get('temperature')
            channel_util = device_metrics.get('channelUtilization')
            air_util_tx = device_metrics.get('airUtilTx')
            env_data = {k: (round(v, 2) if isinstance(v, (int, float)) else v)
                        for k, v in env.items() if v is not None}

            telemetry_update = {}
            if battery_level is not None:
                telemetry_update['battery_level'] = round(battery_level, 1)
            if voltage is not None:
                telemetry_update['voltage'] = round(voltage, 2)
            if uptime_seconds is not None:
                telemetry_update['uptime_seconds'] = uptime_seconds
            if temperature is not None:
                telemetry_update['temperature'] = round(temperature, 1)
            if channel_util is not None:
                telemetry_update['channel_util'] = round(channel_util, 1)
            if air_util_tx is not None:
                telemetry_update['air_util_tx'] = round(air_util_tx, 2)
            if env_data:
                telemetry_update['env'] = env_data

            if node_id in self.nodes:
                self.nodes[node_id]['ts'] = int(time.time())
                self.nodes[node_id]['seen_at'] = int(time.time())
                self.nodes[node_id]['source'] = 'live'
                if telemetry_update:
                    self.nodes[node_id].update(telemetry_update)
                print(f"♡ {node_id} telemetry heartbeat {self._packet_route_tag(packet)}")
                asyncio.run(self.broadcast_node_update(self.nodes[node_id]))
                asyncio.run(self.broadcast_stats_update())
                if plugin_manager:
                    plugin_manager.dispatch_hook_sync('on_telemetry', {
                        'node_id': node_id,
                        'battery': battery_level,
                        'voltage': voltage,
                        'channel_util': channel_util,
                        'air_util_tx': air_util_tx,
                        'snr': None,
                        'temperature': temperature,
                        'env': env_data,
                        'uptime': uptime_seconds,
                        'timestamp': int(time.time())
                    })
                return True
            elif node_id in self.nodes_no_position:
                self.nodes_no_position[node_id]['ts'] = int(time.time())
                self.nodes_no_position[node_id]['seen_at'] = int(time.time())
                self.nodes_no_position[node_id]['source'] = 'live'
                if telemetry_update:
                    self.nodes_no_position[node_id].update(telemetry_update)
                print(f"♡ {node_id} telemetry heartbeat (no GPS) {self._packet_route_tag(packet)}")
                asyncio.run(self.broadcast_node_update(self.nodes_no_position[node_id]))
                asyncio.run(self.broadcast_stats_update())
                if plugin_manager:
                    plugin_manager.dispatch_hook_sync('on_telemetry', {
                        'node_id': node_id,
                        'battery': battery_level,
                        'voltage': voltage,
                        'channel_util': channel_util,
                        'air_util_tx': air_util_tx,
                        'snr': None,
                        'temperature': temperature,
                        'env': env_data,
                        'uptime': uptime_seconds,
                        'timestamp': int(time.time())
                    })
            return True
        except Exception as e:
            print(f"[ERROR] telemetry parse: {e}")
        return False

    def parse_text_from_packet(self, packet):
        """Parse a TEXT_MESSAGE_APP packet received via the Python API."""
        try:
            from_id = sanitize_node_id(packet.get('fromId'))
            if not from_id:
                return False

            to_id = packet.get('toId', '^all')
            text = sanitize_message(packet.get('decoded', {}).get('text', ''))
            if not text:
                return False

            channel_index = packet.get('channel', 0)

            sender_name = from_id
            if from_id in self.nodes:
                sender_name = self.nodes[from_id].get('name', from_id)
            elif from_id in self.nodes_no_position:
                sender_name = self.nodes_no_position[from_id].get('name', from_id)
            elif from_id in self.known_names:
                sender_name = self.known_names[from_id]

            message = {
                'from_id': from_id,
                'from_name': sender_name,
                'to_id': to_id,
                'text': text,
                'timestamp': int(time.time()),
                'is_dm': to_id != '^all',
                'channel_index': channel_index,
                # True only for a DM encrypted and signed with the sender's PKI key
                # (firmware 2.5+); only then is from_id more than a claim.
                'pki': bool(packet.get('pkiEncrypted')),
            }

            if channel_index not in self.messages:
                self.messages[channel_index] = []
            self.messages[channel_index].insert(0, message)
            if len(self.messages[channel_index]) > 50:
                self.messages[channel_index] = self.messages[channel_index][:50]

            dm_marker = " [DM]" if message['is_dm'] else ""
            print(f"💬 [ch{channel_index}] {sender_name}: {text}{dm_marker} {self._packet_route_tag(packet)}")
            hops, snr, rssi, via_mqtt, relay_node_raw = self._packet_radio_meta(packet)
            self.log_packet_to_stats(from_id, 'TEXT_MESSAGE_APP', hops, snr, rssi, via_mqtt, relay_node_raw)
            asyncio.run(self.broadcast_message(message))
            if plugin_manager:
                plugin_manager.dispatch_hook_sync('on_message', message)
            return True
        except Exception as e:
            conn_label = self.connection_type.upper() if self.connection_type else 'TCP'
            print(f"[{conn_label}] text parse error: {e}")
        return False

    async def broadcast_node_update(self, node_data):
        """Broadcast node update to all connected WebSocket clients"""
        if not connected_clients:
            return

        message = safe_json({
            'type': 'node_update',
            'node': node_data,
            'timestamp': int(time.time())
        })

        # Broadcast to all connected clients
        try:
            message.encode('utf-8')
        except UnicodeEncodeError as e:
            print(f"[WS] Skipping node_update broadcast — UTF-8 validation failed: {e}")
            return
        websockets.broadcast(set(connected_clients), message)
        print(f"[WS] Broadcasted update for {node_data['id']} to {len(connected_clients)} clients")

    async def broadcast_node_deleted(self, node_id):
        """Broadcast node deletion to all connected WebSocket clients"""
        if not connected_clients:
            return

        message = safe_json({
            'type': 'node_deleted',
            'node_id': node_id,
            'timestamp': int(time.time())
        })

        # Broadcast to all connected clients
        try:
            message.encode('utf-8')
        except UnicodeEncodeError as e:
            print(f"[WS] Skipping node_deleted broadcast — UTF-8 validation failed: {e}")
            return
        websockets.broadcast(set(connected_clients), message)
        print(f"[WS] Broadcasted deletion for {node_id} to {len(connected_clients)} clients")

    async def broadcast_message(self, message_data):
        """Broadcast new text message to all connected WebSocket clients"""
        if not connected_clients:
            return

        message = safe_json({
            'type': 'new_message',
            'message': message_data,
            'timestamp': int(time.time())
        })

        # Broadcast to all connected clients
        try:
            message.encode('utf-8')
        except UnicodeEncodeError as e:
            print(f"[WS] Skipping new_message broadcast — UTF-8 validation failed: {e}")
            return
        websockets.broadcast(set(connected_clients), message)
        print(f"[WS] Broadcasted message from {message_data['from_id']} to {len(connected_clients)} clients")

    async def broadcast_stats_update(self):
        """Broadcast stats update (max distance, farthest node) to all connected WebSocket clients"""
        if not connected_clients:
            return

        max_dist, farthest_id = self.get_max_distance()
        try:
            relay_nodes = self.stats_db.get_relay_nodes_for_map(self.local_node_id)
        except Exception:
            relay_nodes = []

        message = safe_json({
            'type': 'stats_update',
            'max_distance_km': max_dist,
            'farthest_node': farthest_id,
            'max_distance_by_net': self.get_max_distance_by_net(),
            'network_reach_by_net': self.get_network_reach_by_net(),
            'relay_nodes': relay_nodes,
            'timestamp': int(time.time())
        })

        try:
            message.encode('utf-8')
        except UnicodeEncodeError as e:
            print(f"[WS] Skipping stats_update broadcast — UTF-8 validation failed: {e}")
            return
        websockets.broadcast(set(connected_clients), message)

    async def broadcast_connection_status(self, status, message=''):
        """Broadcast connection status to all connected WebSocket clients"""
        if not connected_clients:
            return
        msg = safe_json({
            'type': 'connection_status',
            'status': status,
            'message': str(message),
            'connection_type': self.connection_type,
            'host': self.host,
            'port': self.port,
            'tracker': getattr(self, 'tracker_info', {}),
            'mapper_version': MAPPER_VERSION,
            'timestamp': int(time.time())
        })
        try:
            msg.encode('utf-8')
        except UnicodeEncodeError as e:
            print(f"[WS] Skipping connection_status broadcast — UTF-8 validation failed: {e}")
            return
        websockets.broadcast(set(connected_clients), msg)
        print(f"[WS] Connection status: {status} ({self.connection_type})")

    async def broadcast_neighbor_update(self, from_id, from_name, neighbors):
        """Broadcast neighbor info update to all WebSocket clients."""
        if not connected_clients:
            return
        msg = safe_json({
            'type': 'neighbor_update',
            'from_id': from_id,
            'from_name': from_name,
            'neighbors': neighbors,
            'timestamp': int(time.time())
        })
        try:
            msg.encode('utf-8')
        except UnicodeEncodeError:
            return
        websockets.broadcast(set(connected_clients), msg)

    def _recompute_stats(self):
        if self._stats_computing:
            return
        self._stats_computing = True
        try:
            result = self.stats_db.get_stats_summary(self.local_node_id)
            result['local_node_id'] = self.local_node_id
            result['retention_days'] = self.stats_db.retention_days
            result['nodes'] = {
                nid: {'name': n.get('name', nid), 'role': n.get('role', 'CLIENT')}
                for nid, n in {**self.nodes, **self.nodes_no_position}.items()
            }
            result['tracker_info'] = getattr(self, 'tracker_info', {})

            # Geographic stats
            geo = {'farthest_node_id': None, 'farthest_node_name': None,
                   'farthest_dist_km': None, 'avg_direct_dist_km': None}
            if self.local_node_id and self.local_node_id in self.nodes:
                local = self.nodes[self.local_node_id]
                if local.get('lat') and local.get('lon'):
                    direct_dists = []
                    for nid, n in self.nodes.items():
                        if nid == self.local_node_id: continue
                        if n.get('hops') != 0 or n.get('via_mqtt'): continue
                        if not n.get('lat') or not n.get('lon'): continue
                        d = self.calculate_distance(local['lat'], local['lon'], n['lat'], n['lon'])
                        direct_dists.append((d, nid, n.get('name', nid)))
                    if direct_dists:
                        direct_dists.sort(reverse=True)
                        geo['farthest_dist_km'] = round(direct_dists[0][0], 2)
                        geo['farthest_node_id'] = direct_dists[0][1]
                        geo['farthest_node_name'] = direct_dists[0][2]
                        geo['avg_direct_dist_km'] = round(sum(d for d, _, _ in direct_dists) / len(direct_dists), 2)
            result['geo'] = geo

            # Enrich with current node names
            all_current_names = {}
            for nid, node in self.nodes.items():
                if node.get('name') and node['name'] != nid:
                    all_current_names[nid] = node['name']
            for nid, node in self.nodes_no_position.items():
                if node.get('name') and node['name'] != nid:
                    all_current_names[nid] = node['name']
            all_current_names.update(self.known_names)
            for sender in result.get('top_senders', []):
                if sender['from_id'] in all_current_names:
                    sender['from_name'] = all_current_names[sender['from_id']]
            for anomaly in result.get('anomalies', []):
                if anomaly.get('node_id') in all_current_names:
                    anomaly['node_name'] = all_current_names[anomaly['node_id']]

            result['neighbor_graph'] = self.stats_db.get_neighbor_graph()

            with self._stats_cache_lock:
                self._stats_cache = result
                self._stats_cache_ts = time.time()
            print(f"[STATS] Cache updated ({len(result.get('top_senders', []))} senders)")
        except Exception as e:
            print(f"[STATS] Recompute error: {e}")
        finally:
            self._stats_computing = False

    def _stats_refresh_loop(self):
        time.sleep(30)
        while True:
            try:
                hours = self.stats_db.rollup_traffic()
                if hours:
                    print(f"[STATS] Rolled up {hours} hour(s) of traffic")
            except Exception as e:
                print(f"[STATS] Rollup error: {e}")
            self._recompute_stats()
            time.sleep(60)

    def _dedup_nodes(self):
        """Remove any node from nodes_no_position that also exists in nodes (has GPS)."""
        duplicates = [nid for nid in self.nodes_no_position if nid in self.nodes]
        for nid in duplicates:
            del self.nodes_no_position[nid]
        if duplicates:
            print(f"[DEDUP] Removed {len(duplicates)} nodes from no-GPS that now have GPS")

    def save_nodes(self):
        """Save to JSON"""
        try:
            self._dedup_nodes()
            max_dist, farthest_id = self.get_max_distance()

            nodes_list = list(self.nodes.values())
            nodes_no_pos_list = list(self.nodes_no_position.values())

            # Add own tracker to nodes list if not already present
            if self.local_node_id and self.local_node_id not in self.nodes and self.local_node_id not in self.nodes_no_position:
                tracker_entry = {
                    'id': self.local_node_id,
                    'name': self.tracker_info.get('long_name') or self.tracker_info.get('node_id', self.local_node_id),
                    'role': self.tracker_info.get('role', 'CLIENT'),
                    'ts': int(time.time()),
                    'seen_at': int(time.time()),
                    'source': 'live',
                    'via_mqtt': False,
                    'hops': 0,
                    'snr': 0,
                }
                lat = self.tracker_info.get('lat')
                lon = self.tracker_info.get('lon')
                alt = self.tracker_info.get('alt', 0)
                if lat and lon:
                    tracker_entry['lat'] = lat
                    tracker_entry['lon'] = lon
                    tracker_entry['alt'] = alt or 0
                    nodes_list.append(tracker_entry)
                else:
                    nodes_no_pos_list.append(tracker_entry)
            else:
                # Update role and name on existing tracker node (may have changed since last run)
                tracker_long_name = (
                    self.tracker_info.get('long_name') or
                    self.local_node_id
                )
                if self.local_node_id in self.nodes:
                    self.nodes[self.local_node_id]['role'] = self.tracker_info.get('role', 'CLIENT')
                    self.nodes[self.local_node_id]['name'] = tracker_long_name
                elif self.local_node_id in self.nodes_no_position:
                    self.nodes_no_position[self.local_node_id]['role'] = self.tracker_info.get('role', 'CLIENT')
                    self.nodes_no_position[self.local_node_id]['name'] = tracker_long_name

            # Get relay stats for map visualization
            try:
                relay_nodes = self.stats_db.get_relay_nodes_for_map(self.local_node_id)
            except Exception:
                relay_nodes = []

            data = {
                'ts': int(time.time()),
                'updated': datetime.now().isoformat(),
                'cnt': len(nodes_list),
                'cnt_no_pos': len(nodes_no_pos_list),
                'max_distance_km': max_dist,
                'farthest_node': farthest_id,
                'max_distance_by_net': self.get_max_distance_by_net(),
                'network_reach_by_net': self.get_network_reach_by_net(),
                'tracker': getattr(self, 'tracker_info', {}),
                'nodes': nodes_list,
                'nodes_no_pos': nodes_no_pos_list,
                'messages': self.messages,
                'known_names': self.known_names,
                'neighbor_graph': self.stats_db.get_neighbor_graph(),
                'relay_nodes': relay_nodes,
                'ignored_nodes': list(self.ignored_nodes.values())
            }
            
            temp_path = self.json_path + '.tmp'
            with open(temp_path, 'w') as f:
                json.dump(data, f, indent=2)
            
            os.replace(temp_path, self.json_path)
            
            dist_info = f", max range: {max_dist} km to {farthest_id}" if max_dist else ""
            print(f"[SAVE] {len(self.nodes)} nodes + {len(self.nodes_no_position)} no-GPS → {self.json_path}{dist_info}")
            self.stats_db.cleanup_old_data()

        except Exception as e:
            print(f"Save error: {e}") 

    def _run_serial(self):
        """Run serial listener using Python Meshtastic API (mirrors _run_tcp)."""
        last_save = time.time()
        last_clean = time.time()
        save_interval = 60
        first_save_done = False
        first_save_delay = 10
        clean_interval = 3600
        restart_count = 0

        while True:
            serial_iface = None
            try:
                print(f"[SERIAL] Connecting to {self.port or 'auto-detect'} (attempt #{restart_count})...")
                serial_iface = SerialMeshtasticInterface(port=self.port)
                self._serial_iface = serial_iface

                def on_connection_established(iface):
                    node_info = iface.getMyNodeInfo()
                    if node_info:
                        node_id = node_info.get('user', {}).get('id')
                        if node_id:
                            self.local_node_id = node_id
                            print(f"[INFO] Local node ID: {node_id}")
                        # Read own tracker position from NodeDB at startup
                        try:
                            self.tracker_info['long_name'] = node_info.get('user', {}).get('longName', '')
                            pos = node_info.get('position', {})
                            lat = pos.get('latitude')
                            lon = pos.get('longitude')
                            alt = pos.get('altitude', 0)
                        except Exception:
                            pass
                        try:
                            role_int = iface.localNode.localConfig.device.role
                            role_map = {0: 'CLIENT', 1: 'CLIENT_MUTE', 2: 'ROUTER', 3: 'CLIENT_BASE',
                                        4: 'TRACKER', 5: 'SENSOR', 6: 'REPEATER', 7: 'TAK',
                                        8: 'TAK_TRACKER', 9: 'LOST_AND_FOUND', 11: 'ROUTER_LATE'}
                            tracker_role = role_map.get(int(role_int), 'CLIENT')
                            self.tracker_info['role'] = tracker_role
                        except Exception:
                            tracker_role = node_info.get('user', {}).get('role', 'CLIENT')
                            self.tracker_info['role'] = tracker_role
                        try:
                            if lat and lon and not (is_placeholder_position(lat, lon)):
                                self.tracker_info['lat'] = round(lat, 6)
                                self.tracker_info['lon'] = round(lon, 6)
                                self.tracker_info['alt'] = alt or 0
                                if self.local_node_id and self.local_node_id not in self.nodes:
                                    tracker_long_name = (
                                        node_info.get('user', {}).get('longName') or
                                        self.tracker_info.get('long_name') or
                                        self.local_node_id
                                    )
                                    self.nodes[self.local_node_id] = {
                                        'id': self.local_node_id,
                                        'name': tracker_long_name,
                                        'lat': round(lat, 6),
                                        'lon': round(lon, 6),
                                        'alt': alt or 0,
                                        'snr': 0,
                                        'role': tracker_role,
                                        'hops': 0,
                                        'ts': int(time.time()),
                                        'seen_at': int(time.time()),
                                        'via_mqtt': False,
                                        'source': 'live'
                                    }
                                    if self.local_node_id in self.nodes_no_position:
                                        del self.nodes_no_position[self.local_node_id]
                                print(f"[INFO] Tracker position loaded from NodeDB: {lat:.4f},{lon:.4f}")
                            else:
                                print(f"[INFO] Tracker has no position in NodeDB")
                        except Exception as e:
                            print(f"[INFO] Could not read tracker position from NodeDB: {e}")
                        self._load_nodedb_from_device(iface)
                    asyncio.run(self.broadcast_connection_status('connected', 'serial'))
                    self._last_radio_packet_time = time.time()

                serial_iface.connect(
                    on_receive=self._on_serial_packet,
                    on_connection_established=on_connection_established
                )
                print(f"[SERIAL] Connected successfully")
                if plugin_manager:
                    plugin_manager.set_interface(serial_iface)
                    self.notify_plugins_connected()
                self._last_radio_packet_time = time.time()

                # Backfill names in stats DB from loaded nodes
                all_known = {**self.nodes, **self.nodes_no_position}
                if all_known:
                    self.stats_db.backfill_names(all_known)
                    print(f"[STATS] Backfilled names for {len(all_known)} nodes")
                self._refresh_all_message_names()

                # Also refresh node names in memory from known_names
                refreshed = 0
                for node_id, node in self.nodes.items():
                    if node.get('name') == node_id and node_id in self.known_names:
                        node['name'] = self.known_names[node_id]
                        refreshed += 1
                for node_id, node in self.nodes_no_position.items():
                    if node.get('name') == node_id and node_id in self.known_names:
                        node['name'] = self.known_names[node_id]
                        refreshed += 1
                if refreshed:
                    print(f"[LOAD] Refreshed {refreshed} node names from known_names cache")

                last_save = time.time()
                first_save_done = False

                while True:
                    if restart_event.is_set():
                        print("[RESTART] Connection change requested, stopping serial listener...")
                        self.save_nodes()
                        return

                    current_interval = first_save_delay if not first_save_done else save_interval
                    if time.time() - last_save > current_interval:
                        self.save_nodes()
                        last_save = time.time()
                        first_save_done = True

                    if time.time() - last_clean > clean_interval:
                        self.clean_old_nodes()
                        self.save_nodes()
                        last_clean = time.time()

                    # Telemetry ping every 24h
                    if time.time() - self._last_telemetry_ping > 86400:
                        self._last_telemetry_ping = time.time()
                        try:
                            threading.Thread(target=self._send_telemetry_ping, daemon=True).start()
                        except Exception:
                            pass

                    # Health check — if no packet received for 60s after connection, assume disconnect
                    silence = time.time() - self._last_radio_packet_time
                    if silence > 60 and first_save_done:
                        print(f"[SERIAL] No packets for {int(silence)}s — assuming disconnect, reconnecting...")
                        raise Exception("Serial silent disconnect detected")

                    time.sleep(1)

            except KeyboardInterrupt:
                print("\n\n[STOP] Stopping by user request...")
                if self.nodes:
                    print("[SAVE] Final save before exit...")
                    self.save_nodes()
                print("[EXIT] Goodbye!")
                break

            except Exception as e:
                print(f"[SERIAL] Error: {e}, reconnecting in 10s...")
                import traceback
                traceback.print_exc()

                if restart_event.is_set():
                    self.save_nodes()
                    return

                if self.nodes:
                    self.save_nodes()

                if self._cli_reboot_pending:
                    print("[WAIT] Device rebooting after CLI config change, waiting 35 seconds...")
                    time.sleep(35)
                    self._cli_reboot_pending = False
                elif self._serial_iface is None:
                    print("[WAIT] Serial was released by CLI, retrying in 5 seconds...")
                    time.sleep(5)
                else:
                    print("[WAIT] Retrying in 10 seconds...")
                    time.sleep(10)
                restart_count += 1

            finally:
                if serial_iface is not None:
                    try:
                        serial_iface.disconnect()
                    except Exception:
                        pass
                try:
                    asyncio.run(self.broadcast_connection_status('disconnected', 'Serial disconnected — reconnecting...'))
                    self.notify_plugins_disconnected('Serial disconnected')
                except Exception:
                    pass
                self._serial_iface = None

    def _on_serial_packet(self, packet):
        """Route incoming serial packets to appropriate parsers (mirrors _on_tcp_packet)."""
        try:
            portnum = packet.get('decoded', {}).get('portnum', '')
            self._log_unparsed_packet(packet, portnum)
            if portnum in ('NODEINFO_APP', portnums_pb2.PortNum.Value('NODEINFO_APP')):
                self.parse_node_info_from_packet(packet)
            elif portnum in ('POSITION_APP', portnums_pb2.PortNum.Value('POSITION_APP')):
                self.parse_position_from_packet(packet)
            elif portnum in ('TELEMETRY_APP', portnums_pb2.PortNum.Value('TELEMETRY_APP')):
                self.parse_telemetry_from_packet(packet)
            elif portnum in ('TEXT_MESSAGE_APP', portnums_pb2.PortNum.Value('TEXT_MESSAGE_APP')):
                self.parse_text_from_packet(packet)
            elif portnum in ('TRACEROUTE_APP', portnums_pb2.PortNum.Value('TRACEROUTE_APP')):
                self._handle_traceroute_packet(packet)
            elif portnum in ('NEIGHBORINFO_APP',):
                self._parse_neighbor_info(packet)
            self._last_radio_packet_time = time.time()
        except Exception as e:
            print(f"[SERIAL] Packet routing error: {e}")

    def _packet_route_tag(self, packet):
        """Build route tag like '[USB→radio]', '[USB→relay×2]', '[USB→mqtt]'.

        Two dimensions:
        - Connection (USB/TCP): based on self.connection_type
        - Route (radio/relay×N/mqtt): based on packet's via_mqtt and hop_start/hop_limit
        """
        conn = 'USB' if self.connection_type == 'serial' else 'TCP'

        via_mqtt = packet.get('viaMqtt') or packet.get('via_mqtt') or False
        if via_mqtt:
            route = 'mqtt'
        else:
            hop_start = packet.get('hopStart') or packet.get('hop_start') or 0
            hop_limit = packet.get('hopLimit') or packet.get('hop_limit') or 0
            if hop_start and hop_limit is not None and hop_start > hop_limit:
                hops = hop_start - hop_limit
                route = f'relay×{hops}'
            else:
                route = 'radio'

        return f'[{conn}→{route}]'

    def _handle_traceroute_packet(self, packet):
        """Handle incoming traceroute response packet from Python API."""
        from_num = packet.get('from', 0)
        to_num = packet.get('to', 0)
        print(f"[TRACEROUTE] ← Callback fired: from=!{from_num:08x} to=!{to_num:08x}")
        try:
            decoded = packet.get('decoded', {})
            # Python API puts data in 'traceroute' field, not 'routeDiscovery'
            tr = decoded.get('traceroute', decoded.get('routeDiscovery', {}))

            route_nums = tr.get('route', [])
            snr_towards = tr.get('snrTowards', [])
            route_back_nums = tr.get('routeBack', [])
            snr_back = tr.get('snrBack', [])

            all_known = {**self.nodes, **self.nodes_no_position}

            def num_to_hop(num, snr=None):
                hex_id = f"!{num:08x}"
                node = all_known.get(hex_id, {})
                return {
                    'id': hex_id,
                    'name': node.get('name', hex_id),
                    'lat': node.get('lat'),
                    'lon': node.get('lon'),
                    'snr': round(snr / 4.0, 2) if snr is not None else None
                }

            # Full route: our node -> intermediate hops -> destination
            full_route = [num_to_hop(from_num)]
            for i, num in enumerate(route_nums):
                snr = snr_towards[i] if i < len(snr_towards) else None
                full_route.append(num_to_hop(num, snr))
            full_route.append(num_to_hop(to_num, snr_towards[-1] if snr_towards else None))

            # Full route back: destination -> intermediate hops -> our node
            full_route_back = [num_to_hop(to_num)]
            for i, num in enumerate(route_back_nums):
                snr = snr_back[i] if i < len(snr_back) else None
                full_route_back.append(num_to_hop(num, snr))
            full_route_back.append(num_to_hop(from_num, snr_back[-1] if snr_back else None))

            self._pending_traceroute_result = {
                'route': full_route,
                'route_back': full_route_back,
                'node_id': f"!{to_num:08x}"
            }
            print(f"[TRACEROUTE] Result parsed: {len(full_route)} hops forward, {len(full_route_back)} hops back")
        except Exception as e:
            import traceback
            print(f"[TRACEROUTE] Error parsing packet: {e}")
            print(f"[TRACEROUTE] Traceback:\n{traceback.format_exc()}")
            print(f"[TRACEROUTE] Raw packet keys: {list(packet.keys())}")
            decoded_keys = list(packet.get('decoded', {}).keys()) if isinstance(packet.get('decoded'), dict) else 'not-a-dict'
            print(f"[TRACEROUTE] Decoded keys: {decoded_keys}")

        # Wake up any waiting coroutine in the asyncio event loop
        if self._traceroute_event is not None and self._traceroute_loop is not None:
            try:
                self._traceroute_loop.call_soon_threadsafe(self._traceroute_event.set)
                print(f"[TRACEROUTE] Event signaled to event loop")
            except Exception as e:
                print(f"[TRACEROUTE] Failed to signal event: {e}")

    def _run_tcp(self):
        """Run TCP listener using the Python Meshtastic API (no subprocess)."""
        last_save = time.time()
        last_clean = time.time()
        save_interval = 60
        first_save_done = False
        first_save_delay = 10
        clean_interval = 3600
        restart_count = 0

        tcp_host, tcp_port = parse_tcp_target(self.host)
        if tcp_port is None:
            print(f"[TCP] Invalid port in '{self.host}', must be 1-65535")
            try:
                asyncio.run(self.broadcast_connection_status(
                    'failed', f"Invalid port in '{self.host}', must be 1-65535"))
            except Exception:
                pass
            # Bad target won't fix itself — wait for a connection change from the web UI
            restart_event.wait()
            return

        while True:
            tcp_iface = None
            try:
                print(f"[TCP] Connecting to {tcp_host}:{tcp_port} (attempt #{restart_count})...")
                tcp_iface = TCPMeshtasticInterface(tcp_host, tcp_port)
                self._tcp_iface = tcp_iface
                tcp_iface.connect(self._on_tcp_packet)
                print(f"[TCP] Connected to {tcp_host}:{tcp_port}")
                if plugin_manager:
                    plugin_manager.set_interface(tcp_iface)
                    self.notify_plugins_connected()
                self._last_radio_packet_time = time.time()

                # Read own tracker position from NodeDB at startup
                try:
                    my_info = tcp_iface.getMyNodeInfo()
                    pos = my_info.get('position', {})
                    lat = pos.get('latitude')
                    lon = pos.get('longitude')
                    alt = pos.get('altitude', 0)
                    self.tracker_info['long_name'] = my_info.get('user', {}).get('longName', '')
                    # Read real role from localConfig
                    try:
                        role_int = tcp_iface.localNode.localConfig.device.role
                        role_map = {0: 'CLIENT', 1: 'CLIENT_MUTE', 2: 'ROUTER', 3: 'CLIENT_BASE',
                                    4: 'TRACKER', 5: 'SENSOR', 6: 'REPEATER', 7: 'TAK',
                                    8: 'TAK_TRACKER', 9: 'LOST_AND_FOUND', 11: 'ROUTER_LATE'}
                        tracker_role = role_map.get(int(role_int), 'CLIENT')
                        self.tracker_info['role'] = tracker_role
                    except Exception:
                        tracker_role = my_info.get('user', {}).get('role', 'CLIENT')
                        self.tracker_info['role'] = tracker_role
                    if lat and lon and not (is_placeholder_position(lat, lon)):
                        self.tracker_info['lat'] = round(lat, 6)
                        self.tracker_info['lon'] = round(lon, 6)
                        self.tracker_info['alt'] = alt or 0
                        if self.local_node_id and self.local_node_id not in self.nodes:
                            tracker_long_name = (
                                my_info.get('user', {}).get('longName') or
                                self.tracker_info.get('long_name') or
                                self.local_node_id
                            )
                            self.nodes[self.local_node_id] = {
                                'id': self.local_node_id,
                                'name': tracker_long_name,
                                'lat': round(lat, 6),
                                'lon': round(lon, 6),
                                'alt': alt or 0,
                                'snr': 0,
                                'role': tracker_role,
                                'hops': 0,
                                'ts': int(time.time()),
                                'seen_at': int(time.time()),
                                'via_mqtt': False,
                                'source': 'live'
                            }
                            if self.local_node_id in self.nodes_no_position:
                                del self.nodes_no_position[self.local_node_id]
                        print(f"[INFO] Tracker position loaded from NodeDB: {lat:.4f},{lon:.4f}")
                    else:
                        print(f"[INFO] Tracker has no position in NodeDB")
                except Exception as e:
                    print(f"[INFO] Could not read tracker position from NodeDB: {e}")

                self._load_nodedb_from_device(tcp_iface)

                # Backfill names in stats DB from loaded nodes
                all_known = {**self.nodes, **self.nodes_no_position}
                if all_known:
                    self.stats_db.backfill_names(all_known)
                    print(f"[STATS] Backfilled names for {len(all_known)} nodes")
                self._refresh_all_message_names()

                # Also refresh node names in memory from known_names
                refreshed = 0
                for node_id, node in self.nodes.items():
                    if node.get('name') == node_id and node_id in self.known_names:
                        node['name'] = self.known_names[node_id]
                        refreshed += 1
                for node_id, node in self.nodes_no_position.items():
                    if node.get('name') == node_id and node_id in self.known_names:
                        node['name'] = self.known_names[node_id]
                        refreshed += 1
                if refreshed:
                    print(f"[LOAD] Refreshed {refreshed} node names from known_names cache")

                last_save = time.time()
                first_save_done = False

                while True:
                    if restart_event.is_set():
                        print("[RESTART] Connection change requested, stopping TCP listener...")
                        self.save_nodes()
                        return

                    current_interval = first_save_delay if not first_save_done else save_interval
                    if time.time() - last_save > current_interval:
                        self.save_nodes()
                        last_save = time.time()
                        first_save_done = True

                    if time.time() - last_clean > clean_interval:
                        self.clean_old_nodes()
                        self.save_nodes()
                        last_clean = time.time()

                    # Telemetry ping every 24h
                    if time.time() - self._last_telemetry_ping > 86400:
                        self._last_telemetry_ping = time.time()
                        try:
                            threading.Thread(target=self._send_telemetry_ping, daemon=True).start()
                        except Exception:
                            pass

                    # Health check — if no packet received for 90s after connection, assume disconnect
                    silence = time.time() - self._last_radio_packet_time
                    if silence > 90 and first_save_done:
                        print(f"[TCP] No packets for {int(silence)}s — assuming disconnect, reconnecting...")
                        raise Exception("TCP silent disconnect detected")

                    time.sleep(1)

            except KeyboardInterrupt:
                print("\n\n[STOP] Stopping by user request...")
                if self.nodes:
                    print("[SAVE] Final save before exit...")
                    self.save_nodes()
                print("[EXIT] Goodbye!")
                break

            except OSError as e:
                if restart_count == 0:
                    print(f"[TCP] First attempt connection reset (Heltec V3 quirk), waiting 5s...")
                else:
                    print(f"[TCP] Connection error: {e}")
                time.sleep(5)
                restart_count += 1
                continue

            except Exception as e:
                if restart_count == 0:
                    print(f"[TCP] First attempt failed (normal for Heltec V3), retrying...")
                else:
                    print(f"[TCP] Error: {e}")
                    import traceback
                    traceback.print_exc()

                if restart_event.is_set():
                    self.save_nodes()
                    return

                if self.nodes:
                    self.save_nodes()

                if self._cli_reboot_pending:
                    print("[WAIT] Device rebooting after CLI config change, waiting 35 seconds...")
                    time.sleep(35)
                    self._cli_reboot_pending = False
                elif self._serial_iface is None:
                    print("[WAIT] Serial was released by CLI, retrying in 5 seconds...")
                    time.sleep(5)
                else:
                    print("[WAIT] Retrying in 10 seconds...")
                    time.sleep(10)
                restart_count += 1

            finally:
                if tcp_iface is not None:
                    try:
                        tcp_iface.disconnect()
                    except Exception:
                        pass
                try:
                    asyncio.run(self.broadcast_connection_status('disconnected', 'TCP disconnected — reconnecting...'))
                    self.notify_plugins_disconnected('TCP disconnected')
                except Exception:
                    pass

    def run(self):
        """Run meshtastic --listen and parse output"""
        print("=" * 60)
        print(f"MeshPulse - LISTEN MODE v{MAPPER_VERSION}")
        print("Continuous monitoring with auto-restart")
        print("=" * 60)
        print(f"Node TTL: {self.max_age//3600} hours")
        print(f"Current nodes in memory: {len(self.nodes)}")
        print(f"WebSocket server: ws://0.0.0.0:8765")
        print("=" * 60)

        if self.connection_type == 'tcp':
            self._run_tcp()
            return

        if self.connection_type == 'serial':
            self._run_serial()
            return

        cmd = [self.meshtastic_cmd, '--port', self.port, '--listen']

        print(f"Command: {' '.join(cmd)}")
        print("Press Ctrl+C to stop\n")
        
        last_save = time.time()
        last_clean = time.time()
        save_interval = 60  # Save every minute
        first_save_done = False
        first_save_delay = 10  # First save after 10 seconds
        clean_interval = 3600  # Clean every hour
        restart_count = 0
        
        while True:
            try:
                print(f"[START] Starting listener (restart #{restart_count})...")
                last_save = time.time()
                first_save_done = False
                
                # Start process
                self.current_process = None
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1
                )
                self.current_process = process

                # Read output line by line
                for line in iter(process.stdout.readline, ''):
                    if not line:
                        break
                        
                    line = line.strip()
                    
                    # Show important lines
                    if 'nodeinfo' in line.lower() or 'connection' in line.lower():
                        display = line[:100] + '...' if len(line) > 100 else line
                        print(f"[RECV] {display}")
                    
                    # Parse node info, position updates, telemetry, and text messages
                    self.parse_node_info(line)
                    self.parse_position_update(line)
                    self.parse_telemetry_update(line)
                    self.parse_text_message(line)
                    
                    # Save periodically - first save after 10s, then every 60s
                    current_interval = first_save_delay if not first_save_done else save_interval
                    if time.time() - last_save > current_interval:
                        self.save_nodes()
                        last_save = time.time()
                        first_save_done = True
                    
                    # Clean old nodes periodically
                    if time.time() - last_clean > clean_interval:
                        self.clean_old_nodes()
                        self.save_nodes()
                        last_clean = time.time()

                    # Check for connection change request
                    if restart_event.is_set():
                        print("[RESTART] Connection change requested, stopping listener...")
                        try:
                            process.terminate()
                            process.wait(timeout=5)
                        except Exception:
                            process.kill()
                        self.current_process = None
                        self.save_nodes()
                        return

                # Process ended
                self.current_process = None
                return_code = process.wait()
                print(f"[WARN] Process ended with code {return_code}")

                # Check if restart was requested
                if restart_event.is_set():
                    self.save_nodes()
                    return

                # Final save before restart
                if self.nodes:
                    self.save_nodes()

                # Wait before restart
                print("[WAIT] Restarting in 10 seconds...")
                time.sleep(10)
                restart_count += 1
                
            except KeyboardInterrupt:
                print("\n\n[STOP] Stopping by user request...")
                try:
                    process.terminate()
                    process.wait(timeout=5)
                except:
                    process.kill()
                
                # Final save
                if self.nodes:
                    print("[SAVE] Final save before exit...")
                    self.save_nodes()
                
                print("[EXIT] Goodbye!")
                break
            
            except Exception as e:
                print(f"[ERROR] {e}")
                import traceback
                traceback.print_exc()
                
                # Wait before retry
                print("[WAIT] Retrying in 30 seconds...")
                time.sleep(30)
                restart_count += 1


def parse_traceroute_output(output):
    """Parse meshtastic traceroute output into route and route_back hop lists.
    Handles formats like:
      NodeA --> NodeB (8.5 dB SNR) --> NodeC (4.0 dB SNR)
      Route back: NodeC --> NodeB (5.0 dB SNR) --> NodeA
    Returns (route, route_back) as lists of {'name', 'id', 'snr'} dicts.
    """
    route = []
    route_back = []

    for line in output.split('\n'):
        line = line.strip()
        if '-->' not in line:
            continue

        is_back = 'back' in line.lower()

        # Strip common prefixes
        hop_part = line
        for prefix in ['Route back:', 'Route back', 'Route to', 'Route:']:
            if hop_part.lower().startswith(prefix.lower()):
                hop_part = hop_part[len(prefix):].lstrip(':').strip()
                break
        # Strip trailing colon separated prefix "!xxxx: hop --> ..."
        colon_idx = hop_part.find(':')
        if colon_idx != -1 and '-->' not in hop_part[:colon_idx]:
            hop_part = hop_part[colon_idx + 1:].strip()

        parts = hop_part.split('-->')
        hops = []
        for part in parts:
            part = part.strip()
            if not part:
                continue

            # Extract SNR value
            snr = None
            snr_match = re.search(r'\(\s*(?:snr\s*:?\s*)?([-\d.]+)\s*(?:dB)?\s*(?:SNR)?\s*\)', part, re.IGNORECASE)
            if snr_match:
                try:
                    snr = float(snr_match.group(1))
                except ValueError:
                    pass

            # Clean name by removing SNR annotation
            name = re.sub(r'\s*\([^)]*\)\s*$', '', part).strip()

            # Extract node ID if present (starts with !)
            node_id = None
            id_match = re.search(r'(![\da-f]{4,8})', name, re.IGNORECASE)
            if id_match:
                node_id = id_match.group(1).lower()

            hop = {'name': name, 'snr': snr}
            if node_id:
                hop['id'] = node_id
            hops.append(hop)

        if hops:
            if is_back:
                route_back = hops
            else:
                route = hops

    return route, route_back


async def run_send_message(text, channel_index, dest_id, websocket):
    """Send a text message.
    TCP mode: uses a dedicated TCPInterface (no listener stop needed).
    Serial mode: sends via Python API (no listener stop needed).
    """
    try:
        if not mapper:
            await websocket.send(json.dumps({'type': 'send_result', 'success': False, 'message': 'Mapper not ready'}, ensure_ascii=False))
            return

        await websocket.send(json.dumps({
            'type': 'send_status', 'status': 'sending', 'connection_type': mapper.connection_type
        }, ensure_ascii=False))

        if mapper.connection_type == 'tcp':
            # TCP: create a separate TCPInterface just for sending; do NOT stop listener
            loop = asyncio.get_event_loop()

            def _do_tcp_send():
                tcp_host, tcp_port = parse_tcp_target(mapper.host)
                if tcp_port is None:
                    return False, f"Invalid port in '{mapper.host}', must be 1-65535"
                iface = meshtastic.tcp_interface.TCPInterface(
                    hostname=tcp_host, portNumber=tcp_port, noProto=False
                )
                try:
                    iface.sendText(
                        text,
                        channelIndex=channel_index,
                        destinationId=dest_id if dest_id else '^all'
                    )
                    return True, 'Message sent'
                except Exception as exc:
                    return False, str(exc)
                finally:
                    try:
                        iface.close()
                    except Exception:
                        pass

            try:
                success, msg = await asyncio.wait_for(
                    loop.run_in_executor(None, _do_tcp_send),
                    timeout=30
                )
            except asyncio.TimeoutError:
                success, msg = False, 'Send timed out'

            print(f"[SEND] TCP ch={channel_index} dest={dest_id or 'broadcast'}: {'OK' if success else 'FAIL'} - {msg}")
            await websocket.send(json.dumps({'type': 'send_result', 'success': success, 'message': msg}, ensure_ascii=False))
            await websocket.send(json.dumps({'type': 'send_status', 'status': 'done', 'success': success}, ensure_ascii=False))

            if success:
                sent_message = {
                    'from_id': mapper.local_node_id,
                    'from_name': 'You',
                    'to_id': dest_id if dest_id else '^all',
                    'text': text,
                    'timestamp': int(time.time()),
                    'is_dm': bool(dest_id),
                    'channel_index': channel_index
                }
                if channel_index not in mapper.messages:
                    mapper.messages[channel_index] = []
                mapper.messages[channel_index].insert(0, sent_message)
                await mapper.broadcast_message(sent_message)

            # No listener restart needed for TCP
            return

        # Serial/USB: send via Python API (no listener stop needed)
        loop = asyncio.get_event_loop()

        def _do_serial_send():
            if not mapper._serial_iface:
                raise Exception("Not connected")
            mapper._serial_iface.sendText(
                text,
                channelIndex=channel_index,
                destinationId=dest_id if dest_id else '^all'
            )
            return True, 'Message sent'

        try:
            success, msg = await asyncio.wait_for(
                loop.run_in_executor(None, _do_serial_send),
                timeout=30
            )
        except asyncio.TimeoutError:
            success, msg = False, 'Send timed out'

        print(f"[SEND] Serial ch={channel_index} dest={dest_id or 'broadcast'}: {'OK' if success else 'FAIL'} - {msg}")
        await websocket.send(json.dumps({'type': 'send_result', 'success': success, 'message': msg}, ensure_ascii=False))
        await websocket.send(json.dumps({'type': 'send_status', 'status': 'done', 'success': success}, ensure_ascii=False))

        if success:
            sent_message = {
                'from_id': mapper.local_node_id,
                'from_name': 'You',
                'to_id': dest_id if dest_id else '^all',
                'text': text,
                'timestamp': int(time.time()),
                'is_dm': bool(dest_id),
                'channel_index': channel_index
            }
            if channel_index not in mapper.messages:
                mapper.messages[channel_index] = []
            mapper.messages[channel_index].insert(0, sent_message)
            await mapper.broadcast_message(sent_message)

    except asyncio.TimeoutError:
        await websocket.send(json.dumps({'type': 'send_result', 'success': False, 'message': 'Send timed out'}, ensure_ascii=False))
        await websocket.send(json.dumps({'type': 'send_status', 'status': 'done', 'success': False}, ensure_ascii=False))
    except Exception as e:
        print(f"[SEND] Error: {e}")
        await websocket.send(json.dumps({'type': 'send_result', 'success': False, 'message': str(e)}, ensure_ascii=False))
        await websocket.send(json.dumps({'type': 'send_status', 'status': 'done', 'success': False}, ensure_ascii=False))


async def run_traceroute(node_id, websocket):
    """Run meshtastic --traceroute and send result to the requesting WebSocket client.
    For serial: stops listener subprocess first, restarts after.
    For TCP: runs in parallel without interrupting listener.
    """
    global traceroute_restart
    try:
        if not mapper:
            await websocket.send(json.dumps({
                'type': 'traceroute_result', 'node_id': node_id, 'error': 'Mapper not ready'
            }, ensure_ascii=False))
            return

        conn_type = mapper.connection_type

        # Notify frontend that traceroute is starting
        await websocket.send(json.dumps({
            'type': 'traceroute_status',
            'status': 'starting',
            'connection_type': conn_type
        }, ensure_ascii=False))

        loop = asyncio.get_event_loop()

        if conn_type == 'serial':
            try:
                if not mapper._serial_iface or not mapper._serial_iface.iface:
                    print(f"[TRACEROUTE] Aborting: serial interface not connected")
                    await websocket.send(json.dumps({
                        'type': 'traceroute_result',
                        'node_id': node_id, 'error': 'Not connected'
                    }, ensure_ascii=False))
                    return

                # Lazy imports — protobufy meshtastic
                from meshtastic import mesh_pb2, portnums_pb2

                import time as _time_module
                MAX_ATTEMPTS = 2          # original + 1 retry
                PER_ATTEMPT_TIMEOUT = 45  # sekundy na pojedynczą próbę
                iface = mapper._serial_iface.iface

                final_result = None
                final_error = None
                tr_total_start = _time_module.time()

                for attempt in range(1, MAX_ATTEMPTS + 1):
                    print(f"[TRACEROUTE] === Attempt {attempt}/{MAX_ATTEMPTS} for {node_id} ===")

                    # Notify frontend about attempt number (best effort, ignore errors)
                    try:
                        await websocket.send(json.dumps({
                            'type': 'traceroute_status',
                            'status': 'attempt',
                            'attempt': attempt,
                            'total_attempts': MAX_ATTEMPTS,
                            'timeout_per_attempt': PER_ATTEMPT_TIMEOUT
                        }, ensure_ascii=False))
                    except Exception:
                        pass

                    # Reset state przed każdą próbą
                    mapper._pending_traceroute_result = None
                    mapper._traceroute_event = asyncio.Event()
                    mapper._traceroute_loop = loop
                    mapper._traceroute_expected_from = node_id

                    attempt_start = _time_module.time()

                    # Wyślij pakiet RouteDiscovery (non-blocking)
                    try:
                        r = mesh_pb2.RouteDiscovery()
                        await loop.run_in_executor(
                            None,
                            lambda: iface.sendData(
                                r,
                                destinationId=node_id,
                                portNum=portnums_pb2.PortNum.TRACEROUTE_APP,
                                wantResponse=True,
                                hopLimit=5
                            )
                        )
                        send_elapsed = _time_module.time() - attempt_start
                        print(f"[TRACEROUTE] → Packet sent in {send_elapsed:.1f}s, waiting for response (max {PER_ATTEMPT_TIMEOUT}s)...")
                    except Exception as send_err:
                        import traceback
                        send_elapsed = _time_module.time() - attempt_start
                        print(f"[TRACEROUTE] ✗ sendData() failed after {send_elapsed:.1f}s: {type(send_err).__name__}: {send_err}")
                        print(f"[TRACEROUTE] Traceback:\n{traceback.format_exc()}")
                        final_error = f"Send failed: {send_err}"
                        continue

                    # Czekaj na event (wybudzony z callbacka) z naszym timeoutem
                    try:
                        await asyncio.wait_for(
                            mapper._traceroute_event.wait(),
                            timeout=PER_ATTEMPT_TIMEOUT
                        )
                        attempt_elapsed = _time_module.time() - attempt_start
                        print(f"[TRACEROUTE] ✓ Event received after {attempt_elapsed:.1f}s")

                        if mapper._pending_traceroute_result:
                            final_result = mapper._pending_traceroute_result
                            mapper._pending_traceroute_result = None
                            print(f"[TRACEROUTE] ✓ SUCCESS on attempt {attempt} after {attempt_elapsed:.1f}s")
                            break
                        else:
                            print(f"[TRACEROUTE] ⚠ Event fired but no _pending_traceroute_result — strange")
                            final_error = "Event fired but no result captured"
                    except asyncio.TimeoutError:
                        attempt_elapsed = _time_module.time() - attempt_start
                        print(f"[TRACEROUTE] ✗ Attempt {attempt} timed out after {attempt_elapsed:.1f}s")
                        final_error = "Timeout - no traceroute response received"
                        if attempt < MAX_ATTEMPTS:
                            print(f"[TRACEROUTE] Will retry...")
                            await asyncio.sleep(2)

                # Cleanup state
                mapper._traceroute_event = None
                mapper._traceroute_loop = None
                mapper._traceroute_expected_from = None

                total_elapsed = _time_module.time() - tr_total_start

                if final_result:
                    print(f"[TRACEROUTE] === DONE in {total_elapsed:.1f}s total ===")
                    all_known = {**mapper.nodes, **mapper.nodes_no_position}
                    for hop in final_result['route'] + final_result['route_back']:
                        hop_id = hop.get('id')
                        if hop_id and hop_id in all_known:
                            n = all_known[hop_id]
                            if 'lat' in n:
                                hop['lat'] = n['lat']
                                hop['lon'] = n['lon']
                            hop['name'] = n.get('name', hop.get('name', hop_id))
                    await websocket.send(json.dumps({
                        'type': 'traceroute_result',
                        'node_id': node_id,
                        'route': final_result['route'],
                        'route_back': final_result['route_back'],
                        'raw': ''
                    }, ensure_ascii=False))
                else:
                    print(f"[TRACEROUTE] === FAILED after {total_elapsed:.1f}s, {MAX_ATTEMPTS} attempts ===")
                    # Construct detailed error message for frontend
                    if final_error and 'imeout' in final_error:
                        detailed_error = f'No response after {MAX_ATTEMPTS} attempts ({total_elapsed:.0f}s total)'
                    else:
                        detailed_error = final_error or 'Unknown error'
                    await websocket.send(json.dumps({
                        'type': 'traceroute_result',
                        'node_id': node_id,
                        'error': detailed_error
                    }, ensure_ascii=False))

            except Exception as e:
                import traceback
                mapper._traceroute_event = None
                mapper._traceroute_loop = None
                mapper._traceroute_expected_from = None
                print(f"[TRACEROUTE] ✗ Outer exception in serial handler: {type(e).__name__}: {e}")
                print(f"[TRACEROUTE] Traceback:\n{traceback.format_exc()}")
                await websocket.send(json.dumps({
                    'type': 'traceroute_result',
                    'node_id': node_id, 'error': str(e)
                }, ensure_ascii=False))
            return

        # TCP: run alongside listener, no interruption needed
        cmd = [mapper.meshtastic_cmd, '--host', mapper.host, '--traceroute', node_id]
        print(f"[TRACEROUTE] TCP mode, running: {' '.join(cmd)}")

        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=57)
                ),
                timeout=60.0
            )
            raw_output = result.stdout + result.stderr
            print(f"[TRACEROUTE] Output: {raw_output[:300]}")
        except asyncio.TimeoutError:
            await websocket.send(json.dumps({
                'type': 'traceroute_result', 'node_id': node_id, 'error': 'Timeout'
            }, ensure_ascii=False))
            return

        route, route_back = parse_traceroute_output(raw_output)

        # Enrich hops with coordinates from known nodes
        all_known = {**mapper.nodes, **mapper.nodes_no_position}
        for hop in route + route_back:
            hop_id = hop.get('id')
            hop_name = hop.get('name')
            node_data = None
            if hop_id and hop_id in all_known:
                node_data = all_known[hop_id]
            if not node_data and hop_name:
                for nid, n in mapper.nodes.items():
                    if n.get('name') == hop_name:
                        node_data = n
                        hop['id'] = nid
                        break
            if node_data:
                if 'lat' in node_data:
                    hop['lat'] = node_data['lat']
                    hop['lon'] = node_data['lon']
                hop['name'] = node_data.get('name', hop.get('name', hop_id))

        await websocket.send(json.dumps({
            'type': 'traceroute_result',
            'node_id': node_id,
            'route': route,
            'route_back': route_back,
            'raw': raw_output
        }, ensure_ascii=False))

    except Exception as e:
        print(f"[TRACEROUTE] Error: {e}")
        try:
            await websocket.send(json.dumps({
                'type': 'traceroute_result', 'node_id': node_id, 'error': str(e)
            }, ensure_ascii=False))
        except Exception:
            pass


async def handle_connection_change(data, websocket):
    """Handle connection type change request from frontend"""
    global mapper, restart_config
    connection_type = data.get('connection_type', 'serial')
    host = (data.get('host') or '').strip()
    keep_data = data.get('keep_data', False)

    # Validate
    if connection_type == 'tcp' and not host:
        await websocket.send(json.dumps({
            'type': 'connection_status', 'status': 'failed',
            'message': 'No host specified'
        }, ensure_ascii=False))
        return
    if connection_type == 'tcp':
        _, tcp_port = parse_tcp_target(host)
        if tcp_port is None:
            print(f"[TCP] Invalid port in '{host}', must be 1-65535")
            await websocket.send(json.dumps({
                'type': 'connection_status', 'status': 'failed',
                'message': f"Invalid port in '{host}', must be 1-65535"
            }, ensure_ascii=False))
            return

    print(f"[WS] Connection change: {connection_type} {host or ''}")

    # Determine port for serial. The UI may name one explicitly (picked from
    # list_serial_ports); an empty value means "auto-detect", which is how this
    # always behaved. Anything else is rejected rather than written to config,
    # where a bad value would be re-read on every start.
    port = mapper.port if (mapper and connection_type == 'serial') else None
    if connection_type == 'serial' and 'port' in data:
        requested = (data.get('port') or '').strip()
        if not requested:
            port = None                      # explicit "auto-detect"
        elif requested.startswith('/dev/'):
            port = requested
        else:
            await websocket.send(json.dumps({
                'type': 'connection_status', 'status': 'failed',
                'message': f"Invalid serial device '{requested}'"
            }, ensure_ascii=False))
            return

    # Save config
    save_config(connection_type, host=host or None, port=port)

    # Store restart params
    restart_config = {
        'connection_type': connection_type,
        'host': host or None,
        'port': port,
        'keep_data': keep_data
    }

    # Send "connecting" status
    await websocket.send(json.dumps({
        'type': 'connection_status', 'status': 'connecting',
        'message': f'Switching to {connection_type}' + (f': {host}' if host else '') + '...'
    }, ensure_ascii=False))

    # Terminate current subprocess to trigger run() exit
    if mapper and mapper.current_process:
        try:
            mapper.current_process.terminate()
        except Exception as e:
            print(f"[WS] Error terminating process: {e}")

    restart_event.set()


# WebSocket server handler
def _write_restored(dest, src, private):
    """Write one file from a backup next to its destination, then swap it in."""
    os.makedirs(os.path.dirname(dest) or '.', exist_ok=True)
    tmp = dest + '.restore'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o666)
    with os.fdopen(fd, 'wb') as f:
        shutil.copyfileobj(src, f, 1024 * 1024)
    os.replace(tmp, dest)


def _restore_backup(src):
    """Restore a backup ZIP from a path or a binary file object.

    Only what a backup contains is written: config.json, nodes.json,
    stats.db, plugins/enabled.json and plugins/<author>/<name>/config.json.
    """
    import zipfile
    if not zipfile.is_zipfile(src):
        return {'success': False, 'error': 'Not a valid ZIP file'}
    if hasattr(src, 'seek'):
        src.seek(0)
    restored, skipped = [], []
    with zipfile.ZipFile(src, 'r') as zf:
        names = zf.namelist()
        if 'backup_info.json' not in names:
            return {'success': False, 'error': 'Not a valid backup (missing backup_info.json)'}
        info = json.loads(zf.read('backup_info.json'))
        backup_type = info.get('backup_type', 'unknown')
        for name in names:
            if name == 'backup_info.json':
                continue
            dest, private = None, False
            if name == 'config.json':
                dest, private = CONFIG_PATH, True
            elif name == 'nodes.json':
                dest = mapper.json_path if mapper else '/var/www/html/meshpulse/nodes.json'
            elif name == 'stats.db':
                sdb = mapper.stats_db if mapper else StatsDB()
                with zf.open(name) as f:
                    sdb.replace_with(f)
                restored.append(name)
                continue
            elif plugin_manager and _restorable_plugin_file(name):
                # A backup only ever holds plugins/enabled.json and
                # plugins/<author>/<name>/config.json. Anything else, or
                # a path that leaves the plugins directory, is refused:
                # restore must not be a way to write code onto the Pi.
                base = os.path.realpath(plugin_manager.plugins_dir)
                cand = os.path.realpath(os.path.join(base, name[len('plugins/'):]))
                if cand.startswith(base + os.sep):
                    dest = cand
            if dest:
                with zf.open(name) as f:
                    _write_restored(dest, f, private)
                restored.append(name)
            elif not name.endswith('/'):
                skipped.append(name)
        if skipped:
            print(f"[RESTORE] Refused {len(skipped)} unexpected entries: {skipped[:5]}")
    return {'success': True, 'backup_type': backup_type, 'files_restored': restored,
            'files_refused': skipped}


async def _run_restore(websocket, src):
    try:
        result = await asyncio.to_thread(_restore_backup, src)
        print(f"[RESTORE] {result}")
    except Exception as e:
        print(f"[RESTORE] Error: {e}")
        result = {'success': False, 'error': str(e)}
    await websocket.send(json.dumps({'type': 'restore_result', **result}, ensure_ascii=False))


async def _run_install(websocket, path):
    if not plugin_manager:
        return
    result = plugin_manager.install(path)
    await websocket.send(json.dumps({'type': 'plugin_installed', **result}, ensure_ascii=False))


# ── Chunked uploads ─────────────────────────────────────────────────
# A WebSocket message is 64 KB at most, so that nobody on the network can
# make a Pi 2 hold megabytes per connection. Backups and plugin packages are
# bigger, so the Config page sends them in pieces:
#   upload_begin  {upload_id, purpose, filename, size}  -> upload_ack {received: 0, chunk}
#   upload_chunk  {upload_id, offset, data: base64}     -> upload_ack {received}
#   upload_cancel {upload_id}
# Pieces must arrive in order. They go to an owner-only temp file next to
# config.json, never to memory, and the last one runs the restore or the
# install, which answer as before (restore_result, plugin_installed).
# Problems answer upload_error {upload_id, error}.
UPLOAD_CHUNK = 40 * 1024             # raw bytes per piece, ~55 KB as base64
UPLOAD_LIMITS = {
    'restore_backup': 512 * 1024 * 1024,
    'install_plugin': 32 * 1024 * 1024,
}
UPLOAD_MAX_ACTIVE = 2                # across all connections
UPLOAD_IDLE = 120                    # seconds without a piece before an upload is dropped
UPLOAD_TYPES = ('upload_begin', 'upload_chunk', 'upload_cancel')
_uploads = {}                        # upload_id -> state


def _upload_dir():
    return os.path.dirname(CONFIG_PATH) or '.'


def _upload_drop(upload_id):
    up = _uploads.pop(upload_id, None)
    if up:
        try:
            up['fh'].close()
        except Exception:
            pass
        try:
            os.remove(up['path'])
        except OSError:
            pass


def _upload_drop_where(pred, why):
    for uid, up in list(_uploads.items()):
        if pred(up):
            print(f"[UPLOAD] {uid}: {why}, dropped")
            _upload_drop(uid)


async def _handle_upload_message(websocket, data):
    t = data.get('type')
    uid = str(data.get('upload_id') or '')[:64]

    async def error(msg, drop=True):
        if drop:
            _upload_drop(uid)
        await websocket.send(json.dumps({'type': 'upload_error', 'upload_id': uid, 'error': msg}))

    up = _uploads.get(uid)
    mine = up is not None and up['ws'] is websocket

    if t == 'upload_cancel':
        if mine:
            print(f"[UPLOAD] {uid}: cancelled")
            _upload_drop(uid)
        return

    if t == 'upload_begin':
        now = time.time()
        _upload_drop_where(lambda u: now - u['last'] > UPLOAD_IDLE, f'no data for {UPLOAD_IDLE} s')
        purpose = data.get('purpose')
        try:
            size = int(data.get('size'))
        except (TypeError, ValueError):
            size = -1
        if not uid or uid in _uploads:
            return await error('Bad upload id', drop=False)
        if purpose not in UPLOAD_LIMITS:
            return await error('Unknown upload purpose', drop=False)
        limit = UPLOAD_LIMITS[purpose]
        if size <= 0:
            return await error('The file is empty', drop=False)
        if size > limit:
            return await error(f'The file is too large (max {limit // (1024 * 1024)} MB)', drop=False)
        if any(u['ws'] is websocket for u in _uploads.values()) or len(_uploads) >= UPLOAD_MAX_ACTIVE:
            return await error('Another upload is still running', drop=False)
        try:
            free = shutil.disk_usage(_upload_dir()).free
        except OSError:
            free = 0
        if free < 2 * size + 64 * 1024 * 1024:
            return await error('Not enough free disk space on the Pi', drop=False)
        fd, path = tempfile.mkstemp(prefix='.upload-', dir=_upload_dir())
        _uploads[uid] = {'ws': websocket, 'purpose': purpose, 'path': path,
                         'fh': os.fdopen(fd, 'wb'), 'size': size, 'received': 0, 'last': now}
        print(f"[UPLOAD] {uid}: {purpose}, {size} bytes")
        await websocket.send(json.dumps({'type': 'upload_ack', 'upload_id': uid,
                                         'received': 0, 'chunk': UPLOAD_CHUNK}))
        return

    if t == 'upload_chunk':
        if not mine:
            return await error('Upload not found (it may have timed out)', drop=False)
        try:
            offset = int(data.get('offset'))
        except (TypeError, ValueError):
            offset = -1
        if offset != up['received']:
            return await error('Upload pieces arrived out of order')
        try:
            raw = base64.b64decode(data.get('data') or '', validate=True)
        except ValueError:
            return await error('Bad upload piece')
        if not raw or len(raw) > UPLOAD_CHUNK or up['received'] + len(raw) > up['size']:
            return await error('Bad upload piece size')
        up['fh'].write(raw)
        up['received'] += len(raw)
        up['last'] = time.time()
        await websocket.send(json.dumps({'type': 'upload_ack', 'upload_id': uid,
                                         'received': up['received']}))
        if up['received'] < up['size']:
            return
        _uploads.pop(uid, None)
        up['fh'].close()
        print(f"[UPLOAD] {uid}: complete, running {up['purpose']}")
        try:
            if up['purpose'] == 'restore_backup':
                await _run_restore(websocket, up['path'])
            else:
                await _run_install(websocket, up['path'])
        finally:
            try:
                os.remove(up['path'])
            except OSError:
                pass


async def websocket_handler(websocket):
    """Handle WebSocket connections"""
    connected_clients.add(websocket)
    client_addr = websocket.remote_address
    print(f"[WS] Client connected: {client_addr}, total clients: {len(connected_clients)}")
    if plugin_manager:
        await plugin_manager.dispatch_hook('on_ws_client_connect', {'client_id': str(client_addr)})

    # Send current connection status and tracker info to newly connected client
    if mapper and hasattr(mapper, 'tracker_info'):
        status_msg = safe_json({
            'type': 'connection_status',
            'status': 'connected',
            'message': '',
            'connection_type': mapper.connection_type,
            'host': mapper.host,
            'port': mapper.port,
            'tracker': mapper.tracker_info,
            'mapper_version': MAPPER_VERSION,
            'timestamp': int(time.time())
        })
        await websocket.send(status_msg)

    authed_key = None   # admin password fingerprint this connection logged in with
    try:
        await websocket.send(json.dumps(_auth_status(False)))
    except Exception:
        pass

    try:
        async for message in websocket:
            try:
                # Never log what could carry a password.
                if message and len(message) < 200 and '"auth' not in message and 'admin_password' not in message:
                    print(f"[WS] ← {client_addr}: {message}")
                if len(message) > 65536:  # 64 KB max; files come in pieces (upload_*)
                    print(f"[WS] Message too large ({len(message)} bytes), ignoring")
                    await websocket.send(json.dumps({'type': 'error', 'error': 'message_too_large',
                                                     'max_bytes': 65536}))
                    continue
                data = json.loads(message)
                _t = data.get('type')
                if _t in ('auth', 'set_admin_password', 'admin_sign_out_everywhere'):
                    authed_key = await _handle_auth_message(websocket, data, client_addr, authed_key)
                    continue
                if _t in admin_auth.PROTECTED:
                    _auth = _admin_auth()
                    if _auth.required() and authed_key != _auth.fingerprint():
                        await websocket.send(json.dumps({'type': 'auth_required', 'request_type': _t}))
                        continue
                if _t in UPLOAD_TYPES:
                    await _handle_upload_message(websocket, data)
                    continue
                if data.get('type') == 'connect':
                    await handle_connection_change(data, websocket)
                elif data.get('type') == 'traceroute':
                    node_id = data.get('node_id')
                    if node_id:
                        asyncio.ensure_future(run_traceroute(node_id, websocket))
                    else:
                        print(f"[WS] Traceroute request missing node_id from {client_addr}")
                elif data.get('type') == 'send_message':
                    text = data.get('text', '').strip()
                    channel_index = int(data.get('channel_index', 0))
                    dest_id = data.get('dest_id') or None
                    if text:
                        asyncio.ensure_future(run_send_message(text, channel_index, dest_id, websocket))
                    else:
                        print(f"[WS] send_message missing text from {client_addr}")
                elif data.get('type') == 'get_stats':
                    if mapper:
                        with mapper._stats_cache_lock:
                            cached = mapper._stats_cache
                            cache_ts = mapper._stats_cache_ts
                        if cached is not None:
                            cached['cache_age'] = int(time.time() - cache_ts)
                            await websocket.send(safe_json({'type': 'stats_data', 'data': cached}))
                        else:
                            await websocket.send(safe_json({'type': 'stats_data', 'data': {
                                'total_packets': 0, 'loading': True
                            }}))
                        t = threading.Thread(target=mapper._recompute_stats, daemon=True)
                        t.start()
                    else:
                        await websocket.send(safe_json({'type': 'stats_data', 'data': {}}))
                elif data.get('type') == 'get_traffic_history':
                    # Reads only the hourly rollup (a few hundred rows at most), off the
                    # event loop: the StatsDB lock can be held by the minute summary.
                    range_key = str(data.get('range', '7d'))
                    if mapper:
                        try:
                            hist = await asyncio.to_thread(mapper.stats_db.get_traffic_history, range_key)
                        except Exception as e:
                            print(f"[STATS] Traffic history error: {e}")
                            hist = {'range': range_key, 'rows': [], 'ports': [], 'error': str(e)}
                    else:
                        hist = {'range': range_key, 'rows': [], 'ports': []}
                    await websocket.send(safe_json({'type': 'traffic_history', 'data': hist}))
                elif data.get('type') == 'get_config':
                    if mapper:
                        try:
                            config = mapper.get_device_config()
                            await websocket.send(json.dumps({
                                'type': 'config_data',
                                'config': config
                            }, ensure_ascii=False))
                        except Exception as e:
                            print(f"[CONFIG] get_config error: {e}")
                            await websocket.send(json.dumps({
                                'type': 'config_error',
                                'error': str(e)
                            }, ensure_ascii=False))

                elif data.get('type') == 'set_config':
                    if mapper:
                        changes = data.get('changes', {})
                        reboot = data.get('reboot', False)
                        applied = []
                        try:
                            conn_type = mapper.connection_type
                            if conn_type == 'serial':
                                cli_port = mapper.port
                                if not cli_port and mapper._serial_iface and mapper._serial_iface.iface:
                                    cli_port = getattr(mapper._serial_iface.iface, 'devPath', None)
                                base_cmd = [mapper.meshtastic_cmd, '--port', cli_port]
                            elif conn_type == 'tcp':
                                base_cmd = [mapper.meshtastic_cmd, '--host', mapper.host]
                            else:
                                raise Exception(f'Unsupported connection type: {conn_type}')

                            set_args = []
                            owner_args = []

                            # Apply neighborinfo changes via Python API before serial disconnect
                            if 'neighborinfo' in changes:
                                ni = changes.pop('neighborinfo')
                                ni_iface = None
                                if conn_type == 'serial' and mapper._serial_iface:
                                    ni_iface = mapper._serial_iface.iface
                                elif conn_type == 'tcp' and mapper._tcp_iface:
                                    ni_iface = mapper._tcp_iface
                                if not ni_iface:
                                    raise Exception('No active connection for neighborinfo config')
                                node = ni_iface.localNode
                                for key, val in ni.items():
                                    if key == 'neighbor_info_enabled':
                                        node.moduleConfig.neighbor_info.enabled = bool(val)
                                    elif key == 'update_interval':
                                        node.moduleConfig.neighbor_info.update_interval = int(val)
                                node.writeConfig('neighbor_info')
                                applied.append('neighborinfo')

                            if 'user' in changes:
                                u = changes['user']
                                if 'long_name' in u:
                                    owner_args.extend(['--set-owner', u['long_name']])
                                    applied.append('user.long_name')
                                if 'short_name' in u:
                                    owner_args.extend(['--set-owner-short', u['short_name']])
                                    applied.append('user.short_name')

                            section_map = {
                                'device': 'device',
                                'position': 'position',
                                'lora': 'lora',
                                'telemetry': 'telemetry',
                                'network': 'network',
                                'bluetooth': 'bluetooth',
                                'power': 'power',
                                'display': 'display',
                            }

                            enum_fields = {
                                'device.role': {0: 'CLIENT', 1: 'CLIENT_MUTE', 2: 'ROUTER', 3: 'ROUTER_CLIENT', 4: 'REPEATER', 5: 'TRACKER', 6: 'SENSOR', 7: 'TAK', 8: 'CLIENT_HIDDEN', 9: 'LOST_AND_FOUND', 10: 'TAK_TRACKER'},
                                'device.rebroadcast_mode': {0: 'ALL', 1: 'ALL_SKIP_DECODING', 2: 'LOCAL_ONLY', 3: 'KNOWN_ONLY', 4: 'NONE'},
                                'lora.modem_preset': {0: 'LONG_FAST', 1: 'LONG_SLOW', 2: 'VERY_LONG_SLOW', 3: 'MEDIUM_SLOW', 4: 'MEDIUM_FAST', 5: 'SHORT_SLOW', 6: 'SHORT_FAST', 7: 'LONG_MODERATE'},
                                'lora.region': {0: 'UNSET', 1: 'US', 2: 'EU_433', 3: 'EU_868', 4: 'CN', 5: 'JP', 6: 'ANZ', 7: 'KR', 8: 'TW', 9: 'RU', 10: 'IN', 11: 'NZ_865', 12: 'TH', 13: 'LORA_24', 14: 'UA_433', 15: 'UA_868', 16: 'MY_433', 17: 'MY_919', 18: 'SG_923'},
                                'display.gps_format': {0: 'DEC', 1: 'DMS', 2: 'UTM', 3: 'MGRS', 4: 'OLC', 5: 'OSGR'},
                                'display.units': {0: 'METRIC', 1: 'IMPERIAL'},
                                'display.oled': {0: 'AUTO', 1: 'SSD1306', 2: 'SH1106', 3: 'SH1107'},
                                'display.displaymode': {0: 'DEFAULT', 1: 'TWOCOLOR', 2: 'INVERTED', 3: 'COLOR'},
                                'network.address_mode': {0: 'DHCP', 1: 'STATIC'},
                                'bluetooth.mode': {0: 'RANDOM_PIN', 1: 'FIXED_PIN', 2: 'NO_PIN'},
                            }

                            for section, cli_section in section_map.items():
                                if section in changes:
                                    for key, val in changes[section].items():
                                        full_key = f'{cli_section}.{key}'
                                        if full_key in enum_fields:
                                            int_val = int(val) if isinstance(val, str) else val
                                            val = enum_fields[full_key].get(int_val, str(val))
                                        elif isinstance(val, bool):
                                            val = 'true' if val else 'false'
                                        set_args.extend(['--set', full_key, str(val)])
                                        applied.append(full_key)

                            module_section_map = {
                                'mqtt': 'mqtt',
                                'store_forward': 'store_forward',
                                'ext_notification': 'ext_notification',
                                'range_test': 'range_test',
                                'canned_message': 'canned_message',
                                'paxcounter': 'paxcounter',
                                'serial_module': 'serial',
                            }

                            mqtt_skip_fields = {'map_reporting_enabled', 'map_publish_interval_secs', 'position_precision'}

                            for section, cli_section in module_section_map.items():
                                if section in changes:
                                    for key, val in changes[section].items():
                                        if section == 'mqtt' and key in mqtt_skip_fields:
                                            continue
                                        full_key = f'{cli_section}.{key}'
                                        if isinstance(val, bool):
                                            val = 'true' if val else 'false'
                                        set_args.extend(['--set', full_key, str(val)])
                                        applied.append(full_key)

                            if not set_args and not owner_args:
                                if reboot:
                                    set_args = ['--reboot']
                                    applied.append('reboot')
                                else:
                                    _local_applied = list(applied)
                                    if 'coverage' in changes:
                                        cov = changes['coverage']
                                        try:
                                            with open(CONFIG_PATH, 'r') as f:
                                                full_config = json.load(f)
                                        except Exception:
                                            full_config = {}
                                        if 'server_url' in cov:
                                            full_config['coverage_server_url'] = cov['server_url']
                                            mapper.config['coverage_server_url'] = cov['server_url']
                                        if 'api_key' in cov:
                                            full_config['coverage_api_key'] = cov['api_key']
                                            mapper.config['coverage_api_key'] = cov['api_key']
                                        if 'antenna_gain' in cov:
                                            full_config['coverage_antenna_gain'] = float(cov['antenna_gain'])
                                            mapper.config['coverage_antenna_gain'] = float(cov['antenna_gain'])
                                        if 'antenna_height' in cov:
                                            full_config['coverage_antenna_height'] = float(cov['antenna_height'])
                                            mapper.config['coverage_antenna_height'] = float(cov['antenna_height'])
                                        if 'max_range_km' in cov:
                                            full_config['coverage_max_range_km'] = int(cov['max_range_km'])
                                            mapper.config['coverage_max_range_km'] = int(cov['max_range_km'])
                                        try:
                                            with open(CONFIG_PATH, 'w') as f:
                                                json.dump(full_config, f, indent=2)
                                            print(f"[CONFIG] Coverage config saved")
                                            _local_applied.append('coverage')
                                        except Exception as e:
                                            print(f"[CONFIG] Coverage save error: {e}")

                                    if 'telemetry_mapper' in changes:
                                        tel = changes['telemetry_mapper']
                                        try:
                                            with open(CONFIG_PATH, 'r') as f:
                                                full_config = json.load(f)
                                        except Exception:
                                            full_config = {}
                                        full_config['telemetry_opt_out'] = bool(tel.get('opt_out', False))
                                        mapper.config['telemetry_opt_out'] = bool(tel.get('opt_out', False))
                                        try:
                                            with open(CONFIG_PATH, 'w') as f:
                                                json.dump(full_config, f, indent=2)
                                            print(f"[CONFIG] Telemetry opt-out saved")
                                            _local_applied.append('telemetry_mapper')
                                        except Exception as e:
                                            print(f"[CONFIG] Telemetry save error: {e}")

                                    await websocket.send(json.dumps({
                                        'type': 'config_saved',
                                        'success': True,
                                        'rebooting': False,
                                        'applied': _local_applied
                                    }, ensure_ascii=False))
                                    continue

                            def _run_set_config():
                                if conn_type == 'serial' and mapper._serial_iface:
                                    print("[CONFIG] Closing serial for CLI access...")
                                    mapper._serial_iface.disconnect()
                                    mapper._serial_iface = None
                                    time.sleep(2)
                                try:
                                    if owner_args:
                                        cmd = base_cmd + owner_args
                                        print(f"[CONFIG] Running: {' '.join(cmd)}")
                                        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                                        if r.returncode != 0:
                                            raise Exception(r.stderr.strip() or r.stdout.strip() or 'Owner set failed')

                                    if set_args:
                                        cmd = base_cmd + set_args
                                        if reboot and '--reboot' not in set_args:
                                            cmd.append('--reboot')
                                        print(f"[CONFIG] Running: {' '.join(cmd)}")
                                        if reboot:
                                            mapper._cli_reboot_pending = True
                                        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                                        if r.returncode != 0:
                                            raise Exception(r.stderr.strip() or r.stdout.strip() or 'Config set failed')
                                finally:
                                    if conn_type == 'serial':
                                        # Don't reconnect here — let _run_serial loop handle it
                                        # It will detect _serial_iface is None and reconnect
                                        print("[CONFIG] Serial released, waiting for auto-reconnect...")

                            print(f"[CONFIG] Applying config via CLI: {applied}, reboot={reboot}")
                            await asyncio.wait_for(
                                asyncio.to_thread(_run_set_config),
                                timeout=90
                            )
                            await websocket.send(json.dumps({
                                'type': 'config_saved',
                                'success': True,
                                'rebooting': reboot,
                                'applied': applied
                            }, ensure_ascii=False))
                            print(f"[CONFIG] Config saved via CLI: {applied}")
                        except asyncio.TimeoutError:
                            print("[CONFIG] set_config timeout after 90s")
                            await websocket.send(json.dumps({
                                'type': 'config_saved',
                                'success': False,
                                'error': 'Timeout applying config (90s)'
                            }, ensure_ascii=False))
                        except (BrokenPipeError, OSError) as e:
                            print(f"[CONFIG] Connection dropped after save (expected if rebooting): {e}")
                            await websocket.send(json.dumps({
                                'type': 'config_saved',
                                'success': True,
                                'applied': applied,
                                'rebooting': True,
                                'warning': 'Device disconnected — config likely saved, device may be rebooting'
                            }, ensure_ascii=False))
                        except Exception as e:
                            print(f"[CONFIG] Error: {e}")
                            await websocket.send(json.dumps({
                                'type': 'config_saved',
                                'success': False,
                                'error': str(e)
                            }, ensure_ascii=False))

                elif data.get('type') == 'set_fixed_position':
                    if mapper:
                        try:
                            lat = data.get('lat', 0)
                            lon = data.get('lon', 0)
                            alt = data.get('alt', 0)
                            conn_type = mapper.connection_type

                            if conn_type == 'serial':
                                port = mapper.port
                                if not port and mapper._serial_iface and mapper._serial_iface.iface:
                                    port = getattr(mapper._serial_iface.iface, 'devPath', None)
                                cmd = [mapper.meshtastic_cmd, '--port', port,
                                       '--setlat', str(lat), '--setlon', str(lon), '--setalt', str(alt)]
                            elif conn_type == 'tcp':
                                cmd = [mapper.meshtastic_cmd, '--host', mapper.host,
                                       '--setlat', str(lat), '--setlon', str(lon), '--setalt', str(alt)]
                            else:
                                raise Exception(f'Unsupported connection type: {conn_type}')

                            def _run_set_position():
                                if conn_type == 'serial' and mapper._serial_iface:
                                    print("[CONFIG] Closing serial for CLI access...")
                                    mapper._serial_iface.disconnect()
                                    mapper._serial_iface = None
                                    time.sleep(2)
                                try:
                                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                                    return result
                                finally:
                                    if conn_type == 'serial':
                                        # Don't reconnect here — let _run_serial loop handle it
                                        # It will detect _serial_iface is None and reconnect
                                        print("[CONFIG] Serial released, waiting for auto-reconnect...")

                            print(f"[CONFIG] Setting fixed position via CLI: {lat}, {lon}, {alt}m")
                            result = await asyncio.wait_for(
                                asyncio.to_thread(_run_set_position),
                                timeout=90
                            )
                            if result.returncode == 0:
                                await websocket.send(json.dumps({
                                    'type': 'fixed_position_result',
                                    'success': True
                                }, ensure_ascii=False))
                                print(f"[CONFIG] Fixed position set: {lat}, {lon}, {alt}m")
                            else:
                                error_msg = result.stderr.strip() or result.stdout.strip() or 'Unknown error'
                                await websocket.send(json.dumps({
                                    'type': 'fixed_position_result',
                                    'success': False,
                                    'error': error_msg
                                }, ensure_ascii=False))
                                print(f"[CONFIG] Fixed position failed: {error_msg}")
                        except asyncio.TimeoutError:
                            await websocket.send(json.dumps({
                                'type': 'fixed_position_result',
                                'success': False,
                                'error': 'Timeout setting position (90s)'
                            }, ensure_ascii=False))
                            print("[CONFIG] set_fixed_position timeout after 90s")
                        except Exception as e:
                            await websocket.send(json.dumps({
                                'type': 'fixed_position_result',
                                'success': False, 'error': str(e)
                            }, ensure_ascii=False))
                            print(f"[CONFIG] set_fixed_position error: {e}")

                elif data.get('type') == 'clear_fixed_position':
                    if mapper:
                        try:
                            conn_type = mapper.connection_type

                            if conn_type == 'serial':
                                port = mapper.port
                                if not port and mapper._serial_iface and mapper._serial_iface.iface:
                                    port = getattr(mapper._serial_iface.iface, 'devPath', None)
                                cmd = [mapper.meshtastic_cmd, '--port', port, '--remove-fixed-position']
                            elif conn_type == 'tcp':
                                cmd = [mapper.meshtastic_cmd, '--host', mapper.host, '--remove-fixed-position']
                            else:
                                raise Exception(f'Unsupported connection type: {conn_type}')

                            def _run_clear_position():
                                if conn_type == 'serial' and mapper._serial_iface:
                                    print("[CONFIG] Closing serial for CLI access...")
                                    mapper._serial_iface.disconnect()
                                    mapper._serial_iface = None
                                    time.sleep(2)
                                try:
                                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                                    return result
                                finally:
                                    if conn_type == 'serial':
                                        # Don't reconnect here — let _run_serial loop handle it
                                        # It will detect _serial_iface is None and reconnect
                                        print("[CONFIG] Serial released, waiting for auto-reconnect...")

                            print("[CONFIG] Clearing fixed position via CLI...")
                            result = await asyncio.wait_for(
                                asyncio.to_thread(_run_clear_position),
                                timeout=90
                            )
                            if result.returncode == 0:
                                await websocket.send(json.dumps({
                                    'type': 'clear_position_result',
                                    'success': True
                                }, ensure_ascii=False))
                                print("[CONFIG] Fixed position cleared")
                            else:
                                error_msg = result.stderr.strip() or result.stdout.strip() or 'Unknown error'
                                await websocket.send(json.dumps({
                                    'type': 'clear_position_result',
                                    'success': False,
                                    'error': error_msg
                                }, ensure_ascii=False))
                                print(f"[CONFIG] Clear position failed: {error_msg}")
                        except asyncio.TimeoutError:
                            await websocket.send(json.dumps({
                                'type': 'clear_position_result',
                                'success': False,
                                'error': 'Timeout clearing position (90s)'
                            }, ensure_ascii=False))
                            print("[CONFIG] clear_fixed_position timeout after 90s")
                        except Exception as e:
                            await websocket.send(json.dumps({
                                'type': 'clear_position_result',
                                'success': False, 'error': str(e)
                            }, ensure_ascii=False))
                            print(f"[CONFIG] clear_fixed_position error: {e}")

                elif data.get('type') == 'get_favorites':
                    try:
                        iface = None
                        if mapper and mapper.connection_type == 'serial' and mapper._serial_iface:
                            iface = mapper._serial_iface.iface
                        elif mapper and mapper.connection_type == 'tcp' and mapper._tcp_iface:
                            iface = mapper._tcp_iface
                        if not iface:
                            raise Exception('No active connection')
                        favorites = []
                        for node_num, node_info in iface.nodes.items():
                            if node_info.get('isFavorite'):
                                user = node_info.get('user', {})
                                favorites.append({
                                    'node_id': user.get('id', node_num),
                                    'name': user.get('longName', user.get('id', node_num)),
                                    'short_name': user.get('shortName', '??'),
                                })
                        await websocket.send(json.dumps({'type': 'favorites_list', 'favorites': favorites}, ensure_ascii=False))
                    except Exception as e:
                        await websocket.send(json.dumps({'type': 'favorites_list', 'favorites': [], 'error': str(e)}, ensure_ascii=False))

                elif data.get('type') == 'set_favorite':
                    node_id = data.get('node_id', '').strip()
                    if not node_id:
                        await websocket.send(json.dumps({'type': 'favorite_result', 'success': False, 'error': 'No node ID provided'}, ensure_ascii=False))
                    elif mapper:
                        try:
                            iface = None
                            if mapper.connection_type == 'serial' and mapper._serial_iface:
                                iface = mapper._serial_iface.iface
                            elif mapper.connection_type == 'tcp' and mapper._tcp_iface:
                                iface = mapper._tcp_iface
                            if not iface:
                                raise Exception('No active connection')
                            iface.localNode.setFavorite(node_id)
                            await websocket.send(json.dumps({'type': 'favorite_result', 'success': True, 'action': 'set', 'node_id': node_id}, ensure_ascii=False))
                            try:
                                favorites = []
                                for node_num, node_info in iface.nodes.items():
                                    if node_info.get('isFavorite'):
                                        user = node_info.get('user', {})
                                        favorites.append({
                                            'node_id': user.get('id', node_num),
                                            'name': user.get('longName', user.get('id', node_num)),
                                            'short_name': user.get('shortName', '??'),
                                        })
                                await websocket.send(json.dumps({'type': 'favorites_list', 'favorites': favorites}, ensure_ascii=False))
                            except:
                                pass
                        except Exception as e:
                            await websocket.send(json.dumps({'type': 'favorite_result', 'success': False, 'error': str(e)}, ensure_ascii=False))

                elif data.get('type') == 'remove_favorite':
                    node_id = data.get('node_id', '').strip()
                    if not node_id:
                        await websocket.send(json.dumps({'type': 'favorite_result', 'success': False, 'error': 'No node ID provided'}, ensure_ascii=False))
                    elif mapper:
                        try:
                            iface = None
                            if mapper.connection_type == 'serial' and mapper._serial_iface:
                                iface = mapper._serial_iface.iface
                            elif mapper.connection_type == 'tcp' and mapper._tcp_iface:
                                iface = mapper._tcp_iface
                            if not iface:
                                raise Exception('No active connection')
                            iface.localNode.removeFavorite(node_id)
                            await websocket.send(json.dumps({'type': 'favorite_result', 'success': True, 'action': 'remove', 'node_id': node_id}, ensure_ascii=False))
                            try:
                                favorites = []
                                for node_num, node_info in iface.nodes.items():
                                    if node_info.get('isFavorite'):
                                        user = node_info.get('user', {})
                                        favorites.append({
                                            'node_id': user.get('id', node_num),
                                            'name': user.get('longName', user.get('id', node_num)),
                                            'short_name': user.get('shortName', '??'),
                                        })
                                await websocket.send(json.dumps({'type': 'favorites_list', 'favorites': favorites}, ensure_ascii=False))
                            except:
                                pass
                        except Exception as e:
                            await websocket.send(json.dumps({'type': 'favorite_result', 'success': False, 'error': str(e)}, ensure_ascii=False))

                elif data.get('type') == 'set_ignored_node':
                    node_id = data.get('node_id')
                    if mapper and node_id:
                        try:
                            iface = None
                            if mapper.connection_type == 'serial' and mapper._serial_iface:
                                iface = mapper._serial_iface.iface
                            elif mapper.connection_type == 'tcp' and mapper._tcp_iface:
                                iface = mapper._tcp_iface.interface
                            if iface:
                                from meshtastic.protobuf import admin_pb2
                                node_num = int(node_id.replace('!', ''), 16)
                                p = admin_pb2.AdminMessage()
                                p.set_ignored_node = node_num
                                iface.localNode._sendAdmin(p)
                                print(f"[CONFIG] Ignored node set: {node_id}")
                                # Save to mapper's ignored list
                                node_name = mapper.nodes.get(node_id, {}).get('name') or \
                                            mapper.nodes_no_position.get(node_id, {}).get('name') or \
                                            mapper.known_names.get(node_id) or node_id
                                mapper.ignored_nodes[node_id] = {
                                    'id': node_id,
                                    'name': node_name,
                                    'ts': int(time.time())
                                }
                                mapper.save_nodes()
                                await websocket.send(json.dumps({
                                    'type': 'ignored_node_result',
                                    'success': True,
                                    'action': 'set',
                                    'node_id': node_id,
                                    'node_name': node_name,
                                    'ignored_nodes': list(mapper.ignored_nodes.values())
                                }, ensure_ascii=False))
                        except Exception as e:
                            print(f"[CONFIG] Error setting ignored node: {e}")
                            await websocket.send(json.dumps({
                                'type': 'ignored_node_result',
                                'success': False,
                                'error': str(e),
                                'node_id': node_id
                            }, ensure_ascii=False))

                elif data.get('type') == 'remove_ignored_node':
                    node_id = data.get('node_id')
                    if mapper and node_id:
                        try:
                            iface = None
                            if mapper.connection_type == 'serial' and mapper._serial_iface:
                                iface = mapper._serial_iface.iface
                            elif mapper.connection_type == 'tcp' and mapper._tcp_iface:
                                iface = mapper._tcp_iface.interface
                            if iface:
                                from meshtastic.protobuf import admin_pb2
                                node_num = int(node_id.replace('!', ''), 16)
                                p = admin_pb2.AdminMessage()
                                p.remove_ignored_node = node_num
                                iface.localNode._sendAdmin(p)
                                print(f"[CONFIG] Ignored node removed: {node_id}")
                                # Remove from mapper's ignored list
                                mapper.ignored_nodes.pop(node_id, None)
                                mapper.save_nodes()
                                await websocket.send(json.dumps({
                                    'type': 'ignored_node_result',
                                    'success': True,
                                    'action': 'remove',
                                    'node_id': node_id,
                                    'ignored_nodes': list(mapper.ignored_nodes.values())
                                }, ensure_ascii=False))
                        except Exception as e:
                            print(f"[CONFIG] Error removing ignored node: {e}")
                            await websocket.send(json.dumps({
                                'type': 'ignored_node_result',
                                'success': False,
                                'error': str(e),
                                'node_id': node_id
                            }, ensure_ascii=False))

                elif data.get('type') == 'get_ignored_nodes':
                    if mapper:
                        await websocket.send(json.dumps({
                            'type': 'ignored_nodes_list',
                            'ignored_nodes': list(mapper.ignored_nodes.values())
                        }, ensure_ascii=False))

                elif data.get('type') == 'save_channel':
                    try:
                        iface = None
                        if mapper and mapper.connection_type == 'serial' and mapper._serial_iface:
                            iface = mapper._serial_iface.iface
                        elif mapper and mapper.connection_type == 'tcp' and mapper._tcp_iface:
                            iface = mapper._tcp_iface
                        if not iface:
                            raise Exception('No active connection')

                        from meshtastic.protobuf import channel_pb2
                        import meshtastic.util

                        ch_index = int(data.get('index', 0))
                        ch_name = data.get('name', '')
                        ch_role = int(data.get('role', 1))
                        ch_psk = data.get('psk', None)

                        node = iface.localNode
                        channel = node.channels[ch_index]

                        channel.settings.name = ch_name

                        if ch_index == 0:
                            channel.role = channel_pb2.Channel.Role.PRIMARY
                        elif ch_role == 3:
                            channel.role = channel_pb2.Channel.Role.DISABLED
                        else:
                            channel.role = channel_pb2.Channel.Role.SECONDARY

                        if ch_psk is not None:
                            if ch_psk == 'none':
                                channel.settings.psk = b''
                            elif ch_psk == 'default':
                                channel.settings.psk = base64.b64decode('AQ==')
                            elif ch_psk == 'random':
                                channel.settings.psk = meshtastic.util.genPSK256()
                            elif ch_psk.startswith('base64:'):
                                channel.settings.psk = base64.b64decode(ch_psk[7:])
                            elif ch_psk.startswith('custom:'):
                                raw = ch_psk[7:].strip()
                                if raw.startswith('0x') or raw.startswith('0X'):
                                    hex_str = raw[2:]
                                    if len(hex_str) % 2 != 0:
                                        raise Exception(f'Invalid hex PSK length: {len(hex_str)} chars (must be even)')
                                    channel.settings.psk = bytes.fromhex(hex_str)
                                else:
                                    channel.settings.psk = base64.b64decode(raw)

                        try:
                            if 'uplink_enabled' in data:
                                channel.settings.uplink_enabled = bool(data['uplink_enabled'])
                            if 'downlink_enabled' in data:
                                channel.settings.downlink_enabled = bool(data['downlink_enabled'])
                        except Exception as e:
                            print(f"[CONFIG] uplink/downlink_enabled not supported: {e}")

                        node.writeChannel(ch_index)
                        print(f"[CONFIG] Channel {ch_index} saved: name={ch_name!r} role={ch_role} psk={'changed' if ch_psk else 'unchanged'}")
                        await websocket.send(json.dumps({
                            'type': 'channel_save_result',
                            'success': True,
                            'index': ch_index
                        }, ensure_ascii=False))
                    except Exception as e:
                        await websocket.send(json.dumps({
                            'type': 'channel_save_result',
                            'success': False,
                            'error': str(e)
                        }, ensure_ascii=False))

                elif data.get('type') == 'clear_node_stats':
                    node_id = data.get('node_id', '').strip()
                    if node_id and mapper:
                        try:
                            mapper.stats_db.clear_node_packets(node_id)
                            await websocket.send(json.dumps({
                                'type': 'clear_node_stats_result',
                                'node_id': node_id,
                                'success': True
                            }, ensure_ascii=False))
                            print(f"[STATS] Cleared packet history for {node_id}")
                        except Exception as e:
                            await websocket.send(json.dumps({
                                'type': 'clear_node_stats_result',
                                'node_id': node_id,
                                'success': False,
                                'error': str(e)
                            }, ensure_ascii=False))
                elif data.get('type') == 'clear_all_stats':
                    mode = data.get('mode', 'stats_only')  # 'stats_only' or 'full_reset'
                    if mapper:
                        try:
                            mapper.stats_db.clear_all_stats()
                            cleared = 'stats'
                            if mode == 'full_reset':
                                # Clear in-memory nodes + nodes.json
                                mapper.nodes = {}
                                mapper.nodes_no_position = {}
                                mapper.known_names = {}
                                mapper.messages = {}
                                mapper.save_nodes()
                                # Broadcast to all clients to clear their maps
                                for client in list(connected_clients):
                                    try:
                                        await client.send(json.dumps({
                                            'type': 'full_reset',
                                            'timestamp': int(time.time())
                                        }, ensure_ascii=False))
                                    except Exception:
                                        pass
                                cleared = 'stats + nodes'
                            await websocket.send(json.dumps({
                                'type': 'clear_all_stats_result',
                                'success': True,
                                'mode': mode
                            }, ensure_ascii=False))
                            print(f"[STATS] Cleared ALL {cleared}")
                        except Exception as e:
                            await websocket.send(json.dumps({
                                'type': 'clear_all_stats_result',
                                'success': False,
                                'error': str(e)
                            }, ensure_ascii=False))
                # ── Plugin management ──────────────────────────────────
                elif data.get('type') == 'get_plugins':
                    if plugin_manager:
                        plugins = plugin_manager.list_plugins()
                        updates = compute_plugin_updates()
                        await websocket.send(json.dumps({
                            'type': 'plugins_list',
                            'plugins': plugins,
                            'available_updates': updates
                        }, ensure_ascii=False))

                elif data.get('type') == 'enable_plugin':
                    if plugin_manager:
                        plugin_id = data.get('plugin_id', '')
                        await websocket.send(json.dumps({
                            'type': 'plugin_progress',
                            'plugin_id': plugin_id,
                            'status': 'installing',
                            'message': 'Installing dependencies...'
                        }, ensure_ascii=False))
                        result = plugin_manager.enable(plugin_id)
                        await websocket.send(json.dumps({
                            'type': 'plugin_progress',
                            'plugin_id': plugin_id,
                            'status': 'done',
                            'message': 'Plugin enabled successfully'
                        }, ensure_ascii=False))
                        await websocket.send(json.dumps({
                            'type': 'plugin_enabled', **result
                        }, ensure_ascii=False))

                elif data.get('type') == 'disable_plugin':
                    if plugin_manager:
                        result = plugin_manager.disable(data.get('plugin_id', ''))
                        await websocket.send(json.dumps({
                            'type': 'plugin_disabled', **result
                        }, ensure_ascii=False))

                elif data.get('type') == 'install_plugin':
                    if plugin_manager:
                        result = plugin_manager.install_from_upload(
                            data.get('filename', 'plugin.meshplugin'),
                            data.get('data', '')
                        )
                        await websocket.send(json.dumps({
                            'type': 'plugin_installed', **result
                        }, ensure_ascii=False))

                elif data.get('type') == 'uninstall_plugin':
                    if plugin_manager:
                        result = plugin_manager.uninstall(data.get('plugin_id', ''))
                        await websocket.send(json.dumps({
                            'type': 'plugin_uninstalled', **result
                        }, ensure_ascii=False))

                elif data.get('type') == 'save_plugin_config':
                    if plugin_manager:
                        plugin_id = data.get('plugin_id', '')
                        config_updates = data.get('config', {})
                        result = plugin_manager.save_plugin_config(plugin_id, config_updates)
                        # Respond to the requesting client
                        await websocket.send(json.dumps({
                            'type': 'plugin_config_saved', **result
                        }, ensure_ascii=False))
                        # Broadcast config change to ALL other clients (so map page can reload plugin)
                        if result.get('success'):
                            full_config = {}
                            plugin_dir = plugin_manager._get_plugin_dir(plugin_id)
                            manifest = plugin_manager._read_manifest(plugin_dir)
                            if manifest:
                                full_config = plugin_manager._load_plugin_config(plugin_dir, manifest)
                            broadcast_msg = json.dumps({
                                'type': 'plugin_config_updated',
                                'plugin_id': plugin_id,
                                'config': full_config
                            }, ensure_ascii=False)
                            for client in list(connected_clients):
                                if client != websocket:
                                    try:
                                        await client.send(broadcast_msg)
                                    except Exception:
                                        pass

                elif data.get('type') == 'fetch_store_plugins':
                    req_url = data.get('url', '').strip()
                    cfg = load_config()
                    store_url = req_url or cfg.get('plugin_store_url', 'https://meshpulse.app/plugins/plugins.json')
                    # Persist URL change if different
                    if req_url and req_url != cfg.get('plugin_store_url', ''):
                        try:
                            with open(CONFIG_PATH, 'r') as _f:
                                _cfg = json.load(_f)
                        except Exception:
                            _cfg = {}
                        _cfg['plugin_store_url'] = req_url
                        with open(CONFIG_PATH, 'w') as _f:
                            json.dump(_cfg, _f, indent=2)
                    def _fetch_store():
                        import urllib.request as _ur
                        with _ur.urlopen(store_url, timeout=15) as _r:
                            return json.loads(_r.read().decode())
                    try:
                        store_data = await asyncio.wait_for(asyncio.to_thread(_fetch_store), timeout=20)
                        await websocket.send(json.dumps({
                            'type': 'store_plugins',
                            'success': True,
                            'plugins': store_data.get('plugins', []),
                            'store_url': store_url
                        }, ensure_ascii=False))
                    except Exception as e:
                        await websocket.send(json.dumps({
                            'type': 'store_plugins',
                            'success': False,
                            'error': str(e),
                            'store_url': store_url
                        }, ensure_ascii=False))

                elif data.get('type') == 'install_from_store':
                    if plugin_manager:
                        download_url = data.get('download_url', '')
                        if not download_url:
                            await websocket.send(json.dumps({
                                'type': 'plugin_installed', 'success': False, 'error': 'No download URL'
                            }, ensure_ascii=False))
                        else:
                            def _install_from_store():
                                import urllib.request as _ur
                                fname = download_url.split('/')[-1].split('?')[0] or 'plugin.meshplugin'
                                tmp = os.path.join(plugin_manager.plugins_dir, f'_store_{fname}')
                                try:
                                    _ur.urlretrieve(download_url, tmp)
                                    result = plugin_manager.install(tmp)
                                finally:
                                    try:
                                        os.remove(tmp)
                                    except Exception:
                                        pass
                                return result
                            try:
                                result = await asyncio.wait_for(
                                    asyncio.to_thread(_install_from_store), timeout=60
                                )
                            except asyncio.TimeoutError:
                                result = {'success': False, 'error': 'Download timed out'}
                            except Exception as e:
                                result = {'success': False, 'error': f'Download failed: {e}'}
                            await websocket.send(json.dumps({
                                'type': 'plugin_installed', **result
                            }, ensure_ascii=False))

                elif data.get('type') == 'list_serial_ports':
                    await websocket.send(json.dumps({
                        'type': 'serial_ports',
                        'ports': list_serial_devices(),
                        'current': mapper.port if mapper else None,
                    }, ensure_ascii=False))

                elif data.get('type') == 'subscribe_plugin':
                    # A browser wants one plugin's channel. Recorded per
                    # connection and dropped when the connection goes away.
                    channel = data.get('channel', '')
                    if isinstance(channel, str) and channel.startswith('plugin:'):
                        plugin_subscriptions.setdefault(websocket, set()).add(channel)
                    else:
                        print(f"[WS] Ignoring subscribe_plugin with bad channel: {channel!r}")

                elif data.get('type') == 'unsubscribe_plugin':
                    channel = data.get('channel', '')
                    subs = plugin_subscriptions.get(websocket)
                    if subs:
                        subs.discard(channel)

                elif data.get('type') == 'plugin_message':
                    # Forward plugin WebSocket message to plugin handler
                    if plugin_manager:
                        channel = data.get('channel', '')
                        plugin_id = plugin_manager.ws_channels.get(channel)
                        if plugin_id and plugin_id in plugin_manager.plugins:
                            plugin = plugin_manager.plugins[plugin_id]

                            async def plugin_reply(payload, _ws=websocket,
                                                   _pid=plugin_id, _ch=channel):
                                """Answer only the client that asked.

                                broadcast_ws() goes to every connected browser,
                                which is wrong for a response — it wastes
                                bandwidth and can hand one user's data to all.
                                """
                                try:
                                    await _ws.send(json.dumps({
                                        'type': 'plugin_data',
                                        'plugin_id': _pid,
                                        'channel': _ch,
                                        'data': payload
                                    }, ensure_ascii=False))
                                except Exception as e:
                                    print(f"[PLUGIN:{_pid}] WS reply failed: {e}")

                            try:
                                if hasattr(plugin, 'on_ws_request'):
                                    await plugin.on_ws_request(
                                        data.get('data', {}), channel, plugin_reply)
                                elif hasattr(plugin, 'on_ws_message'):
                                    await plugin.on_ws_message(data.get('data', {}), channel)
                            except Exception as e:
                                print(f"[PLUGIN:{plugin_id}] WS message error: {e}")

                elif data.get('type') == 'get_elevation':
                    locations = data.get('locations', [])
                    if locations and len(locations) <= 100:
                        try:
                            import urllib.request
                            loc_str = '|'.join([f"{p['latitude']},{p['longitude']}" for p in locations])
                            url = f"https://api.opentopodata.org/v1/srtm90m?locations={loc_str}"
                            req = urllib.request.Request(url, headers={'User-Agent': 'MeshtasticMapper/1.17'})
                            with urllib.request.urlopen(req, timeout=10) as resp:
                                result = json.loads(resp.read().decode())
                            await websocket.send(json.dumps({
                                'type': 'elevation_data',
                                'elevations': [r['elevation'] for r in result.get('results', [])]
                            }, ensure_ascii=False))
                        except Exception as e:
                            await websocket.send(json.dumps({
                                'type': 'elevation_data',
                                'error': str(e)
                            }, ensure_ascii=False))
                elif data.get('type') == 'get_messages':
                    if mapper:
                        channel_names = {}
                        try:
                            iface = None
                            if mapper.connection_type == 'serial' and mapper._serial_iface:
                                iface = mapper._serial_iface.iface
                            elif mapper.connection_type == 'tcp' and mapper._tcp_iface:
                                iface = mapper._tcp_iface
                            if iface:
                                for ch in iface.localNode.channels:
                                    if ch.role != 0:  # not DISABLED
                                        name = ch.settings.name or ('Primary' if ch.index == 0 else f'Ch {ch.index}')
                                        channel_names[ch.index] = name
                        except:
                            pass
                        await websocket.send(json.dumps({
                            'type': 'messages_data',
                            'messages': mapper.messages,
                            'channel_names': channel_names
                        }, ensure_ascii=False))

                elif data.get('type') == 'request_position':
                    node_id = data.get('node_id')
                    if not node_id:
                        await websocket.send(json.dumps({'type': 'error', 'message': 'No node_id provided'}, ensure_ascii=False))
                        return
                    try:
                        from meshtastic.protobuf import mesh_pb2, portnums_pb2

                        loop = asyncio.get_event_loop()

                        def _do_request():
                            iface = None
                            if mapper.connection_type == 'serial' and mapper._serial_iface and mapper._serial_iface.iface:
                                iface = mapper._serial_iface.iface
                            elif mapper.connection_type == 'tcp' and mapper._tcp_iface and mapper._tcp_iface.interface:
                                iface = mapper._tcp_iface.interface

                            if not iface:
                                raise Exception("Not connected")

                            # Send empty position packet with wantResponse=True
                            # This requests the destination node to send back its position
                            pos = mesh_pb2.Position()
                            iface.sendData(
                                pos.SerializeToString(),
                                destinationId=node_id,
                                portNum=portnums_pb2.PortNum.POSITION_APP,
                                wantAck=False,
                                wantResponse=True
                            )

                        await loop.run_in_executor(None, _do_request)
                        print(f"[POS] Requested position from {node_id}")
                        await websocket.send(json.dumps({'type': 'position_requested', 'node_id': node_id}, ensure_ascii=False))

                    except Exception as e:
                        print(f"[POS] Error requesting position from {node_id}: {e}")
                        await websocket.send(json.dumps({'type': 'error', 'message': f'Position request failed: {e}'}, ensure_ascii=False))

                elif data.get('type') == 'remote_admin_get':
                    # Read-only remote administration over PKC admin messages
                    dest = str(data.get('node_id', '')).strip().lower()
                    section = str(data.get('section', ''))
                    if mapper:
                        try:
                            res = await asyncio.to_thread(mapper.remote_admin_get, dest, section)
                        except Exception as e:
                            print(f"[ADMIN] {dest} {section}: {e}")
                            res = {'dest': dest, 'section': section, 'ok': False, 'error': 'EXCEPTION', 'hint': str(e), 'steps': []}
                    else:
                        res = {'dest': dest, 'section': section, 'ok': False, 'error': 'NOT_CONNECTED', 'steps': []}
                    print(f"[ADMIN] get {section} from {dest}: {'OK' if res.get('ok') else res.get('error')}"
                          f"{' in %.1f s' % res['rtt'] if res.get('rtt') else ''}")
                    await websocket.send(safe_json({'type': 'remote_admin_result', 'data': res}))
                elif data.get('type') == 'remote_admin_set':
                    dest = str(data.get('node_id', '')).strip().lower()
                    section = str(data.get('section', ''))
                    if mapper:
                        try:
                            res = await asyncio.to_thread(mapper.remote_admin_set, dest, section, data.get('changes'))
                        except Exception as e:
                            print(f"[ADMIN] set {dest} {section}: {e}")
                            res = {'dest': dest, 'section': section, 'ok': False, 'error': 'EXCEPTION', 'hint': str(e), 'steps': []}
                    else:
                        res = {'dest': dest, 'section': section, 'ok': False, 'error': 'NOT_CONNECTED', 'steps': []}
                    await websocket.send(safe_json({'type': 'remote_admin_set_result', 'data': res}))
                elif data.get('type') in ('remote_admin_state', 'remote_admin_arm', 'remote_admin_disarm'):
                    who = f"{client_addr[0]}" if client_addr else 'unknown'
                    if not mapper:
                        state = {'enabled': False, 'permanent': False, 'armed': False, 'remaining': 0, 'recent': []}
                    elif data['type'] == 'remote_admin_arm':
                        state = await asyncio.to_thread(mapper.remote_admin_arm, data.get('minutes', 60), who)
                    elif data['type'] == 'remote_admin_disarm':
                        state = await asyncio.to_thread(mapper.remote_admin_disarm, who)
                    else:
                        state = await asyncio.to_thread(mapper.remote_admin_state)
                    await websocket.send(safe_json({'type': 'remote_admin_state', 'data': state}))
                elif data.get('type') == 'remote_admin_reboot':
                    dest = str(data.get('node_id', '')).strip().lower()
                    if mapper:
                        try:
                            res = await asyncio.to_thread(mapper.remote_admin_reboot, dest, data.get('seconds', 5))
                        except Exception as e:
                            print(f"[ADMIN] reboot {dest}: {e}")
                            res = {'dest': dest, 'ok': False, 'error': 'EXCEPTION', 'hint': str(e), 'steps': []}
                    else:
                        res = {'dest': dest, 'ok': False, 'error': 'NOT_CONNECTED', 'steps': []}
                    await websocket.send(safe_json({'type': 'remote_admin_reboot_result', 'data': res}))
                elif data.get('type') == 'ping_device':
                    try:
                        is_connected = False
                        if mapper:
                            if mapper.connection_type == 'serial' and mapper._serial_iface and mapper._serial_iface.iface:
                                is_connected = True
                            elif mapper.connection_type == 'tcp' and mapper._tcp_iface and mapper._tcp_iface.interface:
                                is_connected = True
                        await websocket.send(json.dumps({
                            'type': 'pong_device',
                            'connected': is_connected
                        }, ensure_ascii=False))
                    except Exception as e:
                        await websocket.send(json.dumps({
                            'type': 'pong_device',
                            'connected': False
                        }, ensure_ascii=False))

                elif data.get('type') == 'test_coverage':
                    try:
                        import urllib.request
                        import urllib.error
                        cov_url = data.get('url', '').strip()
                        cov_key = data.get('api_key', '').strip()
                        if not cov_url:
                            cov_url = (mapper.config.get('coverage_server_url', '') if mapper else '') or 'https://coverage.meshpulse.app'
                        if not cov_key:
                            cov_key = (mapper.config.get('coverage_api_key', '') if mapper else '')

                        health_url = f"{cov_url.rstrip('/')}/health"
                        req = urllib.request.Request(
                            health_url,
                            method='GET',
                            headers={
                                'X-Api-Key': cov_key,
                                'User-Agent': f'MeshtasticMapper/{MAPPER_VERSION}',
                            }
                        )
                        loop = asyncio.get_event_loop()
                        def _do_health_check():
                            with urllib.request.urlopen(req, timeout=10) as resp:
                                return json.loads(resp.read().decode())
                        result = await loop.run_in_executor(None, _do_health_check)
                        print(f"[COVERAGE] Health check OK: {result}")
                        await websocket.send(json.dumps({
                            'type': 'test_coverage_result',
                            'success': True,
                            'data': result
                        }, ensure_ascii=False))
                    except Exception as e:
                        print(f"[COVERAGE] Health check failed: {e}")
                        await websocket.send(json.dumps({
                            'type': 'test_coverage_result',
                            'success': False,
                            'error': str(e)
                        }, ensure_ascii=False))

                elif data.get('type') == 'get_coverage':
                    try:
                        import urllib.request
                        import urllib.error
                        cov_url = (mapper.config.get('coverage_server_url', '') if mapper else '') or 'https://coverage.meshpulse.app'
                        cov_key = (mapper.config.get('coverage_api_key', '') if mapper else '')
                        payload = json.dumps({
                            'lat': data.get('lat'),
                            'lon': data.get('lon'),
                            'tx_power_dbm': data.get('tx_power_dbm', 27),
                            'frequency_mhz': data.get('frequency_mhz', 868),
                            'height_agl_m': data.get('height_agl_m', 10),
                            'antenna_gain_dbi': data.get('antenna_gain_dbi', 2),
                            'max_range_km': data.get('max_range_km', mapper.config.get('coverage_max_range_km', 25)),
                        }).encode('utf-8')
                        req = urllib.request.Request(
                            f"{cov_url.rstrip('/')}/coverage",
                            data=payload,
                            method='POST',
                            headers={
                                'Content-Type': 'application/json',
                                'X-Api-Key': cov_key,
                                'User-Agent': f'MeshtasticMapper/{MAPPER_VERSION}',
                            }
                        )
                        loop = asyncio.get_event_loop()
                        def _do_coverage_request():
                            with urllib.request.urlopen(req, timeout=180) as resp:
                                return json.loads(resp.read().decode())
                        result = await loop.run_in_executor(None, _do_coverage_request)
                        print(f"[COVERAGE] Done in {result.get('duration_ms', '?')}ms")
                        await websocket.send(json.dumps({
                            'type': 'coverage_data',
                            'png_base64': result.get('png_base64'),
                            'bounds': result.get('bounds'),
                            'duration_ms': result.get('duration_ms'),
                        }, ensure_ascii=False))
                    except Exception as e:
                        print(f"[COVERAGE] Error: {e}")
                        await websocket.send(json.dumps({
                            'type': 'coverage_data',
                            'error': str(e)
                        }, ensure_ascii=False))

                elif data.get('type') == 'create_backup':
                    import io, zipfile, base64, socket as _socket
                    from datetime import datetime, timezone as _tz
                    mode = data.get('mode', 'quick')
                    def _do_backup():
                        buf = io.BytesIO()
                        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
                            info = {
                                'backup_type': mode,
                                'mapper_version': MAPPER_VERSION,
                                'timestamp': datetime.now(_tz.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                                'hostname': _socket.gethostname(),
                                'node_count': (len(mapper.nodes) + len(mapper.nodes_no_position)) if mapper else 0,
                                'plugin_count': len(plugin_manager.enabled_ids) if plugin_manager else 0,
                            }
                            zf.writestr('backup_info.json', json.dumps(info, indent=2))
                            if os.path.exists(CONFIG_PATH):
                                zf.write(CONFIG_PATH, 'config.json')
                            if plugin_manager:
                                enabled_path = os.path.join(plugin_manager.plugins_dir, 'enabled.json')
                                if os.path.exists(enabled_path):
                                    zf.write(enabled_path, 'plugins/enabled.json')
                                for author in os.listdir(plugin_manager.plugins_dir):
                                    author_dir = os.path.join(plugin_manager.plugins_dir, author)
                                    if not os.path.isdir(author_dir) or author.startswith('_'):
                                        continue
                                    for name in os.listdir(author_dir):
                                        cfg = os.path.join(author_dir, name, 'config.json')
                                        if os.path.exists(cfg):
                                            zf.write(cfg, f'plugins/{author}/{name}/config.json')
                            if mode == 'full':
                                nodes_path = mapper.json_path if mapper else '/var/www/html/meshpulse/nodes.json'
                                if os.path.exists(nodes_path):
                                    zf.write(nodes_path, 'nodes.json')
                                if os.path.exists(StatsDB.DB_PATH):
                                    # A consistent copy, not the live file (WAL, writes in flight).
                                    sdb = mapper.stats_db if mapper else StatsDB()
                                    fd, snap = tempfile.mkstemp(prefix='meshpulse-stats-', suffix='.db')
                                    os.close(fd)
                                    try:
                                        sdb.snapshot(snap)
                                        zf.write(snap, 'stats.db')
                                    finally:
                                        os.remove(snap)
                        buf.seek(0)
                        return base64.b64encode(buf.read()).decode('ascii')
                    try:
                        b64 = await asyncio.to_thread(_do_backup)
                        ts = datetime.now().strftime('%Y%m%d-%H%M')
                        await websocket.send(json.dumps({
                            'type': 'backup_ready',
                            'filename': f'meshtastic-backup-{mode}-{ts}.zip',
                            'data': b64,
                        }, ensure_ascii=False))
                        print(f"[BACKUP] {mode} backup sent to client")
                    except Exception as e:
                        print(f"[BACKUP] Error: {e}")
                        await websocket.send(json.dumps({
                            'type': 'backup_ready', 'error': str(e),
                        }, ensure_ascii=False))

                elif data.get('type') == 'restore_backup':
                    # One message, so 64 KB at most. The Config page uploads
                    # backups in chunks (upload_begin / upload_chunk).
                    import io
                    try:
                        src = io.BytesIO(base64.b64decode(data.get('data', '')))
                    except ValueError:
                        src = io.BytesIO(b'')
                    await _run_restore(websocket, src)

                else:
                    print(f"[WS] Unknown message type from {client_addr}: {data.get('type')}")
            except json.JSONDecodeError:
                print(f"[WS] Invalid JSON from {client_addr}")
    except websockets.exceptions.ConnectionClosed:
        print(f"[WS] Client disconnected: {client_addr}")
    finally:
        if plugin_manager:
            await plugin_manager.dispatch_hook('on_ws_client_disconnect', {'client_id': str(client_addr)})
        connected_clients.discard(websocket)
        plugin_subscriptions.pop(websocket, None)
        _upload_drop_where(lambda u: u['ws'] is websocket, 'client disconnected')
        print(f"[WS] Client removed: {client_addr}, total clients: {len(connected_clients)}")


PACKET_EVENT_MAX_AGE_MS = 5000


async def flush_packet_events():
    """Every second, send what the radio heard during that second.

    packet_batch carries every event, for live map plugins (api.packets).
    packet_event, the most recent one alone, stays for the Stats page and
    older pages. RF only: the queue never holds MQTT packets.

    With nobody connected the queue is still emptied, so a page opened
    later is not handed packets from minutes ago as if they were live.
    """
    while True:
        await asyncio.sleep(1)
        if not mapper or not mapper._packet_event_queue:
            continue
        events = []
        while mapper._packet_event_queue:
            events.append(mapper._packet_event_queue.popleft())
        if not connected_clients:
            continue
        cutoff = int(time.time() * 1000) - PACKET_EVENT_MAX_AGE_MS
        events = [e for e in events if e.get('ts', 0) >= cutoff]
        if not events:
            continue
        try:
            clients = set(connected_clients)
            websockets.broadcast(clients, safe_json({
                'type': 'packet_batch',
                'events': [{k: v for k, v in e.items() if k != 'type'} for e in events],
            }))
            websockets.broadcast(clients, safe_json(events[-1]))
        except Exception:
            pass


async def start_websocket_server():
    """Start WebSocket server"""
    print("[WS] Starting WebSocket server on ws://0.0.0.0:8765")
    async with websockets.serve(websocket_handler, "0.0.0.0", 8765, compression=None, max_size=128*1024):
        asyncio.create_task(flush_packet_events())
        await asyncio.Future()  # Run forever


def plugin_claimed_serial_ports():
    """Serial devices that enabled plugins have configured for themselves.

    Serial auto-detection runs before the plugin manager exists, so the stored
    plugin configs are read straight off disk here. Every value is resolved with
    realpath: a plugin pointed at a stable /dev/serial/by-id/... symlink must
    still shadow the /dev/ttyUSB0 that symlink resolves to, which is exactly the
    device naive auto-detection would otherwise hand to the mesh interface.

    Returns:
        dict: {resolved device path: plugin id} — auto-detection must not pick
            these, and the UI can say who holds each one.
    """
    claimed = {}
    # Same rule as PluginManager: plugins/ sits next to backend/ in the repo
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    plugins_dir = os.path.join(repo_root, 'plugins')

    try:
        with open(os.path.join(plugins_dir, 'enabled.json'), 'r') as f:
            enabled_ids = json.load(f)
    except Exception:
        return claimed          # no plugins installed, or unreadable — claim nothing

    for plugin_id in enabled_ids or []:
        parts = str(plugin_id).split('/', 1)
        try:
            with open(os.path.join(plugins_dir, *parts, 'config.json'), 'r') as f:
                plugin_config = json.load(f)
        except Exception:
            continue            # plugin never configured, or config unreadable
        if not isinstance(plugin_config, dict):
            continue
        for value in plugin_config.values():
            if isinstance(value, str) and value.strip().startswith('/dev/'):
                claimed[os.path.realpath(value.strip())] = str(plugin_id)

    return claimed


def list_serial_devices():
    """Enumerate serial devices a radio could be attached to.

    Prefers /dev/serial/by-id/ entries: those names survive a reboot, while
    ttyUSB0/ttyACM0 numbering depends on the order things were plugged in. Each
    entry says what the device calls itself and whether a plugin already speaks
    to it, so a user picking a port can see that "this one is the Meshcore
    radio" rather than finding out by watching two processes fight over it.

    Returns:
        list: dicts with path, dev, label and claimed_by (plugin id or None),
            sorted by label.
    """
    claimed = plugin_claimed_serial_ports()
    by_id_dir = '/dev/serial/by-id'
    devices = {}

    def add(path, label):
        try:
            resolved = os.path.realpath(path)
        except OSError:
            resolved = path
        # by-id wins over a bare tty for the same device: it is the stable name
        if resolved in devices and devices[resolved]['path'].startswith(by_id_dir):
            return
        devices[resolved] = {
            'path': path,
            'dev': resolved,
            'label': label,
            'claimed_by': claimed.get(resolved),
        }

    try:
        for name in sorted(os.listdir(by_id_dir)):
            # usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0
            #   -> "Silicon Labs CP2102 USB to UART Bridge Controller 0001"
            label = re.sub(r'^usb-', '', name)
            label = re.sub(r'-if\d+.*$', '', label).replace('_', ' ').strip()
            add(os.path.join(by_id_dir, name), label or name)
    except OSError:
        pass        # no by-id on this system (or no USB serial at all)

    for path in ('/dev/ttyUSB0', '/dev/ttyUSB1', '/dev/ttyUSB2',
                 '/dev/ttyACM0', '/dev/ttyACM1', '/dev/ttyACM2'):
        if os.path.exists(path):
            add(path, os.path.basename(path))

    return sorted(devices.values(), key=lambda d: d['label'].lower())


def run_websocket_server_thread():
    """Run WebSocket server in separate thread"""
    asyncio.run(start_websocket_server())


if __name__ == '__main__':
    print("Starting MeshPulse (Listen Mode with WebSocket + TCP)...")

    # Create output directory
    os.makedirs('/var/www/html/meshpulse', exist_ok=True)

    possible_ports = [
        '/dev/ttyUSB0', '/dev/ttyUSB1', '/dev/ttyUSB2',
        '/dev/ttyACM0', '/dev/ttyACM1', '/dev/ttyACM2'
    ]

    def detect_serial_port():
        claimed = plugin_claimed_serial_ports()
        for p in possible_ports:
            if not os.path.exists(p):
                continue
            if os.path.realpath(p) in claimed:
                # A plugin (e.g. Meshcore) already talks to this radio. Taking it
                # would have both sides fighting over the same file descriptor and
                # neither getting a usable reply.
                print(f"[SERIAL] Skipping {p} — claimed by an enabled plugin")
                continue
            return p
        return None

    # Load config
    report_config_source()
    config = load_config()
    connection_type = config.get('connection_type', 'serial')
    host = config.get('host')
    port = config.get('port')

    # Auto-detect serial port if needed
    if connection_type == 'serial' and not port:
        port = detect_serial_port()
        if not port:
            print("WARNING: No serial port found")
            print("Checked:", possible_ports)
            claimed_now = plugin_claimed_serial_ports()
            if claimed_now:
                print("Skipped (claimed by enabled plugins):", sorted(claimed_now))
                print("Set \"port\" explicitly in config.json if a device is shared.")
            print("Waiting for TCP connection configuration via web interface...")
            print(f"Open http://localhost/meshpulse/ and configure TCP connection")

    if connection_type == 'serial' and not port and not host:
        print("Connection: Waiting for configuration...\n")
    else:
        print(f"Connection: {connection_type} {host or port or ''}\n")

    try:
        # Start WebSocket server in background thread (exactly once)
        ws_thread = threading.Thread(target=run_websocket_server_thread, daemon=True)
        ws_thread.start()
        print("[WS] WebSocket server thread started")

        # Give WebSocket server time to start
        time.sleep(2)

        # If no connection configured, wait for TCP config from web UI
        if connection_type == 'serial' and not port and not host:
            print("[WAIT] No connection configured - waiting for TCP config from web UI...")
            while not restart_event.is_set():
                time.sleep(1)
            restart_event.clear()
            connection_type = restart_config.get('connection_type', 'tcp')
            host = restart_config.get('host')
            port = restart_config.get('port')
            print(f"[CONFIG] Received from web UI: {connection_type} {host or port or ''}")

        # Initialize plugin system (once, before mapper loop) — plugins are
        # enabled later, after the first mapper instance exists
        if PLUGINS_AVAILABLE:
            plugin_manager = PluginManager(mapper=None, mapper_version=MAPPER_VERSION)
            plugin_manager._connected_clients = connected_clients
            plugin_manager._plugin_subscriptions = plugin_subscriptions

        # Mapper loop with runtime restart support
        _watchdog_started = False
        _plugins_loaded = False
        while True:
            mapper = ListenBasedMapper(
                connection_type=connection_type,
                port=port,
                host=host,
                max_age=86400
            )
            mapper.config = load_config()
            mapper.stats_db.retention_days = StatsDB.clamp_retention(
                mapper.config.get('stats_retention_days'))
            print(f"[STATS] Raw history kept {mapper.stats_db.retention_days} days, "
                  f"hourly rollup kept indefinitely")
            # Update plugin_manager's mapper reference for each new mapper
            # instance — plugins resolve their mapper through it lazily
            if plugin_manager:
                plugin_manager.mapper = mapper
                # Enable plugins exactly once per process, now that a live
                # mapper exists — on_enable() can inject nodes immediately.
                # Guarded: connection restarts re-enter this loop and must
                # not re-enable plugins (would restart their workers).
                if not _plugins_loaded:
                    _plugins_loaded = True
                    plugin_manager.load_enabled_plugins()
                    t = threading.Thread(target=_plugin_update_watcher, daemon=True)
                    t.start()
            if not _watchdog_started:
                watchdog_thread = threading.Thread(target=mapper._watchdog_loop, daemon=True)
                watchdog_thread.start()
                _watchdog_started = True
            mapper.run()

            if restart_event.is_set():
                restart_event.clear()

                if traceroute_restart:
                    traceroute_restart = False
                    # Traceroute-triggered restart: keep same connection, don't clear nodes.json
                    print("[RESTART] Traceroute restart - resuming listener, keeping data")
                elif send_restart:
                    send_restart = False
                    # Send-triggered restart: keep same connection, don't clear nodes.json
                    print("[RESTART] Send restart - resuming listener, keeping data")
                else:
                    # Normal connection change
                    connection_type = restart_config.get('connection_type', 'serial')
                    host = restart_config.get('host')
                    port = restart_config.get('port')
                    keep_data = restart_config.get('keep_data', False)

                    # Clear nodes.json if not keeping previous data
                    if not keep_data:
                        try:
                            empty_data = {
                                'ts': int(time.time()),
                                'updated': datetime.now().isoformat(),
                                'cnt': 0, 'cnt_no_pos': 0,
                                'max_distance_km': None, 'farthest_node': None,
                                'tracker': {}, 'nodes': [], 'nodes_no_pos': [], 'messages': {}
                            }
                            with open('/var/www/html/meshpulse/nodes.json', 'w') as f:
                                json.dump(empty_data, f, indent=2)
                            print("[RESTART] Cleared nodes.json (fresh scan)")
                        except Exception as e:
                            print(f"[RESTART] Error clearing nodes.json: {e}")

                # Auto-detect port if switching back to serial without a stored port
                if connection_type == 'serial' and not port:
                    port = detect_serial_port()
                    if not port:
                        print("[RESTART] ERROR: No serial port found, keeping previous config")
                        connection_type = mapper.connection_type
                        host = mapper.host
                        port = mapper.port

                print(f"[RESTART] New connection: {connection_type} {host or port or ''}")
            else:
                break

    except KeyboardInterrupt:
        print("\nStopped by user")
    except Exception as e:
        print(f"\nFATAL: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
