#!/bin/bash
#
# Meshtastic Network Mapper - Installer
# https://github.com/maxg10/meshtastic-network-mapper
#

# Get version from backend
VERSION=$(grep -m1 "^#ver" backend/meshtastic_mapper.py | awk '{print $2}')

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "========================================"
echo " Meshtastic Network Mapper - Installer"
echo " Version: $VERSION"
echo "========================================"
echo ""

# Check if running from correct directory
if [ ! -f "backend/meshtastic_mapper.py" ]; then
    echo -e "${RED}❌ Error: Run this script from the repository root directory${NC}"
    echo "   cd ~/meshtastic-network-mapper && ./install.sh"
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

# Check plugin Python dependencies
for req_file in "$PLUGIN_DIR"/*/*/requirements.txt; do
    [ -f "$req_file" ] || continue
    plugin_name=$(basename "$(dirname "$req_file")")
    while IFS= read -r line || [ -n "$line" ]; do
        # Skip empty lines and comments
        [[ -z "$line" || "$line" == \#* ]] && continue
        # Strip version specifiers to get bare package name
        pkg_name=$(echo "$line" | sed 's/[>=<!~].*//' | sed 's/[[:space:]].*//' | tr -d ' ')
        [ -z "$pkg_name" ] && continue
        if pip3 show "$pkg_name" &>/dev/null; then
            PKG_VER=$(pip3 show "$pkg_name" 2>/dev/null | grep "^Version:" | awk '{print $2}')
            echo -e "${GREEN}✅ $pkg_name - OK ($PKG_VER)${NC}"
        else
            echo -e "${RED}❌ $pkg_name - NOT FOUND (required by $plugin_name)${NC}"
            echo "   Install with: pip3 install $pkg_name --break-system-packages"
            MISSING=1
        fi
    done < "$req_file"
done

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

# Check if service is already running
if systemctl is-active --quiet meshtastic-mapper 2>/dev/null; then
    echo "⏹️  Stopping existing service..."
    sudo systemctl stop meshtastic-mapper
fi

# Generate service file from template
echo "📝 Generating systemd service file..."
sed -e "s|{{USER}}|$CURRENT_USER|g" \
    -e "s|{{REPO_PATH}}|$REPO_PATH|g" \
    systemd/meshtastic-mapper.service.template > systemd/meshtastic-mapper.service

# Copy service file
echo "📋 Installing systemd service..."
sudo cp systemd/meshtastic-mapper.service /etc/systemd/system/

# Create web directory
echo "📁 Creating web directory..."
sudo mkdir -p /var/www/html/meshtastic

# Copy frontend files
echo "🌐 Copying frontend files..."
sudo cp frontend/index.html /var/www/html/meshtastic/
sudo cp frontend/styles.css /var/www/html/meshtastic/
sudo cp frontend/favicon.ico /var/www/html/meshtastic/
sudo cp frontend/favicon_stats.ico /var/www/html/meshtastic/
sudo cp frontend/stats.html /var/www/html/meshtastic/
sudo cp frontend/config.html /var/www/html/meshtastic/
sudo cp frontend/messages.html /var/www/html/meshtastic/
sudo chown -R $CURRENT_USER:$CURRENT_USER /var/www/html/meshtastic

# Copy plugin frontend assets (lighttpd doesn't follow symlinks)
if [ -d "$PLUGIN_DIR" ]; then
    # Remove old symlink if exists
    if [ -L "/var/www/html/meshtastic/plugins" ]; then
        sudo rm /var/www/html/meshtastic/plugins
    fi
    # Copy plugin files to web root
    sudo mkdir -p /var/www/html/meshtastic/plugins
    # Copy each installed plugin
    for author_dir in "$PLUGIN_DIR"/*/; do
        [ -d "$author_dir" ] || continue
        author=$(basename "$author_dir")
        [ "$author" = "enabled.json" ] && continue
        for plugin_dir in "$author_dir"*/; do
            [ -d "$plugin_dir" ] || continue
            plugin=$(basename "$plugin_dir")
            dest="/var/www/html/meshtastic/plugins/$author/$plugin"
            sudo mkdir -p "$dest"
            sudo cp -r "$plugin_dir"* "$dest/" 2>/dev/null || true
        done
    done
    sudo chown -R $CURRENT_USER:$CURRENT_USER /var/www/html/meshtastic/plugins
    echo "🔌 Plugin assets copied to web root"
fi

# Create empty nodes.json if it doesn't exist
if [ ! -f "/var/www/html/meshtastic/nodes.json" ]; then
    echo "📄 Creating empty nodes.json..."
    echo '{"ts":0,"updated":"","cnt":0,"cnt_no_pos":0,"max_distance_km":null,"farthest_node":null,"tracker":{},"nodes":[],"nodes_no_pos":[],"messages":[]}' | sudo tee /var/www/html/meshtastic/nodes.json > /dev/null
    sudo chown $CURRENT_USER:$CURRENT_USER /var/www/html/meshtastic/nodes.json
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
sudo systemctl enable meshtastic-mapper

echo ""
echo "========================================"
echo -e "${GREEN}✅ Installation complete!${NC}"
echo "========================================"
echo ""
echo "Next steps:"
echo "  1. Start service:  sudo systemctl start meshtastic-mapper"
echo "  2. Check status:   sudo systemctl status meshtastic-mapper"
echo "  3. View logs:      sudo journalctl -u meshtastic-mapper -f"
echo "  4. Open browser:   http://$(hostname).local/meshtastic/"
echo ""
echo ""
