"""Slack Events API handler.

Handles incoming HTTP webhooks from Slack's Events API:
1. URL verification challenge (required for Slack app setup)
2. Event callbacks (messages, mentions, etc.)

Security:
- All requests are verified using the Slack signing secret
- Timestamps are checked to prevent replay attacks
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re as _re
import time
from pathlib import Path
from typing import Any

from .client import SlackClient
from .commands import CommandContext, CommandHandler
from .config import SlackConfig
from .formatter import SlackFormatter
from .models import SlackMessage
from .sessions import SlackSessionManager

# Friendly names for tool operations shown in progress messages
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

# Max file size for downloads (50 MB)
_MAX_FILE_SIZE = 50 * 1024 * 1024

# Progress message update throttle
_STATUS_THROTTLE_SECS = 2.0

logger = logging.getLogger(__name__)


class SlackEventHandler:
    """Handles incoming Slack events.

    This is the main entry point for all Slack → Bridge communication.
    It verifies signatures, parses events, and routes them to either
    the command handler or the session manager.
    """

    def __init__(
        self,
        client: SlackClient,
        session_manager: SlackSessionManager,
        command_handler: CommandHandler,
        config: SlackConfig,
    ) -> None:
        self._client = client
        self._sessions = session_manager
        self._commands = command_handler
        self._config = config
        self._bot_user_id: str | None = None

    async def get_bot_user_id(self) -> str:
        """Get and cache the bot's user ID."""
        if self._bot_user_id is None:
            self._bot_user_id = await self._client.get_bot_user_id()
        return self._bot_user_id

    def verify_signature(
        self,
        body: bytes,
        timestamp: str,
        signature: str,
    ) -> bool:
        """Verify a Slack request signature.

        Slack signs each request with the signing secret. We verify
        the signature to ensure the request is authentic.

        Returns True if the signature is valid.
        """
        if not self._config.signing_secret:
            # In simulator mode, skip verification
            return self._config.simulator_mode

        # Check timestamp to prevent replay attacks (5 minute window)
        try:
            ts = int(timestamp)
        except (ValueError, TypeError):
            return False

        if abs(time.time() - ts) > 300:
            logger.warning("Slack request timestamp too old, possible replay attack")
            return False

        # Compute expected signature
        sig_basestring = f"v0:{timestamp}:{body.decode('utf-8')}"
        expected = (
            "v0="
            + hmac.new(
                self._config.signing_secret.encode(),
                sig_basestring.encode(),
                hashlib.sha256,
            ).hexdigest()
        )

        return hmac.compare_digest(expected, signature)

    async def handle_event_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Handle a Slack event payload.

        This is the main dispatch method. It handles:
        - URL verification challenges
        - Event callbacks (messages, mentions, etc.)

        Returns a response dict to send back to Slack.
        """
        event_type = payload.get("type")

        # URL verification challenge
        if event_type == "url_verification":
            return {"challenge": payload.get("challenge", "")}

        # Event callback
        if event_type == "event_callback":
            event = payload.get("event", {})
            await self._dispatch_event(event)
            return {"ok": True}

        logger.warning(f"Unknown event type: {event_type}")
        return {"ok": True}

    async def _dispatch_event(self, event: dict[str, Any]) -> None:
        """Dispatch a Slack event to the appropriate handler."""
        event_type = event.get("type")

        if event_type == "message":
            await self._handle_message(event)
        elif event_type == "app_mention":
            await self._handle_app_mention(event)
        else:
            logger.debug(f"Ignoring event type: {event_type}")

    async def _handle_message(self, event: dict[str, Any]) -> None:
        """Handle a message event.

        Routes the message to either:
        1. Command handler (if it looks like a command)
        2. Session manager (if there's an active session mapping)
        3. Ignore (if neither applies)
        """
        # Ignore bot messages (prevent loops)
        if event.get("bot_id") or event.get("subtype") == "bot_message":
            return

        # Ignore message edits and deletes
        if event.get("subtype") in ("message_changed", "message_deleted"):
            return

        bot_user_id = await self.get_bot_user_id()
        user_id = event.get("user", "")
        text = event.get("text", "").strip()
        channel_id = event.get("channel", "")
        thread_ts = event.get("thread_ts")
        message_ts = event.get("ts", "")

        if not text or not channel_id:
            return

        message = SlackMessage(
            channel_id=channel_id,
            user_id=user_id,
            text=text,
            ts=message_ts,
            thread_ts=thread_ts,
        )

        # Check if this is a command (mentions bot or starts with bot name)
        # Slack sends mentions as <@U123> or <@U123|displayname> - match both
        is_command = (
            f"<@{bot_user_id}" in text
            or text.lower().startswith(f"@{self._config.bot_name}")
            or text.lower().startswith(f"{self._config.bot_name} ")
        )

        if is_command:
            await self._handle_command_message(message, bot_user_id)
            return

        # Check if there's a session mapping for this context
        mapping = self._sessions.get_mapping(channel_id, thread_ts)
        if mapping and mapping.is_active:
            await self._handle_session_message(message, event=event)
            return

        # No active session mapping for this context
        logger.info(
            "No active session for channel=%s thread_ts=%s (message ignored)",
            channel_id,
            thread_ts or "none",
        )

    async def _handle_app_mention(self, event: dict[str, Any]) -> None:
        """Handle an @mention event.

        Always treated as a command.
        """
        bot_user_id = await self.get_bot_user_id()
        message = SlackMessage(
            channel_id=event.get("channel", ""),
            user_id=event.get("user", ""),
            text=event.get("text", ""),
            ts=event.get("ts", ""),
            thread_ts=event.get("thread_ts"),
        )
        await self._handle_command_message(message, bot_user_id)

    async def _handle_command_message(
        self, message: SlackMessage, bot_user_id: str
    ) -> None:
        """Parse and execute a command from a message."""
        command, args = self._commands.parse_command(message.text, bot_user_id)

        ctx = CommandContext(
            channel_id=message.channel_id,
            user_id=message.user_id,
            thread_ts=message.thread_ts,
            raw_text=message.text,
        )

        # Add a "thinking" reaction (best-effort, never fatal)
        await self._safe_react(message.channel_id, message.ts, "hourglass_flowing_sand")

        result = await self._commands.handle(command, args, ctx)

        # Determine where to reply
        reply_thread = message.thread_ts or message.ts
        if result.create_thread:
            reply_thread = None  # Will create a new thread from the reply

        # Send the response, with fallback for blocks failures.
        # Capture the ts of the first post_message() so we can re-key the session
        # mapping from bare channel_id to channel_id:thread_ts (issue #54).
        posted_ts: str | None = None
        try:
            if result.blocks:
                posted_ts = await self._client.post_message(
                    message.channel_id,
                    text=result.text or "Amplifier",
                    thread_ts=reply_thread,
                    blocks=result.blocks,
                )
            elif result.text:
                for chunk in SlackFormatter.split_message(result.text):
                    ts = await self._client.post_message(
                        message.channel_id,
                        text=chunk,
                        thread_ts=reply_thread,
                    )
                    if posted_ts is None:
                        posted_ts = ts  # Capture ts of the first (thread-creating) post
        except Exception:  # noqa: BLE001
            logger.warning(
                "Failed to send blocks, falling back to plain text", exc_info=True
            )
            # Fallback: send blocks content as plain text
            fallback = result.text or self._blocks_to_plaintext(result.blocks)
            if fallback:
                try:
                    for chunk in SlackFormatter.split_message(fallback):
                        ts = await self._client.post_message(
                            message.channel_id,
                            text=chunk,
                            thread_ts=reply_thread,
                        )
                        if posted_ts is None:
                            posted_ts = ts
                except Exception:
                    logger.exception("Fallback plain-text send also failed")

        # Re-key the session mapping from bare channel_id to the new thread_ts.
        # This prevents a second /new command from overwriting the first session's
        # routing entry (issue #54).
        if result.create_thread and posted_ts is not None:
            self._sessions.rekey_mapping(message.channel_id, posted_ts)

        # Done reaction (best-effort, never fatal)
        await self._safe_react(message.channel_id, message.ts, "white_check_mark")

    async def handle_interactive_payload(self, payload: dict[str, Any]) -> None:
        """Handle a Slack interactive payload (button clicks, modals, etc.).

        Slack sends these when a user clicks a Block Kit button, submits a
        modal, or interacts with a message shortcut.  The payload structure
        varies by interaction type; we currently support ``block_actions``
        for the "Connect" buttons rendered by the session list.
        """
        interaction_type = payload.get("type")

        if interaction_type != "block_actions":
            logger.debug(f"Ignoring interactive type: {interaction_type}")
            return

        actions = payload.get("actions", [])
        if not actions:
            return

        action = actions[0]
        action_id: str = action.get("action_id", "")
        value: str = action.get("value", "")

        # Route connect_session_* buttons to cmd_connect
        if action_id.startswith("connect_session_") and value:
            user = payload.get("user", {})
            channel = payload.get("channel", {})
            message = payload.get("message", {})

            user_id = user.get("id", "")
            channel_id = channel.get("id", "")
            # Interactive payloads in threads include message.thread_ts;
            # if the button was in a top-level message, thread_ts is absent.
            thread_ts = message.get("thread_ts")

            ctx = CommandContext(
                channel_id=channel_id,
                user_id=user_id,
                thread_ts=thread_ts,
                raw_text=f"connect {value}",
            )

            result = await self._commands.handle("connect", [value], ctx)

            # Send the response back to the channel
            reply_thread = thread_ts or message.get("ts")
            try:
                if result.blocks:
                    await self._client.post_message(
                        channel_id,
                        text=result.text or "Amplifier",
                        thread_ts=reply_thread,
                        blocks=result.blocks,
                    )
                elif result.text:
                    for chunk in SlackFormatter.split_message(result.text):
                        await self._client.post_message(
                            channel_id,
                            text=chunk,
                            thread_ts=reply_thread,
                        )
            except Exception:
                logger.exception("Failed to send interactive response")
        else:
            logger.debug(f"Unhandled action: {action_id}")

    async def handle_slash_command(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Handle a Slack slash command payload.

        Slack sends slash commands (e.g. ``/amp list``) as a flat dict with
        ``command``, ``text``, ``user_id``, ``channel_id``, etc.  We parse the
        text the same way we parse @-mention commands and return a Slack
        response payload (``response_type`` + ``text``/``blocks``).
        """
        command_text = payload.get("text", "").strip()
        user_id = payload.get("user_id", "")
        channel_id = payload.get("channel_id", "")

        # Reuse the existing command parser (handles aliases, etc.)
        command, args = self._commands.parse_command(command_text)

        ctx = CommandContext(
            channel_id=channel_id,
            user_id=user_id,
            thread_ts=None,
            raw_text=command_text,
        )

        result = await self._commands.handle(command, args, ctx)

        # Build Slack slash-command response
        response: dict[str, Any] = {
            "response_type": "in_channel",
        }
        if result.blocks:
            response["blocks"] = result.blocks
            response["text"] = result.text or "Amplifier"
        elif result.text:
            response["text"] = result.text

        return response

    async def _safe_react(self, channel: str, ts: str, emoji: str) -> None:
        """Add a reaction, ignoring failures (already_reacted, etc.)."""
        try:
            await self._client.add_reaction(channel, ts, emoji)
        except (RuntimeError, ConnectionError, OSError, ValueError):
            logger.debug(
                f"Reaction '{emoji}' failed (likely duplicate event)", exc_info=True
            )

    @staticmethod
    def _blocks_to_plaintext(blocks: list[dict[str, Any]] | None) -> str:
        """Extract readable text from Block Kit blocks as a fallback."""
        if not blocks:
            return ""
        parts: list[str] = []
        for block in blocks:
            if block.get("type") == "header":
                text = block.get("text", {}).get("text", "")
                if text:
                    parts.append(f"*{text}*")
            elif block.get("type") == "section":
                text = block.get("text", {}).get("text", "")
                if text:
                    parts.append(text)
        return "\n".join(parts)

    # --- Prompt enrichment (Task 1) ---

    async def _build_prompt(
        self,
        message: SlackMessage,
        file_descriptions: list[str] | None = None,
    ) -> str:
        """Build an enriched prompt with context metadata.

        Wraps the user's raw text with sender/channel info and any
        uploaded file descriptions so the AI has full context.
        """
        parts: list[str] = []

        # Sender and channel context
        channel_info = await self._client.get_channel_info(message.channel_id)
        channel_label = f"#{channel_info.name}" if channel_info else message.channel_id
        parts.append(f"[From <@{message.user_id}> in {channel_label}]")

        # File descriptions (populated by _download_files)
        if file_descriptions:
            parts.append("[User uploaded files:")
            parts.extend(f"  {desc}" for desc in file_descriptions)
            parts.append("]")

        # The actual user message
        parts.append(message.text)

        return "\n".join(parts)

    # --- File download (Task 2) ---

    async def _download_files(
        self,
        event: dict[str, Any],
        working_dir: str,
        channel_id: str = "",
        thread_ts: str = "",
    ) -> list[str]:
        """Download files attached to a Slack message.

        Returns a list of description strings like:
            "report.py (1234 bytes) -> ./report.py"

        On failure, posts an error message to the Slack thread so
        the user knows what went wrong.
        """
        files = event.get("files", [])
        if not files:
            return []

        descriptions: list[str] = []
        errors: list[str] = []
        wd = Path(working_dir).expanduser()
        wd.mkdir(parents=True, exist_ok=True)

        for file_info in files:
            url = file_info.get("url_private")
            name = file_info.get("name", "file")
            size = file_info.get("size", 0)

            if not url:
                errors.append(f"{name}: no download URL available")
                continue

            # Enforce size limit
            if size > _MAX_FILE_SIZE:
                errors.append(f"{name}: file too large ({size:,} bytes, max 50MB)")
                logger.warning("File %s too large (%d bytes), skipping", name, size)
                continue

            # Sanitize filename
            safe_name = _re.sub(r"[^\w\-.]", "_", name)

            # Handle filename conflicts
            dest = wd / safe_name
            counter = 1
            while dest.exists():
                stem = Path(safe_name).stem
                suffix = Path(safe_name).suffix
                dest = wd / f"{stem}_{counter}{suffix}"
                counter += 1

            # Download
            try:
                import aiohttp  # pyright: ignore[reportMissingImports]

                async with aiohttp.ClientSession() as session:
                    headers = {"Authorization": f"Bearer {self._config.bot_token}"}
                    async with session.get(url, headers=headers) as resp:
                        if resp.status == 200:
                            data = await resp.read()
                            # Detect HTML redirect (missing files:read scope)
                            content_type = resp.headers.get("Content-Type", "")
                            if "text/html" in content_type or (
                                len(data) < 10000
                                and data[:15].lower().startswith(b"<!doctype html")
                            ):
                                errors.append(
                                    f"{name}: got HTML instead of file content "
                                    "(Slack app needs `files:read` scope -- "
                                    "reinstall the app with updated permissions)"
                                )
                                logger.warning(
                                    "File %s returned HTML instead of content. "
                                    "Add files:read scope and reinstall.",
                                    name,
                                )
                                continue
                            dest.write_bytes(data)
                            descriptions.append(
                                f"{name} ({size} bytes) -> ./{dest.name}"
                            )
                            logger.info("Downloaded %s -> %s", name, dest)
                        elif resp.status == 403:
                            errors.append(
                                f"{name}: access denied (HTTP 403). "
                                "Slack app needs `files:read` scope."
                            )
                            logger.warning(
                                "File download 403 for %s -- missing files:read scope",
                                name,
                            )
                        else:
                            errors.append(
                                f"{name}: download failed (HTTP {resp.status})"
                            )
                            logger.warning(
                                "Failed to download %s: HTTP %d", name, resp.status
                            )
            except ImportError:
                errors.append(
                    "File downloads require aiohttp. "
                    "Install with: `uv pip install amplifier-distro[slack]`"
                )
                logger.warning("aiohttp not available for file downloads")
                break
            except Exception:
                errors.append(f"{name}: download failed (unexpected error)")
                logger.exception("Error downloading file %s", name)

        # Post errors to the Slack thread so the user sees them
        if errors and channel_id:
            error_text = ":warning: *File download issues:*\n" + "\n".join(
                f"\u2022 {e}" for e in errors
            )
            try:
                await self._client.post_message(
                    channel_id, text=error_text, thread_ts=thread_ts or None
                )
            except Exception:
                logger.debug("Failed to post file error to Slack", exc_info=True)

        return descriptions

    # --- Progress messages (Tasks 4-6) ---

    def _friendly_tool_name(self, tool_name: str) -> str:
        """Map internal tool names to user-friendly labels."""
        return TOOL_FRIENDLY_NAMES.get(tool_name, tool_name.replace("_", " ").title())

    @staticmethod
    def _render_todo_status(todos: list[dict[str, Any]]) -> str:
        """Render a todo list as a compact progress display.

        Shows completed count, current in-progress item, next few
        pending items, and a +N more indicator.
        """
        completed = [t for t in todos if t.get("status") == "completed"]
        in_progress = [t for t in todos if t.get("status") == "in_progress"]
        pending = [t for t in todos if t.get("status") == "pending"]

        lines: list[str] = []

        # Completed summary
        if completed:
            lines.append(f"\u2705  {len(completed)} completed")

        # In-progress (show all, usually just one)
        for t in in_progress:
            content = t.get("activeForm") or t.get("content", "Working")
            lines.append(f"\u25b8  *{content}*")

        # Pending (show next 2, then +N more)
        shown_pending = pending[:2]
        for t in shown_pending:
            content = t.get("content", "")
            lines.append(f"\u25cb  {content}")

        remaining = len(pending) - len(shown_pending)
        if remaining > 0:
            lines.append(f"   +{remaining} more")

        return "\n".join(lines)

    async def _execute_with_progress(
        self,
        message: SlackMessage,
        enriched_text: str,
    ) -> str | None:
        """Execute a session message with a live progress indicator.

        Posts an editable "Working..." status message, runs the prompt
        via send_message() (which blocks until complete), updates the
        status with elapsed time while waiting, then deletes the status
        message and returns the response.
        """
        mapping = self._sessions.get_mapping(message.channel_id, message.thread_ts)
        if mapping is None or not mapping.is_active:
            return None

        reply_thread = message.thread_ts or message.ts

        # Post initial status message
        status_ts = await self._client.post_message(
            message.channel_id,
            text="\u2699\ufe0f Working...",
            thread_ts=reply_thread,
        )

        start_time = time.monotonic()

        async def _update_status_loop() -> None:
            """Periodically update the status message with elapsed time."""
            while True:
                await asyncio.sleep(_STATUS_THROTTLE_SECS)
                elapsed = time.monotonic() - start_time
                if elapsed < 10:
                    continue  # Don't show elapsed for quick responses
                minutes = int(elapsed) // 60
                seconds = int(elapsed) % 60
                elapsed_str = (
                    f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"
                )
                try:
                    await self._client.update_message(
                        message.channel_id,
                        status_ts,
                        text=f"\u2699\ufe0f Working... \u00b7 {elapsed_str}",
                    )
                except Exception:
                    logger.debug("Failed to update status message", exc_info=True)

        # Run the execution and status updater concurrently
        status_task = asyncio.create_task(_update_status_loop())
        try:
            response = await self._sessions.route_message(
                message, text_override=enriched_text
            )
        except Exception:
            logger.exception("Error during session execution")
            response = "Error: Failed to get response from Amplifier session."
        finally:
            status_task.cancel()

        # Delete the status message
        try:
            await self._client.delete_message(message.channel_id, status_ts)
        except Exception:
            logger.debug("Failed to delete status message", exc_info=True)

        return response

    # --- Session message handler (rewritten) ---

    async def _handle_session_message(
        self,
        message: SlackMessage,
        event: dict[str, Any] | None = None,
    ) -> None:
        """Route a message to its mapped Amplifier session."""
        # Add thinking indicator (best-effort)
        await self._safe_react(message.channel_id, message.ts, "hourglass_flowing_sand")

        # Look up the session mapping for file download and outbox
        mapping = self._sessions.get_mapping(message.channel_id, message.thread_ts)

        # Download attached files if present
        file_descriptions: list[str] | None = None
        if event and event.get("files") and mapping and mapping.working_dir:
            file_descriptions = await self._download_files(
                event,
                mapping.working_dir,
                channel_id=message.channel_id,
                thread_ts=message.thread_ts or message.ts,
            )

        # Build enriched prompt with context
        enriched_text = await self._build_prompt(message, file_descriptions)

        # Execute with live progress updates
        response = await self._execute_with_progress(message, enriched_text)

        if response:
            reply_thread = message.thread_ts or message.ts
            chunks = SlackFormatter.format_response(response)
            for chunk in chunks:
                await self._client.post_message(
                    message.channel_id,
                    text=chunk,
                    thread_ts=reply_thread,
                )

        # Done reaction (best-effort)
        await self._safe_react(message.channel_id, message.ts, "white_check_mark")
