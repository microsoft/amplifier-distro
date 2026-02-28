"""Amplifier Distro Conventions - Server & Experience Layer

Path constants and naming conventions for the distro experience server.
Core CLI and bundle conventions live in amplifier-foundation's
DIRECTORY_CONTRACT.md -- this file covers only server-specific paths.

Most values are fixed conventions. DISTRO_HOME can be overridden via
the AMPLIFIER_DISTRO_HOME environment variable.
"""

import os

# --- The Root ---
AMPLIFIER_HOME = "~/.amplifier"


# --- Keys & Settings ---
KEYS_FILENAME = "keys.env"
SETTINGS_FILENAME = "settings.yaml"

# --- Distro Home ---
# Override with AMPLIFIER_DISTRO_HOME env var.
DISTRO_HOME = os.environ.get("AMPLIFIER_DISTRO_HOME", "~/.amplifier-distro")

# --- Distro Settings ---
DISTRO_SETTINGS_FILENAME = "settings.yaml"  # distro-layer config (inside DISTRO_HOME)

# --- Local Overlay Bundle ---
# The distro creates a local overlay bundle that includes the distro bundle.
# The wizard/settings apps modify this overlay; the underlying bundle is untouched.
DISTRO_OVERLAY_DIR = f"{DISTRO_HOME}/bundle"

# --- Bundle Cache ---
CACHE_DIR = "cache"  # relative to AMPLIFIER_HOME

# --- Memory Store ---
MEMORY_DIR = "memory"  # relative to AMPLIFIER_HOME
MEMORY_STORE_FILENAME = "memory-store.yaml"
WORK_LOG_FILENAME = "work-log.yaml"

# --- Sessions ---
TRANSCRIPT_FILENAME = "transcript.jsonl"
SESSION_INFO_FILENAME = "session-info.json"
METADATA_FILENAME = "metadata.json"
PROJECTS_DIR = "projects"  # relative to AMPLIFIER_HOME

# --- Server ---
SERVER_DIR = "server"  # relative to AMPLIFIER_HOME
SERVER_SOCKET = "server.sock"
SERVER_PID_FILE = "server.pid"
SERVER_LOG_FILE = "server.log"
SERVER_DEFAULT_PORT = 8400
SLACK_SESSIONS_FILENAME = "slack-sessions.json"
TEAMS_SESSIONS_FILENAME = "teams-sessions.json"

# --- Crash logs ---
CRASH_LOG_FILE = "crash.log"  # relative to SERVER_DIR
WATCHDOG_CRASH_LOG_FILE = "watchdog-crash.log"

# --- Watchdog ---
WATCHDOG_PID_FILE = "watchdog.pid"  # relative to SERVER_DIR
WATCHDOG_LOG_FILE = "watchdog.log"

# --- Platform Service ---
SERVICE_NAME = "amplifier-distro"  # systemd unit name
LAUNCHD_LABEL = "com.amplifier.distro"  # macOS launchd job label

# --- Backup ---
BACKUP_REPO_PATTERN = "{github_handle}/amplifier-backup"
BACKUP_INCLUDE = [
    SETTINGS_FILENAME,
    MEMORY_DIR,
]
BACKUP_EXCLUDE = [
    KEYS_FILENAME,  # Security: never backup keys
    SERVER_DIR,  # Runtime state, not config
]
