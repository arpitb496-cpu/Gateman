import os
import tempfile
import pytest

# Set a valid GATEMAN_ADMIN_TOKEN (>= 16 characters) for test suite execution
os.environ.setdefault("GATEMAN_ADMIN_TOKEN", "gateman_test_admin_token_32chars")

# Set an isolated fallback test data directory before test modules import backend.main
_INITIAL_TEST_DATA_DIR = tempfile.mkdtemp(prefix="gateman_test_data_")
os.environ.setdefault("GATEMAN_DATA_DIR", _INITIAL_TEST_DATA_DIR)

@pytest.fixture(autouse=True)
def isolate_test_data_dir(tmp_path, monkeypatch):
    """Isolate all test whitelist, logs, audit logs and archives into a fresh tmp_path per test."""
    data_dir = str(tmp_path)
    monkeypatch.setenv("GATEMAN_DATA_DIR", data_dir)
    yield tmp_path
