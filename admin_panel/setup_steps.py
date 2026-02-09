"""
Setup step implementations - each step is an async function that runs
shell commands and reports progress via WebSocket.
"""
import asyncio
import os
import subprocess
from pathlib import Path
from config import load_config, save_config, update_step_status, PLATFORM_DIR


async def run_cmd(cmd: str, ws_send=None, env=None) -> tuple[int, str]:
    """Run a shell command, stream output to WebSocket if provided."""
    merged_env = {**os.environ, **(env or {})}
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=merged_env,
    )
    output_lines = []
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        decoded = line.decode("utf-8", errors="replace").rstrip()
        output_lines.append(decoded)
        if ws_send:
            await ws_send(decoded)
    await proc.wait()
    return proc.returncode, "\n".join(output_lines)


# ─── Step 1: System Update ──────────────────────────────────────────────────

async def step_system_update(ws_send=None):
    """Update system packages and install base dependencies."""
    update_step_status("system_update", "running")
    try:
        await run_cmd("apt-get update -y", ws_send)
        await run_cmd("apt-get upgrade -y", ws_send)
        await run_cmd(
            "apt-get install -y build-essential wget curl git "
            "python3-dev python3-pip python3-venv python3-wheel "
            "libxml2-dev libxslt1-dev libldap2-dev libsasl2-dev "
            "libtiff5-dev libjpeg8-dev libopenjp2-7-dev zlib1g-dev "
            "libfreetype6-dev liblcms2-dev libwebp-dev libharfbuzz-dev "
            "libfribidi-dev libxcb1-dev libpq-dev "
            "node-less npm xfonts-75dpi xfonts-base fontconfig",
            ws_send,
        )
        update_step_status("system_update", "done")
        if ws_send:
            await ws_send("✓ System update complete")
    except Exception as e:
        update_step_status("system_update", "error", str(e))
        if ws_send:
            await ws_send(f"✗ Error: {e}")
        raise


# ─── Step 2: PostgreSQL 16 ──────────────────────────────────────────────────

async def step_postgresql(ws_send=None):
    """Install and configure PostgreSQL 16."""
    update_step_status("postgresql", "running")
    try:
        # Add PostgreSQL repo
        await run_cmd(
            "sh -c 'echo \"deb http://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main\" "
            "> /etc/apt/sources.list.d/pgdg.list'",
            ws_send,
        )
        await run_cmd(
            "wget --quiet -O - https://www.postgresql.org/media/keys/ACCC4CF8.asc | apt-key add -",
            ws_send,
        )
        await run_cmd("apt-get update -y", ws_send)
        await run_cmd("apt-get install -y postgresql-16", ws_send)

        # Start and enable
        await run_cmd("systemctl enable postgresql", ws_send)
        await run_cmd("systemctl start postgresql", ws_send)

        # Create odoo superuser role (idempotent)
        await run_cmd(
            'su - postgres -c "psql -tc \\"SELECT 1 FROM pg_roles WHERE rolname=\'odoo\'\\" | grep -q 1 '
            '|| createuser -s odoo"',
            ws_send,
        )

        update_step_status("postgresql", "done")
        if ws_send:
            await ws_send("✓ PostgreSQL 16 installed and configured")
    except Exception as e:
        update_step_status("postgresql", "error", str(e))
        if ws_send:
            await ws_send(f"✗ Error: {e}")
        raise


# ─── Step 3: wkhtmltopdf ────────────────────────────────────────────────────

async def step_wkhtmltopdf(ws_send=None):
    """Install wkhtmltopdf with patched Qt (required by Odoo for PDF reports)."""
    update_step_status("wkhtmltopdf", "running")
    try:
        wk_url = (
            "https://github.com/wkhtmltopdf/packaging/releases/download/"
            "0.12.6.1-3/wkhtmltox_0.12.6.1-3.jammy_amd64.deb"
        )
        await run_cmd(f"wget -q -O /tmp/wkhtmltox.deb {wk_url}", ws_send)
        await run_cmd("apt-get install -y -f /tmp/wkhtmltox.deb", ws_send)
        await run_cmd("rm /tmp/wkhtmltox.deb", ws_send)

        update_step_status("wkhtmltopdf", "done")
        if ws_send:
            await ws_send("✓ wkhtmltopdf installed")
    except Exception as e:
        update_step_status("wkhtmltopdf", "error", str(e))
        if ws_send:
            await ws_send(f"✗ Error: {e}")
        raise


# ─── Step 4: Odoo 19 Source Install ─────────────────────────────────────────

async def step_odoo_source(ws_send=None):
    """Clone Odoo 19 from GitHub and install Python dependencies."""
    update_step_status("odoo_source", "running")
    try:
        odoo_dir = "/opt/odoo"
        odoo_src = f"{odoo_dir}/odoo"
        custom_addons = f"{odoo_dir}/custom-addons"
        venv_dir = f"{odoo_dir}/venv"

        # Create odoo system user (idempotent)
        await run_cmd(
            "id -u odoo &>/dev/null || useradd -m -d /opt/odoo -U -r -s /bin/bash odoo",
            ws_send,
        )

        # Create directories
        await run_cmd(f"mkdir -p {odoo_src} {custom_addons}", ws_send)

        # Clone Odoo source
        if not Path(f"{odoo_src}/.git").exists():
            await run_cmd(
                f"git clone --depth 1 --branch 19.0 https://github.com/odoo/odoo.git {odoo_src}",
                ws_send,
            )
        else:
            await run_cmd(f"cd {odoo_src} && git pull", ws_send)
            if ws_send:
                await ws_send("Odoo source already cloned, pulled latest")

        # Create venv and install deps
        if not Path(venv_dir).exists():
            await run_cmd(f"python3 -m venv {venv_dir}", ws_send)

        await run_cmd(
            f"{venv_dir}/bin/pip install --quiet --upgrade pip wheel",
            ws_send,
        )
        await run_cmd(
            f"{venv_dir}/bin/pip install --quiet -r {odoo_src}/requirements.txt",
            ws_send,
        )

        # Set ownership
        await run_cmd(f"chown -R odoo:odoo {odoo_dir}", ws_send)

        update_step_status("odoo_source", "done")
        if ws_send:
            await ws_send("✓ Odoo 19 source installed")
    except Exception as e:
        update_step_status("odoo_source", "error", str(e))
        if ws_send:
            await ws_send(f"✗ Error: {e}")
        raise


# ─── Step 5: Nginx + Wildcard SSL ───────────────────────────────────────────

async def step_nginx(ws_send=None):
    """Install Nginx and configure wildcard reverse proxy."""
    update_step_status("nginx", "running")
    try:
        config = load_config()
        domain = config.get("domain", "odoo.binaryone.ch")

        await run_cmd("apt-get install -y nginx certbot python3-certbot-nginx", ws_send)

        # Create Nginx config for wildcard routing
        nginx_conf = f"""
# Odoo Platform - Wildcard Reverse Proxy
map $host $odoo_port {{
    ~^(?P<client>[^-]+)-prod\\.{domain.replace('.', '\\.')}$  8069;
    ~^(?P<client>[^-]+)-staging\\.{domain.replace('.', '\\.')}$  8070;
    ~^(?P<client>[^-]+)-dev-(?P<dev>[^.]+)\\.{domain.replace('.', '\\.')}$  8071;
    default  8069;
}}

map $host $odoo_dbfilter {{
    ~^(?P<client>[^-]+)-prod\\.  ^${{client}}_prod$;
    ~^(?P<client>[^-]+)-staging\\.  ^${{client}}_staging$;
    ~^(?P<client>[^-]+)-dev-(?P<dev>[^.]+)\\.  ^${{client}}_dev_${{dev}}$;
    default  .*;
}}

upstream odoo_backend {{
    server 127.0.0.1:8069;
}}

server {{
    listen 80;
    server_name *.{domain};

    # Proxy headers
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    # Odoo longpolling
    location /longpolling {{
        proxy_pass http://127.0.0.1:8072;
    }}

    # Odoo websocket
    location /websocket {{
        proxy_pass http://127.0.0.1:$odoo_port;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }}

    # Main Odoo
    location / {{
        proxy_pass http://127.0.0.1:$odoo_port;
        proxy_read_timeout 720s;
        proxy_connect_timeout 720s;
        proxy_send_timeout 720s;
        client_max_body_size 200m;
    }}
}}

# Admin Panel
server {{
    listen 80;
    server_name admin.{domain};

    location / {{
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }}
}}
"""
        conf_path = "/etc/nginx/sites-available/odoo-platform"
        with open(conf_path, "w") as f:
            f.write(nginx_conf)

        # Enable site
        await run_cmd(
            f"ln -sf {conf_path} /etc/nginx/sites-enabled/odoo-platform",
            ws_send,
        )
        await run_cmd("rm -f /etc/nginx/sites-enabled/default", ws_send)
        await run_cmd("nginx -t", ws_send)
        await run_cmd("systemctl enable nginx", ws_send)
        await run_cmd("systemctl restart nginx", ws_send)

        update_step_status("nginx", "done")
        if ws_send:
            await ws_send("✓ Nginx installed and configured")
            await ws_send(
                "Note: Run certbot manually for wildcard SSL "
                f"(requires DNS challenge for *.{domain})"
            )
    except Exception as e:
        update_step_status("nginx", "error", str(e))
        if ws_send:
            await ws_send(f"✗ Error: {e}")
        raise


# ─── Step 6: Mailpit ────────────────────────────────────────────────────────

async def step_mailpit(ws_send=None):
    """Install Mailpit for catching dev/staging emails."""
    update_step_status("mailpit", "running")
    try:
        # Install Mailpit via official install script
        await run_cmd(
            "bash < <(curl -sL https://raw.githubusercontent.com/axllent/mailpit/develop/install.sh)",
            ws_send,
        )

        # Create systemd service
        mailpit_service = """[Unit]
Description=Mailpit - Email testing tool
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/mailpit --listen 0.0.0.0:8025 --smtp 0.0.0.0:1025
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""
        with open("/etc/systemd/system/mailpit.service", "w") as f:
            f.write(mailpit_service)

        await run_cmd("systemctl daemon-reload", ws_send)
        await run_cmd("systemctl enable mailpit", ws_send)
        await run_cmd("systemctl start mailpit", ws_send)

        update_step_status("mailpit", "done")
        if ws_send:
            await ws_send("✓ Mailpit installed (SMTP :1025, Web UI :8025)")
    except Exception as e:
        update_step_status("mailpit", "error", str(e))
        if ws_send:
            await ws_send(f"✗ Error: {e}")
        raise


# ─── Step 7: First Odoo Instance (prod) ─────────────────────────────────────

async def step_first_instance(ws_send=None, client_name="kaminfeger"):
    """Create the first production Odoo instance."""
    update_step_status("first_instance", "running")
    try:
        await create_odoo_instance(client_name, "prod", 8069, ws_send)
        update_step_status("first_instance", "done")
        if ws_send:
            await ws_send(f"✓ First Odoo instance created: {client_name}_prod on port 8069")
    except Exception as e:
        update_step_status("first_instance", "error", str(e))
        if ws_send:
            await ws_send(f"✗ Error: {e}")
        raise


# ─── Instance Management ────────────────────────────────────────────────────

async def create_odoo_instance(client: str, env: str, port: int, ws_send=None):
    """Create a new Odoo instance with its own systemd service and config."""
    instance_name = f"{client}_{env}"
    db_name = instance_name
    conf_dir = "/etc/odoo"
    log_dir = "/var/log/odoo"
    data_dir = f"/opt/odoo/data/{instance_name}"

    await run_cmd(f"mkdir -p {conf_dir} {log_dir} {data_dir}", ws_send)

    # Determine SMTP settings
    if env == "prod":
        smtp_host = "localhost"
        smtp_port = 25
    else:
        smtp_host = "localhost"
        smtp_port = 1025  # Mailpit

    # Create Odoo config file
    odoo_conf = f"""[options]
admin_passwd = admin
db_host = localhost
db_port = 5432
db_user = odoo
db_password = False
db_name = {db_name}
dbfilter = ^{db_name}$
addons_path = /opt/odoo/odoo/addons,/opt/odoo/custom-addons
data_dir = {data_dir}
logfile = {log_dir}/{instance_name}.log
log_level = info
http_port = {port}
proxy_mode = True
smtp_server = {smtp_host}
smtp_port = {smtp_port}
workers = 2
max_cron_threads = 1
limit_memory_hard = 2684354560
limit_memory_soft = 2147483648
limit_time_cpu = 600
limit_time_real = 1200
"""
    conf_path = f"{conf_dir}/{instance_name}.conf"
    with open(conf_path, "w") as f:
        f.write(odoo_conf)

    # Create systemd service
    service_content = f"""[Unit]
Description=Odoo {instance_name}
After=network.target postgresql.service

[Service]
Type=simple
User=odoo
Group=odoo
ExecStart=/opt/odoo/venv/bin/python3 /opt/odoo/odoo/odoo-bin -c {conf_path}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""
    service_path = f"/etc/systemd/system/odoo-{instance_name}.service"
    with open(service_path, "w") as f:
        f.write(service_content)

    # Create database
    await run_cmd(
        f'su - postgres -c "psql -tc \\"SELECT 1 FROM pg_database WHERE datname=\'{db_name}\'\\" '
        f'| grep -q 1 || createdb -O odoo {db_name}"',
        ws_send,
    )

    # Enable and start
    await run_cmd("systemctl daemon-reload", ws_send)
    await run_cmd(f"systemctl enable odoo-{instance_name}", ws_send)
    await run_cmd(f"systemctl start odoo-{instance_name}", ws_send)

    # Update config
    config = load_config()
    config["instances"][instance_name] = {
        "client": client,
        "env": env,
        "port": port,
        "db_name": db_name,
        "service": f"odoo-{instance_name}",
        "conf": conf_path,
    }
    save_config(config)

    if ws_send:
        await ws_send(f"Instance {instance_name} created on port {port}")


async def delete_odoo_instance(instance_name: str, ws_send=None):
    """Stop and remove an Odoo instance."""
    config = load_config()
    instance = config["instances"].get(instance_name)
    if not instance:
        raise ValueError(f"Instance {instance_name} not found")

    service = instance["service"]
    await run_cmd(f"systemctl stop {service}", ws_send)
    await run_cmd(f"systemctl disable {service}", ws_send)
    await run_cmd(f"rm -f /etc/systemd/system/{service}.service", ws_send)
    await run_cmd(f"rm -f {instance['conf']}", ws_send)
    await run_cmd("systemctl daemon-reload", ws_send)

    # Drop database
    db_name = instance["db_name"]
    await run_cmd(
        f'su - postgres -c "dropdb --if-exists {db_name}"',
        ws_send,
    )

    del config["instances"][instance_name]
    save_config(config)

    if ws_send:
        await ws_send(f"Instance {instance_name} deleted")


# Step registry
SETUP_STEPS = {
    "system_update": step_system_update,
    "postgresql": step_postgresql,
    "wkhtmltopdf": step_wkhtmltopdf,
    "odoo_source": step_odoo_source,
    "nginx": step_nginx,
    "mailpit": step_mailpit,
    "first_instance": step_first_instance,
}
