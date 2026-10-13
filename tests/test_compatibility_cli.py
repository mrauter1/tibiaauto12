import subprocess
import sys


def test_compatibility_cli_help_is_headless():
    script = (
        "import sys; "
        "import tools.compatibility as c; "
        "assert not any(name in sys.modules for name in "
        "('tkinter','keyboard','pyautogui','win32gui')); "
        "c.main(['--help'])"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], text=True, capture_output=True, check=False
    )
    assert completed.returncode == 0
    assert "capture" in completed.stdout.lower()
    assert "native" in completed.stdout.lower()
