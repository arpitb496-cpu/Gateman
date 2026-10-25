import pytest
from starlette.testclient import TestClient
from fastapi.middleware.cors import CORSMiddleware
from backend.main import app, DEFAULT_CORS_ORIGINS, CORS_ORIGINS


class TestCorsConfiguration:
    """Tests for CORS configuration and origin allowance."""

    def test_default_cors_origins_includes_localhost_3000(self):
        """The default CORS origin list must explicitly include http://localhost:3000."""
        assert isinstance(DEFAULT_CORS_ORIGINS, (list, tuple, set))
        assert "http://localhost:3000" in DEFAULT_CORS_ORIGINS

    def test_default_cors_origins_includes_127_0_0_1_3000(self):
        """Should also include 127.0.0.1:3000 for standard local testing."""
        assert "http://127.0.0.1:3000" in DEFAULT_CORS_ORIGINS

    def test_cors_origins_active_list(self):
        """The active CORS_ORIGINS list must include http://localhost:3000."""
        assert "http://localhost:3000" in CORS_ORIGINS

    def test_cors_middleware_registered(self):
        """Verify CORSMiddleware is mounted with http://localhost:3000 in allow_origins."""
        cors_middlewares = [
            m for m in app.user_middleware
            if m.cls == CORSMiddleware
        ]
        assert len(cors_middlewares) >= 1
        middleware = cors_middlewares[0]
        allow_origins = middleware.kwargs.get("allow_origins", [])
        assert "http://localhost:3000" in allow_origins

    def test_cors_request_from_localhost_3000(self):
        """A cross-origin request from http://localhost:3000 must receive CORS approval."""
        client = TestClient(app)
        res = client.get("/health", headers={"Origin": "http://localhost:3000"})
        assert res.status_code == 200
        assert res.headers.get("access-control-allow-origin") == "http://localhost:3000"
        assert res.headers.get("access-control-allow-credentials") == "true"

    def test_cors_preflight_options_localhost_3000(self):
        """Preflight OPTIONS request from http://localhost:3000 must allow the method."""
        client = TestClient(app)
        res = client.options(
            "/api/validate-plate",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Content-Type",
            },
        )
        assert res.status_code == 200
        assert res.headers.get("access-control-allow-origin") == "http://localhost:3000"

    def test_cors_request_from_127_0_0_1_3000(self):
        """Requests from http://127.0.0.1:3000 must also be allowed."""
        client = TestClient(app)
        res = client.get("/health", headers={"Origin": "http://127.0.0.1:3000"})
        assert res.status_code == 200
        assert res.headers.get("access-control-allow-origin") == "http://127.0.0.1:3000"

    def test_cors_rejects_unauthorized_origin(self):
        """Requests from an unauthorized origin should not be granted access-control-allow-origin."""
        client = TestClient(app)
        res = client.get("/health", headers={"Origin": "http://unauthorized-domain.test"})
        assert res.status_code == 200
        assert res.headers.get("access-control-allow-origin") != "http://unauthorized-domain.test"
