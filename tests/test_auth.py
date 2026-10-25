import os
import csv
import glob
import tempfile
import importlib
import pytest
from starlette.testclient import TestClient

# Ensure default test token is set before importing app
TEST_TOKEN = "gateman_test_admin_token_32chars"
os.environ["GATEMAN_ADMIN_TOKEN"] = TEST_TOKEN

import backend.main
from backend.main import (
    app,
    get_admin_token,
    WHITELIST_FILE,
    LOG_FILE,
    AUDIT_LOG_FILE,
    save_whitelist_atomic,
    get_whitelist,
)


class TestAuthenticationAndSecurity:
    """Test suite covering bearer authentication, endpoints, JSON schemas, archiving, and audit trails."""

    def test_health_stays_open_without_token(self):
        """GET /health must be explicitly exempt from authentication."""
        client = TestClient(app)
        res = client.get("/health")
        assert res.status_code == 200
        data = res.json()
        assert data.get("status") == "ok"

    def test_endpoint_401_with_no_token(self):
        """Accessing protected endpoints without an Authorization header must return 401."""
        client = TestClient(app)
        for endpoint in ["/whitelist", "/logs", "/stats"]:
            res = client.get(endpoint)
            assert res.status_code == 401, f"{endpoint} should return 401 when no token is provided"
            assert "detail" in res.json()
            assert res.headers.get("www-authenticate") == "Bearer"

    def test_endpoint_401_with_wrong_token(self):
        """Accessing protected endpoints with an invalid bearer token must return 401."""
        client = TestClient(app)
        wrong_headers = {"Authorization": "Bearer completely_wrong_token_value_here"}
        res = client.get("/whitelist", headers=wrong_headers)
        assert res.status_code == 401
        assert "detail" in res.json()

    def test_endpoint_401_with_malformed_header(self):
        """Headers not starting with 'Bearer ' must return 401."""
        client = TestClient(app)
        res = client.get("/whitelist", headers={"Authorization": f"Basic {TEST_TOKEN}"})
        assert res.status_code == 401

    def test_endpoint_200_with_right_token(self):
        """Protected endpoints must return 200 when a valid Bearer token is provided."""
        client = TestClient(app)
        valid_headers = {"Authorization": f"Bearer {TEST_TOKEN}"}
        res = client.get("/whitelist", headers=valid_headers)
        assert res.status_code == 200
        assert "whitelist" in res.json()

    def test_json_whitelist_add_and_remove(self):
        """
        /whitelist/add and /whitelist/remove must accept a JSON body {'plate': '...'}
        and refuse query parameter invocations.
        """
        client = TestClient(app)
        valid_headers = {"Authorization": f"Bearer {TEST_TOKEN}"}
        test_plate = "DL9CAF9999"

        # 1. Query parameter without JSON body must be rejected (422 Unprocessable Entity)
        query_res = client.post(f"/whitelist/add?plate={test_plate}", headers=valid_headers)
        assert query_res.status_code == 422, "Query parameter invocation must fail schema validation"

        # 2. JSON body addition works
        add_res = client.post(
            "/whitelist/add",
            headers=valid_headers,
            json={"plate": test_plate},
        )
        assert add_res.status_code == 200
        assert add_res.json()["status"] == "added"
        assert add_res.json()["plate"] == test_plate

        # Verify plate is in whitelist
        wl_res = client.get("/whitelist", headers=valid_headers)
        assert test_plate in wl_res.json()["whitelist"]

        # 3. JSON body removal works
        rem_res = client.post(
            "/whitelist/remove",
            headers=valid_headers,
            json={"plate": test_plate},
        )
        assert rem_res.status_code == 200
        assert rem_res.json()["status"] == "removed"

        # Verify plate is removed
        wl_res_after = client.get("/whitelist", headers=valid_headers)
        assert test_plate not in wl_res_after.json()["whitelist"]

    def test_clear_logs_archives_file(self):
        """
        /clear-logs must archive detection_log.csv to a timestamped file before resetting it.
        """
        client = TestClient(app)
        valid_headers = {"Authorization": f"Bearer {TEST_TOKEN}"}

        # Write a dummy entry to detection_log.csv
        unique_marker = "ARCHIVE_MARKER_TEST_1234"
        with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["2026-10-25", "12:00:00", unique_marker, "ALLOWED"])

        # Call /clear-logs
        res = client.post("/clear-logs", headers=valid_headers)
        assert res.status_code == 200
        data = res.json()
        assert data.get("status") == "success"
        archive_name = data.get("archive")
        assert archive_name, "Expected an archive filename in response"

        # Check archive file exists on disk and contains our marker
        dir_name = os.path.dirname(os.path.abspath(LOG_FILE))
        archive_path = os.path.join(dir_name, archive_name)
        assert os.path.exists(archive_path), f"Archive file {archive_path} must exist"

        with open(archive_path, "r", encoding="utf-8") as f:
            content = f.read()
            assert unique_marker in content, "Archived file must contain pre-cleared detection records"

        # Clean up created test archive file
        if os.path.exists(archive_path):
            os.remove(archive_path)

    def test_audit_log_appended(self):
        """
        Actions (whitelist add/remove, clear-logs) must be appended to audit_log.csv with:
        timestamp, action, detail, client_ip.
        """
        client = TestClient(app)
        valid_headers = {"Authorization": f"Bearer {TEST_TOKEN}"}
        plate = "UP14TEST0001"

        client.post("/whitelist/add", headers=valid_headers, json={"plate": plate})
        client.post("/whitelist/remove", headers=valid_headers, json={"plate": plate})
        client.post("/clear-logs", headers=valid_headers)

        assert os.path.exists(AUDIT_LOG_FILE), "audit_log.csv must exist"
        with open(AUDIT_LOG_FILE, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            header = next(reader)
            assert [c.lower() for c in header] == ["timestamp", "action", "detail", "client_ip"]
            rows = list(reader)

        actions = [r[1] for r in rows]
        assert "whitelist_add" in actions
        assert "whitelist_remove" in actions
        assert "clear_logs" in actions

    def test_startup_fails_without_token(self, monkeypatch):
        """
        Application refuses to start if GATEMAN_ADMIN_TOKEN is unset or shorter than 16 chars.
        """
        # Case 1: Unset
        monkeypatch.delenv("GATEMAN_ADMIN_TOKEN", raising=False)
        with pytest.raises(RuntimeError) as exc_unset:
            get_admin_token()
        assert "16 characters" in str(exc_unset.value)

        # Case 2: Shorter than 16 characters
        monkeypatch.setenv("GATEMAN_ADMIN_TOKEN", "short_token_123")
        with pytest.raises(RuntimeError) as exc_short:
            get_admin_token()
        assert "16 characters" in str(exc_short.value)

        # Case 3: TestClient startup lifecycle failure
        with pytest.raises(RuntimeError):
            with TestClient(app):
                pass

        # Case 4: Module reload failure
        monkeypatch.delenv("GATEMAN_ADMIN_TOKEN", raising=False)
        with pytest.raises(RuntimeError):
            importlib.reload(backend.main)

        # Restore valid token
        monkeypatch.setenv("GATEMAN_ADMIN_TOKEN", TEST_TOKEN)
        importlib.reload(backend.main)

    def test_atomic_whitelist_writes(self):
        """Atomic whitelist writer writes under lock without corrupting file or leaving temp artifacts."""
        plates = ["22BH6517A", "DL3CAQ0001", "MH12AB1234"]
        save_whitelist_atomic(plates)
        assert get_whitelist() == set(plates)

        # Ensure no orphan temp files remain in project directory
        dir_name = os.path.dirname(os.path.abspath(WHITELIST_FILE))
        tmp_files = glob.glob(os.path.join(dir_name, "whitelist_*.tmp"))
        assert len(tmp_files) == 0, "No temporary files should be left after atomic replace"
