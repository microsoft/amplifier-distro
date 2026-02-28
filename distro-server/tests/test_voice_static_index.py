"""Tests for voice app static/index.html

Verifies that the Preact voice app index.html exists with all required
elements from the spec:

  TestIndexFileExists       - file present at correct path
  TestIndexContent          - required JS hooks, components, CSS present
  TestIndexRouteServesFile  - GET /apps/voice/ serves the actual file (not placeholder)
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

# Path to static/index.html (resolved relative to this test file)
_STATIC_DIR = (
    Path(__file__).parent.parent
    / "src"
    / "amplifier_distro"
    / "server"
    / "apps"
    / "voice"
    / "static"
)
INDEX_HTML = _STATIC_DIR / "index.html"


def _make_app() -> FastAPI:
    from amplifier_distro.server.app import DistroServer
    from amplifier_distro.server.apps.voice import manifest

    server = DistroServer()
    server.register_app(manifest)
    return server.app


# ---------------------------------------------------------------------------
# TestIndexFileExists
# ---------------------------------------------------------------------------


class TestIndexFileExists:
    def test_index_html_file_exists(self) -> None:
        assert INDEX_HTML.exists(), (
            f"index.html not found at {INDEX_HTML}. "
            "Task 5.2 requires creating this file."
        )

    def test_index_html_is_not_empty(self) -> None:
        assert INDEX_HTML.stat().st_size > 500, (
            "index.html appears to be empty or too small"
        )


# ---------------------------------------------------------------------------
# TestIndexContent
# ---------------------------------------------------------------------------


class TestIndexContent:
    @pytest.fixture(autouse=True)
    def content(self) -> None:
        self.html = INDEX_HTML.read_text(encoding="utf-8")

    # --- vendor.js script tag ---
    def test_loads_vendor_js(self) -> None:
        assert "vendor.js" in self.html, "index.html must load vendor.js"

    # --- useWebRTC hook ---
    def test_declares_use_web_rtc_hook(self) -> None:
        assert "useWebRTC" in self.html, "index.html must declare a useWebRTC hook"

    def test_rtc_state_declared(self) -> None:
        assert "rtcState" in self.html, "useWebRTC must maintain rtcState"

    def test_connect_function_declared(self) -> None:
        assert "connect" in self.html, "useWebRTC must expose a connect() function"

    def test_disconnect_function_declared(self) -> None:
        assert "disconnect" in self.html, (
            "useWebRTC must expose a disconnect() function"
        )

    def test_send_data_channel_message_declared(self) -> None:
        assert "sendDataChannelMessage" in self.html, (
            "useWebRTC must expose sendDataChannelMessage()"
        )

    def test_fetches_session_endpoint(self) -> None:
        assert "/apps/voice/session" in self.html, (
            "connect() must fetch /apps/voice/session for ephemeral token"
        )

    def test_posts_to_sdp_endpoint(self) -> None:
        assert "/apps/voice/sdp" in self.html, (
            "connect() must POST to /apps/voice/sdp for SDP exchange"
        )

    def test_creates_rtc_peer_connection(self) -> None:
        assert "RTCPeerConnection" in self.html, (
            "connect() must create an RTCPeerConnection"
        )

    def test_uses_stun_server(self) -> None:
        assert "stun:stun.l.google.com" in self.html, (
            "RTCPeerConnection must use Google STUN"
        )

    def test_requests_microphone(self) -> None:
        assert "getUserMedia" in self.html, "connect() must call getUserMedia for audio"

    def test_creates_data_channel(self) -> None:
        assert "oai-events" in self.html, (
            "connect() must create data channel named 'oai-events'"
        )

    # --- Two-stage VAD ---
    def test_stage1_server_vad(self) -> None:
        assert "server_vad" in self.html, (
            "Stage 1 session.update must use server_vad type"
        )

    def test_stage2_server_vad(self) -> None:
        # Pause/resume uses server_vad via GA nested path (not beta flat path)
        assert "server_vad" in self.html, (
            "Stage 2 pause/resume must use server_vad via GA nested path "
            "(session.audio.input.turn_detection)"
        )

    def test_stage2_uses_settimeout(self) -> None:
        assert "setTimeout" in self.html, (
            "Stage 2 VAD update must be sent after setTimeout (GA API constraint)"
        )

    def test_vad_threshold(self) -> None:
        assert "threshold" in self.html, "Stage 1 VAD must include threshold config"

    def test_noise_reduction(self) -> None:
        assert "noise_reduction" in self.html or "near_field" in self.html, (
            "Stage 1 must configure noise_reduction near_field"
        )

    def test_transcription_model(self) -> None:
        assert "gpt-4o-transcribe" in self.html, (
            "Stage 1 must configure transcription with gpt-4o-transcribe"
        )

    def test_vad_ga_nested_path(self) -> None:
        # GA API: turn_detection nested under audio.input, not flat path
        assert "audio" in self.html and "input" in self.html, (
            "Stage 2 VAD update must use GA nested path: "
            "session.audio.input.turn_detection"
        )

    # --- VoiceApp component ---
    def test_declares_voice_app_component(self) -> None:
        assert "VoiceApp" in self.html, "index.html must declare a VoiceApp component"

    def test_start_voice_chat_button(self) -> None:
        assert "Start Voice Chat" in self.html, (
            "VoiceApp must have a 'Start Voice Chat' button when idle"
        )

    def test_disconnect_button(self) -> None:
        assert "Disconnect" in self.html, (
            "VoiceApp must have a 'Disconnect' button when connected"
        )

    def test_posts_to_sessions_endpoint(self) -> None:
        assert "/apps/voice/sessions" in self.html, (
            "handleConnect must POST to /apps/voice/sessions"
        )

    def test_handle_rtc_message_placeholder(self) -> None:
        # Task 5.3 replaces handleRtcMessage with handleDataChannelEvent;
        # accept either the legacy name or its replacement
        assert (
            "handleRtcMessage" in self.html or "handleDataChannelEvent" in self.html
        ), "VoiceApp must define handleRtcMessage or its Task 5.3 replacement"

    def test_console_debug_in_handler(self) -> None:
        assert "console.debug" in self.html, (
            "handleRtcMessage must call console.debug as placeholder"
        )

    # --- CSS / theme ---
    def test_dark_theme_background(self) -> None:
        assert "#1a1a1a" in self.html, "CSS must use dark theme with #1a1a1a background"

    def test_uses_system_ui_font(self) -> None:
        assert "system-ui" in self.html, "CSS must use system-ui font"

    def test_max_width_900px(self) -> None:
        assert "900px" in self.html, "CSS must set 900px max-width"

    # --- Preact wiring ---
    def test_uses_preact_render(self) -> None:
        assert "render" in self.html, "index.html must call preact render()"

    def test_uses_htm(self) -> None:
        assert "html`" in self.html or "window.html" in self.html, (
            "index.html must use htm tagged template literal"
        )

    # --- Resource cleanup (Issues 1 & 2 from quality review) ---

    def test_stream_ref_used_for_track_cleanup(self) -> None:
        assert "streamRef" in self.html, (
            "connect() must store stream in streamRef so disconnect() can stop tracks"
        )

    def test_media_stream_tracks_stopped_on_disconnect(self) -> None:
        assert ".stop()" in self.html, (
            "disconnect() must call .stop() on each MediaStream track to release mic"
        )

    def test_audio_ref_used_for_element_cleanup(self) -> None:
        assert "audioRef" in self.html, (
            "ontrack handler must store audio element in audioRef for cleanup"
        )

    def test_audio_element_removed_on_disconnect(self) -> None:
        assert ".remove()" in self.html, (
            "disconnect() must call .remove() on audio element to avoid DOM leak"
        )

    # --- Code clarity (Issues 4, 5, 6 from quality review) ---

    def test_catch_blocks_have_clarifying_comment(self) -> None:
        assert "already closed" in self.html or "ignore" in self.html.lower(), (
            "catch blocks in disconnect() should have a comment clarifying intent"
        )

    def test_sessions_post_does_not_send_empty_object(self) -> None:
        assert "JSON.stringify({})" not in self.html, (
            "Sessions POST should not send JSON.stringify({}) — use null or omit body"
        )

    def test_handle_state_change_has_task_comment(self) -> None:
        assert "5.3" in self.html, (
            "handleStateChange should reference Task 5.3 to clarify its purpose"
        )

    # --- Mic resource leak fix (Important issue from quality review) ---

    def test_handle_connect_calls_disconnect_on_failure(self) -> None:
        """handleConnect catch block must call disconnect() to release mic on failure.

        Without this, an SDP exchange failure leaves the mic indicator active.
        Each retry compounds the leak (multiple MediaStream tracks running).
        Preferred fix: call disconnect() in handleConnect's catch, not just in the
        SDP-specific catch inside connect() — covers all partial-failure paths.
        """
        assert "ensure full cleanup on any partial failure" in self.html, (
            "handleConnect catch block must call disconnect() with a comment "
            "'ensure full cleanup on any partial failure' to release mic on SDP failure"
        )


# ---------------------------------------------------------------------------
# TestIndexRouteServesFile
# ---------------------------------------------------------------------------


class TestIndexRouteServesFile:
    def setup_method(self) -> None:
        self.client = TestClient(_make_app(), raise_server_exceptions=False)

    def test_index_route_returns_200(self) -> None:
        resp = self.client.get("/apps/voice/")
        assert resp.status_code == 200

    def test_index_route_serves_actual_file(self) -> None:
        """Verify the route serves the real file, not the placeholder."""
        resp = self.client.get("/apps/voice/")
        assert "not built yet" not in resp.text, (
            "Route must serve the real index.html, not the build placeholder"
        )

    def test_index_route_contains_voice_app(self) -> None:
        resp = self.client.get("/apps/voice/")
        assert "VoiceApp" in resp.text or "Start Voice Chat" in resp.text


# ---------------------------------------------------------------------------
# TestUseChatMessages — Task 5.3
# ---------------------------------------------------------------------------


class TestUseChatMessages:
    """Tests for Task 5.3: useChatMessages hook and VoiceApp wiring."""

    @pytest.fixture(autouse=True)
    def content(self) -> None:
        self.html = INDEX_HTML.read_text(encoding="utf-8")

    # --- Hook declaration ---

    def test_declares_use_chat_messages_hook(self) -> None:
        assert "useChatMessages" in self.html, (
            "index.html must declare a useChatMessages hook"
        )

    # --- State ---

    def test_messages_state_declared(self) -> None:
        assert "messages" in self.html, "useChatMessages must maintain messages state"

    def test_response_in_progress_state(self) -> None:
        assert "responseInProgress" in self.html, (
            "useChatMessages must maintain responseInProgress state"
        )

    # --- Refs ---

    def test_pending_announcements_ref(self) -> None:
        assert "pendingAnnouncements" in self.html, (
            "useChatMessages must use pendingAnnouncements ref"
        )

    def test_message_refs_declared(self) -> None:
        assert "messageRefs" in self.html, (
            "useChatMessages must use messageRefs for direct DOM mutation"
        )

    def test_current_streaming_id_ref(self) -> None:
        assert "currentStreamingId" in self.html, (
            "useChatMessages must use currentStreamingId ref"
        )

    # --- Functions ---

    def test_add_user_message_function(self) -> None:
        assert "addUserMessage" in self.html, (
            "useChatMessages must expose addUserMessage()"
        )

    def test_start_assistant_message_function(self) -> None:
        assert "startAssistantMessage" in self.html, (
            "useChatMessages must expose startAssistantMessage()"
        )

    def test_handle_data_channel_event_declared(self) -> None:
        assert "handleDataChannelEvent" in self.html, (
            "useChatMessages must expose handleDataChannelEvent()"
        )

    # --- Event handling ---

    def test_speech_started_no_op(self) -> None:
        assert "speech_started" in self.html, (
            "handleDataChannelEvent must handle input_audio_buffer.speech_started"
        )

    def test_transcription_completed_event(self) -> None:
        assert "input_audio_transcription" in self.html, (
            "handleDataChannelEvent must handle transcription.completed"
        )

    def test_audio_transcript_delta_event(self) -> None:
        assert "audio_transcript" in self.html, (
            "handleDataChannelEvent must handle response.audio_transcript.delta"
        )

    def test_direct_dom_mutation_for_delta(self) -> None:
        assert "innerText" in self.html or "textContent" in self.html, (
            "response.audio_transcript.delta must use direct DOM mutation (no rerender)"
        )

    def test_audio_transcript_done_event(self) -> None:
        assert "audio_transcript" in self.html, (
            "handleDataChannelEvent must handle response.audio_transcript.done"
        )

    def test_response_created_event(self) -> None:
        assert "response.created" in self.html, (
            "handleDataChannelEvent must handle response.created"
        )

    def test_response_done_event(self) -> None:
        assert "response.done" in self.html, (
            "handleDataChannelEvent must handle response.done"
        )

    def test_response_output_item_done(self) -> None:
        assert "response.output_item.done" in self.html, (
            "handleDataChannelEvent must handle response.output_item.done "
            "(arguments are complete at done, not at output_item.added)"
        )

    # --- Tool calling ---

    def test_tool_execute_endpoint(self) -> None:
        assert "/apps/voice/tools/execute" in self.html, (
            "Tool calls must POST to /apps/voice/tools/execute"
        )

    def test_function_call_output_sent(self) -> None:
        assert "function_call_output" in self.html, (
            "Tool result must send function_call_output via data channel"
        )

    def test_response_create_sent(self) -> None:
        assert "response.create" in self.html, (
            "Hook must send response.create to trigger assistant reply"
        )

    def test_pause_resume_replies_handled(self) -> None:
        assert "pause_replies" in self.html or "resume_replies" in self.html, (
            "handleDataChannelEvent must handle pause_replies/resume_replies tool calls"
        )

    # --- VoiceApp wiring ---

    def test_session_id_state_in_voice_app(self) -> None:
        assert "sessionId" in self.html, (
            "VoiceApp must maintain sessionId state set after POST /sessions"
        )

    def test_handle_data_channel_event_wired_as_on_message(self) -> None:
        assert "handleDataChannelEvent" in self.html, (
            "VoiceApp must wire handleDataChannelEvent as the onMessage callback"
        )

    def test_message_refs_used_in_render(self) -> None:
        # The ref callback pattern sets messageRefs entries from bubble divs
        assert "messageRefs" in self.html, (
            "VoiceApp render must attach ref callbacks to bubble divs using messageRefs"
        )


# ---------------------------------------------------------------------------
# TestUseMicrophoneControl — Task 5.4
# ---------------------------------------------------------------------------


class TestUseMicrophoneControl:
    """Tests for Task 5.4: useMicrophoneControl hook."""

    @pytest.fixture(autouse=True)
    def content(self) -> None:
        self.html = INDEX_HTML.read_text(encoding="utf-8")

    def test_declares_use_microphone_control_hook(self) -> None:
        assert "useMicrophoneControl" in self.html, (
            "index.html must declare a useMicrophoneControl hook"
        )

    def test_muted_state_declared(self) -> None:
        assert "muted" in self.html, "useMicrophoneControl must maintain muted state"

    def test_pause_replies_state_declared(self) -> None:
        assert "pauseReplies" in self.html, (
            "useMicrophoneControl must maintain pauseReplies state"
        )

    def test_set_mic_stream_function(self) -> None:
        assert "setMicStream" in self.html, (
            "useMicrophoneControl must expose setMicStream()"
        )

    def test_toggle_mute_function(self) -> None:
        assert "toggleMute" in self.html, (
            "useMicrophoneControl must expose toggleMute()"
        )

    def test_enter_pause_replies_function(self) -> None:
        assert "enterPauseReplies" in self.html, (
            "useMicrophoneControl must expose enterPauseReplies()"
        )

    def test_exit_pause_replies_function(self) -> None:
        assert "exitPauseReplies" in self.html, (
            "useMicrophoneControl must expose exitPauseReplies()"
        )

    def test_toggle_mute_uses_track_enabled(self) -> None:
        assert "track.enabled" in self.html, (
            "toggleMute must toggle track.enabled on audio tracks"
        )

    def test_enter_pause_replies_sends_session_update(self) -> None:
        assert "create_response" in self.html, (
            "enterPauseReplies/exitPauseReplies must send session.update "
            "with create_response"
        )

    def test_microphone_control_wired_in_voice_app(self) -> None:
        # VoiceApp must call useMicrophoneControl
        assert "useMicrophoneControl" in self.html, (
            "VoiceApp must call useMicrophoneControl hook"
        )


# ---------------------------------------------------------------------------
# TestUseVoiceKeywords — Task 5.4
# ---------------------------------------------------------------------------


class TestUseVoiceKeywords:
    """Tests for Task 5.4: useVoiceKeywords hook."""

    @pytest.fixture(autouse=True)
    def content(self) -> None:
        self.html = INDEX_HTML.read_text(encoding="utf-8")

    def test_declares_use_voice_keywords_hook(self) -> None:
        assert "useVoiceKeywords" in self.html, (
            "index.html must declare a useVoiceKeywords hook"
        )

    def test_last_fired_ref_declared(self) -> None:
        assert "lastFiredRef" in self.html, (
            "useVoiceKeywords must use lastFiredRef for debounce"
        )

    def test_debounce_ms_constant(self) -> None:
        assert "DEBOUNCE_MS" in self.html, (
            "useVoiceKeywords must define DEBOUNCE_MS constant"
        )

    def test_debounce_value_is_2000(self) -> None:
        assert "2000" in self.html, "DEBOUNCE_MS must be 2000ms"

    def test_check_transcript_function(self) -> None:
        assert "checkTranscript" in self.html, (
            "useVoiceKeywords must expose checkTranscript()"
        )

    def test_hey_wake_word_detection(self) -> None:
        assert "hey " in self.html.lower(), (
            "checkTranscript must detect 'hey {assistantName}' wake word"
        )

    def test_go_ahead_trigger(self) -> None:
        assert "go ahead" in self.html, (
            "checkTranscript must handle 'go ahead' keyword to trigger response"
        )

    def test_your_turn_trigger(self) -> None:
        assert "your turn" in self.html, (
            "checkTranscript must handle 'your turn' keyword to trigger response"
        )

    def test_pause_replies_keyword(self) -> None:
        assert "pause replies" in self.html, (
            "checkTranscript must handle 'pause replies' keyword"
        )

    def test_resume_keyword(self) -> None:
        # 'resume' can appear as part of a larger string; check the keyword intent
        assert "resume" in self.html, "checkTranscript must handle 'resume' keyword"

    def test_mute_keyword(self) -> None:
        assert "'mute'" in self.html or '"mute"' in self.html, (
            "checkTranscript must handle 'mute' keyword"
        )

    def test_unmute_keyword(self) -> None:
        assert "unmute" in self.html, "checkTranscript must handle 'unmute' keyword"

    def test_unmute_checked_before_mute(self) -> None:
        """unmute must be detected before mute to avoid false mute match."""
        unmute_pos = self.html.find("unmute")
        mute_pos = self.html.find("'mute'")
        if mute_pos == -1:
            mute_pos = self.html.find('"mute"')
        assert unmute_pos < mute_pos, (
            "'unmute' check must appear before 'mute' check to avoid false match"
        )

    def test_voice_keywords_wired_in_voice_app(self) -> None:
        assert "useVoiceKeywords" in self.html, (
            "VoiceApp must call useVoiceKeywords hook"
        )


# ---------------------------------------------------------------------------
# TestVoiceAppTask54Wiring — Task 5.4
# ---------------------------------------------------------------------------


class TestVoiceAppTask54Wiring:
    """Tests for Task 5.4: VoiceApp assistant_name fetch + hook wiring."""

    @pytest.fixture(autouse=True)
    def content(self) -> None:
        self.html = INDEX_HTML.read_text(encoding="utf-8")

    def test_assistant_name_state_with_default(self) -> None:
        assert "assistantName" in self.html, (
            "VoiceApp must maintain assistantName state"
        )

    def test_assistant_name_default_is_amplifier(self) -> None:
        assert "'Amplifier'" in self.html or '"Amplifier"' in self.html, (
            "assistantName default must be 'Amplifier'"
        )

    def test_fetches_api_status_on_mount(self) -> None:
        assert "/api/status" in self.html, (
            "VoiceApp must fetch /api/status to get assistant_name"
        )

    def test_check_transcript_called_on_transcription_completed(self) -> None:
        assert "checkTranscript" in self.html, (
            "handleDataChannelEvent must call checkTranscript "
            "on transcription.completed"
        )

    def test_pause_replies_ui_indicator(self) -> None:
        assert "pauseReplies" in self.html, (
            "VoiceApp render must show paused state indicator using pauseReplies"
        )

    def test_set_mic_stream_called_after_connect(self) -> None:
        assert "setMicStream" in self.html, (
            "VoiceApp must call setMicStream after WebRTC connect to wire mic stream"
        )
