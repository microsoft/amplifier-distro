"""Format Amplifier output for Slack messages.

Handles:
- Markdown to Slack mrkdwn conversion (with protected regions)
- Long message splitting (Slack has ~4000 char limit)
- Block Kit message formatting for rich UI elements
- Table → structured list conversion

Uses the "protected regions" pattern: code blocks and inline code are
replaced with placeholders before conversion, then restored after, so
regex replacements never mangle code content.
"""

from __future__ import annotations

import re
from typing import Any

# Placeholder prefix unlikely to appear in real text
_PH = "\x00PH"


def _protect_regions(text: str) -> tuple[str, list[str]]:
    """Replace code blocks and inline code with numbered placeholders.

    Returns (text_with_placeholders, list_of_originals).
    """
    regions: list[str] = []

    def _stash(m: re.Match[str]) -> str:
        idx = len(regions)
        regions.append(m.group(0))
        return f"{_PH}{idx}{_PH}"

    # Fenced code blocks first (greedy across lines)
    text = re.sub(r"```[\s\S]*?```", _stash, text)
    # Then inline code (single backtick)
    text = re.sub(r"`[^`\n]+`", _stash, text)
    return text, regions


def _restore_regions(text: str, regions: list[str]) -> str:
    """Put the originals back in place of placeholders."""
    for i, original in enumerate(regions):
        text = text.replace(f"{_PH}{i}{_PH}", original)
    return text


class SlackFormatter:
    """Converts Amplifier output to Slack-compatible formats."""

    @staticmethod
    def markdown_to_slack(text: str) -> str:
        """Convert standard Markdown to Slack mrkdwn format.

        Key differences:
        - Bold: **text** -> *text*
        - Italic: *text* or _text_ -> _text_
        - Strikethrough: ~~text~~ -> ~text~
        - Code blocks: ```lang\\n...``` -> ```\\n...``` (lang stripped)
        - Links: [text](url) -> <url|text>
        - Headers: # text -> *text* (bold, no header support in Slack)
        - Tables: converted to structured key/value lists

        Code blocks and inline code are protected during conversion
        so their contents are never mangled by the regex passes.
        """
        if not text:
            return ""

        # --- Protect code regions ---
        result, regions = _protect_regions(text)

        # --- Convert Markdown tables to readable lists ---
        result = SlackFormatter._convert_tables(result)

        # --- Standard Markdown → Slack mrkdwn ---

        # Convert links: [text](url) -> <url|text>
        result = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"<\2|\1>", result)

        # Convert headers: # text -> *text*
        result = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", result, flags=re.MULTILINE)

        # Convert bold: **text** -> *text* (must come before italic)
        result = re.sub(r"\*\*(.+?)\*\*", r"*\1*", result)

        # Convert strikethrough: ~~text~~ -> ~text~
        result = re.sub(r"~~(.+?)~~", r"~\1~", result)

        # Bullet lists: - item -> bullet item (Slack renders these better)
        # Use a callable replacement to avoid regex escape issues with
        # the unicode bullet character in raw strings.
        result = re.sub(
            r"^(\s*)[-*]\s+",
            lambda m: m.group(1) + "\u2022 ",
            result,
            flags=re.MULTILINE,
        )

        # --- Restore protected regions ---
        result = _restore_regions(result, regions)

        # Strip language hints from fenced code blocks (after restore)
        result = re.sub(r"```\w*\n", "```\n", result)

        return result

    @staticmethod
    def _convert_tables(text: str) -> str:
        """Convert Markdown tables to structured key/value lists.

        Slack mrkdwn doesn't support tables, so we convert:
            | Name | Value |
            |------|-------|
            | Foo  | Bar   |

        Into:
            *Name:* Foo
            *Value:* Bar
            ---
        """
        lines = text.split("\n")
        out: list[str] = []
        i = 0

        while i < len(lines):
            line = lines[i]

            # Detect table header row (contains | delimiters)
            if (
                "|" in line
                and i + 1 < len(lines)
                and re.match(r"^\s*\|[\s\-:|]+\|\s*$", lines[i + 1])
            ):
                headers = [h.strip() for h in line.strip().strip("|").split("|")]
                i += 2  # skip header + separator

                # Process data rows
                while i < len(lines) and "|" in lines[i]:
                    cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                    # Build key: value pairs
                    parts = []
                    for h, c in zip(headers, cells, strict=False):
                        if c:
                            parts.append(f"*{h}:* {c}")
                    out.append("  ".join(parts))
                    i += 1

                # Separator between table and next content
                if out and out[-1] != "":
                    out.append("")
            else:
                out.append(line)
                i += 1

        return "\n".join(out)

    @staticmethod
    def split_message(text: str, max_length: int = 3900) -> list[str]:
        """Split a long message into chunks respecting Slack's limit.

        Tries to split at paragraph boundaries first, then line boundaries,
        then hard-splits as last resort. Never splits inside code blocks.
        """
        if len(text) <= max_length:
            return [text]

        chunks: list[str] = []
        remaining = text

        while remaining:
            if len(remaining) <= max_length:
                chunks.append(remaining)
                break

            # Try to split at paragraph boundary
            split_point = remaining.rfind("\n\n", 0, max_length)

            # Fall back to line boundary
            if split_point < max_length // 2:
                split_point = remaining.rfind("\n", 0, max_length)

            # Hard split as last resort
            if split_point < max_length // 4:
                split_point = max_length

            chunks.append(remaining[:split_point].rstrip())
            remaining = remaining[split_point:].lstrip("\n")

        return chunks

    @staticmethod
    def format_response(text: str, max_length: int = 3900) -> list[str]:
        """Format an Amplifier response for Slack.

        Converts markdown, splits long messages, returns list of
        message texts ready to send.
        """
        converted = SlackFormatter.markdown_to_slack(text)
        return SlackFormatter.split_message(converted, max_length)

    @staticmethod
    def format_session_list(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Format a session list as Slack Block Kit blocks.

        Returns a list of Block Kit blocks for rich display.
        """
        blocks: list[dict[str, Any]] = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "Amplifier Sessions"},
            }
        ]

        if not sessions:
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "_No sessions found._"},
                }
            )
            return blocks

        for session in sessions:
            session_id = session.get("session_id", "unknown")
            short_id = session_id[:8]
            project = session.get("project", "unknown")
            date = session.get("date_str", "")
            name = session.get("name", "")
            desc = session.get("description", "")

            label = f"*{name}*\n" if name else ""
            label += f"`{short_id}` | {project}"
            if date:
                label += f" | {date}"
            if desc:
                label += f"\n{desc}"

            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": label},
                    "accessory": {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Connect"},
                        "value": session_id,
                        "action_id": f"connect_session_{short_id}",
                    },
                }
            )

        return blocks

    @staticmethod
    def format_error(error: str) -> list[dict[str, Any]]:
        """Format an error as Slack Block Kit blocks."""
        return [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":warning: *Error*\n{error}",
                },
            }
        ]

    @staticmethod
    def format_status(
        session_id: str,
        project: str = "",
        description: str = "",
        is_active: bool = True,
    ) -> list[dict[str, Any]]:
        """Format session status as Slack Block Kit blocks."""
        status_emoji = ":large_green_circle:" if is_active else ":white_circle:"
        status_text = "Active" if is_active else "Inactive"

        text = f"{status_emoji} *Session Status: {status_text}*\n"
        text += f"ID: `{session_id[:8]}`\n"
        if project:
            text += f"Project: {project}\n"
        if description:
            text += f"Description: {description}\n"

        return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]

    @staticmethod
    def format_help() -> list[dict[str, Any]]:
        """Format help text as Slack Block Kit blocks."""
        return [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "Amplifier Slack Bridge"},
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "*Commands* (mention @amp or use in a session thread):\n\n"
                        "\u2022 `list` - List recent Amplifier sessions\n"
                        "\u2022 `projects` - List known projects\n"
                        "\u2022 `new [description]` - Start a new session\n"
                        "\u2022 `connect <id>` - Connect to existing session\n"
                        "\u2022 `status` - Show current session status\n"
                        "\u2022 `breakout` - Move session to its own channel\n"
                        "\u2022 `end` - End the current session\n"
                        "\u2022 `help` - Show this help\n"
                        "\n"
                        "*How it works:*\n"
                        "\u2022 Messages in this channel create threads per session\n"
                        "\u2022 Reply in a thread to continue that session\n"
                        "\u2022 Use `breakout` to promote a thread to its own channel\n"
                    ),
                },
            },
        ]
