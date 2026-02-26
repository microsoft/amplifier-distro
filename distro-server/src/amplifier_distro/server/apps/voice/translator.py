"""VoiceEventTranslator - OpenAI data channel events to browser wire protocol.

Pure transformation logic. No I/O. No state beyond what's needed for the turn.
"""

from __future__ import annotations


class VoiceEventTranslator:
    """Translate OpenAI Realtime API events to browser wire protocol messages."""

    def translate(self, event_type: str, data: dict) -> dict | None:
        """Translate an OpenAI data channel event to the browser wire protocol.

        Args:
            event_type: The OpenAI event type string.
            data: The event payload dict.

        Returns:
            A browser wire protocol dict, or None if the event is handled
            natively (audio buffer, ICE events, etc.).
        """
        match event_type:
            case "input_audio_buffer.speech_started":
                return {"type": "user_turn_start"}

            case "input_audio_buffer.speech_stopped":
                return {"type": "user_turn_end"}

            case "conversation.item.input_audio_transcription.completed":
                return {
                    "type": "user_transcript",
                    "transcript": data.get("transcript"),
                    "item_id": data.get("item_id"),
                }

            case "response.audio_transcript.delta":
                return {"type": "assistant_delta", "delta": data.get("delta")}

            case "response.audio_transcript.done":
                return {"type": "assistant_done", "transcript": data.get("transcript")}

            case "response.output_item.added":
                item = data.get("item", {})
                if item.get("type") == "function_call":
                    return {
                        "type": "tool_call",
                        "name": item.get("name"),
                        "call_id": item.get("call_id"),
                        "arguments": item.get("arguments"),
                    }
                return None

            case "response.done":
                return {"type": "response_done"}

            case "response.created":
                return {"type": "response_created"}

            case "session.created":
                session_id = data.get("session", {}).get("id")
                return {"type": "session_ready", "session_id": session_id}

            case "error":
                return {
                    "type": "realtime_error",
                    "code": data.get("code"),
                    "message": data.get("message"),
                }

            case _:
                return None
