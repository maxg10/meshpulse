#!/bin/bash
#
# Meshtastic Network Mapper - Installer
# https://github.com/maxg10/meshtastic-network-mapper
#

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "========================================"
echo " Meshtastic Network Mapper - Installer"
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

# Check meshtastic CLI
if command -v meshtastic &> /dev/null || [ -f ~/.local/bin/meshtastic ]; then
    echo -e "${GREEN}✅ meshtastic - OK${NC}"
else
    echo -e "${RED}❌ meshtastic - NOT FOUND${NC}"
    echo "   Install with: pip3 install meshtastic --break-system-packages"
    MISSING=1
fi

# Check web server (lighttpd or apache2)
if command -v lighttpd &> /dev/null; then
    echo -e "${GREEN}✅ lighttpd - OK${NC}"
    WEBSERVER="lighttpd"
elif command -v apache2 &> /dev/null; then
    echo -e "${GREEN}✅ apache2 - OK (will use instead of lighttpd)${NC}"
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
sudo chown -R $CURRENT_USER:$CURRENT_USER /var/www/html/meshtastic

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
