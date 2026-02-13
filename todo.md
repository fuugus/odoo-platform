# TODO

## Pending

- **Re-enable Mailpit SMTP after neutralize** — Odoo's `--neutralize` disables outgoing email. After syncing prod to staging, the outgoing mail server config should be updated to point to Mailpit (localhost:1025) so newsletter and email testing works on staging environments. This needs a post-neutralize SQL update or XML-RPC call to set `ir.mail_server` records.
