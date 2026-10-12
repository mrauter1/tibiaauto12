import json
import subprocess
import sys

from core.paths import PROJECT_ROOT
from tools.doctor import check_installation


def test_doctor_has_no_native_side_effects():
    script = """
import sys
from tools.doctor import check_installation
report = check_installation(assets=True)
assert report['ok'], report
assert not {'win32gui', 'keyboard', 'pyautogui', 'core.HookWindow', 'core.MoveMouse'} & sys.modules.keys()
"""
    subprocess.run([sys.executable, "-c", script], cwd=PROJECT_ROOT, check=True)


def test_cli_errors_are_structured():
    result = subprocess.run(
        [sys.executable, "-m", "tools.doctor", "--profile", "../escape.json", "--json"],
        cwd=PROJECT_ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["ok"] is False
    assert any(item["check"] == "calibration" and item["level"] == "error"
               for item in report["checks"])


def test_shipped_walk_route_can_be_checked_offline():
    report = check_installation(route="ratThais")
    assert report["ok"], report
