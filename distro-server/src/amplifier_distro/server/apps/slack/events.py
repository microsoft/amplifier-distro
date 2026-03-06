"""Slack Events API handler.

Handles incoming HTTP webhooks from Slack's Events API:
1. URL verification challenge (required for Slack app setup)
2. Event callbacks (messages, mentions, etc.)

Security:
- All requests are verified using the Slack signing secret
- Timestamps are checked to prevent replay attacks
"""

from __future__ import annotations

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

logger = logging.getLogger(__name__)

# Max file size for Slack file downloads (50 MB)
_MAX_FILE_SIZE = 50 * 1024 * 1024


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
        # Track bot response ts -> (session_id, prompt, channel, thread) for regeneration
        self._message_prompts: dict[str, tuple[str, str, str, str | None]] = {}

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
        elif event_type == "reaction_added":
            await self._handle_reaction(event)
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

        # DM detection: Slack sends channel_type="im" for direct messages
        channel_type = event.get("channel_type", "")
        is_dm = channel_type == "im"

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

        # DMs without a session mapping: treat as a command (e.g., "new", "help")
        if is_dm:
            await self._handle_command_message(message, bot_user_id)
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

    # --- Prompt enrichment ---

    async def _build_prompt(
        self,
        message: SlackMessage,
        file_descriptions: list[str] | None = None,
    ) -> str:
        """Build an enriched prompt with context metadata."""
        parts: list[str] = []

        channel_info = await self._client.get_channel_info(message.channel_id)
        if channel_info:
            channel_label = f"#{channel_info.name}"
        else:
            channel_label = message.channel_id
        parts.append(f"[From <@{message.user_id}> in {channel_label}]")

        if file_descriptions:
            parts.append("[User uploaded files:")
            parts.extend(f"  {desc}" for desc in file_descriptions)
            parts.append("]")

        parts.append(message.text)
        return "\n".join(parts)

    # --- File download ---

    async def _download_files(
        self,
        event: dict[str, Any],
        working_dir: str,
        channel_id: str = "",
        thread_ts: str = "",
    ) -> list[str]:
        """Download files attached to a Slack message.

        Returns description strings. Posts errors to Slack thread.
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
            if size > _MAX_FILE_SIZE:
                errors.append(f"{name}: file too large ({size:,} bytes, max 50MB)")
                continue

            safe_name = _re.sub(r"[^\w\\-.]", "_", name)
            dest = wd / safe_name
            counter = 1
            while dest.exists():
                stem, suffix = Path(safe_name).stem, Path(safe_name).suffix
                dest = wd / f"{stem}_{counter}{suffix}"
                counter += 1

            try:
                import aiohttp  # pyright: ignore[reportMissingImports]

                async with aiohttp.ClientSession() as session:
                    headers = {"Authorization": f"Bearer {self._config.bot_token}"}
                    async with session.get(url, headers=headers) as resp:
                        if resp.status == 200:
                            data = await resp.read()
                            ct = resp.headers.get("Content-Type", "")
                            if "text/html" in ct or (
                                len(data) < 10000
                                and data[:15].lower().startswith(b"<!doctype html")
                            ):
                                errors.append(
                                    f"{name}: got HTML instead of file content "
                                    "(Slack app needs `files:read` scope)"
                                )
                                continue
                            dest.write_bytes(data)
                            descriptions.append(f"{name} ({size} bytes) -> ./{dest.name}")
                            logger.info("Downloaded %s -> %s", name, dest)
                        elif resp.status == 403:
                            errors.append(f"{name}: access denied (needs `files:read` scope)")
                        else:
                            errors.append(f"{name}: download failed (HTTP {resp.status})")
            except ImportError:
                errors.append("File downloads require aiohttp: uv pip install amplifier-distro[slack]")
                break
            except Exception:
                errors.append(f"{name}: download failed (unexpected error)")
                logger.exception("Error downloading file %s", name)

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

    # --- Reaction commands (regenerate + cancel) ---

    async def _handle_reaction(self, event: dict[str, Any]) -> None:
        """Handle reaction_added events for regenerate and cancel."""
        reaction = event.get("reaction", "")
        user = event.get("user", "")
        item = event.get("item", {})
        channel = item.get("channel", "")
        message_ts = item.get("ts", "")

        logger.info(
            "Reaction received: emoji=%s user=%s channel=%s ts=%s item=%s",
            reaction, user, channel, message_ts, item,
        )

        if not channel or not message_ts:
            logger.info("Reaction ignored: missing channel or ts")
            return

        # Don't process our own reactions
        bot_user_id = await self.get_bot_user_id()
        if user == bot_user_id:
            logger.debug("Reaction ignored: from bot itself")
            return

        # Regenerate: re-execute original prompt
        if reaction in ("repeat", "arrows_counterclockwise"):
            prompt_info = self._message_prompts.get(message_ts)
            if prompt_info is None:
                logger.debug("No tracked prompt for message %s", message_ts)
                return

            session_id, original_prompt, original_channel, original_thread = prompt_info
            logger.info("Regenerate requested for session %s", session_id)

            await self._safe_react(channel, message_ts, "hourglass_flowing_sand")

            try:
                response = await self._sessions._backend.send_message(
                    session_id, original_prompt
                )
            except Exception:
                logger.exception("Regenerate failed for session %s", session_id)
                response = "Error: Failed to regenerate response."

            if response:
                reply_thread = original_thread or message_ts
                chunks = SlackFormatter.format_response(response)
                for chunk in chunks:
                    posted_ts = await self._client.post_message(
                        original_channel, text=chunk, thread_ts=reply_thread
                    )
                # Track the new response for future regeneration
                if posted_ts:
                    self._track_prompt(
                        posted_ts, session_id, original_prompt,
                        original_channel, original_thread,
                    )

            await self._safe_react(channel, message_ts, "white_check_mark")
            return

        # Cancel: stop running execution
        if reaction == "x":
            session_id: str | None = None

            # Try prompt tracking first
            prompt_info = self._message_prompts.get(message_ts)
            if prompt_info:
                session_id = prompt_info[0]
                logger.info("Cancel: found session %s via prompt tracking", session_id)
            else:
                # Search all active mappings for this channel
                logger.info(
                    "Cancel: no prompt tracked for ts=%s, searching active mappings "
                    "(channel=%s, tracked_prompts=%d)",
                    message_ts, channel, len(self._message_prompts),
                )
                for m in self._sessions.list_active():
                    if m.channel_id == channel and m.is_active:
                        session_id = m.session_id
                        logger.info("Cancel: found session %s via active mapping scan", session_id)
                        break

            if session_id is None:
                logger.info("Cancel: no session found for channel=%s, ignoring", channel)
                return

            logger.info("Cancel requested for session %s", session_id)

            # Post visible feedback so the user knows cancel was received
            reply_ts = await self._client.post_message(
                channel,
                text=":octagonal_sign: Cancelling...",
                thread_ts=message_ts,
            )

            try:
                await self._sessions._backend.cancel_session(session_id, level="immediate")
                await self._safe_react(channel, message_ts, "white_check_mark")
                # Update the cancel message
                try:
                    await self._client.update_message(
                        channel, reply_ts, text=":octagonal_sign: Cancelled."
                    )
                except Exception:
                    pass
            except Exception:
                logger.exception("Cancel failed for session %s", session_id)
                try:
                    await self._client.update_message(
                        channel, reply_ts,
                        text=":warning: Cancel requested but may not have taken effect.",
                    )
                except Exception:
                    pass
            return

    def _track_prompt(
        self,
        message_ts: str,
        session_id: str,
        prompt: str,
        channel_id: str,
        thread_ts: str | None,
    ) -> None:
        """Track a bot response message_ts -> prompt for regeneration."""
        self._message_prompts[message_ts] = (session_id, prompt, channel_id, thread_ts)
        # Bound the map to prevent unbounded growth
        if len(self._message_prompts) > 500:
            # Remove oldest entries (first 100)
            keys = list(self._message_prompts.keys())[:100]
            for k in keys:
                self._message_prompts.pop(k, None)

    # --- Session message handler ---

    async def _handle_session_message(
        self,
        message: SlackMessage,
        event: dict[str, Any] | None = None,
    ) -> None:
        """Route a message to its mapped Amplifier session."""
        await self._safe_react(message.channel_id, message.ts, "hourglass_flowing_sand")

        mapping = self._sessions.get_mapping(message.channel_id, message.thread_ts)

        # Download attached files if present
        file_descriptions: list[str] | None = None
        if event and event.get("files") and mapping and mapping.working_dir:
            file_descriptions = await self._download_files(
                event, mapping.working_dir,
                channel_id=message.channel_id,
                thread_ts=message.thread_ts or message.ts,
            )

        # Build enriched prompt
        enriched_text = await self._build_prompt(message, file_descriptions)

        # Route through session manager
        response = await self._sessions.route_message(
            message, text_override=enriched_text
        )

        if response:
            reply_thread = message.thread_ts or message.ts
            chunks = SlackFormatter.format_response(response)
            posted_ts: str | None = None
            for chunk in chunks:
                posted_ts = await self._client.post_message(
                    message.channel_id, text=chunk, thread_ts=reply_thread,
                )

            # Track for regeneration
            if posted_ts and mapping:
                self._track_prompt(
                    posted_ts, mapping.session_id, enriched_text,
                    message.channel_id, message.thread_ts,
                )

        await self._safe_react(message.channel_id, message.ts, "white_check_mark")
