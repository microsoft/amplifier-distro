"""Local overlay bundle management.

The distro creates a local bundle that includes the maintained distro bundle.
The wizard and settings apps modify this overlay; the underlying
distro bundle is never touched.

The overlay is a directory containing a ``bundle.yaml`` file:

    ~/.amplifier-distro/bundle/
    └── bundle.yaml

Foundation's ``load_bundle()`` loads it by path and handles all
include resolution and composition automatically.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import yaml

from .conventions import DISTRO_OVERLAY_DIR
from .features import AMPLIFIER_START_URI, Provider, provider_bundle_uri

logger = logging.getLogger(__name__)

# Holds strong references to fire-and-forget reload tasks so they are not
# garbage-collected before they complete (satisfies RUF006).
_reload_tasks: set[asyncio.Task[None]] = set()

# Kept here ONLY for migration — it is never added to new overlays.
_STALE_SESSION_NAMING_URI = (
    "git+https://github.com/microsoft/amplifier-foundation@main"
    "#subdirectory=modules/hooks-session-naming"
)


def overlay_dir() -> Path:
    """Return the overlay bundle directory path, expanded."""
    return Path(DISTRO_OVERLAY_DIR).expanduser()


def overlay_bundle_path() -> Path:
    """Return the path to the overlay bundle.yaml."""
    return overlay_dir() / "bundle.yaml"


def overlay_exists() -> bool:
    """Check whether the local overlay bundle has been created."""
    return overlay_bundle_path().exists()


def read_overlay() -> dict[str, Any]:
    """Read and parse the current overlay bundle. Returns {} if missing."""
    path = overlay_bundle_path()
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text()) or {}
    except (yaml.YAMLError, OSError):
        logger.warning(
            "Overlay bundle at %s is corrupt or unreadable; treating as absent", path
        )
        return {}


def _write_overlay(data: dict[str, Any]) -> Path:
    """Write the overlay bundle.yaml to disk."""
    path = overlay_bundle_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))

    # Trigger live bundle reload if the server is running.
    # Uses get_running_loop() so this is a no-op when called from the CLI / wizard
    # (no event loop running) and silently skips when services aren't initialized.
    try:
        from amplifier_distro.server.services import get_services

        services = get_services()
        reload_bundle = getattr(services.backend, "reload_bundle", None)
        if reload_bundle is not None:
            loop = asyncio.get_running_loop()
            task = loop.create_task(reload_bundle())
            _reload_tasks.add(task)
            task.add_done_callback(_reload_tasks.discard)
    except RuntimeError:
        # RuntimeError covers both "services not initialized" (from get_services())
        # and "no running event loop" (from get_running_loop()) — both are expected
        # in non-server contexts and can be safely ignored.
        pass

    return path


def _filter_includes(includes: list[Any], uri: str) -> list[Any]:
    """Return *includes* with every entry matching *uri* removed."""
    return [
        entry
        for entry in includes
        if (entry.get("bundle") if isinstance(entry, dict) else entry) != uri
    ]


def get_includes(data: dict[str, Any] | None = None) -> list[str]:
    """Extract the list of include URIs from overlay data."""
    if data is None:
        data = read_overlay()
    return [
        entry["bundle"] if isinstance(entry, dict) else entry
        for entry in data.get("includes", [])
    ]


def ensure_overlay(provider: Provider) -> Path:
    """Create (or update) the overlay bundle with the distro bundle + a provider.

    If the overlay already exists, the provider include is added only if
    not already present.  The distro bundle include is always ensured.
    Returns the path to the overlay directory.
    """
    data = read_overlay()

    if not data:
        # Fresh overlay
        data = {
            "bundle": {
                "name": "amplifier-distro",
                "version": "0.1.0",
                "description": "Local Amplifier Distro environment",
            },
            "includes": [
                {"bundle": AMPLIFIER_START_URI},
                {"bundle": provider_bundle_uri(provider)},
            ],
        }
    else:
        # Strip stale entries first so current_uris is clean before checking
        # what's already present (otherwise the stale URI would appear in
        # current_uris and block re-insertion of legitimate URIs).
        data["includes"] = _filter_includes(
            data.get("includes", []), _STALE_SESSION_NAMING_URI
        )
        current_uris = set(get_includes(data))
        includes = data["includes"]

        if AMPLIFIER_START_URI not in current_uris:
            includes.insert(0, {"bundle": AMPLIFIER_START_URI})

        prov_uri = provider_bundle_uri(provider)
        if prov_uri not in current_uris:
            includes.append({"bundle": prov_uri})

    _write_overlay(data)
    return overlay_dir()


def add_include(uri: str) -> None:
    """Add a bundle include to the overlay (idempotent)."""
    data = read_overlay()
    if not data:
        return  # Overlay must exist first

    current_uris = set(get_includes(data))
    if uri not in current_uris:
        data.setdefault("includes", []).append({"bundle": uri})
        _write_overlay(data)


def remove_include(uri: str) -> None:
    """Remove a bundle include from the overlay."""
    data = read_overlay()
    if not data:
        return

    data["includes"] = _filter_includes(data.get("includes", []), uri)
    _write_overlay(data)
