"""Server Services Layer Tests

These tests validate the shared services infrastructure that all
server apps depend on. The services layer owns the session backend
and provides it to all apps via init_services() / get_services().

Exit criteria verified:
1. init_services() creates a ServerServices with MockBackend in dev mode
2. get_services() returns the initialized instance
3. get_services() raises RuntimeError before initialization
4. reset_services() clears the singleton (for test isolation)
5. Custom backend can be injected (for testing)
6. ServerServices extras dict works for extensibility
"""

import pytest

from amplifier_distro.server.services import (
    ServerServices,
    get_services,
    init_services,
    reset_services,
)
from amplifier_distro.server.session_backend import (
    MockBackend,
    SessionBackend,
    SessionInfo,
)


@pytest.fixture(autouse=True)
def _clean_services():
    """Ensure services are reset between tests."""
    reset_services()
    yield
    reset_services()


class TestInitServices:
    """Verify init_services() creates correct backends."""

    def test_dev_mode_uses_mock_backend(self):
        services = init_services(dev_mode=True)
        assert isinstance(services.backend, MockBackend)

    def test_dev_mode_flag_stored(self):
        services = init_services(dev_mode=True)
        assert services.dev_mode is True

    def test_custom_backend_injected(self):
        custom = MockBackend()
        services = init_services(backend=custom)
        assert services.backend is custom

    def test_returns_server_services_instance(self):
        services = init_services(dev_mode=True)
        assert isinstance(services, ServerServices)


class TestGetServices:
    """Verify get_services() retrieval and error handling."""

    def test_raises_before_init(self):
        with pytest.raises(RuntimeError, match="not initialized"):
            get_services()

    def test_returns_initialized_instance(self):
        original = init_services(dev_mode=True)
        retrieved = get_services()
        assert retrieved is original

    def test_returns_same_instance_on_repeated_calls(self):
        init_services(dev_mode=True)
        first = get_services()
        second = get_services()
        assert first is second


class TestResetServices:
    """Verify reset_services() clears the singleton."""

    def test_reset_makes_get_raise(self):
        init_services(dev_mode=True)
        get_services()  # Should work
        reset_services()
        with pytest.raises(RuntimeError):
            get_services()


class TestServerServicesExtras:
    """Verify the extras dict for extensibility."""

    def test_set_and_get_extra(self):
        services = init_services(dev_mode=True)
        services["discovery"] = "mock-discovery"
        assert services["discovery"] == "mock-discovery"

    def test_get_with_default(self):
        services = init_services(dev_mode=True)
        assert services.get("nonexistent") is None
        assert services.get("nonexistent", 42) == 42

    def test_missing_extra_raises_key_error(self):
        services = init_services(dev_mode=True)
        with pytest.raises(KeyError):
            _ = services["missing"]


class TestSessionBackendProtocol:
    """Verify MockBackend conforms to SessionBackend protocol."""

    def test_mock_backend_is_session_backend(self):
        backend = MockBackend()
        assert isinstance(backend, SessionBackend)

    def test_session_info_dataclass(self):
        info = SessionInfo(
            session_id="test-123",
            project_id="proj",
            working_dir="/tmp",
        )
        assert info.session_id == "test-123"
        assert info.is_active is True
        assert info.created_by_app == ""


class TestMockBackendOperations:
    """Verify MockBackend CRUD operations."""

    @pytest.fixture
    def backend(self):
        return MockBackend()

    @pytest.mark.asyncio
    async def test_create_session(self, backend):
        info = await backend.create_session(
            working_dir="/tmp",
            description="test session",
        )
        assert info.session_id.startswith("mock-session-")
        assert info.is_active is True
        assert info.working_dir == "/tmp"

    @pytest.mark.asyncio
    async def test_send_message(self, backend):
        info = await backend.create_session()
        response = await backend.send_message(info.session_id, "hello")
        assert "hello" in response

    @pytest.mark.asyncio
    async def test_send_message_unknown_session(self, backend):
        with pytest.raises(ValueError, match="Unknown session"):
            await backend.send_message("nonexistent", "hello")

    @pytest.mark.asyncio
    async def test_end_session(self, backend):
        info = await backend.create_session()
        await backend.end_session(info.session_id)
        ended = await backend.get_session_info(info.session_id)
        assert ended is not None
        assert ended.is_active is False

    @pytest.mark.asyncio
    async def test_list_active_sessions(self, backend):
        await backend.create_session(description="a")
        await backend.create_session(description="b")
        info_c = await backend.create_session(description="c")
        await backend.end_session(info_c.session_id)

        active = backend.list_active_sessions()
        assert len(active) == 2

    @pytest.mark.asyncio
    async def test_get_session_info_unknown(self, backend):
        result = await backend.get_session_info("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_custom_response_fn(self, backend):
        backend.set_response_fn(lambda sid, msg: f"Custom: {msg}")
        info = await backend.create_session()
        response = await backend.send_message(info.session_id, "test")
        assert response == "Custom: test"

    @pytest.mark.asyncio
    async def test_calls_recorded(self, backend):
        info = await backend.create_session()
        await backend.send_message(info.session_id, "hi")
        await backend.end_session(info.session_id)

        methods = [c["method"] for c in backend.calls]
        assert methods == [
            "create_session",
            "send_message",
            "end_session",
        ]

    @pytest.mark.asyncio
    async def test_message_history(self, backend):
        info = await backend.create_session()
        await backend.send_message(info.session_id, "first")
        await backend.send_message(info.session_id, "second")

        history = backend.get_message_history(info.session_id)
        assert len(history) == 4  # 2 user + 2 assistant
        assert history[0]["role"] == "user"
        assert history[0]["content"] == "first"
        assert history[1]["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_send_to_unknown_session_still_raises(self, backend):
        """Verify existing behavior: truly unknown session IDs raise ValueError."""
        with pytest.raises(ValueError, match="Unknown session"):
            await backend.send_message("completely-fake-id", "hello")


# ---------------------------------------------------------------------------
# FoundationBackend reconnect lock tests (#20)
# ---------------------------------------------------------------------------


class TestFoundationBackendReconnectLock:
    """Verify that concurrent reconnects for the same session are serialized.

    Mocks _reconnect at the instance level so the lock behavior is tested
    in isolation from real transcript loading and bundle creation.
    """

    @pytest.mark.asyncio
    async def test_concurrent_reconnect_calls_resume_once(self):
        """Two concurrent send_message to missing session = one reconnect."""
        import asyncio
        from pathlib import Path
        from unittest.mock import AsyncMock, MagicMock

        from amplifier_distro.server.session_backend import (
            FoundationBackend,
            _SessionHandle,
        )

        backend = FoundationBackend.__new__(FoundationBackend)
        backend._bundle_name = "test-bundle"
        backend._sessions = {}
        backend._reconnect_locks = {}
        backend._session_queues = {}
        backend._worker_tasks = {}
        backend._ended_sessions = set()
        backend._wired_sessions = set()

        reconnect_count = 0

        async def fake_reconnect(session_id, *, working_dir="~"):
            nonlocal reconnect_count
            reconnect_count += 1
            await asyncio.sleep(0.05)  # simulate work
            mock_session = MagicMock()
            mock_session.session_id = session_id
            mock_session.execute = AsyncMock(return_value=f"response-{session_id}")
            handle = _SessionHandle(
                session_id=session_id,
                project_id="test",
                working_dir=Path(working_dir),
                session=mock_session,
            )
            backend._sessions[session_id] = handle
            queue: asyncio.Queue = asyncio.Queue()
            backend._session_queues[session_id] = queue
            backend._worker_tasks[session_id] = asyncio.create_task(
                FoundationBackend._session_worker(backend, session_id)
            )
            return handle

        backend._reconnect = fake_reconnect

        try:
            results = await asyncio.gather(
                backend.send_message("sess-123", "hello"),
                backend.send_message("sess-123", "world"),
            )

            assert results[0] == "response-sess-123"
            assert results[1] == "response-sess-123"

            assert reconnect_count == 1, (
                f"Expected 1 reconnect, got {reconnect_count}. "
                "The per-session lock should prevent duplicate reconnects."
            )
        finally:
            for t in list(backend._worker_tasks.values()):
                t.cancel()

    @pytest.mark.asyncio
    async def test_cached_session_bypasses_lock(self):
        """Normal send_message with cached handle doesn't touch locks."""
        from unittest.mock import AsyncMock, MagicMock

        from amplifier_distro.server.session_backend import FoundationBackend

        backend = FoundationBackend.__new__(FoundationBackend)
        backend._bundle_name = "test-bundle"
        backend._reconnect_locks = {}
        backend._session_queues = {}
        backend._worker_tasks = {}
        backend._ended_sessions = set()
        backend._wired_sessions = set()

        mock_handle = MagicMock()
        mock_handle.session_id = "sess-456"
        mock_handle.run = AsyncMock(return_value="cached response")

        backend._sessions = {"sess-456": mock_handle}

        reconnect_called = False

        async def fake_reconnect(session_id, *, working_dir="~"):
            nonlocal reconnect_called
            reconnect_called = True

        backend._reconnect = fake_reconnect

        try:
            result = await backend.send_message("sess-456", "hi")
            assert result == "cached response"

            assert not reconnect_called
            assert len(backend._reconnect_locks) == 0
        finally:
            for t in list(backend._worker_tasks.values()):
                t.cancel()

    @pytest.mark.asyncio
    async def test_different_sessions_reconnect_independently(self):
        """Two different missing sessions reconnect in parallel (no blocking)."""
        import asyncio
        from pathlib import Path
        from unittest.mock import AsyncMock, MagicMock

        from amplifier_distro.server.session_backend import (
            FoundationBackend,
            _SessionHandle,
        )

        backend = FoundationBackend.__new__(FoundationBackend)
        backend._bundle_name = "test-bundle"
        backend._sessions = {}
        backend._reconnect_locks = {}
        backend._session_queues = {}
        backend._worker_tasks = {}
        backend._ended_sessions = set()
        backend._wired_sessions = set()

        reconnect_count = 0

        async def fake_reconnect(session_id, *, working_dir="~"):
            nonlocal reconnect_count
            reconnect_count += 1
            await asyncio.sleep(0.05)
            mock_session = MagicMock()
            mock_session.session_id = session_id
            mock_session.execute = AsyncMock(return_value=f"response-{session_id}")
            handle = _SessionHandle(
                session_id=session_id,
                project_id="test",
                working_dir=Path(working_dir),
                session=mock_session,
            )
            backend._sessions[session_id] = handle
            queue: asyncio.Queue = asyncio.Queue()
            backend._session_queues[session_id] = queue
            backend._worker_tasks[session_id] = asyncio.create_task(
                FoundationBackend._session_worker(backend, session_id)
            )
            return handle

        backend._reconnect = fake_reconnect

        try:
            results = await asyncio.gather(
                backend.send_message("sess-A", "hello"),
                backend.send_message("sess-B", "world"),
            )

            assert results[0] == "response-sess-A"
            assert results[1] == "response-sess-B"
            assert reconnect_count == 2
        finally:
            for t in list(backend._worker_tasks.values()):
                t.cancel()

    @pytest.mark.asyncio
    async def test_lock_cleaned_up_after_successful_reconnect(self):
        """Lock entry is removed after successful reconnect."""
        import asyncio
        from pathlib import Path
        from unittest.mock import AsyncMock, MagicMock

        from amplifier_distro.server.session_backend import (
            FoundationBackend,
            _SessionHandle,
        )

        backend = FoundationBackend.__new__(FoundationBackend)
        backend._bundle_name = "test-bundle"
        backend._sessions = {}
        backend._reconnect_locks = {}
        backend._session_queues = {}
        backend._worker_tasks = {}
        backend._ended_sessions = set()
        backend._wired_sessions = set()

        async def fake_reconnect(session_id, *, working_dir="~"):
            mock_session = MagicMock()
            mock_session.session_id = session_id
            mock_session.execute = AsyncMock(return_value="ok")
            handle = _SessionHandle(
                session_id=session_id,
                project_id="test",
                working_dir=Path(working_dir),
                session=mock_session,
            )
            backend._sessions[session_id] = handle
            queue: asyncio.Queue = asyncio.Queue()
            backend._session_queues[session_id] = queue
            backend._worker_tasks[session_id] = asyncio.create_task(
                FoundationBackend._session_worker(backend, session_id)
            )
            return handle

        backend._reconnect = fake_reconnect

        try:
            await backend.send_message("sess-cleanup", "hi")
            assert "sess-cleanup" not in backend._reconnect_locks
        finally:
            for t in list(backend._worker_tasks.values()):
                t.cancel()

    @pytest.mark.asyncio
    async def test_reconnect_failure_cleans_up_lock(self):
        """Lock entry is removed even when reconnect fails."""
        from amplifier_distro.server.session_backend import FoundationBackend

        backend = FoundationBackend.__new__(FoundationBackend)
        backend._bundle_name = "test-bundle"
        backend._sessions = {}
        backend._reconnect_locks = {}
        backend._session_queues = {}
        backend._worker_tasks = {}
        backend._ended_sessions = set()
        backend._wired_sessions = set()

        async def fake_reconnect(session_id, *, working_dir="~"):
            raise FileNotFoundError("session dir gone")

        backend._reconnect = fake_reconnect

        with pytest.raises(FileNotFoundError):
            await backend.send_message("sess-gone", "hello")

        assert "sess-gone" not in backend._reconnect_locks

    @pytest.mark.asyncio
    async def test_reconnect_failure_does_not_deadlock_retry(self):
        """After failed reconnect, a retry can proceed (not deadlocked)."""
        import asyncio
        from pathlib import Path
        from unittest.mock import AsyncMock, MagicMock

        from amplifier_distro.server.session_backend import (
            FoundationBackend,
            _SessionHandle,
        )

        backend = FoundationBackend.__new__(FoundationBackend)
        backend._bundle_name = "test-bundle"
        backend._sessions = {}
        backend._reconnect_locks = {}
        backend._session_queues = {}
        backend._worker_tasks = {}
        backend._ended_sessions = set()
        backend._wired_sessions = set()

        call_count = 0

        async def fake_reconnect(session_id, *, working_dir="~"):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("bridge temporarily down")
            mock_session = MagicMock()
            mock_session.session_id = session_id
            mock_session.execute = AsyncMock(return_value="recovered")
            handle = _SessionHandle(
                session_id=session_id,
                project_id="test",
                working_dir=Path(working_dir),
                session=mock_session,
            )
            backend._sessions[session_id] = handle
            queue: asyncio.Queue = asyncio.Queue()
            backend._session_queues[session_id] = queue
            backend._worker_tasks[session_id] = asyncio.create_task(
                FoundationBackend._session_worker(backend, session_id)
            )
            return handle

        backend._reconnect = fake_reconnect

        # First call fails
        with pytest.raises(RuntimeError):
            await backend.send_message("sess-retry", "attempt 1")

        # Second call should succeed (not deadlocked by stale lock)
        try:
            result = await backend.send_message("sess-retry", "attempt 2")
            assert result == "recovered"
            assert call_count == 2
        finally:
            for t in list(backend._worker_tasks.values()):
                t.cancel()


class TestSessionBackendContract:
    """Document the behavioral contract between surfaces and backends.

    These tests codify the exception semantics that all SessionBackend
    implementations must follow:
    - ValueError from send_message = session is permanently dead
    - Surfaces should deactivate their routing entry on ValueError

    Note: test_get_session_info_after_end_shows_inactive is MockBackend-specific.
    FoundationBackend returns None for ended sessions (handle is popped).
    """

    @pytest.fixture
    def backend(self):
        return MockBackend()

    @pytest.mark.asyncio
    async def test_create_end_send_raises_valueerror(self, backend):
        """The canonical lifecycle: create -> end -> send must raise ValueError.

        This is the contract that surfaces (Slack, Web Chat) rely on to detect
        dead sessions. If this test fails, zombie session detection breaks.
        """
        info = await backend.create_session(description="contract test")
        assert info.is_active is True

        await backend.end_session(info.session_id)

        with pytest.raises(ValueError, match="Unknown session"):
            await backend.send_message(info.session_id, "should fail")

    @pytest.mark.asyncio
    async def test_end_session_is_idempotent(self, backend):
        """Ending an already-ended session must not raise."""
        info = await backend.create_session()
        await backend.end_session(info.session_id)
        # Second end should not raise
        await backend.end_session(info.session_id)

    @pytest.mark.asyncio
    async def test_get_session_info_after_end_shows_inactive(self, backend):
        """get_session_info on ended session returns info with is_active=False.

        NOTE: This is MockBackend-specific behavior. FoundationBackend returns None
        for ended sessions because the handle is popped from _sessions.
        """
        info = await backend.create_session()
        await backend.end_session(info.session_id)

        result = await backend.get_session_info(info.session_id)
        assert result is not None
        assert result.is_active is False

    @pytest.mark.asyncio
    async def test_ended_session_not_in_active_list(self, backend):
        """Ended sessions must not appear in list_active_sessions."""
        info = await backend.create_session()
        await backend.end_session(info.session_id)

        active = backend.list_active_sessions()
        active_ids = [s.session_id for s in active]
        assert info.session_id not in active_ids


# ---------------------------------------------------------------------------
# start_services() — FastAPI startup hook (#task-13)
# ---------------------------------------------------------------------------


class TestStartServices:
    """Verify start_services() calls backend.startup() at server startup."""

    @pytest.mark.asyncio
    async def test_start_services_calls_backend_startup(self):
        """start_services() awaits backend.startup() when it exists."""
        from unittest.mock import AsyncMock

        from amplifier_distro.server.services import start_services

        mock_backend = AsyncMock()
        mock_backend.startup = AsyncMock()

        reset_services()
        init_services(backend=mock_backend)
        await start_services()

        mock_backend.startup.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_start_services_safe_without_startup_method(self):
        """start_services() must not raise when backend has no startup()."""
        from amplifier_distro.server.services import start_services

        reset_services()
        init_services(backend=MockBackend())
        # MockBackend has no startup() — should silently do nothing
        await start_services()

    @pytest.mark.asyncio
    async def test_start_services_safe_before_init(self):
        """start_services() silently does nothing when services not initialized."""
        from amplifier_distro.server.services import start_services

        reset_services()
        # No init_services() call — must not raise
        await start_services()
