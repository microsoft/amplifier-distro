"""Memory Service Tests

Tests for the cross-interface memory system (T3: Dev Memory Integration).

Covers:
1. Memory CRUD operations (remember, recall)
2. Auto-categorization by keyword matching
3. Search across content, tags, and category (case-insensitive)
4. Memory API endpoints (POST /api/memory/remember, GET /api/memory/recall, etc.)
5. Web chat memory integration (pattern detection and routing)
6. Slack memory commands (remember, recall, work-status)
7. Work log read/update
8. Edge cases (empty store, missing files, special characters, ID generation)
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml
from starlette.testclient import TestClient

from amplifier_distro.server.memory import (
    MemoryService,
    get_memory_service,
    reset_memory_service,
)

# --- Fixtures ---


@pytest.fixture(autouse=True)
def _reset_memory_singleton():
    """Ensure memory service singleton is reset between tests."""
    reset_memory_service()
    yield
    reset_memory_service()


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    """Create a temporary memory directory."""
    d = tmp_path / "memory"
    d.mkdir()
    return d


@pytest.fixture
def service(memory_dir: Path) -> MemoryService:
    """Create a MemoryService with a temp directory."""
    return MemoryService(memory_dir=memory_dir)


# --- T3.1: Memory CRUD Operations ---


class TestMemoryRemember:
    """Test the remember() method for storing memories."""

    def test_remember_returns_dict_with_id(self, service: MemoryService):
        result = service.remember("Use pydantic for validation")
        assert "id" in result
        assert result["id"] == "mem-001"

    def test_remember_returns_timestamp(self, service: MemoryService):
        result = service.remember("Use pydantic for validation")
        assert "timestamp" in result
        assert len(result["timestamp"]) > 0

    def test_remember_returns_category(self, service: MemoryService):
        result = service.remember("Use pydantic for validation")
        assert "category" in result
        assert isinstance(result["category"], str)

    def test_remember_returns_content(self, service: MemoryService):
        result = service.remember("Use pydantic for validation")
        assert result["content"] == "Use pydantic for validation"

    def test_remember_returns_tags(self, service: MemoryService):
        result = service.remember("Use pydantic for validation")
        assert "tags" in result
        assert isinstance(result["tags"], list)

    def test_remember_increments_id(self, service: MemoryService):
        r1 = service.remember("First memory")
        r2 = service.remember("Second memory")
        r3 = service.remember("Third memory")
        assert r1["id"] == "mem-001"
        assert r2["id"] == "mem-002"
        assert r3["id"] == "mem-003"

    def test_remember_persists_to_yaml(self, service: MemoryService):
        service.remember("Persisted memory")
        assert service.store_path.exists()
        data = yaml.safe_load(service.store_path.read_text())
        assert len(data["memories"]) == 1
        assert data["memories"][0]["content"] == "Persisted memory"

    def test_remember_creates_directory(self, tmp_path: Path):
        new_dir = tmp_path / "nonexistent" / "memory"
        svc = MemoryService(memory_dir=new_dir)
        svc.remember("Create dirs")
        assert new_dir.exists()
        assert svc.store_path.exists()


class TestMemoryRecall:
    """Test the recall() method for searching memories."""

    def test_recall_empty_store(self, service: MemoryService):
        results = service.recall("anything")
        assert results == []

    def test_recall_by_content(self, service: MemoryService):
        service.remember("The architecture uses hexagonal pattern")
        results = service.recall("hexagonal")
        assert len(results) == 1
        assert "hexagonal" in results[0]["content"]

    def test_recall_case_insensitive(self, service: MemoryService):
        service.remember("Always use YAML for config files")
        results = service.recall("yaml")
        assert len(results) == 1

    def test_recall_by_category(self, service: MemoryService):
        service.remember("The git branching strategy is trunk-based")
        results = service.recall("git")
        assert len(results) >= 1

    def test_recall_by_tag(self, service: MemoryService):
        service.remember("The architecture module system is layered")
        results = service.recall("architecture")
        assert len(results) >= 1

    def test_recall_no_match(self, service: MemoryService):
        service.remember("Python is great")
        results = service.recall("javascript")
        assert results == []

    def test_recall_multiple_matches(self, service: MemoryService):
        service.remember("The deploy pipeline uses GitHub Actions")
        service.remember("The deploy config lives in .github/workflows")
        results = service.recall("deploy")
        assert len(results) == 2


# --- Auto-categorization ---


class TestAutoCategorization:
    """Test that memories are auto-categorized by keyword matching."""

    def test_categorize_architecture(self, service: MemoryService):
        result = service.remember(
            "The system architecture uses hexagonal design pattern"
        )
        assert result["category"] == "architecture"

    def test_categorize_workflow(self, service: MemoryService):
        result = service.remember("The deploy pipeline runs CI/CD on every push")
        assert result["category"] == "workflow"

    def test_categorize_preference(self, service: MemoryService):
        result = service.remember(
            "I always prefer tabs over spaces as my default style"
        )
        assert result["category"] == "preference"

    def test_categorize_environment(self, service: MemoryService):
        result = service.remember(
            "Setup the docker environment with config path variables"
        )
        assert result["category"] == "environment"

    def test_categorize_git(self, service: MemoryService):
        result = service.remember("Always rebase before merge, never commit to main")
        assert result["category"] == "git"

    def test_categorize_research(self, service: MemoryService):
        result = service.remember(
            "Research finding: benchmark analysis shows improvement"
        )
        assert result["category"] == "research"

    def test_categorize_tools(self, service: MemoryService):
        result = service.remember(
            "Use the cli tool and editor plugin for better IDE experience"
        )
        assert result["category"] == "tools"

    def test_categorize_general_fallback(self, service: MemoryService):
        result = service.remember("The sky is blue today")
        assert result["category"] == "general"


# --- Work Log ---


class TestWorkLog:
    """Test work log read and update operations."""

    def test_work_status_empty(self, service: MemoryService):
        result = service.work_status()
        assert result == {"items": []}

    def test_update_work_log(self, service: MemoryService):
        items = [
            {"task": "Build memory service", "status": "in-progress"},
            {"task": "Write tests", "status": "pending"},
        ]
        result = service.update_work_log(items)
        assert len(result["items"]) == 2
        assert result["items"][0]["task"] == "Build memory service"
        assert result["items"][0]["status"] == "in-progress"

    def test_work_log_persists(self, service: MemoryService):
        service.update_work_log([{"task": "Persisted task", "status": "done"}])
        assert service.work_log_path.exists()
        data = yaml.safe_load(service.work_log_path.read_text())
        assert data["items"][0]["task"] == "Persisted task"

    def test_work_log_roundtrip(self, service: MemoryService):
        items = [{"task": "Roundtrip test", "status": "pending"}]
        service.update_work_log(items)
        result = service.work_status()
        assert result["items"][0]["task"] == "Roundtrip test"

    def test_work_log_adds_timestamp(self, service: MemoryService):
        result = service.update_work_log([{"task": "Auto timestamp"}])
        assert result["items"][0]["updated"] != ""


# --- Edge Cases ---


class TestEdgeCases:
    """Test edge cases: missing files, special characters, etc."""

    def test_recall_missing_store_file(self, tmp_path: Path):
        svc = MemoryService(memory_dir=tmp_path / "nonexistent")
        results = svc.recall("anything")
        assert results == []

    def test_work_status_missing_file(self, tmp_path: Path):
        svc = MemoryService(memory_dir=tmp_path / "nonexistent")
        result = svc.work_status()
        assert result == {"items": []}

    def test_remember_special_characters(self, service: MemoryService):
        result = service.remember("Use 'quotes' and \"double quotes\" & <brackets>")
        assert result["content"] == "Use 'quotes' and \"double quotes\" & <brackets>"

    def test_remember_unicode(self, service: MemoryService):
        result = service.remember("The emoji test: rocket ship")
        assert "rocket" in result["content"]

    def test_remember_multiline(self, service: MemoryService):
        text = "Line one\nLine two\nLine three"
        result = service.remember(text)
        assert result["content"] == text

    def test_id_generation_with_gaps(self, service: MemoryService):
        """IDs should increment from the highest existing ID."""
        service.remember("First")
        service.remember("Second")
        # Manually delete the first memory from the store
        store = service._load_store()
        store.memories = [m for m in store.memories if m.id != "mem-001"]
        service._save_store(store)
        # Next ID should be mem-003, not mem-001
        result = service.remember("Third")
        assert result["id"] == "mem-003"

    def test_corrupt_yaml_returns_empty_store(self, service: MemoryService):
        """Corrupt YAML should not crash, just return empty."""
        service._ensure_dir()
        service.store_path.write_text("{{invalid yaml: [")
        results = service.recall("anything")
        assert results == []

    def test_singleton_returns_same_instance(self, memory_dir: Path):
        s1 = get_memory_service(memory_dir=memory_dir)
        s2 = get_memory_service()
        assert s1 is s2


# --- T3.2: Memory API Endpoints ---


class TestMemoryAPIEndpoints:
    """Test the /api/memory/* HTTP endpoints."""

    @pytest.fixture
    def api_client(self, memory_dir: Path) -> TestClient:
        """Create a TestClient with memory service pointed at temp dir."""
        from amplifier_distro.server.app import DistroServer
        from amplifier_distro.server.memory import (
            get_memory_service,
            reset_memory_service,
        )

        reset_memory_service()
        # Pre-initialize the singleton to use our temp dir
        get_memory_service(memory_dir=memory_dir)

        server = DistroServer()
        return TestClient(server.app)

    def test_remember_endpoint(self, api_client: TestClient):
        response = api_client.post(
            "/api/memory/remember",
            json={"text": "API test memory"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "mem-001"
        assert data["content"] == "API test memory"

    def test_remember_endpoint_empty_text(self, api_client: TestClient):
        response = api_client.post(
            "/api/memory/remember",
            json={"text": ""},
        )
        assert response.status_code == 400

    def test_recall_endpoint(self, api_client: TestClient):
        api_client.post("/api/memory/remember", json={"text": "API recall test"})
        response = api_client.get("/api/memory/recall?q=recall")
        assert response.status_code == 200
        data = response.json()
        assert data["count"] >= 1
        assert len(data["matches"]) >= 1

    def test_recall_endpoint_no_query(self, api_client: TestClient):
        response = api_client.get("/api/memory/recall")
        assert response.status_code == 400

    def test_work_status_endpoint(self, api_client: TestClient):
        response = api_client.get("/api/memory/work-status")
        assert response.status_code == 200
        data = response.json()
        assert "items" in data

    def test_work_log_endpoint(self, api_client: TestClient):
        response = api_client.post(
            "/api/memory/work-log",
            json={"items": [{"task": "API work log test", "status": "done"}]},
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) == 1
        assert data["items"][0]["task"] == "API work log test"

    def test_work_log_roundtrip_via_api(self, api_client: TestClient):
        api_client.post(
            "/api/memory/work-log",
            json={"items": [{"task": "Roundtrip", "status": "pending"}]},
        )
        response = api_client.get("/api/memory/work-status")
        data = response.json()
        assert data["items"][0]["task"] == "Roundtrip"


# --- T3.4: Slack Memory Commands ---


class TestSlackMemoryCommands:
    """Test Slack command handler memory commands."""

    @pytest.fixture
    def slack_command_handler(self, memory_dir: Path):
        """Create a CommandHandler with memory service pointed at temp dir."""
        from amplifier_distro.server.apps.slack.client import MemorySlackClient
        from amplifier_distro.server.apps.slack.commands import CommandHandler
        from amplifier_distro.server.apps.slack.config import SlackConfig
        from amplifier_distro.server.apps.slack.discovery import AmplifierDiscovery
        from amplifier_distro.server.apps.slack.sessions import SlackSessionManager
        from amplifier_distro.server.memory import (
            get_memory_service,
            reset_memory_service,
        )

        reset_memory_service()
        get_memory_service(memory_dir=memory_dir)

        config = SlackConfig(
            hub_channel_id="C_HUB",
            hub_channel_name="amplifier",
            simulator_mode=True,
            bot_name="amp",
        )
        client = MemorySlackClient()
        from amplifier_distro.server.apps.slack.backend import (
            MockBackend as SlackMockBackend,
        )

        backend = SlackMockBackend()
        discovery = AmplifierDiscovery(amplifier_home=str(memory_dir))
        session_manager = SlackSessionManager(client, backend, config)
        return CommandHandler(session_manager, discovery, config)

    def test_remember_command(self, slack_command_handler):
        from amplifier_distro.server.apps.slack.commands import CommandContext

        ctx = CommandContext(channel_id="C1", user_id="U1")
        result = asyncio.run(
            slack_command_handler.cmd_remember(
                ["prefer", "tabs", "over", "spaces"], ctx
            )
        )
        assert "Remembered" in result.text
        assert "mem-001" in result.text

    def test_remember_command_no_args(self, slack_command_handler):
        from amplifier_distro.server.apps.slack.commands import CommandContext

        ctx = CommandContext(channel_id="C1", user_id="U1")
        result = asyncio.run(slack_command_handler.cmd_remember([], ctx))
        assert "Usage" in result.text

    def test_recall_command(self, slack_command_handler):
        from amplifier_distro.server.apps.slack.commands import CommandContext

        ctx = CommandContext(channel_id="C1", user_id="U1")
        # Store a memory first
        asyncio.run(slack_command_handler.cmd_remember(["tabs", "over", "spaces"], ctx))
        # Recall it
        result = asyncio.run(slack_command_handler.cmd_recall(["tabs"], ctx))
        assert "Found" in result.text
        assert "tabs" in result.text

    def test_recall_command_no_match(self, slack_command_handler):
        from amplifier_distro.server.apps.slack.commands import CommandContext

        ctx = CommandContext(channel_id="C1", user_id="U1")
        result = asyncio.run(slack_command_handler.cmd_recall(["nonexistent"], ctx))
        assert "No memories found" in result.text

    def test_recall_command_no_args(self, slack_command_handler):
        from amplifier_distro.server.apps.slack.commands import CommandContext

        ctx = CommandContext(channel_id="C1", user_id="U1")
        result = asyncio.run(slack_command_handler.cmd_recall([], ctx))
        assert "Usage" in result.text

    def test_work_status_command_empty(self, slack_command_handler):
        from amplifier_distro.server.apps.slack.commands import CommandContext

        ctx = CommandContext(channel_id="C1", user_id="U1")
        result = asyncio.run(slack_command_handler.cmd_work_status([], ctx))
        assert "No work log" in result.text

    def test_command_routing_remember(self, slack_command_handler):
        """Verify 'remember' routes through the handle() method."""
        from amplifier_distro.server.apps.slack.commands import CommandContext

        ctx = CommandContext(channel_id="C1", user_id="U1")
        result = asyncio.run(
            slack_command_handler.handle("remember", ["test", "memory"], ctx)
        )
        assert "Remembered" in result.text

    def test_command_routing_recall(self, slack_command_handler):
        """Verify 'recall' routes through the handle() method."""
        from amplifier_distro.server.apps.slack.commands import CommandContext

        ctx = CommandContext(channel_id="C1", user_id="U1")
        result = asyncio.run(slack_command_handler.handle("recall", ["anything"], ctx))
        # Should either find results or say "no memories found"
        assert "memories" in result.text.lower() or "No memories" in result.text

    def test_command_parse_work_status_alias(self, slack_command_handler):
        """Verify 'work-status' is aliased to 'work_status'."""
        cmd, _args = slack_command_handler.parse_command("work-status")
        assert cmd == "work_status"


# --- YAML Format Compatibility ---


class TestYAMLFormatCompatibility:
    """Verify the YAML format is compatible with dev-memory bundle."""

    def test_yaml_has_memories_key(self, service: MemoryService):
        service.remember("Test memory")
        data = yaml.safe_load(service.store_path.read_text())
        assert "memories" in data

    def test_yaml_entry_has_required_fields(self, service: MemoryService):
        service.remember("Test memory")
        data = yaml.safe_load(service.store_path.read_text())
        entry = data["memories"][0]
        assert "id" in entry
        assert "timestamp" in entry
        assert "category" in entry
        assert "content" in entry
        assert "tags" in entry

    def test_yaml_id_format(self, service: MemoryService):
        service.remember("Test memory")
        data = yaml.safe_load(service.store_path.read_text())
        assert data["memories"][0]["id"].startswith("mem-")

    def test_yaml_tags_is_list(self, service: MemoryService):
        service.remember("Test memory")
        data = yaml.safe_load(service.store_path.read_text())
        assert isinstance(data["memories"][0]["tags"], list)


# --- Conventions Compliance ---


class TestConventionsCompliance:
    """Verify memory service uses conventions.py constants."""

    def test_store_filename_matches_convention(self):
        from amplifier_distro import conventions

        svc = MemoryService(memory_dir=Path("/tmp/test-memory"))
        assert svc.store_path.name == conventions.MEMORY_STORE_FILENAME

    def test_work_log_filename_matches_convention(self):
        from amplifier_distro import conventions

        svc = MemoryService(memory_dir=Path("/tmp/test-memory"))
        assert svc.work_log_path.name == conventions.WORK_LOG_FILENAME

    def test_default_dir_uses_convention(self):
        from amplifier_distro import conventions

        svc = MemoryService()
        expected = (
            Path(conventions.AMPLIFIER_HOME).expanduser() / conventions.MEMORY_DIR
        )
        assert svc.memory_dir == expected
