"""Tests for VoiceEventTranslator - OpenAI data channel events to browser wire protocol.

Translates OpenAI Realtime API event types to browser-facing wire protocol messages.
"""

from __future__ import annotations

from amplifier_distro.server.apps.voice.translator import VoiceEventTranslator


class TestVoiceEventTranslator:
    """Tests for VoiceEventTranslator.translate()."""

    def setup_method(self) -> None:
        self.translator = VoiceEventTranslator()

    # ------------------------------------------------------------------ #
    #  Speech detection events                                             #
    # ------------------------------------------------------------------ #

    def test_speech_started(self) -> None:
        """input_audio_buffer.speech_started -> user_turn_start."""
        result = self.translator.translate("input_audio_buffer.speech_started", {})
        assert result == {"type": "user_turn_start"}

    def test_speech_stopped(self) -> None:
        """input_audio_buffer.speech_stopped -> user_turn_end."""
        result = self.translator.translate("input_audio_buffer.speech_stopped", {})
        assert result == {"type": "user_turn_end"}

    # ------------------------------------------------------------------ #
    #  Transcription events                                                #
    # ------------------------------------------------------------------ #

    def test_transcription_completed(self) -> None:
        """conversation.item.input_audio_transcription.completed -> user_transcript."""
        data = {
            "transcript": "Hello world",
            "item_id": "item_abc123",
        }
        result = self.translator.translate(
            "conversation.item.input_audio_transcription.completed", data
        )
        assert result == {
            "type": "user_transcript",
            "transcript": "Hello world",
            "item_id": "item_abc123",
        }

    def test_audio_transcript_delta(self) -> None:
        """response.audio_transcript.delta -> assistant_delta with delta text."""
        data = {"delta": "Hello"}
        result = self.translator.translate("response.audio_transcript.delta", data)
        assert result == {"type": "assistant_delta", "delta": "Hello"}

    def test_audio_transcript_done(self) -> None:
        """response.audio_transcript.done -> assistant_done with transcript."""
        data = {"transcript": "Hello, how can I help you today?"}
        result = self.translator.translate("response.audio_transcript.done", data)
        assert result == {
            "type": "assistant_done",
            "transcript": "Hello, how can I help you today?",
        }

    # ------------------------------------------------------------------ #
    #  Function call (tool) events                                         #
    # ------------------------------------------------------------------ #

    def test_output_item_added_function_call(self) -> None:
        """response.output_item.added with function_call item -> tool_call."""
        data = {
            "item": {
                "type": "function_call",
                "name": "run_command",
                "call_id": "call_xyz789",
                "arguments": '{"command": "ls"}',
            }
        }
        result = self.translator.translate("response.output_item.added", data)
        assert result == {
            "type": "tool_call",
            "name": "run_command",
            "call_id": "call_xyz789",
            "arguments": '{"command": "ls"}',
        }

    def test_output_item_added_non_function(self) -> None:
        """response.output_item.added with non-function item -> None."""
        data = {
            "item": {
                "type": "message",
                "content": "some content",
            }
        }
        result = self.translator.translate("response.output_item.added", data)
        assert result is None

    # ------------------------------------------------------------------ #
    #  Response lifecycle events                                           #
    # ------------------------------------------------------------------ #

    def test_response_done(self) -> None:
        """response.done -> response_done."""
        result = self.translator.translate("response.done", {"response": {}})
        assert result == {"type": "response_done"}

    # ------------------------------------------------------------------ #
    #  Session events                                                      #
    # ------------------------------------------------------------------ #

    def test_session_created(self) -> None:
        """session.created -> session_ready with session_id from data.session.id."""
        data = {"session": {"id": "sess_abc123", "model": "gpt-4o-realtime-preview"}}
        result = self.translator.translate("session.created", data)
        assert result == {"type": "session_ready", "session_id": "sess_abc123"}

    # ------------------------------------------------------------------ #
    #  Unknown events                                                      #
    # ------------------------------------------------------------------ #

    def test_unknown_event_returns_none(self) -> None:
        """Unknown event types -> None (audio buffer, ICE events handled natively)."""
        result = self.translator.translate(
            "input_audio_buffer.append", {"audio": "base64..."}
        )
        assert result is None
