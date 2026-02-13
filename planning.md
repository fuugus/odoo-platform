# Odoo Deployment Platform

Self-hosted Odoo platform as a replacement for Odoo.sh. One repo, one command, everything is set up.

## Architecture

One server, everything on it:

- **1x PostgreSQL 16** – all databases
- **1x Nginx** – reverse proxy, wildcard SSL
- **Nx Odoo 19 instances** – one per environment/developer, own port, own systemd service
- **1x Mailpit** – catches all emails in staging/dev
- **1x FastAPI Admin Panel** – setup wizard, deployment, database management

## Port Schema

| Port | Instance |
|------|----------|
| 8080 | Admin Panel (FastAPI) |
| 8069 | Odoo Production |
| 8070 | Odoo Staging |
| 8071+ | Odoo Dev (per developer) |

## Naming Conventions

| Entity | Pattern | Example |
|--------|---------|---------|
| Database | `{client}_{env}` | `kaminfeger_prod` |
| Dev DB | `{client}_{env}_{dev}` | `kaminfeger_dev_samuel` |
| Subdomain | `{client}-{env}.odoo.binaryone.ch` | `kaminfeger-staging.odoo.binaryone.ch` |
| Addon module | `{client}_{function}` | `kaminfeger_feuko` |
| Git branch | `{env}/{client}/{feature}` | `staging/kaminfeger/feuko-reports` |

## DNS & Routing

- Wildcard DNS: `*.odoo.binaryone.ch` → Server IP
- Nginx parses the subdomain and routes to the correct Odoo port, setting `dbfilter` to the matching database
- No manual DNS or Nginx entry needed per client

## Why Multiple Odoo Instances?

Odoo shares its addons path and process across all databases of one instance. A Python error or module update → restart → downtime for ALL databases on that instance. Therefore: separate process per environment and developer.

## Bootstrap Concept

```bash
apt install -y python3-pip python3-venv git
git clone <repo>
cd odoo-platform && ./bootstrap.sh
# → starts FastAPI on :8080
# → Web UI shows setup wizard
```

`bootstrap.sh` only installs Python dependencies and starts the admin panel. Everything else happens through the web UI:

1. System Update & Packages
2. PostgreSQL 16
3. Nginx + Wildcard SSL (Let's Encrypt)
4. Odoo 19 (source install from GitHub)
5. Mailpit
6. Create first Odoo instance (prod)

Each step has a button, shows progress, and is idempotent (clicking again is safe).

## Admin Panel Features (after setup)

- **Dashboard**: Module status per environment (reads `ir_module_module` via XML-RPC)
- **Deploy**: git pull → module install/upgrade → service restart
- **Database Management**: create, clone, delete
- **Schema Sync**: transfer custom fields/views/actions between environments
- **Instance Management**: create/delete dev instances

## Developer Workflow

1. Developer works on the server via VS Code Remote SSH
2. Own Odoo instance + own database
3. Commit/push to GitHub (backup + review)
4. PR → merge → admin panel deploys to staging → production

## Mail

| Environment | Setup |
|-------------|-------|
| Production | Real SMTP |
| Staging/Dev | Mailpit (catches everything, sends nothing) |

## Server Requirements

- Ubuntu 24.04 LTS
- Min. 4 GB RAM (8+ recommended with multiple developers)
- Python 3.12 (ships with Ubuntu 24.04)
- PostgreSQL 16
- Odoo 19 requires PostgreSQL 13+

## Pilot Client

Kaminfeger Schweiz (kaminfeger.ch / feuko.ch)

## Current PoC Server

- Trendhosting Virtuozzo (Jelastic), Ubuntu 24.04 VPS
- IP: 82.199.139.228
- SSH via gateway: `gate.trendhosting.cloud:3022`
- Evaluation ongoing – alternative: Hetzner CX52 for ~€32/month
