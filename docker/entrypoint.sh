#!/bin/bash
set -e

echo "🚀 MeshPulse v2.5.4 starting..."

# Legacy path kept for backward compat with existing Docker volumes
DATA_DIR="/var/www/html/meshtastic"
# Ensure data dir exists (fresh container without a pre-existing volume)
mkdir -p "$DATA_DIR"

# Initialize nodes.json if not present
if [ ! -f "$DATA_DIR/nodes.json" ]; then
    echo '{"ts":0,"updated":"","cnt":0,"cnt_no_pos":0,"max_distance_km":null,"farthest_node":null,"tracker":{},"nodes":[],"nodes_no_pos":[],"messages":{}}' > "$DATA_DIR/nodes.json"
    echo "📄 Created empty nodes.json"
fi

# Write config.json based on env vars
if [ -n "$TRACKER_HOST" ]; then
    echo "{\"connection_type\":\"tcp\",\"host\":\"$TRACKER_HOST\",\"port\":null}" > "$DATA_DIR/config.json"
    echo "📡 Tracker host: $TRACKER_HOST"
else
    if [ ! -f "$DATA_DIR/config.json" ]; then
        echo "{\"connection_type\":\"tcp\",\"host\":null,\"port\":null}" > "$DATA_DIR/config.json"
        echo "⚠️  No TRACKER_HOST set — configure via web UI after startup"
    fi
fi

# Always sync fresh frontend files from image (fixes stale files after volume update)
echo "📦 Syncing frontend files from image..."
cp -f /app/frontend_dist/*.html "$DATA_DIR/"
cp -f /app/frontend_dist/*.css "$DATA_DIR/"
cp -f /app/frontend_dist/*.ico "$DATA_DIR/" 2>/dev/null || true
echo "✅ Frontend files updated"

# Ensure plugins directory exists (persistent via volume)
mkdir -p "$DATA_DIR/plugins" 2>/dev/null || true
mkdir -p /app/plugins 2>/dev/null || true

# Start lighttpd in background
echo "🌐 Starting web server on port 80..."
lighttpd -f /etc/lighttpd/lighttpd.conf

# Start mapper
echo "📡 Starting mapper backend..."
exec python3 -u /app/backend/meshpulse.py
