# Odoo Deployment Platform

Self-hosted Odoo platform as a replacement for Odoo.sh.  
One repo, one command, everything gets set up.

## Quick Start

```bash
# On a fresh Ubuntu 24.04 server:
git clone https://github.com/fuugus/odoo-platform.git /root/odoo-platform
cd /root/odoo-platform
chmod +x bootstrap.sh
./bootstrap.sh
```

This installs Python dependencies and starts the **Admin Panel** as a systemd service on port 8080.

## Accessing the Admin Panel

**Via VS Code Remote SSH (recommended for development):**
1. Connect to the server with VS Code Remote SSH
2. Open the **Ports** tab (bottom panel, next to Terminal)
3. Click **"Forward a Port"** and enter `8080`
4. Open `http://127.0.0.1:8080` in your browser

> **Note:** VS Code sometimes auto-detects the port, but if not, add it manually.
> Alternatively, run `systemctl restart odoo-admin-panel` to trigger auto-detection.

**After Nginx setup (production):**
- `http://admin.odoo.binaryone.ch`
