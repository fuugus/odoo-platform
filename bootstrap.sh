#!/bin/bash
# =============================================================================
# Odoo Deployment Platform - Bootstrap Script
# =============================================================================
# This script installs only the minimal Python dependencies and starts the
# FastAPI Admin Panel. Everything else is done through the Web UI.
# =============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
ADMIN_DIR="$SCRIPT_DIR/admin_panel"

echo "============================================="
echo "  Odoo Deployment Platform - Bootstrap"
echo "============================================="
echo ""

# 1. Install system prerequisites
echo "[1/4] Installing system prerequisites..."
apt-get update -qq
apt-get install -y -qq python3-pip python3-venv git > /dev/null 2>&1
echo "  ✓ python3-pip, python3-venv, git installed"

# 2. Create Python virtual environment
echo "[2/4] Creating Python virtual environment..."
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
    echo "  ✓ Virtual environment created at $VENV_DIR"
else
    echo "  ✓ Virtual environment already exists"
fi

# 3. Install Python dependencies
echo "[3/4] Installing Python dependencies..."
source "$VENV_DIR/bin/activate"
pip install --quiet --upgrade pip
pip install --quiet \
    fastapi==0.115.* \
    uvicorn[standard]==0.34.* \
    jinja2==3.1.* \
    python-multipart==0.0.* \
    websockets==14.* \
    httpx==0.28.* \
    psutil==6.*
echo "  ✓ Python dependencies installed"

# 4. Create systemd service for admin panel
echo "[4/4] Setting up Admin Panel service..."
cat > /etc/systemd/system/odoo-admin-panel.service << EOF
[Unit]
Description=Odoo Platform Admin Panel
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$ADMIN_DIR
ExecStart=$VENV_DIR/bin/uvicorn main:app --host 0.0.0.0 --port 8080
Restart=always
RestartSec=5
Environment=PLATFORM_DIR=$SCRIPT_DIR

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable odoo-admin-panel.service
systemctl restart odoo-admin-panel.service

echo ""
echo "============================================="
echo "  ✓ Bootstrap complete!"
echo "============================================="
echo ""
echo "  Admin Panel: http://$(hostname -I | awk '{print $2}'):8080"
echo ""
echo "  Open the URL above in your browser to"
echo "  continue with the Setup Wizard."
echo "============================================="
