# Odoo 19 Custom Addons

Custom modules for Odoo 19 instances managed by the odoo-platform.

## Development

SSH into your dev instance and edit modules directly in `~/addons/`.
Dev instances auto-reload on Python/XML changes (workers=0).

Install/upgrade a module:
```bash
sudo systemctl stop odoo-<instance_name>
sudo /opt/odoo19/venv/bin/python3 /opt/odoo19/odoo/odoo-bin \
    -c /etc/odoo/<instance_name>.conf -d <instance_name> \
    -u <module_name> --stop-after-init --logfile=
sudo systemctl start odoo-<instance_name>
```
