from __future__ import annotations

import numpy as np

from conf.conf_manager import ConfManager
from core.perception import Frame, current_frame
from modules.ChooseConfig import calibrate_profile


def test_production_calibration_captures_once_in_process_and_scopes_all_getters(monkeypatch):
    import core.Getters as getters
    import tibia_client.capture as capture_module

    frame = Frame(np.zeros((600, 800, 3), dtype=np.uint8))
    captures = []
    closes = []
    seen = []

    class FakeCaptureProcess:
        def __init__(self, *, capture_size, startup_timeout):
            assert capture_size == (800, 600)
            assert startup_timeout == 15.0

        def capture(self):
            captures.append(True)
            return frame

        def close(self):
            closes.append(True)

    def value(result):
        def getter(*_args):
            seen.append(current_frame())
            return result
        return getter

    monkeypatch.setattr(capture_module, "CaptureProcess", FakeCaptureProcess)
    monkeypatch.setattr(getters, "GetHealthPosition", value((20, 20)))
    monkeypatch.setattr(getters, "GetManaPosition", value((20, 30)))
    monkeypatch.setattr(getters, "GetBattlePosition", value((300, 50, 450, 400)))
    monkeypatch.setattr(getters, "GetStatsPosition", value((500, 200, 605, 210)))
    monkeypatch.setattr(getters, "GetMapPosition", value((650, 50, 755, 158)))
    monkeypatch.setattr(
        getters, "GetPlayerPosition", value((400, 300, 100, 50, 700, 550))
    )
    monkeypatch.setattr(
        getters,
        "SetSQMs",
        value((
            360, 350, 400, 350, 440, 350,
            360, 300, 400, 300, 440, 300,
            360, 250, 400, 250, 440, 250,
        )),
    )

    built = calibrate_profile(ConfManager.get("Json.json"), (800, 600))

    assert captures == [True]
    assert closes == [True]
    assert len(seen) == 7
    assert all(snapshot is frame for snapshot in seen)
    assert built["calibration_size"] == [800, 600]


def test_injected_calibration_getters_do_not_construct_capture_process(monkeypatch):
    import tibia_client.capture as capture_module

    class ForbiddenCapture:
        def __init__(self, *args, **kwargs):
            raise AssertionError("injected calibration must not capture")

    class Getters:
        GetHealthPosition = staticmethod(lambda: (20, 20))
        GetManaPosition = staticmethod(lambda: (20, 30))
        GetBattlePosition = staticmethod(lambda: (300, 50, 450, 400))
        GetStatsPosition = staticmethod(lambda: (500, 200, 605, 210))
        GetMapPosition = staticmethod(lambda: (650, 50, 755, 158))
        GetPlayerPosition = staticmethod(lambda: (400, 300, 100, 50, 700, 550))
        SetSQMs = staticmethod(lambda *_: (
            360, 350, 400, 350, 440, 350,
            360, 300, 400, 300, 440, 300,
            360, 250, 400, 250, 440, 250,
        ))

    monkeypatch.setattr(capture_module, "CaptureProcess", ForbiddenCapture)
    built = calibrate_profile(
        ConfManager.get("Json.json"), (800, 600), getters=Getters
    )
    assert built["Stats"] is True
