"""Stub mode for UI iteration without real services.

Provides canned responses for endpoints that would normally call
external services (gh CLI, OpenAI, Anthropic, etc.), and optionally
uses a temp AMPLIFIER_HOME so writes don't touch ~/.amplifier/.

Usage:
    amp-distro serve --stub             # stub mode with temp home
    amp-distro serve --stub --reload    # + hot-reload for HTML editing

Stub mode implies --dev (MockBackend for sessions).
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Module-level flag
_stub_mode: bool = False
_stub_home: Path | None = None


def is_stub_mode() -> bool:
    """Check if stub mode is active."""
    return _stub_mode


def get_stub_home() -> Path | None:
    """Get the temp AMPLIFIER_HOME path, if stub mode is active."""
    return _stub_home


def activate_stub_mode() -> Path:
    """Activate stub mode: create temp home, set env vars, return temp path.

    This should be called BEFORE init_services() and create_server().
    """
    global _stub_mode, _stub_home

    _stub_mode = True

    # Create isolated temp home so writes don't touch real ~/.amplifier/
    _stub_home = Path(tempfile.mkdtemp(prefix="amplifier-stub-"))
    os.environ["AMPLIFIER_HOME"] = str(_stub_home)

    # Seed the temp home with minimal config so the UI has something to show
    _seed_stub_config(_stub_home)

    logger.info("Stub mode activated: AMPLIFIER_HOME=%s", _stub_home)
    return _stub_home


def _seed_stub_config(home: Path) -> None:
    """Create minimal config files in the temp home for UI rendering."""
    import yaml

    from amplifier_distro import conventions, distro_settings

    home.mkdir(parents=True, exist_ok=True)

    # Distro settings (replaces old distro.yaml)
    settings = distro_settings.DistroSettings(
        workspace_root="~/dev",
        identity=distro_settings.IdentitySettings(
            github_handle="stub-user",
            git_email="stub@example.com",
        ),
        voice=distro_settings.VoiceSettings(
            voice="ash",
            model="gpt-4o-realtime-preview",
        ),
    )
    distro_home = Path(conventions.DISTRO_HOME).expanduser()
    distro_home.mkdir(parents=True, exist_ok=True)
    distro_settings.save(settings)

    # keys.env -- fake keys so the UI shows "configured" states
    keys = {
        "ANTHROPIC_API_KEY": "sk-ant-stub-key-for-ui-testing-not-real",
        "OPENAI_API_KEY": "sk-stub-key-for-ui-testing-not-real",
    }
    keys_path = home / "keys.env"
    lines = [f'{k}="{v}"' for k, v in keys.items()]
    keys_path.write_text("\n".join(lines) + "\n")
    keys_path.chmod(0o600)

    # Set fake keys in env so provider detection works
    for k, v in keys.items():
        os.environ.setdefault(k, v)

    # settings.yaml -- marks system as "configured" so we get the ready phase
    amp_settings = {
        "bundle": {
            "active": "distro",
            "added": {"distro": str(home / "bundles" / "distro.yaml")},
        }
    }
    (home / "settings.yaml").write_text(
        yaml.dump(amp_settings, default_flow_style=False, sort_keys=False)
    )

    # bundles/ dir with a minimal bundle so status endpoint works
    bundles_dir = home / "bundles"
    bundles_dir.mkdir(exist_ok=True)
    (bundles_dir / "distro.yaml").write_text(
        "# Stub bundle for UI testing\nname: distro\nversion: 0.1.0\n"
    )

    # memory/ dir (empty, but exists so memory service doesn't error)
    (home / "memory").mkdir(exist_ok=True)

    logger.info("Seeded stub config at %s", home)


# ---------------------------------------------------------------------------
# Canned responses for stubbed endpoints
# ---------------------------------------------------------------------------


def stub_detect_environment() -> dict[str, Any]:
    """Canned response for /apps/install-wizard/detect."""
    return {
        "github": {"handle": "stub-user", "configured": True},
        "git": {"installed": True, "configured": True},
        "api_keys": {"anthropic": True, "openai": True},
        "amplifier_cli": {"installed": True},
        "overlay_bundle": {
            "bundle": {"name": "distro", "version": "0.1.0"},
            "includes": [
                {"bundle": "git+https://github.com/microsoft/amplifier-distro@main#subdirectory=bundle"},
                {
                    "bundle": "git+https://github.com/microsoft/amplifier-foundation@main#subdirectory=providers/anthropic-sonnet.yaml"
                },
                {
                    "bundle": "git+https://github.com/microsoft/amplifier-bundle-recipes@main"
                },
            ],
        },
        "workspace_candidates": ["~/dev", "~/dev/ANext"],
    }


def stub_preflight_status() -> dict[str, Any]:
    """Canned response for /api/status (preflight checks)."""
    return {
        "passed": True,
        "checks": [
            {
                "name": "distro.yaml exists",
                "passed": True,
                "message": "Configuration found (stub mode)",
                "severity": "error",
            },
            {
                "name": "GitHub CLI authenticated",
                "passed": True,
                "message": "Authenticated as stub-user (stub mode)",
                "severity": "error",
            },
            {
                "name": "Identity configured",
                "passed": True,
                "message": "github_handle: stub-user (stub mode)",
                "severity": "error",
            },
            {
                "name": "ANTHROPIC_API_KEY",
                "passed": True,
                "message": "Key available (stub mode)",
                "severity": "warning",
            },
            {
                "name": "OPENAI_API_KEY",
                "passed": True,
                "message": "Key available (stub mode)",
                "severity": "warning",
            },
            {
                "name": "Workspace root exists",
                "passed": True,
                "message": "~/dev (stub mode)",
                "severity": "error",
            },
            {
                "name": "Memory store directory",
                "passed": True,
                "message": "Memory directory exists (stub mode)",
                "severity": "warning",
            },
            {
                "name": "Amplifier CLI installed",
                "passed": True,
                "message": "amplifier found in PATH (stub mode)",
                "severity": "error",
            },
        ],
    }


def stub_test_provider(provider: str) -> dict[str, Any]:
    """Canned response for /api/test-provider."""
    return {
        "provider": provider,
        "ok": True,
        "status_code": 200,
        "stub": True,
        "message": f"{provider} connection simulated (stub mode)",
    }


def stub_voice_session() -> dict[str, Any]:
    """Canned response for /apps/voice/session."""
    return {
        "id": "sess_stub_01",
        "object": "realtime.session",
        "model": "gpt-4o-realtime-preview",
        "voice": "ash",
        "client_secret": {
            "value": "stub-ephemeral-token-not-real",
            "expires_at": 9999999999,
        },
        "modalities": ["audio", "text"],
        "stub": True,
    }


def stub_voice_client_secret() -> str:
    """Canned ephemeral client secret token for GA Realtime API (stub mode)."""
    return "ek_test_stub_token_not_real"


def stub_voice_sdp() -> str:
    """Canned SDP answer for /apps/voice/sdp."""
    return (
        "v=0\r\n"
        "o=- 0 0 IN IP4 0.0.0.0\r\n"
        "s=stub\r\n"
        "t=0 0\r\n"
        "a=group:BUNDLE 0\r\n"
        "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
        "c=IN IP4 0.0.0.0\r\n"
        "a=rtpmap:111 opus/48000/2\r\n"
        "a=setup:active\r\n"
        "a=mid:0\r\n"
    )
