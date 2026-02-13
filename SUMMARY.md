# Odoo Deployment Platform — Summary

**Repo:** https://github.com/fuugus/odoo-platform.git
**Server:** Trendhosting VPS (82.199.139.228), Ubuntu 24.04
**Pilot customer:** Kaminfeger Schweiz (kaminfeger.ch / feuko.ch)

## What It Is

Self-hosted Odoo.sh alternative. Single repo that provisions an Ubuntu server with everything needed to run multiple isolated Odoo 19 instances, managed via a web-based admin panel.

## Architecture

- **PostgreSQL 16** — shared DB server
- **Nginx** — wildcard reverse proxy + SSL (`*.odoo.binaryone.ch`)
- **N x Odoo 19** — one systemd service per instance (own port, config, DB)
- **Mailpit** — catches outgoing email in dev/staging
- **FastAPI admin panel** — setup wizard, instance/DB management, deployment (port 8080)

## Status

**Working:**
- One-command bootstrap (`bootstrap.sh`) — installs deps, creates systemd service, starts admin panel
- 7-step setup wizard with live WebSocket progress (system update, PostgreSQL, wkhtmltopdf, Odoo source, Nginx+SSL, Mailpit, first instance)
- Instance lifecycle (create/delete/start/stop/restart) via API and UI
- DB management (list/clone/delete)
- Git-based deployment (pull + restart)
- JSON config state (`platform.json`)


## Access

SSH tunnel from local machine, then open `http://localhost:8080`:
```
ssh -L 8080:localhost:8080 odoo-poc
```
