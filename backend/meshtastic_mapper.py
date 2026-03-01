#!/usr/bin/env python3
#ver 1.6 - WebSocket support
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

# Global set of connected WebSocket clients
connected_clients = set()

class ListenBasedMapper:
    def __init__(self, port='/dev/ttyUSB0', max_age=604800):
        self.port = port
        self.json_path = '/var/www/html/meshtastic/nodes.json'
        self.meshtastic_cmd = os.path.expanduser('~/.local/bin/meshtastic')
        self.max_age = max_age  # 24 hours default

        # Load existing nodes or start fresh
        self.nodes_no_position = {}  # Nodes without GPS position
        self.nodes = self.load_existing_nodes()
        self.local_node_id = self.get_local_node_id()

        # Store text messages (max 50, newest first)
        self.messages = []

        # Save immediately to show tracker info in UI
        self.save_nodes()
    
    def get_local_node_id(self):
        """Get local node info using meshtastic --info"""
        try:
            print("[INFO] Getting local node info...")
            result = subprocess.run(
                [self.meshtastic_cmd, '--port', self.port, '--info'],
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
            
            # Store tracker info
            self.tracker_info = {
                'node_id': node_id,
                'port': self.port,
                'hw_model': hw_model,
                'firmware': firmware
            }
            
            print(f"[INFO] Local node ID: {node_id}")
            print(f"[INFO] Hardware: {hw_model}, Firmware: {firmware}")
            
            return node_id
            
        except subprocess.TimeoutExpired:
            print("[WARN] Timeout getting local node info")
        except Exception as e:
            print(f"[WARN] Error getting local node info: {e}")
        
        self.tracker_info = {
            'node_id': None,
            'port': self.port,
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
                    
                        # Clean old nodes immediately
                        self.clean_old_nodes_from_dict(nodes)
                        self.clean_old_nodes_from_dict(nodes_no_pos)
                    
                        # Store no-GPS nodes
                        self.nodes_no_position = nodes_no_pos
                    
                        self.messages = data.get('messages', [])
                        print(f"[LOAD] Loaded {len(nodes)} nodes + {len(nodes_no_pos)} no-GPS after cleanup, {len(self.messages)} messages")
                        return nodes
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
    
    def clean_old_nodes(self):
        """Clean old nodes from self.nodes and self.nodes_no_position"""
        self.clean_old_nodes_from_dict(self.nodes)
        self.clean_old_nodes_from_dict(self.nodes_no_position)
        
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
                        'via_mqtt': via_mqtt
                    }
                    
                    marker = "✚" if is_new else "↻"
                    print(f"{marker} {node_id} {name[:20]} @ {lat:.4f},{lon:.4f}")
                    
                    # Broadcast to WebSocket clients
                    asyncio.run(self.broadcast_node_update(self.nodes[node_id]))
                    
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
                        'seen_at': int(time.time())
                    }
                    
                    marker = "✚" if is_new else "↻"
                    print(f"{marker} {node_id} {name[:20]} (no GPS)")
                    
                    # Broadcast to WebSocket clients
                    asyncio.run(self.broadcast_node_update(self.nodes_no_position[node_id]))
                    
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
            
            # Update existing node or create minimal entry
            if node_id in self.nodes:
                # Update existing node
                self.nodes[node_id]['lat'] = round(lat, 6)
                self.nodes[node_id]['lon'] = round(lon, 6)
                self.nodes[node_id]['snr'] = round(snr, 1)
                self.nodes[node_id]['hops'] = hops
                self.nodes[node_id]['via_mqtt'] = via_mqtt
                self.nodes[node_id]['ts'] = int(time.time())
                self.nodes[node_id]['seen_at'] = int(time.time())
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
                    'role': 'CLIENT',
                    'hops': hops,
                    'via_mqtt': via_mqtt,
                    'ts': int(time.time()),
                    'seen_at': int(time.time())
                }
                print(f"✚ {node_id} NEW from position @ {lat:.4f},{lon:.4f} hops={hops}{' MQTT' if via_mqtt else ''}")
            
            # Broadcast to WebSocket clients
            asyncio.run(self.broadcast_node_update(self.nodes[node_id]))
            
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

            # Extract device uptime for local/tracker node
            if node_id == self.local_node_id:
                uptime_match = re.search(r"'uptimeSeconds':\s*(\d+)", line)
                if uptime_match:
                    self.tracker_info['uptime_seconds'] = int(uptime_match.group(1))

            # Only update timestamp if node already exists
            if node_id in self.nodes:
                self.nodes[node_id]['ts'] = int(time.time())
                self.nodes[node_id]['seen_at'] = int(time.time())
                print(f"♡ {node_id} telemetry heartbeat")
                
                # Broadcast to WebSocket clients
                asyncio.run(self.broadcast_node_update(self.nodes[node_id]))
                
                return True
            elif node_id in self.nodes_no_position:
                self.nodes_no_position[node_id]['ts'] = int(time.time())
                self.nodes_no_position[node_id]['seen_at'] = int(time.time())
                print(f"♡ {node_id} telemetry heartbeat (no GPS)")
                
                # Broadcast to WebSocket clients
                asyncio.run(self.broadcast_node_update(self.nodes_no_position[node_id]))
                
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

            # Get sender name from nodes
            sender_name = from_id
            if from_id in self.nodes:
                sender_name = self.nodes[from_id].get('name', from_id)
            elif from_id in self.nodes_no_position:
                sender_name = self.nodes_no_position[from_id].get('name', from_id)

            # Create message object
            message = {
                'from_id': from_id,
                'from_name': sender_name,
                'to_id': to_id,
                'text': text,
                'timestamp': int(time.time()),
                'is_dm': to_id != '^all'
            }

            # Add to messages list (newest first, max 50)
            self.messages.insert(0, message)
            if len(self.messages) > 50:
                self.messages = self.messages[:50]

            # Log
            dm_marker = " [DM]" if message['is_dm'] else ""
            print(f"💬 {sender_name}: {text}{dm_marker}")

            # Broadcast to WebSocket clients
            asyncio.run(self.broadcast_message(message))

            return True

        except Exception as e:
            print(f"Text message parse error: {e}")

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
        websockets.broadcast(connected_clients, message)
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
        websockets.broadcast(connected_clients, message)
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
        websockets.broadcast(connected_clients, message)
        print(f"[WS] Broadcasted message from {message_data['from_id']} to {len(connected_clients)} clients")

    def save_nodes(self):
        """Save to JSON"""
        try:
            max_dist, farthest_id = self.get_max_distance()
            
            data = {
                'ts': int(time.time()),
                'updated': datetime.now().isoformat(),
                'cnt': len(self.nodes),
                'cnt_no_pos': len(self.nodes_no_position),
                'max_distance_km': max_dist,
                'farthest_node': farthest_id,
                'tracker': getattr(self, 'tracker_info', {}),
                'nodes': list(self.nodes.values()),
                'nodes_no_pos': list(self.nodes_no_position.values()),
                'messages': self.messages
            }
            
            temp_path = self.json_path + '.tmp'
            with open(temp_path, 'w') as f:
                json.dump(data, f, indent=2)
            
            os.replace(temp_path, self.json_path)
            
            dist_info = f", max range: {max_dist} km to {farthest_id}" if max_dist else ""
            print(f"[SAVE] {len(self.nodes)} nodes + {len(self.nodes_no_position)} no-GPS → {self.json_path}{dist_info}")
            
        except Exception as e:
            print(f"Save error: {e}") 

    def run(self):
        """Run meshtastic --listen and parse output"""
        print("=" * 60)
        print("Meshtastic Mapper - LISTEN MODE v1.6 (with WebSocket)")
        print("Continuous monitoring with auto-restart")
        print("=" * 60)
        print(f"Node TTL: {self.max_age//3600} hours")
        print(f"Current nodes in memory: {len(self.nodes)}")
        print(f"WebSocket server: ws://0.0.0.0:8765")
        print("=" * 60)
        
        cmd = [self.meshtastic_cmd, '--port', self.port, '--listen']
        
        print(f"Command: {' '.join(cmd)}")
        print("Press Ctrl+C to stop\n")
        
        last_save = time.time()
        last_clean = time.time()
        save_interval = 60  # Save every minute
        clean_interval = 3600  # Clean every hour
        restart_count = 0
        
        while True:
            try:
                print(f"[START] Starting listener (restart #{restart_count})...")
                
                # Start process
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1
                )
                
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
                    
                    # Save periodically
                    if time.time() - last_save > save_interval:
                        self.save_nodes()
                        last_save = time.time()
                    
                    # Clean old nodes periodically
                    if time.time() - last_clean > clean_interval:
                        self.clean_old_nodes()
                        self.save_nodes()
                        last_clean = time.time()
                    
                # Process ended
                return_code = process.wait()
                print(f"[WARN] Process ended with code {return_code}")
                
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


# WebSocket server handler
async def websocket_handler(websocket):
    """Handle WebSocket connections"""
    # Register client
    connected_clients.add(websocket)
    client_addr = websocket.remote_address
    print(f"[WS] Client connected: {client_addr}, total clients: {len(connected_clients)}")
    
    try:
        # Keep connection alive
        async for message in websocket:
            # Echo back or handle commands if needed
            print(f"[WS] Received from {client_addr}: {message}")
    except websockets.exceptions.ConnectionClosed:
        print(f"[WS] Client disconnected: {client_addr}")
    finally:
        # Unregister client
        connected_clients.remove(websocket)
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
    print("Starting Meshtastic Mapper (Listen Mode with WebSocket)...")
    
    # Create output directory
    os.makedirs('/var/www/html/meshtastic', exist_ok=True)

    # Auto-detect serial port
    port = None
    possible_ports = [
        '/dev/ttyUSB0', '/dev/ttyUSB1', '/dev/ttyUSB2',
        '/dev/ttyACM0', '/dev/ttyACM1', '/dev/ttyACM2'
    ]
    for p in possible_ports:
        if os.path.exists(p):
            port = p
            break

    if not port:
        print("ERROR: No serial port found")
        print("Checked:", possible_ports)
        sys.exit(1)

    print(f"Using port: {port}\n")
    
    try:
        # Start WebSocket server in background thread
        ws_thread = threading.Thread(target=run_websocket_server_thread, daemon=True)
        ws_thread.start()
        print("[WS] WebSocket server thread started")
        
        # Give WebSocket server time to start
        time.sleep(2)
        
        # Create mapper with 24h TTL (86400 seconds)
        mapper = ListenBasedMapper(port, max_age=604800)
        mapper.run()
    except KeyboardInterrupt:
        print("\nStopped by user")
    except Exception as e:
        print(f"\nFATAL: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
