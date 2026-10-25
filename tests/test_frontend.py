import subprocess
import shutil
import pytest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_frontend_app_js_identical():
    """Ensure root app.js and frontend/app.js are strictly identical."""
    root_app = (REPO_ROOT / "app.js").read_text(encoding="utf-8")
    frontend_app = (REPO_ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    assert root_app == frontend_app, "app.js and frontend/app.js must be identical"


def test_node_check_syntax():
    """Ensure node --check passes for both app.js files."""
    node_path = shutil.which("node")
    if not node_path:
        pytest.skip("Node.js is not installed on this system")

    result_frontend = subprocess.run(
        [node_path, "--check", "frontend/app.js"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result_frontend.returncode == 0, f"frontend/app.js syntax error: {result_frontend.stderr}"

    result_root = subprocess.run(
        [node_path, "--check", "app.js"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result_root.returncode == 0, f"app.js syntax error: {result_root.stderr}"


def test_browser_level_jsdom():
    """Run the browser-level JSDOM test suite."""
    node_path = shutil.which("node")
    if not node_path:
        pytest.skip("Node.js is not installed on this system")

    result = subprocess.run(
        [node_path, "tests/test_browser.js"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Browser test failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
