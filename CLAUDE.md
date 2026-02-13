# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Self-hosted Odoo.sh alternative — a single repo that provisions an Ubuntu 24.04 server with a complete Odoo 19 deployment platform. Manages multiple isolated Odoo instances via a FastAPI-based admin panel.

## Tech Stack

- **Backend:** Python 3.12, FastAPI 0.115, Uvicorn 0.34, Jinja2 templates
- **Frontend:** Vanilla JavaScript, CSS3 custom properties (no build tools, no npm)
- **Infrastructure:** Nginx (wildcard reverse proxy), PostgreSQL 16, systemd, Let's Encrypt SSL
- **Config:** `platform.json` (JSON state file, auto-generated at runtime)

## Commands

```bash
# Start/restart admin panel
systemctl restart odoo-admin-panel

# View admin panel logs
journalctl -u odoo-admin-panel -f

# Run directly for development (from admin_panel/)
cd /root/odoo-platform/admin_panel && /root/odoo-platform/.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8080 --reload

# Odoo instance services
systemctl status odoo-{instance_name}
systemctl restart odoo-{instance_name}

# Bootstrap (first-time setup only)
./bootstrap.sh
```

No test suite or linter is configured.

## Architecture

**Entry point:** `admin_panel/main.py` — FastAPI app with routes, API endpoints, and WebSocket handlers.

**Key modules:**
- `admin_panel/config.py` — Loads/saves `platform.json`, provides `get_config()`/`save_config()`
- `admin_panel/setup_steps.py` — 8 async setup steps (system_update → postgresql → wkhtmltopdf → odoo_source → nginx → mailpit → dns_check → ssl_certs), each idempotent and streaming progress via WebSocket
- `admin_panel/templates/` — Jinja2 templates (base, setup, dashboard, instances, deploy, databases)
- `admin_panel/static/` — Single CSS file + single JS file, no build pipeline

**Instance naming convention:**
- Instance/DB name: `{client}_{env}` (e.g., `kaminfeger_prod`)
- Subdomain: `{client}-{env}.{domain}` (underscores become hyphens)
- Systemd service: `odoo-{instance_name}`
- Config file: `/etc/odoo/{instance_name}.conf`
- Data dir: `/opt/odoo/data/{instance_name}`

**Port allocation:**
- 8069+ → Odoo instances (dynamically allocated, all environments)
- 8080 → admin panel, 8025 → Mailpit UI, 1025 → Mailpit SMTP

**Key patterns:**
- All long-running operations (setup steps, instance creation) stream logs to the browser via WebSocket
- Shell commands run through `asyncio.create_subprocess_shell` with real-time output capture
- Setup steps are ordered and have dependency checks; each can be re-run safely

## UI Design

**Heroku Data–inspired flat design.** White background, purple accent (#6944ba), muted grays. System fonts, uppercase small caps for labels. SVG mask-based icons (data URIs). CSS custom properties in `:root` for theming.

## Project Rules

- **No change-log comments in code.** Don't add comments like `# changed X to Y` or `# added by Claude`. Only add comments where we discussed important functionality worth remembering.
- **Modern, flat UI design.** All UI work must use a clean, professional, flat design. No rounded cartoon elements, no playful colors, no childish aesthetics. Think SaaS admin panels — minimal, sharp, confident.
- **No Co-Authored-By in commits.** Do not append `Co-Authored-By` trailers to commit messages.
- **Commit message format.** First line is a short summary. Follow with a blank line, then bullet points (no dashes or asterisks — just plain lines) describing each meaningful change.
- **English only.** All code, comments, UI text, and documentation must be in professional English. No German or other languages.
- **Restart services after code changes.** After editing backend files, run `systemctl restart odoo-admin-panel` (and any affected Odoo instance services) so changes take effect. The production service does not auto-reload.
