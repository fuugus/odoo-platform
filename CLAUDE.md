# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Self-hosted Odoo.sh alternative — a single repo that provisions an Ubuntu 24.04 server with a multi-version Odoo 18/19 deployment platform. Manages multiple isolated Odoo instances via a FastAPI-based admin panel with Google OAuth authentication.

## Tech Stack

- **Backend:** Python 3.12, FastAPI 0.115, Uvicorn 0.34, Jinja2 templates
- **Frontend:** Vanilla JavaScript, CSS3 custom properties (no build tools, no npm)
- **Infrastructure:** Nginx (wildcard reverse proxy), PostgreSQL 16, systemd, Let's Encrypt SSL
- **Auth:** Google OAuth 2.0 (optional, configured via admin panel)
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
- `admin_panel/config.py` — Loads/saves `platform.json`, provides `load_config()`/`save_config()`/`update_step_status()`. Includes `migrate_config()` for schema upgrades (e.g., `odoo_source` → `odoo_19`/`odoo_18`).
- `admin_panel/setup_steps.py` — 9 async setup steps (system_update → postgresql → wkhtmltopdf → odoo_19/odoo_18 → nginx → mailpit → dns_check → ssl_certs), each idempotent and streaming progress via WebSocket. Also contains `create_odoo_instance()`, `delete_odoo_instance()`, and `sync_instance_from_prod()`.
- `admin_panel/auth.py` — Google OAuth 2.0: consent URL generation, code-for-token exchange, session-based user storage, path exemption logic, WebSocket auth verification.
- `admin_panel/templates/` — Jinja2 templates: base (sidebar layout), setup, dashboard, instances, deploy, databases, login
- `admin_panel/static/css/style.css` + `admin_panel/static/js/app.js` — no build pipeline

**Two separate Python virtual environments:**
- `.venv/` (repo root) — runs the FastAPI admin panel (fastapi, uvicorn, jinja2, websockets, httpx)
- `/opt/odoo{ver}/venv/` — runs Odoo instances (odoo requirements.txt), one per version

**Instance naming convention:**
- Instance/DB name: `{client}_{env}` (e.g., `kaminfeger_prod`)
- Subdomain: `{client}-{env}.{domain}` (underscores become hyphens in URLs)
- Systemd service: `odoo-{instance_name}`
- Config file: `/etc/odoo/{instance_name}.conf`
- Data dir: `/opt/odoo{ver}/data/{instance_name}`
- Client names validated: `^[a-z]+(-[a-z]+)*$` (lowercase + hyphens only)

**Custom addons (in this repo):**
- `odoo19/addons/` — Custom Odoo 19 modules (e.g., `website_kftheme`)
- `odoo18/addons/` — Custom Odoo 18 modules
- Deploy rsyncs from `odoo{ver}/addons/` → `/opt/odoo{ver}/data/{instance_name}/addons/`

**Odoo filesystem layout (`/opt/odoo{ver}/`):**
- `odoo/` — Community source (git, branch {ver}.0)
- `enterprise/` — Enterprise source (git, branch {ver}.0, requires GitHub token)
- `venv/` — Odoo Python venv
- `data/{instance_name}/` — per-instance data dir (filestore, sessions, addons)

**Port allocation:**
- 8069+ → Odoo instances (each gets two consecutive ports: HTTP and gevent/longpolling)
- 8080 → admin panel, 8025 → Mailpit UI, 1025 → Mailpit SMTP

**Nginx config structure:**
- `/etc/nginx/sites-available/odoo-platform` — main config (admin, mailpit, default server)
- `/etc/nginx/odoo-instances/{instance_name}.conf` — per-instance server blocks, auto-generated on instance create/delete
- Main config includes per-instance files via `include /etc/nginx/odoo-instances/*.conf;`

**`platform.json` structure:**
- `domain` — wildcard domain (e.g., `odoo.binaryone.ch`)
- `github_token` — GitHub PAT for Enterprise repo access
- `pg_version` — PostgreSQL version (default "16")
- `setup_steps` — map of step_id → `{status, label, description, message}`
- `instances` — map of instance_name → `{client, env, version, port, workers, master_pw, admin_pw, db_name, service, conf}`
- `auth` — `{google_client_id, google_client_secret, session_secret}` (session_secret auto-generated)
- `clients` — reserved, currently unused

**Key patterns:**
- All long-running operations (setup steps, instance creation, prod sync) stream logs to the browser via WebSocket
- `run_cmd()` in `setup_steps.py` is the core utility — runs shell commands via `asyncio.create_subprocess_shell` and optionally streams each line to a `ws_send` callback
- WebSocket messages use JSON: `{type: "log"|"status"|"error", message|status: ...}`
- Setup steps are ordered with dependency checks (tuples = OR deps, lists = AND); each can be re-run safely
- `sync_instance_from_prod()` flow: stop target → drop target DB → terminate prod connections → `createdb -T` clone → copy filestore → rsync custom addons → `odoo-bin neutralize` → start target
- Static assets use cache-busting via `?v={mtime}` query strings (`_static_url()` helper)
- Auth middleware redirects unauthenticated users to `/login` when OAuth is configured; exempt paths: `/login`, `/auth/*`, `/api/health`, `/static/*`
- Prod instances use localhost:25 (real SMTP); dev/staging use localhost:1025 (Mailpit)

**Frontend (`app.js`) key utilities:**
- `showToast(message, type)` — notification toasts (info/success/error), auto-dismiss 3s
- `StatusPoller` — polls `/api/instances` at 250ms (fast) or 5000ms (slow) to track instance states; settles pending operations after 15s
- `connectWebSocket(path, logElement, callbacks)` — opens WS, streams log/status/error messages to a pre element, auto-scrolls
- Health check poll every 15s updates sidebar status indicator

## UI Design

**Heroku Data-inspired flat design.** White background, purple accent (#6944ba), muted grays. System fonts, uppercase small caps for labels. SVG mask-based icons (data URIs). CSS custom properties in `:root` for theming. 3px border-radius. Cards use bottom borders (no box shadows).

## Project Rules

- **No change-log comments in code.** Don't add comments like `# changed X to Y` or `# added by Claude`. Only add comments where we discussed important functionality worth remembering.
- **Modern, flat UI design.** All UI work must use a clean, professional, flat design. No rounded cartoon elements, no playful colors, no childish aesthetics. Think SaaS admin panels — minimal, sharp, confident.
- **No Co-Authored-By in commits.** Do not append `Co-Authored-By` trailers to commit messages.
- **Commit message format.** First line is a short summary. Follow with a blank line, then bullet points (no dashes or asterisks — just plain lines) describing each meaningful change.
- **English only.** All code, comments, UI text, and documentation must be in professional English. No German or other languages.
- **Restart services after code changes.** After editing backend files, run `systemctl restart odoo-admin-panel` (and any affected Odoo instance services) so changes take effect. The production service does not auto-reload.
- **Never commit or push autonomously.** Only run `git commit` and `git push` when the user explicitly asks. Always wait for confirmation before committing or pushing.
