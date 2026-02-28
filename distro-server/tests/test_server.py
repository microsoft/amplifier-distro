"""Server Acceptance Tests

These tests validate the distro server with its app/plugin system.
The server uses FastAPI with a plugin architecture where apps register
themselves and get mounted at designated paths.

Exit criteria verified:
1. DistroServer creates a FastAPI app with correct metadata
2. Core routes exist: /api/health, /api/config, /api/status, /api/apps
3. register_app() adds an app and mounts its routes
4. register_app() rejects duplicate names with ValueError
5. discover_apps() finds apps in a directory (and ignores non-packages)
6. Example app has correct manifest structure
7. Health endpoint returns {"status": "ok"} with version
8. Apps endpoint lists registered apps with metadata
9. App routes are mounted and accessible at /apps/{name}/
"""

from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI
from starlette.testclient import TestClient

from amplifier_distro.server.app import AppManifest, DistroServer, create_server


class TestDistroServerCreation:
    """Verify DistroServer creates a properly configured FastAPI app.

    Antagonist note: The server is the central coordination point for
    all remote interfaces. It must expose a FastAPI instance with correct
    metadata and be creatable via the factory function.
    """

    def test_creates_fastapi_app(self):
        server = DistroServer()
        assert isinstance(server.app, FastAPI)

    def test_app_has_correct_title(self):
        server = DistroServer(title="Test Distro")
        assert server.app.title == "Test Distro"

    def test_default_title(self):
        server = DistroServer()
        assert server.app.title == "Amplifier Distro"

    def test_app_has_correct_version(self):
        server = DistroServer(version="1.0.0")
        assert server.app.version == "1.0.0"

    def test_default_version(self):
        server = DistroServer()
        assert server.app.version == "0.1.0"

    def test_create_server_factory(self):
        """create_server() is the main entry point for starting the server."""
        server = create_server()
        assert isinstance(server, DistroServer)
        assert isinstance(server.app, FastAPI)

    def test_no_apps_registered_by_default(self):
        server = DistroServer()
        assert server.apps == {}


class TestCoreRoutes:
    """Verify all core routes are registered at the correct paths.

    Antagonist note: We check route existence by inspecting the FastAPI
    route table directly. Each path must exist exactly as specified.
    """

    @pytest.fixture
    def server(self):
        return DistroServer()

    def _route_paths(self, server):
        return [getattr(r, "path", None) for r in server.app.routes]

    def test_health_route_exists(self, server):
        assert "/api/health" in self._route_paths(server)

    def test_sessions_route_exists(self, server):
        assert "/api/sessions" in self._route_paths(server)

    def test_integrations_route_exists(self, server):
        assert "/api/integrations" in self._route_paths(server)

    def test_apps_route_exists(self, server):
        assert "/api/apps" in self._route_paths(server)

    def test_all_four_core_routes_present(self, server):
        """All four core routes must be present simultaneously."""
        paths = self._route_paths(server)
        required = ["/api/health", "/api/sessions", "/api/integrations", "/api/apps"]
        for route in required:
            assert route in paths, f"Missing core route: {route}"


class TestRegisterApp:
    """Verify app registration adds routes and rejects duplicates.

    Antagonist note: register_app() has two key invariants:
    1. A registered app's router is mounted at /apps/{name}
    2. Duplicate names are rejected with ValueError
    These are structural guarantees that prevent app collisions.
    """

    def test_register_adds_app_to_apps_dict(self):
        server = DistroServer()
        router = APIRouter()

        @router.get("/")
        async def index():
            return {"msg": "hello"}

        manifest = AppManifest(
            name="test-app",
            description="Test app",
            router=router,
        )
        server.register_app(manifest)
        assert "test-app" in server.apps

    def test_register_sets_mount_path(self):
        server = DistroServer()
        manifest = AppManifest(name="my-app", description="My app")
        server.register_app(manifest)
        assert server.apps["my-app"].mount_path == "/apps/my-app"

    def test_register_rejects_duplicate_names(self):
        server = DistroServer()
        manifest1 = AppManifest(name="dup", description="first")
        manifest2 = AppManifest(name="dup", description="second")
        server.register_app(manifest1)
        with pytest.raises(ValueError, match="App already registered: dup"):
            server.register_app(manifest2)

    def test_register_mounts_router_at_correct_path(self):
        """Routes from the app's router must appear at /apps/{name}/..."""
        server = DistroServer()
        router = APIRouter()

        @router.get("/ping")
        async def ping():
            return {"pong": True}

        manifest = AppManifest(
            name="pingapp",
            description="Ping app",
            router=router,
        )
        server.register_app(manifest)
        paths = [getattr(r, "path", None) for r in server.app.routes]
        assert "/apps/pingapp/ping" in paths

    def test_register_app_without_router(self):
        """An app with no router should still register (metadata only)."""
        server = DistroServer()
        manifest = AppManifest(name="no-routes", description="Metadata only")
        server.register_app(manifest)
        assert "no-routes" in server.apps
        assert server.apps["no-routes"].mount_path == "/apps/no-routes"


class TestDiscoverApps:
    """Verify app auto-discovery from a directory.

    Antagonist note: discover_apps() scans a directory for Python packages
    that export a 'manifest' AppManifest. It returns a list of registered
    names and silently skips non-packages and packages without manifests.
    """

    def test_returns_empty_for_nonexistent_dir(self):
        server = DistroServer()
        result = server.discover_apps(Path("/nonexistent/apps/dir"))
        assert result == []

    def test_discovers_app_with_manifest(self, tmp_path):
        """Create a minimal app package and verify it's discovered."""
        app_dir = tmp_path / "test_app"
        app_dir.mkdir()
        (app_dir / "__init__.py").write_text(
            "from amplifier_distro.server.app import AppManifest\n"
            "from fastapi import APIRouter\n"
            "router = APIRouter()\n"
            "@router.get('/')\n"
            "async def index(): return {'ok': True}\n"
            "manifest = AppManifest("
            "name='test_app', description='Discovered test', router=router)\n"
        )
        server = DistroServer()
        found = server.discover_apps(tmp_path)
        assert "test_app" in found
        assert "test_app" in server.apps

    def test_ignores_dirs_without_init(self, tmp_path):
        """Directories without __init__.py are not Python packages."""
        not_a_package = tmp_path / "not_a_package"
        not_a_package.mkdir()
        (not_a_package / "readme.txt").write_text("not an app")
        server = DistroServer()
        result = server.discover_apps(tmp_path)
        assert result == []

    def test_ignores_files_not_dirs(self, tmp_path):
        """Regular files in the apps directory should be skipped."""
        (tmp_path / "random_file.py").write_text("x = 1")
        server = DistroServer()
        result = server.discover_apps(tmp_path)
        assert result == []

    def test_returns_list_of_registered_names(self, tmp_path):
        """Return value must be the list of app names that were registered."""
        for name in ["app_a", "app_b"]:
            d = tmp_path / name
            d.mkdir()
            (d / "__init__.py").write_text(
                "from amplifier_distro.server.app import AppManifest\n"
                f"manifest = AppManifest(name='{name}', description='{name}')\n"
            )
        server = DistroServer()
        found = server.discover_apps(tmp_path)
        assert sorted(found) == ["app_a", "app_b"]


class TestHealthEndpoint:
    """Verify the health endpoint returns the expected response.

    Antagonist note: Health checks are used by monitoring tools and load
    balancers. The response format {"status": "ok"} is a contract.
    """

    def test_health_returns_200(self):
        server = DistroServer()
        client = TestClient(server.app)
        response = client.get("/api/health")
        assert response.status_code == 200

    def test_health_returns_status_ok(self):
        server = DistroServer()
        client = TestClient(server.app)
        response = client.get("/api/health")
        data = response.json()
        assert data["status"] == "ok"

    def test_health_includes_version(self):
        server = DistroServer(version="2.0.0")
        client = TestClient(server.app)
        response = client.get("/api/health")
        data = response.json()
        assert data["version"] == "2.0.0"


class TestAppsEndpoint:
    """Verify the apps listing endpoint.

    Antagonist note: The /api/apps endpoint must reflect all registered
    apps with their metadata. It's the discovery mechanism for interfaces.
    """


class TestStatusEndpoint:
    """Verify /api/status exists and returns preflight status in UI contract format."""

    def test_status_returns_contract_keys(self, monkeypatch):
        from amplifier_distro.doctor import CheckStatus, DiagnosticCheck, DoctorReport

        server = DistroServer()
        client = TestClient(server.app)

        monkeypatch.setattr(
            "amplifier_distro.server.stub.is_stub_mode",
            lambda: False,
        )

        report = DoctorReport(
            checks=[
                DiagnosticCheck(name="ok-check", status=CheckStatus.ok, message="ok"),
                DiagnosticCheck(
                    name="warn-check", status=CheckStatus.warning, message="warn"
                ),
            ]
        )
        monkeypatch.setattr(
            "amplifier_distro.server.app.run_diagnostics",
            lambda *args, **kwargs: report,
        )

        resp = client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()

        assert "passed" in data
        assert "checks" in data
        assert data["passed"] is True  # warnings do not fail preflight

        assert data["checks"] == [
            {
                "name": "ok-check",
                "passed": True,
                "message": "ok",
                "severity": "info",
            },
            {
                "name": "warn-check",
                "passed": False,
                "message": "warn",
                "severity": "warning",
            },
        ]

    def test_status_failed_when_any_check_error(self, monkeypatch):
        from amplifier_distro.doctor import CheckStatus, DiagnosticCheck, DoctorReport

        server = DistroServer()
        client = TestClient(server.app)

        monkeypatch.setattr(
            "amplifier_distro.server.stub.is_stub_mode",
            lambda: False,
        )

        report = DoctorReport(
            checks=[
                DiagnosticCheck(name="ok-check", status=CheckStatus.ok, message="ok"),
                DiagnosticCheck(
                    name="error-check", status=CheckStatus.error, message="bad"
                ),
            ]
        )
        monkeypatch.setattr(
            "amplifier_distro.server.app.run_diagnostics",
            lambda *args, **kwargs: report,
        )

        resp = client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()

        assert data["passed"] is False
        assert data["checks"][1] == {
            "name": "error-check",
            "passed": False,
            "message": "bad",
            "severity": "error",
        }

    def test_apps_empty_by_default(self):
        server = DistroServer()
        client = TestClient(server.app)
        response = client.get("/api/apps")
        assert response.status_code == 200
        assert response.json() == {}

    def test_apps_lists_registered_app(self):
        server = DistroServer()
        manifest = AppManifest(
            name="listed-app",
            description="A listed app",
            version="1.0.0",
        )
        server.register_app(manifest)
        client = TestClient(server.app)
        response = client.get("/api/apps")
        data = response.json()
        assert "listed-app" in data
        assert data["listed-app"]["description"] == "A listed app"
        assert data["listed-app"]["version"] == "1.0.0"
        assert data["listed-app"]["mount_path"] == "/apps/listed-app"
        assert data["listed-app"]["enabled"] is True

    def test_apps_lists_multiple_apps(self):
        server = DistroServer()
        for name in ["alpha", "beta", "gamma"]:
            manifest = AppManifest(name=name, description=f"{name} app")
            server.register_app(manifest)
        client = TestClient(server.app)
        data = client.get("/api/apps").json()
        assert set(data.keys()) == {"alpha", "beta", "gamma"}


class TestAppRoutesMounting:
    """Verify app routes are accessible at /apps/{name}/.

    Antagonist note: This is the integration test - register an app with
    a router, then use TestClient to verify HTTP requests reach the app's
    handlers at the mounted prefix.
    """

    def test_app_root_route_accessible(self):
        server = DistroServer()
        router = APIRouter()

        @router.get("/")
        async def root():
            return {"app": "mounted"}

        manifest = AppManifest(
            name="myapp",
            description="My app",
            router=router,
        )
        server.register_app(manifest)
        client = TestClient(server.app)
        response = client.get("/apps/myapp/")
        assert response.status_code == 200
        assert response.json() == {"app": "mounted"}

    def test_app_sub_route_accessible(self):
        server = DistroServer()
        router = APIRouter()

        @router.get("/data")
        async def data():
            return {"items": [1, 2, 3]}

        manifest = AppManifest(
            name="dataapp",
            description="Data app",
            router=router,
        )
        server.register_app(manifest)
        client = TestClient(server.app)
        response = client.get("/apps/dataapp/data")
        assert response.status_code == 200
        assert response.json() == {"items": [1, 2, 3]}

    def test_app_routes_isolated_from_core_routes(self):
        """App routes must not interfere with core /api/ routes."""
        server = DistroServer()
        router = APIRouter()

        @router.get("/health")
        async def app_health():
            return {"app_health": True}

        manifest = AppManifest(
            name="healthapp",
            description="Has its own health route",
            router=router,
        )
        server.register_app(manifest)
        client = TestClient(server.app)

        # Core health route still works
        core_response = client.get("/api/health")
        assert core_response.json()["status"] == "ok"

        # App health route is separate
        app_response = client.get("/apps/healthapp/health")
        assert app_response.json()["app_health"] is True


class TestApiKeyAuth:
    """Verify bearer-token authentication on mutation endpoints.

    Design contract:
    - No api_key configured -> all endpoints open (backward compat)
    - api_key configured -> read-only endpoints open, mutation endpoints
      require Authorization: Bearer <key>
    - Wrong/missing token -> 401
    """

    MUTATION_ENDPOINTS = [
        ("POST", "/api/bridge/session"),
        ("POST", "/api/bridge/execute"),
        ("POST", "/api/memory/remember"),
        ("POST", "/api/memory/work-log"),
        ("POST", "/api/test-provider"),
    ]

    READ_ONLY_ENDPOINTS = [
        ("GET", "/api/health"),
        ("GET", "/api/status"),
        ("GET", "/api/apps"),
    ]

    @pytest.fixture
    def server(self):
        return DistroServer()

    @pytest.fixture
    def client(self, server):
        return TestClient(server.app)

    def _request(self, client, method, path, **kwargs):
        return getattr(client, method.lower())(path, **kwargs)

    # --- No API key configured (default): everything open ---

    def test_no_key_configured_health_open(self, client):
        """With no api_key, read-only endpoints are open."""
        resp = client.get("/api/health")
        assert resp.status_code == 200

    def test_no_key_configured_mutation_open(self, client):
        """With no api_key, mutation endpoints don't require auth.

        We send minimal bodies; we expect non-401 responses (may be
        400/500 due to missing services, but NOT 401).
        """
        resp = client.post("/api/test-provider", json={"provider": "unknown"})
        assert resp.status_code != 401

        resp = client.post("/api/bridge/session", json={})
        assert resp.status_code != 401

    # --- API key configured: auth enforced on mutations ---

    def test_mutation_requires_auth_when_key_set(self, client, monkeypatch):
        """Mutation endpoints return 401 without a token when key is set."""
        monkeypatch.setattr(
            "amplifier_distro.server.app._get_configured_api_key",
            lambda: "test-secret-key",
        )
        for method, path in self.MUTATION_ENDPOINTS:
            resp = self._request(client, method, path, json={})
            assert resp.status_code == 401, (
                f"{method} {path} should be 401 without token"
            )

    def test_mutation_rejects_wrong_token(self, client, monkeypatch):
        """Mutation endpoints return 401 with an incorrect token."""
        monkeypatch.setattr(
            "amplifier_distro.server.app._get_configured_api_key",
            lambda: "test-secret-key",
        )
        headers = {"Authorization": "Bearer wrong-key"}
        for method, path in self.MUTATION_ENDPOINTS:
            resp = self._request(client, method, path, json={}, headers=headers)
            assert resp.status_code == 401, (
                f"{method} {path} should be 401 with wrong token"
            )

    def test_mutation_passes_with_correct_token(self, client, monkeypatch):
        """Mutation endpoints accept the correct bearer token.

        We expect non-401 responses (may be 400/500 due to missing
        backend services, but auth itself passes).
        """
        monkeypatch.setattr(
            "amplifier_distro.server.app._get_configured_api_key",
            lambda: "test-secret-key",
        )
        headers = {"Authorization": "Bearer test-secret-key"}
        for method, path in self.MUTATION_ENDPOINTS:
            resp = self._request(client, method, path, json={}, headers=headers)
            assert resp.status_code != 401, (
                f"{method} {path} should NOT be 401 with correct token"
            )

    def test_read_only_open_when_key_set(self, client, monkeypatch):
        """Read-only endpoints remain accessible even with auth configured."""
        monkeypatch.setattr(
            "amplifier_distro.server.app._get_configured_api_key",
            lambda: "test-secret-key",
        )
        for method, path in self.READ_ONLY_ENDPOINTS:
            resp = self._request(client, method, path)
            assert resp.status_code != 401, (
                f"{method} {path} should be open even with api_key set"
            )

    def test_401_response_body(self, client, monkeypatch):
        """The 401 response includes an informative detail message."""
        monkeypatch.setattr(
            "amplifier_distro.server.app._get_configured_api_key",
            lambda: "test-secret-key",
        )
        resp = client.post("/api/test-provider", json={})
        assert resp.status_code == 401
        body = resp.json()
        assert "detail" in body
        assert "API key" in body["detail"]
