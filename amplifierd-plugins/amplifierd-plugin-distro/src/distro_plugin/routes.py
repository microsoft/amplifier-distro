"""FastAPI routes for the distro plugin.

Provides the ``create_routes()`` factory that returns an ``APIRouter`` with
a ``/distro`` prefix.  All endpoints retrieve ``DistroPluginSettings`` from
``request.app.state.distro.settings``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import shutil
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Any, TypedDict


from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from starlette.responses import FileResponse, HTMLResponse, RedirectResponse

from distro_plugin.config import DistroPluginSettings
from distro_plugin.distro_settings import (
    load as load_distro_settings,
    settings_path as distro_settings_path,
    update as update_distro_settings,
)
from distro_plugin.features import FEATURES, features_for_tier, get_enabled_features
from distro_plugin.overlay import (
    add_include,
    get_includes,
    overlay_exists,
    remove_include,
)
from distro_plugin.providers import (
    PROVIDERS,
    check_provider_status,
    get_provider_catalog,
    handle_provider_request,
    update_provider_model,
)
from distro_plugin.reload import request_reload

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"


class BridgeInfo(TypedDict):
    """Typed structure for a bridge detection result."""

    name: str
    description: str
    available: bool


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------


class WelcomeData(BaseModel):
    workspace_root: str = ""
    github_handle: str = ""
    git_email: str = ""


class ModulesData(BaseModel):
    modules: list[str] = []


class ProviderRequest(BaseModel):
    provider: str = ""
    api_key: str = ""
    model: str = ""
    gh_token: str = ""


class InterfacesData(BaseModel):
    install_cli: bool = False
    install_tui: bool = False


class ModelUpdateRequest(BaseModel):
    provider: str
    model: str


class FeatureToggle(BaseModel):
    feature_id: str
    enabled: bool


class TierRequest(BaseModel):
    tier: int


class DistroSettingsUpdate(BaseModel):
    section: str | None = None
    values: dict[str, Any] = {}


# Candidate directory names to scan under $HOME for workspace detection.
_WORKSPACE_CANDIDATES = ("projects", "repos", "src", "code", "dev", "workspace")


# ---------------------------------------------------------------------------
# Cross-cutting helpers
# ---------------------------------------------------------------------------


def compute_phase(settings: DistroPluginSettings) -> str:
    """Determine the current setup phase based on overlay and provider state.

    Returns one of: ``'unconfigured'``, ``'detected'``, ``'ready'``.

    The logic is:
    - No overlay → ``'unconfigured'`` (first-run wizard)
    - Overlay + at least one fully-configured provider in Distro's settings → ``'ready'``
    - Overlay + no configured provider (even if env keys exist) → ``'detected'``
      This covers users who have API keys in their environment but have not yet
      registered a provider through Distro's UI (e.g. after an upgrade).
      They will see the wizard provider step once more.
    """
    if not overlay_exists(settings):
        return "unconfigured"

    # Check if at least one provider is configured in Distro's own settings.
    # "configured" means: key present + entry in settings.yaml + include in overlay.
    has_configured_provider = False
    for pid in PROVIDERS:
        status = check_provider_status(settings, pid)
        if status["configured"]:
            has_configured_provider = True
            break

    if not has_configured_provider:
        # Keys may exist in env, but no provider is fully set up in Distro.
        # Return "detected" so the wizard's provider step is shown.
        for provider in PROVIDERS.values():
            if os.environ.get(provider.env_var):
                return "detected"  # has keys but needs provider setup in Distro
        return "detected"

    return "ready"


def _get_current_provider(settings: DistroPluginSettings) -> dict[str, Any] | None:
    """Return the current primary provider info by matching overlay URIs."""
    current_uris = set(get_includes(settings))
    for pid, provider in PROVIDERS.items():
        if provider.include in current_uris:
            has_key = bool(os.environ.get(provider.env_var))
            return {
                "id": pid,
                "name": provider.name,
                "model": provider.default_model,
                "has_key": has_key,
            }
    return None


def _build_status(settings: DistroPluginSettings) -> dict[str, Any]:
    """Compose the full status response dict.

    Returns a dict with ``phase``, ``provider``, and ``features``.
    ``features`` is a dict keyed by feature ID; every feature in the catalog
    is represented regardless of whether it is enabled, mirroring the original
    distro-server ``_build_status()`` behaviour.
    """
    phase = compute_phase(settings)
    provider = _get_current_provider(settings)
    enabled_set = set(get_enabled_features(settings))

    features_dict: dict[str, Any] = {}
    for fid, feat in FEATURES.items():
        features_dict[fid] = {
            "enabled": fid in enabled_set,
            "tier": feat.tier,
            "name": feat.name,
            "description": feat.description,
        }

    return {
        "phase": phase,
        "provider": provider,
        "features": features_dict,
    }


# ---------------------------------------------------------------------------
# Async helpers
# ---------------------------------------------------------------------------


_TOOLS: dict[str, tuple[str, str]] = {
    "cli": ("amplifier", "git+https://github.com/microsoft/amplifier"),
    "tui": ("amplifier-tui", "git+https://github.com/ramparte/amplifier-tui"),
}


async def _uv_tool_install(binary: str, package_url: str) -> dict[str, Any]:
    """Run ``uv tool install <package_url>`` asynchronously.

    Returns a status dict with ``status`` and ``installed`` keys."""
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


# ---------------------------------------------------------------------------
# Subprocess helpers for detection
# ---------------------------------------------------------------------------


async def _run_command(*args: str) -> str:
    """Run a subprocess and return stripped stdout, or '' on failure."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0:
            return (stdout or b"").decode().strip()
    except (FileNotFoundError, OSError):
        pass
    return ""


# ---------------------------------------------------------------------------
# Route factory
# ---------------------------------------------------------------------------


def _get_settings(request: Request) -> DistroPluginSettings:
    """Extract settings from the app state."""
    return request.app.state.distro.settings


def create_routes() -> APIRouter:
    """Create and return the distro plugin routes.

    Returns a root-level APIRouter that:
    - Mounts ``StaticFiles`` at ``/static`` for shared CSS/JS assets
    - Serves ``/favicon.svg`` directly
    - Includes all ``/distro/*`` API and HTML page routes
    """
    # Root-level router (no prefix) – serves static assets at app root paths
    # so that HTML pages can reference /static/… and /favicon.svg without any
    # path prefix.
    outer = APIRouter()

    @outer.get("/", include_in_schema=False)
    async def root_redirect(request: Request) -> RedirectResponse:
        """Redirect / to the appropriate destination based on setup phase."""
        # During prewarm, send to /distro/ which serves the loading screen
        bundles_ready = getattr(request.app.state, "bundles_ready", None)
        if bundles_ready and not bundles_ready.is_set():
            return RedirectResponse(url="/distro/")

        try:
            settings = _get_settings(request)
            if compute_phase(settings) != "ready":
                return RedirectResponse(url="/distro/setup")
        except Exception:
            pass
        return RedirectResponse(url="/chat/")

    @outer.get("/favicon.svg", include_in_schema=False)
    async def get_favicon() -> FileResponse:
        """Serve the SVG favicon from the bundled static directory."""
        return FileResponse(_STATIC_DIR / "favicon.svg")

    @outer.get("/static/{file_path:path}", include_in_schema=False)
    async def serve_static(file_path: str) -> FileResponse:
        """Serve static assets (CSS, JS, fonts) from the bundled static directory."""
        full_path = _STATIC_DIR / file_path
        if not full_path.is_file() or not full_path.resolve().is_relative_to(
            _STATIC_DIR.resolve()
        ):
            raise HTTPException(status_code=404)
        return FileResponse(full_path)

    # All /distro/* routes live on a prefixed sub-router included below.
    router = APIRouter(prefix="/distro")

    @router.get("/status")
    async def get_status(request: Request) -> dict[str, Any]:
        """Return current distro setup status."""
        settings = _get_settings(request)
        return _build_status(settings)

    @router.get("/detect")
    async def get_detect(request: Request) -> dict[str, Any]:
        """Run environment detection and return comprehensive results."""
        settings = _get_settings(request)

        # Run subprocess detections concurrently
        gh_user, git_name, git_email = await asyncio.gather(
            _run_command("gh", "api", "user", "-q", ".login"),
            _run_command("git", "config", "--global", "user.name"),
            _run_command("git", "config", "--global", "user.email"),
        )

        github = {
            "user": gh_user,
            "authenticated": bool(gh_user),
        }

        git = {
            "name": git_name,
            "email": git_email,
            "configured": bool(git_name or git_email),
        }

        # Check env vars for API keys
        api_keys: dict[str, bool] = {}
        for pid, provider in PROVIDERS.items():
            api_keys[pid] = bool(os.environ.get(provider.env_var))

        # Scan workspace candidates
        workspace_candidates: list[str] = []
        home = os.path.expanduser("~")
        for candidate in _WORKSPACE_CANDIDATES:
            candidate_path = os.path.join(home, candidate)
            if os.path.isdir(candidate_path):
                workspace_candidates.append(candidate_path)

        # Load saved distro settings for pre-fill
        ds = load_distro_settings(settings)
        saved_root = (
            ds.workspace_root if ds.workspace_root and ds.workspace_root != "~" else ""
        )
        workspace_root = saved_root or (
            workspace_candidates[0] if workspace_candidates else ""
        )

        return {
            "github": github,
            "git": git,
            "api_keys": api_keys,
            "workspace_candidates": workspace_candidates,
            # Flat convenience fields
            "github_user": gh_user,
            "git_name": git_name,
            "git_email": git_email,
            "workspace_root": workspace_root,
            "github_handle": ds.identity.github_handle or gh_user or "",
            "cli_installed": shutil.which("amplifier") is not None,
            "tui_installed": shutil.which("amplifier-tui") is not None,
        }

    # ------------------------------------------------------------------
    # Provider & feature management endpoints
    # ------------------------------------------------------------------

    @router.get("/providers")
    async def get_providers(request: Request) -> dict[str, Any]:
        """Return the full provider catalog with configuration status."""
        settings = _get_settings(request)
        return {"providers": get_provider_catalog(settings)}

    @router.post("/provider")
    async def post_provider(request: Request, body: ProviderRequest) -> dict[str, Any]:
        """Detect and register a provider from an API key or provider ID."""
        settings = _get_settings(request)
        result = handle_provider_request(
            settings,
            provider=body.provider,
            api_key=body.api_key,
            model=body.model,
            gh_token=body.gh_token,
        )
        if result.get("status") == "error":
            raise HTTPException(status_code=400, detail=result.get("detail", ""))
        request_reload(request.app)
        return result

    @router.put("/provider/model")
    async def put_provider_model(
        request: Request, body: ModelUpdateRequest
    ) -> dict[str, Any]:
        """Update the model for an already-configured provider.

        Writes the new default_model to settings.yaml and triggers a
        hot reload so the change takes effect immediately.
        """
        settings = _get_settings(request)
        if body.provider not in PROVIDERS:
            raise HTTPException(status_code=400, detail="Unknown provider")
        updated = update_provider_model(settings, body.provider, body.model)
        if not updated:
            raise HTTPException(
                status_code=400,
                detail="Provider not configured — register it first",
            )
        request_reload(request.app)
        return {"status": "ok", "provider": body.provider, "model": body.model}

    @router.get("/preflight")
    async def get_preflight(request: Request) -> dict[str, Any]:
        """Run preflight diagnostic checks and return a report."""
        settings = _get_settings(request)
        ds = load_distro_settings(settings)
        amplifier_home = Path(settings.amplifier_home).expanduser()
        distro_home = Path(settings.distro_home).expanduser()
        keys_path = amplifier_home / "keys.env"

        # --- Check functions adapted from original doctor.py ---

        def check_config_exists() -> dict:
            cfg_path = distro_home / "settings.yaml"
            if not cfg_path.exists():
                return {
                    "name": "Config file",
                    "passed": False,
                    "message": f"settings.yaml not found at {cfg_path}",
                    "severity": "error",
                }
            return {
                "name": "Config file",
                "passed": True,
                "message": f"Found at {cfg_path}",
            }

        def check_identity() -> dict:
            if ds.identity.github_handle:
                return {
                    "name": "Identity",
                    "passed": True,
                    "message": f"@{ds.identity.github_handle}",
                }
            return {
                "name": "Identity",
                "passed": False,
                "message": "GitHub handle not set",
                "severity": "error",
            }

        def check_workspace() -> dict:
            ws = Path(ds.workspace_root).expanduser()
            if ws.is_dir():
                return {"name": "Workspace", "passed": True, "message": str(ws)}
            return {
                "name": "Workspace",
                "passed": False,
                "message": f"{ws} does not exist",
                "severity": "error",
            }

        def check_keys_permissions() -> dict:
            if not keys_path.exists():
                return {
                    "name": "Keys permissions",
                    "passed": True,
                    "message": "keys.env not present",
                }
            if platform.system() not in ("Linux", "Darwin"):
                return {
                    "name": "Keys permissions",
                    "passed": True,
                    "message": "Permission check skipped (Windows)",
                }
            mode = keys_path.stat().st_mode & 0o777
            if mode == 0o600:
                return {
                    "name": "Keys permissions",
                    "passed": True,
                    "message": "keys.env has correct permissions (600)",
                }
            return {
                "name": "Keys permissions",
                "passed": False,
                "message": f"keys.env has mode {oct(mode)}, should be 600",
                "severity": "warning",
            }

        def check_dir_exists(dir_path: Path, name: str) -> dict:
            if dir_path.is_dir():
                return {"name": name, "passed": True, "message": str(dir_path)}
            return {
                "name": name,
                "passed": False,
                "message": f"{dir_path} does not exist",
                "severity": "warning",
            }

        # --- Async checks ---
        git_name, git_email, gh_user = await asyncio.gather(
            _run_command("git", "config", "--global", "user.name"),
            _run_command("git", "config", "--global", "user.email"),
            _run_command("gh", "api", "user", "-q", ".login"),
        )
        amp_cli_path = shutil.which("amplifier")

        # --- Collate checks ---
        raw_checks = [
            # Config & identity
            check_config_exists(),
            check_identity(),
            check_workspace(),
            # Directories
            check_dir_exists(amplifier_home / "memory", "Memory directory"),
            check_dir_exists(amplifier_home / "cache", "Bundle cache"),
            check_keys_permissions(),
            # Tools
            {
                "name": "Git config",
                "passed": bool(git_name and git_email),
                "message": "Configured"
                if (git_name and git_email)
                else "Name or email not configured",
                "severity": "error" if not (git_name and git_email) else "ok",
            },
            {
                "name": "GitHub CLI",
                "passed": bool(gh_user),
                "message": "Authenticated" if gh_user else "Not authenticated",
                "severity": "warning",
            },
            {
                "name": "Amplifier CLI",
                "passed": bool(amp_cli_path),
                "message": f"Found at {amp_cli_path}"
                if amp_cli_path
                else "Not found in PATH",
                "severity": "error" if not amp_cli_path else "ok",
            },
        ]

        # --- Format for frontend ---
        checks = []
        for check in raw_checks:
            fe_check = {
                "name": check["name"],
                "passed": check["passed"],
                "message": check["message"],
            }
            if check.get("severity") == "warning" and not check["passed"]:
                fe_check["severity"] = "warning"
            checks.append(fe_check)

        overall_passed = all(
            c.get("severity") != "error" for c in raw_checks if not c["passed"]
        )
        return {"passed": overall_passed, "checks": checks}

    @router.get("/modules")
    async def get_modules(request: Request) -> dict[str, Any]:
        """Return the feature catalog with enabled status for each feature."""
        settings = _get_settings(request)
        enabled = set(get_enabled_features(settings))
        modules = [
            {
                "id": feat.id,
                "name": feat.name,
                "description": feat.description,
                "tier": feat.tier,
                "category": feat.category,
                "enabled": feat.id in enabled,
            }
            for feat in FEATURES.values()
        ]
        return {"modules": modules}

    @router.post("/features")
    async def post_features(request: Request, body: FeatureToggle) -> dict[str, Any]:
        """Toggle an individual feature on or off."""
        settings = _get_settings(request)
        feature = FEATURES.get(body.feature_id)
        if not feature:
            raise HTTPException(
                status_code=400, detail=f"Unknown feature: {body.feature_id}"
            )

        if body.enabled:
            # Enable: add includes for the feature and its direct dependencies.
            # NOTE: only single-depth resolution — if a dependency itself had
            # dependencies they would not be added.  The current catalog has at
            # most one level of requires, so this is sufficient.
            for dep_id in feature.requires:
                dep = FEATURES.get(dep_id)
                if dep:
                    for inc in dep.includes:
                        add_include(settings, inc, app=request.app)
            for inc in feature.includes:
                add_include(settings, inc, app=request.app)
        else:
            # Disable: remove only this feature's includes (dependencies may be
            # shared by other enabled features, so leave them in place).
            for inc in feature.includes:
                remove_include(settings, inc, app=request.app)

        return _build_status(settings)

    @router.post("/tier")
    async def post_tier(request: Request, body: TierRequest) -> dict[str, Any]:
        """Enable all features up to the given tier."""
        settings = _get_settings(request)
        # features_for_tier returns lower-tier features first (iterates
        # range(1, tier+1)), so dependencies are naturally added before
        # the features that require them.
        feature_ids = features_for_tier(body.tier)
        for fid in feature_ids:
            feat = FEATURES.get(fid)
            if feat:
                for inc in feat.includes:
                    add_include(settings, inc, app=request.app)
        return {"status": "ok", "tier": body.tier, "features_enabled": feature_ids}

    # ------------------------------------------------------------------
    # Setup wizard step endpoints
    # ------------------------------------------------------------------

    @router.post("/setup/steps/welcome")
    async def step_welcome(request: Request, body: WelcomeData) -> dict[str, Any]:
        """Save identity data from the welcome step."""
        settings = _get_settings(request)
        if body.workspace_root:
            update_distro_settings(settings, workspace_root=body.workspace_root)
        if body.github_handle or body.git_email:
            kwargs: dict[str, str] = {}
            if body.github_handle:
                kwargs["github_handle"] = body.github_handle
            if body.git_email:
                kwargs["git_email"] = body.git_email
            update_distro_settings(settings, section="identity", **kwargs)
        return {"status": "ok"}

    @router.post("/setup/steps/config")
    async def step_config(request: Request) -> dict[str, Any]:
        """Config step passthrough."""
        return {"status": "ok"}

    @router.post("/setup/steps/modules")
    async def step_modules(request: Request, body: ModulesData) -> dict[str, Any]:
        """Enable requested features and disable unrequested ones."""
        settings = _get_settings(request)
        requested = set(body.modules)
        for fid, feat in FEATURES.items():
            if fid in requested:
                for dep_id in feat.requires:
                    dep = FEATURES.get(dep_id)
                    if dep:
                        for inc in dep.includes:
                            add_include(settings, inc, app=request.app)
                for inc in feat.includes:
                    add_include(settings, inc, app=request.app)
                # Provision required directories for features that need them.
                if feat.category == "memory":
                    memory_dir = Path(settings.amplifier_home) / "memory"
                    memory_dir.mkdir(parents=True, exist_ok=True)
            else:
                for inc in feat.includes:
                    remove_include(settings, inc, app=request.app)
        return {"status": "ok"}

    @router.post("/setup/steps/interfaces")
    async def step_interfaces(request: Request, body: InterfacesData) -> dict[str, Any]:
        """Conditionally install CLI/TUI interfaces."""
        result: dict[str, Any] = {"status": "ok"}

        for flag, key in [("install_cli", "cli"), ("install_tui", "tui")]:
            if getattr(body, flag):
                binary, url = _TOOLS[key]
                res = await _uv_tool_install(binary, url)
                if res["status"] == "error":
                    result["status"] = "error"
                    result[f"{key}_error"] = res.get("detail", "")

        result["cli_installed"] = shutil.which("amplifier") is not None
        result["tui_installed"] = shutil.which("amplifier-tui") is not None
        return result

    @router.post("/setup/steps/provider/validate")
    async def step_provider_validate(
        request: Request, body: ProviderRequest
    ) -> dict[str, Any]:
        """Phase 1: Validate provider auth and return model list."""
        provider_id = body.provider.strip()
        if not provider_id or provider_id not in PROVIDERS:
            raise HTTPException(status_code=400, detail="Unknown provider")

        provider = PROVIDERS[provider_id]
        models = list(provider.fallback_models)
        default_model = provider.default_model

        # Standard key-bearing providers just return model list
        if provider.needs_key:
            return {
                "status": "needs_model",
                "models": models,
                "default_model": default_model,
            }

        # Keyless (GitHub Copilot): check env vars then gh CLI
        copilot_env_vars = [
            "COPILOT_AGENT_TOKEN",
            "COPILOT_GITHUB_TOKEN",
            "GH_TOKEN",
            "GITHUB_TOKEN",
        ]
        for var in copilot_env_vars:
            if os.environ.get(var):
                return {
                    "status": "needs_model",
                    "models": models,
                    "default_model": default_model,
                    "token_source": "env",
                    "gh_token": "",
                }

        # Fall back to gh auth token
        try:
            result = subprocess.run(
                ["gh", "auth", "token"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                return {
                    "status": "needs_model",
                    "models": models,
                    "default_model": default_model,
                    "token_source": "gh_cli",
                    "gh_token": result.stdout.strip(),
                }
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        return {
            "status": "error",
            "detail": (
                "GitHub authentication not found. "
                "Run gh auth login in a terminal and try again."
            ),
        }

    @router.post("/setup/steps/provider")
    async def step_provider(request: Request, body: ProviderRequest) -> dict[str, Any]:
        """Phase 2: Register a provider with explicit key or sync from environment."""
        settings = _get_settings(request)
        if body.api_key.strip() or body.provider.strip():
            result = handle_provider_request(
                settings,
                provider=body.provider,
                api_key=body.api_key,
                model=body.model,
                gh_token=body.gh_token,
            )
            if result.get("status") == "error":
                raise HTTPException(status_code=400, detail=result.get("detail", ""))
            request_reload(request.app)
            return result
        # No explicit provider action — user is just proceeding to the next step.
        # Providers are registered individually via POST /distro/provider or
        # auto-detect (e.g. GitHub Copilot).  "Next" means "I'm done here."
        return {"status": "ok", "synced": 0}

    @router.post("/setup/steps/verify")
    async def step_verify(request: Request) -> dict[str, Any]:
        """Compute phase and return comprehensive setup status."""
        settings = _get_settings(request)
        phase = compute_phase(settings)
        ov_exists = overlay_exists(settings)
        ds = load_distro_settings(settings)
        return {
            "status": "ok",
            "phase": phase,
            "ready": phase == "ready",
            "overlay_exists": ov_exists,
            "workspace_root": ds.workspace_root,
            "github_handle": ds.identity.github_handle,
            "has_api_key": any(
                bool(os.environ.get(provider.env_var))
                for provider in PROVIDERS.values()
            ),
            "cli_installed": shutil.which("amplifier") is not None,
            "tui_installed": shutil.which("amplifier-tui") is not None,
        }

    # ------------------------------------------------------------------
    # Distro settings CRUD
    # ------------------------------------------------------------------

    @router.get("/distro-settings")
    async def get_distro_settings(request: Request) -> dict[str, Any]:
        """Return the current distro settings as a dict with the file path."""
        settings = _get_settings(request)
        ds = load_distro_settings(settings)
        return {
            "settings": asdict(ds),
            "path": str(distro_settings_path(settings)),
        }

    @router.post("/distro-settings")
    async def post_distro_settings(
        request: Request, body: DistroSettingsUpdate
    ) -> dict[str, Any]:
        """Update distro settings (root or section fields)."""
        settings = _get_settings(request)
        update_distro_settings(settings, section=body.section, **body.values)
        return {"status": "ok"}

    # ------------------------------------------------------------------
    # Static HTML pages
    # ------------------------------------------------------------------

    @router.get("/", response_model=None)
    async def get_dashboard(request: Request) -> HTMLResponse | RedirectResponse:
        """Serve the dashboard landing page, or redirect to setup if unconfigured."""
        # Serve loading screen while bundle prewarm is in progress
        bundles_ready = getattr(request.app.state, "bundles_ready", None)
        if bundles_ready and not bundles_ready.is_set():
            loading_path = _STATIC_DIR / "loading.html"
            try:
                return HTMLResponse(content=loading_path.read_text())
            except OSError:
                return HTMLResponse(
                    content="<h1>Starting up&hellip;</h1><p>Preparing your environment.</p>",
                    status_code=503,
                    headers={"Retry-After": "5"},
                )

        settings = _get_settings(request)
        phase = compute_phase(settings)
        if phase != "ready":
            return RedirectResponse(url="/distro/setup")
        html_path = _STATIC_DIR / "dashboard.html"
        try:
            content = html_path.read_text()
            # Tell the frontend whether PAM auth is active so the auth
            # widget can skip its /auth/me probe when there is no session
            # infrastructure to query.
            auth_on = hasattr(request.app.state, "auth_verify_session")
            openai_status = check_provider_status(settings, "openai")
            tags = (
                f"<script>"
                f"window.__AUTH_ENABLED={str(auth_on).lower()};"
                f"window.__OPENAI_CONFIGURED={str(openai_status['has_key']).lower()}"
                f"</script>"
            )
            content = content.replace("</head>", tags + "</head>", 1)
            return HTMLResponse(content=content)
        except OSError:
            return HTMLResponse(
                content="<h1>Dashboard not available</h1><p>Static files not found.</p>",
                status_code=500,
            )

    @router.get("/setup")
    async def get_setup_page(request: Request) -> HTMLResponse:
        """Serve the setup wizard HTML page."""
        html_path = _STATIC_DIR / "wizard.html"
        try:
            content = html_path.read_text()
            return HTMLResponse(content=content)
        except OSError:
            return HTMLResponse(
                content="<h1>Setup UI not available</h1><p>Static files not found.</p>",
                status_code=500,
            )

    @router.get("/settings")
    async def get_settings_page(request: Request) -> HTMLResponse:
        """Serve the settings HTML page."""
        html_path = _STATIC_DIR / "settings.html"
        try:
            content = html_path.read_text()
            # Tell the frontend whether PAM auth is active so the auth
            # widget can skip its /auth/me probe when there is no session
            # infrastructure to query.
            auth_on = hasattr(request.app.state, "auth_verify_session")
            tag = f"<script>window.__AUTH_ENABLED={str(auth_on).lower()}</script>"
            content = content.replace("</head>", tag + "</head>", 1)
            return HTMLResponse(content=content)
        except OSError:
            return HTMLResponse(
                content="<h1>Settings UI not available</h1><p>Static files not found.</p>",
                status_code=500,
            )

    outer.include_router(router)
    return outer
