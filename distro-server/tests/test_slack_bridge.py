"""Tests for the Slack bridge.

Covers: models, config, client, formatter, discovery, backend,
session management, commands, events, and HTTP endpoints.
"""

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

# --- Fixtures ---


@pytest.fixture
def slack_client():
    """Create a fresh MemorySlackClient."""
    from amplifier_distro.server.apps.slack.client import MemorySlackClient

    return MemorySlackClient()


@pytest.fixture
def mock_backend():
    """Create a fresh MockBackend."""
    from amplifier_distro.server.apps.slack.backend import MockBackend

    return MockBackend()


@pytest.fixture
def slack_config():
    """Create a test SlackConfig."""
    from amplifier_distro.server.apps.slack.config import SlackConfig

    return SlackConfig(
        hub_channel_id="C_HUB",
        hub_channel_name="amplifier",
        simulator_mode=True,
        bot_name="amp",
    )


@pytest.fixture
def session_manager(slack_client, mock_backend, slack_config):
    """Create a SlackSessionManager with test dependencies."""
    from amplifier_distro.server.apps.slack.sessions import SlackSessionManager

    return SlackSessionManager(slack_client, mock_backend, slack_config)


@pytest.fixture
def discovery(tmp_path):
    """Create an AmplifierDiscovery pointed at a temp directory."""
    from amplifier_distro.server.apps.slack.discovery import AmplifierDiscovery

    return AmplifierDiscovery(amplifier_home=str(tmp_path))


@pytest.fixture
def command_handler(session_manager, discovery, slack_config):
    """Create a CommandHandler with test dependencies."""
    from amplifier_distro.server.apps.slack.commands import CommandHandler

    return CommandHandler(session_manager, discovery, slack_config)


@pytest.fixture
def bridge_client(slack_client, mock_backend, slack_config, discovery):
    """Create a TestClient for the Slack bridge HTTP endpoints."""
    from amplifier_distro.server.app import DistroServer
    from amplifier_distro.server.apps.slack import _state, initialize, manifest

    # Clear any previous state
    _state.clear()

    server = DistroServer()

    # Initialize with injected test dependencies
    initialize(
        config=slack_config,
        client=slack_client,
        backend=mock_backend,
        discovery=discovery,
    )

    server.register_app(manifest)
    return TestClient(server.app)


# --- Model Tests ---


class TestSlackModels:
    """Test data models for correctness and edge cases."""

    def test_slack_message_conversation_key_top_level(self):
        from amplifier_distro.server.apps.slack.models import SlackMessage

        msg = SlackMessage(channel_id="C123", user_id="U1", text="hi", ts="1.0")
        assert msg.conversation_key == "C123"
        assert not msg.is_threaded

    def test_slack_message_conversation_key_threaded(self):
        from amplifier_distro.server.apps.slack.models import SlackMessage

        msg = SlackMessage(
            channel_id="C123", user_id="U1", text="hi", ts="2.0", thread_ts="1.0"
        )
        assert msg.conversation_key == "C123:1.0"
        assert msg.is_threaded

    def test_session_mapping_conversation_key(self):
        from amplifier_distro.server.apps.slack.models import SessionMapping

        m1 = SessionMapping(session_id="s1", channel_id="C1")
        assert m1.conversation_key == "C1"

        m2 = SessionMapping(session_id="s2", channel_id="C1", thread_ts="1.0")
        assert m2.conversation_key == "C1:1.0"

    def test_session_mapping_defaults(self):
        from amplifier_distro.server.apps.slack.models import SessionMapping

        m = SessionMapping(session_id="test", channel_id="C1")
        assert m.is_active is True
        assert m.created_at  # Should have a default timestamp
        assert m.last_active

    def test_session_mapping_has_working_dir(self):
        """SessionMapping has a working_dir field that defaults to empty string."""
        from amplifier_distro.server.apps.slack.models import SessionMapping

        m = SessionMapping(session_id="s1", channel_id="C1", working_dir="~/repo/foo")
        assert m.working_dir == "~/repo/foo"

        m_default = SessionMapping(session_id="s2", channel_id="C2")
        assert m_default.working_dir == ""

    def test_channel_type_enum(self):
        from amplifier_distro.server.apps.slack.models import ChannelType

        assert ChannelType.HUB == "hub"
        assert ChannelType.SESSION == "session"


# --- Config Tests ---


class TestSlackConfig:
    """Test configuration loading and mode detection."""

    def test_default_config(self):
        from amplifier_distro.server.apps.slack.config import SlackConfig

        cfg = SlackConfig()
        assert not cfg.is_configured
        assert cfg.mode == "unconfigured"

    def test_simulator_mode(self):
        from amplifier_distro.server.apps.slack.config import SlackConfig

        cfg = SlackConfig(simulator_mode=True)
        assert cfg.mode == "simulator"

    def test_live_mode(self):
        from amplifier_distro.server.apps.slack.config import SlackConfig

        cfg = SlackConfig(bot_token="xoxb-test", signing_secret="secret")
        assert cfg.is_configured
        assert cfg.mode == "events-api"

    def test_from_env(self):
        from amplifier_distro.server.apps.slack.config import SlackConfig

        env = {
            "SLACK_BOT_TOKEN": "xoxb-from-env",
            "SLACK_SIGNING_SECRET": "env-secret",
            "SLACK_HUB_CHANNEL_ID": "C_ENV",
            "SLACK_SIMULATOR_MODE": "true",
        }
        with patch.dict(os.environ, env, clear=False):
            cfg = SlackConfig.from_env()
        assert cfg.bot_token == "xoxb-from-env"
        assert cfg.signing_secret == "env-secret"
        assert cfg.hub_channel_id == "C_ENV"
        assert cfg.simulator_mode is True


# --- Client Tests ---


class TestMemorySlackClient:
    """Test the in-memory Slack client."""

    def test_post_message(self, slack_client):
        ts = asyncio.run(slack_client.post_message("C1", "hello"))
        assert ts  # Non-empty timestamp
        assert len(slack_client.sent_messages) == 1
        assert slack_client.sent_messages[0].text == "hello"
        assert slack_client.sent_messages[0].channel == "C1"

    def test_post_threaded_message(self, slack_client):
        asyncio.run(slack_client.post_message("C1", "reply", thread_ts="parent.ts"))
        assert len(slack_client.sent_messages) == 1
        assert slack_client.sent_messages[0].thread_ts == "parent.ts"

    def test_update_message(self, slack_client):
        asyncio.run(slack_client.update_message("C1", "1.0", "updated"))
        assert len(slack_client.updated_messages) == 1
        assert slack_client.updated_messages[0]["text"] == "updated"

    def test_create_channel(self, slack_client):
        ch = asyncio.run(slack_client.create_channel("test-channel", topic="Test"))
        assert ch.name == "test-channel"
        assert ch.topic == "Test"
        assert ch.id.startswith("C")
        # Should be retrievable
        info = asyncio.run(slack_client.get_channel_info(ch.id))
        assert info is not None
        assert info.name == "test-channel"

    def test_get_nonexistent_channel(self, slack_client):
        info = asyncio.run(slack_client.get_channel_info("C_FAKE"))
        assert info is None

    def test_add_reaction(self, slack_client):
        asyncio.run(slack_client.add_reaction("C1", "1.0", "thumbsup"))
        assert len(slack_client.reactions) == 1
        assert slack_client.reactions[0]["emoji"] == "thumbsup"

    def test_get_bot_user_id(self, slack_client):
        uid = asyncio.run(slack_client.get_bot_user_id())
        assert uid == "U_AMP_BOT"

    def test_seed_channel(self, slack_client):
        from amplifier_distro.server.apps.slack.models import SlackChannel

        ch = SlackChannel(id="C_SEED", name="seeded")
        slack_client.seed_channel(ch)
        info = asyncio.run(slack_client.get_channel_info("C_SEED"))
        assert info is not None
        assert info.name == "seeded"

    def test_on_message_sent_callback(self, slack_client):
        captured = []
        slack_client.on_message_sent = lambda msg: captured.append(msg)
        asyncio.run(slack_client.post_message("C1", "watched"))
        assert len(captured) == 1
        assert captured[0].text == "watched"


# --- Formatter Tests ---


class TestSlackFormatter:
    """Test markdown to Slack mrkdwn conversion and message splitting."""

    def test_bold_conversion(self):
        from amplifier_distro.server.apps.slack.formatter import SlackFormatter

        assert SlackFormatter.markdown_to_slack("**bold**") == "*bold*"

    def test_strikethrough_conversion(self):
        from amplifier_distro.server.apps.slack.formatter import SlackFormatter

        assert SlackFormatter.markdown_to_slack("~~strike~~") == "~strike~"

    def test_link_conversion(self):
        from amplifier_distro.server.apps.slack.formatter import SlackFormatter

        result = SlackFormatter.markdown_to_slack("[click here](https://example.com)")
        assert result == "<https://example.com|click here>"

    def test_header_conversion(self):
        from amplifier_distro.server.apps.slack.formatter import SlackFormatter

        assert SlackFormatter.markdown_to_slack("## My Header") == "*My Header*"

    def test_bullet_conversion(self):
        from amplifier_distro.server.apps.slack.formatter import SlackFormatter

        result = SlackFormatter.markdown_to_slack("- item one\n- item two")
        assert "item one" in result
        assert "item two" in result

    def test_empty_string(self):
        from amplifier_distro.server.apps.slack.formatter import SlackFormatter

        assert SlackFormatter.markdown_to_slack("") == ""

    def test_split_short_message(self):
        from amplifier_distro.server.apps.slack.formatter import SlackFormatter

        result = SlackFormatter.split_message("short", max_length=100)
        assert result == ["short"]

    def test_split_long_message(self):
        from amplifier_distro.server.apps.slack.formatter import SlackFormatter

        text = "paragraph one\n\nparagraph two\n\nparagraph three"
        result = SlackFormatter.split_message(text, max_length=25)
        assert len(result) >= 2
        combined = "\n".join(result)
        assert "paragraph one" in combined
        assert "paragraph three" in combined

    def test_format_session_list_empty(self):
        from amplifier_distro.server.apps.slack.formatter import SlackFormatter

        blocks = SlackFormatter.format_session_list([])
        assert any("No sessions" in str(b) for b in blocks)

    def test_format_session_list_with_data(self):
        from amplifier_distro.server.apps.slack.formatter import SlackFormatter

        sessions = [
            {
                "session_id": "abc12345-full-uuid",
                "project": "test-project",
                "date_str": "02/08 10:00",
                "name": "My Session",
                "description": "test desc",
            },
        ]
        blocks = SlackFormatter.format_session_list(sessions)
        assert len(blocks) >= 2  # Header + at least one section
        assert any("connect_session" in str(b) for b in blocks)

    def test_format_help(self):
        from amplifier_distro.server.apps.slack.formatter import SlackFormatter

        blocks = SlackFormatter.format_help()
        text = str(blocks)
        assert "list" in text
        assert "new" in text
        assert "connect" in text

    def test_format_error(self):
        from amplifier_distro.server.apps.slack.formatter import SlackFormatter

        blocks = SlackFormatter.format_error("something broke")
        assert any("something broke" in str(b) for b in blocks)

    def test_format_status(self):
        from amplifier_distro.server.apps.slack.formatter import SlackFormatter

        blocks = SlackFormatter.format_status("abc123", project="proj", is_active=True)
        text = str(blocks)
        assert "abc123" in text
        assert "Active" in text


# --- Discovery Tests ---


class TestAmplifierDiscovery:
    """Test session and project discovery from the filesystem.

    Uses a temp directory structure mimicking ~/.amplifier/projects/.
    """

    def _create_session(
        self, base: Path, project_path: str, session_id: str, name: str = ""
    ):
        """Helper to create a fake session on disk."""
        encoded = project_path.replace("/", "-")
        sessions_dir = base / "projects" / encoded / "sessions" / session_id
        sessions_dir.mkdir(parents=True, exist_ok=True)

        # Write transcript (required)
        (sessions_dir / "transcript.jsonl").write_text(
            '{"role":"user","content":"test"}\n'
        )

        # Write metadata if name provided
        if name:
            (sessions_dir / "metadata.json").write_text(
                json.dumps({"name": name, "description": f"desc for {name}"})
            )

    def test_list_sessions_empty(self, discovery):
        assert discovery.list_sessions() == []

    def test_list_sessions(self, tmp_path, discovery):
        self._create_session(tmp_path, "/home/sam/project-a", "sess-001", name="First")
        self._create_session(tmp_path, "/home/sam/project-b", "sess-002", name="Second")

        sessions = discovery.list_sessions()
        assert len(sessions) == 2
        names = {s.name for s in sessions}
        assert "First" in names
        assert "Second" in names

    def test_list_sessions_skips_sub_sessions(self, tmp_path, discovery):
        self._create_session(tmp_path, "/home/sam/proj", "main-uuid-1234")
        self._create_session(
            tmp_path, "/home/sam/proj", "main-uuid_sub-agent"
        )  # Sub-session

        sessions = discovery.list_sessions()
        assert len(sessions) == 1
        assert sessions[0].session_id == "main-uuid-1234"

    def test_list_sessions_requires_transcript(self, tmp_path, discovery):
        encoded = "-home-sam-proj"
        d = tmp_path / "projects" / encoded / "sessions" / "no-transcript"
        d.mkdir(parents=True)

        sessions = discovery.list_sessions()
        assert len(sessions) == 0

    def test_list_sessions_project_filter(self, tmp_path, discovery):
        self._create_session(tmp_path, "/home/sam/alpha", "s1")
        self._create_session(tmp_path, "/home/sam/beta", "s2")

        alpha_sessions = discovery.list_sessions(project_filter="alpha")
        assert len(alpha_sessions) == 1
        assert alpha_sessions[0].project == "alpha"

    def test_get_session(self, tmp_path, discovery):
        self._create_session(tmp_path, "/home/sam/proj", "target-uuid", name="Target")

        session = discovery.get_session("target-uuid")
        assert session is not None
        assert session.name == "Target"
        assert session.project == "proj"

    def test_get_session_not_found(self, discovery):
        assert discovery.get_session("nonexistent") is None

    def test_list_projects(self, tmp_path, discovery):
        # NOTE: The encoding replaces ALL hyphens with slashes, so project
        # names must not contain hyphens (they'd decode as path separators).
        self._create_session(tmp_path, "/home/sam/alpha", "s1")
        self._create_session(tmp_path, "/home/sam/alpha", "s2")
        self._create_session(tmp_path, "/home/sam/beta", "s3")

        projects = discovery.list_projects()
        assert len(projects) == 2
        by_name = {p.project_name: p for p in projects}
        assert by_name["alpha"].session_count == 2
        assert by_name["beta"].session_count == 1

    def test_decode_project_path(self):
        from amplifier_distro.server.apps.slack.discovery import AmplifierDiscovery

        assert (
            AmplifierDiscovery._decode_project_path("-home-sam-dev") == "/home/sam/dev"
        )

    def test_extract_project_name(self):
        from amplifier_distro.server.apps.slack.discovery import AmplifierDiscovery

        assert (
            AmplifierDiscovery._extract_project_name("/home/sam/dev/my-project")
            == "my-project"
        )


# --- Backend Tests ---


class TestMockBackend:
    """Test the mock session backend."""

    def test_create_session(self, mock_backend):
        info = asyncio.run(mock_backend.create_session(description="test"))
        assert info.session_id.startswith("mock-session-")
        assert info.is_active

    def test_send_message_echo(self, mock_backend):
        info = asyncio.run(mock_backend.create_session())
        response = asyncio.run(mock_backend.send_message(info.session_id, "hello"))
        assert "hello" in response

    def test_send_message_custom_fn(self, mock_backend):
        mock_backend.set_response_fn(lambda sid, msg: f"Custom: {msg}")
        info = asyncio.run(mock_backend.create_session())
        response = asyncio.run(mock_backend.send_message(info.session_id, "test"))
        assert response == "Custom: test"

    def test_send_message_unknown_session(self, mock_backend):
        with pytest.raises(ValueError, match="Unknown session"):
            asyncio.run(mock_backend.send_message("fake-id", "hello"))

    def test_end_session(self, mock_backend):
        info = asyncio.run(mock_backend.create_session())
        asyncio.run(mock_backend.end_session(info.session_id))
        info2 = asyncio.run(mock_backend.get_session_info(info.session_id))
        assert info2 is not None
        assert not info2.is_active

    def test_list_active_sessions(self, mock_backend):
        asyncio.run(mock_backend.create_session())
        asyncio.run(mock_backend.create_session())
        info3 = asyncio.run(mock_backend.create_session())
        asyncio.run(mock_backend.end_session(info3.session_id))

        active = mock_backend.list_active_sessions()
        assert len(active) == 2

    def test_calls_recorded(self, mock_backend):
        asyncio.run(mock_backend.create_session(description="tracked"))
        assert len(mock_backend.calls) == 1
        assert mock_backend.calls[0]["method"] == "create_session"
        assert mock_backend.calls[0]["description"] == "tracked"


# --- Session Manager Tests ---


class TestSlackSessionManager:
    """Test the Slack-to-Amplifier session routing table."""

    def test_create_session(self, session_manager):
        mapping = asyncio.run(
            session_manager.create_session("C_HUB", "thread.1", "U1", "test session")
        )
        assert mapping.session_id.startswith("mock-session-")
        assert mapping.channel_id == "C_HUB"
        assert mapping.thread_ts == "thread.1"
        assert mapping.description == "test session"

    def test_get_mapping(self, session_manager):
        asyncio.run(session_manager.create_session("C1", "t1", "U1"))

        found = session_manager.get_mapping("C1", "t1")
        assert found is not None

        not_found = session_manager.get_mapping("C1", "t999")
        assert not_found is None

    def test_route_message(self, session_manager):
        from amplifier_distro.server.apps.slack.models import SlackMessage

        asyncio.run(session_manager.create_session("C1", "t1", "U1"))

        msg = SlackMessage(
            channel_id="C1", user_id="U1", text="hello amp", ts="2.0", thread_ts="t1"
        )
        response = asyncio.run(session_manager.route_message(msg))
        assert response is not None
        assert "hello amp" in response  # Mock echoes the message

    def test_route_message_no_mapping(self, session_manager):
        from amplifier_distro.server.apps.slack.models import SlackMessage

        msg = SlackMessage(channel_id="C_UNKNOWN", user_id="U1", text="lost", ts="1.0")
        response = asyncio.run(session_manager.route_message(msg))
        assert response is None

    def test_end_session(self, session_manager):
        from amplifier_distro.server.apps.slack.models import SlackMessage

        asyncio.run(session_manager.create_session("C1", "t1", "U1"))

        ended = asyncio.run(session_manager.end_session("C1", "t1"))
        assert ended is True

        # Routing should now return None (inactive)
        msg = SlackMessage(
            channel_id="C1",
            user_id="U1",
            text="after end",
            ts="3.0",
            thread_ts="t1",
        )
        response = asyncio.run(session_manager.route_message(msg))
        assert response is None

    def test_breakout_to_channel(self, session_manager):
        asyncio.run(
            session_manager.create_session("C_HUB", "t1", "U1", "breakout test")
        )

        new_ch = asyncio.run(session_manager.breakout_to_channel("C_HUB", "t1"))
        assert new_ch is not None
        assert new_ch.name.startswith("amp-")

        # Old mapping should be gone, new one should exist
        old = session_manager.get_mapping("C_HUB", "t1")
        assert old is None

        new = session_manager.get_mapping(new_ch.id)
        assert new is not None

    def test_list_active(self, session_manager):
        asyncio.run(session_manager.create_session("C1", "t1", "U1"))
        asyncio.run(session_manager.create_session("C1", "t2", "U2"))

        active = session_manager.list_active()
        assert len(active) == 2

    def test_list_user_sessions(self, session_manager):
        asyncio.run(session_manager.create_session("C1", "t1", "U1"))
        asyncio.run(session_manager.create_session("C1", "t2", "U1"))
        asyncio.run(session_manager.create_session("C1", "t3", "U2"))

        u1_sessions = session_manager.list_user_sessions("U1")
        assert len(u1_sessions) == 2
        u2_sessions = session_manager.list_user_sessions("U2")
        assert len(u2_sessions) == 1

    def test_rekey_mapping_moves_bare_key_to_thread_key(self, session_manager):
        """rekey_mapping() upgrades bare channel_id key to channel_id:thread_ts."""
        asyncio.run(session_manager.create_session("C_HUB", None, "U1", "rekey test"))

        assert session_manager.get_mapping("C_HUB") is not None
        assert session_manager.get_mapping("C_HUB", "1234567890.000001") is None

        session_manager.rekey_mapping("C_HUB", "1234567890.000001")

        assert session_manager.get_mapping("C_HUB") is None

        mapping = session_manager.get_mapping("C_HUB", "1234567890.000001")
        assert mapping is not None
        assert mapping.thread_ts == "1234567890.000001"
        assert mapping.description == "rekey test"

    def test_rekey_mapping_no_op_when_key_missing(self, session_manager):
        """rekey_mapping() is safe when the bare key doesn't exist — no exception."""
        session_manager.rekey_mapping("C_NONEXISTENT", "ts.0")
        assert session_manager.get_mapping("C_NONEXISTENT") is None

    def test_get_mapping_thread_does_not_fall_back_to_bare_channel(
        self, session_manager
    ):
        """Thread lookup must NOT fall back to a bare-channel key (issue #54 regression guard)."""
        # Session stored under bare key (slash command path before rekey_mapping runs)
        asyncio.run(session_manager.create_session("C_HUB", None, "U1"))
        assert session_manager.get_mapping("C_HUB") is not None  # bare key exists

        # A threaded lookup must NOT match the bare-channel session
        assert session_manager.get_mapping("C_HUB", "some.thread.ts") is None

    def test_create_session_stores_working_dir_on_mapping(self, session_manager):
        """create_session populates working_dir from backend's SessionInfo."""
        mapping = asyncio.run(
            session_manager.create_session("C_HUB", "thread.1", "U1", "wd test")
        )
        # MockBackend.create_session returns info.working_dir = the working_dir
        # it was called with. SlackConfig defaults to "~".
        assert mapping.working_dir != "", "working_dir must be populated"

    def test_connect_session_stores_working_dir_on_mapping(
        self, session_manager, mock_backend
    ):
        """connect_session populates working_dir from backend's SessionInfo."""
        mapping = asyncio.run(
            session_manager.connect_session(
                "C_HUB",
                "thread.2",
                "U1",
                working_dir="~/repo/specific-project",
                description="connect wd test",
            )
        )
        assert mapping.working_dir == "~/repo/specific-project"

    def test_connect_session_with_session_id_calls_resume_not_create(
        self, session_manager, mock_backend
    ):
        """When session_id is provided, backend.resume_session is called instead
        of create_session."""
        mapping = asyncio.run(
            session_manager.connect_session(
                "C_HUB",
                "thread.resume-1",
                "U1",
                working_dir="~/repo/project",
                session_id="known-session-abc123",
            )
        )

        # The mapping carries the exact session_id we passed in
        assert mapping.session_id == "known-session-abc123"
        assert mapping.working_dir == "~/repo/project"

        resume_calls = [
            c for c in mock_backend.calls if c["method"] == "resume_session"
        ]
        create_calls = [
            c for c in mock_backend.calls if c["method"] == "create_session"
        ]
        assert len(resume_calls) == 1, "resume_session must be called exactly once"
        assert resume_calls[0]["session_id"] == "known-session-abc123"
        assert resume_calls[0]["working_dir"] == "~/repo/project"
        assert len(create_calls) == 0, "create_session must NOT be called"

    def test_connect_session_without_session_id_calls_create_as_before(
        self, session_manager, mock_backend
    ):
        """Backward compat: omitting session_id still calls backend.create_session."""
        asyncio.run(
            session_manager.connect_session(
                "C_HUB",
                "thread.resume-2",
                "U1",
                working_dir="~/repo/project",
                # no session_id — must use create_session path
            )
        )

        create_calls = [
            c for c in mock_backend.calls if c["method"] == "create_session"
        ]
        resume_calls = [
            c for c in mock_backend.calls if c["method"] == "resume_session"
        ]
        assert len(create_calls) == 1, "create_session must be called once"
        assert len(resume_calls) == 0, "resume_session must NOT be called"

    def test_create_session_uses_explicit_working_dir(
        self, session_manager, mock_backend
    ):
        """create_session passes explicit working_dir to backend."""
        asyncio.run(
            session_manager.create_session(
                "C1",
                "t1",
                "U1",
                "explicit wd",
                working_dir="~/repo/explicit",
            )
        )
        # Check what working_dir the backend was called with
        create_call = [
            c for c in mock_backend.calls if c["method"] == "create_session"
        ][-1]
        assert create_call["working_dir"] == "~/repo/explicit"

    def test_create_session_falls_back_to_config_default(
        self, session_manager, mock_backend, slack_config
    ):
        """create_session uses config default when no working_dir specified."""
        slack_config.default_working_dir = "~/repo/configured"
        asyncio.run(session_manager.create_session("C1", "t1", "U1", "default wd"))
        create_call = [
            c for c in mock_backend.calls if c["method"] == "create_session"
        ][-1]
        assert create_call["working_dir"] == "~/repo/configured"

    def test_create_session_none_working_dir_uses_default(
        self, session_manager, mock_backend, slack_config
    ):
        """Explicitly passing working_dir=None falls back to config default."""
        slack_config.default_working_dir = "~/repo/fallback"
        asyncio.run(
            session_manager.create_session(
                "C1",
                "t1",
                "U1",
                "none wd",
                working_dir=None,
            )
        )
        create_call = [
            c for c in mock_backend.calls if c["method"] == "create_session"
        ][-1]
        assert create_call["working_dir"] == "~/repo/fallback"


# --- Command Handler Tests ---


class TestCommandHandler:
    """Test command parsing and execution."""

    def test_parse_command_with_mention(self, command_handler):
        cmd, args = command_handler.parse_command("<@U_BOT> list", "U_BOT")
        assert cmd == "list"
        assert args == []

    def test_parse_command_with_args(self, command_handler):
        cmd, args = command_handler.parse_command("<@U_BOT> connect abc123", "U_BOT")
        assert cmd == "connect"
        assert args == ["abc123"]

    def test_parse_command_alias(self, command_handler):
        cmd, _ = command_handler.parse_command("<@U_BOT> ls", "U_BOT")
        assert cmd == "list"

        cmd, _ = command_handler.parse_command("<@U_BOT> start", "U_BOT")
        assert cmd == "new"

        cmd, _ = command_handler.parse_command("<@U_BOT> ?", "U_BOT")
        assert cmd == "help"

    def test_parse_empty_command(self, command_handler):
        cmd, args = command_handler.parse_command("<@U_BOT>", "U_BOT")
        assert cmd == "help"

    def test_cmd_help(self, command_handler):
        from amplifier_distro.server.apps.slack.commands import CommandContext

        ctx = CommandContext(channel_id="C1", user_id="U1")
        result = asyncio.run(command_handler.handle("help", [], ctx))
        assert result.blocks is not None
        assert len(result.blocks) >= 1

    def test_cmd_new(self, command_handler):
        from amplifier_distro.server.apps.slack.commands import CommandContext

        ctx = CommandContext(channel_id="C_HUB", user_id="U1", thread_ts=None)
        result = asyncio.run(command_handler.handle("new", ["my", "session"], ctx))
        assert "Started new session" in result.text

    def test_cmd_new_shows_working_dir_in_response(self, command_handler, slack_config):
        """cmd_new response includes the working directory."""
        from amplifier_distro.server.apps.slack.commands import CommandContext

        slack_config.default_working_dir = "~/repo/my-project"
        ctx = CommandContext(channel_id="C_HUB", user_id="U1", thread_ts=None)
        result = asyncio.run(command_handler.handle("new", ["test"], ctx))
        assert "~/repo/my-project" in result.text

    def test_cmd_new_shows_hint_when_in_home_dir(self, command_handler, slack_config):
        """cmd_new shows a configuration hint when working dir is ~ (unconfigured)."""
        from amplifier_distro.server.apps.slack.commands import CommandContext

        slack_config.default_working_dir = "~"
        ctx = CommandContext(channel_id="C_HUB", user_id="U1", thread_ts=None)
        result = asyncio.run(command_handler.handle("new", ["test"], ctx))
        assert "~" in result.text
        assert "default_working_dir" in result.text

    def test_cmd_new_no_hint_when_working_dir_set(self, command_handler, slack_config):
        """cmd_new does NOT show config hint when a real working dir is set."""
        from amplifier_distro.server.apps.slack.commands import CommandContext

        slack_config.default_working_dir = "~/repo/configured"
        ctx = CommandContext(channel_id="C_HUB", user_id="U1", thread_ts=None)
        result = asyncio.run(command_handler.handle("new", ["test"], ctx))
        assert "default_working_dir" not in result.text

    def test_cmd_new_dir_flag_sets_working_directory(
        self, command_handler, slack_config
    ):
        """--dir flag overrides the config default_working_dir."""
        from amplifier_distro.server.apps.slack.commands import CommandContext

        slack_config.default_working_dir = "~"
        ctx = CommandContext(channel_id="C_HUB", user_id="U1", thread_ts=None)
        result = asyncio.run(
            command_handler.handle(
                "new", ["--dir", "/Users/samule/repo/myproject"], ctx
            )
        )
        assert "/Users/samule/repo/myproject" in result.text
        # Explicit --dir should not trigger the "set default_working_dir" tip
        assert "default_working_dir" not in result.text

    def test_cmd_new_dir_flag_with_description(self, command_handler, slack_config):
        """--dir extracts the path; remaining args become the session description."""
        from amplifier_distro.server.apps.slack.commands import CommandContext

        ctx = CommandContext(channel_id="C_HUB", user_id="U1", thread_ts=None)
        result = asyncio.run(
            command_handler.handle(
                "new", ["--dir", "/some/path", "fix", "the", "auth", "bug"], ctx
            )
        )
        # Path must appear as the working dir ("in `/some/path`"), not as part of description
        assert "in `/some/path`" in result.text
        # Description must be the remaining args only — --dir should not bleed into it
        assert "--dir" not in result.text
        assert "fix the auth bug" in result.text

    def test_cmd_new_dir_flag_missing_value_returns_error(self, command_handler):
        """--dir without a path argument returns a usage error, no session created."""
        from amplifier_distro.server.apps.slack.commands import CommandContext

        ctx = CommandContext(channel_id="C_HUB", user_id="U1", thread_ts=None)
        result = asyncio.run(command_handler.handle("new", ["--dir"], ctx))
        # Should NOT start a session — must return an error
        assert "Started new session" not in result.text
        assert "--dir" in result.text

    def test_cmd_status_no_session(self, command_handler):
        from amplifier_distro.server.apps.slack.commands import CommandContext

        ctx = CommandContext(channel_id="C1", user_id="U1")
        result = asyncio.run(command_handler.handle("status", [], ctx))
        assert "No active sessions" in result.text

    def test_cmd_end_no_session(self, command_handler):
        from amplifier_distro.server.apps.slack.commands import CommandContext

        ctx = CommandContext(channel_id="C1", user_id="U1")
        result = asyncio.run(command_handler.handle("end", [], ctx))
        assert "No active session" in result.text

    def test_cmd_unknown(self, command_handler):
        from amplifier_distro.server.apps.slack.commands import CommandContext

        ctx = CommandContext(channel_id="C1", user_id="U1")
        result = asyncio.run(command_handler.handle("bogus", [], ctx))
        assert "Unknown command" in result.text

    def test_cmd_discover(self, command_handler):
        from amplifier_distro.server.apps.slack.commands import CommandContext

        ctx = CommandContext(channel_id="C1", user_id="U1")
        result = asyncio.run(command_handler.handle("discover", [], ctx))
        assert "No local sessions" in result.text

    def test_cmd_connect_no_args(self, command_handler):
        from amplifier_distro.server.apps.slack.commands import CommandContext

        ctx = CommandContext(channel_id="C1", user_id="U1")
        result = asyncio.run(command_handler.handle("connect", [], ctx))
        assert "Usage" in result.text

    def test_cmd_connect_passes_session_id_to_resume(
        self, command_handler, mock_backend, tmp_path
    ):
        """cmd_connect passes the discovered session_id to
        connect_session (resume path).
        """
        from amplifier_distro.server.apps.slack.commands import CommandContext

        # Create a session directory so AmplifierDiscovery can find it.
        session_id = "abcdef01-0000-0000-0000-000000000001"
        sessions_dir = tmp_path / "projects" / "-tmp-testproj" / "sessions" / session_id
        sessions_dir.mkdir(parents=True)
        (sessions_dir / "transcript.jsonl").write_text(
            '{"role":"user","content":"hello"}\n'
        )

        ctx = CommandContext(channel_id="C1", user_id="U1")
        result = asyncio.run(command_handler.handle("connect", [session_id[:8]], ctx))

        # Should succeed — no error message
        assert "Could not resume" not in result.text

        # resume_session was called with the exact session_id from discovery
        resume_calls = [
            c for c in mock_backend.calls if c["method"] == "resume_session"
        ]
        assert len(resume_calls) == 1, "resume_session must be called exactly once"
        assert resume_calls[0]["session_id"] == session_id

        # create_session was NOT called — no fresh session
        create_calls = [
            c for c in mock_backend.calls if c["method"] == "create_session"
        ]
        assert len(create_calls) == 0, "create_session must NOT be called"

    def test_cmd_connect_returns_error_on_resume_failure(
        self, tmp_path, slack_config, slack_client
    ):
        """If resume_session raises ValueError,
        cmd_connect returns a clear error message.
        """
        from amplifier_distro.server.apps.slack.commands import (
            CommandContext,
            CommandHandler,
        )
        from amplifier_distro.server.apps.slack.discovery import AmplifierDiscovery
        from amplifier_distro.server.apps.slack.sessions import SlackSessionManager
        from amplifier_distro.server.session_backend import MockBackend

        class FailingResumeBackend(MockBackend):
            async def resume_session(self, session_id: str, working_dir: str) -> None:
                raise ValueError("Bridge cannot locate session directory")

        failing_backend = FailingResumeBackend()
        discovery = AmplifierDiscovery(amplifier_home=str(tmp_path))
        session_manager = SlackSessionManager(
            slack_client, failing_backend, slack_config
        )
        handler = CommandHandler(session_manager, discovery, slack_config)

        session_id = "deadbeef-0000-0000-0000-000000000002"
        sessions_dir = tmp_path / "projects" / "-tmp-testproj" / "sessions" / session_id
        sessions_dir.mkdir(parents=True)
        (sessions_dir / "transcript.jsonl").write_text(
            '{"role":"user","content":"hello"}\n'
        )

        ctx = CommandContext(channel_id="C1", user_id="U1")
        result = asyncio.run(handler.handle("connect", [session_id[:8]], ctx))

        # Error message must mention the failure and
        # include the target_id the user typed
        assert "Could not resume" in result.text
        assert session_id[:8] in result.text


# --- Events Handler Tests ---


class TestSlackEventHandler:
    """Test Slack event handling and dispatch."""

    def _make_handler(
        self, slack_client, session_manager, command_handler, slack_config
    ):
        from amplifier_distro.server.apps.slack.events import SlackEventHandler

        return SlackEventHandler(
            slack_client, session_manager, command_handler, slack_config
        )

    def test_url_verification(
        self, slack_client, session_manager, command_handler, slack_config
    ):
        handler = self._make_handler(
            slack_client, session_manager, command_handler, slack_config
        )
        result = asyncio.run(
            handler.handle_event_payload(
                {
                    "type": "url_verification",
                    "challenge": "test_challenge_123",
                }
            )
        )
        assert result["challenge"] == "test_challenge_123"

    def test_message_event_command(
        self, slack_client, session_manager, command_handler, slack_config
    ):
        handler = self._make_handler(
            slack_client, session_manager, command_handler, slack_config
        )

        result = asyncio.run(
            handler.handle_event_payload(
                {
                    "type": "event_callback",
                    "event": {
                        "type": "app_mention",
                        "text": "<@U_AMP_BOT> help",
                        "user": "U1",
                        "channel": "C_HUB",
                        "ts": "1.0",
                    },
                }
            )
        )
        assert result == {"ok": True}
        # Should have sent a response message
        assert len(slack_client.sent_messages) >= 1

    def test_ignores_bot_messages(
        self, slack_client, session_manager, command_handler, slack_config
    ):
        handler = self._make_handler(
            slack_client, session_manager, command_handler, slack_config
        )

        asyncio.run(
            handler.handle_event_payload(
                {
                    "type": "event_callback",
                    "event": {
                        "type": "message",
                        "bot_id": "B123",
                        "text": "bot loop prevention",
                        "channel": "C1",
                        "ts": "1.0",
                    },
                }
            )
        )
        assert len(slack_client.sent_messages) == 0

    def test_signature_verification_simulator_mode(
        self, slack_client, session_manager, command_handler, slack_config
    ):
        handler = self._make_handler(
            slack_client, session_manager, command_handler, slack_config
        )
        # In simulator mode, verification should pass
        assert handler.verify_signature(b"body", "0", "v0=fake") is True

    def test_session_message_routing(
        self, slack_client, session_manager, command_handler, slack_config
    ):
        handler = self._make_handler(
            slack_client, session_manager, command_handler, slack_config
        )

        # Create a session first
        asyncio.run(session_manager.create_session("C1", "t1", "U1"))

        # Send a message in that thread (not mentioning bot)
        asyncio.run(
            handler.handle_event_payload(
                {
                    "type": "event_callback",
                    "event": {
                        "type": "message",
                        "text": "what is the meaning of life",
                        "user": "U1",
                        "channel": "C1",
                        "thread_ts": "t1",
                        "ts": "2.0",
                    },
                }
            )
        )
        # Should have sent a response (mock echo)
        assert len(slack_client.sent_messages) >= 1


# --- HTTP Endpoint Tests ---


class TestSlackBridgeEndpoints:
    """Test the FastAPI HTTP endpoints."""

    def test_bridge_status(self, bridge_client):
        resp = bridge_client.get("/apps/slack/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["mode"] == "simulator"

    def test_list_sessions_empty(self, bridge_client):
        resp = bridge_client.get("/apps/slack/sessions")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_discover_empty(self, bridge_client):
        resp = bridge_client.get("/apps/slack/discover")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_projects_empty(self, bridge_client):
        resp = bridge_client.get("/apps/slack/projects")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_events_url_verification(self, bridge_client):
        resp = bridge_client.post(
            "/apps/slack/events",
            json={"type": "url_verification", "challenge": "abc"},
        )
        assert resp.status_code == 200
        assert resp.json()["challenge"] == "abc"

    def test_events_message(self, bridge_client):
        resp = bridge_client.post(
            "/apps/slack/events",
            json={
                "type": "event_callback",
                "event": {
                    "type": "app_mention",
                    "text": "<@U_AMP_BOT> help",
                    "user": "U1",
                    "channel": "C1",
                    "ts": "1.0",
                },
            },
        )
        assert resp.status_code == 200

    def test_slash_command(self, bridge_client):
        resp = bridge_client.post(
            "/apps/slack/commands/amp",
            data={
                "text": "help",
                "user_id": "U1",
                "user_name": "testuser",
                "channel_id": "C1",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "blocks" in data or "text" in data


# --- Config File Tests ---


class TestSlackConfigFile:
    """Test SlackConfig loading from keys.env + distro settings + env.

    Opinion #11: secrets in keys.env, config in distro settings (settings.yaml).
    """

    def test_from_env_only(self):
        """Config loads from env vars when no config files."""
        from amplifier_distro.server.apps.slack.config import SlackConfig

        with patch.dict(
            os.environ,
            {
                "SLACK_BOT_TOKEN": "xoxb-env",
                "SLACK_APP_TOKEN": "xapp-env",
                "SLACK_SOCKET_MODE": "true",
            },
            clear=False,
        ):
            cfg = SlackConfig.from_env()
            assert cfg.bot_token == "xoxb-env"
            assert cfg.app_token == "xapp-env"
            assert cfg.socket_mode is True

    def test_from_files(self, tmp_path):
        """Config loads from keys.env + distro settings when no env vars."""
        from amplifier_distro import conventions
        from amplifier_distro.server.apps.slack import config as config_mod

        # Write keys.env (secrets)
        keys_file = tmp_path / "keys.env"
        keys_file.write_text(
            'SLACK_BOT_TOKEN="xoxb-file"\nSLACK_APP_TOKEN="xapp-file"\n'
        )

        # Write distro settings.yaml (config in DISTRO_HOME format)
        settings_file = tmp_path / "settings.yaml"
        settings_file.write_text(
            "slack:\n"
            "  hub_channel_id: C_FILE\n"
            "  hub_channel_name: test-channel\n"
            "  socket_mode: true\n"
        )

        # Patch both home paths to use our temp dir
        original = config_mod._amplifier_home
        config_mod._amplifier_home = lambda: tmp_path
        try:
            # Clear env vars that would override
            env = {
                "SLACK_BOT_TOKEN": "",
                "SLACK_APP_TOKEN": "",
                "SLACK_HUB_CHANNEL_ID": "",
                "SLACK_SOCKET_MODE": "",
                "SLACK_SIGNING_SECRET": "",
                "SLACK_SIMULATOR_MODE": "",
                "SLACK_HUB_CHANNEL_NAME": "",
            }
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(conventions, "DISTRO_HOME", str(tmp_path)),
            ):
                cfg = config_mod.SlackConfig.from_env()
                assert cfg.bot_token == "xoxb-file"
                assert cfg.app_token == "xapp-file"
                assert cfg.hub_channel_id == "C_FILE"
                assert cfg.socket_mode is True
        finally:
            config_mod._amplifier_home = original

    def test_env_overrides_file(self, tmp_path):
        """Env vars take priority over keys.env values."""
        from amplifier_distro.server.apps.slack import config as config_mod

        # Write keys.env with a different token
        keys_file = tmp_path / "keys.env"
        keys_file.write_text('SLACK_BOT_TOKEN="xoxb-file"\n')

        original = config_mod._amplifier_home
        config_mod._amplifier_home = lambda: tmp_path
        try:
            with patch.dict(
                os.environ,
                {"SLACK_BOT_TOKEN": "xoxb-env"},
                clear=False,
            ):
                cfg = config_mod.SlackConfig.from_env()
                assert cfg.bot_token == "xoxb-env"
        finally:
            config_mod._amplifier_home = original

    def test_from_env_reads_default_working_dir(self, tmp_path):
        """default_working_dir is read from distro settings slack section."""
        from amplifier_distro import conventions
        from amplifier_distro.server.apps.slack import config as config_mod

        settings_file = tmp_path / "settings.yaml"
        settings_file.write_text("slack:\n  default_working_dir: ~/repo/my-project\n")

        original = config_mod._amplifier_home
        config_mod._amplifier_home = lambda: tmp_path
        try:
            env = {"SLACK_DEFAULT_WORKING_DIR": ""}
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(conventions, "DISTRO_HOME", str(tmp_path)),
            ):
                cfg = config_mod.SlackConfig.from_env()
                assert cfg.default_working_dir == "~/repo/my-project"
        finally:
            config_mod._amplifier_home = original

    def test_from_env_default_working_dir_env_override(self, tmp_path):
        """SLACK_DEFAULT_WORKING_DIR env var overrides distro settings."""
        from amplifier_distro import conventions
        from amplifier_distro.server.apps.slack import config as config_mod

        settings_file = tmp_path / "settings.yaml"
        settings_file.write_text("slack:\n  default_working_dir: ~/repo/from-file\n")

        original = config_mod._amplifier_home
        config_mod._amplifier_home = lambda: tmp_path
        try:
            env = {"SLACK_DEFAULT_WORKING_DIR": "/custom/from-env"}
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(conventions, "DISTRO_HOME", str(tmp_path)),
            ):
                cfg = config_mod.SlackConfig.from_env()
                assert cfg.default_working_dir == "/custom/from-env"
        finally:
            config_mod._amplifier_home = original

    def test_from_env_default_working_dir_defaults_to_tilde(self, tmp_path):
        """default_working_dir falls back to '~' when not configured."""
        from amplifier_distro.server.apps.slack import config as config_mod

        original = config_mod._amplifier_home
        config_mod._amplifier_home = lambda: tmp_path
        try:
            env = {"SLACK_DEFAULT_WORKING_DIR": ""}
            with patch.dict(os.environ, env, clear=False):
                cfg = config_mod.SlackConfig.from_env()
                assert cfg.default_working_dir == "~"
        finally:
            config_mod._amplifier_home = original


# --- Setup Module Tests ---


class TestSlackSetup:
    """Test the Slack bridge setup/install module.

    Opinion #11: secrets in keys.yaml, config in distro.yaml.
    """

    def test_setup_status_unconfigured(self, bridge_client):
        """Setup status shows unconfigured when no tokens."""
        resp = bridge_client.get("/apps/slack/setup/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "steps" in data
        assert "configured" in data
        assert isinstance(data["config_path"], str)
        assert isinstance(data["keys_path"], str)

    def test_setup_manifest(self, bridge_client):
        """Manifest endpoint returns valid app manifest."""
        resp = bridge_client.get("/apps/slack/setup/manifest")
        assert resp.status_code == 200
        data = resp.json()
        assert "manifest" in data
        assert "manifest_yaml" in data
        assert "instructions" in data
        assert "create_url" in data

        # Verify manifest structure
        m = data["manifest"]
        assert m["features"]["bot_user"]["always_online"] is True
        assert "app_mentions:read" in m["oauth_config"]["scopes"]["bot"]
        assert m["settings"]["socket_mode_enabled"] is True

    def test_validate_bad_prefix(self, bridge_client):
        """Validate rejects tokens with wrong prefix."""
        resp = bridge_client.post(
            "/apps/slack/setup/validate",
            json={"bot_token": "not-a-valid-token"},
        )
        assert resp.status_code == 400

    def test_configure_saves_to_keys_and_distro(self, bridge_client, tmp_path):
        """Configure persists secrets to keys.env, config to distro settings."""
        from amplifier_distro import conventions, distro_settings
        from amplifier_distro.server.apps.slack import setup

        # Redirect both home paths to temp dir
        original = setup._amplifier_home
        setup._amplifier_home = lambda: tmp_path
        try:
            with patch.object(conventions, "DISTRO_HOME", str(tmp_path)):
                resp = bridge_client.post(
                    "/apps/slack/setup/configure",
                    json={
                        "bot_token": "xoxb-test-token",
                        "app_token": "xapp-test-token",
                        "hub_channel_id": "C_TEST",
                        "hub_channel_name": "test-channel",
                        "socket_mode": True,
                    },
                )
                assert resp.status_code == 200
                data = resp.json()
                assert data["status"] == "saved"
                assert data["mode"] == "socket"

                # Verify secrets in keys.env (.env format)
                keys = setup.load_keys()
                assert keys["SLACK_BOT_TOKEN"] == "xoxb-test-token"
                assert keys["SLACK_APP_TOKEN"] == "xapp-test-token"

                # Verify config in distro settings
                ds = distro_settings.load().slack
                assert ds.hub_channel_id == "C_TEST"
                assert ds.hub_channel_name == "test-channel"
                assert ds.socket_mode is True
        finally:
            setup._amplifier_home = original

    def test_channels_no_token(self, bridge_client):
        """Channels endpoint requires a bot token."""
        with patch.dict(os.environ, {"SLACK_BOT_TOKEN": ""}, clear=False):
            resp = bridge_client.get("/apps/slack/setup/channels")
            assert resp.status_code == 400

    def test_test_no_token(self, bridge_client):
        """Test endpoint requires a bot token."""
        from amplifier_distro.server.apps.slack import setup

        # Ensure no keys file returns empty
        original = setup._amplifier_home
        setup._amplifier_home = lambda: Path("/tmp/nonexistent-slack-test")
        try:
            with patch.dict(os.environ, {"SLACK_BOT_TOKEN": ""}, clear=False):
                resp = bridge_client.post(
                    "/apps/slack/setup/test",
                    json={},
                )
                assert resp.status_code == 400
        finally:
            setup._amplifier_home = original


# --- Setup Config Persistence Tests ---


class TestSlackSetupHelpers:
    """Test setup module helper functions (keys.env + distro settings)."""

    def test_save_and_load_keys(self, tmp_path):
        """Round-trip: save keys then load them back."""
        from amplifier_distro.server.apps.slack import setup

        original = setup._amplifier_home
        setup._amplifier_home = lambda: tmp_path
        try:
            setup._save_keys(
                {
                    "SLACK_BOT_TOKEN": "xoxb-round-trip",
                    "SLACK_APP_TOKEN": "xapp-round-trip",
                }
            )
            loaded = setup.load_keys()
            assert loaded["SLACK_BOT_TOKEN"] == "xoxb-round-trip"
            assert loaded["SLACK_APP_TOKEN"] == "xapp-round-trip"
        finally:
            setup._amplifier_home = original

    def test_save_and_load_distro_slack(self, tmp_path):
        """Round-trip: save slack config to distro settings then load it back."""
        from amplifier_distro import conventions, distro_settings
        from amplifier_distro.server.apps.slack import setup

        original = setup._amplifier_home
        setup._amplifier_home = lambda: tmp_path
        try:
            with patch.object(conventions, "DISTRO_HOME", str(tmp_path)):
                setup._save_distro_slack(
                    hub_channel_id="C_RT",
                    hub_channel_name="test",
                    socket_mode=True,
                )
                slack = distro_settings.load().slack
                assert slack.hub_channel_id == "C_RT"
                assert slack.hub_channel_name == "test"
                assert slack.socket_mode is True
        finally:
            setup._amplifier_home = original

    def test_distro_slack_preserves_other_sections(self, tmp_path):
        """Writing slack section preserves other distro settings."""
        from amplifier_distro import conventions, distro_settings
        from amplifier_distro.server.apps.slack import setup

        original = setup._amplifier_home
        setup._amplifier_home = lambda: tmp_path

        try:
            with patch.object(conventions, "DISTRO_HOME", str(tmp_path)):
                # Pre-populate settings with existing workspace_root + identity
                initial = distro_settings.DistroSettings(workspace_root="~/dev")
                initial.identity.github_handle = "test"
                distro_settings.save(initial)

                # Save only the slack section
                setup._save_distro_slack(hub_channel_id="C_NEW")

                # Verify other sections are preserved
                loaded = distro_settings.load()
                assert loaded.workspace_root == "~/dev"
                assert loaded.identity.github_handle == "test"
                # New slack section added
                assert loaded.slack.hub_channel_id == "C_NEW"
        finally:
            setup._amplifier_home = original

    def test_load_missing_config(self, tmp_path):
        """Loading from non-existent paths returns empty dict / defaults."""
        from amplifier_distro import conventions, distro_settings
        from amplifier_distro.server.apps.slack import setup

        nonexistent = tmp_path / "nonexistent"
        original = setup._amplifier_home
        setup._amplifier_home = lambda: nonexistent
        try:
            with patch.object(conventions, "DISTRO_HOME", str(nonexistent)):
                assert setup.load_keys() == {}
                # distro_settings returns defaults when file doesn't exist
                slack = distro_settings.load().slack
                assert slack.hub_channel_id == ""
        finally:
            setup._amplifier_home = original

    def test_keys_file_permissions(self, tmp_path):
        """keys.env is written with chmod 600 (owner-only)."""
        from amplifier_distro.server.apps.slack import setup

        original = setup._amplifier_home
        setup._amplifier_home = lambda: tmp_path
        try:
            setup._save_keys({"SLACK_BOT_TOKEN": "xoxb-perms"})
            path = tmp_path / "keys.env"
            assert path.exists()
            mode = oct(path.stat().st_mode & 0o777)
            assert mode == "0o600"
        finally:
            setup._amplifier_home = original


# --- Event Deduplication Tests ---


class TestSocketModeDedup:
    """Test event deduplication in SocketModeAdapter."""

    def test_dedup_prevents_double_processing(self):
        """Same channel:ts should be deduplicated."""
        from amplifier_distro.server.apps.slack.socket_mode import (
            SocketModeAdapter,
        )

        adapter = SocketModeAdapter.__new__(SocketModeAdapter)
        adapter._seen_events = {}

        assert adapter._is_duplicate("C1:1.0") is False
        assert adapter._is_duplicate("C1:1.0") is True

    def test_dedup_different_messages_pass(self):
        """Different channel:ts pairs are not duplicates."""
        from amplifier_distro.server.apps.slack.socket_mode import (
            SocketModeAdapter,
        )

        adapter = SocketModeAdapter.__new__(SocketModeAdapter)
        adapter._seen_events = {}

        assert adapter._is_duplicate("C1:1.0") is False
        assert adapter._is_duplicate("C1:2.0") is False
        assert adapter._is_duplicate("C2:1.0") is False


# --- Command Routing Fix Tests ---


class TestCommandRoutingFix:
    """Test that command routing works for all mention formats.

    The bug: Slack sends mentions as <@U123> or <@U123|displayname>.
    The old regex only matched <@U123>, so <@U123|name> commands
    were not stripped, causing all commands to appear "unknown".
    """

    def test_parse_command_with_display_name_mention(self, command_handler):
        """<@U_BOT|amp> list should parse correctly."""
        cmd, args = command_handler.parse_command("<@U_BOT|amp> list", "U_BOT")
        assert cmd == "list"
        assert args == []

    def test_parse_command_with_display_name_and_args(self, command_handler):
        """<@U_BOT|SlackBridge> connect abc123 should parse correctly."""
        cmd, args = command_handler.parse_command(
            "<@U_BOT|SlackBridge> connect abc123", "U_BOT"
        )
        assert cmd == "connect"
        assert args == ["abc123"]

    def test_parse_command_display_name_only(self, command_handler):
        """Just mentioning <@U_BOT|amp> with no command should default to help."""
        cmd, args = command_handler.parse_command("<@U_BOT|amp>", "U_BOT")
        assert cmd == "help"
        assert args == []

    def test_parse_command_standard_mention(self, command_handler):
        """Standard <@U_BOT> still works (regression guard)."""
        cmd, args = command_handler.parse_command("<@U_BOT> status", "U_BOT")
        assert cmd == "status"
        assert args == []

    def test_disconnect_alias(self, command_handler):
        """disconnect should alias to end."""
        cmd, _ = command_handler.parse_command("<@U_BOT> disconnect", "U_BOT")
        assert cmd == "end"


# --- Integration Tests: Full Event Pipeline ---


class TestEventPipelineIntegration:
    """Integration tests routing each command through the full event pipeline.

    Each test sends a Slack event payload through handle_event_payload()
    and verifies the bridge responds correctly.
    """

    def _make_handler(
        self, slack_client, session_manager, command_handler, slack_config
    ):
        from amplifier_distro.server.apps.slack.events import SlackEventHandler

        return SlackEventHandler(
            slack_client, session_manager, command_handler, slack_config
        )

    def _app_mention_payload(self, text, channel="C_HUB", user="U1", ts="1.0"):
        """Build an app_mention event payload."""
        return {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "text": text,
                "user": user,
                "channel": channel,
                "ts": ts,
            },
        }

    def test_help_via_event(
        self, slack_client, session_manager, command_handler, slack_config
    ):
        handler = self._make_handler(
            slack_client, session_manager, command_handler, slack_config
        )
        asyncio.run(
            handler.handle_event_payload(self._app_mention_payload("<@U_AMP_BOT> help"))
        )
        assert len(slack_client.sent_messages) >= 1
        # Help response uses blocks
        sent = slack_client.sent_messages[0]
        assert sent.channel == "C_HUB"

    def test_list_via_event(
        self, slack_client, session_manager, command_handler, slack_config
    ):
        handler = self._make_handler(
            slack_client, session_manager, command_handler, slack_config
        )
        asyncio.run(
            handler.handle_event_payload(self._app_mention_payload("<@U_AMP_BOT> list"))
        )
        assert len(slack_client.sent_messages) >= 1

    def test_new_via_event(
        self, slack_client, session_manager, command_handler, slack_config
    ):
        handler = self._make_handler(
            slack_client, session_manager, command_handler, slack_config
        )
        asyncio.run(
            handler.handle_event_payload(
                self._app_mention_payload("<@U_AMP_BOT> new my test session")
            )
        )
        assert len(slack_client.sent_messages) >= 1
        text = slack_client.sent_messages[0].text
        assert "Started" in text or "session" in text.lower()

    def test_status_via_event(
        self, slack_client, session_manager, command_handler, slack_config
    ):
        handler = self._make_handler(
            slack_client, session_manager, command_handler, slack_config
        )
        asyncio.run(
            handler.handle_event_payload(
                self._app_mention_payload("<@U_AMP_BOT> status")
            )
        )
        assert len(slack_client.sent_messages) >= 1

    def test_discover_via_event(
        self, slack_client, session_manager, command_handler, slack_config
    ):
        handler = self._make_handler(
            slack_client, session_manager, command_handler, slack_config
        )
        asyncio.run(
            handler.handle_event_payload(
                self._app_mention_payload("<@U_AMP_BOT> discover")
            )
        )
        assert len(slack_client.sent_messages) >= 1

    def test_sessions_via_event(
        self, slack_client, session_manager, command_handler, slack_config
    ):
        handler = self._make_handler(
            slack_client, session_manager, command_handler, slack_config
        )
        asyncio.run(
            handler.handle_event_payload(
                self._app_mention_payload("<@U_AMP_BOT> sessions")
            )
        )
        assert len(slack_client.sent_messages) >= 1

    def test_config_via_event(
        self, slack_client, session_manager, command_handler, slack_config
    ):
        handler = self._make_handler(
            slack_client, session_manager, command_handler, slack_config
        )
        asyncio.run(
            handler.handle_event_payload(
                self._app_mention_payload("<@U_AMP_BOT> config")
            )
        )
        assert len(slack_client.sent_messages) >= 1
        text = slack_client.sent_messages[0].text
        assert "Configuration" in text

    def test_connect_via_event(
        self, slack_client, session_manager, command_handler, slack_config
    ):
        handler = self._make_handler(
            slack_client, session_manager, command_handler, slack_config
        )
        asyncio.run(
            handler.handle_event_payload(
                self._app_mention_payload("<@U_AMP_BOT> connect abc123")
            )
        )
        assert len(slack_client.sent_messages) >= 1

    def test_disconnect_via_event(
        self, slack_client, session_manager, command_handler, slack_config
    ):
        handler = self._make_handler(
            slack_client, session_manager, command_handler, slack_config
        )
        asyncio.run(
            handler.handle_event_payload(
                self._app_mention_payload("<@U_AMP_BOT> disconnect")
            )
        )
        assert len(slack_client.sent_messages) >= 1
        text = slack_client.sent_messages[0].text
        assert "No active session" in text

    def test_display_name_mention_routes_correctly(
        self, slack_client, session_manager, command_handler, slack_config
    ):
        """Slack mentions with |displayname should route to the right command."""
        handler = self._make_handler(
            slack_client, session_manager, command_handler, slack_config
        )
        asyncio.run(
            handler.handle_event_payload(
                self._app_mention_payload("<@U_AMP_BOT|amp> list")
            )
        )
        assert len(slack_client.sent_messages) >= 1


# --- Edge Case Tests ---


class TestCommandEdgeCases:
    """Test edge cases in command parsing and handling."""

    def test_empty_message_text(self, command_handler):
        """Empty text should parse to help."""
        cmd, args = command_handler.parse_command("", "U_BOT")
        assert cmd == "help"
        assert args == []

    def test_whitespace_only_message(self, command_handler):
        """Whitespace-only text should parse to help."""
        cmd, args = command_handler.parse_command("   ", "U_BOT")
        assert cmd == "help"
        assert args == []

    def test_unknown_command_response(self, command_handler):
        from amplifier_distro.server.apps.slack.commands import CommandContext

        ctx = CommandContext(channel_id="C1", user_id="U1")
        result = asyncio.run(command_handler.handle("xyzzy", [], ctx))
        assert "Unknown command" in result.text
        assert "help" in result.text.lower()

    def test_malformed_mention_treated_as_text(self, command_handler):
        """Partial mentions like <@U_BOT should not crash."""
        cmd, args = command_handler.parse_command("<@U_BOT list", "U_BOT")
        # The regex won't match a malformed mention, so it becomes the first word
        assert cmd is not None  # Should not crash

    def test_mention_with_extra_spaces(self, command_handler):
        """Extra spaces between mention and command should work."""
        cmd, args = command_handler.parse_command("<@U_BOT>   list  ", "U_BOT")
        assert cmd == "list"
        assert args == []

    def test_cmd_sessions_empty(self, command_handler):
        """Sessions command with no active sessions."""
        from amplifier_distro.server.apps.slack.commands import CommandContext

        ctx = CommandContext(channel_id="C1", user_id="U1")
        result = asyncio.run(command_handler.handle("sessions", [], ctx))
        assert "No active" in result.text

    def test_cmd_config_shows_bot_name(self, command_handler):
        """Config command includes bot name."""
        from amplifier_distro.server.apps.slack.commands import CommandContext

        ctx = CommandContext(channel_id="C1", user_id="U1")
        result = asyncio.run(command_handler.handle("config", [], ctx))
        assert "amp" in result.text  # bot_name from slack_config fixture


# --- Session Persistence Tests ---


class TestSessionPersistence:
    """Test session persistence to JSON file."""

    def test_save_and_load_round_trip(
        self, slack_client, mock_backend, slack_config, tmp_path
    ):
        """Sessions saved to disk are loaded back on new manager creation."""
        from amplifier_distro.server.apps.slack.sessions import SlackSessionManager

        persist_path = tmp_path / "slack-sessions.json"

        # Create manager with persistence, add a session
        mgr1 = SlackSessionManager(
            slack_client, mock_backend, slack_config, persistence_path=persist_path
        )
        asyncio.run(mgr1.create_session("C1", "t1", "U1", "persisted session"))

        # Verify file was written
        assert persist_path.exists()
        data = json.loads(persist_path.read_text())
        assert len(data) == 1
        assert data[0]["channel_id"] == "C1"
        assert data[0]["thread_ts"] == "t1"
        assert data[0]["description"] == "persisted session"

        # Create a NEW manager pointing at same file - should load the session
        mgr2 = SlackSessionManager(
            slack_client, mock_backend, slack_config, persistence_path=persist_path
        )
        loaded = mgr2.get_mapping("C1", "t1")
        assert loaded is not None
        assert loaded.description == "persisted session"
        assert loaded.channel_id == "C1"
        assert loaded.is_active is True

    def test_persistence_survives_end_session(
        self, slack_client, mock_backend, slack_config, tmp_path
    ):
        """Ending a session is persisted (is_active=False)."""
        from amplifier_distro.server.apps.slack.sessions import SlackSessionManager

        persist_path = tmp_path / "slack-sessions.json"
        mgr = SlackSessionManager(
            slack_client, mock_backend, slack_config, persistence_path=persist_path
        )
        asyncio.run(mgr.create_session("C1", "t1", "U1"))
        asyncio.run(mgr.end_session("C1", "t1"))

        # Reload and verify
        mgr2 = SlackSessionManager(
            slack_client, mock_backend, slack_config, persistence_path=persist_path
        )
        loaded = mgr2.get_mapping("C1", "t1")
        assert loaded is not None
        assert loaded.is_active is False

    def test_persistence_no_file_on_startup(
        self, slack_client, mock_backend, slack_config, tmp_path
    ):
        """Manager starts cleanly when no persistence file exists."""
        from amplifier_distro.server.apps.slack.sessions import SlackSessionManager

        persist_path = tmp_path / "nonexistent" / "slack-sessions.json"
        mgr = SlackSessionManager(
            slack_client, mock_backend, slack_config, persistence_path=persist_path
        )
        assert mgr.list_active() == []

    def test_persistence_disabled_when_none(
        self, slack_client, mock_backend, slack_config, tmp_path
    ):
        """When persistence_path is None, no file operations occur."""
        from amplifier_distro.server.apps.slack.sessions import SlackSessionManager

        mgr = SlackSessionManager(
            slack_client, mock_backend, slack_config, persistence_path=None
        )
        asyncio.run(mgr.create_session("C1", "t1", "U1"))
        # No file should be created anywhere in tmp_path
        assert list(tmp_path.iterdir()) == []

    def test_persistence_includes_all_fields(
        self, slack_client, mock_backend, slack_config, tmp_path
    ):
        """Persisted JSON includes all required session fields."""
        from amplifier_distro.server.apps.slack.sessions import SlackSessionManager

        persist_path = tmp_path / "slack-sessions.json"
        mgr = SlackSessionManager(
            slack_client, mock_backend, slack_config, persistence_path=persist_path
        )
        asyncio.run(mgr.create_session("C1", "t1", "U1", "full fields test"))

        data = json.loads(persist_path.read_text())
        record = data[0]
        required_fields = {
            "session_id",
            "channel_id",
            "thread_ts",
            "created_at",
            "last_active",
        }
        for field in required_fields:
            assert field in record, f"Missing field: {field}"

    def test_persistence_handles_corrupt_file(
        self, slack_client, mock_backend, slack_config, tmp_path
    ):
        """Manager handles corrupt persistence file gracefully."""
        from amplifier_distro.server.apps.slack.sessions import SlackSessionManager

        persist_path = tmp_path / "slack-sessions.json"
        persist_path.write_text("NOT VALID JSON {{{{")

        # Should not raise, just log a warning and start empty
        mgr = SlackSessionManager(
            slack_client, mock_backend, slack_config, persistence_path=persist_path
        )
        assert mgr.list_active() == []

    def test_save_load_round_trips_working_dir(
        self, slack_client, mock_backend, slack_config, tmp_path
    ):
        """working_dir survives save/load round trip."""
        from amplifier_distro.server.apps.slack.sessions import SlackSessionManager

        persist_path = tmp_path / "slack-sessions.json"
        mgr1 = SlackSessionManager(
            slack_client, mock_backend, slack_config, persistence_path=persist_path
        )
        asyncio.run(mgr1.create_session("C1", "t1", "U1", "wd test"))

        # Verify the JSON file contains working_dir
        data = json.loads(persist_path.read_text())
        assert "working_dir" in data[0], "working_dir must be in persisted JSON"

        # Load into a new manager and verify
        mgr2 = SlackSessionManager(
            slack_client, mock_backend, slack_config, persistence_path=persist_path
        )
        loaded = mgr2.get_mapping("C1", "t1")
        assert loaded is not None
        assert loaded.working_dir != "", "working_dir must survive round trip"

    def test_load_sessions_backward_compat_no_working_dir(
        self, slack_client, mock_backend, slack_config, tmp_path
    ):
        """Old JSON files without working_dir load without error."""
        from amplifier_distro.server.apps.slack.sessions import SlackSessionManager

        persist_path = tmp_path / "slack-sessions.json"
        # Write an old-format JSON without working_dir
        old_data = [
            {
                "session_id": "old-session-001",
                "channel_id": "C1",
                "thread_ts": "t1",
                "project_id": "proj",
                "description": "old session",
                "created_by": "U1",
                "created_at": "2026-01-01T00:00:00",
                "last_active": "2026-01-01T00:00:00",
                "is_active": True,
            }
        ]
        persist_path.write_text(json.dumps(old_data))

        # Must load without error
        mgr = SlackSessionManager(
            slack_client, mock_backend, slack_config, persistence_path=persist_path
        )
        loaded = mgr.get_mapping("C1", "t1")
        assert loaded is not None
        assert loaded.working_dir == ""  # Default for missing field

    def test_save_sessions_includes_all_dataclass_fields(
        self, slack_client, mock_backend, slack_config, tmp_path
    ):
        """_save_sessions output includes every SessionMapping dataclass field.

        This prevents future fields from being silently dropped by the manual
        serialization in _save_sessions().
        """
        from dataclasses import fields

        from amplifier_distro.server.apps.slack.models import SessionMapping
        from amplifier_distro.server.apps.slack.sessions import SlackSessionManager

        persist_path = tmp_path / "slack-sessions.json"
        mgr = SlackSessionManager(
            slack_client, mock_backend, slack_config, persistence_path=persist_path
        )
        asyncio.run(mgr.create_session("C1", "t1", "U1", "field check"))

        data = json.loads(persist_path.read_text())
        record = data[0]

        # Every dataclass field (except computed properties) must be in JSON
        dataclass_field_names = {f.name for f in fields(SessionMapping)}
        json_keys = set(record.keys())
        missing = dataclass_field_names - json_keys
        assert not missing, (
            f"_save_sessions() is missing fields: {missing}. "
            "Add them to the dict literal in _save_sessions()."
        )

    def test_default_persistence_path_uses_conventions(self):
        """The default persistence path is built from conventions constants."""
        from amplifier_distro.conventions import (
            AMPLIFIER_HOME,
            SERVER_DIR,
            SLACK_SESSIONS_FILENAME,
        )
        from amplifier_distro.server.apps.slack.sessions import (
            _default_persistence_path,
        )

        path = _default_persistence_path()
        assert (
            path
            == Path(AMPLIFIER_HOME).expanduser() / SERVER_DIR / SLACK_SESSIONS_FILENAME
        )


# --- Thread Routing Fix Tests (Issue #54) ---


class TestThreadRoutingFix:
    """Regression tests for issue #54: thread routing cross-contamination.

    Bug: Two @amp new commands in the same channel both map to the bare
    channel_id key. The second create_session() overwrites the first's entry,
    so messages in thread A get routed to session B.

    These tests drive through the full SlackEventHandler pipeline (not just
    the session manager) to confirm the wiring between _handle_command_message()
    and rekey_mapping() is correct end-to-end.
    """

    def _make_handler(
        self, slack_client, session_manager, command_handler, slack_config
    ):
        from amplifier_distro.server.apps.slack.events import SlackEventHandler

        return SlackEventHandler(
            slack_client, session_manager, command_handler, slack_config
        )

    def _app_mention_payload(self, text, channel="C_HUB", user="U1", ts="1.0"):
        """Build an app_mention event payload (no thread_ts — top-level command)."""
        return {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "text": text,
                "user": user,
                "channel": channel,
                "ts": ts,
            },
        }

    def test_two_new_commands_in_same_channel_dont_overwrite(
        self, slack_client, session_manager, command_handler, slack_config
    ):
        """Two @amp new commands in the same channel get independent routing keys.

        Before the fix: the second create_session() writes under the same bare
        channel_id key as the first, destroying the first's routing entry. All
        subsequent messages in thread A would silently land in session B.

        After the fix: each session is re-keyed to its own thread_ts immediately
        after the bot posts its reply, so get_mapping(channel, thread_ts_A) and
        get_mapping(channel, thread_ts_B) return two distinct session mappings.
        """
        handler = self._make_handler(
            slack_client, session_manager, command_handler, slack_config
        )

        # --- First @amp new ---
        asyncio.run(
            handler.handle_event_payload(
                self._app_mention_payload(
                    "<@U_AMP_BOT> new first session", ts="100.000001"
                )
            )
        )
        assert len(slack_client.sent_messages) >= 1, (
            "Bot must post at least one message in response to @amp new"
        )
        first_reply = slack_client.sent_messages[0]
        assert first_reply.thread_ts is None, (
            "First @amp new reply must start a brand-new thread (thread_ts=None)."
        )
        thread_ts_A = first_reply.ts

        # --- Second @amp new ---
        asyncio.run(
            handler.handle_event_payload(
                self._app_mention_payload(
                    "<@U_AMP_BOT> new second session", ts="200.000001"
                )
            )
        )
        assert len(slack_client.sent_messages) >= 2, (
            "Bot must post a reply for the second @amp new as well"
        )
        second_reply = slack_client.sent_messages[1]
        assert second_reply.thread_ts is None, (
            "Second @amp new reply must also start a brand-new thread"
        )
        thread_ts_B = second_reply.ts

        # Sanity: the two threads are distinct
        assert thread_ts_A != thread_ts_B

        # --- Verify the routing table ---

        # Bare "C_HUB" entry must be gone after both re-keys
        assert session_manager.get_mapping("C_HUB") is None, (
            "Bare 'C_HUB' routing key must be gone after re-keying. "
            "Its presence means the second @amp new overwrote session A's entry."
        )

        # Each thread ts must resolve to its own independent session
        mapping_A = session_manager.get_mapping("C_HUB", thread_ts_A)
        mapping_B = session_manager.get_mapping("C_HUB", thread_ts_B)

        assert mapping_A is not None, (
            f"No routing entry found for thread_ts_A={thread_ts_A!r}. "
            "Session A was lost — this is the cross-contamination bug."
        )
        assert mapping_B is not None, (
            f"No routing entry found for thread_ts_B={thread_ts_B!r}. "
            "Session B was not registered correctly."
        )
        assert mapping_A.session_id != mapping_B.session_id, (
            "Session A and session B must be different objects. "
            "If they match, session A's routing entry was overwritten by session B."
        )


# --- Zombie Session Bug Fix Tests ---


class TestZombieSessionFix:
    """Test that dead sessions are deactivated, not left as zombies.

    Bug: route_message() catches ALL exceptions from backend.send_message()
    with a bare 'except Exception' and returns a generic error string, but
    never deactivates the mapping. A session whose backend handle is lost
    (FoundationBackend raises ValueError) persists as is_active=True forever.

    Fix: catch ValueError specifically (= session permanently dead),
    deactivate the mapping, and save. Keep the broad except Exception
    for transient errors (network, timeout) where retry may succeed.
    """

    def test_route_message_valueerror_deactivates_mapping(
        self, session_manager, mock_backend
    ):
        """ValueError from backend.send_message deactivates the mapping."""
        from amplifier_distro.server.apps.slack.models import SlackMessage

        # Create a session
        mapping = asyncio.run(
            session_manager.create_session("C1", "t1", "U1", "zombie test")
        )
        assert mapping.is_active is True

        # End the session on the backend (simulates lost handle).
        # After Task 1's fix, MockBackend.send_message raises ValueError
        # for ended sessions — matching FoundationBackend production behavior.
        asyncio.run(mock_backend.end_session(mapping.session_id))

        msg = SlackMessage(
            channel_id="C1", user_id="U1", text="hello", ts="2.0", thread_ts="t1"
        )
        response = asyncio.run(session_manager.route_message(msg))

        # Mapping must be deactivated
        updated = session_manager.get_mapping("C1", "t1")
        assert updated is not None
        assert updated.is_active is False, (
            "Mapping should be deactivated after ValueError from backend"
        )

        # Response should tell the user the session is dead
        assert response is not None
        assert "session has ended" in response.lower()

    def test_route_message_transient_error_keeps_mapping_active(
        self, session_manager, mock_backend
    ):
        """RuntimeError (transient) must NOT deactivate the mapping."""
        from amplifier_distro.server.apps.slack.models import SlackMessage

        asyncio.run(session_manager.create_session("C1", "t1", "U1"))

        # Make the backend raise RuntimeError (= transient failure).
        # Use set_response_fn because we need a non-ValueError exception
        # while the session is still active on the backend.
        def transient_failure(sid, msg):
            raise RuntimeError("network timeout")

        mock_backend.set_response_fn(transient_failure)

        msg = SlackMessage(
            channel_id="C1", user_id="U1", text="hello", ts="2.0", thread_ts="t1"
        )
        response = asyncio.run(session_manager.route_message(msg))

        # Mapping must stay active (transient error, may recover)
        mapping = session_manager.get_mapping("C1", "t1")
        assert mapping is not None
        assert mapping.is_active is True, (
            "Transient errors must not deactivate the mapping"
        )

        # Response should be the generic error
        assert response is not None
        assert "Error" in response
