"""Backup System Tests

These tests validate the backup system: file collection,
backup/restore flows, and CLI commands.

Exit criteria verified:
1. File collection includes correct files (conventions.BACKUP_INCLUDE)
2. File collection excludes keys, server
3. Backup flow creates repo and pushes (mocked gh/git)
4. Restore flow clones and copies (mocked git)
5. Restore never restores keys.yaml
6. CLI backup and restore commands exist and work
7. Configurable repo names via CLI --name flag
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from amplifier_distro import conventions
from amplifier_distro.backup import (
    BackupResult,
    RestoreResult,
    _resolve_repo,
    collect_backup_files,
)
from amplifier_distro.cli import main

# ---------------------------------------------------------------------------
#  Repo name resolution
# ---------------------------------------------------------------------------


class TestResolveRepo:
    """Verify _resolve_repo builds the correct owner/repo string."""

    def test_defaults_to_gh_handle(self):
        assert _resolve_repo("alice") == "alice/amplifier-backup"

    def test_custom_repo_name(self):
        assert _resolve_repo("alice", repo_name="my-bak") == "alice/my-bak"

    def test_custom_owner_overrides_handle(self):
        assert _resolve_repo("alice", repo_owner="myorg") == "myorg/amplifier-backup"

    def test_custom_owner_and_name(self):
        assert (
            _resolve_repo("alice", repo_name="state", repo_owner="myorg")
            == "myorg/state"
        )


# ---------------------------------------------------------------------------
#  File collection
# ---------------------------------------------------------------------------


class TestCollectBackupFiles:
    """Verify collect_backup_files includes/excludes the right things."""

    @pytest.fixture()
    def amp_home(self, tmp_path):
        """Create a realistic ~/.amplifier directory tree."""
        home = tmp_path / ".amplifier"
        home.mkdir()

        # Included files
        (home / conventions.SETTINGS_FILENAME).write_text("theme: dark")

        # Included directory: memory/
        mem = home / conventions.MEMORY_DIR
        mem.mkdir()
        (mem / conventions.MEMORY_STORE_FILENAME).write_text("memories: []")
        (mem / conventions.WORK_LOG_FILENAME).write_text("log: []")

        # Excluded files / directories
        (home / conventions.KEYS_FILENAME).write_text("SECRET=abc")
        server = home / conventions.SERVER_DIR
        server.mkdir()
        (server / "server.pid").write_text("1234")

        return home

    def test_includes_settings(self, amp_home):
        files = collect_backup_files(amp_home)
        names = [f.name for f in files]
        assert conventions.SETTINGS_FILENAME in names

    def test_includes_memory_files(self, amp_home):
        files = collect_backup_files(amp_home)
        names = [f.name for f in files]
        assert conventions.MEMORY_STORE_FILENAME in names
        assert conventions.WORK_LOG_FILENAME in names

    def test_excludes_keys(self, amp_home):
        files = collect_backup_files(amp_home)
        names = [f.name for f in files]
        assert conventions.KEYS_FILENAME not in names

    def test_excludes_server(self, amp_home):
        files = collect_backup_files(amp_home)
        rel_parts = [f.relative_to(amp_home).parts[0] for f in files]
        assert conventions.SERVER_DIR not in rel_parts

    def test_returns_sorted_paths(self, amp_home):
        files = collect_backup_files(amp_home)
        assert files == sorted(files)

    def test_empty_home_returns_empty_list(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        assert collect_backup_files(empty) == []

    def test_returns_absolute_paths(self, amp_home):
        files = collect_backup_files(amp_home)
        assert all(f.is_absolute() for f in files)


# ---------------------------------------------------------------------------
#  Backup flow (mocked subprocess)
# ---------------------------------------------------------------------------


class TestBackupFlow:
    """Verify the backup function orchestrates gh and git correctly."""

    @pytest.fixture()
    def amp_home(self, tmp_path):
        home = tmp_path / ".amplifier"
        home.mkdir()
        (home / conventions.SETTINGS_FILENAME).write_text("ok")
        mem = home / conventions.MEMORY_DIR
        mem.mkdir()
        (mem / "note.yaml").write_text("hello")
        return home

    @patch("amplifier_distro.backup._run_git")
    @patch("amplifier_distro.backup._ensure_repo_exists", return_value=True)
    def test_backup_success(self, _mock_repo, _mock_git, amp_home):
        from amplifier_distro.backup import backup

        result = backup(amp_home, "alice")
        assert result.status == "success"
        assert result.repo == "alice/amplifier-backup"
        assert len(result.files) > 0
        assert result.timestamp != ""

    @patch("amplifier_distro.backup._ensure_repo_exists", return_value=False)
    def test_backup_fails_when_repo_unavailable(self, _mock_repo, amp_home):
        from amplifier_distro.backup import backup

        result = backup(amp_home, "alice")
        assert result.status == "error"
        assert "Could not create" in result.message

    def test_backup_no_files(self, tmp_path):
        from amplifier_distro.backup import backup

        empty = tmp_path / "empty"
        empty.mkdir()
        result = backup(empty, "alice")
        assert result.status == "error"
        assert "No files" in result.message

    @patch(
        "amplifier_distro.backup._ensure_repo_exists",
        side_effect=FileNotFoundError,
    )
    def test_backup_handles_missing_gh_cli(self, _mock, amp_home):
        from amplifier_distro.backup import backup

        result = backup(amp_home, "alice")
        assert result.status == "error"
        assert "gh CLI" in result.message


# ---------------------------------------------------------------------------
#  Restore flow (mocked subprocess)
# ---------------------------------------------------------------------------


class TestRestoreFlow:
    """Verify the restore function clones and copies correctly."""

    @patch("amplifier_distro.backup.subprocess.run")
    def test_restore_success(self, mock_run, tmp_path):
        from amplifier_distro.backup import restore

        amp_home = tmp_path / ".amplifier"
        amp_home.mkdir()

        def fake_clone(cmd, **kwargs):
            """Simulate git clone by creating files in the target dir."""
            clone_dir = Path(cmd[-1])
            clone_dir.mkdir(parents=True, exist_ok=True)
            (clone_dir / conventions.SETTINGS_FILENAME).write_text("settings")
            # Simulate .git dir (should be skipped)
            git_dir = clone_dir / ".git"
            git_dir.mkdir()
            (git_dir / "config").write_text("gitconfig")
            # Simulate keys.yaml in backup (should NOT be restored)
            (clone_dir / conventions.KEYS_FILENAME).write_text("SECRET")
            return MagicMock(returncode=0)

        mock_run.side_effect = fake_clone
        result = restore(amp_home, "alice")

        assert result.status == "success"
        assert len(result.files) == 1  # settings.yaml only
        assert conventions.KEYS_FILENAME not in result.files
        assert (amp_home / conventions.SETTINGS_FILENAME).exists()
        assert not (amp_home / conventions.KEYS_FILENAME).exists()

    @patch(
        "amplifier_distro.backup.subprocess.run",
        side_effect=FileNotFoundError,
    )
    def test_restore_clone_failure(self, _mock, tmp_path):
        from amplifier_distro.backup import restore

        amp_home = tmp_path / ".amplifier"
        amp_home.mkdir()
        result = restore(amp_home, "alice")
        assert result.status == "error"
        assert "Clone failed" in result.message

    @patch("amplifier_distro.backup.subprocess.run")
    def test_restore_never_restores_keys(self, mock_run, tmp_path):
        """Security: keys.yaml must NEVER be restored even if present."""
        from amplifier_distro.backup import restore

        amp_home = tmp_path / ".amplifier"
        amp_home.mkdir()

        def fake_clone(cmd, **kwargs):
            clone_dir = Path(cmd[-1])
            clone_dir.mkdir(parents=True, exist_ok=True)
            (clone_dir / conventions.KEYS_FILENAME).write_text("SECRET=xyz")
            return MagicMock(returncode=0)

        mock_run.side_effect = fake_clone
        result = restore(amp_home, "alice")

        assert result.status == "success"
        assert not (amp_home / conventions.KEYS_FILENAME).exists()
        assert conventions.KEYS_FILENAME in result.message


# ---------------------------------------------------------------------------
#  CLI commands
# ---------------------------------------------------------------------------


class TestBackupCLI:
    """Verify backup and restore CLI commands exist and accept --name."""

    def test_backup_command_help(self):
        runner = CliRunner()
        result = runner.invoke(main, ["backup", "--help"])
        assert result.exit_code == 0
        assert "backup" in result.output.lower()

    def test_restore_command_help(self):
        runner = CliRunner()
        result = runner.invoke(main, ["restore", "--help"])
        assert result.exit_code == 0
        assert "restore" in result.output.lower()

    def test_backup_has_name_option(self):
        runner = CliRunner()
        result = runner.invoke(main, ["backup", "--help"])
        assert "--name" in result.output

    def test_restore_has_name_option(self):
        runner = CliRunner()
        result = runner.invoke(main, ["restore", "--help"])
        assert "--name" in result.output

    @patch("amplifier_distro.backup._detect_gh_handle", return_value=None)
    def test_backup_fails_without_gh_handle(self, _mock):
        """backup must fail if gh handle cannot be detected."""
        runner = CliRunner()
        result = runner.invoke(main, ["backup"])
        assert result.exit_code != 0
        assert "GitHub handle" in result.output or "gh CLI" in result.output

    @patch("amplifier_distro.backup._detect_gh_handle", return_value=None)
    def test_restore_fails_without_gh_handle(self, _mock):
        """restore must fail if gh handle cannot be detected."""
        runner = CliRunner()
        result = runner.invoke(main, ["restore"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
#  Result models
# ---------------------------------------------------------------------------


class TestResultModels:
    """Verify BackupResult and RestoreResult data models."""

    def test_backup_result_defaults(self):
        r = BackupResult(status="success")
        assert r.files == []
        assert r.timestamp == ""
        assert r.message == ""
        assert r.repo == ""

    def test_restore_result_defaults(self):
        r = RestoreResult(status="error", message="fail")
        assert r.files == []
        assert r.repo == ""
