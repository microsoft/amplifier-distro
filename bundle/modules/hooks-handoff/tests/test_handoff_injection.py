"""Tests for handoff injection helpers and session start injection."""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import AsyncMock

from amplifier_module_hooks_handoff import HandoffConfig, HandoffHook


# ---------------------------------------------------------------------------
# Shared test helpers
# ---------------------------------------------------------------------------


def make_hook(projects_dir: str, enabled: bool = True) -> HandoffHook:
    """Create a HandoffHook wired to a tmp projects directory."""
    config = HandoffConfig(enabled=enabled, projects_dir=projects_dir)
    hook = HandoffHook(config)
    hook._coordinator = AsyncMock()
    return hook


def write_handoff(sessions_dir: Path, session_id: str, content: str) -> Path:
    """Write a handoff.md into sessions_dir/<session_id>/handoff.md."""
    session_dir = sessions_dir / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / "handoff.md"
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Task 2 — _find_latest_handoff
# ---------------------------------------------------------------------------


class TestFindLatestHandoff:
    def test_no_sessions_directory_returns_none(self, tmp_path):
        """When no sessions dir exists for the project, return None."""
        hook = make_hook(str(tmp_path))
        assert hook._find_latest_handoff("my-project") is None

    def test_empty_sessions_directory_returns_none(self, tmp_path):
        """When sessions dir exists but has no handoff files, return None."""
        (tmp_path / "my-project" / "sessions").mkdir(parents=True)
        hook = make_hook(str(tmp_path))
        assert hook._find_latest_handoff("my-project") is None

    def test_single_handoff_returns_content(self, tmp_path):
        """A single handoff.md should have its content returned as a string."""
        sessions_dir = tmp_path / "my-project" / "sessions"
        write_handoff(sessions_dir, "abc123", "# Prior Work\n\nDid stuff.")
        hook = make_hook(str(tmp_path))
        assert hook._find_latest_handoff("my-project") == "# Prior Work\n\nDid stuff."

    def test_multiple_handoffs_returns_newest_by_mtime(self, tmp_path):
        """When multiple handoffs exist, return the one with the latest mtime."""
        sessions_dir = tmp_path / "my-project" / "sessions"
        old_path = write_handoff(sessions_dir, "session-old", "Old handoff content.")
        new_path = write_handoff(sessions_dir, "session-new", "New handoff content.")
        now = time.time()
        os.utime(old_path, (now - 10, now - 10))  # 10s in the past
        os.utime(new_path, (now, now))  # now
        hook = make_hook(str(tmp_path))
        assert hook._find_latest_handoff("my-project") == "New handoff content."

    def test_empty_file_returns_none(self, tmp_path):
        """An empty handoff.md should be skipped — return None."""
        sessions_dir = tmp_path / "my-project" / "sessions"
        write_handoff(sessions_dir, "abc123", "")
        hook = make_hook(str(tmp_path))
        assert hook._find_latest_handoff("my-project") is None

    def test_malformed_yaml_frontmatter_returns_raw_content(self, tmp_path):
        """Malformed YAML frontmatter is not parsed — return raw file content."""
        sessions_dir = tmp_path / "my-project" / "sessions"
        content = "---\nnot: valid: yaml: ::::\n---\n\n# Body text"
        write_handoff(sessions_dir, "abc123", content)
        hook = make_hook(str(tmp_path))
        assert hook._find_latest_handoff("my-project") == content.strip()
