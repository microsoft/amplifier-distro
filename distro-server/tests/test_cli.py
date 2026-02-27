"""Tests for the amp-distro CLI main command group.

Tests cover:
1. Hidden 'watchdog' subcommand visibility
2. watchdog --help exits cleanly
3. watchdog delegates to run_watchdog_loop with correct args

"""

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner


class TestWatchdogSubcommand:
    """Verify the hidden 'watchdog' subcommand behavior."""

    @pytest.mark.xfail(
        reason="RED phase: watchdog CLI subcommand not yet implemented",
        strict=True,
    )
    def test_watchdog_subcommand_hidden(self) -> None:
        """'watchdog' must NOT appear in 'amp-distro --help' output."""
        from amplifier_distro.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["--help"])

        assert result.exit_code == 0
        assert "watchdog" not in result.output
        assert "watchdog" in main.commands

    @pytest.mark.xfail(
        reason="RED phase: watchdog CLI subcommand not yet implemented",
        strict=True,
    )
    def test_watchdog_subcommand_exists(self) -> None:
        """'amp-distro watchdog --help' must succeed with exit code 0."""
        from amplifier_distro.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["watchdog", "--help"])

        assert result.exit_code == 0

    @pytest.mark.xfail(
        reason="RED phase: watchdog CLI subcommand not yet implemented",
        strict=True,
    )
    @patch("amplifier_distro.server.watchdog.run_watchdog_loop")
    def test_watchdog_delegates_to_run_watchdog_loop(
        self, mock_loop: MagicMock
    ) -> None:
        """watchdog --host X --port Y calls run_watchdog_loop(host=X, port=Y)."""
        from amplifier_distro.cli import main

        runner = CliRunner()
        result = runner.invoke(
            main, ["watchdog", "--host", "0.0.0.0", "--port", "9000"]
        )

        assert result.exit_code == 0
        mock_loop.assert_called_once_with(host="0.0.0.0", port=9000)
