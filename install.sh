#!/bin/bash
#
# MeshPulse - Installer
# https://github.com/maxg10/meshpulse
#

# Get version from backend (MAPPER_VERSION is the single source of truth)
VERSION=$(grep -m1 "^MAPPER_VERSION" backend/meshpulse.py | grep -o "'[^']*'" | tr -d "'")

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "========================================"
echo " MeshPulse - Installer"
echo " Version: $VERSION"
echo "========================================"
echo ""

# Check if running from correct directory
if [ ! -f "backend/meshpulse.py" ]; then
    echo -e "${RED}❌ Error: Run this script from the repository root directory${NC}"
    echo "   cd ~/meshpulse && ./install.sh"
    exit 1
fi

REPO_PATH=$(pwd)
CURRENT_USER=$(whoami)
PLUGIN_DIR="$REPO_PATH/plugins"

echo "📍 Repository path: $REPO_PATH"
echo "👤 Installing for user: $CURRENT_USER"
echo ""

# ============================================
# PHASE 1: Check dependencies
# ============================================
echo "🔍 Checking dependencies..."
echo ""

MISSING=0

# Check Python3
if command -v python3 &> /dev/null; then
    PYTHON_VER=$(python3 --version 2>&1 | awk '{print $2}')
    echo -e "${GREEN}✅ python3 - OK ($PYTHON_VER)${NC}"
else
    echo -e "${RED}❌ python3 - NOT FOUND${NC}"
    echo "   Install with: sudo apt install python3"
    MISSING=1
fi

# Check pip3
if command -v pip3 &> /dev/null; then
    PIP_VER=$(pip3 --version 2>&1 | awk '{print $2}')
    echo -e "${GREEN}✅ pip3 - OK ($PIP_VER)${NC}"
else
    echo -e "${RED}❌ pip3 - NOT FOUND${NC}"
    echo "   Install with: sudo apt install python3-pip"
    MISSING=1
fi

# Check meshtastic CLI
if command -v meshtastic &> /dev/null || [ -f ~/.local/bin/meshtastic ]; then
    MESH_VER=$(meshtastic --version 2>/dev/null || ~/.local/bin/meshtastic --version 2>/dev/null)
    echo -e "${GREEN}✅ meshtastic - OK ($MESH_VER)${NC}"
else
    echo -e "${RED}❌ meshtastic - NOT FOUND${NC}"
    echo "   Install with: pip3 install meshtastic --break-system-packages"
    MISSING=1
fi

# Check websockets library
if python3 -c "import websockets" 2>/dev/null; then
    WS_VER=$(python3 -c "import websockets; print(websockets.__version__)" 2>/dev/null || echo "unknown")
    echo -e "${GREEN}✅ websockets - OK ($WS_VER)${NC}"
else
    echo -e "${RED}❌ websockets - NOT FOUND${NC}"
    echo "   Install with: pip3 install websockets --break-system-packages"
    MISSING=1
fi

# Check web server (lighttpd or apache2)
if command -v lighttpd &> /dev/null; then
    LIGHTTPD_VER=$(lighttpd -v 2>&1 | head -1 | awk '{print $1}')
    echo -e "${GREEN}✅ lighttpd - OK ($LIGHTTPD_VER)${NC}"
    WEBSERVER="lighttpd"
elif command -v apache2 &> /dev/null; then
    APACHE_VER=$(apache2 -v 2>&1 | head -1 | awk '{print $3}')
    echo -e "${GREEN}✅ apache2 - OK ($APACHE_VER)${NC}"
    WEBSERVER="apache2"
else
    echo -e "${RED}❌ web server - NOT FOUND${NC}"
    echo "   Install with: sudo apt install lighttpd"
    MISSING=1
fi

# Check if user is in dialout group (for USB access)
if groups $CURRENT_USER | grep -q dialout; then
    echo -e "${GREEN}✅ dialout group - OK${NC}"
else
    echo -e "${YELLOW}⚠️  dialout group - user not in group${NC}"
    echo "   Add with: sudo usermod -aG dialout $CURRENT_USER"
    echo "   (logout and login required after adding)"
    MISSING=1
fi

echo ""

# ============================================
# Exit if missing dependencies
# ============================================
if [ $MISSING -eq 1 ]; then
    echo -e "${YELLOW}⚠️  Missing dependencies! Install them and run this script again.${NC}"
    exit 1
fi

# ============================================
# PHASE 2: Installation
# ============================================
echo -e "${GREEN}🚀 All dependencies satisfied! Installing...${NC}"
echo ""

# Backward compat: stop and disable old service name if exists
sudo systemctl stop meshtastic-mapper 2>/dev/null || true
sudo systemctl disable meshtastic-mapper 2>/dev/null || true

# Check if service is already running
if systemctl is-active --quiet meshpulse 2>/dev/null; then
    echo "⏹️  Stopping existing service..."
    sudo systemctl stop meshpulse
fi

# Generate service file from template
echo "📝 Generating systemd service file..."
sed -e "s|{{USER}}|$CURRENT_USER|g" \
    -e "s|{{REPO_PATH}}|$REPO_PATH|g" \
    systemd/meshpulse.service.template > systemd/meshpulse.service

# Copy service file
echo "📋 Installing systemd service..."
sudo cp systemd/meshpulse.service /etc/systemd/system/

# Create web directory
echo "📁 Creating web directory..."
sudo mkdir -p /var/www/html/meshpulse

# Copy frontend files
echo "🌐 Copying frontend files..."
sudo cp frontend/index.html /var/www/html/meshpulse/
sudo cp frontend/styles.css /var/www/html/meshpulse/
sudo cp frontend/favicon.ico /var/www/html/meshpulse/
sudo cp frontend/favicon_stats.ico /var/www/html/meshpulse/
sudo cp frontend/stats.html /var/www/html/meshpulse/
sudo cp frontend/config.html /var/www/html/meshpulse/
sudo cp frontend/messages.html /var/www/html/meshpulse/
sudo chown -R $CURRENT_USER:$CURRENT_USER /var/www/html/meshpulse

# Copy plugin frontend assets (lighttpd doesn't follow symlinks)
if [ -d "$PLUGIN_DIR" ]; then
    # Remove old symlink if exists
    if [ -L "/var/www/html/meshpulse/plugins" ]; then
        sudo rm /var/www/html/meshpulse/plugins
    fi
    # Copy plugin files to web root
    sudo mkdir -p /var/www/html/meshpulse/plugins
    # Copy each installed plugin
    for author_dir in "$PLUGIN_DIR"/*/; do
        [ -d "$author_dir" ] || continue
        author=$(basename "$author_dir")
        [ "$author" = "enabled.json" ] && continue
        for plugin_dir in "$author_dir"*/; do
            [ -d "$plugin_dir" ] || continue
            plugin=$(basename "$plugin_dir")
            dest="/var/www/html/meshpulse/plugins/$author/$plugin"
            sudo mkdir -p "$dest"
            sudo cp -r "$plugin_dir"* "$dest/" 2>/dev/null || true
        done
    done
    sudo chown -R $CURRENT_USER:$CURRENT_USER /var/www/html/meshpulse/plugins
    echo "🔌 Plugin assets copied to web root"
fi

# Create empty nodes.json if it doesn't exist
if [ ! -f "/var/www/html/meshpulse/nodes.json" ]; then
    echo "📄 Creating empty nodes.json..."
    echo '{"ts":0,"updated":"","cnt":0,"cnt_no_pos":0,"max_distance_km":null,"farthest_node":null,"tracker":{},"nodes":[],"nodes_no_pos":[],"messages":[]}' | sudo tee /var/www/html/meshpulse/nodes.json > /dev/null
    sudo chown $CURRENT_USER:$CURRENT_USER /var/www/html/meshpulse/nodes.json
fi

# Configure lighttpd redirect: /meshtastic/ → /meshpulse/ (backward compat)
if command -v lighttpd &> /dev/null; then
    echo "🔀 Configuring backward compat redirect /meshtastic/ → /meshpulse/..."
    sudo mkdir -p /etc/lighttpd/conf-available
    cat << 'LIGHTTPD_EOF' | sudo tee /etc/lighttpd/conf-available/meshpulse.conf > /dev/null
# MeshPulse - redirect legacy /meshtastic/ to /meshpulse/
$HTTP["url"] =~ "^/meshtastic(/.*)?$" {
    url.redirect = ( "^/meshtastic(/.*)?$" => "/meshpulse$1" )
}
LIGHTTPD_EOF
    sudo ln -sf /etc/lighttpd/conf-available/meshpulse.conf /etc/lighttpd/conf-enabled/meshpulse.conf
    sudo systemctl reload lighttpd 2>/dev/null || sudo service lighttpd reload 2>/dev/null || true
    echo "✅ Redirect configured"
fi

# Create config.json from example if it doesn't exist
if [ ! -f "$REPO_PATH/config.json" ]; then
    echo "⚙️  Creating config.json from example..."
    cp "$REPO_PATH/config.json.example" "$REPO_PATH/config.json"
fi

# Reload systemd
echo "🔄 Reloading systemd..."
sudo systemctl daemon-reload

# Enable service
echo "✨ Enabling service..."
sudo systemctl enable meshpulse

echo ""
echo "========================================"
echo -e "${GREEN}✅ Installation complete!${NC}"
echo "========================================"
echo ""
echo "Next steps:"
echo "  1. Start service:  sudo systemctl start meshpulse"
echo "  2. Check status:   sudo systemctl status meshpulse"
echo "  3. View logs:      sudo journalctl -u meshpulse -f"
echo "  4. Open browser:   http://$(hostname).local/meshpulse/"
echo ""
echo ""
