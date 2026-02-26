"""Tests for atomic_write utility.

Verifies that file writes are crash-safe: the target file is never
left in a truncated or partially-written state.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from amplifier_distro.fileutil import atomic_write


class TestAtomicWrite:
    """Verify atomic_write() crash-safety and correctness."""

    def test_writes_content_to_new_file(self, tmp_path: Path) -> None:
        target = tmp_path / "test.json"
        atomic_write(target, '{"key": "value"}')
        assert target.read_text() == '{"key": "value"}'

    def test_overwrites_existing_file(self, tmp_path: Path) -> None:
        target = tmp_path / "test.json"
        target.write_text("old content")
        atomic_write(target, "new content")
        assert target.read_text() == "new content"

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        target = tmp_path / "sub" / "dir" / "test.json"
        atomic_write(target, "nested")
        assert target.read_text() == "nested"

    def test_preserves_original_on_write_failure(self, tmp_path: Path) -> None:
        """If os.replace fails, the original file is untouched."""
        target = tmp_path / "test.json"
        target.write_text("original")

        with (
            patch(
                "amplifier_distro.fileutil.os.replace", side_effect=OSError("disk full")
            ),
            pytest.raises(OSError, match="disk full"),
        ):
            atomic_write(target, "should not appear")

        assert target.read_text() == "original"

    def test_cleans_up_temp_file_on_failure(self, tmp_path: Path) -> None:
        """Temp file is removed if os.replace fails."""
        target = tmp_path / "test.json"

        with (
            patch(
                "amplifier_distro.fileutil.os.replace", side_effect=OSError("disk full")
            ),
            pytest.raises(OSError),
        ):
            atomic_write(target, "content")

        # No .tmp files should remain
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == [], f"Temp files not cleaned up: {tmp_files}"

    def test_handles_unicode_content(self, tmp_path: Path) -> None:
        target = tmp_path / "unicode.txt"
        content = "Hello \u2014 \u4e16\u754c"
        atomic_write(target, content)
        assert target.read_text(encoding="utf-8") == content

    def test_empty_content(self, tmp_path: Path) -> None:
        target = tmp_path / "empty.txt"
        atomic_write(target, "")
        assert target.read_text() == ""

    def test_preserves_original_on_write_phase_failure(self, tmp_path: Path) -> None:
        """If the write itself fails (not replace), original is untouched."""
        target = tmp_path / "test.json"
        target.write_text("original")

        with patch(
            "amplifier_distro.fileutil.os.fdopen",
            side_effect=OSError("EIO"),
        ), pytest.raises(OSError, match="EIO"):
            atomic_write(target, "should not appear")

        assert target.read_text() == "original"
        assert list(tmp_path.glob("*.tmp")) == []

    def test_no_temp_files_left_on_success(self, tmp_path: Path) -> None:
        """After successful write, no .tmp files remain."""
        target = tmp_path / "test.json"
        atomic_write(target, "content")
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == []
