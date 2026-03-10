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
import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/setup", tags=["slack-setup"])

# Default paths
_DEFAULT_AMPLIFIER_HOME = "~/.amplifier"
_KEYS_FILENAME = "keys.env"
_PLUGIN_CONFIG_DIR = "plugins/slack"
_PLUGIN_CONFIG_FILE = "config.yaml"

# --- The Slack App Manifest (for one-click app creation) ---

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


class ConfigureRequest(BaseModel):
    bot_token: str
    app_token: str = ""
    signing_secret: str = ""
    hub_channel_id: str = ""
    hub_channel_name: str = "amplifier"
    socket_mode: bool = True


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
async def get_manifest() -> dict[str, Any]:
    """Return the Slack App Manifest for one-click app creation."""
    manifest_yaml = yaml.dump(
        SLACK_APP_MANIFEST, default_flow_style=False, sort_keys=False
    )
    return {
        "manifest": SLACK_APP_MANIFEST,
        "manifest_yaml": manifest_yaml,
        "instructions": (
            "1. Go to https://api.slack.com/apps\n"
            "2. Click 'Create New App' > 'From a manifest'\n"
            "3. Select your workspace\n"
            "4. Choose YAML format and paste the manifest\n"
            "5. Click 'Create'\n"
            "6. Go to 'Install App' and install to your workspace\n"
            "7. Copy the Bot Token (xoxb-...) from OAuth & Permissions\n"
            "8. Copy the App Token (xapp-...) from Basic Information\n"
            "   > App-Level Tokens (create one with 'connections:write')\n"
            "9. Use /setup/configure to save both tokens"
        ),
        "create_url": "https://api.slack.com/apps?new_app=1",
    }


@router.post("/automate")
async def automate_setup(request: Request) -> dict[str, Any]:
    """Launch browser automation to create and configure the Slack app."""
    from .setup_automation import automate_slack_setup, check_agent_browser

    if not check_agent_browser():
        return JSONResponse(
            {
                "error": "agent-browser not installed",
                "install_cmd": "npm install -g agent-browser && agent-browser install",
            },
            status_code=424,
        )

    manifest = copy.deepcopy(SLACK_APP_MANIFEST)
    manifest_yaml_str = yaml.dump(manifest, default_flow_style=False, sort_keys=False)
    result = await automate_slack_setup(manifest_yaml_str)

    if result.success:
        _save_keys(
            {
                "SLACK_BOT_TOKEN": result.bot_token,
                "SLACK_APP_TOKEN": result.app_token,
            }
        )
        os.environ["SLACK_BOT_TOKEN"] = result.bot_token
        os.environ["SLACK_APP_TOKEN"] = result.app_token
        return {"success": True, "message": "Slack app created and configured!"}

    return {
        "success": False,
        "error": result.error,
        "step_reached": result.step_reached,
        "partial_tokens": {
            "bot_token": bool(result.bot_token),
            "app_token": bool(result.app_token),
        },
    }


@router.get("/automate/status")
async def automate_status(request: Request) -> Any:
    """SSE stream of automation progress; falls back to plain JSON."""
    from .setup_automation import get_automation_status

    try:
        from sse_starlette.sse import EventSourceResponse

        async def _generate():
            while True:
                status = get_automation_status()
                yield {"event": "progress", "data": json.dumps(status)}
                if status.get("complete"):
                    break
                import asyncio

                await asyncio.sleep(1)

        return EventSourceResponse(_generate())
    except ImportError:
        return get_automation_status()
