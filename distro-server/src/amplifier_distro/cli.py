"""amp-distro CLI - Amplifier Experience Server management tool.

Manages the experience server (web chat, Slack, voice), backup/restore,
platform service registration, and diagnostics.
"""

import json
import sys
from pathlib import Path

import click

from . import conventions


class _EpilogGroup(click.Group):
    """Click group that preserves epilog formatting."""

    def format_epilog(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        if self.epilog:
            formatter.write("\n")
            for line in self.epilog.splitlines():
                formatter.write(f"{line}\n")


EPILOG = """\
Quick-start examples:

  amp-distro serve           Start the experience server (foreground)
  amp-distro serve --dev     Dev mode (mock sessions, no LLM needed)
  amp-distro backup          Back up Amplifier state to GitHub
  amp-distro restore         Restore from backup
  amp-distro service install Register as auto-start service"""


@click.group(
    cls=_EpilogGroup,
    epilog=EPILOG,
    help="Amplifier Experience Server management tool.\n\n"
    "Manages the experience server, backups, and platform service.",
)
@click.version_option(package_name="amplifier-distro")
def main() -> None:
    """Amplifier Experience Server management tool."""


# -- Server --------------------------------------------------------------


@main.command("serve")
@click.option(
    "--host", default="127.0.0.1", help="Bind host (use 0.0.0.0 for LAN/Tailscale)"
)
@click.option(
    "--port", default=conventions.SERVER_DEFAULT_PORT, type=int, help="Bind port"
)
@click.option(
    "--apps-dir", default=None, type=click.Path(exists=True), help="Apps directory"
)
@click.option("--reload", is_flag=True, help="Enable auto-reload for development")
@click.option("--dev", is_flag=True, help="Dev mode: mock session backend (no LLM)")
@click.option(
    "--stub",
    is_flag=True,
    help="Stub mode: serve UI with canned data for fast iteration (implies --dev)",
)
def serve_cmd(
    host: str,
    port: int,
    apps_dir: str | None,
    reload: bool,
    dev: bool,
    stub: bool,
) -> None:
    """Start the experience server."""
    from .server.cli import _run_foreground

    if stub:
        dev = True
    _run_foreground(host, port, apps_dir, reload, dev, stub=stub)


# -- Backup commands -----------------------------------------------------


@main.command("backup", help="Back up Amplifier state to a private GitHub repo.")
@click.option("--name", default="amplifier-backup", help="Backup repo name.")
def backup_cmd(name: str) -> None:
    """Back up Amplifier state to a private GitHub repo."""
    from .backup import _detect_gh_handle, backup

    gh_handle = _detect_gh_handle()
    if not gh_handle:
        click.echo(
            "Error: Could not detect GitHub handle. "
            "Is the gh CLI installed and authenticated?",
            err=True,
        )
        sys.exit(1)

    amplifier_home = Path(conventions.AMPLIFIER_HOME).expanduser()
    click.echo("Starting backup...")
    result = backup(amplifier_home, gh_handle, repo_name=name)

    if result.status == "success":
        click.echo(f"  {result.message}")
        for f in result.files:
            click.echo(f"    {f}")
    else:
        click.echo(f"Backup failed: {result.message}", err=True)
        sys.exit(1)


@main.command("restore", help="Restore Amplifier state from a private GitHub repo.")
@click.option("--name", default="amplifier-backup", help="Backup repo name.")
def restore_cmd(name: str) -> None:
    """Restore Amplifier state from a private GitHub repo."""
    from .backup import _detect_gh_handle, restore

    gh_handle = _detect_gh_handle()
    if not gh_handle:
        click.echo(
            "Error: Could not detect GitHub handle. "
            "Is the gh CLI installed and authenticated?",
            err=True,
        )
        sys.exit(1)

    amplifier_home = Path(conventions.AMPLIFIER_HOME).expanduser()
    click.echo("Starting restore...")
    result = restore(amplifier_home, gh_handle, repo_name=name)

    if result.status == "success":
        click.echo(f"  {result.message}")
        for f in result.files:
            click.echo(f"    {f}")
    else:
        click.echo(f"Restore failed: {result.message}", err=True)
        sys.exit(1)


# -- Doctor command ------------------------------------------------------


@main.command("doctor")
@click.option("--fix", is_flag=True, help="Auto-fix issues that can be resolved.")
@click.option("--json", "as_json", is_flag=True, help="Output machine-readable JSON.")
def doctor_cmd(fix: bool, as_json: bool) -> None:
    """Diagnose and auto-fix common problems.

    Runs a comprehensive suite of checks against the local Amplifier
    installation.  Use --fix to automatically resolve fixable issues
    (missing directories, wrong permissions, stale PID files).
    """
    from .doctor import run_diagnostics, run_fixes

    amplifier_home = Path(conventions.AMPLIFIER_HOME).expanduser()
    report = run_diagnostics(amplifier_home)

    # Apply fixes if requested
    fixes_applied: list[str] = []
    if fix:
        fixes_applied = run_fixes(amplifier_home, report)
        # Re-run diagnostics to show updated state
        if fixes_applied:
            report = run_diagnostics(amplifier_home)

    if as_json:
        _print_doctor_json(report, fixes_applied)
    else:
        _print_doctor_report(report, fixes_applied)

    # Exit non-zero if any errors remain
    if report.summary["error"] > 0:
        sys.exit(1)


def _print_doctor_report(report: object, fixes: list[str]) -> None:
    """Format and print a doctor report with coloured status markers."""
    click.echo("Amplifier Distro - Doctor\n")

    for check in report.checks:  # type: ignore[union-attr]
        if check.status == "ok":
            mark = click.style("\u2714", fg="green")  # checkmark
        elif check.status == "warning":
            mark = click.style("!", fg="yellow")
        else:
            mark = click.style("\u2718", fg="red")  # X

        click.echo(f"  {mark} {check.name}: {check.message}")

        # Show fix suggestion for non-ok checks that have a fix
        if check.status != "ok" and check.fix_available:
            click.echo(click.style(f"    fix: {check.fix_description}", fg="cyan"))

    # Summary
    s = report.summary  # type: ignore[union-attr]
    click.echo(f"\n  {s['ok']} ok, {s['warning']} warning(s), {s['error']} error(s)")

    if fixes:
        click.echo("\nFixes applied:")
        checkmark = click.style("\u2714", fg="green")
        for f in fixes:
            click.echo(f"  {checkmark} {f}")


def _print_doctor_json(report: object, fixes: list[str]) -> None:
    """Print the doctor report as machine-readable JSON."""
    data = {
        "checks": [c.model_dump() for c in report.checks],  # type: ignore[union-attr]
        "summary": report.summary,  # type: ignore[union-attr]
        "fixes_applied": fixes,
    }
    click.echo(json.dumps(data, indent=2))


# -- Service commands ----------------------------------------------------


@main.group("service")
def service_group() -> None:
    """Manage platform auto-start service (systemd/launchd)."""


@service_group.command("install")
@click.option(
    "--no-watchdog",
    is_flag=True,
    help="Install server only, without the health watchdog.",
)
def service_install(no_watchdog: bool) -> None:
    """Install the platform service for auto-start on boot."""
    from .service import install_service

    result = install_service(include_watchdog=not no_watchdog)
    if result.success:
        click.echo(f"Service installed ({result.platform})")
        for detail in result.details:
            click.echo(f"  {detail}")
    else:
        click.echo(f"Failed: {result.message}", err=True)
        for detail in result.details:
            click.echo(f"  {detail}", err=True)
        raise SystemExit(1)


@service_group.command("uninstall")
def service_uninstall() -> None:
    """Remove the platform auto-start service."""
    from .service import uninstall_service

    result = uninstall_service()
    if result.success:
        click.echo(f"Service removed ({result.platform})")
        for detail in result.details:
            click.echo(f"  {detail}")
    else:
        click.echo(f"Failed: {result.message}", err=True)
        raise SystemExit(1)


@service_group.command("status")
def service_cmd_status() -> None:
    """Check platform service status."""
    from .service import service_status

    result = service_status()
    click.echo(f"Platform: {result.platform}")
    click.echo(f"Status: {result.message}")
    for detail in result.details:
        click.echo(f"  {detail}")


# -- Watchdog (hidden: for service supervision only) ---------------------


@main.command("watchdog", hidden=True)
@click.option("--host", default="127.0.0.1", help="Bind host")
@click.option(
    "--port",
    default=conventions.SERVER_DEFAULT_PORT,
    type=int,
    help="Bind port",
)
def watchdog_cmd(host: str, port: int) -> None:
    """Run the health watchdog (for service supervision — not user-facing)."""
    from .server.watchdog import run_watchdog_loop

    run_watchdog_loop(host=host, port=port)
