"""Backup and restore for non-git Amplifier state.

Backs up configuration, memory, and custom bundles to a private GitHub
repository. Uses the ``gh`` CLI for repo creation and ``git`` for push/pull.

Security: keys.yaml is NEVER backed up or restored.  After a restore the
user must re-enter their API keys.

All paths are derived from conventions.py constants -- no hardcoded paths.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from . import conventions

# ---------------------------------------------------------------------------
#  Result models
# ---------------------------------------------------------------------------


class BackupResult(BaseModel):
    """Result of a backup operation."""

    status: str  # "success" or "error"
    files: list[str] = Field(default_factory=list)
    timestamp: str = ""
    message: str = ""
    repo: str = ""


class RestoreResult(BaseModel):
    """Result of a restore operation."""

    status: str  # "success" or "error"
    files: list[str] = Field(default_factory=list)
    message: str = ""
    repo: str = ""


# ---------------------------------------------------------------------------
#  File collection
# ---------------------------------------------------------------------------


def collect_backup_files(amplifier_home: Path) -> list[Path]:
    """Collect files to back up from *amplifier_home*.

    Uses :pydata:`conventions.BACKUP_INCLUDE` to decide which top-level
    files and directories to include.  Each entry is either a plain file
    or a directory whose contents are collected recursively.
    """
    files: list[Path] = []

    for entry in conventions.BACKUP_INCLUDE:
        path = amplifier_home / entry
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(child for child in path.rglob("*") if child.is_file())

    return sorted(files)


# ---------------------------------------------------------------------------
#  Backup
# ---------------------------------------------------------------------------


def backup(
    amplifier_home: Path,
    gh_handle: str,
    repo_name: str = "amplifier-backup",
    repo_owner: str | None = None,
) -> BackupResult:
    """Back up Amplifier state to a private GitHub repository.

    Creates a private repo via ``gh`` if it does not exist, copies the
    selected files into a temporary directory, then force-pushes them.
    """
    repo_full = _resolve_repo(gh_handle, repo_name, repo_owner)
    timestamp = datetime.now(tz=UTC).isoformat()

    # Collect files
    files = collect_backup_files(amplifier_home)
    if not files:
        return BackupResult(
            status="error",
            message="No files to back up",
            repo=repo_full,
        )

    # Ensure repo exists
    try:
        if not _ensure_repo_exists(repo_full):
            return BackupResult(
                status="error",
                message=f"Could not create or access repo {repo_full}",
                repo=repo_full,
            )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return BackupResult(
            status="error",
            message=f"gh CLI error: {exc}",
            repo=repo_full,
        )

    # Copy files into a temp dir, then push
    with tempfile.TemporaryDirectory(prefix="amplifier-backup-") as tmp:
        tmp_path = Path(tmp)

        rel_paths: list[str] = []
        for f in files:
            rel = f.relative_to(amplifier_home)
            dest = tmp_path / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest)
            rel_paths.append(str(rel))

        try:
            _run_git(tmp_path, ["init"])
            _run_git(tmp_path, ["add", "."])
            _run_git(tmp_path, ["commit", "-m", f"Backup {timestamp}"])
            _run_git(
                tmp_path,
                [
                    "remote",
                    "add",
                    "origin",
                    f"https://github.com/{repo_full}.git",
                ],
            )
            _run_git(tmp_path, ["branch", "-M", "main"])
            _run_git(tmp_path, ["push", "-u", "origin", "main", "--force"])
        except subprocess.CalledProcessError as exc:
            return BackupResult(
                status="error",
                message=f"Git error: {exc.stderr or exc.stdout or str(exc)}",
                repo=repo_full,
                files=rel_paths,
            )

    return BackupResult(
        status="success",
        files=rel_paths,
        timestamp=timestamp,
        message=f"Backed up {len(rel_paths)} file(s) to {repo_full}",
        repo=repo_full,
    )


# ---------------------------------------------------------------------------
#  Restore
# ---------------------------------------------------------------------------


def restore(
    amplifier_home: Path,
    gh_handle: str,
    repo_name: str = "amplifier-backup",
    repo_owner: str | None = None,
) -> RestoreResult:
    """Restore Amplifier state from a private GitHub repository.

    Clones the backup repo and copies files back to *amplifier_home*.
    ``keys.yaml`` is **never** restored.
    """
    repo_full = _resolve_repo(gh_handle, repo_name, repo_owner)

    with tempfile.TemporaryDirectory(prefix="amplifier-restore-") as tmp:
        clone_dest = Path(tmp) / "repo"

        try:
            subprocess.run(
                [
                    "git",
                    "clone",
                    f"https://github.com/{repo_full}.git",
                    str(clone_dest),
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=True,
            )
        except (
            FileNotFoundError,
            subprocess.TimeoutExpired,
            subprocess.CalledProcessError,
        ) as exc:
            return RestoreResult(
                status="error",
                message=f"Clone failed: {exc}",
                repo=repo_full,
            )

        # Copy files back, skipping .git/ and keys.yaml
        restored: list[str] = []
        skip_dirs = {".git"}

        for item in clone_dest.rglob("*"):
            if not item.is_file():
                continue
            rel = item.relative_to(clone_dest)

            # Skip .git internals
            if rel.parts[0] in skip_dirs:
                continue
            # Security: never restore keys
            if rel.name == conventions.KEYS_FILENAME:
                continue

            dest = amplifier_home / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, dest)
            restored.append(str(rel))

    return RestoreResult(
        status="success",
        files=sorted(restored),
        message=(
            f"Restored {len(restored)} file(s) from {repo_full}. "
            f"Note: {conventions.KEYS_FILENAME} was NOT restored "
            f"(re-enter keys manually)."
        ),
        repo=repo_full,
    )


# ---------------------------------------------------------------------------
#  Helpers (private)
# ---------------------------------------------------------------------------


def _detect_gh_handle() -> str | None:
    """Detect GitHub handle via gh CLI. Returns None on failure."""
    try:
        result = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _resolve_repo(
    gh_handle: str,
    repo_name: str = "amplifier-backup",
    repo_owner: str | None = None,
) -> str:
    """Build the ``owner/repo`` string."""
    owner = repo_owner or gh_handle
    return f"{owner}/{repo_name}"


def _ensure_repo_exists(repo_full: str) -> bool:
    """Create a private GitHub repo if it does not already exist."""
    result = subprocess.run(
        ["gh", "repo", "view", repo_full],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode == 0:
        return True

    result = subprocess.run(
        [
            "gh",
            "repo",
            "create",
            repo_full,
            "--private",
            "--description",
            "Amplifier configuration backup (auto-managed)",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.returncode == 0


def _run_git(cwd: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a git command in the given working directory."""
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
