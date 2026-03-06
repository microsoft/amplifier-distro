"""Slack text streaming — pipes Amplifier token events to Slack's streaming API.

Uses Slack's chat.startStream / chat.appendStream / chat.stopStream APIs
to deliver LLM tokens in real-time. Falls back to batch post_message()
when streaming is not available (missing assistant:write scope, simulator
mode, or API errors).

The pattern mirrors the chat app's connection.py event fanout loop:
  1. Wire an asyncio.Queue to the session via backend.resume_session()
  2. Call backend.execute(session_id, prompt)
  3. Consume events from the queue:
     - content_block:delta → append text to Slack stream
     - tool:pre → set assistant thread status ("Reading files...")
     - tool:post → clear status
     - orchestrator:complete → stop stream, done
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .client import SlackClient
    from .config import SlackConfig

logger = logging.getLogger(__name__)

# Friendly tool names for status messages
TOOL_FRIENDLY_NAMES: dict[str, str] = {
    "read_file": "Reading files",
    "write_file": "Writing files",
    "edit_file": "Editing files",
    "apply_patch": "Applying patches",
    "bash": "Running command",
    "grep": "Searching",
    "glob": "Finding files",
    "delegate": "Delegating to agent",
    "web_search": "Searching the web",
    "web_fetch": "Fetching web content",
    "python_check": "Checking code quality",
    "LSP": "Analyzing code",
    "todo": "Planning tasks",
    "recipes": "Running recipe",
    "mode": "Switching mode",
}

# Buffer size for Slack streaming (characters to accumulate before flushing)
_STREAM_BUFFER_SIZE = 40

# Event queue max size (same as chat app)
_EVENT_QUEUE_MAX_SIZE = 10000


def _friendly_tool_name(tool_name: str) -> str:
    """Map internal tool names to user-friendly labels."""
    return TOOL_FRIENDLY_NAMES.get(tool_name, tool_name.replace("_", " ").title())


class SlackStreamer:
    """Streams Amplifier execution events to Slack.

    Manages the lifecycle of one streaming execution:
      1. Wire event queue to the session
      2. Start Slack stream (or fallback to status message)
      3. Pipe content_block:delta tokens to Slack
      4. Show tool status between text chunks
      5. Finalize when orchestrator:complete arrives
    """

    def __init__(
        self,
        client: SlackClient,
        config: SlackConfig,
        backend: Any,  # FoundationBackend
    ) -> None:
        self._client = client
        self._config = config
        self._backend = backend
        self._team_id: str | None = None
        self._bot_user_id: str | None = None

    async def execute_streaming(
        self,
        session_id: str,
        prompt: str,
        channel: str,
        thread_ts: str,
        working_dir: str = "",
        user_id: str = "",
    ) -> str | None:
        """Execute a prompt with real-time token streaming to Slack.

        Returns the full response text on completion, or None on failure.
        Falls back to batch mode if streaming APIs are unavailable.
        """
        # Create event queue and wire it to the session
        event_queue: asyncio.Queue = asyncio.Queue(maxsize=_EVENT_QUEUE_MAX_SIZE)

        try:
            await self._backend.resume_session(
                session_id, working_dir or "~", event_queue=event_queue
            )
        except (ValueError, RuntimeError):
            logger.debug(
                "Could not wire event queue for %s, falling back to batch mode",
                session_id,
                exc_info=True,
            )
            return None  # Caller should fall back to send_message

        # Try to use Slack's native streaming API
        stream_id = await self._try_start_stream(channel, thread_ts, user_id=user_id)
        use_native_streaming = stream_id is not None

        if not use_native_streaming:
            # Fallback: post an editable status message
            status_ts = await self._client.post_message(
                channel, text="\u2699\ufe0f Working...", thread_ts=thread_ts
            )

        # Start execution in background
        exec_task = asyncio.create_task(
            self._backend.execute(session_id, prompt)
        )

        # Consume events
        full_text: list[str] = []
        start_time = time.monotonic()
        done = False

        try:
            while not done:
                # Check if execution finished
                if exec_task.done():
                    # Drain remaining events
                    while not event_queue.empty():
                        try:
                            evt_name, evt_data = event_queue.get_nowait()
                            await self._process_event(
                                evt_name, evt_data, full_text,
                                channel, thread_ts, stream_id,
                                use_native_streaming,
                            )
                        except asyncio.QueueEmpty:
                            break
                    done = True
                    continue

                # Wait for next event
                try:
                    evt_name, evt_data = await asyncio.wait_for(
                        event_queue.get(), timeout=2.0
                    )
                except TimeoutError:
                    # Update status message with elapsed time (fallback mode only)
                    if not use_native_streaming:
                        elapsed = time.monotonic() - start_time
                        if elapsed >= 10:
                            mins = int(elapsed) // 60
                            secs = int(elapsed) % 60
                            t = f"{mins}m {secs:02d}s" if mins else f"{secs}s"
                            try:
                                await self._client.update_message(
                                    channel, status_ts, text=f"\u2699\ufe0f Working... \u00b7 {t}"
                                )
                            except Exception:
                                pass
                    continue

                result = await self._process_event(
                    evt_name, evt_data, full_text,
                    channel, thread_ts, stream_id,
                    use_native_streaming,
                )
                if result == "done":
                    done = True

            # Check for execution errors
            if exec_task.done() and exec_task.exception():
                raise exec_task.exception()

        except Exception:
            logger.exception("Error during streaming execution for session %s", session_id)
            full_text.append("\n\nError: Execution failed. Check server logs.")

        # Finalize
        response = "".join(full_text) if full_text else None

        if use_native_streaming and stream_id:
            await self._try_stop_stream(stream_id)
        elif not use_native_streaming:
            # Delete the status message
            try:
                await self._client.delete_message(channel, status_ts)
            except Exception:
                logger.debug("Failed to delete status message", exc_info=True)

        return response

    async def _process_event(
        self,
        evt_name: str,
        evt_data: dict[str, Any],
        full_text: list[str],
        channel: str,
        thread_ts: str,
        stream_id: str | None,
        use_streaming: bool,
    ) -> str | None:
        """Process a single event from the queue.

        Returns "done" when execution is complete, None otherwise.
        """
        if evt_name == "content_block:delta":
            delta = evt_data.get("delta", "")
            if isinstance(delta, dict):
                delta = delta.get("text") or delta.get("thinking") or ""
            if not isinstance(delta, str):
                delta = str(delta) if delta else ""
            if delta:
                full_text.append(delta)
                if use_streaming and stream_id:
                    await self._try_append_stream(stream_id, delta)

        elif evt_name == "content_block:end":
            # Extract full text for non-streaming providers
            text = evt_data.get("text", "")
            if isinstance(text, str) and text and not full_text:
                full_text.append(text)

        elif evt_name == "tool:pre":
            tool_name = evt_data.get("tool", "")
            if tool_name and use_streaming:
                await self._try_set_status(
                    channel, thread_ts, f"Using {_friendly_tool_name(tool_name)}..."
                )

        elif evt_name == "tool:post":
            if use_streaming:
                await self._try_set_status(channel, thread_ts, "")

        elif evt_name in ("orchestrator:complete", "prompt:complete"):
            return "done"

        return None

    # --- Slack Streaming API wrappers (best-effort, never fatal) ---

    async def _slack_api(self, method: str, **kwargs: Any) -> dict[str, Any] | None:
        """Make a Slack API call with proper timeout. Returns response dict or None."""
        try:
            import httpx

            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"https://slack.com/api/{method}",
                    headers={"Authorization": f"Bearer {self._config.bot_token}"},
                    json=kwargs,
                )
                return resp.json()
        except Exception:
            logger.debug("Slack API call %s failed", method, exc_info=True)
            return None

    async def _resolve_team_id(self) -> str | None:
        """Get and cache the workspace team ID via auth.test."""
        if self._team_id is not None:
            return self._team_id
        data = await self._slack_api("auth.test")
        if data and data.get("ok"):
            self._team_id = data.get("team_id", "")
            self._bot_user_id = data.get("user_id", "")
            logger.debug("Resolved team_id: %s, bot_user_id: %s", self._team_id, self._bot_user_id)
            return self._team_id
        return None

    async def _resolve_bot_user_id(self) -> str | None:
        """Get and cache the bot's user ID."""
        if self._bot_user_id is not None:
            return self._bot_user_id
        await self._resolve_team_id()
        return self._bot_user_id

    async def _try_start_stream(
        self, channel: str, thread_ts: str, user_id: str = ""
    ) -> str | None:
        """Start a Slack text stream. Returns stream_id or None on failure."""
        team_id = await self._resolve_team_id()
        if not team_id:
            logger.info("Could not resolve team_id, skipping streaming")
            return None

        kwargs: dict[str, Any] = {
            "channel": channel,
            "thread_ts": thread_ts,
            "recipient_team_id": team_id,
        }
        # recipient_user_id is required — use the message sender
        if user_id:
            kwargs["recipient_user_id"] = user_id
        data = await self._slack_api("chat.startStream", **kwargs)
        if data and data.get("ok"):
            stream_id = data.get("stream_id", "")
            logger.info("Slack stream started: %s", stream_id)
            return stream_id
        error = data.get("error", "unknown") if data else "no response"
        logger.info("Slack streaming not available (%s), using fallback", error)
        return None

    async def _try_append_stream(self, stream_id: str, text: str) -> None:
        """Append text to a Slack stream. Best-effort."""
        await self._slack_api("chat.appendStream", stream_id=stream_id, text=text)

    async def _try_stop_stream(self, stream_id: str) -> None:
        """Stop a Slack stream. Best-effort."""
        result = await self._slack_api("chat.stopStream", stream_id=stream_id)
        if result:
            logger.info("Slack stream stopped: %s", stream_id)

    async def _try_set_status(
        self, channel: str, thread_ts: str, status: str
    ) -> None:
        """Set assistant thread status (typing indicator). Best-effort."""
        await self._slack_api(
            "assistant.threads.setStatus",
            channel_id=channel, thread_ts=thread_ts, status=status,
        )
