"""Distro settings — YAML config schema, load/save/update.

Provides a typed, dataclass-based schema for all distro configuration
that isn't secrets (those stay in keys.env) or Amplifier-foundation
config (that stays in settings.yaml).

All public functions take ``settings: DistroPluginSettings`` as the first
parameter to determine the distro home directory.

Usage::

    from distro_plugin.config import DistroPluginSettings
    from distro_plugin.distro_settings import load, save, DistroSettings

    plugin_settings = DistroPluginSettings()
    ds = load(plugin_settings)
    ds.slack.hub_channel_name = "my-channel"
    save(plugin_settings, ds)
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import os
import tempfile
import threading
import typing
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from distro_plugin.config import DistroPluginSettings

logger = logging.getLogger(__name__)

_SETTINGS_FILENAME = "settings.yaml"

# Guards the load/modify/save cycle so concurrent API requests
# cannot clobber each other.
_settings_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


@dataclass
class IdentitySettings:
    """User identity (persisted from wizard detection)."""

    github_handle: str = ""
    git_email: str = ""


@dataclass
class BackupSettings:
    """GitHub backup repository configuration."""

    repo_name: str = "amplifier-backup"
    repo_owner: str = ""  # empty = same as github_handle


@dataclass
class SlackSettings:
    """Slack bridge non-secret configuration."""

    hub_channel_id: str = ""
    hub_channel_name: str = "amplifier"
    socket_mode: bool = False
    default_working_dir: str = "~"
    simulator_mode: bool = False
    thread_per_session: bool = True
    allow_breakout: bool = True
    channel_prefix: str = "amp-"
    bot_name: str = "slackbridge"
    default_bundle: str = ""
    max_message_length: int = 3900
    response_timeout: int = 300


@dataclass
class VoiceSettings:
    """Voice bridge configuration."""

    voice: str = "ash"
    model: str = "gpt-4o-realtime-preview"
    instructions: str = ""
    tools_enabled: bool = False
    assistant_name: str = "Amplifier"


@dataclass
class WatchdogSettings:
    """Watchdog timing configuration."""

    check_interval: int = 30
    restart_after: int = 300
    max_restarts: int = 5


@dataclass
class TlsSettings:
    """TLS configuration for the distro server."""

    mode: str = "off"
    certfile: str = ""
    keyfile: str = ""


@dataclass
class ServerSettings:
    """Server runtime configuration."""

    tls: TlsSettings = field(default_factory=TlsSettings)


@dataclass
class DistroSettings:
    """Root settings object for the distro experience layer."""

    workspace_root: str = "~"
    identity: IdentitySettings = field(default_factory=IdentitySettings)
    backup: BackupSettings = field(default_factory=BackupSettings)
    slack: SlackSettings = field(default_factory=SlackSettings)
    voice: VoiceSettings = field(default_factory=VoiceSettings)
    watchdog: WatchdogSettings = field(default_factory=WatchdogSettings)
    server: ServerSettings = field(default_factory=ServerSettings)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _nested_from_dict(cls: type, data: dict[str, Any]) -> Any:
    """Recursively instantiate a dataclass from a dict, ignoring unknown keys.

    Uses ``typing.get_type_hints`` to resolve annotations that are stored as
    strings when ``from __future__ import annotations`` is active.
    """
    try:
        hints = typing.get_type_hints(cls)
    except (NameError, AttributeError, TypeError):
        hints = {}

    filtered = {}
    for fld in dataclasses.fields(cls):
        if fld.name not in data:
            continue
        value = data[fld.name]
        fld_type = hints.get(fld.name)
        if (
            fld_type is not None
            and isinstance(fld_type, type)
            and dataclasses.is_dataclass(fld_type)
        ):
            filtered[fld.name] = (
                _nested_from_dict(fld_type, value) if isinstance(value, dict) else value
            )
        else:
            filtered[fld.name] = value
    return cls(**filtered)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def settings_path(settings: DistroPluginSettings) -> Path:
    """Return the distro settings file path."""
    return Path(settings.distro_home) / _SETTINGS_FILENAME


def load(settings: DistroPluginSettings) -> DistroSettings:
    """Load distro settings from disk, returning defaults for missing values."""
    path = settings_path(settings)
    if not path.exists():
        return DistroSettings()

    try:
        raw = yaml.safe_load(path.read_text())
        if not isinstance(raw, dict):
            return DistroSettings()
        return _nested_from_dict(DistroSettings, raw)
    except (OSError, yaml.YAMLError):
        logger.warning("Failed to read distro settings from %s", path, exc_info=True)
        return DistroSettings()


def save(settings: DistroPluginSettings, distro: DistroSettings) -> Path:
    """Persist distro settings to disk atomically. Returns the file path.

    Writes to a temporary file in the same directory then renames it into
    place.  On POSIX the rename is atomic, so a crash mid-write can never
    leave a truncated settings file.

    Preserves any keys in the existing file that are not owned by
    ``DistroSettings`` (e.g. the ``config.providers`` section written by
    ``providers.py``).
    """
    path = settings_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Read existing file content so we can preserve keys not owned by DistroSettings
    # (e.g. config.providers written by providers.py).
    existing_data: dict[str, Any] = {}
    if path.exists():
        try:
            existing_data = yaml.safe_load(path.read_text()) or {}
        except (yaml.YAMLError, OSError):
            existing_data = {}

    # Merge: DistroSettings fields overwrite, everything else is preserved.
    existing_data.update(asdict(distro))

    content = yaml.dump(existing_data, default_flow_style=False, sort_keys=False)

    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".settings-", suffix=".yaml.tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return path


def update(
    settings: DistroPluginSettings,
    section: str | None = None,
    **kwargs: Any,
) -> DistroSettings:
    """Load, update one or more fields, save, and return the updated settings.

    If *section* is given (e.g. ``"slack"``), kwargs are applied to that
    nested dataclass.  Otherwise they are applied to the root.

    The entire load/modify/save cycle is protected by ``_settings_lock``
    so concurrent callers cannot clobber each other's writes.
    """
    with _settings_lock:
        distro = load(settings)
        if section is not None:
            nested = getattr(distro, section)
            for key, value in kwargs.items():
                if hasattr(nested, key):
                    setattr(nested, key, value)
        else:
            for key, value in kwargs.items():
                if hasattr(distro, key):
                    setattr(distro, key, value)
        save(settings, distro)
        return distro
