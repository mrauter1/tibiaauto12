import subprocess
import sys

from tools import compatibility as compatibility_cli

from test_compatibility import alignment, capture_check, context, operation
from tibia_client.compatibility import CompatibilityProfile


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


def test_cli_validate_reports_shared_physical_cursor_restore_requirement(
    monkeypatch, capsys
):
    ctx = context()
    profile = CompatibilityProfile(
        "Hunt.json", ctx, ("left_click",), (operation(ctx, "left_click"),),
        alignment(ctx), (capture_check(ctx),),
    )
    monkeypatch.setattr(compatibility_cli, "load_profile", lambda _name: profile)
    monkeypatch.setattr(
        compatibility_cli.ConfManager, "get", staticmethod(lambda _name: {"calibration": True})
    )
    monkeypatch.setattr(
        compatibility_cli, "context_from_calibration",
        lambda _calibration, _environment, input_size: ctx,
    )

    assert compatibility_cli.main([
        "validate", "Hunt.compatibility.json", "--require", "left_click"
    ]) == 1
    assert "cursor_restore" in capsys.readouterr().out
