"""Install Wizard App - Multi-step setup wizard.

Handles initial Amplifier setup only. Post-setup configuration
management (features, tiers, provider changes) lives in the
settings app.

Routes:
    GET  /             - Setup wizard
    GET  /detect       - Auto-detect environment
    GET  /modules      - Feature/module catalog
    GET  /providers    - Provider catalog with config status
    POST /steps/welcome   - Save identity + workspace
    POST /steps/config    - Save cache/preflight
    POST /steps/modules   - Toggle features in overlay
    POST /steps/interfaces - CLI/TUI install
    POST /steps/provider  - Save API key + provider
    POST /steps/verify    - Final verification
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from amplifier_distro import distro_settings, overlay
from amplifier_distro.features import (
    FEATURES,
    PROVIDERS,
    get_provider_catalog,
    handle_provider_request,
    sync_providers,
)
from amplifier_distro.server.app import AppManifest
from amplifier_distro.server.apps.settings import (
    _get_enabled_features,
    detect_bridges,
)

router = APIRouter()
steps_router = APIRouter(prefix="/steps")

_static_dir = Path(__file__).parent / "static"


# --- Pydantic Models ---


class WelcomeData(BaseModel):
    workspace_root: str = ""
    github_handle: str = ""
    git_email: str = ""


class ModulesData(BaseModel):
    modules: list[str] = []


class ProviderData(BaseModel):
    provider: str = ""
    api_key: str = ""


# --- HTML Pages ---


@router.get("/", response_class=HTMLResponse)
async def wizard_page() -> HTMLResponse:
    """Serve the setup wizard."""
    html_file = _static_dir / "wizard.html"
    if html_file.exists():
        return HTMLResponse(content=html_file.read_text())
    return HTMLResponse(
        content="<h1>Install Wizard</h1><p>wizard.html not found.</p>",
        status_code=500,
    )


# --- API Routes ---


@router.get("/detect")
async def detect_environment() -> dict[str, Any]:
    """Auto-detect environment: GitHub, git, API keys, CLI/TUI, bundles.

    Returns both nested objects (backward compat) and flat convenience
    fields that the wizard JS reads directly.
    """
    from amplifier_distro.server.stub import is_stub_mode, stub_detect_environment

    if is_stub_mode():
        return stub_detect_environment()

    result: dict[str, Any] = {}

    # Load existing distro settings for pre-fill
    settings = distro_settings.load()

    # GitHub
    gh_handle: str | None = None
    try:
        proc = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0:
            gh_handle = proc.stdout.strip()
            result["github"] = {"handle": gh_handle, "configured": True}
        else:
            result["github"] = {"handle": None, "configured": False}
    except (FileNotFoundError, subprocess.TimeoutExpired):
        result["github"] = {"handle": None, "configured": False}

    # Git
    git_installed = shutil.which("git") is not None
    git_configured = False
    git_email: str | None = None
    if git_installed:
        try:
            proc = subprocess.run(
                ["git", "config", "--global", "user.email"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                git_configured = True
                git_email = proc.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    result["git"] = {
        "installed": git_installed,
        "configured": git_configured,
        "email": git_email,
    }

    # API keys (check env)
    result["api_keys"] = {
        pid: bool(os.environ.get(p.env_var)) for pid, p in PROVIDERS.items()
    }

    # Amplifier CLI & TUI
    cli_installed = shutil.which("amplifier") is not None
    result["amplifier_cli"] = {"installed": cli_installed}
    result["tui_installed"] = shutil.which("amplifier-tui") is not None

    # Overlay bundle
    result["overlay_bundle"] = overlay.read_overlay() or None

    # Workspace candidates
    home = Path.home()
    candidates = []
    for name in ["dev", "dev/ANext", "projects", "workspace", "code", "src"]:
        p = home / name
        if p.exists() and p.is_dir():
            candidates.append(f"~/{name}")
    result["workspace_candidates"] = candidates

    # Bridges (Slack, Voice)
    result["bridges"] = detect_bridges()

    # --- Flat convenience fields (settings win, then detection, then None) ---

    # workspace_root: settings > first candidate > ""
    result["workspace_root"] = (
        settings.workspace_root
        if settings.workspace_root and settings.workspace_root != "~"
        else (candidates[0] if candidates else "")
    )

    # github_handle: settings > gh CLI detection
    result["github_handle"] = settings.identity.github_handle or gh_handle or ""

    # git_email: settings > git config detection
    result["git_email"] = settings.identity.git_email or git_email or ""

    # Active provider: which provider has a key in env
    result["provider"] = None
    for pid, p in PROVIDERS.items():
        if os.environ.get(p.env_var):
            result["provider"] = pid
            break

    # has_api_key: any provider key present
    result["has_api_key"] = any(
        bool(os.environ.get(p.env_var)) for p in PROVIDERS.values()
    )

    # cli/tui installed flat aliases
    result["cli_installed"] = cli_installed
    result["tui_installed"] = shutil.which("amplifier-tui") is not None

    return result


@router.get("/modules")
async def get_modules() -> dict[str, Any]:
    """Return the feature/module catalog with current enabled state."""
    currently_enabled = set(_get_enabled_features())

    modules = [
        {
            "id": fid,
            "name": feature.name,
            "description": feature.description,
            "tier": feature.tier,
            "category": feature.category,
            "default": feature.tier <= 1,
            "enabled": fid in currently_enabled,
            "requires": feature.requires,
        }
        for fid, feature in FEATURES.items()
    ]

    return {"modules": modules}


@router.get("/providers")
async def get_providers() -> dict[str, Any]:
    """Return all supported providers with their current configuration status."""
    return {"providers": get_provider_catalog()}


# --- Step Handlers ---


@steps_router.post("/welcome")
async def step_welcome(req: WelcomeData) -> dict[str, Any]:
    """Save identity + workspace root from the welcome step."""
    if req.workspace_root:
        distro_settings.update(workspace_root=req.workspace_root)
    if req.github_handle or req.git_email:
        kwargs: dict[str, str] = {}
        if req.github_handle:
            kwargs["github_handle"] = req.github_handle
        if req.git_email:
            kwargs["git_email"] = req.git_email
        distro_settings.update("identity", **kwargs)
    return {"status": "ok"}


@steps_router.post("/config")
async def step_config(request: Request) -> dict[str, Any]:
    """Acknowledge config step (cache/preflight live in foundation settings)."""
    return {"status": "ok"}


@steps_router.post("/modules")
async def step_modules(req: ModulesData) -> dict[str, Any]:
    """Toggle features in the overlay bundle based on selected module IDs."""
    requested = set(req.modules)
    for fid, feature in FEATURES.items():
        if fid in requested:
            # Enable: add dependencies first, then feature includes
            for dep_id in feature.requires:
                dep = FEATURES[dep_id]
                for inc in dep.includes:
                    overlay.add_include(inc)
            for inc in feature.includes:
                overlay.add_include(inc)
        else:
            for inc in feature.includes:
                overlay.remove_include(inc)
    return {"status": "ok", "enabled": req.modules}


class InterfacesData(BaseModel):
    install_cli: bool = False
    install_tui: bool = False


_TOOLS: dict[str, tuple[str, str]] = {
    "cli": ("amplifier", "git+https://github.com/microsoft/amplifier"),
    "tui": ("amplifier-tui", "git+https://github.com/ramparte/amplifier-tui"),
}


async def _uv_tool_install(binary: str, package_url: str) -> dict[str, Any]:
    """Install a tool via ``uv tool install``, return status dict."""
    if shutil.which(binary) is not None:
        return {"status": "ok", "installed": True}
    try:
        proc = await asyncio.create_subprocess_exec(
            "uv",
            "tool",
            "install",
            package_url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            detail = stderr.decode().strip() if stderr else "Install failed"
            return {"status": "error", "detail": detail, "installed": False}
    except FileNotFoundError:
        return {
            "status": "error",
            "detail": "uv is not installed. Install it first: https://docs.astral.sh/uv/",
            "installed": False,
        }
    return {"status": "ok", "installed": shutil.which(binary) is not None}


@steps_router.post("/interfaces")
async def step_interfaces(req: InterfacesData) -> dict[str, Any]:
    """Handle interfaces step, optionally installing the CLI and/or TUI."""
    result: dict[str, Any] = {"status": "ok"}

    for flag, key in [("install_cli", "cli"), ("install_tui", "tui")]:
        if getattr(req, flag):
            binary, url = _TOOLS[key]
            res = await _uv_tool_install(binary, url)
            if res["status"] == "error":
                result["status"] = "error"
                result[f"{key}_error"] = res["detail"]

    result["cli_installed"] = shutil.which("amplifier") is not None
    result["tui_installed"] = shutil.which("amplifier-tui") is not None
    return result


@steps_router.post("/provider")
async def step_provider(req: ProviderData) -> dict[str, Any]:
    """Save API key and provider configuration.

    Three modes:
    - **Explicit key**: ``api_key`` provided — register that provider.
    - **Use existing key**: ``provider`` set, no ``api_key`` — look up key
      from environment or keys.env and register.
    - **Sync** (from "Next" button): both empty — auto-register all
      providers that have keys but aren't fully configured.
    """
    if req.api_key.strip() or req.provider:
        return handle_provider_request(provider=req.provider, api_key=req.api_key)

    # Sync mode (from "Next" button) - auto-register incomplete providers
    synced = sync_providers()
    return {
        "status": "ok",
        "synced": [
            {
                "provider": r.provider_id,
                "provider_name": r.provider_name,
                "ok": r.ok,
                "overlay_error": r.overlay_error,
            }
            for r in synced
        ],
    }


@steps_router.post("/verify")
async def step_verify(request: Request) -> dict[str, Any]:
    """Final verification step - check overall readiness."""
    from amplifier_distro.server.apps.settings import compute_phase

    phase = compute_phase()
    settings = distro_settings.load()

    return {
        "status": "ok",
        "phase": phase,
        "ready": phase == "ready",
        "workspace_root": settings.workspace_root,
        "github_handle": settings.identity.github_handle,
        "has_api_key": any(bool(os.environ.get(p.env_var)) for p in PROVIDERS.values()),
        "cli_installed": shutil.which("amplifier") is not None,
        "tui_installed": shutil.which("amplifier-tui") is not None,
        "overlay_exists": overlay.overlay_exists(),
    }


# Wire the steps sub-router into the main router
router.include_router(steps_router)

manifest = AppManifest(
    name="install-wizard",
    description="Setup wizard for Amplifier",
    version="0.1.0",
    router=router,
)
