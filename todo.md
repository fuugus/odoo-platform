# TODO

## Pending

- **Re-enable Mailpit SMTP after neutralize** — Odoo's `--neutralize` disables outgoing email. After syncing prod to staging, the outgoing mail server config should be updated to point to Mailpit (localhost:1025) so newsletter and email testing works on staging environments. This needs a post-neutralize SQL update or XML-RPC call to set `ir.mail_server` records.

- ~~Authentication on admin panel~~
- Backup/restore
- Schema-sync between environments
- **Separate custom addons from platform repo** — Currently `odoo19/addons/` lives inside the platform repo alongside the admin panel. Custom client modules (kaminfeger_base, kaminfeger_website) should move to their own per-client repos so the platform stays generic and client code is managed independently.
