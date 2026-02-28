"""Tests for the amp-distro CLI top-level commands.

Tests cover:
1. watchdog subcommand is hidden from --help
2. watchdog --help exits successfully (command exists)
3. watchdog subcommand delegates to run_watchdog_loop with correct kwargs
"""

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from amplifier_distro.cli import main


class TestWatchdogCommand:
    """Verify the hidden watchdog subcommand on the main CLI group."""

    def test_watchdog_hidden_from_help(self) -> None:
        """'amp-distro --help' must NOT list the watchdog command."""
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "watchdog" not in result.output

    def test_watchdog_help_exits_zero(self) -> None:
        """'amp-distro watchdog --help' must exit with code 0 (command exists)."""
        runner = CliRunner()
        result = runner.invoke(main, ["watchdog", "--help"])
        assert result.exit_code == 0

    def test_watchdog_calls_run_watchdog_loop_with_host_and_port(self) -> None:
        """watchdog subcommand calls run_watchdog_loop with correct host and port."""
        runner = CliRunner()
        mock_loop = MagicMock()
        # Patch the module where the symbol lives, not amplifier_distro.cli, because
        # the import happens inside the function body (lazy import) — at call time the
        # name is resolved from amplifier_distro.server.watchdog, not the CLI module.
        with patch("amplifier_distro.server.watchdog.run_watchdog_loop", mock_loop):
            result = runner.invoke(
                main,
                [
                    "watchdog",
                    "--host",
                    "0.0.0.0",
                    "--port",
                    "9999",
                ],
            )

        assert result.exit_code == 0
        mock_loop.assert_called_once_with(host="0.0.0.0", port=9999)
