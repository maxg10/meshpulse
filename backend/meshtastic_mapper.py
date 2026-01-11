#!/usr/bin/env python3
"""
Meshtastic Mapper - Listen Mode
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
    def __init__(self, port='/dev/ttyUSB0'):
        self.port = port
        self.json_path = '/var/www/html/meshtastic/nodes.json'
        self.nodes = {}
        self.meshtastic_cmd = '/home/maxg/.local/bin/meshtastic'
        
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
                    
                    self.nodes[node_id] = {
                        'id': node_id,
                        'name': name,
                        'lat': round(lat, 6),
                        'lon': round(lon, 6),
                        'alt': alt,
                        'snr': round(snr, 1),
                        'ts': int(time.time())
                    }
                    
                    print(f"? {node_id} {name[:20]} @ {lat:.4f},{lon:.4f}")
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
        print("Meshtastic Mapper - LISTEN MODE v1.1")
        print("Continuous monitoring with auto-restart")
        print("=" * 60)
        
        cmd = [self.meshtastic_cmd, '--port', self.port, '--listen']
        
        print(f"Command: {' '.join(cmd)}")
        print("Press Ctrl+C to stop\n")
        
        last_save = time.time()
        save_interval = 60
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
    print("Starting Meshtastic Mapper (Listen Mode)...")
    
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
        mapper = ListenBasedMapper(port)
        mapper.run()
    except KeyboardInterrupt:
        print("\nStopped by user")
    except Exception as e:
        print(f"\nFATAL: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
