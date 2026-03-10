"""
Platform configuration and state management.
"""
import json
import os
import secrets
from pathlib import Path

PLATFORM_DIR = Path(os.environ.get("PLATFORM_DIR", "/root/odoo-platform"))
CONFIG_FILE = PLATFORM_DIR / "platform.json"

# Defaults
DEFAULT_CONFIG = {
    "domain": "odoo.binaryone.ch",
    "github_token": "",
    "pg_version": "16",
    "custom_addons_repos": {
        "19": "",
        "18": "",
    },
    "setup_steps": {
        "system_update": {"status": "pending", "label": "System Update & Packages", "description": "Installs build tools, Python dev headers, image libraries, fonts, and wkhtmltopdf.", "message": ""},
        "postgresql": {"status": "pending", "label": "PostgreSQL 16", "description": "Adds the official PostgreSQL repo and installs v16.", "message": ""},
        "odoo_19": {"status": "pending", "label": "Odoo 19 Source", "description": "Clones Community + Enterprise 19.0, creates Python venv.", "message": ""},
        "odoo_18": {"status": "pending", "label": "Odoo 18 Source", "description": "Clones Community + Enterprise 18.0, creates Python venv.", "message": ""},
        "nginx": {"status": "pending", "label": "Nginx Reverse Proxy", "description": "Routes subdomains to Odoo instances by port.", "message": ""},
        "mailpit": {"status": "pending", "label": "Mailpit", "description": "Local SMTP catch-all for dev/staging emails.", "message": ""},
        "dns_check": {"status": "pending", "label": "DNS Check", "description": "Verify that *.domain resolves to this server's IP.", "message": ""},
        "ssl_certs": {"status": "pending", "label": "SSL Certificates", "description": "Issues Let's Encrypt certs for admin and mailpit.", "message": ""},
    },
    "instances": {},
    "clients": {},
    "auth": {
        "google_client_id": "",
        "google_client_secret": "",
        "session_secret": "",
    },
}


def migrate_config(config: dict) -> dict:
    """Migrate config from old schema to new multi-version schema."""
    changed = False
    steps = config.get("setup_steps", {})

    # Migrate odoo_source -> odoo_19 + odoo_18
    if "odoo_source" in steps:
        old = steps.pop("odoo_source")
        steps["odoo_19"] = {
            "status": old.get("status", "pending"),
            "label": "Odoo 19 Source",
            "description": "Clones Community + Enterprise 19.0, creates Python venv.",
            "message": old.get("message", ""),
        }
        steps["odoo_18"] = {
            "status": "pending",
            "label": "Odoo 18 Source",
            "description": "Clones Community + Enterprise 18.0, creates Python venv.",
            "message": "",
        }
        changed = True

    # Remove old odoo_version field
    if "odoo_version" in config:
        del config["odoo_version"]
        changed = True

    # Add custom_addons_repos if missing
    if "custom_addons_repos" not in config:
        config["custom_addons_repos"] = {"19": "", "18": ""}
        changed = True

    # Add version field to existing instances
    for name, inst in config.get("instances", {}).items():
        if "version" not in inst:
            inst["version"] = "19"
            changed = True

    # Add auth section if missing
    if "auth" not in config:
        config["auth"] = DEFAULT_CONFIG["auth"].copy()
        changed = True

    # Migrate ssh_key_set (boolean) -> ssh_key_count (integer)
    for name, inst in config.get("instances", {}).items():
        if "ssh_key_set" in inst:
            inst["ssh_key_count"] = 1 if inst.pop("ssh_key_set") else 0
            changed = True

    # Merge wkhtmltopdf step into system_update
    if "wkhtmltopdf" in steps:
        wk_status = steps.pop("wkhtmltopdf").get("status", "pending")
        su = steps.get("system_update", {})
        if wk_status == "done" and su.get("status") == "done":
            su["description"] = "Installs build tools, Python dev headers, image libraries, fonts, and wkhtmltopdf."
        changed = True

    # Auto-generate session_secret if empty
    auth = config.get("auth", {})
    if not auth.get("session_secret"):
        auth["session_secret"] = secrets.token_hex(32)
        config["auth"] = auth
        changed = True

    if changed:
        save_config(config)

    return config


def load_config() -> dict:
    """Load platform config from disk, or return defaults."""
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            config = json.load(f)
        return migrate_config(config)
    return DEFAULT_CONFIG.copy()


def save_config(config: dict):
    """Persist platform config to disk."""
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def update_step_status(step_id: str, status: str, message: str = ""):
    """Update a setup step's status (pending/running/done/error)."""
    config = load_config()
    if step_id in config["setup_steps"]:
        config["setup_steps"][step_id]["status"] = status
        config["setup_steps"][step_id]["message"] = message
    save_config(config)
    return config
