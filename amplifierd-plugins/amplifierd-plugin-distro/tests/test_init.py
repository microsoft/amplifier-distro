"""Tests for distro_plugin.__init__ — create_router bundle registry integration."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


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
