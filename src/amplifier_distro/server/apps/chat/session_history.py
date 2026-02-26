"""Chat session history — scans ~/.amplifier/projects/ to discover past sessions.

Schema (one dict per session in scan_sessions() output):
    session_id: str             — session directory name (UUID-like)
    cwd: str                    — working directory decoded from project dir name
    message_count: int          — number of transcript lines with a 'role' key
    last_user_message: str|None — last user message text, truncated to 120 chars
    last_updated: str           — ISO-format mtime of transcript.jsonl (or session dir)
    revision: str               — mtime_ns:size signature for stale-change detection

Performance note: scan_sessions() reads transcript.jsonl for every session.
Large transcripts (>10k lines) are scanned completely. This is acceptable
for typical session counts (<200) but should be profiled if latency becomes
an issue. Future optimization: seek from end of file for last_user_message.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from amplifier_distro.conventions import (
    AMPLIFIER_HOME,
    PROJECTS_DIR,
    SESSION_INFO_FILENAME,
    TRANSCRIPT_FILENAME,
)

logger = logging.getLogger(__name__)

_AMPLIFIER_HOME_OVERRIDE: str | None = None  # Overridable in tests
# Same character set as _VALID_SESSION_ID in chat/__init__.py —
# keep in sync if session ID format changes
_VALID_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]+$")


def _get_amplifier_home() -> str:
    return (
        _AMPLIFIER_HOME_OVERRIDE
        if _AMPLIFIER_HOME_OVERRIDE is not None
        else AMPLIFIER_HOME
    )


def _decode_cwd(project_dir_name: str) -> str:
    """Decode project directory name back to filesystem path.

    The Amplifier framework encodes CWD via get_project_slug(): every '/'
    (and '\\') in the absolute path is replaced with '-'.  This is lossy —
    a literal '-' in a directory name is indistinguishable from a '/'.

    We resolve ambiguity by walking the filesystem greedily: at each
    position we try the shortest component (fewest dash-joined parts) whose
    path actually exists on disk and recurse.  If the whole path can't be
    reconstructed (temp dirs, CI, or just an unknown machine) we fall back
    to the naïve replacement.

    Example: '-Users-alice-repo-amplifier-distro'
      → filesystem finds /Users/alice/repo/amplifier-distro  ✓
      → naïve would give /Users/alice/repo/amplifier/distro  ✗
    """
    if not project_dir_name.startswith("-"):
        return project_dir_name.replace("-", "/")

    parts = project_dir_name[1:].split("-")

    def _search(idx: int, current: Path) -> str | None:
        if idx == len(parts):
            return str(current)
        # Try consuming 1…N parts as a single path component (shortest first)
        for end in range(idx + 1, len(parts) + 1):
            component = "-".join(parts[idx:end])
            candidate = current / component
            if candidate.exists():
                result = _search(end, candidate)
                if result is not None:
                    return result
        return None

    resolved = _search(0, Path("/"))
    if resolved is not None:
        return resolved
    # Fallback: naïve replacement (correct when no literal dashes exist in names)
    return "/" + "/".join(parts)


def _read_session_meta(session_dir: Path) -> dict[str, Any]:
    """Extract lightweight metadata from a single session directory."""
    # Try to read CWD from session-info.json (written by Amplifier framework)
    cwd_from_info: str | None = None
    info_path = session_dir / SESSION_INFO_FILENAME
    try:
        data = json.loads(info_path.read_text(encoding="utf-8"))
        raw = data.get("working_dir")
        if isinstance(raw, str) and raw:
            normalized = os.path.normpath(raw)
            if os.path.isabs(normalized) and len(normalized) <= 4096:
                cwd_from_info = normalized
            else:
                cwd_from_info = None
        else:
            cwd_from_info = None
    except (OSError, json.JSONDecodeError):
        pass

    transcript_path = session_dir / TRANSCRIPT_FILENAME

    message_count = 0
    last_user_message: str | None = None

    last_updated, revision = _session_revision_signature(session_dir)

    if transcript_path.exists():
        try:
            with transcript_path.open(encoding="utf-8") as f:
                for raw_line in f:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(entry, dict) or not entry.get("role"):
                        continue
                    message_count += 1
                    if entry["role"] == "user":
                        content = entry.get("content", "")
                        if isinstance(content, str):
                            last_user_message = content[:120]
                        elif isinstance(content, list):
                            for block in content:
                                if (
                                    isinstance(block, dict)
                                    and block.get("type") == "text"
                                ):
                                    last_user_message = (block.get("text") or "")[:120]
                                    break
        except OSError:
            logger.warning(
                "Could not read transcript at %s", transcript_path, exc_info=True
            )

    return {
        "session_id": session_dir.name,
        "message_count": message_count,
        "last_user_message": last_user_message,
        "last_updated": last_updated,
        "revision": revision,
        "cwd_from_info": cwd_from_info,  # verbatim CWD if available
    }


def _session_revision_signature(session_dir: Path) -> tuple[str, str]:
    """Return (last_updated_iso, revision_signature) for one session directory."""
    transcript_path = session_dir / TRANSCRIPT_FILENAME
    stat_target = transcript_path if transcript_path.exists() else session_dir
    try:
        stat = stat_target.stat()
        last_updated = datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat()
        mtime_ns = getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))
        revision = f"{int(mtime_ns)}:{int(stat.st_size)}"
        return last_updated, revision
    except OSError:
        return datetime.now(tz=UTC).isoformat(), "0:0"


def _iter_session_dirs(projects_path: Path) -> list[Path]:
    """Return validated session directories under ~/.amplifier/projects."""
    if not projects_path.exists():
        return []

    try:
        project_dirs = list(projects_path.iterdir())
    except OSError:
        logger.warning("Could not list projects at %s", projects_path, exc_info=True)
        return []

    try:
        resolved_projects = projects_path.resolve()
    except OSError:
        logger.warning(
            "Could not resolve projects path %s",
            projects_path,
            exc_info=True,
        )
        return []

    session_dirs: list[Path] = []
    for project_dir in project_dirs:
        # Symlink containment — skip any project dir that escapes projects_path
        try:
            if project_dir.resolve().parent != resolved_projects:
                logger.warning("Skipping symlink escape: %s", project_dir)
                continue
        except OSError:
            logger.warning(
                "Could not resolve path for %s — skipping", project_dir, exc_info=True
            )
            continue

        if not project_dir.is_dir():
            continue

        sessions_subdir = project_dir / "sessions"
        if not sessions_subdir.is_dir():
            continue

        try:
            resolved_sessions = sessions_subdir.resolve()
            candidates = [
                d
                for d in sessions_subdir.iterdir()
                if d.is_dir() and d.resolve().is_relative_to(resolved_sessions)
            ]
        except OSError:
            logger.warning(
                "Could not list sessions in %s", sessions_subdir, exc_info=True
            )
            continue

        for session_dir in candidates:
            if not _VALID_SESSION_ID_RE.fullmatch(session_dir.name):
                logger.debug(
                    "Skipping session dir with non-standard name: %r", session_dir.name
                )
                continue
            session_dirs.append(session_dir)

    return session_dirs


def scan_sessions(amplifier_home: str | None = None) -> list[dict[str, Any]]:
    """Scan ~/.amplifier/projects/ and return lightweight metadata for all sessions.

    Returns a list sorted newest-first by last_updated.
    Never raises — malformed sessions are included with degraded metadata.
    """
    home = amplifier_home or _get_amplifier_home()
    projects_path = Path(home).expanduser() / PROJECTS_DIR

    results: list[dict[str, Any]] = []
    for session_dir in _iter_session_dirs(projects_path):
        try:
            meta = _read_session_meta(session_dir)
            project_dir_name = session_dir.parent.parent.name
            # Prefer verbatim CWD from session-info.json; fall back to decoded name
            meta["cwd"] = meta.pop("cwd_from_info") or _decode_cwd(project_dir_name)
            results.append(meta)
        except Exception:  # noqa: BLE001
            logger.warning(
                "Skipping session %s due to unexpected error",
                session_dir,
                exc_info=True,
            )

    results.sort(key=lambda s: s["last_updated"], reverse=True)
    return results


def scan_session_revisions(
    session_ids: set[str] | None = None,
    amplifier_home: str | None = None,
) -> list[dict[str, str]]:
    """Return lightweight revision metadata for session directories on disk."""
    home = amplifier_home or _get_amplifier_home()
    projects_path = Path(home).expanduser() / PROJECTS_DIR
    wanted = set(session_ids) if session_ids is not None else None

    rows: list[dict[str, str]] = []
    for session_dir in _iter_session_dirs(projects_path):
        session_id = session_dir.name
        if wanted is not None and session_id not in wanted:
            continue
        last_updated, revision = _session_revision_signature(session_dir)
        rows.append(
            {
                "session_id": session_id,
                "last_updated": last_updated,
                "revision": revision,
            }
        )

    rows.sort(key=lambda s: s["last_updated"], reverse=True)
    return rows
