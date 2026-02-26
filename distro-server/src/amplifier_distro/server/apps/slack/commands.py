"""Command handlers for the Slack bridge.

Handles two types of commands:
1. Bot commands: Messages mentioning @amp (e.g., "@amp list")
2. Slash commands: /amp <command> (registered with Slack)

Both route through the same CommandHandler, which delegates to
the session manager and discovery service.
"""

from __future__ import annotations

import contextlib
import logging
import re
from dataclasses import dataclass
from typing import Any, ClassVar

from .config import SlackConfig
from .discovery import AmplifierDiscovery
from .formatter import SlackFormatter
from .sessions import SlackSessionManager

logger = logging.getLogger(__name__)


@dataclass
class CommandContext:
    """Context for a command invocation."""

    channel_id: str
    user_id: str
    user_name: str = ""
    thread_ts: str | None = None
    # Raw text of the command (e.g., "list" or "connect abc123")
    raw_text: str = ""


@dataclass
class CommandResult:
    """Result of a command execution."""

    text: str = ""
    blocks: list[dict[str, Any]] | None = None
    ephemeral: bool = False  # Only visible to the command issuer
    thread_ts: str | None = None  # Reply in this thread
    create_thread: bool = False  # Start a new thread for this reply


class CommandHandler:
    """Handles bot commands and slash commands.

    Commands are parsed from the message text after stripping the
    bot mention. The handler routes to the appropriate method.
    """

    COMMANDS: ClassVar[dict[str, str]] = {
        "list": "List recent Amplifier sessions",
        "sessions": "List active bridge sessions",
        "projects": "List known projects",
        "new": "Start a new Amplifier session",
        "connect": "Connect to an existing session",
        "disconnect": "Disconnect from the current session",
        "status": "Show current session status",
        "breakout": "Move session to its own channel",
        "end": "End the current session",
        "help": "Show help",
        "discover": "Discover local sessions on this machine",
        "config": "Show bridge configuration",
        "remember": "Store a memory (e.g., `remember prefer tabs over spaces`)",
        "recall": "Search memories (e.g., `recall architecture decisions`)",
        "work-status": "Show current work log",
    }

    def __init__(
        self,
        session_manager: SlackSessionManager,
        discovery: AmplifierDiscovery,
        config: SlackConfig,
    ) -> None:
        self._sessions = session_manager
        self._discovery = discovery
        self._config = config

    def parse_command(self, text: str, bot_user_id: str = "") -> tuple[str, list[str]]:
        """Parse a command from message text.

        Strips bot mention and extracts command + arguments.
        Returns (command, args) tuple.

        Examples:
            "<@U123> list" -> ("list", [])
            "<@U123> connect abc123" -> ("connect", ["abc123"])
            "new my cool session" -> ("new", ["my", "cool", "session"])
        """
        # Strip bot mention (handles both <@U123> and <@U123|displayname>)
        cleaned = text.strip()
        if bot_user_id:
            cleaned = re.sub(
                rf"<@{re.escape(bot_user_id)}(?:\|[^>]*)?>", "", cleaned
            ).strip()

        # Also strip the bot name
        if self._config.bot_name:
            pattern = rf"^@?{re.escape(self._config.bot_name)}\b"
            cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE).strip()

        parts = cleaned.split()
        if not parts:
            return "help", []

        command = parts[0].lower()
        args = parts[1:]

        # Normalize aliases
        aliases = {
            "ls": "list",
            "start": "new",
            "create": "new",
            "attach": "connect",
            "join": "connect",
            "disconnect": "end",
            "info": "status",
            "quit": "end",
            "stop": "end",
            "close": "end",
            "?": "help",
            "work-status": "work_status",
        }
        command = aliases.get(command, command)

        return command, args

    async def handle(
        self, command: str, args: list[str], ctx: CommandContext
    ) -> CommandResult:
        """Route and execute a command."""
        handler = getattr(self, f"cmd_{command}", None)
        if handler is None:
            return CommandResult(
                text=f"Unknown command: `{command}`. Try `help` for available commands."
            )

        try:
            return await handler(args, ctx)
        except Exception as e:
            logger.exception(f"Error handling command: {command}")
            return CommandResult(text=f"Error: {e}")

    async def cmd_help(self, args: list[str], ctx: CommandContext) -> CommandResult:
        """Show help."""
        return CommandResult(blocks=SlackFormatter.format_help())

    async def cmd_list(self, args: list[str], ctx: CommandContext) -> CommandResult:
        """List recent Amplifier sessions from the filesystem."""
        project_filter = args[0] if args else None
        sessions = self._discovery.list_sessions(
            limit=15, project_filter=project_filter
        )

        if not sessions:
            return CommandResult(text="_No sessions found._")

        session_dicts = [
            {
                "session_id": s.session_id,
                "project": s.project,
                "date_str": s.date_str,
                "name": s.name,
                "description": s.description,
            }
            for s in sessions
        ]
        return CommandResult(blocks=SlackFormatter.format_session_list(session_dicts))

    async def cmd_projects(self, args: list[str], ctx: CommandContext) -> CommandResult:
        """List known projects."""
        projects = self._discovery.list_projects()

        if not projects:
            return CommandResult(text="_No projects found._")

        lines = [
            "*Known Projects:*\n",
            *[
                f"• *{p.project_name}* - {p.session_count} sessions"
                f" (last: {p.last_active})"
                for p in projects
            ],
        ]

        return CommandResult(text="\n".join(lines))

    async def cmd_new(self, args: list[str], ctx: CommandContext) -> CommandResult:
        """Start a new Amplifier session."""
        working_dir: str | None = None

        # Parse optional --dir <path> flag
        if "--dir" in args:
            idx = args.index("--dir")
            if idx + 1 >= len(args):
                return CommandResult(
                    text="Missing value for `--dir`. Usage: `new [--dir <path>] [description]`"
                )
            working_dir = args[idx + 1]
            args = args[:idx] + args[idx + 2 :]

        description = " ".join(args) if args else ""

        try:
            mapping = await self._sessions.create_session(
                channel_id=ctx.channel_id,
                thread_ts=ctx.thread_ts,
                user_id=ctx.user_id,
                description=description,
                working_dir=working_dir,
            )
        except ValueError as e:
            return CommandResult(text=str(e))

        short_id = mapping.session_id[:8]
        text = f"Started new session `{short_id}` in `{mapping.working_dir}`"
        if description:
            text += f"\n_{description}_"
        text += "\nReply in this thread to interact with the session."

        # Show a hint if the session landed in the home directory (unconfigured)
        if mapping.working_dir in ("~", "", "~/"):
            text += (
                "\n_Tip: set `slack.default_working_dir` in distro"
                " settings to default to your project directory._"
            )

        return CommandResult(
            text=text,
            create_thread=self._config.thread_per_session and ctx.thread_ts is None,
        )

    async def cmd_connect(self, args: list[str], ctx: CommandContext) -> CommandResult:
        """Connect to an existing session."""
        if not args:
            return CommandResult(
                text="Usage: `connect <session_id>`\n"
                "Provide the session ID (or first 8 chars)."
            )

        target_id = args[0]

        # Look up the session in discovery
        session = self._discovery.get_session(target_id)

        # Also try prefix match
        if session is None:
            all_sessions = self._discovery.list_sessions(limit=200)
            matches = [s for s in all_sessions if s.session_id.startswith(target_id)]
            if len(matches) == 1:
                session = matches[0]
            elif len(matches) > 1:
                lines = [
                    "Multiple sessions match. Be more specific:\n",
                    *[
                        f"• `{m.session_id[:12]}` - {m.project} ({m.date_str})"
                        for m in matches[:5]
                    ],
                ]
                return CommandResult(text="\n".join(lines))

        if session is None:
            return CommandResult(text=f"Session not found: `{target_id}`")

        # Resume the discovered session in the backend so messages can be
        # routed to the live session process.
        try:
            mapping = await self._sessions.connect_session(
                channel_id=ctx.channel_id,
                thread_ts=ctx.thread_ts,
                user_id=ctx.user_id,
                session_id=session.session_id,  # NEW — triggers resume path
                working_dir=session.project_path,
                description=session.description or session.name,
            )
        except ValueError as e:
            return CommandResult(text=f"Could not resume session `{target_id}`: {e}")

        short_id = mapping.session_id[:8]
        text = f"Connected to session `{short_id}` ({session.project})"
        if session.name:
            text += f"\n_{session.name}_"

        return CommandResult(
            text=text,
            create_thread=self._config.thread_per_session and ctx.thread_ts is None,
        )

    async def cmd_status(self, args: list[str], ctx: CommandContext) -> CommandResult:
        """Show current session status."""
        mapping = self._sessions.get_mapping(ctx.channel_id, ctx.thread_ts)

        if mapping is None:
            # Show user's active sessions
            user_sessions = self._sessions.list_user_sessions(ctx.user_id)
            if not user_sessions:
                return CommandResult(
                    text="_No active sessions. Use `new` to start one._"
                )

            lines = ["*Your active sessions:*\n"]
            for m in user_sessions:
                short_id = m.session_id[:8]
                lines.append(
                    f"• `{short_id}` in <#{m.channel_id}>"
                    f" - {m.description or 'no description'}"
                )
            return CommandResult(text="\n".join(lines))

        return CommandResult(
            blocks=SlackFormatter.format_status(
                session_id=mapping.session_id,
                project=mapping.project_id,
                description=mapping.description,
                is_active=mapping.is_active,
            )
        )

    async def cmd_breakout(self, args: list[str], ctx: CommandContext) -> CommandResult:
        """Move session to its own channel."""
        if ctx.thread_ts is None:
            return CommandResult(
                text="Use `breakout` within a session thread"
                " to move it to its own channel."
            )

        channel_name = args[0] if args else None

        try:
            new_channel = await self._sessions.breakout_to_channel(
                ctx.channel_id, ctx.thread_ts, channel_name
            )
        except ValueError as e:
            return CommandResult(text=str(e))

        if new_channel is None:
            return CommandResult(text="No active session in this thread.")

        return CommandResult(
            text=f"Session moved to <#{new_channel.id}>. Continue there!"
        )

    async def cmd_end(self, args: list[str], ctx: CommandContext) -> CommandResult:
        """End the current session."""
        ended = await self._sessions.end_session(ctx.channel_id, ctx.thread_ts)
        if not ended:
            return CommandResult(text="_No active session to end._")
        return CommandResult(text="Session ended. Start a new one with `new`.")

    async def cmd_discover(self, args: list[str], ctx: CommandContext) -> CommandResult:
        """Discover local sessions - verbose listing with details."""
        limit = 10
        if args:
            with contextlib.suppress(ValueError):
                limit = int(args[0])

        sessions = self._discovery.list_sessions(limit=limit)
        if not sessions:
            return CommandResult(text="_No local sessions found._")

        lines = [f"*Local Amplifier Sessions* (showing {len(sessions)}):\n"]
        for s in sessions:
            short_id = s.session_id[:8]
            line = f"• `{short_id}` | *{s.project}* | {s.date_str}"
            if s.name:
                line += f" | _{s.name}_"
            lines.append(line)

        return CommandResult(text="\n".join(lines))

    async def cmd_sessions(self, args: list[str], ctx: CommandContext) -> CommandResult:
        """List active bridge sessions (Slack-to-Amplifier mappings)."""
        active = self._sessions.list_active()
        if not active:
            return CommandResult(text="_No active bridge sessions._")

        lines = [f"*Active Bridge Sessions* ({len(active)}):\n"]
        for m in active:
            short_id = m.session_id[:8]
            channel_ref = f"<#{m.channel_id}>"
            line = f"• `{short_id}` in {channel_ref}"
            if m.description:
                line += f" - {m.description}"
            lines.append(line)

        return CommandResult(text="\n".join(lines))

    async def cmd_config(self, args: list[str], ctx: CommandContext) -> CommandResult:
        """Show current bridge configuration."""
        lines = [
            "*Bridge Configuration:*\n",
            f"• *Bot name:* {self._config.bot_name}",
        ]
        if self._config.hub_channel_id:
            lines.append(f"• *Hub channel:* <#{self._config.hub_channel_id}>")
        else:
            lines.append("• *Hub channel:* _not set_")
        lines.extend(
            [
                f"• *Thread per session:* {self._config.thread_per_session}",
                f"• *Allow breakout:* {self._config.allow_breakout}",
                f"• *Channel prefix:* `{self._config.channel_prefix}`",
                f"• *Mode:* {self._config.mode}",
            ]
        )
        if self._config.default_bundle:
            lines.append(f"• *Default bundle:* {self._config.default_bundle}")
        return CommandResult(text="\n".join(lines))

    # --- Memory commands ---

    async def cmd_remember(self, args: list[str], ctx: CommandContext) -> CommandResult:
        """Store a memory via the shared memory service."""
        if not args:
            return CommandResult(
                text="Usage: `remember <text>`\n"
                "Example: `remember prefer tabs over spaces`"
            )

        from amplifier_distro.server.memory import get_memory_service

        text = " ".join(args)
        service = get_memory_service()
        result = service.remember(text)

        return CommandResult(
            text=(
                f"Remembered `{result['id']}` "
                f"(category: _{result['category']}_, "
                f"tags: {', '.join(result['tags'])})\n"
                f"> {result['content']}"
            )
        )

    async def cmd_recall(self, args: list[str], ctx: CommandContext) -> CommandResult:
        """Search memories via the shared memory service."""
        if not args:
            return CommandResult(
                text="Usage: `recall <query>`\nExample: `recall architecture decisions`"
            )

        from amplifier_distro.server.memory import get_memory_service

        query = " ".join(args)
        service = get_memory_service()
        results = service.recall(query)

        if not results:
            return CommandResult(text=f"_No memories found matching '{query}'._")

        lines = [
            f"*Found {len(results)} memory(ies):*\n",
            *[f"- `{m['id']}` ({m['category']}) {m['content']}" for m in results],
        ]

        return CommandResult(text="\n".join(lines))

    async def cmd_work_status(
        self, args: list[str], ctx: CommandContext
    ) -> CommandResult:
        """Show the current work log."""
        from amplifier_distro.server.memory import get_memory_service

        service = get_memory_service()
        result = service.work_status()

        items = result.get("items", [])
        if not items:
            return CommandResult(text="_No work log entries._")

        lines = ["*Work Log:*\n"]
        for item in items:
            status_val = item.get("status", "pending")
            task = item.get("task", "")
            lines.append(f"- [{status_val}] {task}")

        return CommandResult(text="\n".join(lines))
