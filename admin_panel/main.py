"""
Odoo Deployment Platform - FastAPI Admin Panel
Main application with setup wizard, instance management, and deployment.
"""
import asyncio
import json
import os
import re
import subprocess
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from config import load_config, save_config, PLATFORM_DIR
from setup_steps import (
    SETUP_STEPS, STEP_ORDER, STEP_DEPS, VERSION_STEPS,
    is_step_unlocked, get_installed_versions, odoo_base_dir,
    create_odoo_instance, delete_odoo_instance, sync_instance_from_prod, run_cmd,
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
    })


@app.get("/deploy", response_class=HTMLResponse)
async def deploy_page(request: Request):
    """Deployment page."""
    config = load_config()
    return templates.TemplateResponse("deploy.html", {
        "request": request,
        "config": config,
    })


@app.get("/databases", response_class=HTMLResponse)
async def databases_page(request: Request):
    """Database management page."""
    config = load_config()
    return templates.TemplateResponse("databases.html", {
        "request": request,
        "config": config,
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
    allowed_keys = {"domain", "github_token", "custom_addons_repos"}
    for key in data:
        if key in allowed_keys:
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
        instance["restart_needed"] = True
        save_config(config)

    return {"status": "ok"}


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
    """Deploy: git pull custom addons, copy to instance, restart."""
    data = await request.json()
    instance_name = data.get("instance")
    modules = data.get("modules", "all")

    config = load_config()
    instance = config["instances"].get(instance_name)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    version = instance.get("version", "19")
    base = odoo_base_dir(version)
    shared_addons = f"{base}/custom-addons"
    instance_addons = f"{base}/data/{instance_name}/addons"

    # Git pull shared custom addons
    git_output = ""
    if Path(f"{shared_addons}/.git").exists():
        result = subprocess.run(
            ["git", "-C", shared_addons, "pull"],
            capture_output=True, text=True, timeout=60
        )
        git_output = result.stdout
    else:
        git_output = "(no custom-addons repo configured)"

    # Copy to instance addons dir
    if Path(shared_addons).exists():
        subprocess.run(
            ["rsync", "-a", "--exclude=.git", f"{shared_addons}/", f"{instance_addons}/"],
            capture_output=True, timeout=60
        )
        subprocess.run(
            ["chown", "-R", "odoo:odoo", instance_addons],
            capture_output=True, timeout=10
        )

    # Restart the service to pick up changes
    service = instance["service"]
    subprocess.run(["systemctl", "restart", service], capture_output=True, timeout=30)

    return {
        "status": "ok",
        "git_output": git_output,
        "instance": instance_name,
    }


# ─── WebSocket for Setup Steps ──────────────────────────────────────────────

@app.websocket("/ws/setup/{step_id}")
async def ws_setup_step(websocket: WebSocket, step_id: str):
    """WebSocket endpoint for running setup steps with live output."""
    await websocket.accept()

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
