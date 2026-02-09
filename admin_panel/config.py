"""
Platform configuration and state management.
"""
import json
import os
from pathlib import Path

PLATFORM_DIR = Path(os.environ.get("PLATFORM_DIR", "/root/odoo-platform"))
CONFIG_FILE = PLATFORM_DIR / "platform.json"

# Defaults
DEFAULT_CONFIG = {
    "domain": "odoo.binaryone.ch",
    "letsencrypt_email": "",
    "odoo_version": "19.0",
    "pg_version": "16",
    "setup_steps": {
        "system_update": {"status": "pending", "label": "System Update & Pakete"},
        "postgresql": {"status": "pending", "label": "PostgreSQL 16"},
        "wkhtmltopdf": {"status": "pending", "label": "wkhtmltopdf"},
        "odoo_source": {"status": "pending", "label": "Odoo 19 Source-Install"},
        "nginx": {"status": "pending", "label": "Nginx + Wildcard SSL"},
        "mailpit": {"status": "pending", "label": "Mailpit"},
        "first_instance": {"status": "pending", "label": "Erste Odoo-Instanz (prod)"},
    },
    "instances": {},
    "clients": {},
}


def load_config() -> dict:
    """Load platform config from disk, or return defaults."""
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return json.load(f)
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
        if message:
            config["setup_steps"][step_id]["message"] = message
    save_config(config)
    return config
