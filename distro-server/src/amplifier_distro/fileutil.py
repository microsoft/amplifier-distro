"""File utilities for amplifier-distro.

Provides atomic_write() for crash-safe file persistence.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path


def atomic_write(path: Path, content: str) -> None:
    """Write content to *path* atomically via temp-file + rename.

    Guarantees that *path* is never left in a truncated or partially-written
    state.  On success the file contains exactly *content*; on any failure
    the previous file (if any) is untouched.

    Uses ``os.fdopen`` to handle short writes internally, and ``os.fsync``
    to ensure data is durable before renaming.

    Works on all platforms (``os.replace`` is atomic on POSIX; on Windows it
    is atomic when source and destination are on the same volume).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = -1  # os.fdopen owns the fd now
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        # Clean up temp file on failure (best-effort)
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
