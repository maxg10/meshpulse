"""MQTT Proxy Plugin — proxies MQTT traffic for trackers without WiFi."""

import threading
import time
import paho.mqtt.client as mqtt
from mapper.plugin_api import MeshPlugin


class MqttProxyPlugin(MeshPlugin):
    """MQTT client proxy using tracker's firmware MQTT config."""

    def __init__(self):
        super().__init__()
        self.mqtt_client = None
        self.mqtt_connected = False
        self.mqtt_thread = None
        self._stop_event = threading.Event()

        # Stats
        self.uplink_count = 0
        self.downlink_count = 0
        self.last_uplink = None
        self.last_downlink = None

        # Config from tracker
        self.broker_address = None
        self.broker_port = 1883
        self.username = None
        self.password = None
        self.root_topic = "msh"
        self.tls_enabled = False
        self.node_id = None  # e.g., "!7b6c8272"

    def on_enable(self):
        """Read tracker MQTT config and connect to broker."""
        print("[MQTT-PROXY] Enabling MQTT Proxy plugin...")

        mqtt_config = self.get_tracker_config('mqtt')
        if not mqtt_config:
            print("[MQTT-PROXY] ERROR: Could not read MQTT config from tracker")
            print("[MQTT-PROXY] Make sure tracker is connected and MQTT is enabled in firmware")
            return

        if not mqtt_config.get('enabled', False):
            print("[MQTT-PROXY] WARNING: MQTT is not enabled on the tracker")
            print("[MQTT-PROXY] Enable MQTT in Config → MQTT tab first")
            return

        if not mqtt_config.get('proxy_to_client_enabled', False):
            print("[MQTT-PROXY] WARNING: 'Proxy to Client' is not enabled on the tracker")
            print("[MQTT-PROXY] Enable 'Proxy to Client' in Config → MQTT tab")
            return

        self.broker_address = mqtt_config.get('address', '') or 'mqtt.meshtastic.org'
        self.username = mqtt_config.get('username', '') or None
        self.password = mqtt_config.get('password', '') or None
        self.root_topic = mqtt_config.get('root', '') or 'msh'
        self.tls_enabled = mqtt_config.get('tls_enabled', False)

        # Support "host:port" in address field
        if ':' in self.broker_address:
            parts = self.broker_address.rsplit(':', 1)
            self.broker_address = parts[0]
            try:
                self.broker_port = int(parts[1])
            except ValueError:
                self.broker_port = 8883 if self.tls_enabled else 1883
        else:
            self.broker_port = 8883 if self.tls_enabled else 1883

        print(f"[MQTT-PROXY] Broker: {self.broker_address}:{self.broker_port}")
        print(f"[MQTT-PROXY] Root topic: {self.root_topic}")
        print(f"[MQTT-PROXY] TLS: {self.tls_enabled}")
        print(f"[MQTT-PROXY] Username: {'(set)' if self.username else '(anonymous)'}")

        config = self.config
        if config.get('auto_connect', True):
            self._start_mqtt()

    def on_disable(self):
        """Disconnect from MQTT broker."""
        print("[MQTT-PROXY] Disabling MQTT Proxy plugin...")
        self._stop_mqtt()
        print(f"[MQTT-PROXY] Session stats: ↑ {self.uplink_count} uplink, ↓ {self.downlink_count} downlink")

    def _start_mqtt(self):
        """Start MQTT client in background thread."""
        self._stop_event.clear()
        self.mqtt_thread = threading.Thread(target=self._mqtt_loop, daemon=True)
        self.mqtt_thread.start()

    def _stop_mqtt(self):
        """Stop MQTT client."""
        self._stop_event.set()
        if self.mqtt_client:
            try:
                self.mqtt_client.disconnect()
            except Exception:
                pass
            self.mqtt_client = None
        self.mqtt_connected = False

    def _mqtt_loop(self):
        """MQTT client loop with auto-reconnect."""
        config = self.config
        reconnect_interval = config.get('reconnect_interval', 30)

        while not self._stop_event.is_set():
            try:
                self._connect_mqtt()
                self.mqtt_client.loop_forever()
            except Exception as e:
                print(f"[MQTT-PROXY] MQTT error: {e}")
                self.mqtt_connected = False

            if not self._stop_event.is_set():
                print(f"[MQTT-PROXY] Reconnecting in {reconnect_interval}s...")
                self._stop_event.wait(reconnect_interval)

    def _connect_mqtt(self):
        """Create and connect MQTT client."""
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id="meshtastic-mapper-proxy",
            clean_session=True
        )

        client.on_connect = self._on_mqtt_connect
        client.on_disconnect = self._on_mqtt_disconnect
        client.on_message = self._on_mqtt_message

        if self.username:
            client.username_pw_set(self.username, self.password)

        if self.tls_enabled:
            client.tls_set()

        print(f"[MQTT-PROXY] Connecting to {self.broker_address}:{self.broker_port}...")
        client.connect(self.broker_address, self.broker_port, keepalive=60)
        self.mqtt_client = client

    def _on_mqtt_connect(self, client, userdata, flags, reason_code, properties=None):
        """Called when connected to MQTT broker."""
        if reason_code == 0:
            self.mqtt_connected = True
            print(f"[MQTT-PROXY] ✓ Connected to {self.broker_address}")
            subscribe_topic = f"{self.root_topic}/2/e/#"
            client.subscribe(subscribe_topic, qos=0)
            print(f"[MQTT-PROXY] Subscribed to: {subscribe_topic}")
        else:
            self.mqtt_connected = False
            print(f"[MQTT-PROXY] Connection failed, reason: {reason_code}")

    def _on_mqtt_disconnect(self, client, userdata, flags, reason_code, properties=None):
        """Called when disconnected from MQTT broker."""
        self.mqtt_connected = False
        if reason_code != 0:
            print(f"[MQTT-PROXY] Disconnected unexpectedly (reason: {reason_code})")
        else:
            print("[MQTT-PROXY] Disconnected")

    def _on_mqtt_message(self, client, userdata, msg):
        """Downlink: forward broker message to tracker.

        Handles both actual downlink and implicit ACK (broker echoes our uplink
        back to us — firmware needs this echo to confirm delivery).
        Retained messages are skipped to avoid NO_RESPONSE storms on reconnect.
        """
        try:
            config = self.config

            if msg.retain:
                if config.get('log_traffic', False):
                    print(f"[MQTT-PROXY] Skipping retained message: {msg.topic}")
                return

            if config.get('log_traffic', False):
                print(f"[MQTT-PROXY] ↓ MQTT recv: {msg.topic} ({len(msg.payload)} bytes)")

            self.send_mqtt_to_device(msg.topic, msg.payload)

            self.downlink_count += 1
            self.last_downlink = time.time()

        except Exception as e:
            print(f"[MQTT-PROXY] Downlink error: {e}")

    async def on_mqtt_proxy(self, topic: str, data: bytes):
        """Uplink: publish tracker's outgoing MQTT message to the broker."""
        if not self.mqtt_connected or not self.mqtt_client:
            return

        try:
            config = self.config

            # Extract our node ID from topic on first uplink
            # Topic format: msh/PL/2/e/MediumFast/!7b6c8272
            if not self.node_id and topic:
                parts = topic.split('/')
                if parts and parts[-1].startswith('!'):
                    self.node_id = parts[-1]
                    print(f"[MQTT-PROXY] Detected node ID: {self.node_id}")

            self.mqtt_client.publish(topic, data, qos=0, retain=False)

            self.uplink_count += 1
            self.last_uplink = time.time()

            if config.get('log_traffic', False):
                print(f"[MQTT-PROXY] ↑ Published: {topic} ({len(data)} bytes)")

        except Exception as e:
            print(f"[MQTT-PROXY] Uplink error: {e}")
