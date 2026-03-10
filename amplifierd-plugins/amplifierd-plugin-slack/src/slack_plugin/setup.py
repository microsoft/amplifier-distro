"""Slack Bridge Setup Module - guided installation and configuration.

Secrets live in keys.env, non-secret config in a plugin-local YAML file.

Provides API routes for:
- Checking setup status (what's configured, what's missing)
- Validating tokens against the Slack API
- Discovering channels for hub selection
- Persisting secrets to ~/.amplifier/keys.env (chmod 600)
- Persisting config to ~/.amplifier/plugins/slack/config.yaml
- Returning the Slack App Manifest for one-click app creation
- End-to-end connectivity test

The setup flow:
1. User creates Slack app (using manifest)
2. POST /setup/validate with bot_token + app_token
3. GET /setup/channels to pick the hub channel
4. POST /setup/configure to persist everything
5. POST /setup/test to verify end-to-end
"""

from __future__ import annotations

import copy
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import httpx
import yaml
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/setup", tags=["slack-setup"])

# Default paths
_DEFAULT_AMPLIFIER_HOME = "~/.amplifier"
_KEYS_FILENAME = "keys.env"
_PLUGIN_CONFIG_DIR = "plugins/slack"
_PLUGIN_CONFIG_FILE = "config.yaml"

# --- The Slack App Manifest (for one-click app creation) ---

_oauth_states: dict[str, float] = {}  # state_token -> expiry timestamp
_oauth_pending: dict[
    str, dict
] = {}  # state_token -> {client_id, client_secret, redirect_uri}

SLACK_APP_MANIFEST = {
    "display_information": {
        "name": "Amplifier Bridge",
        "description": "Connects Slack to Amplifier AI sessions",
        "background_color": "#1a1a2e",
    },
    "features": {
        "bot_user": {
            "display_name": "amplifier",
            "always_online": True,
        },
    },
    "oauth_config": {
        "scopes": {
            "bot": [
                "app_mentions:read",
                "channels:history",
                "channels:read",
                "chat:write",
                "reactions:read",
                "reactions:write",
                "channels:manage",
                "channels:join",
                "files:read",
                "files:write",
                "im:history",
                "im:read",
                "im:write",
            ],
        },
    },
    "settings": {
        "event_subscriptions": {
            "bot_events": [
                "app_mention",
                "message.channels",
                "message.groups",
                "message.im",
                "reaction_added",
            ],
        },
        "interactivity": {
            "is_enabled": True,
        },
        "org_deploy_enabled": False,
        "socket_mode_enabled": True,
    },
}


# --- Pydantic Models ---


class ValidateRequest(BaseModel):
    bot_token: str
    app_token: str = ""


class OAuthBeginRequest(BaseModel):
    client_id: str
    client_secret: str


class ConfigureRequest(BaseModel):
    bot_token: str
    app_token: str = ""
    signing_secret: str = ""
    hub_channel_id: str = ""
    hub_channel_name: str = "amplifier"
    socket_mode: bool = True
    client_id: str = ""
    client_secret: str = ""


class TestRequest(BaseModel):
    channel_id: str = ""


# --- Persistence helpers ---


def _amplifier_home() -> Path:
    return Path(os.environ.get("AMPLIFIER_HOME", _DEFAULT_AMPLIFIER_HOME)).expanduser()


def _keys_path() -> Path:
    return _amplifier_home() / _KEYS_FILENAME


def _config_path() -> Path:
    return _amplifier_home() / _PLUGIN_CONFIG_DIR / _PLUGIN_CONFIG_FILE


def load_keys() -> dict[str, Any]:
    """Load ~/.amplifier/keys.env (.env format)."""
    path = _keys_path()
    if not path.exists():
        return {}
    result: dict[str, Any] = {}
    try:
        for raw_line in path.read_text().splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            if key:
                result[key] = value
    except OSError:
        logger.warning("Failed to read keys.env", exc_info=True)
    return result


def _save_keys(updates: dict[str, str]) -> None:
    """Merge updates into keys.env (chmod 600, .env format)."""
    path = _keys_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    # Read existing lines, update matching keys, append new ones
    lines: list[str] = []
    found_keys: set[str] = set()
    if path.exists():
        for raw_line in path.read_text().splitlines():
            stripped = raw_line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                existing_key, _, _ = stripped.partition("=")
                existing_key = existing_key.strip()
                if existing_key in updates and updates.get(existing_key):
                    lines.append(f'{existing_key}="{updates[existing_key]}"')
                    found_keys.add(existing_key)
                    continue
            lines.append(raw_line)

    # Append keys not already found
    for key, value in updates.items():
        if value and key not in found_keys:
            lines.append(f'{key}="{value}"')

    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)


def _load_slack_config() -> dict[str, Any]:
    """Load plugin-local slack config from YAML."""
    path = _config_path()
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text())
        return data if isinstance(data, dict) else {}
    except (yaml.YAMLError, OSError):
        logger.warning("Failed to read slack config", exc_info=True)
        return {}


def _save_slack_config(**kwargs: Any) -> None:
    """Persist slack config fields to plugin-local YAML."""
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    existing = _load_slack_config()
    existing.update({k: v for k, v in kwargs.items() if v is not None})

    path.write_text(yaml.dump(existing, default_flow_style=False, sort_keys=False))


# --- Slack API helpers ---


async def _slack_api(method: str, token: str, **kwargs: Any) -> dict[str, Any]:
    """Call a Slack Web API method and return the response."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://slack.com/api/{method}",
            headers={"Authorization": f"Bearer {token}"},
            json=kwargs if kwargs else None,
            timeout=15.0,
        )
        data = resp.json()
        return data


async def _validate_bot_token(token: str) -> dict[str, Any]:
    """Validate a bot token via auth.test."""
    data = await _slack_api("auth.test", token)
    if not data.get("ok"):
        return {"valid": False, "error": data.get("error", "unknown")}
    return {
        "valid": True,
        "team": data.get("team"),
        "team_id": data.get("team_id"),
        "user": data.get("user"),
        "user_id": data.get("user_id"),
        "bot_id": data.get("bot_id"),
    }


async def _validate_app_token(token: str) -> dict[str, Any]:
    """Validate an app token via apps.connections.open (dry run)."""
    data = await _slack_api("apps.connections.open", token)
    if not data.get("ok"):
        return {"valid": False, "error": data.get("error", "unknown")}
    return {"valid": True}


async def _list_channels(token: str, *, limit: int = 200) -> list[dict[str, Any]]:
    """List public channels the bot can see."""
    data = await _slack_api(
        "conversations.list",
        token,
        types="public_channel",
        limit=limit,
        exclude_archived=True,
    )
    if not data.get("ok"):
        return []
    channels = data.get("channels", [])
    return [
        {
            "id": ch["id"],
            "name": ch.get("name", ""),
            "is_member": ch.get("is_member", False),
            "num_members": ch.get("num_members", 0),
            "topic": ch.get("topic", {}).get("value", ""),
        }
        for ch in channels
    ]


# --- OAuth helpers ---


def _create_oauth_state() -> str:
    """Create a CSRF state token with 10-minute TTL, pruning expired entries."""
    now = time.time()
    expired = [k for k, v in _oauth_states.items() if v < now]
    for k in expired:
        _oauth_states.pop(k, None)
        _oauth_pending.pop(k, None)

    state = secrets.token_urlsafe(32)
    _oauth_states[state] = now + 600  # 10 minutes
    return state


def _validate_oauth_state(state: str) -> bool:
    """Validate and consume a CSRF state token. Returns False if missing/expired."""
    expiry = _oauth_states.pop(state, None)
    if expiry is None:
        return False
    return time.time() < expiry


def _error_page(message: str) -> HTMLResponse:
    """Return a styled HTML error page for OAuth failures."""
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Setup Error — Amplifier Slack</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<script src="/static/theme-init.js"></script>
<link rel="stylesheet" href="/static/amplifier-theme.css">
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ display: flex; align-items: center; justify-content: center;
          min-height: 100vh; font-family: var(--font-body);
          background: var(--canvas); color: var(--ink); padding: 20px; }}
  .card {{ background: var(--canvas-warm); border: 1px solid var(--canvas-mist);
           border-radius: var(--radius-card); padding: 48px 40px;
           max-width: 480px; width: 100%; box-shadow: var(--shadow-lift);
           text-align: center; }}
  h2 {{ font-size: 20px; font-weight: 700; color: var(--error); margin-bottom: 16px; }}
  p {{ color: var(--ink-slate); margin-bottom: 28px; font-size: 15px; line-height: 1.6; }}
  a {{ display: inline-flex; align-items: center; gap: 6px; color: var(--signal);
       text-decoration: none; font-weight: 600; font-size: 14px; }}
  a:hover {{ text-decoration: underline; }}
</style>
</head>
<body>
<div class="card">
  <h1 class="wordmark wordmark-sm">amplifier</h1>
  <h2 style="margin-top:16px;">Setup Error</h2>
  <p>{message}</p>
  <a href="/slack/setup-ui">&#8592; Back to Setup</a>
</div>
</body>
</html>"""
    return HTMLResponse(content=html, status_code=400)


# --- Routes ---


@router.get("/status")
async def setup_status() -> dict[str, Any]:
    """Check what's configured and what's missing."""
    keys = load_keys()
    cfg = _load_slack_config()

    bot_token = os.environ.get("SLACK_BOT_TOKEN", "") or keys.get("SLACK_BOT_TOKEN", "")
    app_token = os.environ.get("SLACK_APP_TOKEN", "") or keys.get("SLACK_APP_TOKEN", "")
    hub_channel_id = os.environ.get("SLACK_HUB_CHANNEL_ID", "") or cfg.get(
        "hub_channel_id", ""
    )
    sm_env = os.environ.get("SLACK_SOCKET_MODE", "")
    socket_mode = (
        sm_env.lower() in ("1", "true", "yes")
        if sm_env
        else cfg.get("socket_mode", False)
    )

    steps = {
        "bot_token": bool(bot_token),
        "app_token": bool(app_token),
        "hub_channel": bool(hub_channel_id),
        "socket_mode": socket_mode,
        "keys_persisted": bool(keys.get("SLACK_BOT_TOKEN")),
        "config_persisted": bool(cfg.get("hub_channel_id")),
    }
    all_required = steps["bot_token"] and steps["hub_channel"]
    if socket_mode:
        all_required = all_required and steps["app_token"]

    return {
        "configured": all_required,
        "steps": steps,
        "keys_path": str(_keys_path()),
        "config_path": str(_config_path()),
        "mode": "socket"
        if socket_mode and app_token
        else "events-api"
        if bot_token
        else "unconfigured",
    }


@router.post("/validate")
async def validate_tokens(req: ValidateRequest) -> dict[str, Any]:
    """Validate Slack tokens against the API."""
    if not req.bot_token.startswith("xoxb-"):
        raise HTTPException(
            status_code=400,
            detail="Bot token must start with 'xoxb-'. "
            "Find it at: OAuth & Permissions > Bot User OAuth Token",
        )

    result: dict[str, Any] = {}
    result["bot_token"] = await _validate_bot_token(req.bot_token)

    if req.app_token:
        if not req.app_token.startswith("xapp-"):
            raise HTTPException(
                status_code=400,
                detail="App token must start with 'xapp-'. "
                "Find it at: Basic Information > App-Level Tokens, "
                "or enable Socket Mode to generate one.",
            )
        result["app_token"] = await _validate_app_token(req.app_token)
    else:
        result["app_token"] = {"valid": False, "error": "not_provided"}

    result["all_valid"] = result["bot_token"]["valid"] and (
        not req.app_token or result["app_token"]["valid"]
    )

    return result


@router.get("/channels")
async def list_channels(bot_token: str = "") -> dict[str, Any]:
    """List channels visible to the bot for hub channel selection."""
    keys = load_keys()
    token = (
        bot_token
        or os.environ.get("SLACK_BOT_TOKEN", "")
        or keys.get("SLACK_BOT_TOKEN", "")
    )
    if not token:
        raise HTTPException(
            status_code=400,
            detail="No bot token available. Validate tokens first.",
        )

    channels = await _list_channels(token)
    channels.sort(key=lambda c: (not c["is_member"], c["name"]))

    return {
        "channels": channels,
        "count": len(channels),
        "tip": "Choose a channel for the Amplifier hub. "
        "The bot must be invited to it (/invite @amplifier).",
    }


@router.post("/configure")
async def configure(req: ConfigureRequest) -> dict[str, Any]:
    """Save Slack secrets to keys.env and config to plugin config.

    Secrets and config in standard locations.
    Also sets environment variables for the current process.
    """
    # 1. Persist secrets to keys.env
    _save_keys(
        {
            "SLACK_BOT_TOKEN": req.bot_token,
            "SLACK_APP_TOKEN": req.app_token,
            "SLACK_SIGNING_SECRET": req.signing_secret,
            "SLACK_CLIENT_ID": req.client_id,
            "SLACK_CLIENT_SECRET": req.client_secret,
        }
    )

    # 2. Persist config to plugin-local YAML
    _save_slack_config(
        hub_channel_name=req.hub_channel_name,
        socket_mode=req.socket_mode,
        hub_channel_id=req.hub_channel_id or None,
    )

    # 3. Set env vars for current process (bridge reads from env)
    env_map = {
        "SLACK_BOT_TOKEN": req.bot_token,
        "SLACK_APP_TOKEN": req.app_token,
        "SLACK_SIGNING_SECRET": req.signing_secret,
        "SLACK_HUB_CHANNEL_ID": req.hub_channel_id,
        "SLACK_HUB_CHANNEL_NAME": req.hub_channel_name,
        "SLACK_SOCKET_MODE": "true" if req.socket_mode else "false",
    }
    for key, value in env_map.items():
        if value:
            os.environ[key] = value

    return {
        "status": "saved",
        "keys_path": str(_keys_path()),
        "config_path": str(_config_path()),
        "mode": "socket" if req.socket_mode else "events-api",
    }


@router.post("/test")
async def test_connection(req: TestRequest) -> dict[str, Any]:
    """Send a test message to verify end-to-end connectivity."""
    keys = load_keys()
    cfg = _load_slack_config()

    token = os.environ.get("SLACK_BOT_TOKEN", "") or keys.get("SLACK_BOT_TOKEN", "")
    channel = (
        req.channel_id
        or os.environ.get("SLACK_HUB_CHANNEL_ID", "")
        or cfg.get("hub_channel_id", "")
    )

    if not token:
        raise HTTPException(status_code=400, detail="No bot token configured")
    if not channel:
        raise HTTPException(status_code=400, detail="No channel specified")

    data = await _slack_api(
        "chat.postMessage",
        token,
        channel=channel,
        text="Amplifier Bridge connected. Setup complete.",
    )

    if not data.get("ok"):
        error = data.get("error", "unknown")
        hints: dict[str, str] = {
            "channel_not_found": "Channel ID is wrong or bot isn't in the channel. "
            "Try: /invite @amplifier in the channel.",
            "not_in_channel": "Bot needs to be invited: /invite @amplifier",
            "invalid_auth": "Bot token is invalid or expired.",
            "missing_scope": "Bot token is missing 'chat:write' scope. "
            "Add it in OAuth & Permissions, then reinstall the app.",
        }
        return {
            "success": False,
            "error": error,
            "hint": hints.get(error, f"Slack API error: {error}"),
        }

    return {
        "success": True,
        "channel": channel,
        "message_ts": data.get("ts"),
        "message": "Test message sent. Check the channel in Slack.",
    }


@router.get("/manifest")
async def get_manifest(request: Request) -> dict[str, Any]:
    """Return the Slack App Manifest with a dynamic redirect_url injected."""
    manifest = copy.deepcopy(SLACK_APP_MANIFEST)
    base = str(request.base_url).rstrip("/")
    callback = f"{base}/slack/setup/oauth/callback"
    manifest["oauth_config"]["redirect_urls"] = [callback]

    manifest_yaml = yaml.dump(manifest, default_flow_style=False, sort_keys=False)
    create_url = "https://api.slack.com/apps?new_app=1&manifest_yaml=" + quote(
        manifest_yaml
    )

    return {
        "manifest": manifest,
        "manifest_yaml": manifest_yaml,
        "create_url": create_url,
    }


@router.post("/oauth/begin")
async def oauth_begin(req: OAuthBeginRequest, request: Request) -> JSONResponse:
    """Start the OAuth install flow — returns a Slack authorize URL.

    Enforces HTTPS because Slack only allows HTTPS redirect URIs.
    """
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    if scheme != "https":
        return JSONResponse(
            {
                "error": (
                    "OAuth requires HTTPS. Restart with: amp-distro serve --tls auto"
                )
            },
            status_code=400,
        )

    state = _create_oauth_state()
    base = str(request.base_url).rstrip("/")
    redirect_uri = f"{base}/slack/setup/oauth/callback"
    _oauth_pending[state] = {
        "client_id": req.client_id,
        "client_secret": req.client_secret,
        "redirect_uri": redirect_uri,
    }

    scopes = ",".join(SLACK_APP_MANIFEST["oauth_config"]["scopes"]["bot"])
    auth_url = (
        f"https://slack.com/oauth/v2/authorize"
        f"?client_id={req.client_id}"
        f"&scope={scopes}"
        f"&redirect_uri={redirect_uri}"
        f"&state={state}"
    )
    return JSONResponse({"authorize_url": auth_url})


@router.get("/oauth/callback")
async def oauth_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
) -> HTMLResponse | RedirectResponse:
    """Handle Slack's OAuth redirect — exchange code for bot token."""
    if error:
        return _error_page(f"Slack returned an error: {error}")

    if not _validate_oauth_state(state):
        return _error_page("Invalid or expired state. Please try again.")

    pending = _oauth_pending.pop(state, None)
    if not pending:
        return _error_page("Session expired. Please restart the OAuth flow.")

    # Exchange authorization code for bot token
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://slack.com/api/oauth.v2.access",
            data={
                "client_id": pending["client_id"],
                "client_secret": pending["client_secret"],
                "code": code,
                "redirect_uri": pending["redirect_uri"],
            },
            timeout=15.0,
        )
        data = resp.json()

    if not data.get("ok"):
        return _error_page(f"Token exchange failed: {data.get('error', 'unknown')}")

    bot_token = data.get("access_token", "")
    team_name = data.get("team", {}).get("name", "")
    app_id = data.get("app_id", "")
    team_id = data.get("team", {}).get("id", "")

    # Persist bot token and client credentials to keys.env
    _save_keys(
        {
            "SLACK_BOT_TOKEN": bot_token,
            "SLACK_CLIENT_ID": pending["client_id"],
            "SLACK_CLIENT_SECRET": pending["client_secret"],
        }
    )
    os.environ["SLACK_BOT_TOKEN"] = bot_token

    params = urlencode(
        {"oauth": "success", "team": team_name, "app_id": app_id, "team_id": team_id}
    )
    return RedirectResponse(f"/slack/setup-ui?{params}")
