"""Tests for the SchedulerService.

Covers offset parsing, schedule calculation (simple, negative offset,
past-today rollover, overrides, weekday/weekend patterns, timezone),
and integration scenarios (trigger, config reload, start/stop lifecycle).

Reference dates used throughout:
  2025-06-15  Sunday   (weekend,  weekday() == 6)
  2025-06-16  Monday   (weekday,  weekday() == 0)
  2025-06-17  Tuesday  (weekday,  weekday() == 1)
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
import yaml

# Module path where SchedulerService lives -- used for patching `datetime`.
_MODULE = "amplifier_distro.server.apps.routines"

from amplifier_distro.server.apps.routines import SchedulerService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _patch_now(fake_now):
    """Return a context-manager that patches ``datetime`` in the scheduler module.

    * ``datetime.now()`` returns *fake_now*.
    * ``datetime.combine()`` delegates to the real implementation so the
      scheduler can still construct fire-time datetimes.
    """
    mock_dt = MagicMock()
    mock_dt.now.return_value = fake_now
    mock_dt.combine = datetime.combine
    return patch(f"{_MODULE}.datetime", mock_dt)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_config():
    """Minimal valid routines.yaml config."""
    return {
        "schema_version": "1.0",
        "profile": {
            "timezone": "America/Los_Angeles",
            "events": {
                "wake": "07:00",
                "lunch": {"weekdays": "12:00", "weekends": "12:30"},
            },
            "overrides": {},
        },
        "routines": {
            "morning-report": {
                "trigger": {"event": "wake", "offset": "-30m"},
                "enabled": True,
                "tasks": [
                    {
                        "id": "check-email",
                        "instruction": "Check email",
                        "priority": 1,
                    },
                ],
                "delivery": {"method": "smart"},
            },
        },
    }


@pytest.fixture
def config_file(tmp_path, sample_config):
    """Write *sample_config* to a temporary ``routines.yaml`` and return the path."""
    path = tmp_path / "routines.yaml"
    path.write_text(yaml.dump(sample_config))
    return path


@pytest.fixture
def mock_backend():
    """AsyncMock that satisfies the SessionBackend protocol."""
    backend = AsyncMock()
    backend.create_session.return_value = MagicMock(session_id="test-session-123")
    backend.send_message.return_value = "OK"
    return backend


@pytest.fixture
def scheduler(config_file, mock_backend, sample_config):
    """A SchedulerService with config pre-loaded (no file-watch overhead)."""
    svc = SchedulerService(config_path=config_file, backend=mock_backend)
    svc._config = sample_config
    return svc


# ===================================================================
# Offset parsing (static method -- no instance or time mock needed)
# ===================================================================


class TestParseOffset:
    """Unit tests for SchedulerService._parse_offset."""

    def test_parse_offset_minutes(self):
        """Parse '30m' and '-30m' correctly."""
        assert SchedulerService._parse_offset("30m") == timedelta(minutes=30)
        assert SchedulerService._parse_offset("-30m") == timedelta(minutes=-30)

    def test_parse_offset_hours(self):
        """Parse '1h' and '-2h' correctly."""
        assert SchedulerService._parse_offset("1h") == timedelta(hours=1)
        assert SchedulerService._parse_offset("-2h") == timedelta(hours=-2)

    def test_parse_offset_zero(self):
        """Zero and empty offsets return a zero timedelta."""
        assert SchedulerService._parse_offset("0m") == timedelta()
        assert SchedulerService._parse_offset("") == timedelta()

    def test_parse_offset_invalid_returns_zero(self):
        """Unrecognised formats silently return zero timedelta."""
        assert SchedulerService._parse_offset("abc") == timedelta()
        assert SchedulerService._parse_offset("30x") == timedelta()


# ===================================================================
# Schedule calculation
# ===================================================================


class TestNextFire:
    """Tests for _calculate_next_fire and _resolve_event_time.

    Reference dates:
      2025-06-15  Sunday   (weekend)
      2025-06-16  Monday   (weekday)
    """

    # -- simple / offset ---------------------------------------------------

    def test_next_fire_simple(self, scheduler):
        """Simple event + offset calculates correctly."""
        tz = ZoneInfo("America/Los_Angeles")
        fake_now = datetime(2025, 6, 16, 5, 0, tzinfo=tz)  # Mon 05:00

        with _patch_now(fake_now):
            routine = scheduler._config["routines"]["morning-report"]
            result = scheduler._calculate_next_fire(routine)

        # wake=07:00 minus 30m => 06:30 today
        assert result == datetime(2025, 6, 16, 6, 30, tzinfo=tz)

    def test_next_fire_negative_offset(self, scheduler):
        """Negative offset fires before event time."""
        tz = ZoneInfo("America/Los_Angeles")
        fake_now = datetime(2025, 6, 16, 5, 0, tzinfo=tz)

        with _patch_now(fake_now):
            routine = scheduler._config["routines"]["morning-report"]
            result = scheduler._calculate_next_fire(routine)

        event_time = datetime(2025, 6, 16, 7, 0, tzinfo=tz)
        assert result < event_time  # fires BEFORE the event
        assert result == datetime(2025, 6, 16, 6, 30, tzinfo=tz)

    def test_next_fire_past_today(self, scheduler):
        """If time already passed today, schedules for tomorrow."""
        tz = ZoneInfo("America/Los_Angeles")
        # 08:00 is well past the 06:30 fire time
        fake_now = datetime(2025, 6, 16, 8, 0, tzinfo=tz)

        with _patch_now(fake_now):
            routine = scheduler._config["routines"]["morning-report"]
            result = scheduler._calculate_next_fire(routine)

        expected = datetime(2025, 6, 17, 6, 30, tzinfo=tz)
        assert result == expected
        assert result > fake_now

    # -- overrides ---------------------------------------------------------

    def test_next_fire_with_override(self, scheduler, sample_config):
        """Override date uses override time."""
        tz = ZoneInfo("America/Los_Angeles")
        sample_config["profile"]["overrides"]["wake"] = {
            "date": "2025-06-16",
            "time": "09:00",
        }
        scheduler._config = sample_config

        fake_now = datetime(2025, 6, 16, 5, 0, tzinfo=tz)

        with _patch_now(fake_now):
            result = scheduler._calculate_next_fire(
                scheduler._config["routines"]["morning-report"]
            )

        # overridden wake 09:00 minus 30m => 08:30
        assert result == datetime(2025, 6, 16, 8, 30, tzinfo=tz)

    # -- weekday / weekend patterns ----------------------------------------

    def test_next_fire_weekday_pattern(self, scheduler, sample_config):
        """Weekday pattern schedules correctly on weekday."""
        tz = ZoneInfo("America/Los_Angeles")
        # Add a lunch routine that uses the weekday/weekend event
        sample_config["routines"]["lunch-break"] = {
            "trigger": {"event": "lunch", "offset": "0m"},
            "enabled": True,
            "tasks": [
                {"id": "lunch", "instruction": "Lunch reminder", "priority": 1},
            ],
        }
        scheduler._config = sample_config

        fake_now = datetime(2025, 6, 16, 10, 0, tzinfo=tz)  # Monday
        assert fake_now.weekday() == 0  # sanity check

        with _patch_now(fake_now):
            result = scheduler._calculate_next_fire(
                sample_config["routines"]["lunch-break"]
            )

        # Weekday lunch = 12:00
        assert result == datetime(2025, 6, 16, 12, 0, tzinfo=tz)

    def test_next_fire_weekend_pattern(self, scheduler, sample_config):
        """Weekend pattern schedules correctly on weekend."""
        tz = ZoneInfo("America/Los_Angeles")
        sample_config["routines"]["lunch-break"] = {
            "trigger": {"event": "lunch", "offset": "0m"},
            "enabled": True,
            "tasks": [
                {"id": "lunch", "instruction": "Lunch reminder", "priority": 1},
            ],
        }
        scheduler._config = sample_config

        fake_now = datetime(2025, 6, 15, 10, 0, tzinfo=tz)  # Sunday
        assert fake_now.weekday() == 6  # sanity check

        with _patch_now(fake_now):
            result = scheduler._calculate_next_fire(
                sample_config["routines"]["lunch-break"]
            )

        # Weekend lunch = 12:30
        assert result == datetime(2025, 6, 15, 12, 30, tzinfo=tz)

    # -- timezone ----------------------------------------------------------

    def test_next_fire_timezone(self, scheduler, sample_config):
        """Fire times respect user timezone."""
        tz_ny = ZoneInfo("America/New_York")
        sample_config["profile"]["timezone"] = "America/New_York"
        scheduler._config = sample_config

        fake_now = datetime(2025, 6, 16, 5, 0, tzinfo=tz_ny)

        with _patch_now(fake_now):
            result = scheduler._calculate_next_fire(
                scheduler._config["routines"]["morning-report"]
            )

        # wake=07:00 ET minus 30m => 06:30 ET
        expected = datetime(2025, 6, 16, 6, 30, tzinfo=tz_ny)
        assert result == expected
        assert result.tzinfo is not None


# ===================================================================
# Overrides
# ===================================================================


class TestOverrides:
    """Override resolution and auto-expiry tests."""

    def test_override_auto_expiry(self, scheduler, sample_config):
        """Expired overrides are cleaned up (past-date override is ignored)."""
        tz = ZoneInfo("America/Los_Angeles")
        # Override for a date that has already passed
        sample_config["profile"]["overrides"]["wake"] = {
            "date": "2025-06-10",
            "time": "10:00",
        }
        scheduler._config = sample_config

        fake_now = datetime(2025, 6, 16, 5, 0, tzinfo=tz)

        with _patch_now(fake_now):
            result = scheduler._calculate_next_fire(
                scheduler._config["routines"]["morning-report"]
            )

        # Override for June 10 must NOT affect June 16; default wake applies
        assert result == datetime(2025, 6, 16, 6, 30, tzinfo=tz)

    def test_resolve_event_time_override_match(self, scheduler, sample_config):
        """Override matching the requested date is used."""
        sample_config["profile"]["overrides"]["wake"] = {
            "date": "2025-06-16",
            "time": "09:00",
        }
        scheduler._config = sample_config

        assert scheduler._resolve_event_time("wake", date(2025, 6, 16)) == "09:00"

    def test_resolve_event_time_override_no_match(self, scheduler, sample_config):
        """Override for a different date is ignored."""
        sample_config["profile"]["overrides"]["wake"] = {
            "date": "2025-06-20",
            "time": "09:00",
        }
        scheduler._config = sample_config

        assert scheduler._resolve_event_time("wake", date(2025, 6, 16)) == "07:00"

    def test_resolve_unknown_event_raises(self, scheduler):
        """Requesting an undefined event raises ValueError."""
        with pytest.raises(ValueError, match="not found"):
            scheduler._resolve_event_time("dinner", date(2025, 6, 16))


# ===================================================================
# Integration
# ===================================================================


class TestIntegration:
    """Higher-level tests that exercise async paths and backend calls."""

    @pytest.mark.asyncio
    async def test_create_trigger_report_saved(self, scheduler, mock_backend):
        """Create routine -> trigger -> verify session created."""
        result = await scheduler.trigger_now("morning-report")

        assert result == "Routine 'morning-report' triggered"
        mock_backend.create_session.assert_called_once()
        mock_backend.send_message.assert_called_once()

        # Session id forwarded correctly to send_message
        msg_args = mock_backend.send_message.call_args.args
        assert msg_args[0] == "test-session-123"
        assert "morning-report" in msg_args[1]

    @pytest.mark.asyncio
    async def test_trigger_nonexistent_routine(self, scheduler):
        """Triggering a non-existent routine returns an error message."""
        result = await scheduler.trigger_now("does-not-exist")
        assert "not found" in result

    @pytest.mark.asyncio
    async def test_config_reload(self, config_file, mock_backend, sample_config):
        """Modify config while scheduler is running, verify reschedule."""
        svc = SchedulerService(config_path=config_file, backend=mock_backend)
        svc._load_config()

        assert "morning-report" in svc._config.get("routines", {})

        # Append a second routine to the config file
        sample_config["routines"]["evening-summary"] = {
            "trigger": {"event": "wake", "offset": "12h"},
            "enabled": True,
            "tasks": [
                {
                    "id": "summarize",
                    "instruction": "Summarize the day",
                    "priority": 1,
                },
            ],
        }
        config_file.write_text(yaml.dump(sample_config))

        # Simulate what the config watcher does on mtime change
        svc._load_config()

        assert "evening-summary" in svc._config["routines"]
        assert "morning-report" in svc._config["routines"]

    @pytest.mark.asyncio
    async def test_start_stop(self, config_file, mock_backend):
        """Scheduler starts and stops cleanly."""
        svc = SchedulerService(config_path=config_file, backend=mock_backend)

        with patch.object(svc, "_auto_archive", new_callable=AsyncMock):
            await svc.start()

            assert svc._running is True
            assert len(svc._tasks) > 0
            assert svc._watch_task is not None

            await svc.stop()

            assert svc._running is False
            assert len(svc._tasks) == 0

    def test_status_shape(self, scheduler):
        """status() returns expected keys for each routine."""
        tz = ZoneInfo("America/Los_Angeles")
        fake_now = datetime(2025, 6, 16, 5, 0, tzinfo=tz)

        with _patch_now(fake_now):
            status = scheduler.status()

        assert "morning-report" in status
        entry = status["morning-report"]
        assert entry["enabled"] is True
        assert entry["task_count"] == 1
        assert entry["delivery_method"] == "smart"
        assert "next_fire" in entry

    @pytest.mark.asyncio
    async def test_disabled_routine_not_scheduled(
        self, config_file, mock_backend, sample_config
    ):
        """Disabled routines are skipped during scheduling."""
        sample_config["routines"]["morning-report"]["enabled"] = False
        config_file.write_text(yaml.dump(sample_config))

        svc = SchedulerService(config_path=config_file, backend=mock_backend)
        svc._load_config()

        tz = ZoneInfo("America/Los_Angeles")
        with _patch_now(datetime(2025, 6, 16, 5, 0, tzinfo=tz)):
            svc._schedule_all()

        assert "morning-report" not in svc._tasks

    @pytest.mark.asyncio
    async def test_missing_config_file(self, tmp_path, mock_backend):
        """Missing config file results in empty config, no crash."""
        svc = SchedulerService(
            config_path=tmp_path / "nonexistent.yaml", backend=mock_backend
        )
        svc._load_config()
        assert svc._config == {}
