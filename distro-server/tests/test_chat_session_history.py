"""Tests for scan_sessions() and GET /api/sessions/history."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from amplifier_distro.server.app import DistroServer
from amplifier_distro.server.services import init_services, reset_services

# —— Fixtures ——————————————————————————————————————————————————————————————


@pytest.fixture(autouse=True)
def _clean():
    reset_services()
    yield
    reset_services()


@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    """Override AMPLIFIER_HOME to a fresh tmp_path for scanner tests."""
    import amplifier_distro.server.apps.chat.session_history as sh_mod

    monkeypatch.setattr(sh_mod, "_AMPLIFIER_HOME_OVERRIDE", str(tmp_path))
    return tmp_path


@pytest.fixture
def chat_client(tmp_path) -> TestClient:
    """TestClient with _AMPLIFIER_HOME_OVERRIDE pointed at tmp_path."""
    import amplifier_distro.server.apps.chat.session_history as sh_mod

    sh_mod._AMPLIFIER_HOME_OVERRIDE = str(tmp_path)
    try:
        init_services(dev_mode=True)
        from amplifier_distro.server.apps.chat import manifest

        server = DistroServer()
        server.register_app(manifest)
        yield TestClient(server.app)
    finally:
        sh_mod._AMPLIFIER_HOME_OVERRIDE = None  # always reset


def _make_session(
    tmp_home: Path,
    project_dir_name: str,
    session_id: str,
    lines: list[dict] | None = None,
) -> Path:
    """Helper: create a fake session directory with optional transcript.jsonl."""
    session_dir = tmp_home / "projects" / project_dir_name / "sessions" / session_id
    session_dir.mkdir(parents=True)
    if lines is not None:
        transcript = session_dir / "transcript.jsonl"
        transcript.write_text(
            "\n".join(json.dumps(line) for line in lines),
            encoding="utf-8",
        )
    return session_dir


# —— TestScanSessions ————————————————————————————————————————————————————


class TestScanSessions:
    def test_returns_empty_list_when_no_projects_dir(self, tmp_home):
        from amplifier_distro.server.apps.chat.session_history import scan_sessions

        result = scan_sessions()
        assert result == []

    def test_returns_session_with_transcript(self, tmp_home):
        from amplifier_distro.server.apps.chat.session_history import scan_sessions

        _make_session(
            tmp_home,
            "-Users-test",
            "abc-123",
            lines=[
                {"role": "user", "content": "hello there"},
                {"role": "assistant", "content": "Hi! How can I help?"},
            ],
        )

        result = scan_sessions()

        assert len(result) == 1
        s = result[0]
        assert s["session_id"] == "abc-123"
        assert s["cwd"] == "/Users/test"
        assert s["message_count"] == 2
        assert s["last_user_message"] == "hello there"
        assert s["last_updated"] is not None
        from datetime import datetime

        datetime.fromisoformat(s["last_updated"])  # must be valid ISO 8601 string

    def test_returns_last_user_message_when_multiple(self, tmp_home):
        """last_user_message is the final user turn, not the first."""
        from amplifier_distro.server.apps.chat.session_history import scan_sessions

        _make_session(
            tmp_home,
            "-Users-test",
            "multi-turn",
            lines=[
                {"role": "user", "content": "first message"},
                {"role": "assistant", "content": "reply"},
                {"role": "user", "content": "follow-up question"},
                {"role": "assistant", "content": "final reply"},
            ],
        )

        result = scan_sessions()

        assert result[0]["last_user_message"] == "follow-up question"

    def test_decodes_cwd_from_project_dir(self, tmp_home):
        from amplifier_distro.server.apps.chat.session_history import scan_sessions

        session_dir = _make_session(
            tmp_home,
            "-Users-alice-repo-amplifier-distro",
            "sess-1",
            lines=[{"role": "user", "content": "test"}],
        )
        # Write session-info.json with verbatim CWD (as Amplifier framework would)
        (session_dir / "session-info.json").write_text(
            json.dumps({"working_dir": "/Users/alice/repo/amplifier-distro"}),
            encoding="utf-8",
        )

        result = scan_sessions()

        assert result[0]["cwd"] == "/Users/alice/repo/amplifier-distro"

    def test_handles_missing_transcript_gracefully(self, tmp_home):
        """Session directory with no transcript.jsonl returns session with 0 count."""
        from amplifier_distro.server.apps.chat.session_history import scan_sessions

        _make_session(tmp_home, "-Users-test", "no-transcript", lines=None)

        result = scan_sessions()

        assert len(result) == 1
        s = result[0]
        assert s["session_id"] == "no-transcript"
        assert s["message_count"] == 0
        assert s["last_user_message"] is None

    def test_handles_malformed_transcript_gracefully(self, tmp_home):
        """Malformed JSON lines are skipped; session still appears."""
        from amplifier_distro.server.apps.chat.session_history import scan_sessions

        session_dir = _make_session(tmp_home, "-Users-test", "bad-json", lines=None)
        (session_dir / "transcript.jsonl").write_text(
            'not valid json\n{"role": "user", "content": "valid line"}\n{broken',
            encoding="utf-8",
        )

        result = scan_sessions()

        assert len(result) == 1
        assert result[0]["message_count"] == 1
        assert result[0]["last_user_message"] == "valid line"

    def test_sorts_newest_first(self, tmp_home):
        """Two sessions: the one with newer mtime comes first."""
        from amplifier_distro.server.apps.chat.session_history import scan_sessions

        older = _make_session(
            tmp_home,
            "-Users-test",
            "older-session",
            lines=[
                {"role": "user", "content": "older"},
            ],
        )
        # Force a deterministic mtime difference — no sleep needed.
        os.utime(older / "transcript.jsonl", (0, 0))

        _make_session(
            tmp_home,
            "-Users-test",
            "newer-session",
            lines=[
                {"role": "user", "content": "newer"},
            ],
        )

        result = scan_sessions()

        assert result[0]["session_id"] == "newer-session"
        assert result[1]["session_id"] == "older-session"

    def test_truncates_last_user_message_to_120_chars(self, tmp_home):
        from amplifier_distro.server.apps.chat.session_history import scan_sessions

        long_msg = "x" * 200
        _make_session(
            tmp_home,
            "-Users-test",
            "long-msg",
            lines=[
                {"role": "user", "content": long_msg},
            ],
        )

        result = scan_sessions()

        assert len(result[0]["last_user_message"]) == 120

    def test_skips_files_at_project_root(self, tmp_home):
        """Files (not dirs) in projects/ are ignored without crashing."""
        from amplifier_distro.server.apps.chat.session_history import scan_sessions

        projects = tmp_home / "projects"
        projects.mkdir(parents=True)
        (projects / "not-a-dir.txt").write_text("ignore me")

        result = scan_sessions()

        assert result == []

    def test_skips_non_role_lines_in_message_count(self, tmp_home):
        """Lines without a 'role' key are not counted as messages."""
        from amplifier_distro.server.apps.chat.session_history import scan_sessions

        _make_session(
            tmp_home,
            "-Users-test",
            "mixed-lines",
            lines=[
                {"type": "metadata", "value": "not a message"},
                {"role": "user", "content": "real message"},
                {"role": "assistant", "content": "reply"},
            ],
        )

        result = scan_sessions()

        assert result[0]["message_count"] == 2

    def test_last_user_message_from_list_content_block(self, tmp_home):
        """User messages with list content extract the first text block."""
        from amplifier_distro.server.apps.chat.session_history import scan_sessions

        _make_session(
            tmp_home,
            "-Users-test",
            "blocks",
            lines=[
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "x", "content": "ok"},
                        {"type": "text", "text": "now do the next thing"},
                    ],
                },
            ],
        )
        result = scan_sessions()
        assert result[0]["last_user_message"] == "now do the next thing"

    def test_last_user_message_not_clobbered_by_tool_result_turn(self, tmp_home):
        """A user turn with only tool_result blocks must not clear last_user_message."""
        from amplifier_distro.server.apps.chat.session_history import scan_sessions

        _make_session(
            tmp_home,
            "-Users-test",
            "tool-loop",
            lines=[
                {"role": "user", "content": "please rename the file"},
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "name": "bash", "id": "t1"}],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "t1", "content": "done"}
                    ],
                },
            ],
        )
        result = scan_sessions()
        assert result[0]["last_user_message"] == "please rename the file"

    def test_empty_cwd_from_info_falls_back_to_decode(self, tmp_home):
        """session-info.json with working_dir=\'\' falls back to _decode_cwd."""
        from amplifier_distro.server.apps.chat.session_history import scan_sessions

        session_dir = _make_session(
            tmp_home,
            "-Users-test",
            "empty-cwd",
            lines=[
                {"role": "user", "content": "hi"},
            ],
        )
        (session_dir / "session-info.json").write_text(
            json.dumps({"working_dir": ""}), encoding="utf-8"
        )
        result = scan_sessions()
        assert result[0]["cwd"] == "/Users/test"

    def test_malformed_session_info_json_falls_back_to_decode(self, tmp_home):
        """Corrupted session-info.json silently falls back to _decode_cwd."""
        from amplifier_distro.server.apps.chat.session_history import scan_sessions

        session_dir = _make_session(
            tmp_home,
            "-Users-test",
            "corrupt-info",
            lines=[
                {"role": "user", "content": "hi"},
            ],
        )
        (session_dir / "session-info.json").write_text(
            "{not valid json", encoding="utf-8"
        )
        result = scan_sessions()
        assert result[0]["cwd"] == "/Users/test"

    def test_skips_file_inside_sessions_dir(self, tmp_home):
        """A non-directory entry inside sessions/ is silently ignored."""
        from amplifier_distro.server.apps.chat.session_history import scan_sessions

        proj = tmp_home / "projects" / "-Users-test" / "sessions"
        proj.mkdir(parents=True)
        (proj / "lock.pid").write_text("1234")
        _make_session(
            tmp_home,
            "-Users-test",
            "real-session",
            lines=[{"role": "user", "content": "hi"}],
        )
        result = scan_sessions()
        assert len(result) == 1
        assert result[0]["session_id"] == "real-session"

    def test_skips_project_dir_symlink_escape(self, tmp_home):
        """A project dir that is a symlink escaping projects/ is silently skipped."""
        from amplifier_distro.server.apps.chat.session_history import scan_sessions

        # Create a real directory outside projects/ with a session inside it
        target = tmp_home / "secret"
        target.mkdir()
        (target / "sessions" / "sess-leaked").mkdir(parents=True)
        # Symlink inside projects/ pointing to that external directory
        projects_dir = tmp_home / "projects"
        projects_dir.mkdir(parents=True, exist_ok=True)
        (projects_dir / "escaped-link").symlink_to(target)

        result = scan_sessions()
        assert result == []  # symlink escape caught, nothing returned

    def test_skips_session_with_invalid_id_characters(self, tmp_home):
        """Session dirs with path-unsafe names are silently skipped."""
        from amplifier_distro.server.apps.chat.session_history import scan_sessions

        proj = tmp_home / "projects" / "-Users-test" / "sessions"
        proj.mkdir(parents=True)
        # Create a session dir with spaces in the name
        (proj / "invalid name with spaces").mkdir()
        # Also create a valid session to confirm valid ones still work
        _make_session(
            tmp_home,
            "-Users-test",
            "valid-session",
            lines=[{"role": "user", "content": "hi"}],
        )

        result = scan_sessions()
        assert len(result) == 1
        assert result[0]["session_id"] == "valid-session"


# —— TestSessionHistoryEndpoint ————————————————————————————————————


class TestSessionHistoryEndpoint:
    def test_history_returns_200(self, chat_client):
        r = chat_client.get("/apps/chat/api/sessions/history")
        assert r.status_code == 200

    def test_history_response_has_sessions_key(self, chat_client):
        r = chat_client.get("/apps/chat/api/sessions/history")
        assert "sessions" in r.json()

    def test_history_empty_when_no_sessions(self, chat_client):
        r = chat_client.get("/apps/chat/api/sessions/history")
        assert r.json()["sessions"] == []

    def test_history_returns_session_shape(self, chat_client, tmp_path):
        """A session on disk appears in the response with the correct fields."""
        _make_session(
            tmp_path,
            "-Users-test",
            "endpoint-sess-1",
            lines=[
                {"role": "user", "content": "what time is it"},
                {"role": "assistant", "content": "It is noon."},
            ],
        )

        r = chat_client.get("/apps/chat/api/sessions/history")

        assert r.status_code == 200
        sessions = r.json()["sessions"]
        assert len(sessions) == 1
        s = sessions[0]
        assert s["session_id"] == "endpoint-sess-1"
        assert s["cwd"] == "/Users/test"
        assert s["message_count"] == 2
        assert s["last_user_message"] == "what time is it"
        assert "last_updated" in s

    def test_history_sessions_sorted_newest_first(self, chat_client, tmp_path):
        """Newest sessions appear first in the list."""
        older = _make_session(
            tmp_path, "-Users-test", "older", lines=[{"role": "user", "content": "old"}]
        )
        os.utime(
            older / "transcript.jsonl", (0, 0)
        )  # force mtime to epoch — deterministic, no sleep needed
        _make_session(
            tmp_path, "-Users-test", "newer", lines=[{"role": "user", "content": "new"}]
        )

        sessions = chat_client.get("/apps/chat/api/sessions/history").json()["sessions"]

        assert sessions[0]["session_id"] == "newer"
        assert sessions[1]["session_id"] == "older"
