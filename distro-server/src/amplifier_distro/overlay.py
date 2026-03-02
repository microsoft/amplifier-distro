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

import logging
from pathlib import Path
from typing import Any

import yaml

from .conventions import DISTRO_OVERLAY_DIR
from .features import AMPLIFIER_START_URI, Provider, provider_bundle_uri

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
#  Overlay migration tables
# ---------------------------------------------------------------------------
# URIs that should be silently removed (no replacement).
_STALE_URIS: list[str] = [
    # hooks-session-naming is now properly declared in the distro bundle at
    # https://github.com/microsoft/amplifier-bundle-distro/blob/main/behaviors/start.yaml
    # No longer needs migration stripping.
]

# URIs that moved — old URI → current URI.  The overlay migration
# replaces these in-place so the include order is preserved.
_URI_REPLACEMENTS: dict[str, str] = {
    # Distro bundle moved from monorepo subdirectory to its own repo.
    "git+https://github.com/microsoft/amplifier-distro@main#subdirectory=bundle": (
        AMPLIFIER_START_URI
    ),
    # Provider bundles that never existed in foundation; now live in the
    # distro bundle repo.
    "git+https://github.com/microsoft/amplifier-foundation@main"
    "#subdirectory=providers/gemini-pro.yaml": (
        f"{AMPLIFIER_START_URI}#subdirectory=providers/gemini-pro.yaml"
    ),
    "git+https://github.com/microsoft/amplifier-foundation@main"
    "#subdirectory=providers/ollama.yaml": (
        f"{AMPLIFIER_START_URI}#subdirectory=providers/ollama.yaml"
    ),
    "git+https://github.com/microsoft/amplifier-foundation@main"
    "#subdirectory=providers/azure-openai.yaml": (
        f"{AMPLIFIER_START_URI}#subdirectory=providers/azure-openai.yaml"
    ),
}


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


def _migrate_includes(data: dict[str, Any]) -> bool:
    """Apply all known URI migrations to overlay includes.

    1. Replace moved URIs with their current equivalents (in-place).
    2. Remove stale URIs that have no replacement.

    Returns ``True`` if any entries were changed.
    """
    includes: list[Any] = data.get("includes", [])
    changed = False

    # Pass 1: in-place replacements for moved URIs.
    for i, entry in enumerate(includes):
        uri = entry.get("bundle") if isinstance(entry, dict) else entry
        if uri in _URI_REPLACEMENTS:
            new_uri = _URI_REPLACEMENTS[uri]
            includes[i] = {"bundle": new_uri} if isinstance(entry, dict) else new_uri
            changed = True

    # Pass 2: remove entries with no replacement.
    original_len = len(includes)
    for uri in _STALE_URIS:
        includes = _filter_includes(includes, uri)
    if len(includes) < original_len:
        changed = True

    data["includes"] = includes
    return changed


def migrate_overlay() -> None:
    """Apply one-time migrations to the overlay bundle.

    Replaces moved include URIs with their current equivalents,
    removes stale URIs, and ensures the distro bundle include
    is present.  No-op when no overlay exists or no changes are
    needed.
    """
    data = read_overlay()
    if not data:
        return

    changed = _migrate_includes(data)

    # Ensure the current distro bundle URI is present.
    current_uris = set(get_includes(data))
    if AMPLIFIER_START_URI not in current_uris:
        data.setdefault("includes", []).insert(0, {"bundle": AMPLIFIER_START_URI})
        changed = True

    if changed:
        _write_overlay(data)
        logger.info("Overlay migrated: stale URIs replaced with current equivalents")


def ensure_overlay(provider: Provider) -> Path:
    """Create or update the overlay with the distro bundle + a provider.

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
        # Migrate stale entries first so current_uris is clean before
        # checking what's already present.
        _migrate_includes(data)
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


def snapshot_overlay() -> str | None:
    """Capture current overlay content for rollback.

    Returns the raw text of ``bundle.yaml``, or ``None`` if no overlay
    exists yet.
    """
    path = overlay_bundle_path()
    if not path.exists():
        return None
    return path.read_text()


def restore_overlay(snapshot: str | None) -> None:
    """Restore overlay from a previous snapshot.

    If *snapshot* is ``None`` the overlay file is removed (reverting to
    the "no overlay" state).
    """
    path = overlay_bundle_path()
    if snapshot is None:
        path.unlink(missing_ok=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(snapshot)
