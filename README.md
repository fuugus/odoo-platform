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

## Setup Wizard

Open the Admin Panel and click through the setup steps:

1. **System Update** – apt upgrade + build dependencies
2. **PostgreSQL 16** – database server
3. **wkhtmltopdf** – PDF report generation
4. **Odoo 19 Source** – clone from GitHub + Python venv
5. **Nginx + SSL** – wildcard reverse proxy
6. **Mailpit** – email catcher for dev/staging
7. **First Instance** – creates production Odoo instance

Each step is **idempotent** – safe to run again.

## Service Management

```bash
# Admin Panel
systemctl status odoo-admin-panel
systemctl restart odoo-admin-panel

# Odoo instances (after setup)
systemctl status odoo-kaminfeger_prod
systemctl restart odoo-kaminfeger_prod
```

## Project Structure

```
odoo-platform/
├── bootstrap.sh              # One-command setup
├── platform.json             # Runtime config (auto-generated)
├── planning.md               # Architecture & design notes
├── README.md                 # This file
└── admin_panel/
    ├── main.py               # FastAPI application
    ├── config.py             # Config management
    ├── setup_steps.py        # Setup step implementations
    ├── static/css/style.css  # Dark theme UI
    ├── static/js/app.js      # Frontend JS
    └── templates/            # Jinja2 HTML templates
```

## Architecture

See [planning.md](planning.md) for full architecture details.
