# Meshtastic Network Mapper — Plugin API
# Copyright (C) 2025-2026 Mariusz "Max" Gieparda
# Licensed under GPL-3.0 — see LICENSE file

"""
MeshPlugin base class — the API that plugin developers use.

A plugin creates a subclass of MeshPlugin and overrides the hooks it needs.
The mapper instantiates the plugin and calls hooks when events occur.

Example:
    from mapper.plugin_api import MeshPlugin

    class MyPlugin(MeshPlugin):
        def on_enable(self):
            self.log("Plugin enabled!")

        async def on_message(self, message):
            if message['text'].startswith('!hello'):
                await self.send_mesh_message("Hello!", to_id=message['from_id'])
"""

import sqlite3
import os
import json


class MeshPlugin:
    """Base class for all Meshtastic Mapper plugins.

    Attributes set by mapper at load time:
        plugin_id (str): Unique ID e.g. "maxg10/bbs"
        plugin_dir (str): Absolute path to plugin directory
        config (dict): Merged defaults + user config overrides
        _mapper: Reference to ListenBasedMapper instance (internal)
        _manager: Reference to PluginManager instance (internal)
    """

    plugin_id = None
    plugin_dir = None
    config = {}
    _mapper = None
    _manager = None

    # ── Database ────────────────────────────────────────────────

    def get_database(self):
        """Get plugin's own isolated SQLite database.

        Returns:
            sqlite3.Connection to plugins/{author}/{name}/data.db
            Row factory is set to sqlite3.Row for dict-like access.

        The plugin has full control over its own database.
        Mapper's stats.db is NOT accessible — use hooks/getters for mapper data.
        """
        db_path = os.path.join(self.plugin_dir, 'data.db')
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ── Logging ─────────────────────────────────────────────────

    def log(self, message):
        """Log message with plugin prefix.

        Args:
            message (str): Message to log

        Output: [PLUGIN:maxg10/bbs] Your message here
        """
        print(f"[PLUGIN:{self.plugin_id}] {message}")

    # ── Mesh Communication ──────────────────────────────────────

    async def send_mesh_message(self, text, to_id=None, channel=0):
        """Send text message to mesh network.

        Args:
            text (str): Message text to send
            to_id (str, optional): Destination node ID for DM. None = broadcast.
            channel (int): Channel index (0 = primary)

        This is implemented by the mapper — calls the appropriate
        serial/TCP interface to actually transmit.
        """
        if self._manager:
            await self._manager.send_mesh_message(text, to_id, channel)

    def send_mqtt_to_device(self, topic, data):
        """Send MQTT downlink message to the mesh device.

        The device acts as MQTT client proxy — this sends a message that
        the device will publish to the MQTT broker on behalf of the mapper.
        Synchronous — safe to call from both async hooks and sync paho-mqtt
        callbacks running in a background thread.

        Args:
            topic (str): MQTT topic
            data (bytes): MQTT payload
        """
        if self._manager:
            self._manager.send_mqtt_to_device(topic, data)

    # ── WebSocket ───────────────────────────────────────────────

    async def broadcast_ws(self, data, channel=None):
        """Broadcast data to WebSocket clients.

        Args:
            data (dict): Data to JSON-serialize and send
            channel (str, optional): If specified, only clients subscribed to
                'plugin:{plugin_id}:{channel}' receive it.
                If None, broadcast to ALL connected clients.
        """
        if self._manager:
            await self._manager.broadcast_plugin_ws(self.plugin_id, data, channel)

    def register_ws_channel(self, channel_name):
        """Register a WebSocket channel for this plugin.

        Frontend subscribes via:
            MapperAPI.ws.subscribe(channel_name, callback)

        The actual channel name becomes: plugin:{plugin_id}:{channel_name}

        Args:
            channel_name (str): Channel name (without prefix)
        """
        if self._manager:
            self._manager.register_ws_channel(self.plugin_id, channel_name)

    # ── Data Access (read-only) ─────────────────────────────────

    def get_config(self):
        """Get plugin config dict (manifest defaults merged with user overrides)."""
        return self.config

    def get_nodes(self):
        """Get all nodes with GPS positions.

        Returns:
            dict: Copy of mapper's nodes dict. Keys are node IDs like '!7b6c8272'.
        """
        if self._mapper:
            return dict(self._mapper.nodes)
        return {}

    def get_nodes_no_position(self):
        """Get all nodes without GPS positions.

        Returns:
            dict: Copy of mapper's no-position nodes dict.
        """
        if self._mapper:
            return dict(self._mapper.nodes_no_position)
        return {}

    def get_node(self, node_id):
        """Get a specific node's data.

        Args:
            node_id (str): Node ID e.g. '!7b6c8272'

        Returns:
            dict or None: Node data if found, None otherwise.
        """
        if self._mapper:
            return (self._mapper.nodes.get(node_id) or
                    self._mapper.nodes_no_position.get(node_id))
        return None

    def get_tracker_info(self):
        """Get local tracker node info.

        Returns:
            dict: {node_id, connection_type} of the local tracker
        """
        if self._mapper:
            return {
                'node_id': self._mapper.local_node_id,
                'connection_type': self._mapper.connection_type,
            }
        return {}

    def get_messages(self, channel=0):
        """Get messages for a channel.

        Args:
            channel (int): Channel index (0 = primary)

        Returns:
            list: Copy of messages list for that channel
        """
        if self._mapper:
            return list(self._mapper.messages.get(channel, []))
        return []

    def get_tracker_config(self, section):
        """Get a device config section from the connected radio.

        Reads from localConfig or moduleConfig of the radio.
        Common localConfig sections: 'device', 'position', 'power',
            'network', 'display', 'lora', 'bluetooth'
        Common moduleConfig sections: 'mqtt', 'serial', 'telemetry',
            'neighborInfo', 'rangeTest', 'store_and_forward'

        Args:
            section (str): Config section name

        Returns:
            dict: Config values for that section, or {} if unavailable
        """
        if self._manager:
            return self._manager.get_tracker_config(section)
        return {}

    # ── API Routes ──────────────────────────────────────────────

    def register_api_route(self, method, path, handler):
        """Register HTTP API endpoint for this plugin.

        The path is automatically prefixed:
            register_api_route('GET', '/messages', handler)
            → mounted at /api/plugins/{plugin_id}/messages

        Args:
            method (str): HTTP method ('GET', 'POST', 'PUT', 'DELETE')
            path (str): Route path (will be prefixed)
            handler (callable): Async function(request_data) -> dict
        """
        if self._manager:
            self._manager.register_api_route(self.plugin_id, method, path, handler)

    # ── Plugin Config ───────────────────────────────────────────

    def save_config(self):
        """Save current config to disk.

        Writes self.config to plugins/{author}/{name}/config.json
        """
        config_path = os.path.join(self.plugin_dir, 'config.json')
        try:
            with open(config_path, 'w') as f:
                json.dump(self.config, f, indent=2)
        except Exception as e:
            self.log(f"Config save error: {e}")

    # ── Lifecycle Hooks (override in subclass) ──────────────────

    def on_enable(self):
        """Called when plugin is enabled. Setup resources here.

        This is where the plugin should:
        - Initialize database tables
        - Register WebSocket channels
        - Register API routes
        - Start any background tasks
        """
        pass

    def on_config_update(self, new_config):
        """Called when plugin config is changed via the UI.

        Override this to react to config changes without disable/enable.
        The default implementation updates self.config.

        Args:
            new_config (dict): Full merged config (defaults + user overrides)
        """
        self.config = new_config

    def on_disable(self):
        """Called when plugin is disabled. Cleanup here.

        This is where the plugin should:
        - Close database connections
        - Clean up temp files
        - Cancel background tasks
        """
        pass

    # ── Mesh Hooks (override in subclass) ───────────────────────

    async def on_message(self, message):
        """New text message received from mesh.

        Args:
            message (dict): {
                'from_id': str,       # e.g. '!7b6c8272'
                'from_name': str,     # e.g. 'MaxNode'
                'to_id': str,         # e.g. '!abcd1234' or '^all'
                'text': str,
                'timestamp': int,     # Unix timestamp
                'is_dm': bool,
                'channel_index': int
            }
        """
        pass

    async def on_node_update(self, node):
        """Node info updated (new node or changed nodeinfo).

        Args:
            node (dict): Full node data dict with id, name, lat, lon, etc.
        """
        pass

    async def on_position(self, position):
        """GPS position update received.

        Args:
            position (dict): {
                'node_id': str,
                'lat': float, 'lon': float, 'alt': int,
                'speed': int, 'heading': int,
                'snr': float, 'rssi': int,
                'hops': int, 'timestamp': int
            }
        """
        pass

    async def on_telemetry(self, telemetry):
        """Telemetry data received.

        Args:
            telemetry (dict): {
                'node_id': str,
                'battery': int, 'voltage': float,
                'channel_util': float, 'air_util_tx': float,
                'snr': float, 'temperature': float,
                'uptime': int, ...
            }
        """
        pass

    async def on_neighborinfo(self, neighborinfo):
        """NeighborInfo update received.

        Args:
            neighborinfo (dict): {
                'node_id': str,
                'neighbors': [{'id': str, 'snr': float}, ...]
            }
        """
        pass

    async def on_mqtt_proxy(self, topic, data):
        """MQTT client proxy message received from mesh device.

        The mesh device is acting as an MQTT client proxy — this hook fires
        when the device wants to publish a message to an MQTT broker.
        The plugin can inspect or forward the message to a real broker.

        Args:
            topic (str): MQTT topic string
            data (bytes): MQTT payload (raw bytes)
        """
        pass

    # ── System Hooks (override in subclass) ─────────────────────

    async def on_connect(self, connection_info):
        """Mapper connected to mesh node.

        Args:
            connection_info (dict): {
                'connection_type': str,   # 'serial' or 'tcp'
                'host_or_port': str,
                'local_node_id': str
            }
        """
        pass

    async def on_disconnect(self, reason):
        """Mapper lost connection to mesh node.

        Args:
            reason (dict): {'reason': str}
        """
        pass

    async def on_node_expire(self, node_info):
        """Node exceeded TTL and was removed.

        Args:
            node_info (dict): {'node_id': str, 'last_seen': int}
        """
        pass

    async def on_ws_client_connect(self, client_info):
        """New WebSocket client connected.

        Args:
            client_info (dict): {'client_id': str}
        """
        pass

    async def on_ws_client_disconnect(self, client_info):
        """WebSocket client disconnected.

        Args:
            client_info (dict): {'client_id': str}
        """
        pass
