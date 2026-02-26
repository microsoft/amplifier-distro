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

from pathlib import Path
from typing import Any

import yaml

from .conventions import DISTRO_OVERLAY_DIR
from .features import AMPLIFIER_START_URI, Provider, provider_bundle_uri


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
        return {}


def _write_overlay(data: dict[str, Any]) -> Path:
    """Write the overlay bundle.yaml to disk."""
    path = overlay_bundle_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
    return path


def get_includes(data: dict[str, Any] | None = None) -> list[str]:
    """Extract the list of include URIs from overlay data."""
    if data is None:
        data = read_overlay()
    return [
        entry["bundle"] if isinstance(entry, dict) else entry
        for entry in data.get("includes", [])
    ]


def ensure_overlay(provider: Provider) -> Path:
    """Create (or update) the overlay bundle with include to maintained bundle + a provider.

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
        # Existing overlay — ensure includes are present
        current_uris = set(get_includes(data))
        includes = data.setdefault("includes", [])

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

    data["includes"] = [
        entry
        for entry in data.get("includes", [])
        if (entry.get("bundle") if isinstance(entry, dict) else entry) != uri
    ]
    _write_overlay(data)
