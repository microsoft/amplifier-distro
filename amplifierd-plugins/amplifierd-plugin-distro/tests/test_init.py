"""Tests for distro_plugin.__init__ — create_router bundle registry integration."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import yaml


def _make_overlay(tmp_path):
    """Create a minimal overlay bundle.yaml under tmp_path."""
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "bundle.yaml").write_text("bundle:\n  name: test\nincludes: []\n")
    return bundle_dir


def test_create_router_registers_overlay_when_exists(tmp_path):
    """register() is called with {"distro": overlay_dir} when overlay exists."""
    from distro_plugin import create_router

    _make_overlay(tmp_path)

    mock_registry = MagicMock()
    state = SimpleNamespace(bundle_registry=mock_registry)

    with patch.dict(os.environ, {"DISTRO_PLUGIN_DISTRO_HOME": str(tmp_path)}):
        create_router(state)

    expected_overlay_dir = str(tmp_path / "bundle")
    mock_registry.register.assert_called_once_with({"distro": expected_overlay_dir})


def test_create_router_skips_registration_when_no_overlay(tmp_path):
    """register() is NOT called when no overlay bundle.yaml exists."""
    from distro_plugin import create_router

    # tmp_path exists but has no bundle/bundle.yaml
    mock_registry = MagicMock()
    state = SimpleNamespace(bundle_registry=mock_registry)

    with patch.dict(os.environ, {"DISTRO_PLUGIN_DISTRO_HOME": str(tmp_path)}):
        create_router(state)

    mock_registry.register.assert_not_called()


def test_create_router_handles_no_bundle_registry(tmp_path):
    """No error raised when state has no bundle_registry attribute."""
    from distro_plugin import create_router

    _make_overlay(tmp_path)

    # state has no bundle_registry attribute at all
    state = SimpleNamespace()

    with patch.dict(os.environ, {"DISTRO_PLUGIN_DISTRO_HOME": str(tmp_path)}):
        create_router(state)  # should not raise


# -- _patch_provider_loading (#new) ------------------------------------------


def test_patch_provider_loading_merges_cli_and_distro_providers(tmp_path):
    """_patch_provider_loading merges CLI + Distro providers, Distro wins on conflict."""
    import sys
    from types import ModuleType
    from unittest.mock import patch as mock_patch

    from distro_plugin import _patch_provider_loading
    from distro_plugin.config import DistroPluginSettings

    settings = DistroPluginSettings(
        distro_home=tmp_path / "distro",
        amplifier_home=tmp_path / "amplifier",
    )

    cli_providers = [{"module": "provider-anthropic", "config": {"priority": 10}}]
    distro_providers = [{"module": "provider-openai", "config": {"priority": 1}}]

    def fake_load(home=None):
        if home is not None and str(home) == str(settings.distro_home):
            return distro_providers
        return cli_providers

    fake_module = ModuleType("amplifierd.providers")
    fake_module.load_provider_config = fake_load

    fake_amplifierd = ModuleType("amplifierd")

    with mock_patch.dict(
        sys.modules,
        {"amplifierd": fake_amplifierd, "amplifierd.providers": fake_module},
    ):
        _patch_provider_loading(settings)
        merged = fake_module.load_provider_config()

    modules = [p["module"] for p in merged]
    assert "provider-openai" in modules, "Distro providers must be included"
    assert "provider-anthropic" in modules, "CLI-only providers must be included"
    # Distro ordering comes first, CLI-only appended after
    assert modules.index("provider-openai") < modules.index("provider-anthropic")


def test_patch_provider_loading_distro_wins_on_module_conflict(tmp_path):
    """When CLI and Distro have the same module, Distro's entry wins."""
    import sys
    from types import ModuleType
    from unittest.mock import patch as mock_patch

    from distro_plugin import _patch_provider_loading
    from distro_plugin.config import DistroPluginSettings

    settings = DistroPluginSettings(
        distro_home=tmp_path / "distro",
        amplifier_home=tmp_path / "amplifier",
    )

    cli_providers = [{"module": "provider-anthropic", "config": {"priority": 5}}]
    distro_providers = [{"module": "provider-anthropic", "config": {"priority": 1}}]

    def fake_load(home=None):
        if home is not None and str(home) == str(settings.distro_home):
            return distro_providers
        return cli_providers

    fake_module = ModuleType("amplifierd.providers")
    fake_module.load_provider_config = fake_load

    fake_amplifierd = ModuleType("amplifierd")

    with mock_patch.dict(
        sys.modules,
        {"amplifierd": fake_amplifierd, "amplifierd.providers": fake_module},
    ):
        _patch_provider_loading(settings)
        merged = fake_module.load_provider_config()

    assert len(merged) == 1, "Conflict should produce a single merged entry"
    assert merged[0]["config"]["priority"] == 1, "Distro entry wins on conflict"


def test_patch_provider_loading_handles_missing_amplifierd(tmp_path):
    """_patch_provider_loading is a no-op when amplifierd is not installed."""
    import sys
    from unittest.mock import patch as mock_patch

    from distro_plugin import _patch_provider_loading
    from distro_plugin.config import DistroPluginSettings

    settings = DistroPluginSettings(
        distro_home=tmp_path / "distro",
        amplifier_home=tmp_path / "amplifier",
    )

    # Remove amplifierd from sys.modules to simulate it not being installed
    modules_without_amplifierd = {
        k: v for k, v in sys.modules.items() if not k.startswith("amplifierd")
    }
    with mock_patch.dict(sys.modules, modules_without_amplifierd, clear=True):
        _patch_provider_loading(settings)  # Should not raise


def test_create_router_installs_provider_patch(tmp_path):
    """create_router() patches amplifierd.providers.load_provider_config at startup."""
    import sys
    from types import ModuleType
    from types import SimpleNamespace
    from unittest.mock import patch as mock_patch

    from distro_plugin import create_router

    def original_fn(home=None):
        return []

    fake_module = ModuleType("amplifierd.providers")
    fake_module.load_provider_config = original_fn
    fake_amplifierd = ModuleType("amplifierd")

    state = SimpleNamespace()

    with mock_patch.dict(
        os.environ,
        {
            "DISTRO_PLUGIN_DISTRO_HOME": str(tmp_path),
            "DISTRO_PLUGIN_AMPLIFIER_HOME": str(tmp_path),
        },
    ):
        with mock_patch.dict(
            sys.modules,
            {"amplifierd": fake_amplifierd, "amplifierd.providers": fake_module},
        ):
            create_router(state)

    # After create_router(), the function on the fake module should be replaced
    assert fake_module.load_provider_config is not original_fn, (
        "create_router() must patch amplifierd.providers.load_provider_config"
    )


# -- _reconcile_provider_overlay ---------------------------------------------


def _write_distro_settings(distro_home: Path, providers: list[dict]) -> None:
    """Write a minimal settings.yaml with config.providers into distro_home."""
    distro_home.mkdir(parents=True, exist_ok=True)
    settings_path = distro_home / "settings.yaml"
    settings_path.write_text(
        yaml.dump({"config": {"providers": providers}}, default_flow_style=False)
    )


def _write_overlay(distro_home: Path, includes: list[str]) -> None:
    """Write a minimal overlay bundle.yaml with the given includes."""
    bundle_dir = distro_home / "bundle"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "bundle.yaml").write_text(
        yaml.dump(
            {"bundle": {"name": "test"}, "includes": [{"bundle": u} for u in includes]},
            default_flow_style=False,
        )
    )


def test_reconcile_adds_missing_provider_include(tmp_path):
    """_reconcile_provider_overlay adds overlay include for providers in settings."""
    from distro_plugin import _reconcile_provider_overlay
    from distro_plugin.config import DistroPluginSettings
    from distro_plugin.overlay import get_includes
    from distro_plugin.providers import PROVIDERS

    distro_home = tmp_path / "distro"
    amplifier_home = tmp_path / "amplifier"

    # Provider in settings but overlay is empty
    _write_distro_settings(
        distro_home,
        [{"id": "github-copilot", "module": "provider-github-copilot"}],
    )
    _write_overlay(distro_home, [])  # no includes yet

    settings = DistroPluginSettings(
        distro_home=distro_home,
        amplifier_home=amplifier_home,
    )

    _reconcile_provider_overlay(settings)

    includes = get_includes(settings)
    copilot_include = PROVIDERS["github-copilot"].include
    assert copilot_include in includes, (
        f"Expected {copilot_include!r} to be added to overlay includes; got {includes}"
    )


def test_reconcile_no_op_when_include_already_present(tmp_path):
    """_reconcile_provider_overlay does not duplicate existing overlay includes."""
    from distro_plugin import _reconcile_provider_overlay
    from distro_plugin.config import DistroPluginSettings
    from distro_plugin.overlay import get_includes
    from distro_plugin.providers import PROVIDERS

    distro_home = tmp_path / "distro"
    amplifier_home = tmp_path / "amplifier"

    copilot_include = PROVIDERS["github-copilot"].include
    _write_distro_settings(
        distro_home,
        [{"id": "github-copilot", "module": "provider-github-copilot"}],
    )
    _write_overlay(distro_home, [copilot_include])  # already present

    settings = DistroPluginSettings(
        distro_home=distro_home,
        amplifier_home=amplifier_home,
    )

    before = get_includes(settings)
    _reconcile_provider_overlay(settings)
    after = get_includes(settings)

    assert after.count(copilot_include) == before.count(copilot_include), (
        "Include should not be duplicated when already present"
    )


def test_reconcile_no_op_when_no_providers_in_settings(tmp_path):
    """_reconcile_provider_overlay is a no-op when settings has no providers."""
    from distro_plugin import _reconcile_provider_overlay
    from distro_plugin.config import DistroPluginSettings
    from distro_plugin.overlay import get_includes

    distro_home = tmp_path / "distro"
    amplifier_home = tmp_path / "amplifier"

    # Settings file exists but has no providers
    distro_home.mkdir(parents=True, exist_ok=True)
    (distro_home / "settings.yaml").write_text(yaml.dump({"config": {}}))
    _write_overlay(distro_home, [])

    settings = DistroPluginSettings(
        distro_home=distro_home,
        amplifier_home=amplifier_home,
    )

    _reconcile_provider_overlay(settings)  # must not raise

    includes = get_includes(settings)
    # Overlay should remain empty (only the distro bundle URI may be added by
    # add_include bootstrapping — but since we didn't call add_include here,
    # the overlay is untouched).
    assert includes == [], f"Expected empty includes, got {includes}"


def test_reconcile_handles_missing_settings_file(tmp_path):
    """_reconcile_provider_overlay is a no-op when settings.yaml does not exist."""
    from distro_plugin import _reconcile_provider_overlay
    from distro_plugin.config import DistroPluginSettings

    distro_home = tmp_path / "distro"
    amplifier_home = tmp_path / "amplifier"

    # No settings.yaml at all
    settings = DistroPluginSettings(
        distro_home=distro_home,
        amplifier_home=amplifier_home,
    )

    _reconcile_provider_overlay(settings)  # must not raise


def test_reconcile_ignores_cli_settings(tmp_path):
    """_reconcile_provider_overlay must NOT read amplifier_home/settings.yaml.

    Providers that exist only in the CLI settings file must not be added to
    the overlay.  Reading from CLI's file is wrong because:
    - We cannot distinguish Distro-written entries from CLI-written entries.
    - It would add providers (e.g. azure-openai, vllm) that the user never
      activated through Distro.
    """
    from distro_plugin import _reconcile_provider_overlay
    from distro_plugin.config import DistroPluginSettings
    from distro_plugin.overlay import get_includes
    from distro_plugin.providers import PROVIDERS

    distro_home = tmp_path / "distro"
    amplifier_home = tmp_path / "amplifier"

    # Provider only in CLI settings — Distro settings has NO providers
    amplifier_home.mkdir(parents=True, exist_ok=True)
    (amplifier_home / "settings.yaml").write_text(
        yaml.dump(
            {
                "config": {
                    "providers": [{"id": "anthropic", "module": "provider-anthropic"}]
                }
            },
            default_flow_style=False,
        )
    )

    # Distro settings exists but is empty (no providers)
    distro_home.mkdir(parents=True, exist_ok=True)
    (distro_home / "settings.yaml").write_text(yaml.dump({"config": {}}))
    _write_overlay(distro_home, [])

    settings = DistroPluginSettings(
        distro_home=distro_home,
        amplifier_home=amplifier_home,
    )

    _reconcile_provider_overlay(settings)

    includes = get_includes(settings)
    anthropic_include = PROVIDERS["anthropic"].include
    assert anthropic_include not in includes, (
        f"CLI-only provider {anthropic_include!r} must NOT be added to overlay; "
        f"got {includes}"
    )
