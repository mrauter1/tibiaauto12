from dataclasses import replace
import numpy as np
import pytest
from core.perception import Frame
from tibia_client.minimap_localization import MatchResult
from tibia_client.coordinate_monitor import CoordinateSample,CoordinateState,display_coordinate


@pytest.mark.parametrize('reference',['Player crosshair','Map center'])
def test_tied_coordinate_is_visible_but_stale_results_still_clear(reference):
    result=MatchResult(True,32200,32100,7,1.25,.80,.80,100,'ambiguous',10.)
    sample=CoordinateSample(Frame(np.zeros((80,80,3),np.uint8)),(0,0,80,80),(40,40),
                            result,10.,10.,reference)
    state=CoordinateState('Monitoring',True,sample)
    display=display_coordinate(state,10.1)
    assert display.status=='Ambiguous'
    assert '32200' in display.value
    assert 'Exact score tie' in display.detail and reference in display.detail
    assert display_coordinate(state,11.).status=='Stale'
    assert '32200' not in display_coordinate(state,11.).value
    low=replace(state,sample=replace(sample,result=replace(result,accepted=False,reason='low agreement')))
    assert display_coordinate(low,10.1).status=='Unknown'
