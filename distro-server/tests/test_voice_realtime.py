"""Tests for voice/realtime.py - GA API client for OpenAI Realtime.

Tests the two GA API functions:
  - create_client_secret: POST to /client_secrets, returns ephemeral token
  - exchange_sdp: POST SDP offer to /calls, returns SDP answer

Exit criteria (6 tests):
  TestCreateClientSecret:
    - returns token value string
    - posts to correct endpoint (CLIENT_SECRETS_ENDPOINT)
    - payload includes session type 'realtime'
    - raises HTTPException on 401
  TestExchangeSdp:
    - returns SDP answer string
    - uses ephemeral token as Bearer auth
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException


class TestCreateClientSecret:
    """Tests for create_client_secret()."""

    @pytest.mark.asyncio
    async def test_returns_token_value_string(self) -> None:
        """create_client_secret returns the ephemeral token string (data['value'])."""
        from amplifier_distro.server.apps.voice.realtime import (
            VoiceConfig,
            create_client_secret,
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.is_error = False
        mock_response.json.return_value = {"value": "ek_abc123"}

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        config = VoiceConfig(
            model="gpt-4o-realtime-preview",
            voice="ash",
            instructions="You are helpful.",
            openai_api_key="sk-test",
        )

        with patch(
            "amplifier_distro.server.apps.voice.realtime.httpx.AsyncClient",
            return_value=mock_client,
        ):
            result = await create_client_secret(config)

        assert result == "ek_abc123"

    @pytest.mark.asyncio
    async def test_posts_to_client_secrets_endpoint(self) -> None:
        """create_client_secret POSTs to CLIENT_SECRETS_ENDPOINT."""
        from amplifier_distro.server.apps.voice.realtime import (
            CLIENT_SECRETS_ENDPOINT,
            VoiceConfig,
            create_client_secret,
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.is_error = False
        mock_response.json.return_value = {"value": "ek_tok"}

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        config = VoiceConfig(
            model="gpt-4o-realtime-preview",
            voice="ash",
            instructions="",
            openai_api_key="sk-test",
        )

        with patch(
            "amplifier_distro.server.apps.voice.realtime.httpx.AsyncClient",
            return_value=mock_client,
        ):
            await create_client_secret(config)

        call_args = mock_client.post.call_args
        assert call_args[0][0] == CLIENT_SECRETS_ENDPOINT

    @pytest.mark.asyncio
    async def test_payload_includes_session_type_realtime(self) -> None:
        """create_client_secret payload has session.type = 'realtime'."""
        from amplifier_distro.server.apps.voice.realtime import (
            VoiceConfig,
            create_client_secret,
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.is_error = False
        mock_response.json.return_value = {"value": "ek_tok"}

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        config = VoiceConfig(
            model="gpt-4o-realtime-preview",
            voice="ash",
            instructions="Be helpful.",
            openai_api_key="sk-test",
        )

        with patch(
            "amplifier_distro.server.apps.voice.realtime.httpx.AsyncClient",
            return_value=mock_client,
        ):
            await create_client_secret(config)

        call_kwargs = mock_client.post.call_args[1]
        payload = call_kwargs["json"]
        assert payload["session"]["type"] == "realtime"

    @pytest.mark.asyncio
    async def test_raises_http_exception_on_401(self) -> None:
        """create_client_secret raises HTTPException(401) when OpenAI returns 401."""
        from amplifier_distro.server.apps.voice.realtime import (
            VoiceConfig,
            create_client_secret,
        )

        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.is_error = True
        mock_response.text = "Unauthorized"

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        config = VoiceConfig(
            model="gpt-4o-realtime-preview",
            voice="ash",
            instructions="",
            openai_api_key="sk-bad",
        )

        with (
            patch(
                "amplifier_distro.server.apps.voice.realtime.httpx.AsyncClient",
                return_value=mock_client,
            ),
            pytest.raises(HTTPException) as exc_info,
        ):
            await create_client_secret(config)

        assert exc_info.value.status_code == 401


class TestExchangeSdp:
    """Tests for exchange_sdp()."""

    @pytest.mark.asyncio
    async def test_returns_sdp_answer_string(self) -> None:
        """exchange_sdp returns the SDP answer text from OpenAI."""
        from amplifier_distro.server.apps.voice.realtime import exchange_sdp

        sdp_answer = "v=0\r\no=- 99999 2 IN IP4 127.0.0.1\r\n"

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.is_error = False
        mock_response.text = sdp_answer

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "amplifier_distro.server.apps.voice.realtime.httpx.AsyncClient",
            return_value=mock_client,
        ):
            result = await exchange_sdp(
                sdp_offer="v=0\r\n",
                ephemeral_token="ek_test_token",
                model="gpt-4o-realtime-preview",
            )

        assert result == sdp_answer

    @pytest.mark.asyncio
    async def test_uses_ephemeral_token_as_bearer_auth(self) -> None:
        """exchange_sdp sends Authorization: Bearer {ephemeral_token}."""
        from amplifier_distro.server.apps.voice.realtime import exchange_sdp

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.is_error = False
        mock_response.text = "v=0\r\n"

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "amplifier_distro.server.apps.voice.realtime.httpx.AsyncClient",
            return_value=mock_client,
        ):
            await exchange_sdp(
                sdp_offer="v=0\r\n",
                ephemeral_token="ek_my_token",
                model="gpt-4o-realtime-preview",
            )

        call_kwargs = mock_client.post.call_args[1]
        headers = call_kwargs["headers"]
        assert headers["Authorization"] == "Bearer ek_my_token"

    @pytest.mark.asyncio
    async def test_sends_model_as_query_param(self) -> None:
        """exchange_sdp sends model as a URL query parameter."""
        from amplifier_distro.server.apps.voice.realtime import exchange_sdp

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.is_error = False
        mock_response.text = "v=0\r\n"

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "amplifier_distro.server.apps.voice.realtime.httpx.AsyncClient",
            return_value=mock_client,
        ):
            await exchange_sdp(
                sdp_offer="v=0\r\n",
                ephemeral_token="ek_test_token",
                model="gpt-4o-realtime-preview",
            )

        call_kwargs = mock_client.post.call_args[1]
        assert call_kwargs["params"] == {"model": "gpt-4o-realtime-preview"}

    @pytest.mark.asyncio
    async def test_raises_http_exception_on_error(self) -> None:
        """exchange_sdp raises HTTPException when OpenAI returns a 4xx/5xx response."""
        from amplifier_distro.server.apps.voice.realtime import exchange_sdp

        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.is_error = True
        mock_response.text = "Forbidden"

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch(
                "amplifier_distro.server.apps.voice.realtime.httpx.AsyncClient",
                return_value=mock_client,
            ),
            pytest.raises(HTTPException) as exc_info,
        ):
            await exchange_sdp(
                sdp_offer="v=0\r\n",
                ephemeral_token="ek_test_token",
                model="gpt-4o-realtime-preview",
            )

        assert exc_info.value.status_code == 403
