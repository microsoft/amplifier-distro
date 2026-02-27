"""Regression tests: fresh overlay must NOT contain the stale SESSION_NAMING_URI.

These tests verify that the session-naming hook is not injected into a freshly
created overlay after the fix in overlay.py.  hooks-session-naming is a Python
module package, not a bundle; injecting it via overlay includes caused
'Not a valid bundle' errors on every session.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
import yaml

from amplifier_distro import overlay
from amplifier_distro.features import (
    AMPLIFIER_START_URI,
    PROVIDERS,
    provider_bundle_uri,
)

# The stale URI that must NOT appear in fresh overlays.
# Defined independently here so these tests remain valid even after the
# constant is removed from overlay.py as part of the fix.
_STALE_URI = (
    "git+https://github.com/microsoft/amplifier-foundation@main"
    "#subdirectory=modules/hooks-session-naming"
)

_ANTHROPIC = PROVIDERS["anthropic"]


@pytest.fixture
def overlay_path(tmp_path):
    """Redirect all overlay I/O to a temporary directory.

    Patches both ``overlay.overlay_bundle_path`` and ``overlay.overlay_dir``
    so that no test ever touches ``~/.amplifier-distro``.
    """
    bundle_yaml = tmp_path / "bundle.yaml"
    with (
        patch.object(overlay, "overlay_bundle_path", return_value=bundle_yaml),
        patch.object(overlay, "overlay_dir", return_value=tmp_path),
    ):
        yield bundle_yaml


class TestFreshOverlayDoesNotInjectSessionNaming:
    """A freshly created overlay must not include the session-naming hook."""

    def test_session_naming_uri_absent_from_fresh_overlay(self, overlay_path):
        """Session-naming URI must NOT appear in a fresh overlay."""
        overlay.ensure_overlay(_ANTHROPIC)
        data = yaml.safe_load(overlay_path.read_text()) or {}
        uris = overlay.get_includes(data)
        assert _STALE_URI not in uris, (
            f"Fresh overlay must not include stale session-naming URI: {_STALE_URI!r}"
        )

    def test_fresh_overlay_still_contains_start_uri(self, overlay_path):
        """AMPLIFIER_START_URI must still be present in a fresh overlay."""
        overlay.ensure_overlay(_ANTHROPIC)
        data = yaml.safe_load(overlay_path.read_text()) or {}
        uris = overlay.get_includes(data)
        assert AMPLIFIER_START_URI in uris, (
            f"Fresh overlay must include AMPLIFIER_START_URI: {AMPLIFIER_START_URI!r}"
        )

    def test_fresh_overlay_still_contains_provider_uri(self, overlay_path):
        """The provider bundle URI must still be present in a fresh overlay."""
        overlay.ensure_overlay(_ANTHROPIC)
        data = yaml.safe_load(overlay_path.read_text()) or {}
        uris = overlay.get_includes(data)
        expected = provider_bundle_uri(_ANTHROPIC)
        assert expected in uris, (
            f"Fresh overlay must include provider URI: {expected!r}"
        )
