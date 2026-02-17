# CLAUDE.md — Odoo 19 Custom Addons

You are working inside an Odoo 19 instance's custom addons directory. This directory is a git working tree shared across all instances via a local bare repo at `/opt/git/odoo19-addons.git`.

## Where You Are

- **This directory:** Custom addons for one Odoo 19 instance (git repo)
- **Shared repo:** `/opt/git/odoo19-addons.git` (bare, all instances share this)
- **Odoo source:** `/opt/odoo19/odoo/` (community), `/opt/odoo19/enterprise/` (enterprise)
- **Instance config:** `/etc/odoo/<instance_name>.conf`
- **Instance data:** `/opt/odoo19/data/<instance_name>/`
- **Logs:** `/var/log/odoo/<instance_name>.log`
- **Instance name** = directory name under `data/` (e.g. `client_dev_name`)

## Git Workflow

```bash
git pull                            # get latest from other devs
git add -A && git commit -m "..."   # commit your changes
git push                            # share with team
```

All instances share the same repo. After pushing, other instances get changes via `git pull` or the admin panel's Deploy function.

## Module Architecture

**`{client}_base`** — All custom field and model definitions live here. Studio Bridge converts between Odoo Studio (DB definitions) and this module in both directions, so keeping all definitions in one place ensures clean round-trips. Developers can freely edit this module.

**Other modules** (e.g. `{client}_website`, `{client}_hr`, ...) — Custom logic, controllers, templates, reports, etc. These modules must never define custom fields or models — only reference the ones from `{client}_base` via `depends`.

## Odoo Module Structure

A minimal module needs:
- `__manifest__.py` — name, version, depends, data files
- `__init__.py` — Python imports
- `models/`, `views/`, `security/`, `data/` — as needed

## Deploy and Restart

Dev instances (workers=0) auto-reload Python and XML changes. For a full restart:

```bash
sudo systemctl restart odoo-<instance_name>

# Tail logs
journalctl -u odoo-<instance_name> -f
```

To install or upgrade a module:
```bash
sudo systemctl stop odoo-<instance_name>
sudo -u odoo /opt/odoo19/venv/bin/python3 /opt/odoo19/odoo/odoo-bin \
    -c /etc/odoo/<instance_name>.conf \
    -d <instance_name> \
    -u <module_name> \
    --stop-after-init --logfile=
sudo systemctl start odoo-<instance_name>
```

## Database Access

```bash
psql -U odoo -d <instance_name>
```

## Project Rules

- **No change-log comments in code.** Don't add comments like `# changed X to Y` or `# added by Claude`.
- **No Co-Authored-By in commits.**
- **English only.** All code, comments, and documentation in English. UI strings in modules may use any language as needed by the client.
- **Commit message format.** Short summary first line, blank line, then bullet points.
