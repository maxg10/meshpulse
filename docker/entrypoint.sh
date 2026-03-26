#!/bin/bash
set -e

echo "🚀 Meshtastic Network Mapper starting..."

# Initialize nodes.json if not present in volume
if [ ! -f /app/data/nodes.json ]; then
    echo '{"ts":0,"updated":"","cnt":0,"cnt_no_pos":0,"max_distance_km":null,"farthest_node":null,"tracker":{},"nodes":[],"nodes_no_pos":[],"messages":{}}' > /app/data/nodes.json
    echo "📄 Created empty nodes.json"
fi

# Write config.json based on env vars
if [ -n "$TRACKER_HOST" ]; then
    echo "{\"connection_type\":\"tcp\",\"host\":\"$TRACKER_HOST\",\"port\":null}" > /app/config.json
    echo "📡 Tracker host: $TRACKER_HOST"
else
    echo "{\"connection_type\":\"tcp\",\"host\":null,\"port\":null}" > /app/config.json
    echo "⚠️  No TRACKER_HOST set — configure via web UI after startup"
fi

# Start lighttpd in background
echo "🌐 Starting web server..."
lighttpd -f /etc/lighttpd/lighttpd.conf

# Start mapper
echo "📡 Starting mapper backend..."
exec python3 -u /app/backend/meshtastic_mapper.py
