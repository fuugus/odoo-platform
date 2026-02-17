# Odoo 18 Custom Addons

Custom modules for Odoo 18 instances managed by the odoo-platform. Shared via local bare git repo at `/opt/git/odoo18-addons.git`.

## Development

SSH into your dev instance and edit modules directly in `~/addons/`.
Dev instances auto-reload on Python/XML changes (workers=0).

Install/upgrade a module:
```bash
sudo systemctl stop odoo-<instance_name>
sudo /opt/odoo18/venv/bin/python3 /opt/odoo18/odoo/odoo-bin \
    -c /etc/odoo/<instance_name>.conf -d <instance_name> \
    -u <module_name> --stop-after-init --logfile=
sudo systemctl start odoo-<instance_name>
```
