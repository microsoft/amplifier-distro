"""Tests for distro_plugin.distro_settings — YAML config schema, load/save/update."""

import pytest
import yaml

from distro_plugin.config import DistroPluginSettings
from distro_plugin.distro_settings import (
    DistroSettings,
    load,
    save,
    settings_path,
    update,
)


# -- Helpers ------------------------------------------------------------------


def _make_settings(tmp_path):
    return DistroPluginSettings(
        distro_home=tmp_path / "distro",
        amplifier_home=tmp_path / "amplifier",
    )


# -- Tests --------------------------------------------------------------------


def test_settings_path_returns_correct_path(tmp_path):
    s = _make_settings(tmp_path)
    result = settings_path(s)
    assert result == tmp_path / "distro" / "settings.yaml"


def test_load_returns_defaults_when_no_file(tmp_path):
    s = _make_settings(tmp_path)
    ds = load(s)
    assert isinstance(ds, DistroSettings)
    assert ds.workspace_root == "~"
    assert ds.identity.github_handle == ""
    assert ds.slack.hub_channel_name == "amplifier"


def test_save_and_load_roundtrip(tmp_path):
    s = _make_settings(tmp_path)
    original = DistroSettings(workspace_root="/projects")
    save(s, original)
    loaded = load(s)
    assert loaded.workspace_root == "/projects"
    assert loaded.identity.github_handle == ""  # default preserved


def test_update_root_field(tmp_path):
    s = _make_settings(tmp_path)
    result = update(s, workspace_root="/new/root")
    assert result.workspace_root == "/new/root"
    # Verify persisted
    reloaded = load(s)
    assert reloaded.workspace_root == "/new/root"


def test_update_section_field(tmp_path):
    s = _make_settings(tmp_path)
    result = update(s, section="slack", hub_channel_name="my-channel")
    assert result.slack.hub_channel_name == "my-channel"
    # Verify persisted
    reloaded = load(s)
    assert reloaded.slack.hub_channel_name == "my-channel"


def test_update_preserves_other_fields(tmp_path):
    s = _make_settings(tmp_path)
    # Set up initial state
    update(s, workspace_root="/keep-me", section=None)
    # Now update a section field
    result = update(s, section="slack", hub_channel_name="changed")
    assert result.workspace_root == "/keep-me"
    assert result.slack.hub_channel_name == "changed"
    # Default nested fields preserved
    assert result.slack.socket_mode is False


def test_nested_dataclass_roundtrip(tmp_path):
    s = _make_settings(tmp_path)
    original = DistroSettings()
    original.identity.github_handle = "testuser"
    original.identity.git_email = "test@example.com"
    original.slack.hub_channel_name = "custom-channel"
    original.voice.voice = "nova"
    original.watchdog.check_interval = 60

    save(s, original)
    loaded = load(s)

    assert loaded.identity.github_handle == "testuser"
    assert loaded.identity.git_email == "test@example.com"
    assert loaded.slack.hub_channel_name == "custom-channel"
    assert loaded.voice.voice == "nova"
    assert loaded.watchdog.check_interval == 60
    # Verify the YAML on disk is correct
    raw = yaml.safe_load(settings_path(s).read_text())
    assert raw["identity"]["github_handle"] == "testuser"
    assert raw["voice"]["voice"] == "nova"


def test_load_returns_defaults_for_corrupt_yaml(tmp_path):
    """Malformed or non-dict YAML falls back to defaults."""
    s = _make_settings(tmp_path)
    path = settings_path(s)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write invalid YAML
    path.write_text("[invalid yaml{{{")
    ds = load(s)
    assert isinstance(ds, DistroSettings)
    assert ds.workspace_root == "~"

    # Also test YAML that parses but isn't a dict (e.g. a bare scalar)
    path.write_text("null")
    ds = load(s)
    assert isinstance(ds, DistroSettings)
    assert ds.workspace_root == "~"


def test_update_raises_on_invalid_section(tmp_path):
    """update() with a nonexistent section raises AttributeError."""
    s = _make_settings(tmp_path)
    with pytest.raises(AttributeError):
        update(s, section="nonexistent", some_key="value")


def test_save_preserves_non_owned_sections(tmp_path):
    """save() must not erase keys it doesn't own (e.g. config.providers)."""
    s = _make_settings(tmp_path)
    path = settings_path(s)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Simulate providers.py writing config.providers to the shared file
    existing = {
        "workspace_root": "~/old",
        "config": {"providers": [{"name": "openai", "model": "gpt-4o"}]},
    }
    path.write_text(yaml.dump(existing))

    # Now save() with DistroSettings — should update distro keys but keep config
    ds = DistroSettings(workspace_root="~/new")
    save(s, ds)

    raw = yaml.safe_load(path.read_text())
    # DistroSettings field updated
    assert raw["workspace_root"] == "~/new"
    # config.providers untouched
    assert raw["config"]["providers"] == [{"name": "openai", "model": "gpt-4o"}]


def test_save_with_no_existing_file_creates_file(tmp_path):
    """save() with no prior file still works and writes all DistroSettings fields."""
    s = _make_settings(tmp_path)
    ds = DistroSettings(workspace_root="/fresh")
    save(s, ds)

    raw = yaml.safe_load(settings_path(s).read_text())
    assert raw["workspace_root"] == "/fresh"
    assert "identity" in raw
    assert "slack" in raw


def test_update_preserves_non_owned_sections(tmp_path):
    """update() round-trip must not erase keys it doesn't own."""
    s = _make_settings(tmp_path)
    path = settings_path(s)
    path.parent.mkdir(parents=True, exist_ok=True)

    existing = {
        "workspace_root": "~/before",
        "config": {"providers": [{"name": "anthropic"}]},
    }
    path.write_text(yaml.dump(existing))

    update(s, workspace_root="~/after")

    raw = yaml.safe_load(path.read_text())
    assert raw["workspace_root"] == "~/after"
    assert raw["config"]["providers"] == [{"name": "anthropic"}]
