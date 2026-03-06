"""
Odoo Deployment Platform - FastAPI Admin Panel
Main application with setup wizard, instance management, and deployment.
"""
import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile as _tempfile
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import psutil

from config import load_config, save_config, PLATFORM_DIR
from auth import (
    is_auth_enabled, get_current_user, is_path_exempt,
    get_google_auth_url, exchange_code_for_user, generate_state, verify_ws_auth,
)
from setup_steps import (
    SETUP_STEPS, STEP_ORDER, STEP_DEPS, VERSION_STEPS,
    is_step_unlocked, get_installed_versions, odoo_base_dir,
    create_odoo_instance, delete_odoo_instance, sync_instance_from_prod, run_cmd,
    _generate_password, fix_addons_ownership, get_instance_owner,
    backup_instance, restore_instance,
)
from admin_pw import verify_admin_pw, reset_admin_pw, normalize_superuser
from neutralize import is_neutralized, neutralize_db, deneutralize_db
from studio_bridge import (
    get_custom_addons_info, get_instance_addons, get_studio_stats,
    export_studio_to_module, convert_module_to_studio, install_or_upgrade_module,
    fix_misplaced_definitions,
)

_public_ip_cache = None


def get_public_ip() -> str:
    """Detect the server's public IP address (cached)."""
    global _public_ip_cache
    if _public_ip_cache is not None:
        return _public_ip_cache
    for url in ["https://api.ipify.org", "https://icanhazip.com", "https://ifconfig.me"]:
        try:
            result = subprocess.run(
                ["curl", "-s", "--max-time", "3", url],
                capture_output=True, text=True, timeout=5
            )
            ip = result.stdout.strip()
            if ip and not ip.startswith("<"):
                _public_ip_cache = ip
                return _public_ip_cache
        except Exception:
            continue
    _public_ip_cache = "–"
    return _public_ip_cache

_instance_stats: dict[str, dict] = {}


async def _collect_instance_stats():
    """Background task: collect per-instance CPU, RAM, disk every 15s."""
    global _instance_stats
    while True:
        try:
            config = load_config()
            instances = config.get("instances", {})
            stats: dict[str, dict] = {}

            procs = list(psutil.process_iter(["pid", "cmdline", "cpu_percent", "memory_info"]))
            for name, inst in instances.items():
                conf = inst.get("conf", "")
                cpu = 0.0
                ram = 0
                for p in procs:
                    try:
                        cmdline = " ".join(p.info.get("cmdline") or [])
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
                    if conf and conf in cmdline:
                        cpu += p.info.get("cpu_percent", 0) or 0
                        mem = p.info.get("memory_info")
                        if mem:
                            ram += mem.rss
                stats[name] = {"cpu_percent": min(round(cpu, 1), 100.0), "ram_mb": round(ram / 1048576)}

            async def _measure_disk(iname, inst):
                version = inst.get("version", "19")
                data_dir = f"/opt/odoo{version}/data/{iname}"
                try:
                    proc = await asyncio.create_subprocess_shell(
                        f"du -sb {data_dir} 2>/dev/null",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    stdout, _ = await proc.communicate()
                    size = int(stdout.decode().split()[0]) if stdout.strip() else 0
                    stats[iname]["disk_mb"] = round(size / 1048576)
                except Exception:
                    stats[iname]["disk_mb"] = 0

            await asyncio.gather(*[_measure_disk(n, i) for n, i in instances.items()])
            _instance_stats = stats
        except Exception:
            pass
        await asyncio.sleep(15)


app = FastAPI(title="Odoo Deployment Platform", version="1.0.0")


@app.on_event("startup")
async def _start_stats_collector():
    asyncio.create_task(_collect_instance_stats())


# Static files and templates
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

STATIC_DIR = Path(__file__).parent / "static"


def _static_url(path: str) -> str:
    """Return /static/path?v=<mtime> for cache-busting."""
    try:
        mtime = int((STATIC_DIR / path).stat().st_mtime)
    except OSError:
        mtime = 0
    return f"/static/{path}?v={mtime}"

templates.env.globals["static_url"] = _static_url


# ─── Helper ─────────────────────────────────────────────────────────────────

def is_setup_complete() -> bool:
    """Check if all required setup steps are done (at least one version step)."""
    config = load_config()
    steps = config["setup_steps"]
    if not any(steps.get(vs, {}).get("status") == "done" for vs in VERSION_STEPS):
        return False
    for step_id, step in steps.items():
        if step_id in VERSION_STEPS:
            continue
        if step["status"] != "done":
            return False
    return True


RESERVED_PORTS = {8025, 8080, 1025}


def get_next_port() -> int:
    """Get the next available port starting from 8069.
    Each instance uses two consecutive ports: HTTP and gevent (HTTP+1).
    """
    config = load_config()
    used = set()
    for inst in config["instances"].values():
        p = inst["port"]
        used.update({p, p + 1})
    used.update(RESERVED_PORTS)
    port = 8069
    while port in used or (port + 1) in used:
        port += 1
    return port


def _step_deps_json() -> dict:
    """Serialize STEP_DEPS for Jinja2/JSON (tuples -> lists)."""
    return {
        k: [list(d) if isinstance(d, tuple) else d for d in v]
        for k, v in STEP_DEPS.items()
    }


# ─── Auth Middleware ──────────────────────────────────────────────────────────

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    config = load_config()
    if not is_auth_enabled(config):
        return await call_next(request)
    if is_path_exempt(request.url.path):
        return await call_next(request)
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return await call_next(request)


# Session middleware (must be added AFTER auth middleware so it wraps it)
_init_config = load_config()
app.add_middleware(
    SessionMiddleware,
    secret_key=_init_config.get("auth", {}).get("session_secret", "fallback-dev-key"),
    session_cookie="odoo_platform_session",
    max_age=14 * 24 * 60 * 60,
    same_site="lax",
    https_only=False,
)


# ─── Auth Routes ─────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    config = load_config()
    user = get_current_user(request)
    if user and is_auth_enabled(config):
        return RedirectResponse(url="/")
    return templates.TemplateResponse("login.html", {
        "request": request,
        "error": error,
        "auth_enabled": is_auth_enabled(config),
    })


@app.get("/auth/google")
async def auth_google(request: Request):
    config = load_config()
    if not is_auth_enabled(config):
        return RedirectResponse(url="/")
    state = generate_state()
    request.session["oauth_state"] = state
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    redirect_uri = f"{scheme}://{request.url.netloc}/auth/callback"
    auth_url = get_google_auth_url(config, redirect_uri, state)
    return RedirectResponse(url=auth_url)


@app.get("/auth/callback")
async def auth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error:
        return RedirectResponse(url=f"/login?error={error}")
    config = load_config()
    if not is_auth_enabled(config):
        return RedirectResponse(url="/")
    expected_state = request.session.pop("oauth_state", "")
    if not state or state != expected_state:
        return RedirectResponse(url="/login?error=invalid_state")
    if not code:
        return RedirectResponse(url="/login?error=no_code")
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    redirect_uri = f"{scheme}://{request.url.netloc}/auth/callback"
    user_info = await exchange_code_for_user(code, config, redirect_uri)
    if not user_info or not user_info.get("email"):
        return RedirectResponse(url="/login?error=auth_failed")
    email = user_info["email"].lower().strip()
    request.session["user"] = {
        "email": email,
        "name": user_info.get("name", email),
        "picture": user_info.get("picture", ""),
    }
    return RedirectResponse(url="/")


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login")


@app.post("/api/auth/remove")
async def api_remove_auth(request: Request):
    config = load_config()
    config["auth"]["google_client_id"] = ""
    config["auth"]["google_client_secret"] = ""
    save_config(config)
    request.session.clear()
    return {"status": "ok"}


# ─── Pages ───────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Main page - redirects to setup wizard or dashboard."""
    if not is_setup_complete():
        return RedirectResponse(url="/setup")
    return RedirectResponse(url="/dashboard")


@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    """Setup wizard page."""
    config = load_config()
    return templates.TemplateResponse("setup.html", {
        "request": request,
        "config": config,
        "steps": config["setup_steps"],
        "step_ids": STEP_ORDER,
        "step_deps": _step_deps_json(),
        "is_step_unlocked": is_step_unlocked,
        "version_steps": VERSION_STEPS,
        "user": get_current_user(request),
    })


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    """Main dashboard after setup is complete."""
    config = load_config()
    # Get service statuses
    instances = {}
    for name, inst in config.get("instances", {}).items():
        service = inst.get("service", "")
        try:
            result = subprocess.run(
                ["systemctl", "is-active", service],
                capture_output=True, text=True, timeout=5
            )
            status = result.stdout.strip()
        except Exception:
            status = "unknown"
        instances[name] = {**inst, "status": status}

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "config": config,
        "instances": instances,
        "public_ip": get_public_ip(),
        "user": get_current_user(request),
    })


@app.get("/instances", response_class=HTMLResponse)
async def instances_page(request: Request):
    """Instance management page."""
    config = load_config()
    instances = {}
    for name, inst in config.get("instances", {}).items():
        service = inst.get("service", "")
        try:
            result = subprocess.run(
                ["systemctl", "is-active", service],
                capture_output=True, text=True, timeout=5
            )
            status = result.stdout.strip()
        except Exception:
            status = "unknown"
        db_name = inst.get("db_name", name)
        try:
            neutralized = is_neutralized(db_name)
        except Exception:
            neutralized = None
        instances[name] = {**inst, "status": status, "neutralized": neutralized, **_instance_stats.get(name, {})}

    return templates.TemplateResponse("instances.html", {
        "request": request,
        "config": config,
        "instances": instances,
        "installed_versions": get_installed_versions(config),
        "user": get_current_user(request),
    })


@app.get("/deploy", response_class=HTMLResponse)
async def deploy_page(request: Request):
    """Deployment page."""
    config = load_config()
    return templates.TemplateResponse("deploy.html", {
        "request": request,
        "config": config,
        "user": get_current_user(request),
    })


@app.get("/databases", response_class=HTMLResponse)
async def databases_page(request: Request):
    """Database management page."""
    config = load_config()
    return templates.TemplateResponse("databases.html", {
        "request": request,
        "config": config,
        "user": get_current_user(request),
    })


@app.get("/studio-bridge", response_class=HTMLResponse)
async def studio_bridge_page(request: Request, instance: str = ""):
    """Studio Bridge page."""
    config = load_config()
    instances = {}
    for name, inst in config.get("instances", {}).items():
        instances[name] = inst
    return templates.TemplateResponse("modules.html", {
        "request": request,
        "config": config,
        "instances": instances,
        "selected_instance": instance,
        "user": get_current_user(request),
    })


# ─── API Endpoints ──────────────────────────────────────────────────────────

@app.get("/api/config")
async def api_get_config():
    """Get current platform configuration."""
    return load_config()


@app.post("/api/config")
async def api_update_config(request: Request):
    """Update platform configuration."""
    data = await request.json()
    config = load_config()
    allowed_keys = {"domain", "github_token", "auth"}
    for key in data:
        if key in allowed_keys:
            if key == "auth":
                existing_auth = config.get("auth", {})
                existing_auth.update(data[key])
                config["auth"] = existing_auth
            else:
                config[key] = data[key]
    save_config(config)
    return {"status": "ok", "config": config}


@app.get("/api/instances")
async def api_list_instances():
    """List all Odoo instances with their status."""
    config = load_config()
    instances = {}
    for name, inst in config.get("instances", {}).items():
        service = inst.get("service", "")
        try:
            result = subprocess.run(
                ["systemctl", "is-active", service],
                capture_output=True, text=True, timeout=5
            )
            status = result.stdout.strip()
        except Exception:
            status = "unknown"
        db_name = inst.get("db_name", name)
        try:
            neutralized = is_neutralized(db_name)
        except Exception:
            neutralized = None
        instances[name] = {**inst, "status": status, "neutralized": neutralized, **_instance_stats.get(name, {})}
    return instances


@app.post("/api/instances")
async def api_create_instance(request: Request):
    """Create a new Odoo instance."""
    data = await request.json()
    client = data.get("client")
    env = data.get("env", "dev")
    dev_name = data.get("dev_name", "")
    version = data.get("version", "19")
    port = data.get("port", get_next_port())
    default_workers = {"prod": 4, "staging": 2}.get(env, 0)
    workers = int(data.get("workers", default_workers))

    if not client:
        raise HTTPException(status_code=400, detail="client is required")

    config = load_config()
    installed = get_installed_versions(config)
    if version not in installed:
        raise HTTPException(status_code=400, detail=f"Odoo {version} is not installed")

    if env == "dev" and dev_name:
        actual_env = f"dev_{dev_name}"
    else:
        actual_env = env

    await create_odoo_instance(client, actual_env, port, workers, version=version)
    return {"status": "ok", "instance": f"{client}_{actual_env}", "port": port}


@app.delete("/api/instances/{instance_name}")
async def api_delete_instance(instance_name: str):
    """Delete an Odoo instance."""
    await delete_odoo_instance(instance_name)
    return {"status": "ok"}


@app.post("/api/instances/{instance_name}/restart")
async def api_restart_instance(instance_name: str):
    """Restart an Odoo instance."""
    config = load_config()
    instance = config["instances"].get(instance_name)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")
    service = instance["service"]
    result = subprocess.run(
        ["systemctl", "restart", service],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=result.stderr)
    instance.pop("restart_needed", None)
    save_config(config)
    return {"status": "ok"}


@app.post("/api/instances/{instance_name}/stop")
async def api_stop_instance(instance_name: str):
    """Stop an Odoo instance."""
    config = load_config()
    instance = config["instances"].get(instance_name)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")
    service = instance["service"]
    subprocess.run(["systemctl", "stop", service], capture_output=True, timeout=30)
    return {"status": "ok"}


@app.patch("/api/instances/{instance_name}")
async def api_update_instance(instance_name: str, request: Request):
    """Update instance settings (e.g. workers)."""
    config = load_config()
    instance = config["instances"].get(instance_name)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    data = await request.json()
    workers = data.get("workers")
    if workers is not None:
        workers = int(workers)
        instance["workers"] = workers
        conf_path = instance["conf"]
        with open(conf_path, "r") as f:
            conf = f.read()
        conf = re.sub(r"^workers\s*=.*$", f"workers = {workers}", conf, flags=re.MULTILINE)
        conf = re.sub(r"^max_cron_threads\s*=.*$", f"max_cron_threads = {1 if workers > 0 else 0}", conf, flags=re.MULTILINE)
        with open(conf_path, "w") as f:
            f.write(conf)

        # Toggle --dev flag in systemd service based on workers
        version = instance.get("version", "19")
        base = odoo_base_dir(version)
        dev_flag = " --dev=xml,reload" if workers == 0 else ""
        service_path = f"/etc/systemd/system/{instance['service']}.service"
        with open(service_path, "r") as f:
            svc = f.read()
        svc = re.sub(
            r"(ExecStart=.*odoo-bin -c \S+)(\s+--dev=\S+)?",
            rf"\1{dev_flag}",
            svc,
        )
        with open(service_path, "w") as f:
            f.write(svc)
        subprocess.run(["systemctl", "daemon-reload"], capture_output=True, timeout=10)

        instance["restart_needed"] = True
        save_config(config)

    return {"status": "ok"}


@app.get("/api/instances/admin-pw-status")
async def api_admin_pw_status():
    """Check if stored admin_pw matches DB for all instances."""
    config = load_config()
    result = {}
    for name, inst in config.get("instances", {}).items():
        db_name = inst.get("db_name", name)
        stored_pw = inst.get("admin_pw", "")
        if not stored_pw:
            result[name] = {"match": False}
            continue
        result[name] = {"match": verify_admin_pw(db_name, stored_pw)}
    return result


@app.post("/api/instances/{instance_name}/reset-admin-pw")
async def api_reset_admin_pw(instance_name: str):
    """Generate a new admin password, write to DB, update platform.json."""
    config = load_config()
    instance = config["instances"].get(instance_name)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    db_name = instance.get("db_name", instance_name)
    new_pw = _generate_password()

    try:
        reset_admin_pw(db_name, new_pw)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to reset password: {e}")

    instance["admin_pw"] = new_pw
    save_config(config)
    return {"status": "ok", "admin_pw": new_pw}


SSH_KEY_PREFIXES = (
    "ssh-rsa", "ssh-ed25519", "ssh-dss",
    "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521",
    "sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com",
)

SSH_TYPE_NAMES = {
    "ssh-rsa": "RSA", "ssh-ed25519": "ED25519", "ssh-dss": "DSA",
    "ecdsa-sha2-nistp256": "ECDSA", "ecdsa-sha2-nistp384": "ECDSA",
    "ecdsa-sha2-nistp521": "ECDSA",
    "sk-ssh-ed25519@openssh.com": "ED25519-SK",
    "sk-ecdsa-sha2-nistp256@openssh.com": "ECDSA-SK",
}


def _parse_ssh_key(line: str) -> dict | None:
    """Parse an SSH public key line into {type, fingerprint, comment, line}."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    parts = line.split()
    if len(parts) < 2:
        return None
    key_type = parts[0]
    if not any(key_type == p for p in SSH_KEY_PREFIXES):
        return None
    try:
        key_data = base64.b64decode(parts[1])
    except Exception:
        return None
    digest = hashlib.sha256(key_data).digest()
    fp = "SHA256:" + base64.b64encode(digest).rstrip(b"=").decode()
    comment = " ".join(parts[2:]) if len(parts) > 2 else ""
    friendly = SSH_TYPE_NAMES.get(key_type, key_type.upper())
    return {"type": friendly, "fingerprint": fp, "comment": comment, "line": line}


def _read_authorized_keys(path: str) -> list[dict]:
    """Read and parse all SSH keys from an authorized_keys file."""
    keys = []
    try:
        with open(path) as f:
            for raw in f:
                parsed = _parse_ssh_key(raw)
                if parsed:
                    keys.append(parsed)
    except FileNotFoundError:
        pass
    return keys


def _write_authorized_keys(path: str, keys: list[dict], ssh_user: str):
    """Write keys back to authorized_keys and fix ownership."""
    with open(path, "w") as f:
        for k in keys:
            f.write(k["line"] + "\n")
    shutil.chown(path, user=ssh_user, group=ssh_user)


def _keys_response(keys: list[dict]) -> list[dict]:
    """Strip internal 'line' field for API responses."""
    return [{"type": k["type"], "fingerprint": k["fingerprint"], "comment": k["comment"]} for k in keys]


def _get_ssh_context(instance_name: str):
    """Shared validation for SSH key endpoints. Returns (instance, ssh_user, auth_keys_path, config)."""
    config = load_config()
    instance = config["instances"].get(instance_name)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")
    ssh_user = instance.get("ssh_user")
    if not ssh_user:
        raise HTTPException(status_code=400, detail="Not a dev instance")
    version = instance.get("version", "19")
    data_dir = f"{odoo_base_dir(version)}/data/{instance_name}"
    auth_keys = f"{data_dir}/.ssh/authorized_keys"
    return instance, ssh_user, auth_keys, config


@app.get("/api/instances/{instance_name}/ssh-keys")
async def api_list_ssh_keys(instance_name: str):
    """List all SSH public keys for a dev instance."""
    instance, ssh_user, auth_keys, config = _get_ssh_context(instance_name)
    keys = _read_authorized_keys(auth_keys)
    return {"keys": _keys_response(keys)}


@app.post("/api/instances/{instance_name}/ssh-keys")
async def api_add_ssh_key(instance_name: str, request: Request):
    """Add an SSH public key to a dev instance."""
    instance, ssh_user, auth_keys, config = _get_ssh_context(instance_name)

    data = await request.json()
    public_key = data.get("public_key", "").strip()
    parsed = _parse_ssh_key(public_key)
    if not parsed:
        raise HTTPException(status_code=400, detail="Invalid SSH public key")

    existing = _read_authorized_keys(auth_keys)
    for k in existing:
        if k["fingerprint"] == parsed["fingerprint"]:
            raise HTTPException(status_code=409, detail="This key is already authorized")

    existing.append(parsed)
    _write_authorized_keys(auth_keys, existing, ssh_user)

    instance["ssh_key_count"] = len(existing)
    save_config(config)
    return {"status": "ok", "keys": _keys_response(existing)}


@app.delete("/api/instances/{instance_name}/ssh-keys/{fingerprint}")
async def api_remove_ssh_key(instance_name: str, fingerprint: str):
    """Remove an SSH public key by fingerprint hash."""
    instance, ssh_user, auth_keys, config = _get_ssh_context(instance_name)

    full_fp = f"SHA256:{fingerprint}"
    existing = _read_authorized_keys(auth_keys)
    remaining = [k for k in existing if k["fingerprint"] != full_fp]

    if len(remaining) == len(existing):
        raise HTTPException(status_code=404, detail="Key not found")

    _write_authorized_keys(auth_keys, remaining, ssh_user)

    instance["ssh_key_count"] = len(remaining)
    save_config(config)
    return {"status": "ok", "keys": _keys_response(remaining)}


@app.post("/api/instances/{instance_name}/sync-to-repo")
async def api_sync_to_repo(instance_name: str):
    """Force-push this instance's addons state to the shared repo (overwrites)."""
    config = load_config()
    instance = config["instances"].get(instance_name)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    version = instance.get("version", "19")
    addons = f"{odoo_base_dir(version)}/data/{instance_name}/addons"

    if not Path(f"{addons}/.git").exists():
        raise HTTPException(status_code=400, detail="No git repo in addons directory")

    # Stage, commit, and force-push to overwrite shared repo
    owner = get_instance_owner(instance)
    subprocess.run(["sudo", "-u", owner, "git", "-C", addons, "add", "-A"], capture_output=True, timeout=10)
    status = subprocess.run(
        ["sudo", "-u", owner, "git", "-C", addons, "status", "--porcelain"],
        capture_output=True, text=True, timeout=10,
    )
    if not status.stdout.strip():
        return {"status": "ok", "message": "Nothing to sync — already up to date"}

    subprocess.run(
        ["sudo", "-u", owner, "git", "-C", addons, "commit", "-m", f"Sync from {instance_name}"],
        capture_output=True, text=True, timeout=10,
    )
    push = subprocess.run(
        ["sudo", "-u", "odoo", "git", "-C", addons, "push", "--force"],
        capture_output=True, text=True, timeout=30,
    )
    if push.returncode != 0:
        raise HTTPException(status_code=500, detail=push.stderr)

    return {"status": "ok", "message": "Addons synced to repo"}


@app.post("/api/instances/{instance_name}/neutralize")
async def api_toggle_neutralize(instance_name: str, request: Request):
    """Toggle neutralization for an instance."""
    config = load_config()
    instance = config["instances"].get(instance_name)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    data = await request.json()
    neutralized = data.get("neutralized", True)
    db_name = instance.get("db_name", instance_name)

    try:
        if neutralized:
            stats = neutralize_db(db_name)
        else:
            stats = deneutralize_db(db_name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Neutralization failed: {e}")

    service = instance.get("service", f"odoo-{instance_name}")
    subprocess.run(["systemctl", "restart", service], capture_output=True, timeout=30)

    return {"status": "ok", "neutralized": neutralized, "stats": stats}


@app.get("/api/instances/{instance_name}/backup")
async def api_backup_instance(instance_name: str):
    """Download a full backup zip of the instance (Odoo DB manager compatible)."""
    config = load_config()
    instance = config["instances"].get(instance_name)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    try:
        zip_path = await backup_instance(instance_name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Backup failed: {e}")

    filename = Path(zip_path).name
    tmp_dir = str(Path(zip_path).parent)

    def cleanup():
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return FileResponse(
        path=zip_path,
        media_type="application/zip",
        filename=filename,
        background=BackgroundTask(cleanup),
    )


@app.get("/api/instances/{instance_name}/addons-zip")
async def api_download_addons(instance_name: str):
    """Download the instance's custom addons directory as a zip (respects .gitignore)."""
    config = load_config()
    instance = config["instances"].get(instance_name)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    version = instance.get("version", "19")
    addons = Path(f"{odoo_base_dir(version)}/data/{instance_name}/addons")
    if not addons.exists():
        raise HTTPException(status_code=404, detail="Addons directory not found")

    from datetime import datetime
    stamp = datetime.now().strftime("%Y-%m-%d-%H-%M")
    slug = instance_name.replace("_", "-")
    filename = f"{stamp}-{slug}-addons.zip"

    tmp_dir = _tempfile.mkdtemp()
    zip_path = os.path.join(tmp_dir, filename)

    if Path(f"{addons}/.git").exists():
        owner = get_instance_owner(instance)
        result = subprocess.run(
            ["sudo", "-u", owner, "git", "-C", str(addons), "archive", "--format=zip", "-o", zip_path, "HEAD"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise HTTPException(status_code=500, detail=result.stderr)
    else:
        shutil.make_archive(zip_path.removesuffix(".zip"), "zip", str(addons.parent), addons.name)

    def cleanup():
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return FileResponse(
        path=zip_path,
        media_type="application/zip",
        filename=filename,
        background=BackgroundTask(cleanup),
    )


_pending_uploads: dict[str, str] = {}


@app.post("/api/instances/{instance_name}/upload-backup")
async def api_upload_backup(instance_name: str, file: UploadFile = File(...)):
    """Upload a backup zip file for later restore via WebSocket."""
    config = load_config()
    instance = config["instances"].get(instance_name)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    tmp = _tempfile.NamedTemporaryFile(delete=False, suffix=".zip", prefix="odoo_upload_")
    try:
        while chunk := await file.read(1024 * 1024):
            tmp.write(chunk)
        tmp.close()
    except Exception:
        tmp.close()
        os.unlink(tmp.name)
        raise

    upload_id = secrets.token_hex(8)
    _pending_uploads[upload_id] = tmp.name
    return {"upload_id": upload_id}


@app.get("/api/databases")
async def api_list_databases():
    """List all PostgreSQL databases."""
    try:
        result = subprocess.run(
            ["su", "-", "postgres", "-c",
             "psql -t -A -c \"SELECT datname FROM pg_database WHERE datistemplate = false ORDER BY datname\""],
            capture_output=True, text=True, timeout=10
        )
        databases = [db.strip() for db in result.stdout.strip().split("\n") if db.strip()]
        return {"databases": databases}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/databases/clone")
async def api_clone_database(request: Request):
    """Clone a database."""
    data = await request.json()
    source = data.get("source")
    target = data.get("target")
    if not source or not target:
        raise HTTPException(status_code=400, detail="source and target required")

    result = subprocess.run(
        ["su", "-", "postgres", "-c", f"createdb -O odoo -T {source} {target}"],
        capture_output=True, text=True, timeout=120
    )
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=result.stderr)
    return {"status": "ok"}


@app.delete("/api/databases/{db_name}")
async def api_delete_database(db_name: str):
    """Delete a database."""
    # Safety check - don't delete system databases
    if db_name in ("postgres", "template0", "template1"):
        raise HTTPException(status_code=400, detail="Cannot delete system database")
    result = subprocess.run(
        ["su", "-", "postgres", "-c", f"dropdb --if-exists {db_name}"],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=result.stderr)
    return {"status": "ok"}


@app.post("/api/deploy")
async def api_deploy(request: Request):
    """Deploy: git pull addons from local bare repo, restart instance."""
    data = await request.json()
    instance_name = data.get("instance")

    config = load_config()
    instance = config["instances"].get(instance_name)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    version = instance.get("version", "19")
    base = odoo_base_dir(version)
    instance_addons = f"{base}/data/{instance_name}/addons"

    # Git pull from local bare repo
    owner = get_instance_owner(instance)
    git_output = ""
    if Path(f"{instance_addons}/.git").exists():
        result = subprocess.run(
            ["sudo", "-u", owner, "git", "-C", instance_addons, "pull"],
            capture_output=True, text=True, timeout=30,
        )
        git_output = result.stdout.strip() or result.stderr.strip()
        if result.returncode != 0:
            return {
                "status": "error",
                "git_output": git_output,
                "instance": instance_name,
                "message": "Git pull failed — instance was NOT restarted.",
            }
    else:
        git_output = "(no git repo in addons directory)"

    # Restart the service to pick up changes
    service = instance["service"]
    subprocess.run(["systemctl", "restart", service], capture_output=True, timeout=30)

    return {
        "status": "ok",
        "git_output": git_output,
        "instance": instance_name,
    }


# ─── Studio Bridge API ───────────────────────────────────────────────────────

@app.get("/api/studio-bridge/info/{instance_name}")
async def api_studio_bridge_info(instance_name: str):
    """Full Studio customization stats for a single instance."""
    config = load_config()
    instance = config.get("instances", {}).get(instance_name)
    if not instance:
        return {"error": f"Instance {instance_name} not found"}
    version = instance.get("version", "19")
    instance_addons = get_instance_addons(instance_name, version)
    addon_names = [a["name"] for a in instance_addons]
    stats = get_studio_stats(instance["db_name"], instance["client"], addon_names)
    stats["instance_addons"] = instance_addons
    return stats


@app.get("/api/studio-bridge/summary")
async def api_studio_bridge_summary():
    """Lightweight customization summary for all instances."""
    config = load_config()
    result = {}
    for name, inst in config.get("instances", {}).items():
        stats = get_studio_stats(inst["db_name"], inst["client"])
        if "error" in stats:
            result[name] = {"error": stats["error"]}
        else:
            result[name] = {
                "has_customizations": stats["has_customizations"],
                "module_state": stats["module_state"],
                "fields": stats["fields"],
                "misplaced": len(stats.get("misplaced_fields", [])) + len(stats.get("misplaced_models", [])),
            }
    return result


# ─── WebSocket for Setup Steps ──────────────────────────────────────────────

@app.websocket("/ws/setup/{step_id}")
async def ws_setup_step(websocket: WebSocket, step_id: str):
    """WebSocket endpoint for running setup steps with live output."""
    await websocket.accept()

    config = load_config()
    if not await verify_ws_auth(websocket, config):
        await websocket.send_json({"type": "error", "message": "Authentication required"})
        await websocket.close()
        return

    if step_id not in SETUP_STEPS:
        await websocket.send_json({"type": "error", "message": f"Unknown step: {step_id}"})
        await websocket.close()
        return

    async def ws_send(message: str):
        await websocket.send_json({"type": "log", "message": message})

    try:
        step_fn = SETUP_STEPS[step_id]
        await websocket.send_json({"type": "status", "status": "running"})
        await step_fn(ws_send=ws_send)
        await websocket.send_json({"type": "status", "status": "done"})
    except Exception as e:
        await websocket.send_json({"type": "error", "message": str(e)})
        await websocket.send_json({"type": "status", "status": "error"})
    finally:
        await websocket.close()


@app.websocket("/ws/instance/create")
async def ws_create_instance(websocket: WebSocket):
    """WebSocket endpoint for creating instances with live output."""
    await websocket.accept()

    config = load_config()
    if not await verify_ws_auth(websocket, config):
        await websocket.send_json({"type": "error", "message": "Authentication required"})
        await websocket.close()
        return

    try:
        data = await websocket.receive_json()
        client = data.get("client")
        env = data.get("env", "dev")
        dev_name = data.get("dev_name", "")
        version = data.get("version", "19")
        port = data.get("port", get_next_port())
        default_workers = {"prod": 4, "staging": 2}.get(env, 0)
        workers = int(data.get("workers", default_workers))

        if env == "dev" and dev_name:
            actual_env = f"dev_{dev_name}"
        else:
            actual_env = env

        async def ws_send(message: str):
            await websocket.send_json({"type": "log", "message": message})

        await websocket.send_json({"type": "status", "status": "running"})
        await create_odoo_instance(client, actual_env, port, workers, ws_send, version=version)
        await websocket.send_json({"type": "status", "status": "done"})
    except Exception as e:
        await websocket.send_json({"type": "error", "message": str(e)})
    finally:
        await websocket.close()


@app.websocket("/ws/instance/sync/{instance_name}")
async def ws_sync_instance(websocket: WebSocket, instance_name: str):
    """WebSocket endpoint for syncing an instance from prod with live output."""
    await websocket.accept()

    config = load_config()
    if not await verify_ws_auth(websocket, config):
        await websocket.send_json({"type": "error", "message": "Authentication required"})
        await websocket.close()
        return

    try:
        async def ws_send(message: str):
            await websocket.send_json({"type": "log", "message": message})

        await websocket.send_json({"type": "status", "status": "running"})
        await sync_instance_from_prod(instance_name, ws_send)
        await websocket.send_json({"type": "status", "status": "done"})
    except Exception as e:
        await websocket.send_json({"type": "error", "message": str(e)})
        await websocket.send_json({"type": "status", "status": "error"})
    finally:
        await websocket.close()


@app.websocket("/ws/instance/{instance_name}/upgrade")
async def ws_upgrade_instance(websocket: WebSocket, instance_name: str):
    """WebSocket endpoint for upgrading custom addons with live output."""
    await websocket.accept()

    config = load_config()
    if not await verify_ws_auth(websocket, config):
        await websocket.send_json({"type": "error", "message": "Authentication required"})
        await websocket.close()
        return

    try:
        instance = config["instances"].get(instance_name)
        if not instance:
            raise ValueError(f"Instance {instance_name} not found")

        version = instance.get("version", "19")
        db_name = instance["db_name"]
        service = instance["service"]
        conf = instance["conf"]
        base = odoo_base_dir(version)
        addons_dir = Path(f"{base}/data/{instance_name}/addons")

        modules = [
            d.name for d in addons_dir.iterdir()
            if d.is_dir() and (d / "__manifest__.py").exists()
        ] if addons_dir.exists() else []

        if not modules:
            raise ValueError("No custom addons found to upgrade")

        module_list = ",".join(modules)

        async def ws_send(message: str):
            await websocket.send_json({"type": "log", "message": message})

        await websocket.send_json({"type": "status", "status": "running"})
        await ws_send(f"Upgrading modules: {module_list}")

        await ws_send("Stopping service...")
        await run_cmd(f"systemctl stop {service}", ws_send)

        try:
            await ws_send("Running module upgrade...")
            await run_cmd(
                f"{base}/venv/bin/python {base}/odoo/odoo-bin"
                f" -c {conf} -u {module_list} -d {db_name}"
                f" --stop-after-init --logfile=",
                ws_send,
            )
        finally:
            await ws_send("Starting service...")
            await run_cmd(f"systemctl start {service}", ws_send)

        await websocket.send_json({"type": "status", "status": "done"})
    except Exception as e:
        await websocket.send_json({"type": "error", "message": str(e)})
        await websocket.send_json({"type": "status", "status": "error"})
    finally:
        await websocket.close()


@app.websocket("/ws/instance/restore/{instance_name}")
async def ws_restore_instance(websocket: WebSocket, instance_name: str):
    """WebSocket endpoint for restoring an instance from a backup with live output."""
    await websocket.accept()

    config = load_config()
    if not await verify_ws_auth(websocket, config):
        await websocket.send_json({"type": "error", "message": "Authentication required"})
        await websocket.close()
        return

    try:
        data = await websocket.receive_json()
        upload_id = data.get("upload_id")
        if not upload_id or upload_id not in _pending_uploads:
            raise ValueError("Invalid or expired upload ID")

        zip_path = _pending_uploads.pop(upload_id)

        async def ws_send(message: str):
            await websocket.send_json({"type": "log", "message": message})

        await websocket.send_json({"type": "status", "status": "running"})
        await restore_instance(instance_name, zip_path, ws_send)
        await websocket.send_json({"type": "status", "status": "done"})
    except Exception as e:
        await websocket.send_json({"type": "error", "message": str(e)})
        await websocket.send_json({"type": "status", "status": "error"})
    finally:
        await websocket.close()


# ─── WebSocket for Studio Bridge ─────────────────────────────────────────────

@app.websocket("/ws/studio-bridge/export")
async def ws_studio_bridge_export(websocket: WebSocket):
    """Export Studio customizations to a module and install it on the instance."""
    await websocket.accept()

    config = load_config()
    if not await verify_ws_auth(websocket, config):
        await websocket.send_json({"type": "error", "message": "Authentication required"})
        await websocket.close()
        return

    try:
        data = await websocket.receive_json()
        instance_name = data.get("instance_name")
        db_name = data.get("db_name")
        client = data.get("client")
        version = data.get("version", "19")

        if not db_name or not client or not instance_name:
            raise ValueError("instance_name, db_name and client are required")

        module_name = f"{client}_base"

        async def ws_send(message: str):
            await websocket.send_json({"type": "log", "message": message})

        await websocket.send_json({"type": "status", "status": "running"})
        instance_addons = f"{odoo_base_dir(version)}/data/{instance_name}/addons"
        await export_studio_to_module(db_name, client, version, ws_send, addons_dir=instance_addons)
        await fix_addons_ownership(instance_name, config, ws_send)
        await ws_send(f"\n{'=' * 50}")
        await ws_send(f"Installing {module_name} on {instance_name}...")
        await ws_send(f"{'=' * 50}\n")
        await install_or_upgrade_module(instance_name, module_name, "install", ws_send)
        await websocket.send_json({"type": "status", "status": "done"})
    except Exception as e:
        await websocket.send_json({"type": "error", "message": str(e)})
        await websocket.send_json({"type": "status", "status": "error"})
    finally:
        await websocket.close()


@app.websocket("/ws/studio-bridge/fix-misplaced")
async def ws_studio_bridge_fix_misplaced(websocket: WebSocket):
    """Fix misplaced field/model definitions by moving them to studio_customization."""
    await websocket.accept()

    config = load_config()
    if not await verify_ws_auth(websocket, config):
        await websocket.send_json({"type": "error", "message": "Authentication required"})
        await websocket.close()
        return

    try:
        data = await websocket.receive_json()
        instance_name = data.get("instance_name")
        db_name = data.get("db_name")
        client = data.get("client")
        version = data.get("version", "19")

        if not db_name or not client or not instance_name:
            raise ValueError("instance_name, db_name and client are required")

        async def ws_send(message: str):
            await websocket.send_json({"type": "log", "message": message})

        await websocket.send_json({"type": "status", "status": "running"})
        await fix_misplaced_definitions(db_name, client, version, instance_name, ws_send)
        await websocket.send_json({"type": "status", "status": "done"})
    except Exception as e:
        await websocket.send_json({"type": "error", "message": str(e)})
        await websocket.send_json({"type": "status", "status": "error"})
    finally:
        await websocket.close()


@app.websocket("/ws/studio-bridge/revert")
async def ws_studio_bridge_revert(websocket: WebSocket):
    """Revert module fields back to Studio state and restart the instance."""
    await websocket.accept()

    config = load_config()
    if not await verify_ws_auth(websocket, config):
        await websocket.send_json({"type": "error", "message": "Authentication required"})
        await websocket.close()
        return

    try:
        data = await websocket.receive_json()
        instance_name = data.get("instance_name")
        db_name = data.get("db_name")
        client = data.get("client")

        if not db_name or not client or not instance_name:
            raise ValueError("instance_name, db_name and client are required")

        async def ws_send(message: str):
            await websocket.send_json({"type": "log", "message": message})

        instance = config["instances"].get(instance_name)
        if not instance:
            raise ValueError(f"Instance {instance_name} not found")
        service = instance["service"]

        await websocket.send_json({"type": "status", "status": "running"})

        # Stop the instance before modifying the DB — the running Odoo process
        # detects registry changes and may drop custom models when it sees the
        # module was uninstalled.
        await ws_send(f"Stopping {service}...")
        await run_cmd(f"systemctl stop {service}", ws_send)

        await convert_module_to_studio(db_name, client, ws_send)
        await fix_addons_ownership(instance_name, config, ws_send)

        await ws_send(f"\nStarting {service}...")
        await run_cmd(f"systemctl start {service}", ws_send)
        await ws_send("Instance started.")
        await websocket.send_json({"type": "status", "status": "done"})
    except Exception as e:
        await websocket.send_json({"type": "error", "message": str(e)})
        await websocket.send_json({"type": "status", "status": "error"})
    finally:
        await websocket.close()


# ─── Health Check ────────────────────────────────────────────────────────────

@app.get("/api/server-info")
async def api_server_info():
    """Server info including public IP and domain."""
    config = load_config()
    return {
        "public_ip": get_public_ip(),
        "domain": config.get("domain", ""),
    }


@app.get("/api/system-stats")
async def api_system_stats():
    """CPU, RAM, and disk usage stats."""
    cpu = psutil.cpu_percent(interval=0.5)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    return {
        "cpu": {"percent": cpu, "cores": psutil.cpu_count()},
        "ram": {
            "percent": mem.percent,
            "used_gb": round(mem.used / 1073741824, 1),
            "total_gb": round(mem.total / 1073741824, 1),
        },
        "disk": {
            "percent": disk.percent,
            "used_gb": round(disk.used / 1073741824, 1),
            "total_gb": round(disk.total / 1073741824, 1),
        },
    }


@app.get("/api/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok", "setup_complete": is_setup_complete()}
