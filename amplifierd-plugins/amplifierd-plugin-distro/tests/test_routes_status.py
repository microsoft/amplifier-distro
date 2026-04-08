"""Tests for GET /distro/status and GET /distro/detect endpoints."""

from __future__ import annotations


def test_status_returns_expected_structure_defaults_unconfigured(client):
    """GET /distro/status returns phase, provider, features; defaults to 'unconfigured'."""
    resp = client.get("/distro/status")
    assert resp.status_code == 200

    data = resp.json()

    # Required top-level keys
    assert "phase" in data
    assert "provider" in data
    assert "features" in data

    # Default phase when nothing is configured
    assert data["phase"] == "unconfigured"


def test_detect_returns_expected_structure(client):
    """GET /distro/detect returns github, git, api_keys, workspace_candidates, and flat convenience fields."""
    resp = client.get("/distro/detect")
    assert resp.status_code == 200

    data = resp.json()

    # Required top-level keys
    assert "github" in data
    assert "git" in data
    assert "api_keys" in data
    assert "workspace_candidates" in data

    # Flat convenience fields should exist
    assert "github_user" in data
    assert "git_name" in data
    assert "git_email" in data


def test_status_phase_ready_when_overlay_and_provider(settings, client, monkeypatch):
    """Status phase becomes 'ready' when overlay exists AND provider is fully configured in Distro's settings."""
    from distro_plugin.overlay import add_include
    from distro_plugin.providers import PROVIDERS, add_provider_config

    # Set a provider API key in environment
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key-12345")

    # Register provider in Distro's settings.yaml (in_settings = True)
    add_provider_config(settings, "anthropic")

    # Add provider include to overlay (in_overlay = True)
    add_include(settings, PROVIDERS["anthropic"].include)

    resp = client.get("/distro/status")
    assert resp.status_code == 200

    data = resp.json()
    assert data["phase"] == "ready"


def test_status_phase_detected_when_overlay_and_env_key_but_no_configured_provider(
    settings, client, monkeypatch
):
    """Status phase is 'detected' when overlay exists and env key set, but provider not in Distro settings.

    This covers existing users upgrading: they may have ANTHROPIC_API_KEY set in their
    environment and the overlay exists, but have never registered a provider through
    Distro's UI. They should see the wizard's provider step once more.
    """
    from distro_plugin.overlay import add_include
    from distro_plugin.providers import PROVIDERS

    # Create overlay so overlay_exists() returns True
    add_include(settings, PROVIDERS["anthropic"].include)

    # Key is in env — but provider is NOT registered in distro_home/settings.yaml
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key-12345")

    resp = client.get("/distro/status")
    assert resp.status_code == 200

    data = resp.json()
    assert data["phase"] == "detected", (
        "Expected 'detected': overlay exists and env key is set, "
        "but no provider is fully configured in Distro's own settings"
    )


def test_status_phase_detected_when_overlay_but_no_provider_key(
    settings, client, monkeypatch
):
    """Status phase becomes 'detected' when overlay exists but no provider key is set."""
    from distro_plugin.overlay import add_include
    from distro_plugin.providers import PROVIDERS

    # Create the overlay so overlay_exists() returns True
    add_include(settings, PROVIDERS["anthropic"].include)

    # Ensure no provider env vars are set
    for provider in PROVIDERS.values():
        monkeypatch.delenv(provider.env_var, raising=False)

    # No provider env var set — phase should be 'detected' (not 'ready')
    resp = client.get("/distro/status")
    assert resp.status_code == 200

    data = resp.json()
    assert data["phase"] == "detected"


def test_status_features_is_dict_keyed_by_feature_id(client):
    """_build_status returns 'features' as a dict keyed by feature ID, not a list."""
    resp = client.get("/distro/status")
    assert resp.status_code == 200

    data = resp.json()
    features = data["features"]

    # Must be a dict, not a list
    assert isinstance(features, dict), (
        f"Expected features to be dict, got {type(features)}"
    )


def test_status_features_contains_all_catalog_features(client):
    """_build_status returns ALL features from the catalog, not just enabled ones."""
    from distro_plugin.features import FEATURES

    resp = client.get("/distro/status")
    data = resp.json()
    features = data["features"]

    for fid in FEATURES:
        assert fid in features, f"Feature '{fid}' missing from status response"


def test_status_features_entries_have_required_fields(client):
    """Each feature entry in _build_status has 'enabled', 'tier', 'name', 'description'."""
    resp = client.get("/distro/status")
    data = resp.json()
    features = data["features"]

    for fid, feat_data in features.items():
        assert "enabled" in feat_data, f"Feature {fid} missing 'enabled'"
        assert "tier" in feat_data, f"Feature {fid} missing 'tier'"
        assert "name" in feat_data, f"Feature {fid} missing 'name'"
        assert "description" in feat_data, f"Feature {fid} missing 'description'"


def test_status_features_all_disabled_when_no_overlay(client):
    """_build_status shows all features disabled when no overlay exists."""
    resp = client.get("/distro/status")
    data = resp.json()
    features = data["features"]

    for fid, feat_data in features.items():
        assert feat_data["enabled"] is False, f"Feature {fid} should be disabled"


def test_status_features_shows_enabled_after_add(settings, client):
    """_build_status reflects a feature as enabled after its includes are added."""
    from distro_plugin.features import FEATURES
    from distro_plugin.overlay import add_include

    feat = FEATURES["dev-memory"]
    for inc in feat.includes:
        add_include(settings, inc)

    resp = client.get("/distro/status")
    data = resp.json()
    features = data["features"]

    assert features["dev-memory"]["enabled"] is True
    # Other features should still be disabled
    assert features["recipes"]["enabled"] is False


def test_run_command_returns_empty_on_missing_executable():
    """_run_command returns '' when the command executable does not exist."""
    import asyncio

    from distro_plugin.routes import _run_command

    result = asyncio.run(_run_command("__nonexistent_command_xyz__", "--version"))
    assert result == ""
