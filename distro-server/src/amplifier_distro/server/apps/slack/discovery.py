"""Discover local Amplifier sessions from the filesystem.

Sessions are stored at:
    ~/.amplifier/projects/<encoded-project-path>/sessions/<session-uuid>/

Where <encoded-project-path> encodes the absolute path by replacing '/' with '-'
and prepending '-'. For example:
    /home/sam/dev/my-project -> -home-sam-dev-my-project

Each session directory contains:
    - transcript.jsonl (required - without this, the session is ignored)
    - metadata.json (optional - provides name/description)
    - session-info.json (optional)
    - events.jsonl (optional)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class DiscoveredSession:
    """A session found on the local filesystem."""

    session_id: str
    project: str  # Short label (e.g., "my-project")
    project_path: str  # Full reconstructed path
    mtime: float  # Filesystem modification time
    date_str: str  # Formatted date string
    name: str = ""  # From metadata.json
    description: str = ""  # From metadata.json


@dataclass
class DiscoveredProject:
    """A project found on the local filesystem."""

    project_id: str  # The encoded directory name
    project_name: str  # Short label
    project_path: str  # Reconstructed full path
    session_count: int  # Number of sessions
    last_active: str  # Most recent session date


class AmplifierDiscovery:
    """Discovers Amplifier sessions from the local filesystem.

    This mirrors the discovery logic from amplifier-tui's SessionManager,
    scanning ~/.amplifier/projects/ for session directories.
    """

    def __init__(self, amplifier_home: str = "~/.amplifier") -> None:
        self._home = Path(amplifier_home).expanduser()
        self._projects_dir = self._home / "projects"

    @property
    def projects_dir(self) -> Path:
        return self._projects_dir

    def list_sessions(
        self,
        limit: int = 50,
        project_filter: str | None = None,
    ) -> list[DiscoveredSession]:
        """List recent sessions across all projects.

        Args:
            limit: Maximum number of sessions to return.
            project_filter: If set, only return sessions from this project.

        Returns:
            Sessions sorted by most recent first.
        """
        if not self._projects_dir.exists():
            return []

        sessions: list[DiscoveredSession] = []

        for project_dir in self._projects_dir.iterdir():
            if not project_dir.is_dir():
                continue

            project_path = self._decode_project_path(project_dir.name)
            project_name = self._extract_project_name(project_path)

            if project_filter and project_name != project_filter:
                continue

            sessions_dir = project_dir / "sessions"
            if not sessions_dir.exists():
                # Some projects store sessions directly
                sessions_dir = project_dir

            for session_dir in sessions_dir.iterdir():
                if not session_dir.is_dir():
                    continue

                session_id = session_dir.name

                # Skip sub-sessions (contain _ in the UUID)
                if "_" in session_id:
                    continue

                # Require transcript.jsonl
                transcript = session_dir / "transcript.jsonl"
                if not transcript.exists():
                    continue

                try:
                    mtime = transcript.stat().st_mtime
                except OSError:
                    continue

                # Load metadata if available
                name = ""
                description = ""
                metadata_file = session_dir / "metadata.json"
                if metadata_file.exists():
                    try:
                        meta = json.loads(metadata_file.read_text())
                        name = meta.get("name", "")
                        description = meta.get("description", "")
                    except (json.JSONDecodeError, OSError):
                        pass

                dt = datetime.fromtimestamp(mtime, tz=UTC)
                date_str = dt.strftime("%m/%d %H:%M")

                sessions.append(
                    DiscoveredSession(
                        session_id=session_id,
                        project=project_name,
                        project_path=project_path,
                        mtime=mtime,
                        date_str=date_str,
                        name=name,
                        description=description,
                    )
                )

        # Sort by most recent first
        sessions.sort(key=lambda s: s.mtime, reverse=True)
        return sessions[:limit]

    def get_session(self, session_id: str) -> DiscoveredSession | None:
        """Find a specific session by ID.

        Searches across all projects for the given session UUID.
        """
        if not self._projects_dir.exists():
            return None

        for project_dir in self._projects_dir.iterdir():
            if not project_dir.is_dir():
                continue

            # Check both direct and sessions/ subdirectory
            for sessions_dir in [project_dir / "sessions", project_dir]:
                session_dir = sessions_dir / session_id
                if session_dir.exists() and (session_dir / "transcript.jsonl").exists():
                    project_path = self._decode_project_path(project_dir.name)
                    project_name = self._extract_project_name(project_path)

                    mtime = (session_dir / "transcript.jsonl").stat().st_mtime
                    dt = datetime.fromtimestamp(mtime, tz=UTC)

                    name = ""
                    description = ""
                    metadata_file = session_dir / "metadata.json"
                    if metadata_file.exists():
                        try:
                            meta = json.loads(metadata_file.read_text())
                            name = meta.get("name", "")
                            description = meta.get("description", "")
                        except (json.JSONDecodeError, OSError):
                            pass

                    return DiscoveredSession(
                        session_id=session_id,
                        project=project_name,
                        project_path=project_path,
                        mtime=mtime,
                        date_str=dt.strftime("%m/%d %H:%M"),
                        name=name,
                        description=description,
                    )
        return None

    def list_projects(self) -> list[DiscoveredProject]:
        """List all known projects with session counts."""
        if not self._projects_dir.exists():
            return []

        projects: list[DiscoveredProject] = []

        for project_dir in self._projects_dir.iterdir():
            if not project_dir.is_dir():
                continue

            project_path = self._decode_project_path(project_dir.name)
            project_name = self._extract_project_name(project_path)

            # Count sessions
            sessions_dir = project_dir / "sessions"
            if not sessions_dir.exists():
                sessions_dir = project_dir

            session_count = 0
            latest_mtime = 0.0

            for session_dir in sessions_dir.iterdir():
                if not session_dir.is_dir():
                    continue
                if "_" in session_dir.name:
                    continue
                transcript = session_dir / "transcript.jsonl"
                if transcript.exists():
                    session_count += 1
                    try:
                        mt = transcript.stat().st_mtime
                        if mt > latest_mtime:
                            latest_mtime = mt
                    except OSError:
                        pass

            if session_count > 0:
                dt = datetime.fromtimestamp(latest_mtime, tz=UTC)
                projects.append(
                    DiscoveredProject(
                        project_id=project_dir.name,
                        project_name=project_name,
                        project_path=project_path,
                        session_count=session_count,
                        last_active=dt.strftime("%m/%d %H:%M"),
                    )
                )

        projects.sort(key=lambda p: p.project_name)
        return projects

    @staticmethod
    def _decode_project_path(dir_name: str) -> str:
        """Decode an encoded project directory name back to a path.

        The encoding replaces '/' with '-' and prepends '-'.
        E.g., '-home-sam-dev-project' -> '/home/sam/dev/project'
        """
        if dir_name.startswith("-"):
            return dir_name.replace("-", "/")
        return dir_name

    @staticmethod
    def _extract_project_name(project_path: str) -> str:
        """Extract a short project name from the full path.

        Takes the last path component.
        E.g., '/home/sam/dev/my-project' -> 'my-project'
        """
        parts = project_path.rstrip("/").split("/")
        return parts[-1] if parts else project_path
