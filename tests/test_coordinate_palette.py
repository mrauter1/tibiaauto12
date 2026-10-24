"""Regression checks for rendered palette colours and fractional tile alignment."""
from pathlib import Path
from dataclasses import replace
import time

import numpy as np
from PIL import Image
import pytest

from core.paths import project_path
from core.perception import Frame
from tibia_client.coordinate_monitor import (
    DEFAULT_MAPS, CoordinateMonitor, CoordinateSettings, find_crosshair,
)
from tibia_client.minimap_localization import MinimapLocator, palette_codes


def test_actual_palette_quantization_and_water_guard():
    loc=MinimapLocator.__new__(MinimapLocator)
    loc.cache=None
    palette=np.array([[[0,0,0],[0,204,0],[51,102,153],[153,153,153]]],np.uint8)
    loc.codes={7:palette_codes(palette)}
    rendered=np.array([[[3,193,8],[54,99,150],[252,0,253]]],np.uint8)
    assert loc.quantize_capture(rendered).tolist()==[[25,52,0]]
    # A water background and one marker colour are not distinctive terrain.
    q=np.full((31,31),52,np.uint8)
    q[2:10,2:10]=25
    assert loc.verify(q,[(0,0,7)])==[]


@pytest.mark.parametrize('use_center,expected', [
    (False,(31955,31877,7)), (True,(31953,31875,7)),
])
def test_recorded_island_auto_zoom_all_floors(use_center,expected):
    if not (DEFAULT_MAPS/'manifest.json').is_file():
        pytest.skip('Local reference atlas is not installed')
    fixture=project_path('tests/fixtures/minimap/island-minimap-20261023.png')
    frame=Frame(Image.open(fixture))
    assert find_crosshair(frame.pixels)[0]==(69.5,69.)
    class Capture:
        last_observed_at=None
        def capture(self,token):
            token.checkpoint()
            self.last_observed_at=time.monotonic()
            return frame
        def close(self): pass
    options=CoordinateSettings(DEFAULT_MAPS,(0,0,*frame.size),use_center=use_center)
    monitor=CoordinateMonitor(options,capture_factory=Capture)
    try:
        monitor.start()
        deadline=time.monotonic()+15
        while monitor.state().sample is None and monitor.state().phase!='Error' and time.monotonic()<deadline:
            time.sleep(.02)
        state=monitor.state()
        assert state.sample is not None,state.detail
        result=state.sample.result
        assert result.accepted,result
        # Atlas alignment reviewed against the captured island; not a separate
        # in-game measurement of the player's coordinate.
        assert (result.x,result.y,result.z)==expected
        assert result.pixels_per_tile==1.25
        assert result.score>=.70 and result.score-result.runner_up>=.04
        assert state.sample.reference==('Map center' if use_center else 'Player crosshair')
    finally:
        monitor.close()
        assert monitor.wait()
