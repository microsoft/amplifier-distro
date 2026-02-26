"""Memory service for the Amplifier Distro server.

Provides cross-interface memory storage and retrieval, compatible with
the dev-memory bundle YAML format. All interfaces (web chat, Slack, CLI)
route through this single service.

Storage paths are constructed from conventions.py constants - no hardcoded paths.

YAML format (memory-store.yaml):
    memories:
      - id: "mem-001"
        timestamp: "2026-01-05T12:00:00Z"
        category: "workflow"
        content: "The actual memory text"
        tags: ["tag1", "tag2"]

YAML format (work-log.yaml):
    items:
      - task: "Build memory service"
        status: "in-progress"
        updated: "2026-01-05T12:00:00Z"
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from amplifier_distro import conventions

logger = logging.getLogger(__name__)

# --- Category keywords for auto-categorization ---

_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "architecture": [
        "architecture",
        "design",
        "pattern",
        "module",
        "structure",
        "layer",
        "component",
        "system",
        "interface",
        "abstraction",
    ],
    "workflow": [
        "workflow",
        "process",
        "step",
        "pipeline",
        "deploy",
        "ci",
        "cd",
        "build",
        "release",
        "sprint",
    ],
    "preference": [
        "prefer",
        "like",
        "always",
        "never",
        "habit",
        "style",
        "convention",
        "standard",
        "favorite",
        "default",
    ],
    "environment": [
        "env",
        "environment",
        "setup",
        "install",
        "config",
        "path",
        "variable",
        "os",
        "machine",
        "docker",
    ],
    "git": [
        "git",
        "commit",
        "branch",
        "merge",
        "rebase",
        "pull request",
        "pr",
        "repo",
        "clone",
        "push",
    ],
    "research": [
        "research",
        "investigate",
        "explore",
        "compare",
        "evaluate",
        "benchmark",
        "study",
        "analysis",
        "finding",
        "learned",
    ],
    "pattern": [
        "pattern",
        "idiom",
        "recipe",
        "technique",
        "trick",
        "approach",
        "method",
        "strategy",
        "solution",
        "workaround",
    ],
    "tools": [
        "tool",
        "editor",
        "ide",
        "plugin",
        "extension",
        "cli",
        "command",
        "utility",
        "library",
        "package",
    ],
}


# --- Pydantic models ---


class MemoryEntry(BaseModel):
    """A single memory entry in the store."""

    id: str
    timestamp: str
    category: str
    content: str
    tags: list[str] = Field(default_factory=list)


class MemoryStore(BaseModel):
    """The top-level memory store structure."""

    memories: list[MemoryEntry] = Field(default_factory=list)


class WorkLogItem(BaseModel):
    """A single work log entry."""

    task: str
    status: str = "pending"
    updated: str = ""


class WorkLog(BaseModel):
    """The top-level work log structure."""

    items: list[WorkLogItem] = Field(default_factory=list)


# --- Memory Service ---


class MemoryService:
    """Cross-interface memory storage and retrieval.

    Reads/writes memory-store.yaml and work-log.yaml in the
    conventional memory directory (~/.amplifier/memory/).

    All paths are derived from conventions.py constants.
    """

    def __init__(self, memory_dir: Path | None = None) -> None:
        """Initialize the memory service.

        Args:
            memory_dir: Override for the memory directory (for testing).
                        Defaults to ~/.amplifier/memory/ per conventions.
        """
        if memory_dir is None:
            memory_dir = (
                Path(conventions.AMPLIFIER_HOME).expanduser() / conventions.MEMORY_DIR
            )
        self._memory_dir = memory_dir
        self._store_path = self._memory_dir / conventions.MEMORY_STORE_FILENAME
        self._work_log_path = self._memory_dir / conventions.WORK_LOG_FILENAME

    @property
    def memory_dir(self) -> Path:
        """The memory storage directory."""
        return self._memory_dir

    @property
    def store_path(self) -> Path:
        """Path to memory-store.yaml."""
        return self._store_path

    @property
    def work_log_path(self) -> Path:
        """Path to work-log.yaml."""
        return self._work_log_path

    def _ensure_dir(self) -> None:
        """Create the memory directory if it doesn't exist."""
        self._memory_dir.mkdir(parents=True, exist_ok=True)

    def _load_store(self) -> MemoryStore:
        """Load the memory store from disk."""
        if not self._store_path.exists():
            return MemoryStore()
        try:
            data = yaml.safe_load(self._store_path.read_text()) or {}
            return MemoryStore(**data)
        except (yaml.YAMLError, OSError, ValueError):
            logger.exception("Failed to load memory store from %s", self._store_path)
            return MemoryStore()

    def _save_store(self, store: MemoryStore) -> None:
        """Save the memory store to disk."""
        self._ensure_dir()
        data = {"memories": [m.model_dump() for m in store.memories]}
        from amplifier_distro.fileutil import atomic_write

        atomic_write(self._store_path, yaml.dump(data, default_flow_style=False))

    def _load_work_log(self) -> WorkLog:
        """Load the work log from disk."""
        if not self._work_log_path.exists():
            return WorkLog()
        try:
            data = yaml.safe_load(self._work_log_path.read_text()) or {}
            return WorkLog(**data)
        except (yaml.YAMLError, OSError, ValueError):
            logger.exception("Failed to load work log from %s", self._work_log_path)
            return WorkLog()

    def _save_work_log(self, log: WorkLog) -> None:
        """Save the work log to disk."""
        self._ensure_dir()
        data = {"items": [item.model_dump() for item in log.items]}
        from amplifier_distro.fileutil import atomic_write

        atomic_write(self._work_log_path, yaml.dump(data, default_flow_style=False))

    def _next_id(self, store: MemoryStore) -> str:
        """Generate the next memory ID (mem-001, mem-002, etc.)."""
        if not store.memories:
            return "mem-001"
        # Extract numeric suffixes from existing IDs
        max_num = 0
        for m in store.memories:
            match = re.match(r"mem-(\d+)", m.id)
            if match:
                max_num = max(max_num, int(match.group(1)))
        return f"mem-{max_num + 1:03d}"

    def _auto_categorize(self, text: str) -> str:
        """Auto-categorize a memory based on keyword matching.

        Returns the best-matching category, or 'general' if no match.
        """
        text_lower = text.lower()
        best_category = "general"
        best_score = 0

        for category, keywords in _CATEGORY_KEYWORDS.items():
            score = sum(1 for kw in keywords if kw in text_lower)
            if score > best_score:
                best_score = score
                best_category = category

        return best_category

    def _auto_tags(self, text: str, category: str) -> list[str]:
        """Extract auto-generated tags from memory text.

        Tags are derived from the category and significant words.
        """
        tags = [category]

        # Add tags for any category keywords found in the text
        text_lower = text.lower()
        for cat, keywords in _CATEGORY_KEYWORDS.items():
            if cat == category:
                continue
            for kw in keywords:
                if kw in text_lower and kw not in tags:
                    tags.append(kw)
                    break  # One tag per secondary category

        return tags

    def remember(self, text: str) -> dict[str, Any]:
        """Store a memory with auto-ID, auto-category, auto-tags, timestamp.

        Args:
            text: The memory content to store.

        Returns:
            Dict with the stored memory entry details.
        """
        store = self._load_store()
        mem_id = self._next_id(store)
        category = self._auto_categorize(text)
        tags = self._auto_tags(text, category)
        timestamp = datetime.now(UTC).isoformat()

        entry = MemoryEntry(
            id=mem_id,
            timestamp=timestamp,
            category=category,
            content=text,
            tags=tags,
        )
        store.memories.append(entry)
        self._save_store(store)

        logger.info("Stored memory %s (category: %s)", mem_id, category)
        return entry.model_dump()

    def recall(self, query: str) -> list[dict[str, Any]]:
        """Search memories by content, tags, and category.

        Uses case-insensitive substring matching across content, tags,
        and category fields.

        Args:
            query: Search query string.

        Returns:
            List of matching memory entries as dicts.
        """
        store = self._load_store()
        query_lower = query.lower()
        results = []

        for m in store.memories:
            # Match against content
            if query_lower in m.content.lower():
                results.append(m.model_dump())
                continue
            # Match against category
            if query_lower in m.category.lower():
                results.append(m.model_dump())
                continue
            # Match against tags
            if any(query_lower in tag.lower() for tag in m.tags):
                results.append(m.model_dump())
                continue

        return results

    def work_status(self) -> dict[str, Any]:
        """Read the current work log.

        Returns:
            Dict with work log items.
        """
        log = self._load_work_log()
        return {"items": [item.model_dump() for item in log.items]}

    def update_work_log(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        """Update the work log with new items.

        Args:
            items: List of work log item dicts with 'task', 'status' keys.

        Returns:
            Dict with the updated work log items.
        """
        timestamp = datetime.now(UTC).isoformat()
        log_items = [
            WorkLogItem(
                task=item.get("task", ""),
                status=item.get("status", "pending"),
                updated=item.get("updated", timestamp),
            )
            for item in items
        ]
        log = WorkLog(items=log_items)
        self._save_work_log(log)

        logger.info("Updated work log with %d items", len(log_items))
        return {"items": [item.model_dump() for item in log.items]}


# --- Module-level singleton ---

_instance: MemoryService | None = None
_instance_lock = threading.Lock()


def get_memory_service(memory_dir: Path | None = None) -> MemoryService:
    """Get or create the memory service singleton.

    Args:
        memory_dir: Override for testing. Only used on first call.

    Returns:
        The MemoryService instance.
    """
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = MemoryService(memory_dir=memory_dir)
        return _instance


def reset_memory_service() -> None:
    """Reset the memory service singleton (for testing)."""
    global _instance
    with _instance_lock:
        _instance = None
