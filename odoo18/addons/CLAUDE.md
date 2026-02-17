# CLAUDE.md — Odoo 18 Custom Addons

You are working inside an Odoo 18 instance's custom addons directory.

## Where You Are

- **This directory:** Custom addons for one Odoo 18 instance
- **Odoo source:** `/opt/odoo18/odoo/` (community), `/opt/odoo18/enterprise/` (enterprise)
- **Instance config:** `/etc/odoo/<instance_name>.conf`
- **Instance data:** `/opt/odoo18/data/<instance_name>/`
- **Logs:** `/var/log/odoo/<instance_name>.log`
- **Instance name** = directory name under `data/`, e.g. `client_dev_name`

## Odoo Module Structure

Each subdirectory here is an Odoo module. A minimal module needs:
- `__manifest__.py` — name, version, depends, data files
- `__init__.py` — Python imports
- `models/`, `views/`, `security/`, `data/` — as needed

## Deploy and Restart

Dev instances (workers=0) auto-reload Python and XML changes. For a full restart:

```bash
# Find your instance service
systemctl list-units 'odoo-*' --no-pager

# Restart it
sudo systemctl restart odoo-<instance_name>

# Tail logs
sudo journalctl -u odoo-<instance_name> -f
```

To install or upgrade a module, restart with:
```bash
sudo systemctl stop odoo-<instance_name>
sudo /opt/odoo18/venv/bin/python3 /opt/odoo18/odoo/odoo-bin \
    -c /etc/odoo/<instance_name>.conf \
    -d <instance_name> \
    -u <module_name> \
    --stop-after-init --logfile=
sudo systemctl start odoo-<instance_name>
```

## Platform Admin Panel

The instance is managed by an admin panel at `https://admin.odoo.<domain>/`. It handles:
- Instance creation/deletion, sync from prod, deploy, SSL
- Studio Bridge (export Odoo Studio customizations to code modules)
- SSH key management, admin password verification

Developers cannot access the admin panel directly. Ask the project admin for instance operations.

## Database Access

```bash
psql -U odoo -d <instance_name>
```

## Project Rules

- **No change-log comments in code.** Don't add comments like `# changed X to Y` or `# added by Claude`.
- **No Co-Authored-By in commits.**
- **English only.** All code, comments, and documentation in English. UI strings in modules may use any language as needed by the client.
- **Commit message format.** Short summary first line, blank line, then bullet points.
