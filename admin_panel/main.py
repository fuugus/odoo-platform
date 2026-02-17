"""
Odoo Deployment Platform - FastAPI Admin Panel
Main application with setup wizard, instance management, and deployment.
"""
import asyncio
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from config import load_config, save_config, PLATFORM_DIR
from auth import (
    is_auth_enabled, get_current_user, is_path_exempt,
    get_google_auth_url, exchange_code_for_user, generate_state, verify_ws_auth,
)
from setup_steps import (
    SETUP_STEPS, STEP_ORDER, STEP_DEPS, VERSION_STEPS,
    is_step_unlocked, get_installed_versions, odoo_base_dir,
    create_odoo_instance, delete_odoo_instance, sync_instance_from_prod, run_cmd,
    _generate_password,
)
from admin_pw import verify_admin_pw, reset_admin_pw, normalize_superuser
from studio_bridge import (
    get_custom_addons_info, get_instance_addons, get_studio_stats,
    export_studio_to_module, convert_module_to_studio, install_or_upgrade_module,
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

app = FastAPI(title="Odoo Deployment Platform", version="1.0.0")

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
        instances[name] = {**inst, "status": status}

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
        instances[name] = {**inst, "status": status}
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


@app.post("/api/instances/{instance_name}/ssh-key")
async def api_set_ssh_key(instance_name: str, request: Request):
    """Set SSH public key for a dev instance."""
    config = load_config()
    instance = config["instances"].get(instance_name)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")
    ssh_user = instance.get("ssh_user")
    if not ssh_user:
        raise HTTPException(status_code=400, detail="Not a dev instance")

    data = await request.json()
    public_key = data.get("public_key", "").strip()
    if not public_key or not public_key.startswith("ssh-"):
        raise HTTPException(status_code=400, detail="Invalid SSH public key")

    version = instance.get("version", "19")
    data_dir = f"{odoo_base_dir(version)}/data/{instance_name}"
    auth_keys = f"{data_dir}/.ssh/authorized_keys"

    with open(auth_keys, "w") as f:
        f.write(public_key + "\n")

    instance["ssh_key_set"] = True
    save_config(config)
    return {"status": "ok"}


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
    subprocess.run(["git", "-C", addons, "add", "-A"], capture_output=True, timeout=10)
    status = subprocess.run(
        ["git", "-C", addons, "status", "--porcelain"],
        capture_output=True, text=True, timeout=10,
    )
    if not status.stdout.strip():
        return {"status": "ok", "message": "Nothing to sync — already up to date"}

    subprocess.run(
        ["git", "-C", addons, "commit", "-m", f"Sync from {instance_name}"],
        capture_output=True, text=True, timeout=10,
    )
    push = subprocess.run(
        ["git", "-C", addons, "push", "--force"],
        capture_output=True, text=True, timeout=30,
    )
    if push.returncode != 0:
        raise HTTPException(status_code=500, detail=push.stderr)

    return {"status": "ok", "message": "Addons synced to repo"}


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
    git_output = ""
    if Path(f"{instance_addons}/.git").exists():
        result = subprocess.run(
            ["git", "-C", instance_addons, "pull"],
            capture_output=True, text=True, timeout=30,
        )
        git_output = result.stdout.strip() or result.stderr.strip()
        owner = instance.get("ssh_user", "odoo")
        subprocess.run(
            ["chown", "-R", f"{owner}:odoo", instance_addons],
            capture_output=True, timeout=10,
        )
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


@app.get("/api/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok", "setup_complete": is_setup_complete()}
