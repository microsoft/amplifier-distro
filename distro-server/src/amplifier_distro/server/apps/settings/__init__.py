"""Settings App - post-setup configuration management.

Provides the settings dashboard and API for managing features,
providers, tiers, and bridges after initial setup is complete.

Routes:
    GET  /          - Settings dashboard page
    GET  /status    - Current setup state (phase, features, provider, bridges)
    GET  /providers - Provider catalog with configuration status
    POST /features  - Toggle a feature on/off
    POST /tier      - Set feature tier level
    POST /provider  - Add/update provider (key entry or use existing key)
    GET  /bridges   - Bridge configuration status
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from amplifier_distro import distro_settings, overlay
from amplifier_distro.conventions import (
    AMPLIFIER_HOME,
    KEYS_FILENAME,
    SETTINGS_FILENAME,
)
from amplifier_distro.features import (
    FEATURES,
    PROVIDERS,
    get_provider_catalog,
    handle_provider_request,
    provider_bundle_uri,
)
from amplifier_distro.server.app import AppManifest

# Bridge env-var / keys.env lookups used by detect_bridges()
_BRIDGE_DEFS: dict[str, dict[str, Any]] = {
    "slack": {
        "name": "Slack",
        "description": "Connect Slack channels to Amplifier sessions",
        "required_keys": ["SLACK_BOT_TOKEN"],
        "optional_keys": ["SLACK_APP_TOKEN"],
        "setup_url": "/apps/slack/setup/status",
    },
    "voice": {
        "name": "Voice",
        "description": "Real-time voice conversations via OpenAI Realtime API",
        "required_keys": ["OPENAI_API_KEY"],
        "optional_keys": [],
        "setup_url": "/apps/voice/",
    },
}

router = APIRouter()

_static_dir = Path(__file__).parent / "static"


# --- Pydantic Models ---


class FeatureToggle(BaseModel):
    feature_id: str
    enabled: bool


class TierRequest(BaseModel):
    tier: int


class ProviderRequest(BaseModel):
    provider: str = ""
    api_key: str = ""


# --- Helpers ---


def _amplifier_home() -> Path:
    return Path(AMPLIFIER_HOME).expanduser()


def _settings_path() -> Path:
    return _amplifier_home() / SETTINGS_FILENAME


def _keys_path() -> Path:
    return _amplifier_home() / KEYS_FILENAME


def _has_any_provider_key() -> bool:
    """Check if any provider API key is available in environment."""
    return any(bool(os.environ.get(p.env_var)) for p in PROVIDERS.values())


def compute_phase() -> str:
    """Compute current setup phase.

    Returns:
        "unconfigured" - no overlay bundle OR no provider key in env
        "ready"        - overlay bundle exists AND at least one provider key
    """
    if not overlay.overlay_exists():
        return "unconfigured"
    if not _has_any_provider_key():
        return "unconfigured"
    return "ready"


def persist_api_key(provider_id: str, api_key: str) -> None:
    """Write an API key to keys.env (.env format, chmod 600).

    Uses the same format as amplifier CLI's KeyManager:
    ``KEY="value"`` lines in ``~/.amplifier/keys.env``.
    Existing keys are preserved; the target key is added or updated.
    """
    provider = PROVIDERS[provider_id]
    keys_path = _keys_path()
    keys_path.parent.mkdir(parents=True, exist_ok=True)

    key_name = provider.env_var

    # Read existing lines, update or append
    lines: list[str] = []
    found = False
    if keys_path.exists():
        for raw_line in keys_path.read_text().splitlines():
            stripped = raw_line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                existing_key, _, _ = stripped.partition("=")
                if existing_key.strip() == key_name:
                    lines.append(f'{key_name}="{api_key}"')
                    found = True
                    continue
            lines.append(raw_line)

    if not found:
        lines.append(f'{key_name}="{api_key}"')

    keys_path.write_text("\n".join(lines) + "\n")
    with contextlib.suppress(OSError):
        keys_path.chmod(0o600)  # Windows may not support this

    # Also set in current process
    os.environ[key_name] = api_key


def add_provider_config(provider_id: str) -> None:
    """Add a provider to config.providers[] in settings.yaml (additive).

    Skips if the provider module is already listed. Stores API keys
    as ``${VAR}`` placeholders (never raw values) to match the
    amplifier CLI convention.
    """
    provider = PROVIDERS[provider_id]
    settings_path = _settings_path()
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    settings: dict = {}
    if settings_path.exists():
        settings = yaml.safe_load(settings_path.read_text()) or {}

    config = settings.setdefault("config", {})
    providers_list: list[dict] = config.setdefault("providers", [])

    # Check if this provider module is already configured
    for entry in providers_list:
        if entry.get("module") == provider.module_id:
            return  # Already present, don't duplicate

    # Add the new provider
    new_entry: dict[str, Any] = {
        "module": provider.module_id,
        "config": {
            "default_model": provider.default_model,
            "api_key": f"${{{provider.env_var}}}",
            "priority": 1,
        },
    }
    if provider.source_url:
        new_entry["source"] = provider.source_url

    # Demote existing priority-1 providers to priority 10
    for existing in providers_list:
        existing_config = existing.get("config", {})
        if existing_config.get("priority") == 1:
            existing_config["priority"] = 10

    providers_list.append(new_entry)

    settings_path.write_text(
        yaml.dump(settings, default_flow_style=False, sort_keys=False)
    )


def load_keys() -> dict[str, str]:
    """Load keys.env if it exists, returning a dict of key=value pairs."""
    keys_path = _keys_path()
    if not keys_path.exists():
        return {}
    result: dict[str, str] = {}
    try:
        for raw_line in keys_path.read_text().splitlines():
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
        pass
    return result


def detect_bridges() -> dict[str, Any]:
    """Detect configuration status of all known bridges.

    Checks env vars first, then keys.env.
    """
    keys = load_keys()
    bridges: dict[str, Any] = {}

    for bid, defn in _BRIDGE_DEFS.items():
        present: list[str] = []
        missing: list[str] = []
        for k in defn["required_keys"]:
            if os.environ.get(k) or keys.get(k):
                present.append(k)
            else:
                missing.append(k)

        configured = len(missing) == 0
        bridges[bid] = {
            "name": defn["name"],
            "description": defn["description"],
            "configured": configured,
            "missing_keys": missing,
            "setup_url": defn["setup_url"],
        }
    return bridges


def _get_enabled_features() -> list[str]:
    """Return IDs of features currently included in the overlay bundle."""
    current_uris = set(overlay.get_includes())
    enabled = []
    for fid, feature in FEATURES.items():
        if all(inc in current_uris for inc in feature.includes):
            enabled.append(fid)
    return enabled


def _get_current_provider() -> str | None:
    """Return the current provider ID from the overlay, or None."""
    current_uris = set(overlay.get_includes())
    for pid, provider in PROVIDERS.items():
        if provider_bundle_uri(provider) in current_uris:
            return pid
    return None


def _build_status() -> dict[str, Any]:
    """Build the full status response."""
    phase = compute_phase()
    provider = _get_current_provider()
    enabled = set(_get_enabled_features())

    features: dict[str, Any] = {}
    for fid, feature in FEATURES.items():
        features[fid] = {
            "enabled": fid in enabled,
            "tier": feature.tier,
            "name": feature.name,
            "description": feature.description,
        }

    return {
        "phase": phase,
        "provider": provider,
        "features": features,
        "bridges": detect_bridges(),
    }


# --- HTML Pages ---


@router.get("/", response_class=HTMLResponse)
async def settings_page() -> HTMLResponse:
    """Serve the settings dashboard."""
    html_file = _static_dir / "settings.html"
    if html_file.exists():
        return HTMLResponse(content=html_file.read_text())
    return HTMLResponse(
        content="<h1>Settings</h1><p>settings.html not found.</p>",
        status_code=500,
    )


# --- API Routes ---


@router.get("/status")
async def get_status() -> dict[str, Any]:
    """Current setup state (computed from filesystem, never stored)."""
    return _build_status()


@router.post("/features")
async def toggle_feature(req: FeatureToggle) -> dict[str, Any]:
    """Toggle a feature on or off."""
    if req.feature_id not in FEATURES:
        raise HTTPException(
            status_code=400, detail=f"Unknown feature: {req.feature_id}"
        )

    feature = FEATURES[req.feature_id]
    if req.enabled:
        # Add dependencies first
        for req_id in feature.requires:
            dep = FEATURES[req_id]
            for inc in dep.includes:
                overlay.add_include(inc)
        for inc in feature.includes:
            overlay.add_include(inc)
    else:
        for inc in feature.includes:
            overlay.remove_include(inc)

    return _build_status()


@router.post("/tier")
async def set_tier(req: TierRequest) -> dict[str, Any]:
    """Set feature tier level."""
    from amplifier_distro.features import features_for_tier

    needed = features_for_tier(req.tier)
    current = set(_get_enabled_features())
    for fid in needed:
        if fid not in current:
            feature = FEATURES[fid]
            for dep_id in feature.requires:
                dep = FEATURES[dep_id]
                for inc in dep.includes:
                    overlay.add_include(inc)
            for inc in feature.includes:
                overlay.add_include(inc)

    return _build_status()


@router.get("/providers")
async def get_providers() -> dict[str, Any]:
    """Provider catalog with configuration status."""
    return {"providers": get_provider_catalog()}


@router.post("/provider")
async def change_provider(req: ProviderRequest) -> dict[str, Any]:
    """Add or update a provider configuration.

    Two modes:
    - **Explicit key**: ``api_key`` provided — register that provider.
    - **Use existing key**: ``provider`` set, no ``api_key`` — look up key
      from environment or keys.env and register.
    """
    result = handle_provider_request(provider=req.provider, api_key=req.api_key)
    if result["status"] == "error":
        raise HTTPException(status_code=400, detail=str(result["detail"]))
    return result


@router.get("/bridges")
async def get_bridges() -> dict[str, Any]:
    """Status of all communication bridges (Slack, Voice)."""
    return {"bridges": detect_bridges()}


# --- Distro Settings CRUD ---


class DistroSettingsUpdate(BaseModel):
    """Partial update for distro settings. All fields optional."""

    workspace_root: str | None = None
    identity: dict[str, Any] | None = None
    backup: dict[str, Any] | None = None
    slack: dict[str, Any] | None = None
    voice: dict[str, Any] | None = None
    watchdog: dict[str, Any] | None = None


@router.get("/distro-settings")
async def get_distro_settings() -> dict[str, Any]:
    """Read all distro settings."""
    from dataclasses import asdict

    settings = distro_settings.load()
    return {"settings": asdict(settings), "path": str(distro_settings._settings_path())}


@router.post("/distro-settings")
async def update_distro_settings(req: DistroSettingsUpdate) -> dict[str, Any]:
    """Update distro settings (partial merge)."""
    from dataclasses import asdict

    if req.workspace_root is not None:
        distro_settings.update(workspace_root=req.workspace_root)
    for section_name in ("identity", "backup", "slack", "voice", "watchdog"):
        section_data = getattr(req, section_name)
        if section_data is not None:
            distro_settings.update(section_name, **section_data)

    settings = distro_settings.load()
    return {"settings": asdict(settings), "path": str(distro_settings._settings_path())}


manifest = AppManifest(
    name="settings",
    description="Settings dashboard and configuration management",
    version="0.1.0",
    router=router,
)
