# Odoo Deployment Platform

Self-hosted Odoo-Plattform als Ersatz für Odoo.sh. Ein Repo, ein Command, alles wird aufgesetzt.

## Architektur

Ein Server, alles drauf:

- **1× PostgreSQL 16** – alle Datenbanken
- **1× Nginx** – Reverse Proxy, Wildcard SSL
- **N× Odoo 19 Instanzen** – je eine pro Environment/Entwickler, eigener Port, eigener systemd Service
- **1× Mailpit** – fängt alle Mails in Staging/Dev ab
- **1× FastAPI Admin Panel** – Setup-Wizard, Deployment, DB-Verwaltung

## Port-Schema

| Port | Instanz |
|------|---------|
| 8080 | Admin Panel (FastAPI) |
| 8069 | Odoo Production |
| 8070 | Odoo Staging |
| 8071+ | Odoo Dev (pro Entwickler) |

## Naming Conventions

| Entity | Pattern | Beispiel |
|--------|---------|----------|
| Datenbank | `{client}_{env}` | `kaminfeger_prod` |
| Dev-DB | `{client}_{env}_{dev}` | `kaminfeger_dev_samuel` |
| Subdomain | `{client}-{env}.odoo.binaryone.ch` | `kaminfeger-staging.odoo.binaryone.ch` |
| Addon-Modul | `{client}_{function}` | `kaminfeger_feuko` |
| Git Branch | `{env}/{client}/{feature}` | `staging/kaminfeger/feuko-reports` |

## DNS & Routing

- Wildcard DNS: `*.odoo.binaryone.ch` → Server IP
- Nginx parst Subdomain, routet zum richtigen Odoo-Port, setzt `dbfilter` auf passende DB
- Kein manueller DNS- oder Nginx-Eintrag pro Client nötig

## Warum mehrere Odoo-Instanzen?

Odoo teilt sich Addons-Pfad und Prozess über alle DBs einer Instanz. Ein Python-Error oder Modul-Update → Restart → Downtime für ALLE DBs auf dieser Instanz. Deshalb: eigener Prozess pro Environment und Entwickler.

## Bootstrap-Konzept

```bash
apt install -y python3-pip python3-venv git
git clone <repo>
cd odoo-platform && ./bootstrap.sh
# → startet FastAPI auf :8080
# → Web-UI zeigt Setup-Wizard
```

`bootstrap.sh` installiert nur Python-Deps und startet das Admin Panel. Alles weitere passiert über die Web-UI:

1. System Update & Pakete
2. PostgreSQL 16
3. Nginx + Wildcard SSL (Let's Encrypt)
4. Odoo 19 (Source-Install von GitHub)
5. Mailpit
6. Erste Odoo-Instanz (prod) erstellen

Jeder Step hat einen Button, zeigt Progress, ist idempotent (nochmal drücken = kein Problem).

## Admin Panel Funktionen (nach Setup)

- **Dashboard**: Modul-Status pro Environment (liest `ir_module_module` via XML-RPC)
- **Deploy**: git pull → Modul install/upgrade → Service Restart
- **DB-Verwaltung**: erstellen, klonen, löschen
- **Schema-Sync**: Custom Fields/Views/Actions zwischen Environments übertragen
- **Instanz-Verwaltung**: neue Dev-Instanz erstellen/löschen

## Entwickler-Workflow

1. Dev arbeitet auf Server via VS Code Remote SSH
2. Eigene Odoo-Instanz + eigene DB
3. Commit/Push zu GitHub (Backup + Review)
4. PR → Merge → Admin Panel deployt auf Staging → Production

## Mail

| Environment | Setup |
|-------------|-------|
| Production | Echter SMTP |
| Staging/Dev | Mailpit (fängt alles ab, sendet nichts) |

## Technische Voraussetzungen Server

- Ubuntu 24.04 LTS
- Min. 4 GB RAM (8+ empfohlen bei mehreren Devs)
- Python 3.12 (kommt mit Ubuntu 24.04)
- PostgreSQL 16
- Odoo 19 benötigt PostgreSQL 13+

## Pilot-Kunde

Kaminfeger Schweiz (kaminfeger.ch / feuko.ch)

## Aktueller PoC-Server

- Trendhosting Virtuozzo (Jelastic), Ubuntu 24.04 VPS
- IP: 82.199.139.228
- SSH via Gateway: `gate.trendhosting.cloud:3022`
- Evaluierung läuft – Alternative: Hetzner CX52 für ~€32/Monat