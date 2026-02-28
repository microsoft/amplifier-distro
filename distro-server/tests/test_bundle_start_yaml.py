"""Tests for bundle/behaviors/start.yaml structure and content."""

from pathlib import Path

import yaml

BUNDLE_START_YAML = (
    Path(__file__).parent.parent.parent / "bundle" / "behaviors" / "start.yaml"
)


def load_start_yaml() -> dict:
    """Load and parse the start.yaml bundle file."""
    return yaml.safe_load(BUNDLE_START_YAML.read_text())


class TestStartYamlStructure:
    def test_file_is_valid_yaml(self):
        """start.yaml must be valid YAML."""
        data = load_start_yaml()
        assert isinstance(data, dict)

    def test_bundle_metadata_present(self):
        """bundle: metadata block must be present."""
        data = load_start_yaml()
        assert "bundle" in data
        assert data["bundle"]["name"] == "start-behavior"

    def test_hooks_section_present(self):
        """hooks: section must be present."""
        data = load_start_yaml()
        assert "hooks" in data
        assert isinstance(data["hooks"], list)

    def test_hooks_handoff_present(self):
        """hooks-handoff entry must remain in hooks: section."""
        data = load_start_yaml()
        hook_modules = [h["module"] for h in data["hooks"]]
        assert "hooks-handoff" in hook_modules

    def test_hooks_preflight_present(self):
        """hooks-preflight entry must remain in hooks: section."""
        data = load_start_yaml()
        hook_modules = [h["module"] for h in data["hooks"]]
        assert "hooks-preflight" in hook_modules

    def test_agents_section_present(self):
        """agents: section must be present."""
        data = load_start_yaml()
        assert "agents" in data

    def test_context_section_present(self):
        """context: section must be present."""
        data = load_start_yaml()
        assert "context" in data


class TestHooksSessionNaming:
    def test_hooks_session_naming_present(self):
        """hooks-session-naming must be present in the hooks: section."""
        data = load_start_yaml()
        hook_modules = [h["module"] for h in data["hooks"]]
        assert "hooks-session-naming" in hook_modules, (
            "hooks-session-naming not found in hooks: section"
        )

    def test_hooks_session_naming_source(self):
        """hooks-session-naming must have the correct source URL."""
        data = load_start_yaml()
        entry = next(
            (h for h in data["hooks"] if h["module"] == "hooks-session-naming"), None
        )
        assert entry is not None, "hooks-session-naming not found in hooks: section"
        expected_source = "git+https://github.com/microsoft/amplifier-foundation@main#subdirectory=modules/hooks-session-naming"
        assert entry["source"] == expected_source, (
            f"Expected source '{expected_source}', got '{entry.get('source')}'"
        )

    def test_hooks_session_naming_config_initial_trigger_turn(self):
        """hooks-session-naming config must have initial_trigger_turn: 2."""
        data = load_start_yaml()
        entry = next(
            (h for h in data["hooks"] if h["module"] == "hooks-session-naming"), None
        )
        assert entry is not None, "hooks-session-naming not found in hooks: section"
        assert entry["config"]["initial_trigger_turn"] == 2

    def test_hooks_session_naming_config_update_interval_turns(self):
        """hooks-session-naming config must have update_interval_turns: 5."""
        data = load_start_yaml()
        entry = next(
            (h for h in data["hooks"] if h["module"] == "hooks-session-naming"), None
        )
        assert entry is not None, "hooks-session-naming not found in hooks: section"
        assert entry["config"]["update_interval_turns"] == 5

    def test_hooks_session_naming_after_preflight(self):
        """hooks-session-naming must appear after hooks-preflight."""
        data = load_start_yaml()
        hook_modules = [h["module"] for h in data["hooks"]]
        preflight_idx = hook_modules.index("hooks-preflight")
        naming_idx = hook_modules.index("hooks-session-naming")
        assert naming_idx > preflight_idx, (
            "hooks-session-naming should appear after hooks-preflight"
        )
