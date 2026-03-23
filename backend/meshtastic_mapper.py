#!/usr/bin/env python3
#ver 2.0.4
#Max Gieparda (c)2026
"""
Meshtastic Mapper - Listen Mode with TTL + WebSocket
Works on slow Raspberry Pi Model B+
Real-time updates via WebSocket
"""
import subprocess
import json
import time
import re
from datetime import datetime
import sys
import os
import asyncio
import websockets
import threading
import base64
import sqlite3
import meshtastic
import meshtastic.tcp_interface
import meshtastic.serial_interface
from meshtastic import mesh_pb2, portnums_pb2
from meshtastic.protobuf import config_pb2

VERSION = '2.0.4'

# Global set of connected WebSocket clients
connected_clients = set()

# Config path (shared with frontend)
CONFIG_PATH = '/var/www/html/meshtastic/config.json'

# Runtime restart support
mapper = None
restart_event = threading.Event()
restart_config = {}
traceroute_restart = False      # Legacy flag - kept for compatibility, no longer used with Python API
send_restart = False            # Legacy flag - kept for compatibility, no longer used with Python API
send_restart_no_nodes = False   # Legacy flag - kept for compatibility, no longer used with Python API


class TCPMeshtasticInterface:
    """Wraps meshtastic.tcp_interface.TCPInterface for use as a TCP listener/sender."""

    def __init__(self, host, port=4403):
        self.host = host
        self.port = port
        self.interface = None
        self._on_receive_ref = None

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

        self._on_receive_ref = _on_receive
        pub.subscribe(_on_receive, "meshtastic.receive")

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

        self._on_receive_ref = _on_receive
        pub.subscribe(_on_receive, "meshtastic.receive")

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


class StatsDB:
    """SQLite database for network statistics - keeps 3 days of data."""

    DB_PATH = '/var/www/html/meshtastic/stats.db'
    RETENTION_DAYS = 3

    def __init__(self):
        self.lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with self.lock:
            conn = sqlite3.connect(self.DB_PATH)
            c = conn.cursor()
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
            c.execute('CREATE INDEX IF NOT EXISTS idx_neighbors_ts ON neighbors(ts)')
            c.execute('CREATE INDEX IF NOT EXISTS idx_neighbors_from ON neighbors(from_id)')
            conn.commit()
            conn.close()

    def log_packet(self, from_id, from_name, portnum, hops, snr, rssi, via_mqtt, relay_node_id, relayed_by_us):
        with self.lock:
            conn = sqlite3.connect(self.DB_PATH)
            conn.execute('''INSERT INTO packets
                (ts, from_id, from_name, portnum, hops, snr, rssi, via_mqtt, relay_node_id, relayed_by_us)
                VALUES (?,?,?,?,?,?,?,?,?,?)''',
                (int(time.time()), from_id, from_name, portnum, hops, snr, rssi,
                 1 if via_mqtt else 0, relay_node_id, 1 if relayed_by_us else 0))
            conn.commit()
            conn.close()

    def log_anomaly(self, node_id, node_name, anomaly_type, details, severity='warning'):
        with self.lock:
            conn = sqlite3.connect(self.DB_PATH)
            conn.execute('''INSERT INTO anomalies (ts, node_id, node_name, anomaly_type, details, severity)
                VALUES (?,?,?,?,?,?)''',
                (int(time.time()), node_id, node_name, anomaly_type, details, severity))
            conn.commit()
            conn.close()

    def cleanup_old_data(self):
        """Remove data older than RETENTION_DAYS."""
        cutoff = int(time.time()) - (self.RETENTION_DAYS * 86400)
        with self.lock:
            conn = sqlite3.connect(self.DB_PATH)
            conn.execute('DELETE FROM packets WHERE ts < ?', (cutoff,))
            conn.execute('DELETE FROM node_activity WHERE ts < ?', (cutoff,))
            conn.execute('DELETE FROM anomalies WHERE ts < ?', (cutoff,))
            conn.execute('DELETE FROM neighbors WHERE ts < ?', (cutoff,))
            conn.commit()
            conn.close()

    def clear_node_packets(self, node_id):
        """Delete all packet history for a specific node from stats.db."""
        with self.lock:
            conn = sqlite3.connect(self.DB_PATH)
            conn.execute('DELETE FROM packets WHERE from_id = ?', (node_id,))
            conn.execute('DELETE FROM anomalies WHERE node_id = ?', (node_id,))
            conn.commit()
            conn.close()

    def update_node_name(self, node_id, name):
        """Update from_name for all packets from this node where name was unknown (= node_id)."""
        if not name or name == node_id:
            return
        with self.lock:
            conn = sqlite3.connect(self.DB_PATH)
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
            conn = sqlite3.connect(self.DB_PATH)
            conn.execute('DELETE FROM neighbors WHERE from_id = ?', (from_id,))
            for neighbor in neighbors:
                conn.execute('''INSERT INTO neighbors (ts, from_id, from_name, neighbor_id, neighbor_name, snr)
                    VALUES (?,?,?,?,?,?)''',
                    (now, from_id, from_name, neighbor['id'], neighbor.get('name', neighbor['id']), neighbor.get('snr')))
            conn.commit()
            conn.close()

    def get_neighbor_graph(self):
        """Get neighbor graph for topology visualization. Only returns data from last 6 hours."""
        since = int(time.time()) - 21600
        with self.lock:
            conn = sqlite3.connect(self.DB_PATH)
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
            conn = sqlite3.connect(self.DB_PATH)
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
            conn = sqlite3.connect(self.DB_PATH)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()

            total = c.execute('SELECT COUNT(*) as cnt FROM packets WHERE ts > ?', (since_24h,)).fetchone()['cnt']
            relayed = c.execute('SELECT COUNT(*) as cnt FROM packets WHERE ts > ? AND relayed_by_us = 1', (since_24h,)).fetchone()['cnt']
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
            own_variants = [
                local_node_id or '',
                (local_node_id or '').lstrip('!'),
                '!' + (local_node_id or '').lstrip('!')
            ]
            relayed_nodes = c.execute('''SELECT from_id, MAX(from_name) as from_name, COUNT(*) as cnt
                FROM packets WHERE ts > ? AND relayed_by_us = 1
                AND from_id NOT IN (?, ?, ?)
                GROUP BY from_id ORDER BY cnt DESC LIMIT 20''',
                (since_24h, *own_variants)).fetchall()
            anomalies = c.execute('''SELECT ts, node_id, node_name, anomaly_type, details, severity
                FROM anomalies WHERE ts > ? ORDER BY ts DESC LIMIT 50''', (since_24h,)).fetchall()
            by_type = c.execute('''SELECT portnum, COUNT(*) as cnt
                FROM packets WHERE ts > ?
                GROUP BY portnum ORDER BY cnt DESC''', (since_24h,)).fetchall()
            topology = c.execute('''SELECT from_id, MAX(from_name) as from_name, COUNT(*) as relay_count
                FROM packets WHERE ts > ? AND relayed_by_us = 1
                GROUP BY from_id''', (since_24h,)).fetchall()

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

            conn.close()

            return {
                'total_packets': total,
                'relayed_packets': relayed,
                'relay_percentage': round(relayed / total * 100, 1) if total > 0 else 0,
                'top_senders': [dict(r) for r in top_senders],
                'active_node_count': active_node_count,
                'hourly_activity': [{'hour': r['hour'], 'count': r['cnt']} for r in hourly],
                'relayed_nodes': [dict(r) for r in relayed_nodes],
                'anomalies': [dict(r) for r in anomalies],
                'packet_types': [dict(r) for r in by_type],
                'topology': [dict(r) for r in topology],
                'snr_distribution': [{'bucket': r['bucket'], 'cnt': r['cnt']} for r in snr_dist],
                'rssi_distribution': [{'bucket': r['bucket'], 'cnt': r['cnt']} for r in rssi_dist],
                'hop_distribution': [{'hops': r['hops'], 'cnt': r['cnt']} for r in hop_dist],
                'daily_7d': [{'day': r['day'], 'count': r['cnt']} for r in daily_7d],
                'data_window_minutes': data_window_minutes
            }

    def get_topology_graph(self):
        """Get node connections for D3.js graph."""
        since = int(time.time()) - 86400
        with self.lock:
            conn = sqlite3.connect(self.DB_PATH)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            nodes_data = c.execute('''SELECT DISTINCT from_id, MAX(from_name) as from_name, COUNT(*) as packet_count
                FROM packets WHERE ts > ? GROUP BY from_id''', (since,)).fetchall()
            conn.close()
            return [dict(r) for r in nodes_data]


def load_config():
    """Load connection config from JSON file"""
    try:
        with open(CONFIG_PATH, 'r') as f:
            return json.load(f)
    except Exception:
        return {'connection_type': 'serial', 'port': None, 'host': None}


def save_config(connection_type, host=None, port=None):
    """Save connection config to JSON file"""
    config = {'connection_type': connection_type, 'host': host, 'port': port}
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, 'w') as f:
            json.dump(config, f, indent=2)
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
        self._pending_traceroute_result = None
        self.json_path = '/var/www/html/meshtastic/nodes.json'
        self.meshtastic_cmd = os.path.expanduser('~/.local/bin/meshtastic')
        self.max_age = max_age

        # Load existing nodes or start fresh
        self._loaded_radio_stats = None  # Populated by load_existing_nodes if available
        self.nodes_no_position = {}  # Nodes without GPS position
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
                'channels': channels
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
            'firmware': 'Unknown'
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

                    # Always restore radio_stats regardless of node count
                    saved_radio_stats = data.get('tracker', {}).get('radio_stats')
                    if saved_radio_stats:
                        self._loaded_radio_stats = saved_radio_stats

                    return nodes if (existing_count > 0 or existing_no_pos > 0) else {}
        except Exception as e:
            print(f"[LOAD] Starting fresh (no existing data): {e}")
    
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
                asyncio.run(self.broadcast_node_deleted(node_id))

        if removed:
            hours = self.max_age // 3600
            print(f"[CLEAN] Removed {len(removed)} old nodes (>{hours}h old)")
            for node_id in removed[:5]:  # Show first 5
                print(f"  - {node_id}")
            # Remove expired nodes from name cache if no longer in either node dict
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

        if reboot:
            try:
                node.reboot()
                print(f"[CONFIG] Device reboot requested")
            except (BrokenPipeError, OSError):
                print(f"[CONFIG] Connection dropped during reboot (expected)")

        print(f"[CONFIG] Applied: {applied}")
        return applied

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
            from_id = packet.get('fromId')
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
                neighbor_id = f"!{neighbor_num:08x}" if isinstance(neighbor_num, int) else str(neighbor_num)
                neighbor_name = (
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
        except Exception as e:
            print(f"[NEIGHBOR] Parse error: {e}")

    def _update_message_names(self, node_id, name):
        """Update from_name in stored messages when a node's name becomes known"""
        self.known_names[node_id] = name
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

        relayed_by_us = False
        relay_node_id = None
        if relay_node_raw is not None and self.local_node_id:
            our_num = int(self.local_node_id.replace('!', ''), 16)
            if relay_node_raw > 0xFF:
                # Full node number (Python API)
                is_our_relay = (relay_node_raw == our_num)
                is_not_self = (from_id != self.local_node_id)
                relayed_by_us = is_our_relay and is_not_self
                relay_node_id = f"!{relay_node_raw:08x}"
            else:
                # Last byte only (legacy CLI mode)
                our_last_byte = our_num & 0xFF
                relayed_by_us = (relay_node_raw == our_last_byte) and (from_id != self.local_node_id)
                relay_node_id = f"relay_{relay_node_raw:02x}"
            # Try to resolve full node ID from known nodes
            for nid in {**self.nodes, **self.nodes_no_position}:
                try:
                    nid_num = int(nid.replace('!', ''), 16)
                    if relay_node_raw > 0xFF:
                        if nid_num == relay_node_raw:
                            relay_node_id = nid
                            break
                    else:
                        if nid_num & 0xFF == relay_node_raw:
                            relay_node_id = nid
                            break
                except:
                    pass

        self._last_radio_packet_time = time.time()  # watchdog reset
        self.stats_db.log_packet(from_id, from_name, portnum, hops, snr, rssi, via_mqtt, relay_node_id, relayed_by_us)

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
                name = node_data.get('user', {}).get('longName', node_id)
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

                    return True

                else:
                    # Node without position - save to separate dict
                    name = node_data.get('user', {}).get('longName', node_id)
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
            if lat == 0 and lon == 0:
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
                    'name': node_id,  # Will be updated when nodeinfo arrives
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
                                 'numPacketsRxBad', 'numRxDupe', 'numTxRelay', 'numOnlineNodes', 'numTotalNodes']
                radio_stats = {}
                for field in radio_fields:
                    m = re.search(rf"'{field}':\s*([\d.]+)", line)
                    if m:
                        val = m.group(1)
                        radio_stats[field] = float(val) if '.' in val else int(val)
                if radio_stats:
                    self.tracker_info['radio_stats'] = radio_stats
                    tracker_updated = True

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

                return True
            elif node_id in self.nodes_no_position:
                self.nodes_no_position[node_id]['ts'] = int(time.time())
                self.nodes_no_position[node_id]['seen_at'] = int(time.time())
                self.nodes_no_position[node_id]['source'] = 'live'
                print(f"♡ {node_id} telemetry heartbeat (no GPS)")

                # Broadcast to WebSocket clients
                asyncio.run(self.broadcast_node_update(self.nodes_no_position[node_id]))
                asyncio.run(self.broadcast_stats_update())
                
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
                'channel_index': channel_index
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

            return True

        except Exception as e:
            print(f"Text message parse error: {e}")

        return False

    # ------------------------------------------------------------------
    # Python API packet parsers (TCP mode — receive already-decoded dicts)
    # ------------------------------------------------------------------

    def _on_tcp_packet(self, packet):
        """Route an incoming TCP API packet to the appropriate parser."""
        portnum = packet.get('decoded', {}).get('portnum', '')
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

    def parse_node_info_from_packet(self, packet):
        """Parse a NODEINFO_APP packet received via the Python API."""
        try:
            decoded = packet.get('decoded', {})
            user = decoded.get('user', {})
            node_id = user.get('id') or packet.get('fromId')
            if not node_id:
                return False

            name = user.get('longName') or node_id
            role = user.get('role', 'CLIENT')
            snr = packet.get('rxSnr', 0)
            via_mqtt = packet.get('viaMqtt', False)

            hop_start = packet.get('hopStart')
            hop_limit = packet.get('hopLimit')
            hops = (hop_start - hop_limit) if (hop_start is not None and hop_limit is not None) else None

            pos = decoded.get('position', {})
            lat_i = pos.get('latitudeI')
            lon_i = pos.get('longitudeI')
            lat = pos.get('latitude') or (lat_i / 1e7 if lat_i is not None else None)
            lon = pos.get('longitude') or (lon_i / 1e7 if lon_i is not None else None)

            if lat is not None and lon is not None and not (lat == 0 and lon == 0):
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
                    'source': 'live'
                }
                # Remove from no-position dict if node now has GPS
                if node_id in self.nodes_no_position:
                    del self.nodes_no_position[node_id]
                    print(f"[GPS] {node_id} moved from no-GPS to GPS list")
                if node_id == self.local_node_id:
                    self.tracker_info['lat'] = round(lat, 6)
                    self.tracker_info['lon'] = round(lon, 6)
                    self.tracker_info['alt'] = alt or 0
                marker = "✚" if is_new else "↻"
                print(f"{marker} {node_id} {name[:20]} @ {lat:.4f},{lon:.4f} [TCP]")
                self._update_message_names(node_id, name)
                relay_node_raw = packet.get('relayNode')
                self.log_packet_to_stats(node_id, 'NODEINFO_APP', hops, snr, None, via_mqtt, relay_node_raw)
                asyncio.run(self.broadcast_node_update(self.nodes[node_id]))
                asyncio.run(self.broadcast_stats_update())
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
                    'source': 'live'
                }
                marker = "✚" if is_new else "↻"
                print(f"{marker} {node_id} {name[:20]} (no GPS) [TCP]")
                self._update_message_names(node_id, name)
                relay_node_raw = packet.get('relayNode')
                self.log_packet_to_stats(node_id, 'NODEINFO_APP', hops, snr, None, via_mqtt, relay_node_raw)
                asyncio.run(self.broadcast_node_update(self.nodes_no_position[node_id]))
                asyncio.run(self.broadcast_stats_update())
            return True
        except Exception as e:
            print(f"[TCP] nodeinfo parse error: {e}")
        return False

    def parse_position_from_packet(self, packet):
        """Parse a POSITION_APP packet received via the Python API."""
        try:
            node_id = packet.get('fromId')
            if not node_id:
                return False

            decoded = packet.get('decoded', {})
            pos = decoded.get('position', {})
            lat_i = pos.get('latitudeI')
            lon_i = pos.get('longitudeI')
            lat = pos.get('latitude') or (lat_i / 1e7 if lat_i is not None else None)
            lon = pos.get('longitude') or (lon_i / 1e7 if lon_i is not None else None)

            if lat is None or lon is None or (lat == 0 and lon == 0):
                return False

            snr = packet.get('rxSnr', 0)
            rssi = packet.get('rxRssi') or None
            via_mqtt = packet.get('viaMqtt', False)
            hop_start = packet.get('hopStart')
            hop_limit = packet.get('hopLimit')
            hops = (hop_start - hop_limit) if (hop_start is not None and hop_limit is not None) else None
            relay_node_raw = packet.get('relayNode')

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
                print(f"↻ {node_id} position update @ {lat:.4f},{lon:.4f} hops={hops} [TCP]")
            else:
                self.nodes[node_id] = {
                    'id': node_id,
                    'name': node_id,
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
                print(f"✚ {node_id} NEW from position @ {lat:.4f},{lon:.4f} hops={hops} [TCP]")

            if node_id == self.local_node_id:
                self.tracker_info['lat'] = round(lat, 6)
                self.tracker_info['lon'] = round(lon, 6)
                self.tracker_info['alt'] = pos.get('altitude', 0)

            self.log_packet_to_stats(node_id, 'POSITION_APP', hops, snr, rssi, via_mqtt, relay_node_raw)
            asyncio.run(self.broadcast_node_update(self.nodes[node_id]))
            asyncio.run(self.broadcast_stats_update())
            return True
        except Exception as e:
            print(f"[TCP] position parse error: {e}")
        return False

    def parse_telemetry_from_packet(self, packet):
        """Parse a TELEMETRY_APP packet received via the Python API."""
        try:
            node_id = packet.get('fromId')
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
                                 'numPacketsRxBad', 'numRxDupe', 'numTxRelay', 'numOnlineNodes', 'numTotalNodes']
                radio_stats = {f: local_stats[f] for f in radio_fields if f in local_stats}
                if not radio_stats:
                    # Fallback: some firmware reports these in deviceMetrics
                    radio_stats = {f: device_metrics[f] for f in radio_fields if f in device_metrics}
                if radio_stats:
                    self.tracker_info['radio_stats'] = radio_stats
                    tracker_updated = True

                if tracker_updated:
                    asyncio.run(self.broadcast_connection_status('connected'))

            rssi = packet.get('rxRssi') or None
            if rssi is not None:
                if node_id in self.nodes:
                    self.nodes[node_id]['rssi'] = rssi
                elif node_id in self.nodes_no_position:
                    self.nodes_no_position[node_id]['rssi'] = rssi

            if node_id != self.local_node_id:
                relay_node_raw = packet.get('relayNode')
                self.log_packet_to_stats(node_id, 'TELEMETRY_APP', None, None, rssi, False, relay_node_raw)

            if node_id in self.nodes:
                self.nodes[node_id]['ts'] = int(time.time())
                self.nodes[node_id]['seen_at'] = int(time.time())
                self.nodes[node_id]['source'] = 'live'
                print(f"♡ {node_id} telemetry heartbeat [TCP]")
                asyncio.run(self.broadcast_node_update(self.nodes[node_id]))
                asyncio.run(self.broadcast_stats_update())
                return True
            elif node_id in self.nodes_no_position:
                self.nodes_no_position[node_id]['ts'] = int(time.time())
                self.nodes_no_position[node_id]['seen_at'] = int(time.time())
                self.nodes_no_position[node_id]['source'] = 'live'
                print(f"♡ {node_id} telemetry heartbeat (no GPS) [TCP]")
                asyncio.run(self.broadcast_node_update(self.nodes_no_position[node_id]))
                asyncio.run(self.broadcast_stats_update())
            return True
        except Exception as e:
            print(f"[TCP] telemetry parse error: {e}")
        return False

    def parse_text_from_packet(self, packet):
        """Parse a TEXT_MESSAGE_APP packet received via the Python API."""
        try:
            from_id = packet.get('fromId')
            if not from_id:
                return False

            to_id = packet.get('toId', '^all')
            text = packet.get('decoded', {}).get('text', '')
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
                'channel_index': channel_index
            }

            if channel_index not in self.messages:
                self.messages[channel_index] = []
            self.messages[channel_index].insert(0, message)
            if len(self.messages[channel_index]) > 50:
                self.messages[channel_index] = self.messages[channel_index][:50]

            dm_marker = " [DM]" if message['is_dm'] else ""
            print(f"💬 [ch{channel_index}] {sender_name}: {text}{dm_marker} [TCP]")
            relay_node_raw = packet.get('relayNode')
            self.log_packet_to_stats(from_id, 'TEXT_MESSAGE_APP', None, None, None, False, relay_node_raw)
            asyncio.run(self.broadcast_message(message))
            return True
        except Exception as e:
            print(f"[TCP] text parse error: {e}")
        return False

    async def broadcast_node_update(self, node_data):
        """Broadcast node update to all connected WebSocket clients"""
        if not connected_clients:
            return

        message = json.dumps({
            'type': 'node_update',
            'node': node_data,
            'timestamp': int(time.time())
        })

        # Broadcast to all connected clients
        websockets.broadcast(set(connected_clients), message)
        print(f"[WS] Broadcasted update for {node_data['id']} to {len(connected_clients)} clients")

    async def broadcast_node_deleted(self, node_id):
        """Broadcast node deletion to all connected WebSocket clients"""
        if not connected_clients:
            return

        message = json.dumps({
            'type': 'node_deleted',
            'node_id': node_id,
            'timestamp': int(time.time())
        })

        # Broadcast to all connected clients
        websockets.broadcast(set(connected_clients), message)
        print(f"[WS] Broadcasted deletion for {node_id} to {len(connected_clients)} clients")

    async def broadcast_message(self, message_data):
        """Broadcast new text message to all connected WebSocket clients"""
        if not connected_clients:
            return

        message = json.dumps({
            'type': 'new_message',
            'message': message_data,
            'timestamp': int(time.time())
        })

        # Broadcast to all connected clients
        websockets.broadcast(set(connected_clients), message)
        print(f"[WS] Broadcasted message from {message_data['from_id']} to {len(connected_clients)} clients")

    async def broadcast_stats_update(self):
        """Broadcast stats update (max distance, farthest node) to all connected WebSocket clients"""
        if not connected_clients:
            return

        max_dist, farthest_id = self.get_max_distance()

        message = json.dumps({
            'type': 'stats_update',
            'max_distance_km': max_dist,
            'farthest_node': farthest_id,
            'timestamp': int(time.time())
        })

        websockets.broadcast(set(connected_clients), message)

    async def broadcast_connection_status(self, status, message=''):
        """Broadcast connection status to all connected WebSocket clients"""
        if not connected_clients:
            return
        msg = json.dumps({
            'type': 'connection_status',
            'status': status,
            'message': str(message),
            'connection_type': self.connection_type,
            'host': self.host,
            'port': self.port,
            'tracker': getattr(self, 'tracker_info', {}),
            'timestamp': int(time.time())
        })
        websockets.broadcast(set(connected_clients), msg)
        print(f"[WS] Connection status: {status} ({self.connection_type})")

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
                    'role': self.tracker_info.get('role', 'ROUTER'),
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

            data = {
                'ts': int(time.time()),
                'updated': datetime.now().isoformat(),
                'cnt': len(nodes_list),
                'cnt_no_pos': len(nodes_no_pos_list),
                'max_distance_km': max_dist,
                'farthest_node': farthest_id,
                'tracker': getattr(self, 'tracker_info', {}),
                'nodes': nodes_list,
                'nodes_no_pos': nodes_no_pos_list,
                'messages': self.messages,
                'known_names': self.known_names
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
                            if lat and lon and not (lat == 0 and lon == 0):
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
                                        'role': node_info.get('user', {}).get('role', 'ROUTER'),
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
                    asyncio.run(self.broadcast_connection_status('connected', 'serial'))
                    self._last_radio_packet_time = time.time()

                serial_iface.connect(
                    on_receive=self._on_serial_packet,
                    on_connection_established=on_connection_established
                )
                print(f"[SERIAL] Connected successfully")
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

                print("[WAIT] Retrying in 10 seconds...")
                time.sleep(10)
                restart_count += 1

            finally:
                if serial_iface is not None:
                    try:
                        serial_iface.disconnect()
                    except Exception:
                        pass
                self._serial_iface = None

    def _on_serial_packet(self, packet):
        """Route incoming serial packets to appropriate parsers (mirrors _on_tcp_packet)."""
        try:
            portnum = packet.get('decoded', {}).get('portnum', '')
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

    def _handle_traceroute_packet(self, packet):
        """Handle incoming traceroute response packet from Python API."""
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

            from_num = packet.get('from', 0)
            to_num = packet.get('to', 0)

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
            print(f"[TRACEROUTE] Error parsing packet: {e}")

    def _run_tcp(self):
        """Run TCP listener using the Python Meshtastic API (no subprocess)."""
        last_save = time.time()
        last_clean = time.time()
        save_interval = 60
        first_save_done = False
        first_save_delay = 10
        clean_interval = 3600
        restart_count = 0

        while True:
            tcp_iface = None
            try:
                print(f"[TCP] Connecting to {self.host} (attempt #{restart_count})...")
                tcp_iface = TCPMeshtasticInterface(self.host)
                self._tcp_iface = tcp_iface
                tcp_iface.connect(self._on_tcp_packet)
                print(f"[TCP] Connected to {self.host}")

                # Read own tracker position from NodeDB at startup
                try:
                    my_info = tcp_iface.getMyNodeInfo()
                    pos = my_info.get('position', {})
                    lat = pos.get('latitude')
                    lon = pos.get('longitude')
                    alt = pos.get('altitude', 0)
                    self.tracker_info['long_name'] = my_info.get('user', {}).get('longName', '')
                    if lat and lon and not (lat == 0 and lon == 0):
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
                                'role': my_info.get('user', {}).get('role', 'ROUTER'),
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

                print("[WAIT] Retrying in 10 seconds...")
                time.sleep(10)
                restart_count += 1

            finally:
                if tcp_iface is not None:
                    try:
                        tcp_iface.disconnect()
                    except Exception:
                        pass

    def run(self):
        """Run meshtastic --listen and parse output"""
        print("=" * 60)
        print(f"Meshtastic Mapper - LISTEN MODE v{VERSION}")
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
            await websocket.send(json.dumps({'type': 'send_result', 'success': False, 'message': 'Mapper not ready'}))
            return

        await websocket.send(json.dumps({
            'type': 'send_status', 'status': 'sending', 'connection_type': mapper.connection_type
        }))

        if mapper.connection_type == 'tcp':
            # TCP: create a separate TCPInterface just for sending; do NOT stop listener
            loop = asyncio.get_event_loop()

            def _do_tcp_send():
                iface = meshtastic.tcp_interface.TCPInterface(
                    hostname=mapper.host, noProto=False
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
            await websocket.send(json.dumps({'type': 'send_result', 'success': success, 'message': msg}))
            await websocket.send(json.dumps({'type': 'send_status', 'status': 'done', 'success': success}))

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
        await websocket.send(json.dumps({'type': 'send_result', 'success': success, 'message': msg}))
        await websocket.send(json.dumps({'type': 'send_status', 'status': 'done', 'success': success}))

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
        await websocket.send(json.dumps({'type': 'send_result', 'success': False, 'message': 'Send timed out'}))
        await websocket.send(json.dumps({'type': 'send_status', 'status': 'done', 'success': False}))
    except Exception as e:
        print(f"[SEND] Error: {e}")
        await websocket.send(json.dumps({'type': 'send_result', 'success': False, 'message': str(e)}))
        await websocket.send(json.dumps({'type': 'send_status', 'status': 'done', 'success': False}))


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
            }))
            return

        conn_type = mapper.connection_type

        # Notify frontend that traceroute is starting
        await websocket.send(json.dumps({
            'type': 'traceroute_status',
            'status': 'starting',
            'connection_type': conn_type
        }))

        loop = asyncio.get_event_loop()

        if conn_type == 'serial':
            try:
                if not mapper._serial_iface or not mapper._serial_iface.iface:
                    await websocket.send(json.dumps({
                        'type': 'traceroute_result',
                        'node_id': node_id, 'error': 'Not connected'
                    }))
                    return

                mapper._pending_traceroute_result = None
                print(f"[TRACEROUTE] Sending traceroute via serial Python API...")
                await loop.run_in_executor(
                    None,
                    lambda: mapper._serial_iface.iface.sendTraceRoute(node_id, hopLimit=5)
                )
                print(f"[TRACEROUTE] Sent, checking for result...")
                # Small yield to let any pending callbacks complete
                await asyncio.sleep(0.5)
                for _ in range(60):
                    await asyncio.sleep(1)
                    if mapper._pending_traceroute_result:
                        result = mapper._pending_traceroute_result
                        mapper._pending_traceroute_result = None
                        all_known = {**mapper.nodes, **mapper.nodes_no_position}
                        for hop in result['route'] + result['route_back']:
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
                            'route': result['route'],
                            'route_back': result['route_back'],
                            'raw': ''
                        }))
                        return

                await websocket.send(json.dumps({
                    'type': 'traceroute_result',
                    'node_id': node_id,
                    'error': 'Timeout - no traceroute response received'
                }))
            except Exception as e:
                await websocket.send(json.dumps({
                    'type': 'traceroute_result',
                    'node_id': node_id, 'error': str(e)
                }))
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
            }))
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
        }))

    except Exception as e:
        print(f"[TRACEROUTE] Error: {e}")
        try:
            await websocket.send(json.dumps({
                'type': 'traceroute_result', 'node_id': node_id, 'error': str(e)
            }))
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
        }))
        return

    print(f"[WS] Connection change: {connection_type} {host or ''}")

    # Determine port for serial
    port = mapper.port if (mapper and connection_type == 'serial') else None

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
    }))

    # Terminate current subprocess to trigger run() exit
    if mapper and mapper.current_process:
        try:
            mapper.current_process.terminate()
        except Exception as e:
            print(f"[WS] Error terminating process: {e}")

    restart_event.set()


# WebSocket server handler
async def websocket_handler(websocket):
    """Handle WebSocket connections"""
    connected_clients.add(websocket)
    client_addr = websocket.remote_address
    print(f"[WS] Client connected: {client_addr}, total clients: {len(connected_clients)}")

    # Send current connection status and tracker info to newly connected client
    if mapper and hasattr(mapper, 'tracker_info'):
        status_msg = json.dumps({
            'type': 'connection_status',
            'status': 'connected',
            'message': '',
            'connection_type': mapper.connection_type,
            'host': mapper.host,
            'port': mapper.port,
            'tracker': mapper.tracker_info,
            'timestamp': int(time.time())
        })
        await websocket.send(status_msg)

    try:
        async for message in websocket:
            try:
                data = json.loads(message)
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
                        stats = mapper.stats_db.get_stats_summary(mapper.local_node_id)
                        stats['local_node_id'] = mapper.local_node_id
                        stats['nodes'] = {**mapper.nodes, **mapper.nodes_no_position}
                        stats['tracker_info'] = getattr(mapper, 'tracker_info', {})
                        # Geographic stats: farthest node, avg distance of direct nodes
                        geo = {'farthest_node_id': None, 'farthest_node_name': None,
                               'farthest_dist_km': None, 'avg_direct_dist_km': None}
                        if mapper.local_node_id and mapper.local_node_id in mapper.nodes:
                            local = mapper.nodes[mapper.local_node_id]
                            if local.get('lat') and local.get('lon'):
                                direct_dists = []
                                for nid, n in mapper.nodes.items():
                                    if nid == mapper.local_node_id: continue
                                    if n.get('hops') != 0 or n.get('via_mqtt'): continue
                                    if not n.get('lat') or not n.get('lon'): continue
                                    d = mapper.calculate_distance(local['lat'], local['lon'], n['lat'], n['lon'])
                                    direct_dists.append((d, nid, n.get('name', nid)))
                                if direct_dists:
                                    direct_dists.sort(reverse=True)
                                    geo['farthest_dist_km'] = round(direct_dists[0][0], 2)
                                    geo['farthest_node_id'] = direct_dists[0][1]
                                    geo['farthest_node_name'] = direct_dists[0][2]
                                    geo['avg_direct_dist_km'] = round(sum(d for d, _, _ in direct_dists) / len(direct_dists), 2)
                        stats['geo'] = geo
                        # Enrich with current node names (override stale DB names)
                        all_current_names = {}
                        for nid, node in mapper.nodes.items():
                            if node.get('name') and node['name'] != nid:
                                all_current_names[nid] = node['name']
                        for nid, node in mapper.nodes_no_position.items():
                            if node.get('name') and node['name'] != nid:
                                all_current_names[nid] = node['name']
                        all_current_names.update(mapper.known_names)
                        for sender in stats.get('top_senders', []):
                            if sender['from_id'] in all_current_names:
                                sender['from_name'] = all_current_names[sender['from_id']]
                        for node in stats.get('relayed_nodes', []):
                            if node['from_id'] in all_current_names:
                                node['from_name'] = all_current_names[node['from_id']]
                        for anomaly in stats.get('anomalies', []):
                            if anomaly.get('node_id') in all_current_names:
                                anomaly['node_name'] = all_current_names[anomaly['node_id']]
                        stats['neighbor_graph'] = mapper.stats_db.get_neighbor_graph()
                        await websocket.send(json.dumps({'type': 'stats_data', 'data': stats}))
                    else:
                        await websocket.send(json.dumps({'type': 'stats_data', 'data': {}}))
                elif data.get('type') == 'get_config':
                    if mapper:
                        try:
                            config = mapper.get_device_config()
                            await websocket.send(json.dumps({
                                'type': 'config_data',
                                'config': config
                            }))
                        except Exception as e:
                            print(f"[CONFIG] get_config error: {e}")
                            await websocket.send(json.dumps({
                                'type': 'config_error',
                                'error': str(e)
                            }))

                elif data.get('type') == 'set_config':
                    if mapper:
                        changes = data.get('changes', {})
                        reboot = data.get('reboot', False)
                        try:
                            result = mapper.apply_device_config(changes, reboot)
                            await websocket.send(json.dumps({
                                'type': 'config_saved',
                                'success': True,
                                'rebooting': reboot,
                                'applied': result
                            }))
                        except (BrokenPipeError, OSError) as e:
                            # Device disconnected after reboot — this is expected
                            print(f"[CONFIG] Connection dropped after save (expected if rebooting): {e}")
                            await websocket.send(json.dumps({
                                'type': 'config_saved',
                                'success': True,
                                'applied': [],
                                'rebooting': True,
                                'warning': 'Device disconnected — config likely saved, device may be rebooting'
                            }))
                        except Exception as e:
                            print(f"[CONFIG] Error: {e}")
                            await websocket.send(json.dumps({
                                'type': 'config_saved',
                                'success': False,
                                'error': str(e)
                            }))

                elif data.get('type') == 'set_fixed_position':
                    if mapper:
                        try:
                            iface = None
                            if mapper.connection_type == 'serial' and mapper._serial_iface:
                                iface = mapper._serial_iface.iface
                            elif mapper.connection_type == 'tcp' and mapper._tcp_iface:
                                iface = mapper._tcp_iface
                            if iface:
                                lat = data.get('lat', 0)
                                lon = data.get('lon', 0)
                                alt = data.get('alt', 0)
                                iface.localNode.setFixedPosition(lat, lon, alt)
                                await websocket.send(json.dumps({
                                    'type': 'fixed_position_result',
                                    'success': True
                                }))
                                print(f"[CONFIG] Fixed position set: {lat}, {lon}, {alt}m")
                        except Exception as e:
                            await websocket.send(json.dumps({
                                'type': 'fixed_position_result',
                                'success': False, 'error': str(e)
                            }))

                elif data.get('type') == 'clear_fixed_position':
                    if mapper:
                        try:
                            iface = None
                            if mapper.connection_type == 'serial' and mapper._serial_iface:
                                iface = mapper._serial_iface.iface
                            elif mapper.connection_type == 'tcp' and mapper._tcp_iface:
                                iface = mapper._tcp_iface
                            if iface:
                                iface.localNode.removeFixedPosition()
                                await websocket.send(json.dumps({
                                    'type': 'clear_position_result',
                                    'success': True
                                }))
                                print(f"[CONFIG] Fixed position cleared")
                        except Exception as e:
                            await websocket.send(json.dumps({
                                'type': 'clear_position_result',
                                'success': False, 'error': str(e)
                            }))

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
                        await websocket.send(json.dumps({'type': 'favorites_list', 'favorites': favorites}))
                    except Exception as e:
                        await websocket.send(json.dumps({'type': 'favorites_list', 'favorites': [], 'error': str(e)}))

                elif data.get('type') == 'set_favorite':
                    node_id = data.get('node_id', '').strip()
                    if not node_id:
                        await websocket.send(json.dumps({'type': 'favorite_result', 'success': False, 'error': 'No node ID provided'}))
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
                            await websocket.send(json.dumps({'type': 'favorite_result', 'success': True, 'action': 'set', 'node_id': node_id}))
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
                                await websocket.send(json.dumps({'type': 'favorites_list', 'favorites': favorites}))
                            except:
                                pass
                        except Exception as e:
                            await websocket.send(json.dumps({'type': 'favorite_result', 'success': False, 'error': str(e)}))

                elif data.get('type') == 'remove_favorite':
                    node_id = data.get('node_id', '').strip()
                    if not node_id:
                        await websocket.send(json.dumps({'type': 'favorite_result', 'success': False, 'error': 'No node ID provided'}))
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
                            await websocket.send(json.dumps({'type': 'favorite_result', 'success': True, 'action': 'remove', 'node_id': node_id}))
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
                                await websocket.send(json.dumps({'type': 'favorites_list', 'favorites': favorites}))
                            except:
                                pass
                        except Exception as e:
                            await websocket.send(json.dumps({'type': 'favorite_result', 'success': False, 'error': str(e)}))

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

                        node.writeChannel(ch_index)
                        print(f"[CONFIG] Channel {ch_index} saved: name={ch_name!r} role={ch_role} psk={'changed' if ch_psk else 'unchanged'}")
                        await websocket.send(json.dumps({
                            'type': 'channel_save_result',
                            'success': True,
                            'index': ch_index
                        }))
                    except Exception as e:
                        await websocket.send(json.dumps({
                            'type': 'channel_save_result',
                            'success': False,
                            'error': str(e)
                        }))

                elif data.get('type') == 'clear_node_stats':
                    node_id = data.get('node_id', '').strip()
                    if node_id and mapper:
                        try:
                            mapper.stats_db.clear_node_packets(node_id)
                            await websocket.send(json.dumps({
                                'type': 'clear_node_stats_result',
                                'node_id': node_id,
                                'success': True
                            }))
                            print(f"[STATS] Cleared packet history for {node_id}")
                        except Exception as e:
                            await websocket.send(json.dumps({
                                'type': 'clear_node_stats_result',
                                'node_id': node_id,
                                'success': False,
                                'error': str(e)
                            }))
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
                            }))
                        except Exception as e:
                            await websocket.send(json.dumps({
                                'type': 'elevation_data',
                                'error': str(e)
                            }))
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
                        }))

                else:
                    print(f"[WS] Unknown message type from {client_addr}: {data.get('type')}")
            except json.JSONDecodeError:
                print(f"[WS] Invalid JSON from {client_addr}")
    except websockets.exceptions.ConnectionClosed:
        print(f"[WS] Client disconnected: {client_addr}")
    finally:
        connected_clients.discard(websocket)
        print(f"[WS] Client removed: {client_addr}, total clients: {len(connected_clients)}")


async def start_websocket_server():
    """Start WebSocket server"""
    print("[WS] Starting WebSocket server on ws://0.0.0.0:8765")
    async with websockets.serve(websocket_handler, "0.0.0.0", 8765):
        await asyncio.Future()  # Run forever


def run_websocket_server_thread():
    """Run WebSocket server in separate thread"""
    asyncio.run(start_websocket_server())


if __name__ == '__main__':
    print("Starting Meshtastic Mapper (Listen Mode with WebSocket + TCP)...")

    # Create output directory
    os.makedirs('/var/www/html/meshtastic', exist_ok=True)

    possible_ports = [
        '/dev/ttyUSB0', '/dev/ttyUSB1', '/dev/ttyUSB2',
        '/dev/ttyACM0', '/dev/ttyACM1', '/dev/ttyACM2'
    ]

    def detect_serial_port():
        for p in possible_ports:
            if os.path.exists(p):
                return p
        return None

    # Load config
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
            print("Waiting for TCP connection configuration via web interface...")
            print(f"Open http://localhost/meshtastic/ and configure TCP connection")

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

        # Mapper loop with runtime restart support
        _watchdog_started = False
        while True:
            mapper = ListenBasedMapper(
                connection_type=connection_type,
                port=port,
                host=host,
                max_age=86400
            )
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
                            with open('/var/www/html/meshtastic/nodes.json', 'w') as f:
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
