"""
Setup step implementations - each step is an async function that runs
shell commands and reports progress via WebSocket.
"""
import asyncio
import json as _json
import os
import re
import secrets
import shutil
import string
import subprocess
import tempfile
import zipfile
from datetime import datetime
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
    output = "\n".join(output_lines)
    if proc.returncode != 0:
        raise RuntimeError(f"Command failed (exit {proc.returncode}): {cmd}")
    return proc.returncode, output


# ─── Step 1: System Update ──────────────────────────────────────────────────

async def step_system_update(ws_send=None):
    """Update system packages, install base dependencies, fonts, and wkhtmltopdf."""
    update_step_status("system_update", "running")
    try:
        await run_cmd("apt-get update -y", ws_send)
        await run_cmd("apt-get upgrade -y", ws_send)
        await run_cmd(
            "apt-get install -y build-essential wget curl git rsync "
            "python3-dev python3-pip python3-venv python3-wheel "
            "libxml2-dev libxslt1-dev libldap2-dev libsasl2-dev "
            "libtiff5-dev libjpeg8-dev libopenjp2-7-dev zlib1g-dev "
            "libfreetype6-dev liblcms2-dev libwebp-dev libharfbuzz-dev "
            "libfribidi-dev libxcb1-dev libpq-dev libcairo2-dev "
            "xfonts-75dpi xfonts-base fontconfig "
            "fonts-dejavu-core fonts-freefont-ttf fonts-noto-core "
            "fonts-font-awesome fonts-inconsolata fonts-roboto-unhinted gsfonts",
            ws_send,
        )

        # wkhtmltopdf with patched Qt (required by Odoo for PDF reports)
        if ws_send:
            await ws_send("Installing wkhtmltopdf...")
        wk_url = (
            "https://github.com/wkhtmltopdf/packaging/releases/download/"
            "0.12.6.1-3/wkhtmltox_0.12.6.1-3.jammy_amd64.deb"
        )
        await run_cmd(f"wget -q -O /tmp/wkhtmltox.deb {wk_url}", ws_send)
        await run_cmd("apt-get install -y -f /tmp/wkhtmltox.deb", ws_send)
        await run_cmd("rm -f /tmp/wkhtmltox.deb", ws_send)

        update_step_status("system_update", "done")
        if ws_send:
            await ws_send("OK System update complete")
    except Exception as e:
        update_step_status("system_update", "error", str(e))
        if ws_send:
            await ws_send(f"Error: {e}")
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
            await ws_send("OK PostgreSQL 16 installed and configured")
    except Exception as e:
        update_step_status("postgresql", "error", str(e))
        if ws_send:
            await ws_send(f"Error: {e}")
        raise


# ─── Step 3/4: Odoo Version Source Install ──────────────────────────────────

def odoo_base_dir(version: str) -> str:
    """Return the base directory for a given Odoo version, e.g. /opt/odoo19."""
    return f"/opt/odoo{version}"


def get_instance_owner(instance: dict) -> str:
    """Return the filesystem owner for an instance's addons directory.

    Dev instances have an ssh_user (e.g. dev-adrian), others use 'odoo'.
    """
    return instance.get("ssh_user", "odoo")


async def fix_addons_ownership(instance_name: str, config: dict = None, ws_send=None):
    """Set correct ownership on an instance's addons directory.

    Resolves the right owner (ssh_user for dev instances, odoo otherwise)
    and recursively chowns the addons directory.
    """
    if config is None:
        config = load_config()
    instance = config.get("instances", {}).get(instance_name)
    if not instance:
        return
    version = instance.get("version", "19")
    owner = get_instance_owner(instance)
    addons_dir = f"{odoo_base_dir(version)}/data/{instance_name}/addons"
    await run_cmd(f"chown -R {owner}:odoo {addons_dir}", ws_send)


async def step_odoo_version(version: str, ws_send=None):
    """Clone Odoo Community + Enterprise for a specific version and install Python dependencies."""
    step_id = f"odoo_{version}"
    branch = f"{version}.0"
    update_step_status(step_id, "running")
    try:
        config = load_config()
        github_token = config.get("github_token", "")
        if not github_token:
            raise RuntimeError("GitHub token is required for Enterprise repo. Set it in the config above.")

        base = odoo_base_dir(version)
        odoo_src = f"{base}/odoo"
        enterprise_src = f"{base}/enterprise"
        venv_dir = f"{base}/venv"

        community_url = "https://github.com/odoo/odoo.git"
        enterprise_url = f"https://{github_token}@github.com/odoo/enterprise.git"

        # One-time migration: move /opt/odoo -> /opt/odoo19 if old layout exists
        if version == "19":
            old_dir = Path("/opt/odoo")
            new_dir = Path(base)
            if (old_dir / "odoo" / "odoo-bin").exists() and not (new_dir / "odoo" / "odoo-bin").exists():
                if ws_send:
                    await ws_send(f"Migrating /opt/odoo -> {base}...")
                await run_cmd(f"mv /opt/odoo {base}", ws_send)

        # Create odoo system user (idempotent)
        await run_cmd(
            "id -u odoo >/dev/null 2>&1 || useradd -m -d /opt/odoo-home -U -r -s /bin/bash odoo",
            ws_send,
        )
        await run_cmd("mkdir -p /opt/odoo-home && chown odoo:odoo /opt/odoo-home", ws_send)
        await run_cmd(
            "sudo -u odoo git config --global --add safe.directory '*'",
            ws_send,
        )

        # Create directories
        await run_cmd(f"mkdir -p {odoo_src} {enterprise_src} {base}/data", ws_send)

        # Clone Odoo Community
        if ws_send:
            await ws_send(f"Cloning Odoo {version} Community...")
        if not Path(f"{odoo_src}/.git").exists():
            await run_cmd(
                f"git clone --depth 1 --branch {branch} {community_url} {odoo_src}",
                ws_send,
            )
        else:
            await run_cmd(f"cd {odoo_src} && git pull", ws_send)
            if ws_send:
                await ws_send("Community already cloned, pulled latest")

        # Clone Odoo Enterprise
        if ws_send:
            await ws_send(f"Cloning Odoo {version} Enterprise...")
        if not Path(f"{enterprise_src}/.git").exists():
            await run_cmd(
                f"git clone --depth 1 --branch {branch} {enterprise_url} {enterprise_src}",
                ws_send,
            )
        else:
            await run_cmd(f"cd {enterprise_src} && git pull", ws_send)
            if ws_send:
                await ws_send("Enterprise already cloned, pulled latest")

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
        # rl-renderPM is needed by reportlab 4.x for PDF rendering (barcodes, images)
        # but Odoo's requirements.txt only includes it for Windows, expecting the deb
        # package python3-renderpm on Linux — which doesn't exist in a venv setup.
        await run_cmd(
            f"{venv_dir}/bin/pip install --quiet rl-renderPM",
            ws_send,
        )

        # Set ownership
        await run_cmd(f"chown -R odoo:odoo {base}", ws_send)

        update_step_status(step_id, "done")
        if ws_send:
            await ws_send(f"OK Odoo {version} Community + Enterprise installed")
    except Exception as e:
        update_step_status(step_id, "error", str(e))
        if ws_send:
            await ws_send(f"Error: {e}")
        raise


async def step_odoo_19(ws_send=None):
    await step_odoo_version("19", ws_send)


async def step_odoo_18(ws_send=None):
    await step_odoo_version("18", ws_send)


# ─── Step 6: Nginx Reverse Proxy ───────────────────────────────────────────

async def step_nginx(ws_send=None):
    """Install Nginx and configure reverse proxy with per-instance config files."""
    update_step_status("nginx", "running")
    try:
        await run_cmd("apt-get install -y nginx certbot python3-certbot-nginx", ws_send)

        # Main config: admin, mailpit, default server + include for instance configs
        nginx_conf = """# Odoo Platform - Reverse Proxy
# Per-instance configs are in /etc/nginx/odoo-instances/*.conf

include /etc/nginx/odoo-instances/*.conf;

# Admin Panel
server {
    listen 80;
    server_name ~^admin\\.;

    client_max_body_size 2g;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 600s;
    }
}

# Mailpit web UI
server {
    listen 80;
    server_name ~^mailpit\\.;

    location / {
        proxy_pass http://127.0.0.1:8025;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}

# Default: bare IP or unknown subdomain
server {
    listen 80 default_server;
    server_name _;

    location / {
        default_type text/plain;
        return 200 "Odoo Platform is running. Configure DNS: *.your-domain -> this IP\\n";
    }
}
"""
        # Create instance config directory
        await run_cmd(f"mkdir -p {NGINX_INSTANCE_DIR}", ws_send)

        # Write main config
        conf_path = "/etc/nginx/sites-available/odoo-platform"
        with open(conf_path, "w") as f:
            f.write(nginx_conf)
        if ws_send:
            await ws_send("Nginx main config written")

        # Write per-instance configs for any existing instances
        config = load_config()
        domain = config.get("domain", "")
        for name, inst in config.get("instances", {}).items():
            await write_instance_nginx_conf(
                name, inst["client"], inst["env"], inst["port"], domain, ws_send
            )

        # Enable site
        await run_cmd(
            f"ln -sf {conf_path} /etc/nginx/sites-enabled/odoo-platform",
            ws_send,
        )
        await run_cmd("rm -f /etc/nginx/sites-enabled/default", ws_send)
        await run_cmd("nginx -t", ws_send)
        await run_cmd("systemctl enable nginx", ws_send)
        await run_cmd("systemctl reload nginx || systemctl start nginx", ws_send)

        update_step_status("nginx", "done")
        if ws_send:
            await ws_send("OK Nginx installed and configured")
    except Exception as e:
        update_step_status("nginx", "error", str(e))
        if ws_send:
            await ws_send(f"Error: {e}")
        raise


# ─── Step 7: Mailpit ────────────────────────────────────────────────────────

async def step_mailpit(ws_send=None):
    """Install Mailpit for catching dev/staging emails."""
    update_step_status("mailpit", "running")
    try:
        # Install Mailpit via official install script
        await run_cmd(
            "curl -sL https://raw.githubusercontent.com/axllent/mailpit/develop/install.sh | bash",
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
            await ws_send("OK Mailpit installed (SMTP :1025, Web UI :8025)")
    except Exception as e:
        update_step_status("mailpit", "error", str(e))
        if ws_send:
            await ws_send(f"Error: {e}")
        raise


# ─── Step 8: DNS Check ─────────────────────────────────────────────────────

async def step_dns_check(ws_send=None):
    """Verify that *.domain resolves to this server's public IP."""
    update_step_status("dns_check", "running")
    try:
        config = load_config()
        domain = config.get("domain", "")
        if not domain:
            raise RuntimeError("No domain configured. Set it in the config above.")

        # Detect public IP
        public_ip = None
        for url in ["https://api.ipify.org", "https://icanhazip.com", "https://ifconfig.me"]:
            try:
                result = await asyncio.create_subprocess_shell(
                    f"curl -s --max-time 3 {url}",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await result.communicate()
                ip = stdout.decode().strip()
                if ip and not ip.startswith("<"):
                    public_ip = ip
                    break
            except Exception:
                continue
        if not public_ip:
            raise RuntimeError("Could not detect server public IP.")

        if ws_send:
            await ws_send(f"Server public IP: {public_ip}")

        # Resolve admin.{domain}
        check_host = f"admin.{domain}"
        if ws_send:
            await ws_send(f"Resolving {check_host}...")

        proc = await asyncio.create_subprocess_shell(
            f"host {check_host}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate()
        output = stdout.decode().strip()
        if ws_send:
            await ws_send(output)

        if public_ip in output:
            update_step_status("dns_check", "done")
            if ws_send:
                await ws_send(f"OK {check_host} resolves to {public_ip}")
        else:
            raise RuntimeError(
                f"{check_host} does not resolve to {public_ip}. "
                f"Add a wildcard A record: *.{domain} -> {public_ip}"
            )
    except Exception as e:
        update_step_status("dns_check", "error", str(e))
        if ws_send:
            await ws_send(f"Error: {e}")
        raise


# ─── Step 9: SSL Certificates ─────────────────────────────────────────────

async def step_ssl_certs(ws_send=None):
    """Issue Let's Encrypt SSL certificates for admin, mailpit, and all instances."""
    update_step_status("ssl_certs", "running")
    try:
        config = load_config()
        domain = config.get("domain", "")
        if not domain:
            raise RuntimeError("No domain configured. Set it in the config above.")

        await run_cmd("apt-get install -y certbot python3-certbot-nginx", ws_send)

        # Collect all FQDNs: admin, mailpit, plus all instance subdomains
        fqdns = [f"admin.{domain}", f"mailpit.{domain}"]
        for name, inst in config.get("instances", {}).items():
            env_prefix = inst["env"].replace("_", "-")
            fqdns.append(f"{inst['client']}-{env_prefix}.{domain}")

        errors = []
        for fqdn in fqdns:
            if ws_send:
                await ws_send(f"Requesting certificate for {fqdn}...")
            try:
                await run_cmd(
                    f"certbot --nginx -d {fqdn} --non-interactive "
                    f"--agree-tos --register-unsafely-without-email "
                    f"--keep-until-expiring",
                    ws_send,
                )
                if ws_send:
                    await ws_send(f"OK SSL certificate issued for {fqdn}")
            except Exception as e:
                errors.append(f"{fqdn}: {e}")
                if ws_send:
                    await ws_send(f"Warning: SSL for {fqdn} failed: {e}")

        if errors:
            raise RuntimeError("; ".join(errors))

        update_step_status("ssl_certs", "done")
        if ws_send:
            await ws_send("OK SSL certificates issued")
    except Exception as e:
        update_step_status("ssl_certs", "error", str(e))
        if ws_send:
            await ws_send(f"Error: {e}")
        raise


# ─── Nginx Per-Instance Config ─────────────────────────────────────────────

NGINX_INSTANCE_DIR = "/etc/nginx/odoo-instances"


def _instance_nginx_conf(instance_name: str, client: str, env: str, port: int, domain: str) -> str:
    """Build a single Nginx server block for an Odoo instance."""
    env_prefix = env.replace("_", "-")
    fqdn = f"{client}-{env_prefix}.{domain}"
    return f"""# {instance_name}
server {{
    listen 80;
    server_name {fqdn};

    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    location /websocket {{
        proxy_pass http://127.0.0.1:{port + 1};
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }}

    location / {{
        proxy_pass http://127.0.0.1:{port};
        proxy_read_timeout 720s;
        proxy_connect_timeout 720s;
        proxy_send_timeout 720s;
        client_max_body_size 200m;
    }}
}}
"""


async def write_instance_nginx_conf(instance_name: str, client: str, env: str, port: int, domain: str, ws_send=None):
    """Write a per-instance Nginx config file."""
    await run_cmd(f"mkdir -p {NGINX_INSTANCE_DIR}", ws_send)
    conf_path = f"{NGINX_INSTANCE_DIR}/{instance_name}.conf"
    content = _instance_nginx_conf(instance_name, client, env, port, domain)
    with open(conf_path, "w") as f:
        f.write(content)
    if ws_send:
        await ws_send(f"Nginx config written: {conf_path}")


async def remove_instance_nginx_conf(instance_name: str, ws_send=None):
    """Remove a per-instance Nginx config file."""
    conf_path = f"{NGINX_INSTANCE_DIR}/{instance_name}.conf"
    if Path(conf_path).exists():
        Path(conf_path).unlink()
        if ws_send:
            await ws_send(f"Nginx config removed: {conf_path}")


async def reload_nginx(ws_send=None):
    """Test and reload Nginx configuration."""
    await run_cmd("nginx -t", ws_send)
    await run_cmd("systemctl reload nginx", ws_send)


# ─── Instance Management ────────────────────────────────────────────────────

def _generate_password(length=16):
    """Generate a random alphanumeric password."""
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))


_NAME_RE = re.compile(r'^[a-z]+(-[a-z]+)*$')


async def _rebuild_dev_sudoers(ssh_user: str, current_instance: str, current_version: str, config: dict, ws_send=None, exclude_instance: str = None):
    """Rebuild sudoers for a dev user, covering ALL their instances."""
    sudoers_file = f"/etc/sudoers.d/{ssh_user}"
    sudoers_lines = []
    odoo_bin_versions = set()

    for inst_name, inst in config.get("instances", {}).items():
        if inst_name == exclude_instance:
            continue
        if inst.get("ssh_user") != ssh_user:
            continue
        service = inst["service"]
        ver = inst.get("version", "19")
        sudoers_lines.append(f"{ssh_user} ALL=(ALL) NOPASSWD: /usr/bin/systemctl start {service}")
        sudoers_lines.append(f"{ssh_user} ALL=(ALL) NOPASSWD: /usr/bin/systemctl stop {service}")
        sudoers_lines.append(f"{ssh_user} ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart {service}")
        odoo_bin_versions.add(ver)

    # Include the current instance being created (not yet in config)
    if current_instance and current_instance not in config.get("instances", {}):
        service = f"odoo-{current_instance}"
        sudoers_lines.append(f"{ssh_user} ALL=(ALL) NOPASSWD: /usr/bin/systemctl start {service}")
        sudoers_lines.append(f"{ssh_user} ALL=(ALL) NOPASSWD: /usr/bin/systemctl stop {service}")
        sudoers_lines.append(f"{ssh_user} ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart {service}")
        odoo_bin_versions.add(current_version)

    for ver in sorted(odoo_bin_versions):
        base = odoo_base_dir(ver)
        odoo_bin = f"{base}/venv/bin/python3 {base}/odoo/odoo-bin"
        sudoers_lines.append(f"{ssh_user} ALL=(odoo) NOPASSWD: {odoo_bin} *")

    if sudoers_lines:
        with open(sudoers_file, "w") as f:
            f.write("\n".join(sudoers_lines) + "\n")
        await run_cmd(f"chmod 440 {sudoers_file}", ws_send)
    else:
        Path(sudoers_file).unlink(missing_ok=True)


async def create_odoo_instance(client: str, env: str, port: int, workers: int = 2, ws_send=None, version: str = "19"):
    """Create a new Odoo instance with its own systemd service and config."""
    if not _NAME_RE.match(client):
        raise ValueError(f"Invalid client name: {client} (lowercase letters and hyphens only)")

    base = odoo_base_dir(version)
    instance_name = f"{client}_{env}"
    db_name = instance_name
    conf_dir = "/etc/odoo"
    log_dir = "/var/log/odoo"
    data_dir = f"{base}/data/{instance_name}"
    instance_addons = f"{data_dir}/addons"
    master_pw = _generate_password()
    admin_pw = _generate_password()

    await run_cmd(f"mkdir -p {conf_dir} {log_dir} {data_dir} {data_dir}/sessions {data_dir}/filestore", ws_send)
    await run_cmd(f"chown odoo:odoo {data_dir} {data_dir}/sessions {data_dir}/filestore {log_dir}", ws_send)

    # Clone custom addons from local bare repo
    bare_repo = f"/opt/git/odoo{version}-addons.git"
    if Path(bare_repo).exists():
        if ws_send:
            await ws_send("Cloning addons from local git repo...")
        await run_cmd(f"git clone {bare_repo} {instance_addons}", ws_send)
        await run_cmd(f"chown -R odoo:odoo {instance_addons}", ws_send)
        await run_cmd(f"sudo -u odoo git -C {instance_addons} config user.name Deploy", ws_send)
        await run_cmd(f"sudo -u odoo git -C {instance_addons} config user.email deploy@odoo-platform", ws_send)
    else:
        await run_cmd(f"mkdir -p {instance_addons}", ws_send)
        await run_cmd(f"chown odoo:odoo {instance_addons}", ws_send)

    # Generate instance-specific CLAUDE.md in data dir (parent of addons git repo)
    conf_path = f"{conf_dir}/{instance_name}.conf"
    service = f"odoo-{instance_name}"
    odoo_bin = f"{base}/venv/bin/python3 {base}/odoo/odoo-bin"
    claude_md = f"""# {instance_name} — Instance Context

You are working on the **{instance_name}** Odoo instance.

- Instance name: `{instance_name}`
- Service: `{service}`
- Database: `{db_name}`
- Config: `{conf_path}`

## Quick Commands

```bash
# Restart
sudo systemctl restart {service}

# Logs
journalctl -u {service} -f

# Upgrade a module
sudo systemctl stop {service}
sudo -u odoo {odoo_bin} -c {conf_path} -d {db_name} -u <module> --stop-after-init --logfile=
sudo systemctl start {service}

# Install a module
sudo systemctl stop {service}
sudo -u odoo {odoo_bin} -c {conf_path} -d {db_name} -i <module> --stop-after-init --logfile=
sudo systemctl start {service}
```
"""
    claude_md_path = f"{data_dir}/CLAUDE.md"
    Path(claude_md_path).write_text(claude_md)
    await run_cmd(f"chown odoo:odoo {claude_md_path}", ws_send)

    # Determine SMTP settings
    if env == "prod":
        smtp_host = "localhost"
        smtp_port = 25
    else:
        smtp_host = "localhost"
        smtp_port = 1025  # Mailpit

    # Create Odoo config file
    odoo_conf = f"""[options]
admin_passwd = {master_pw}
db_host = False
db_port = False
db_user = odoo
db_password = False
db_name = {db_name}
dbfilter = ^{db_name}$
addons_path = {base}/enterprise,{base}/odoo/addons,{instance_addons}
data_dir = {data_dir}
logfile = {log_dir}/{instance_name}.log
log_level = info
http_port = {port}
gevent_port = {port + 1}
proxy_mode = True
smtp_server = {smtp_host}
smtp_port = {smtp_port}
workers = {workers}
max_cron_threads = {1 if workers > 0 else 0}
limit_memory_hard = 2684354560
limit_memory_soft = 2147483648
limit_time_cpu = 600
limit_time_real = 1200
"""
    with open(conf_path, "w") as f:
        f.write(odoo_conf)

    # Create systemd service
    dev_flag = " --dev=xml,reload" if workers == 0 else ""
    service_content = f"""[Unit]
Description=Odoo {instance_name}
After=network.target postgresql.service

[Service]
Type=simple
User=odoo
Group=odoo
ExecStart={base}/venv/bin/python3 {base}/odoo/odoo-bin -c {conf_path}{dev_flag}
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

    # Initialize Odoo if database is empty (no ir_module_module table)
    _, check_out = await run_cmd(
        f"su - postgres -c \"psql -d {db_name} -tc \\\"SELECT 1 FROM information_schema.tables "
        f"WHERE table_name='ir_module_module'\\\"\"",
        ws_send,
    )
    if "1" not in check_out:
        if ws_send:
            await ws_send("Initializing Odoo database (this may take a minute)...")
        await run_cmd(
            f'su - odoo -s /bin/bash -c "'
            f'{base}/venv/bin/python3 {base}/odoo/odoo-bin '
            f'-c {conf_path} -i base --stop-after-init"',
            ws_send,
        )
    else:
        if ws_send:
            await ws_send("Database already initialized, skipping")

    # Set admin user password and normalize superuser
    if ws_send:
        await ws_send("Setting admin user password...")
    await run_cmd(
        f"su - postgres -c \"psql -d {db_name} -c \\\"UPDATE res_users SET password='{admin_pw}', login='admin' WHERE id=2\\\"\"",
        ws_send,
    )
    await run_cmd(
        f"su - postgres -c \"psql -d {db_name} -c \\\"UPDATE res_partner SET name='Admin' WHERE id=(SELECT partner_id FROM res_users WHERE id=2)\\\"\"",
        ws_send,
    )

    # Neutralize dev/staging instances
    if env != "prod":
        if ws_send:
            await ws_send("Neutralizing database...")
        from neutralize import neutralize_db
        neutralize_db(db_name)

    # Enable and start
    await run_cmd("systemctl daemon-reload", ws_send)
    await run_cmd(f"systemctl enable odoo-{instance_name}", ws_send)
    await run_cmd(f"systemctl start odoo-{instance_name}", ws_send)

    # Set up SSH access for dev instances
    ssh_user = None
    if env.startswith("dev"):
        dev_name = env.split("_", 1)[1] if "_" in env else env
        ssh_user = f"{client}-dev-{dev_name}"
        if ws_send:
            await ws_send(f"Setting up SSH access ({ssh_user})...")
        await run_cmd(
            f"id {ssh_user} >/dev/null 2>&1 || useradd -r -d {data_dir} -s /bin/bash -G odoo {ssh_user}",
            ws_send,
        )
        await run_cmd(f"chown {ssh_user}:odoo {data_dir}", ws_send)
        await run_cmd(f"chmod 755 {data_dir}", ws_send)
        await run_cmd(f"chown -R {ssh_user}:odoo {instance_addons}", ws_send)
        await run_cmd(f"chmod -R g+ws {instance_addons}", ws_send)
        # Configure git identity for dev user
        await run_cmd(f"sudo -u {ssh_user} git -C {instance_addons} config user.name {dev_name.capitalize()}", ws_send)
        await run_cmd(f"sudo -u {ssh_user} git -C {instance_addons} config user.email {dev_name}@odoo-platform", ws_send)
        await run_cmd(f"sudo -u {ssh_user} git config -f {data_dir}/.gitconfig --add safe.directory '*'", ws_send)
        await run_cmd(f"chown {ssh_user}:odoo {data_dir}/.gitconfig", ws_send)
        await run_cmd(f"mkdir -p {data_dir}/.ssh", ws_send)
        await run_cmd(f"touch {data_dir}/.ssh/authorized_keys", ws_send)
        await run_cmd(f"chown -R {ssh_user}:{ssh_user} {data_dir}/.ssh", ws_send)
        await run_cmd(f"chmod 700 {data_dir}/.ssh && chmod 600 {data_dir}/.ssh/authorized_keys", ws_send)

        # Grant limited sudo: service control + odoo-bin for module upgrades
        await run_cmd(f"usermod -aG systemd-journal {ssh_user}", ws_send)

    # Update config
    config = load_config()
    inst_config = {
        "client": client,
        "env": env,
        "version": version,
        "port": port,
        "workers": workers,
        "master_pw": master_pw,
        "admin_pw": admin_pw,
        "db_name": db_name,
        "service": f"odoo-{instance_name}",
        "conf": conf_path,
    }
    if ssh_user:
        inst_config["ssh_user"] = ssh_user
    config["instances"][instance_name] = inst_config
    save_config(config)

    # Rebuild sudoers after config is saved (needs all instances in config)
    if ssh_user:
        await _rebuild_dev_sudoers(ssh_user, None, None, config, ws_send)

    # Nginx per-instance config + SSL
    domain = config.get("domain", "")
    if domain:
        await write_instance_nginx_conf(instance_name, client, env, port, domain, ws_send)
        await reload_nginx(ws_send)

        env_prefix = env.replace("_", "-")
        fqdn = f"{client}-{env_prefix}.{domain}"
        try:
            await run_cmd(
                f"certbot --nginx -d {fqdn} --non-interactive "
                f"--agree-tos --register-unsafely-without-email "
                f"--keep-until-expiring",
                ws_send,
            )
        except Exception:
            if ws_send:
                await ws_send(f"SSL for {fqdn} skipped (DNS not ready?)")

    if ws_send:
        await ws_send(f"Instance {instance_name} created on port {port}")


async def delete_odoo_instance(instance_name: str, ws_send=None):
    """Stop and remove an Odoo instance."""
    config = load_config()
    instance = config["instances"].get(instance_name)
    if not instance:
        raise ValueError(f"Instance {instance_name} not found")

    version = instance.get("version", "19")
    base = odoo_base_dir(version)

    async def _safe(cmd, label=None):
        """Run a cleanup command, log but don't abort on failure."""
        try:
            await run_cmd(cmd, ws_send)
        except Exception as e:
            msg = f"Warning: {label or cmd} — {e}"
            if ws_send:
                await ws_send(msg)

    service = instance["service"]
    await _safe(f"systemctl stop {service}", "stop service")
    await _safe(f"systemctl disable {service}", "disable service")
    await _safe(f"rm -f /etc/systemd/system/{service}.service", "remove service file")
    await _safe(f"rm -f {instance['conf']}", "remove config")
    await _safe("systemctl daemon-reload", "daemon-reload")

    # Drop database
    db_name = instance["db_name"]
    await _safe(f'su - postgres -c "dropdb --if-exists {db_name}"', "drop database")

    # Remove SSH user and sudoers if exists
    ssh_user = instance.get("ssh_user")
    if ssh_user:
        # Check if this user has other instances before removing
        other_instances = [n for n, i in config.get("instances", {}).items()
                          if n != instance_name and i.get("ssh_user") == ssh_user]
        if other_instances:
            # Rebuild sudoers without the deleted instance
            await _rebuild_dev_sudoers(ssh_user, None, None, config, ws_send, exclude_instance=instance_name)
        else:
            await _safe(f"rm -f /etc/sudoers.d/{ssh_user}", "remove sudoers")
            await _safe(f"userdel {ssh_user} 2>/dev/null || true", "remove user")

    # Clean up data directory
    data_dir = f"{base}/data/{instance_name}"
    await _safe(f"rm -rf {data_dir}", "remove data directory")

    # Remove Nginx config
    try:
        await remove_instance_nginx_conf(instance_name, ws_send)
    except Exception:
        pass
    await _safe("nginx -t && systemctl reload nginx", "reload nginx")

    del config["instances"][instance_name]
    save_config(config)

    if ws_send:
        await ws_send(f"Instance {instance_name} deleted")


async def sync_instance_from_prod(instance_name: str, ws_send=None):
    """Sync a staging/dev instance from its client's prod database and filestore."""
    config = load_config()
    target = config["instances"].get(instance_name)
    if not target:
        raise ValueError(f"Instance {instance_name} not found")

    if target["env"] == "prod":
        raise ValueError("Cannot sync a prod instance from itself")

    client = target["client"]
    prod_name = f"{client}_prod"
    prod = config["instances"].get(prod_name)
    if not prod:
        raise ValueError(f"No prod instance found for client {client}")

    target_version = target.get("version", "19")
    prod_version = prod.get("version", "19")
    if target_version != prod_version:
        raise ValueError(f"Version mismatch: {instance_name} is v{target_version}, {prod_name} is v{prod_version}")

    base = odoo_base_dir(target_version)
    target_db = target["db_name"]
    target_service = target["service"]
    target_conf = target["conf"]
    prod_db = prod["db_name"]
    target_data_dir = f"{base}/data/{instance_name}"
    prod_data_dir = f"{base}/data/{prod_name}"

    # Stop target instance
    if ws_send:
        await ws_send(f"Stopping {target_service}...")
    await run_cmd(f"systemctl stop {target_service}", ws_send)

    # Drop target database
    if ws_send:
        await ws_send(f"Dropping database {target_db}...")
    await run_cmd(
        f'su - postgres -c "dropdb --if-exists {target_db}"',
        ws_send,
    )

    # Terminate active connections to prod DB so createdb -T works
    if ws_send:
        await ws_send(f"Preparing to clone {prod_db}...")
    await run_cmd(
        f"su - postgres -c \"psql -c \\\"SELECT pg_terminate_backend(pid) "
        f"FROM pg_stat_activity WHERE datname='{prod_db}' "
        f"AND pid <> pg_backend_pid()\\\"\"",
        ws_send,
    )

    # Clone prod database
    if ws_send:
        await ws_send(f"Cloning {prod_db} -> {target_db}...")
    await run_cmd(
        f'su - postgres -c "createdb -O odoo -T {prod_db} {target_db}"',
        ws_send,
    )

    # Copy filestore
    prod_filestore = f"{prod_data_dir}/filestore/{prod_db}"
    target_filestore = f"{target_data_dir}/filestore/{target_db}"
    if Path(prod_filestore).exists():
        if ws_send:
            await ws_send(f"Copying filestore from {prod_name}...")
        await run_cmd(f"rm -rf {target_filestore}", ws_send)
        await run_cmd(f"mkdir -p {target_data_dir}/filestore", ws_send)
        await run_cmd(f"cp -a {prod_filestore} {target_filestore}", ws_send)
        await run_cmd(f"chown -R odoo:odoo {target_data_dir}/filestore", ws_send)
    else:
        if ws_send:
            await ws_send("No filestore to copy (prod filestore not found)")

    # Pull latest addons from git repo
    target_addons = f"{target_data_dir}/addons"
    if Path(f"{target_addons}/.git").exists():
        if ws_send:
            await ws_send("Pulling latest addons from git repo...")
        owner = get_instance_owner(config["instances"].get(instance_name, {}))
        await run_cmd(f"sudo -u {owner} git -C {target_addons} fetch origin", ws_send)
        await run_cmd(f"sudo -u {owner} git -C {target_addons} reset --hard origin/main", ws_send)

    # Neutralize the cloned database
    if ws_send:
        await ws_send("Neutralizing database...")
    from neutralize import neutralize_db
    stats = neutralize_db(target_db)
    if ws_send:
        await ws_send(f"Neutralized: {stats['crons']} crons disabled, "
                      f"{stats['mail_servers']} mail servers disabled")

    # Check/reset admin password after clone
    from admin_pw import verify_admin_pw, reset_admin_pw, normalize_superuser
    stored_pw = target.get("admin_pw", "")
    if stored_pw and verify_admin_pw(target_db, stored_pw):
        if ws_send:
            await ws_send("Admin password matches — keeping existing password")
        normalize_superuser(target_db)
    else:
        new_pw = _generate_password()
        if ws_send:
            await ws_send("Admin password mismatch — setting new password")
        reset_admin_pw(target_db, new_pw)
        config = load_config()
        config["instances"][instance_name]["admin_pw"] = new_pw
        save_config(config)
    if ws_send:
        await ws_send("Superuser normalized: login=admin, name=Admin")

    # Start target instance
    if ws_send:
        await ws_send(f"Starting {target_service}...")
    await run_cmd(f"systemctl start {target_service}", ws_send)

    if ws_send:
        await ws_send(f"Sync complete: {instance_name} now mirrors {prod_name}")


# ─── Backup & Restore ─────────────────────────────────────────────────────

async def backup_instance(instance_name: str, ws_send=None) -> str:
    """Create an Odoo-compatible backup zip (dump.sql + filestore + manifest.json).

    Returns the path to the temporary zip file.
    """
    config = load_config()
    instance = config["instances"].get(instance_name)
    if not instance:
        raise ValueError(f"Instance {instance_name} not found")

    version = instance.get("version", "19")
    base = odoo_base_dir(version)
    db_name = instance["db_name"]
    data_dir = f"{base}/data/{instance_name}"
    filestore_dir = f"{data_dir}/filestore/{db_name}"

    tmp_dir = tempfile.mkdtemp(prefix="odoo_backup_")
    try:
        if ws_send:
            await ws_send(f"Dumping database {db_name}...")
        dump_path = f"{tmp_dir}/dump.sql"
        await run_cmd(f"pg_dump -U odoo --no-owner --file={dump_path} {db_name}", ws_send)

        if ws_send:
            await ws_send("Generating manifest...")
        proc = await asyncio.create_subprocess_shell(
            f"psql -U odoo -d {db_name} -t -A -c "
            "\"SELECT name FROM ir_module_module WHERE state = 'installed' ORDER BY name\"",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        modules = [m.strip() for m in stdout.decode().strip().split("\n") if m.strip()]

        proc2 = await asyncio.create_subprocess_shell(
            f"psql -U odoo -d {db_name} -t -A -c "
            "\"SELECT latest_version FROM ir_module_module WHERE name = 'base'\"",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout2, _ = await proc2.communicate()
        odoo_version = stdout2.decode().strip() or f"{version}.0"

        manifest = {
            "odoo_dump": "1",
            "db_name": db_name,
            "version": odoo_version,
            "version_info": [int(version), 0, 0, "final", 0],
            "major_version": f"{version}.0",
            "pg_version": "16.0",
            "modules": {m: True for m in modules},
        }
        manifest_path = f"{tmp_dir}/manifest.json"
        with open(manifest_path, "w") as f:
            _json.dump(manifest, f, indent=2)

        if ws_send:
            await ws_send("Creating zip archive...")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        zip_path = f"{tmp_dir}/{db_name}_{timestamp}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(dump_path, "dump.sql")
            zf.write(manifest_path, "manifest.json")
            if Path(filestore_dir).exists():
                for root, dirs, files in os.walk(filestore_dir):
                    for fname in files:
                        full = os.path.join(root, fname)
                        arcname = "filestore/" + os.path.relpath(full, filestore_dir)
                        zf.write(full, arcname)

        os.unlink(dump_path)
        os.unlink(manifest_path)

        zip_size = Path(zip_path).stat().st_size
        if ws_send:
            await ws_send(f"Backup complete: {zip_size / 1024 / 1024:.1f} MB")
        return zip_path
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise


async def restore_instance(instance_name: str, zip_path: str, ws_send=None):
    """Restore an Odoo instance from a backup zip (Odoo database manager format).

    Stops instance, drops DB, restores dump.sql + filestore, resets admin pw, starts instance.
    """
    config = load_config()
    instance = config["instances"].get(instance_name)
    if not instance:
        raise ValueError(f"Instance {instance_name} not found")

    version = instance.get("version", "19")
    base = odoo_base_dir(version)
    db_name = instance["db_name"]
    service = instance["service"]
    data_dir = f"{base}/data/{instance_name}"
    filestore_dir = f"{data_dir}/filestore/{db_name}"

    tmp_dir = tempfile.mkdtemp(prefix="odoo_restore_")
    try:
        if ws_send:
            await ws_send("Validating backup archive...")
        with zipfile.ZipFile(zip_path, "r") as zf:
            names = zf.namelist()
            if "dump.sql" not in names:
                raise ValueError("Invalid backup: dump.sql not found in archive")
            zf.extractall(tmp_dir)

        if ws_send:
            await ws_send(f"Stopping {service}...")
        await run_cmd(f"systemctl stop {service}", ws_send)

        if ws_send:
            await ws_send(f"Terminating connections to {db_name}...")
        await run_cmd(
            f"su - postgres -c \"psql -c \\\"SELECT pg_terminate_backend(pid) "
            f"FROM pg_stat_activity WHERE datname='{db_name}' "
            f"AND pid <> pg_backend_pid()\\\"\"",
            ws_send,
        )

        if ws_send:
            await ws_send(f"Dropping database {db_name}...")
        await run_cmd(f'su - postgres -c "dropdb --if-exists {db_name}"', ws_send)

        if ws_send:
            await ws_send(f"Creating empty database {db_name}...")
        await run_cmd(f'su - postgres -c "createdb -O odoo {db_name}"', ws_send)

        if ws_send:
            await ws_send("Restoring database from dump...")
        await run_cmd(f"psql -U odoo --dbname={db_name} -q -o /dev/null -f {tmp_dir}/dump.sql", ws_send)

        extracted_filestore = f"{tmp_dir}/filestore"
        if Path(extracted_filestore).exists():
            if ws_send:
                await ws_send("Restoring filestore...")
            if Path(filestore_dir).exists():
                shutil.rmtree(filestore_dir)
            os.makedirs(os.path.dirname(filestore_dir), exist_ok=True)
            shutil.copytree(extracted_filestore, filestore_dir)
            await run_cmd(f"chown -R odoo:odoo {filestore_dir}", ws_send)
        else:
            if ws_send:
                await ws_send("No filestore in backup archive")

        if ws_send:
            await ws_send("Resetting admin password...")
        from admin_pw import reset_admin_pw
        new_pw = _generate_password()
        reset_admin_pw(db_name, new_pw)
        config = load_config()
        config["instances"][instance_name]["admin_pw"] = new_pw
        save_config(config)

        if ws_send:
            await ws_send(f"Starting {service}...")
        await run_cmd(f"systemctl start {service}", ws_send)

        if ws_send:
            await ws_send(f"Restore complete. New admin password: {new_pw}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        if Path(zip_path).exists():
            os.unlink(zip_path)


# ─── Step Dependency System ────────────────────────────────────────────────

VERSION_STEPS = {"odoo_19", "odoo_18"}

STEP_DEPS = {
    "system_update": [],
    "postgresql": ["system_update"],
    "odoo_19": ["postgresql"],
    "odoo_18": ["postgresql"],
    "nginx": [("odoo_19", "odoo_18")],
    "mailpit": ["nginx"],
    "dns_check": ["mailpit"],
    "ssl_certs": ["dns_check"],
}

STEP_ORDER = [
    "system_update", "postgresql",
    "odoo_19", "odoo_18",
    "nginx", "mailpit", "dns_check", "ssl_certs",
]

SETUP_STEPS = {
    "system_update": step_system_update,
    "postgresql": step_postgresql,
    "odoo_19": step_odoo_19,
    "odoo_18": step_odoo_18,
    "nginx": step_nginx,
    "mailpit": step_mailpit,
    "dns_check": step_dns_check,
    "ssl_certs": step_ssl_certs,
}


def is_step_unlocked(step_id: str, config: dict) -> bool:
    """Check if a step's dependencies are satisfied."""
    deps = STEP_DEPS.get(step_id, [])
    steps = config.get("setup_steps", {})
    for dep in deps:
        if isinstance(dep, tuple):
            if not any(steps.get(d, {}).get("status") == "done" for d in dep):
                return False
        else:
            if steps.get(dep, {}).get("status") != "done":
                return False
    return True


def get_installed_versions(config: dict) -> list[str]:
    """Return list of installed Odoo version strings (e.g. ['19', '18'])."""
    versions = []
    steps = config.get("setup_steps", {})
    for vs in sorted(VERSION_STEPS, reverse=True):
        ver = vs.replace("odoo_", "")
        if steps.get(vs, {}).get("status") == "done":
            versions.append(ver)
    return versions
