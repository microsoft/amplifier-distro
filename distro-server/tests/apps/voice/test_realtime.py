"""Tests for realtime.py — OpenAI Realtime API client."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from amplifier_distro.server.apps.voice.realtime import (
    VoiceConfig,
    create_client_secret,
)


def _make_config() -> VoiceConfig:
    return VoiceConfig(
        model="gpt-4o-realtime-preview",
        voice="ash",
        instructions="You are a helpful assistant.",
        tools=[],
        openai_api_key="test-key",
    )


def test_session_payload_includes_input_audio_transcription(monkeypatch):
    """create_client_secret must include input_audio_transcription in session payload.

    RED: fails before the field is added to the payload dict.
    GREEN: passes after adding {"model": "whisper-1"} to the session dict.
    """
    captured = {}

    mock_response = MagicMock()
    mock_response.is_error = False
    mock_response.json.return_value = {"value": "ek_test_token"}

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, *, json, headers, **kwargs):
            captured.update(json)
            return mock_response

    import amplifier_distro.server.apps.voice.realtime as realtime_module

    monkeypatch.setattr(realtime_module, "httpx", _make_httpx_stub(FakeAsyncClient))

    token = asyncio.run(create_client_secret(_make_config()))

    assert token == "ek_test_token"  # noqa: S105
    session = captured.get("session", {})
    assert "input_audio_transcription" in session, (
        f"input_audio_transcription missing from session payload. Got: {session}"
    )
    assert session["input_audio_transcription"] == {"model": "whisper-1"}, (
        f"Expected whisper-1, got: {session['input_audio_transcription']}"
    )


def _make_httpx_stub(async_client_cls):
    """Build a minimal httpx stub that replaces just AsyncClient."""
    import types

    stub = types.SimpleNamespace()
    stub.AsyncClient = async_client_cls
    stub.HTTPException = Exception  # not used in this path
    return stub
