"""amplifierd-plugin-distro — distro setup wizard and settings management."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter

from distro_plugin.config import DistroPluginSettings
from distro_plugin.routes import create_routes


def _patch_provider_loading(settings: DistroPluginSettings) -> None:
    """Patch amplifierd's load_provider_config to read from Distro's settings only.

    The amplifierd package reads provider config from ~/.amplifier/settings.yaml by
    default (the CLI's home).  Distro stores its own provider config in
    ~/.amplifier-distro/settings.yaml.  This patch replaces the default so that
    Distro sessions use ONLY the providers the user registered through Distro's UI.
    The CLI's settings.yaml is completely irrelevant to Distro sessions.

    The patch is safe because:
    - amplifierd imports load_provider_config with local imports inside functions
      (app.py:47, session_manager.py:265, session_manager.py:413), so the
      module-level attribute is resolved at call time.
    - Distro is fully self-contained: all provider setup goes through Distro's UI.
    """
    try:
        import amplifierd.providers as _amp_providers
    except ImportError:
        return  # amplifierd not installed; nothing to patch

    _original_load = _amp_providers.load_provider_config
    distro_home = settings.distro_home

    def _merged_load_provider_config(home=None):
        """Load providers from Distro's own settings only."""
        return _original_load(distro_home)

    _amp_providers.load_provider_config = _merged_load_provider_config


def _reconcile_provider_overlay(settings: DistroPluginSettings) -> None:
    """Ensure every provider in Distro settings has a matching overlay include.

    Migrates providers that were configured before the overlay-include fix
    (e.g. keyless providers like GitHub Copilot that previously skipped
    step 3 of registration).

    Only reads from ``distro_home/settings.yaml``.  The CLI's settings.yaml
    is intentionally ignored: we cannot reliably distinguish entries written
    by Distro from entries written by the CLI, and reading it would add
    providers the user never activated through Distro.
    """
    import logging
    from pathlib import Path

    import yaml

    from distro_plugin.overlay import add_include, get_includes
    from distro_plugin.providers import PROVIDERS

    logger = logging.getLogger(__name__)

    distro_settings_path = Path(settings.distro_home) / "settings.yaml"

    if not distro_settings_path.is_file():
        return
    try:
        data = yaml.safe_load(distro_settings_path.read_text()) or {}
    except Exception:
        return

    raw_providers = data.get("config", {}).get("providers", []) or []
    configured_providers = [p for p in raw_providers if isinstance(p, dict)]

    if not configured_providers:
        return

    # Get current overlay includes
    current_includes = set(get_includes(settings))

    # Check each configured provider against the catalog
    migrated: list[str] = []
    for entry in configured_providers:
        provider_id = entry.get("id")
        module_id = entry.get("module")

        # Find matching catalog entry (id takes priority, fall back to module)
        catalog_entry = None
        if provider_id and provider_id in PROVIDERS:
            catalog_entry = PROVIDERS[provider_id]
        elif module_id:
            for p in PROVIDERS.values():
                if p.module_id == module_id:
                    catalog_entry = p
                    break

        if catalog_entry is None:
            continue

        if catalog_entry.include not in current_includes:
            try:
                add_include(settings, catalog_entry.include)
                current_includes.add(catalog_entry.include)
                migrated.append(catalog_entry.id)
            except OSError:
                pass

    if migrated:
        logger.info("Migrated %d provider(s) to overlay: %s", len(migrated), migrated)


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

    # Patch load_provider_config so Distro sessions use only Distro's own providers
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

    # Reconcile provider overlay includes: ensure every provider recorded in
    # Distro's own settings has a matching include in the overlay bundle.
    # This migrates keyless providers (e.g. GitHub Copilot) that were
    # registered before the overlay-include step was unconditional.
    _reconcile_provider_overlay(settings)

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
