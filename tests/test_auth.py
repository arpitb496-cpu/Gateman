import os
import csv
import glob
import tempfile
import importlib
from pathlib import Path
import pytest
from starlette.testclient import TestClient

# Ensure default test token is set before importing app
TEST_TOKEN = "gateman_test_admin_token_32chars"
os.environ["GATEMAN_ADMIN_TOKEN"] = TEST_TOKEN

import backend.main
from backend.main import (
    app,
    get_admin_token,
    save_whitelist_atomic,
    get_whitelist,
    get_whitelist_file,
    get_log_file,
    get_audit_log_file,
    get_client_ip,
    get_data_dir,
)


class TestAuthenticationAndSecurity:
    """Test suite covering bearer authentication, endpoints, JSON schemas, archiving, and audit trails."""

    def test_health_stays_open_without_token(self):
        """GET /health must be explicitly exempt from authentication and report status and device."""
        client = TestClient(app)
        res = client.get("/health")
        assert res.status_code == 200
        data = res.json()
        assert data.get("status") in ("degraded", "ok")
        assert "device" in data

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
        log_file = get_log_file()
        with open(log_file, "a", newline="", encoding="utf-8") as f:
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
        dir_name = os.path.dirname(os.path.abspath(log_file))
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

        audit_file = get_audit_log_file()
        assert os.path.exists(audit_file), "audit_log.csv must exist"
        with open(audit_file, "r", encoding="utf-8") as f:
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
        # Case 1: Unset with TestClient
        monkeypatch.delenv("GATEMAN_ADMIN_TOKEN", raising=False)
        with pytest.raises(RuntimeError) as exc_unset:
            with TestClient(app):
                pass
        assert "16 characters" in str(exc_unset.value)

        # Case 2: Shorter than 16 characters with TestClient
        monkeypatch.setenv("GATEMAN_ADMIN_TOKEN", "short_token_123")
        with pytest.raises(RuntimeError) as exc_short:
            with TestClient(app):
                pass
        assert "16 characters" in str(exc_short.value)

        # Case 3: Middleware returns 500 when token missing during request
        monkeypatch.delenv("GATEMAN_ADMIN_TOKEN", raising=False)
        client = TestClient(app, raise_server_exceptions=False)
        res = client.get("/whitelist", headers={"Authorization": "Bearer any_token_here_123456"})
        assert res.status_code == 500

        # Case 4: Module import does not fail without token (cli_scanner compatibility)
        monkeypatch.delenv("GATEMAN_ADMIN_TOKEN", raising=False)
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
        whitelist_file = get_whitelist_file()
        dir_name = os.path.dirname(os.path.abspath(whitelist_file))
        tmp_files = glob.glob(os.path.join(dir_name, "whitelist_*.tmp"))
        assert len(tmp_files) == 0, "No temporary files should be left after atomic replace"

    def test_trusted_proxy_ips_behavior(self, monkeypatch):
        """
        get_client_ip() ignores X-Forwarded-For unless immediate peer is listed in TRUSTED_PROXY_IPS;
        otherwise it uses request.client.host.
        """
        client = TestClient(app)
        valid_headers = {
            "Authorization": f"Bearer {TEST_TOKEN}",
            "X-Forwarded-For": "203.0.113.195, 10.0.0.1",
        }

        # Case 1: TRUSTED_PROXY_IPS is unset -> ignores X-Forwarded-For
        monkeypatch.delenv("TRUSTED_PROXY_IPS", raising=False)
        res = client.post("/whitelist/add", headers=valid_headers, json={"plate": "DL11AA0001"})
        assert res.status_code == 200

        with open(get_audit_log_file(), "r", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        last_row = rows[-1]
        assert last_row[1] == "whitelist_add"
        # TestClient default peer host is 'testclient'
        assert last_row[3] == "testclient", "Untrusted peer must not use X-Forwarded-For"

        # Case 2: Immediate peer is NOT in TRUSTED_PROXY_IPS
        monkeypatch.setenv("TRUSTED_PROXY_IPS", "10.0.0.1, 192.168.1.1")
        client.post("/whitelist/add", headers=valid_headers, json={"plate": "DL11AA0002"})
        with open(get_audit_log_file(), "r", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        assert rows[-1][3] == "testclient", "Peer not in TRUSTED_PROXY_IPS must be ignored"

        # Case 3: Immediate peer IS in TRUSTED_PROXY_IPS
        monkeypatch.setenv("TRUSTED_PROXY_IPS", "testclient, 127.0.0.1")
        client.post("/whitelist/add", headers=valid_headers, json={"plate": "DL11AA0003"})
        with open(get_audit_log_file(), "r", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        assert rows[-1][3] == "203.0.113.195", "Trusted proxy must parse first IP from X-Forwarded-For"

    def test_data_dir_isolation_uses_tmp_path(self, tmp_path, monkeypatch):
        """
        Verify that GATEMAN_DATA_DIR directs whitelist, detection logs, audit logs,
        and archives to the specified directory without touching project root.
        """
        custom_dir = tmp_path / "custom_data_dir"
        monkeypatch.setenv("GATEMAN_DATA_DIR", str(custom_dir))

        assert get_data_dir() == str(custom_dir)
        assert get_whitelist_file() == str(custom_dir / "whitelist.txt")
        assert get_log_file() == str(custom_dir / "detection_log.csv")
        assert get_audit_log_file() == str(custom_dir / "audit_log.csv")

        client = TestClient(app)
        valid_headers = {"Authorization": f"Bearer {TEST_TOKEN}"}

        # Whitelist add
        res_wl = client.post("/whitelist/add", headers=valid_headers, json={"plate": "HR26DK8888"})
        assert res_wl.status_code == 200
        assert os.path.exists(custom_dir / "whitelist.txt")
        assert os.path.exists(custom_dir / "audit_log.csv")

        # Clear logs produces archive in custom_dir
        res_clear = client.post("/clear-logs", headers=valid_headers)
        assert res_clear.status_code == 200
        archive_name = res_clear.json()["archive"]
        assert os.path.exists(custom_dir / archive_name)

    def test_init_whitelist_empty_with_warning(self, tmp_path, monkeypatch):
        """init_whitelist() must not create default plates; create an empty file and log a warning."""
        custom_dir = tmp_path / "empty_wl_dir"
        monkeypatch.setenv("GATEMAN_DATA_DIR", str(custom_dir))
        wl_file = custom_dir / "whitelist.txt"
        assert not os.path.exists(wl_file)

        backend.main.init_whitelist()
        assert os.path.exists(wl_file)
        assert backend.main.get_whitelist() == set()
        with open(wl_file, "r", encoding="utf-8") as f:
            content = f.read().strip()
        assert content == ""

    def test_allow_no_models_degraded_and_503(self):
        """
        With GATEMAN_ALLOW_NO_MODELS=1, /health returns degraded and /detect returns 503.
        """
        import io
        client = TestClient(app)
        res_h = client.get("/health")
        assert res_h.status_code == 200
        assert res_h.json()["status"] == "degraded"
        assert "device" in res_h.json()

        # /detect returns 503
        dummy_img = io.BytesIO(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xbf\x00\xff\xd9")
        res_d = client.post(
            "/detect",
            headers={"Authorization": f"Bearer {TEST_TOKEN}"},
            files={"file": ("test.jpg", dummy_img, "image/jpeg")},
        )
        assert res_d.status_code == 503
        assert "not loaded" in res_d.json().get("detail", "")

    def test_device_selection_order(self, monkeypatch):
        """Device selection must prioritize cuda, then mps, then cpu."""
        import torch

        # Case 1: CUDA available -> cuda
        monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
        assert backend.main.select_device() == "cuda"

        # Case 2: CUDA false, MPS true -> mps
        monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
        if hasattr(torch.backends, "mps"):
            monkeypatch.setattr(torch.backends.mps, "is_available", lambda: True)
            assert backend.main.select_device() == "mps"

        # Case 3: CUDA false, MPS false -> cpu
        if hasattr(torch.backends, "mps"):
            monkeypatch.setattr(torch.backends.mps, "is_available", lambda: False)
        assert backend.main.select_device() == "cpu"

    def test_backend_whitelist_deleted_and_env_data_dir(self, tmp_path, monkeypatch):
        """
        backend/whitelist.txt must not exist. Whitelist is only read from $GATEMAN_DATA_DIR/whitelist.txt.
        whitelist.example.txt must exist with fake plates.
        """
        project_root = Path(backend.main.PROJECT_ROOT)
        assert not os.path.exists(project_root / "backend" / "whitelist.txt")

        # Check whitelist.example.txt
        example_file = project_root / "whitelist.example.txt"
        assert os.path.exists(example_file)
        with open(example_file, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip() and not line.startswith("#")]
        assert len(lines) > 0
        assert "DL01AB1234" in lines

        # Check .gitignore contains files
        gitignore = project_root / ".gitignore"
        assert os.path.exists(gitignore)
        with open(gitignore, "r", encoding="utf-8") as f:
            gi_content = f.read()
        assert "whitelist.txt" in gi_content
        assert "detection_log.csv" in gi_content
        assert "detection_log.txt" in gi_content

    def test_dockerfile_and_dockerignore(self):
        """
        Dockerfile must set ENV GATEMAN_DATA_DIR=/data, VOLUME /data,
        and .dockerignore must ignore data files and model backups.
        """
        project_root = Path(backend.main.PROJECT_ROOT)
        dockerfile = project_root / "backend" / "Dockerfile"
        if not os.path.exists(dockerfile):
            dockerfile = project_root / "Dockerfile"
        assert os.path.exists(dockerfile)
        with open(dockerfile, "r", encoding="utf-8") as f:
            df_content = f.read()
        assert "ENV GATEMAN_DATA_DIR=/data" in df_content
        assert "VOLUME /data" in df_content

        dockerignore = project_root / ".dockerignore"
        assert os.path.exists(dockerignore)
        with open(dockerignore, "r", encoding="utf-8") as f:
            di_content = f.read()
        assert "whitelist.txt" in di_content
        assert "detection_log.csv" in di_content
        assert "*.pt.bak" in di_content

    def test_cli_scanner_imports_cleanly_without_admin_token(self, monkeypatch):
        """
        cli_scanner.py imports backend.main without requiring GATEMAN_ADMIN_TOKEN at import time.
        """
        monkeypatch.delenv("GATEMAN_ADMIN_TOKEN", raising=False)
        import cli_scanner
        assert hasattr(cli_scanner, "DEVICE")

    def test_frontend_node_check_and_parity(self):
        """
        frontend/app.js and app.js must pass node --check, be identical,
        and include SYSTEM OFFLINE - MANUAL CONTROL handling.
        """
        import subprocess
        project_root = Path(backend.main.PROJECT_ROOT)
        fe_app = project_root / "frontend" / "app.js"
        root_app = project_root / "app.js"
        fe_html = project_root / "frontend" / "index.html"
        root_html = project_root / "index.html"

        # 1. node --check
        res1 = subprocess.run(["node", "--check", str(fe_app)], capture_output=True, text=True)
        assert res1.returncode == 0, f"node --check failed: {res1.stderr}"
        res2 = subprocess.run(["node", "--check", str(root_app)], capture_output=True, text=True)
        assert res2.returncode == 0, f"node --check failed: {res2.stderr}"

        # 2. File parity
        with open(fe_app, "r", encoding="utf-8") as f1, open(root_app, "r", encoding="utf-8") as f2:
            assert f1.read() == f2.read(), "frontend/app.js and app.js must be identical"
        with open(fe_html, "r", encoding="utf-8") as f1, open(root_html, "r", encoding="utf-8") as f2:
            assert f1.read() == f2.read(), "frontend/index.html and index.html must be identical"

        # 3. System offline banner in HTML and app.js
        with open(fe_html, "r", encoding="utf-8") as f:
            html_text = f.read()
        assert "system-offline-banner" in html_text
        assert "SYSTEM OFFLINE - MANUAL CONTROL" in html_text

        with open(fe_app, "r", encoding="utf-8") as f:
            app_text = f.read()
        assert "SYSTEM OFFLINE - MANUAL CONTROL" in app_text
        assert "setSystemOffline" in app_text
        assert "degraded" in app_text
        assert "503" in app_text



