"""
Google OAuth authentication for the admin panel.
"""
import secrets
import urllib.parse

import httpx
from starlette.requests import Request
from starlette.websockets import WebSocket

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"

EXEMPT_PREFIXES = ("/login", "/auth/", "/api/health", "/static/")


def is_auth_enabled(config: dict) -> bool:
    auth = config.get("auth", {})
    return bool(auth.get("google_client_id") and auth.get("google_client_secret"))


def get_current_user(request: Request) -> dict | None:
    return request.session.get("user")


def get_google_auth_url(config: dict, redirect_uri: str, state: str) -> str:
    auth = config.get("auth", {})
    params = {
        "client_id": auth["google_client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "online",
        "state": state,
        "prompt": "select_account",
    }
    return f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"


async def exchange_code_for_user(code: str, config: dict, redirect_uri: str) -> dict | None:
    auth = config.get("auth", {})
    async with httpx.AsyncClient() as client:
        token_resp = await client.post(GOOGLE_TOKEN_URL, data={
            "code": code,
            "client_id": auth["google_client_id"],
            "client_secret": auth["google_client_secret"],
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        })
        if token_resp.status_code != 200:
            return None
        tokens = token_resp.json()
        access_token = tokens.get("access_token")
        if not access_token:
            return None

        user_resp = await client.get(GOOGLE_USERINFO_URL, headers={
            "Authorization": f"Bearer {access_token}",
        })
        if user_resp.status_code != 200:
            return None
        return user_resp.json()


def generate_state() -> str:
    return secrets.token_urlsafe(32)


def is_path_exempt(path: str) -> bool:
    return any(path.startswith(p) for p in EXEMPT_PREFIXES)


async def verify_ws_auth(websocket: WebSocket, config: dict) -> bool:
    if not is_auth_enabled(config):
        return True
    user = websocket.session.get("user")
    return bool(user)
