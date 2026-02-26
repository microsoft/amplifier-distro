"""Server Root Landing Page Tests

These tests validate:
1. GET / redirects to install wizard when unconfigured
2. GET / serves a landing page when configured (ready phase)
3. HTML pages contain expected elements (title, Amplifier branding)
"""

from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient

from amplifier_distro.server.app import DistroServer


def _make_client() -> TestClient:
    """Create a test client with apps discovered."""
    server = DistroServer()
    builtin_apps = (
        Path(__file__).parent.parent / "src" / "amplifier_distro" / "server" / "apps"
    )
    server.discover_apps(builtin_apps)
    return TestClient(server.app)


class TestRootLandingPage:
    """Verify GET / serves a landing page when configured, redirects when not.

    The root URL is the first thing a user hits.
    When unconfigured, it redirects to the install wizard.
    When ready, it serves an HTML landing page with app links.
    """

    def test_root_returns_200_when_ready(self):
        with patch(
            "amplifier_distro.server.apps.settings.compute_phase",
            return_value="ready",
        ):
            client = _make_client()
            response = client.get("/")
            assert response.status_code == 200

    def test_root_returns_html_when_ready(self):
        with patch(
            "amplifier_distro.server.apps.settings.compute_phase",
            return_value="ready",
        ):
            client = _make_client()
            response = client.get("/")
            content_type = response.headers.get("content-type", "")
            assert "text/html" in content_type

    def test_root_contains_amplifier_when_ready(self):
        with patch(
            "amplifier_distro.server.apps.settings.compute_phase",
            return_value="ready",
        ):
            client = _make_client()
            response = client.get("/")
            assert "Amplifier" in response.text

    def test_root_redirects_to_wizard_when_unconfigured(self):
        """When unconfigured, GET / redirects to /apps/install-wizard/."""
        with patch(
            "amplifier_distro.server.apps.settings.compute_phase",
            return_value="unconfigured",
        ):
            client = _make_client()
            response = client.get("/", follow_redirects=False)
            assert response.status_code == 307
            assert response.headers["location"] == "/apps/install-wizard/"

    def test_root_serves_landing_when_ready(self):
        """When configured (ready phase), GET / serves the landing page."""
        with patch(
            "amplifier_distro.server.apps.settings.compute_phase",
            return_value="ready",
        ):
            client = _make_client()
            response = client.get("/")
            assert response.status_code == 200
            assert "/apps/chat/" in response.text
