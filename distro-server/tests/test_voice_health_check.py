"""Server Health Check Tests — Task 15

Validates that the voice app loads without import errors and the
/apps/voice/api/status endpoint returns the expected shape.

Acceptance criteria (from task spec):
  - Server starts with no ImportError / ModuleNotFoundError / tracebacks
  - GET /apps/voice/api/status → HTTP 200
  - response.status ∈ {'ready', 'unconfigured'}
  - response.model is present and non-empty
  - response.voice is present and non-empty
  - response.api_key_set is a bool
"""

from __future__ import annotations

from starlette.testclient import TestClient


def _make_app():
    """Build the full DistroServer with the voice app registered."""
    from amplifier_distro.server.app import DistroServer
    from amplifier_distro.server.apps.voice import manifest

    server = DistroServer()
    server.register_app(manifest)
    return server.app


# ---------------------------------------------------------------------------
# Import health — no ImportError / ModuleNotFoundError allowed
# ---------------------------------------------------------------------------


class TestVoiceImportHealth:
    """Importing the voice app and its submodules must not raise."""

    def test_voice_module_imports_without_error(self) -> None:
        import amplifier_distro.server.apps.voice  # noqa: F401

    def test_voice_connection_imports_without_error(self) -> None:
        import amplifier_distro.server.apps.voice.connection  # noqa: F401

    def test_voice_realtime_imports_without_error(self) -> None:
        import amplifier_distro.server.apps.voice.realtime  # noqa: F401

    def test_server_app_imports_without_error(self) -> None:
        import amplifier_distro.server.app  # noqa: F401


# ---------------------------------------------------------------------------
# /apps/voice/api/status endpoint health check
# ---------------------------------------------------------------------------


class TestVoiceStatusEndpointHealth:
    """The status endpoint must return the expected shape, confirming the
    voice app loaded correctly end-to-end."""

    def setup_method(self) -> None:
        self.client = TestClient(_make_app(), raise_server_exceptions=False)

    # -- HTTP level ----------------------------------------------------------

    def test_status_endpoint_returns_http_200(self) -> None:
        resp = self.client.get("/apps/voice/api/status")
        assert resp.status_code == 200, (
            f"Expected HTTP 200, got {resp.status_code}: {resp.text}"
        )

    def test_status_endpoint_returns_json(self) -> None:
        resp = self.client.get("/apps/voice/api/status")
        data = resp.json()
        assert isinstance(data, dict), "Response body must be a JSON object"

    # -- Required fields present ---------------------------------------------

    def test_status_field_is_present(self) -> None:
        data = self.client.get("/apps/voice/api/status").json()
        assert "status" in data, "Response must contain 'status' field"

    def test_model_field_is_present(self) -> None:
        data = self.client.get("/apps/voice/api/status").json()
        assert "model" in data, "Response must contain 'model' field"

    def test_voice_field_is_present(self) -> None:
        data = self.client.get("/apps/voice/api/status").json()
        assert "voice" in data, "Response must contain 'voice' field"

    def test_api_key_set_field_is_present(self) -> None:
        data = self.client.get("/apps/voice/api/status").json()
        assert "api_key_set" in data, "Response must contain 'api_key_set' field"

    # -- Field value constraints (acceptance criteria) -----------------------

    def test_status_value_is_ready_or_unconfigured(self) -> None:
        """status must be 'ready' or 'unconfigured', never an error string."""
        data = self.client.get("/apps/voice/api/status").json()
        valid_statuses = {"ready", "unconfigured"}
        assert data["status"] in valid_statuses, (
            f"status must be one of {valid_statuses}, got {data['status']!r}"
        )

    def test_model_field_is_non_empty(self) -> None:
        """model must be a non-empty string (e.g. 'gpt-4o-realtime-preview')."""
        data = self.client.get("/apps/voice/api/status").json()
        assert isinstance(data["model"], str) and data["model"].strip(), (
            f"model must be non-empty, got {data['model']!r}"
        )

    def test_voice_field_is_non_empty(self) -> None:
        """voice must be a non-empty string (e.g. 'ash')."""
        data = self.client.get("/apps/voice/api/status").json()
        assert isinstance(data["voice"], str) and data["voice"].strip(), (
            f"voice must be non-empty, got {data['voice']!r}"
        )

    def test_api_key_set_field_is_bool(self) -> None:
        data = self.client.get("/apps/voice/api/status").json()
        assert isinstance(data["api_key_set"], bool), (
            f"api_key_set must be bool, got {type(data['api_key_set'])}"
        )

    def test_turn_server_is_none(self) -> None:
        """turn_server is not yet used; must be null/None."""
        data = self.client.get("/apps/voice/api/status").json()
        assert data.get("turn_server") is None, (
            f"turn_server must be None, got {data.get('turn_server')!r}"
        )

    def test_status_is_unconfigured_when_no_api_key(self) -> None:
        """Without OPENAI_API_KEY, status must be 'unconfigured' (not an error)."""
        import os

        saved = os.environ.pop("OPENAI_API_KEY", None)
        try:
            data = self.client.get("/apps/voice/api/status").json()
            assert data["status"] == "unconfigured"
            assert data["api_key_set"] is False
        finally:
            if saved is not None:
                os.environ["OPENAI_API_KEY"] = saved
