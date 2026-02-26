"""Routines app -- scheduled AI task execution.

Provides the SchedulerService (background task scheduling) and REST API
for routine management within the distro server.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from fastapi import APIRouter, HTTPException

from amplifier_distro.server.app import AppManifest
from amplifier_distro.server.services import get_services
from amplifier_distro.server.session_backend import SessionBackend

logger = logging.getLogger(__name__)

ROUTINES_CONFIG = Path.home() / ".amplifier" / "routines.yaml"
REPORTS_DIR = Path.home() / ".amplifier" / "routines" / "reports"


# ---------------------------------------------------------------------------
# SchedulerService
# ---------------------------------------------------------------------------


class SchedulerService:
    """Manages routine scheduling within the distro server."""

    def __init__(self, config_path: Path, backend: SessionBackend) -> None:
        self.config_path = config_path
        self.backend = backend
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._config: dict = {}
        self._last_run: dict[str, str] = {}
        self._watch_task: asyncio.Task[None] | None = None
        self._running = False

    # -- Lifecycle --

    async def start(self) -> None:
        self._running = True
        self._load_config()
        self._schedule_all()
        self._watch_task = asyncio.create_task(self._watch_config())
        await self._auto_archive()
        await self._clean_expired_overrides()
        logger.info(
            "SchedulerService started with %d routines",
            len(self._tasks),
        )

    async def stop(self) -> None:
        self._running = False
        if self._watch_task:
            self._watch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watch_task
        for task in self._tasks.values():
            task.cancel()
        for task in self._tasks.values():
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        logger.info("SchedulerService stopped")

    # -- Config I/O --

    def _load_config(self) -> None:
        if not self.config_path.exists():
            self._config = {}
            return
        try:
            with open(self.config_path) as f:
                self._config = yaml.safe_load(f) or {}
        except yaml.YAMLError:
            logger.exception("Failed to parse %s", self.config_path)
            self._config = {}

    def _save_config(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.config_path, "w") as f:
            yaml.dump(
                self._config,
                f,
                default_flow_style=False,
                sort_keys=False,
            )

    # -- Scheduling --

    def _schedule_all(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()

        for name, routine in self._config.get("routines", {}).items():
            if not routine.get("enabled", True):
                continue
            try:
                next_fire = self._calculate_next_fire(routine)
                self._tasks[name] = asyncio.create_task(
                    self._wait_and_fire(name, routine, next_fire)
                )
                logger.info("Scheduled %s for %s", name, next_fire.isoformat())
            except Exception:
                logger.exception("Failed to schedule routine: %s", name)

    def _get_timezone(self) -> ZoneInfo:
        tz_str = self._config.get("profile", {}).get("timezone", "UTC")
        return ZoneInfo(tz_str)

    def _calculate_next_fire(self, routine: dict) -> datetime:
        tz = self._get_timezone()
        now = datetime.now(tz)
        trigger = routine["trigger"]
        event_name = trigger["event"]

        for days_ahead in range(8):
            check_date = (now + timedelta(days=days_ahead)).date()
            try:
                event_time_str = self._resolve_event_time(event_name, check_date)
            except ValueError:
                continue

            hour, minute = map(int, event_time_str.split(":"))
            fire_dt = datetime.combine(check_date, time(hour, minute), tzinfo=tz)

            offset_str = trigger.get("offset", "0m")
            fire_dt += self._parse_offset(offset_str)

            if fire_dt > now:
                return fire_dt

        # Fallback: tomorrow
        tomorrow = (now + timedelta(days=1)).date()
        event_time_str = self._resolve_event_time(event_name, tomorrow)
        hour, minute = map(int, event_time_str.split(":"))
        fire_dt = datetime.combine(tomorrow, time(hour, minute), tzinfo=tz)
        fire_dt += self._parse_offset(trigger.get("offset", "0m"))
        return fire_dt

    def _resolve_event_time(self, event_name: str, for_date: date) -> str:
        overrides = self._config.get("profile", {}).get("overrides", {})
        if event_name in overrides:
            override = overrides[event_name]
            if override.get("date") == for_date.isoformat():
                return override["time"]

        events = self._config.get("profile", {}).get("events", {})
        event = events.get(event_name)
        if event is None:
            raise ValueError(f"Event '{event_name}' not found")

        if isinstance(event, str):
            return event

        is_weekend = for_date.weekday() >= 5
        if is_weekend and "weekends" in event:
            return event["weekends"]
        elif not is_weekend and "weekdays" in event:
            return event["weekdays"]
        return event.get("weekdays") or event.get("weekends", "00:00")

    @staticmethod
    def _parse_offset(offset_str: str) -> timedelta:
        if not offset_str or offset_str == "0m":
            return timedelta()
        match = re.match(r"^(-?)(\d+)(m|h)$", offset_str.strip())
        if not match:
            return timedelta()
        sign = -1 if match.group(1) == "-" else 1
        value = int(match.group(2))
        unit = match.group(3)
        if unit == "h":
            return timedelta(hours=value * sign)
        return timedelta(minutes=value * sign)

    # -- Execution --

    async def _wait_and_fire(
        self, name: str, routine: dict, fire_time: datetime
    ) -> None:
        tz = self._get_timezone()
        now = datetime.now(tz)
        delay = (fire_time - now).total_seconds()

        if delay > 0:
            logger.debug(
                "Routine %s sleeping %.0f seconds until %s",
                name,
                delay,
                fire_time,
            )
            await asyncio.sleep(delay)

        if not self._running:
            return

        try:
            await self._execute_routine(name, routine)
            self._last_run[name] = datetime.now(tz).isoformat()
        except Exception:
            logger.exception("Failed to execute routine: %s", name)

        if self._running:
            self._load_config()
            updated = self._config.get("routines", {}).get(name)
            if updated and updated.get("enabled", True):
                next_fire = self._calculate_next_fire(updated)
                self._tasks[name] = asyncio.create_task(
                    self._wait_and_fire(name, updated, next_fire)
                )

    async def _execute_routine(self, name: str, routine: dict) -> None:
        logger.info("Executing routine: %s", name)
        today = date.today().isoformat()

        session = await self.backend.create_session(
            working_dir=str(Path.home()),
            bundle_name="routines",
            description=f"Routine execution: {name}",
        )

        prompt = (
            f"Run my {name} routine. "
            f"Read ~/.amplifier/routines.yaml for the task list. "
            f"Today is {today}. Execute all tasks, build the report, "
            f"save it to ~/.amplifier/routines/reports/{today}/{name}.md"
            f", and handle delivery."
        )

        await self.backend.send_message(session.session_id, prompt)
        logger.info(
            "Routine %s execution session created: %s",
            name,
            session.session_id,
        )

    # -- Config Watching --

    async def _watch_config(self) -> None:
        last_mtime = self._get_mtime()
        while self._running:
            await asyncio.sleep(60)
            current_mtime = self._get_mtime()
            if current_mtime != last_mtime:
                logger.info("routines.yaml changed, rescheduling")
                last_mtime = current_mtime
                self._load_config()
                self._schedule_all()

    def _get_mtime(self) -> float:
        try:
            return self.config_path.stat().st_mtime
        except FileNotFoundError:
            return 0.0

    # -- Maintenance --

    async def _auto_archive(self) -> None:
        if not REPORTS_DIR.exists():
            return
        archive_dir = REPORTS_DIR / "archive"
        cutoff = date.today() - timedelta(days=30)

        for entry in REPORTS_DIR.iterdir():
            if entry.name == "archive" or not entry.is_dir():
                continue
            try:
                dir_date = date.fromisoformat(entry.name)
                if dir_date < cutoff:
                    dest = archive_dir / entry.name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    entry.rename(dest)
                    logger.info("Archived reports: %s", entry.name)
            except ValueError:
                continue

    async def _clean_expired_overrides(self) -> None:
        overrides = self._config.get("profile", {}).get("overrides", {})
        today = date.today().isoformat()
        expired = [name for name, o in overrides.items() if o.get("date", "") < today]
        if expired:
            for name in expired:
                del overrides[name]
            self._save_config()
            logger.info("Cleaned expired overrides: %s", expired)

    # -- Public API --

    async def trigger_now(self, name: str) -> str:
        routine = self._config.get("routines", {}).get(name)
        if not routine:
            return f"Routine '{name}' not found"
        await self._execute_routine(name, routine)
        return f"Routine '{name}' triggered"

    def status(self) -> dict:
        result = {}
        for name, routine in self._config.get("routines", {}).items():
            try:
                next_fire = self._calculate_next_fire(routine)
                next_fire_str = next_fire.isoformat()
            except Exception:
                next_fire_str = "unknown"

            result[name] = {
                "enabled": routine.get("enabled", True),
                "next_fire": next_fire_str,
                "last_run": self._last_run.get(name),
                "trigger": routine.get("trigger", {}),
                "delivery_method": routine.get("delivery", {}).get("method", "smart"),
                "task_count": len(routine.get("tasks", [])),
            }
        return result

    def health(self) -> dict:
        return {
            "status": "running" if self._running else "stopped",
            "routines_scheduled": len(self._tasks),
            "config_loaded": bool(self._config),
        }


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------

router = APIRouter()


def _get_scheduler() -> SchedulerService:
    services = get_services()
    scheduler = services.get("scheduler")
    if not scheduler:
        raise HTTPException(503, "Scheduler service not available")
    return scheduler


@router.get("/status")
async def get_routine_status():
    return _get_scheduler().status()


@router.post("/trigger/{name}")
async def trigger_routine(name: str):
    result = await _get_scheduler().trigger_now(name)
    return {"message": result}


@router.get("/reports")
async def list_reports(report_date: str | None = None):
    if not REPORTS_DIR.exists():
        return {"reports": []}

    if report_date:
        date_dir = REPORTS_DIR / report_date
        if not date_dir.exists():
            date_dir = REPORTS_DIR / "archive" / report_date
        if not date_dir.exists():
            return {"reports": []}
        return {
            "date": report_date,
            "reports": [f.stem for f in date_dir.glob("*.md")],
        }

    dates = [
        entry.name
        for entry in sorted(REPORTS_DIR.iterdir(), reverse=True)
        if entry.is_dir() and entry.name != "archive"
    ]
    return {"dates": dates[:30]}


@router.get("/reports/{report_date}/{name}")
async def get_report(report_date: str, name: str):
    report_path = REPORTS_DIR / report_date / f"{name}.md"
    if not report_path.exists():
        report_path = REPORTS_DIR / "archive" / report_date / f"{name}.md"
    if not report_path.exists():
        raise HTTPException(404, f"Report not found: {name} for {report_date}")
    return {
        "date": report_date,
        "routine": name,
        "content": report_path.read_text(),
    }


@router.get("/health")
async def scheduler_health():
    return _get_scheduler().health()


# ---------------------------------------------------------------------------
# App Lifecycle
# ---------------------------------------------------------------------------

_scheduler: SchedulerService | None = None


async def on_startup() -> None:
    global _scheduler
    services = get_services()
    backend = services.backend
    _scheduler = SchedulerService(config_path=ROUTINES_CONFIG, backend=backend)
    services["scheduler"] = _scheduler
    await _scheduler.start()


async def on_shutdown() -> None:
    if _scheduler is not None:
        await _scheduler.stop()


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

manifest = AppManifest(
    name="routines",
    description=("Scheduled AI task execution with natural language management"),
    version="0.1.0",
    router=router,
    on_startup=on_startup,
    on_shutdown=on_shutdown,
)
