#!/usr/bin/env python3
"""
Meshtastic Mapper - Listen Mode with TTL
Works on slow Raspberry Pi Model B+
"""
import subprocess
import json
import time
import re
from datetime import datetime
import sys
import os

class ListenBasedMapper:
    def __init__(self, port='/dev/ttyUSB0', max_age=86400):
        self.port = port
        self.json_path = '/var/www/html/meshtastic/nodes.json'
        self.meshtastic_cmd = os.path.expanduser('~/.local/bin/meshtastic')
        self.max_age = max_age  # 24 hours default
        
        # Load existing nodes or start fresh
        self.nodes = self.load_existing_nodes()
        
    def load_existing_nodes(self):
        """Load nodes from existing JSON file"""
        try:
            if os.path.exists(self.json_path):
                with open(self.json_path, 'r') as f:
                    data = json.load(f)
                    existing_count = data.get('cnt', 0)
                    if existing_count > 0:
                        print(f"[LOAD] Found {existing_count} existing nodes from previous run")
                        # Convert list to dict with id as key
                        nodes = {node['id']: node for node in data.get('nodes', [])}
                        # Clean old nodes immediately
                        self.clean_old_nodes_from_dict(nodes)
                        print(f"[LOAD] Loaded {len(nodes)} nodes after cleanup")
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
        
        if removed:
            hours = self.max_age // 3600
            print(f"[CLEAN] Removed {len(removed)} old nodes (>{hours}h old)")
            for node_id in removed[:5]:  # Show first 5
                print(f"  - {node_id}")
    
    def clean_old_nodes(self):
        """Clean old nodes from self.nodes"""
        self.clean_old_nodes_from_dict(self.nodes)
        
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
                
                if node_id and 'latitudeI' in pos and 'longitudeI' in pos:
                    lat = pos['latitudeI'] / 1e7
                    lon = pos['longitudeI'] / 1e7
                    alt = pos.get('altitude', 0)
                    snr = node_data.get('snr', 0)
                    role = node_data.get('user', {}).get('role', 'CLIENT')
                    
                    # Check if node exists and show update message
                    is_new = node_id not in self.nodes
                    
                    self.nodes[node_id] = {
                        'id': node_id,
                        'name': name,
                        'lat': round(lat, 6),
                        'lon': round(lon, 6),
                        'alt': alt,
                        'snr': round(snr, 1),
                        'role': role,
                        'ts': int(time.time())  # Update timestamp
                    }
                    
                    marker = "?" if is_new else "?"
                    print(f"{marker} {node_id} {name[:20]} @ {lat:.4f},{lon:.4f}")
                    return True
                    
            except Exception as e:
                print(f"Parse error: {e}")
        
        return False
    
    def save_nodes(self):
        """Save to JSON"""
        try:
            data = {
                'ts': int(time.time()),
                'updated': datetime.now().isoformat(),
                'cnt': len(self.nodes),
                'nodes': list(self.nodes.values())
            }
            
            temp_path = self.json_path + '.tmp'
            with open(temp_path, 'w') as f:
                json.dump(data, f, indent=2)
            
            os.replace(temp_path, self.json_path)
            
            print(f"[SAVE] {len(self.nodes)} nodes ? {self.json_path}")
            
        except Exception as e:
            print(f"Save error: {e}")
    
    def run(self):
        """Run meshtastic --listen and parse output"""
        print("=" * 60)
        print("Meshtastic Mapper - LISTEN MODE v1.2 (with TTL)")
        print("Continuous monitoring with auto-restart")
        print("=" * 60)
        print(f"Node TTL: {self.max_age//3600} hours")
        print(f"Current nodes in memory: {len(self.nodes)}")
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
                    
                    # Parse node info
                    if self.parse_node_info(line):
                        # Save periodically
                        if time.time() - last_save > save_interval:
                            self.save_nodes()
                            last_save = time.time()
                        
                        # Clean old nodes periodically
                        if time.time() - last_clean > clean_interval:
                            self.clean_old_nodes()
                            self.save_nodes()  # Save after cleanup
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

if __name__ == '__main__':
    print("Starting Meshtastic Mapper (Listen Mode with TTL)...")
    
    # Create output directory
    os.makedirs('/var/www/html/meshtastic', exist_ok=True)
    
    # Detect port
    if os.path.exists('/dev/ttyUSB0'):
        port = '/dev/ttyUSB0'
    elif os.path.exists('/dev/ttyACM0'):
        port = '/dev/ttyACM0'
    else:
        print("ERROR: No serial port found")
        sys.exit(1)
    
    print(f"Using port: {port}\n")
    
    try:
        # Create mapper with 24h TTL (86400 seconds)
        mapper = ListenBasedMapper(port, max_age=86400)
        mapper.run()
    except KeyboardInterrupt:
        print("\nStopped by user")
    except Exception as e:
        print(f"\nFATAL: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
