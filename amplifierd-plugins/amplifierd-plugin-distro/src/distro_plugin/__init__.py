"""amplifierd-plugin-distro — distro setup wizard and settings management."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter

from distro_plugin.config import DistroPluginSettings
from distro_plugin.routes import create_routes


def _patch_provider_loading(settings: DistroPluginSettings) -> None:
    """Patch amplifierd's load_provider_config to merge CLI + Distro providers.

    The amplifierd package reads provider config only from ~/.amplifier/settings.yaml.
    Distro now stores its own provider config in ~/.amplifier-distro/settings.yaml.
    This patch makes load_provider_config() merge both sources so that Distro
    sessions see providers from both files, with Distro entries winning on conflict.

    The patch is safe because:
    - amplifierd imports load_provider_config with local imports inside functions
      (app.py:47, session_manager.py:265, session_manager.py:413), so the
      module-level attribute is resolved at call time.
    - When the env var / distro settings are absent, behavior is identical to
      the original (backward-compatible).
    """
    try:
        import amplifierd.providers as _amp_providers
    except ImportError:
        return  # amplifierd not installed; nothing to patch

    _original_load = _amp_providers.load_provider_config
    distro_home = settings.distro_home

    def _merged_load_provider_config(home=None):
        """Load providers from CLI settings, then overlay Distro providers."""
        cli_providers = _original_load(home)
        # Read Distro's own provider config
        distro_providers = _original_load(distro_home)
        if not distro_providers:
            return cli_providers
        if not cli_providers:
            return distro_providers
        # Merge: Distro providers win on module-name conflict
        cli_by_module = {
            p["module"]: p
            for p in cli_providers
            if isinstance(p, dict) and "module" in p
        }
        distro_by_module = {
            p["module"]: p
            for p in distro_providers
            if isinstance(p, dict) and "module" in p
        }
        # Start with CLI providers, override with Distro where they share a module
        merged_modules = dict(cli_by_module)
        merged_modules.update(distro_by_module)
        # Preserve Distro ordering for providers that exist in Distro,
        # append CLI-only providers at the end
        result = []
        seen: set[str] = set()
        for p in distro_providers:
            if isinstance(p, dict) and "module" in p:
                result.append(merged_modules[p["module"]])
                seen.add(p["module"])
        for p in cli_providers:
            if isinstance(p, dict) and "module" in p and p["module"] not in seen:
                result.append(p)
        return result

    _amp_providers.load_provider_config = _merged_load_provider_config


def create_router(state: Any) -> APIRouter:
    """Plugin entry point called by amplifierd to discover and mount routes.

    Instantiates ``DistroPluginSettings`` from environment, attaches it to
    *state* so route handlers can retrieve it via
    ``request.app.state.distro.settings``, and returns the ``APIRouter``.

    Also runs overlay migration at startup so any stale URIs from previous
    installations are silently upgraded to current equivalents.
    """
    settings = DistroPluginSettings()
    state.distro = SimpleNamespace(settings=settings)

    # Patch load_provider_config to merge CLI + Distro providers
    _patch_provider_loading(settings)

    from distro_plugin.overlay import migrate_overlay, overlay_exists

    migrate_overlay(settings)

    # Register the overlay bundle as "distro", shadowing the well-known git URI.
    # This ensures prewarm and session creation use the user's customized bundle
    # (with their selected providers and features) instead of the raw upstream.
    # The overlay's bundle.yaml includes the upstream distro bundle via includes:.
    bundle_registry = getattr(state, "bundle_registry", None)
    if bundle_registry and overlay_exists(settings):
        from pathlib import Path

        overlay_dir = str(Path(settings.distro_home) / "bundle")
        bundle_registry.register({"distro": overlay_dir})

    # Best-effort check for unreachable feature bundle URIs.
    # Warnings are logged so they appear in the startup output, surfacing
    # broken features (e.g. non-existent repos) that would otherwise fail
    # silently during the bundle loading phase.
    from distro_plugin.features import check_feature_uris

    uri_warnings = check_feature_uris(settings)
    if uri_warnings:
        import logging

        logger = logging.getLogger(__name__)
        for w in uri_warnings:
            logger.warning("Feature URI check: %s", w)

    return create_routes()
