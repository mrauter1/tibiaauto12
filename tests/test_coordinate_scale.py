import numpy as np
import pytest
from tibia_client.minimap_localization import MinimapLocator, palette_codes, AUTO_SCALES
from tibia_client.coordinate_monitor import CoordinateSettings

@pytest.fixture(scope='module')
def locator():
    rng=np.random.default_rng(6321)
    palette=np.array([[0,204,0],[153,153,153],[255,102,0],[255,255,0]],np.uint8)
    loc=MinimapLocator.__new__(MinimapLocator)
    loc.origin=(31000,32000);loc.h=loc.w=384;loc.cache=None
    loc.rgb={7:palette[rng.integers(0,4,(384,384))]}
    loc.codes={7:palette_codes(loc.rgb[7])}
    loc.min_score=.9;loc.keys=loc.positions=None
    loc.build_index();loc.prepare_scales()
    return loc

@pytest.mark.parametrize('step',[2,4,8])
@pytest.mark.parametrize('pitch',[1.,1.25])
def test_subpixel_every_sampling_phase(locator,step,pitch):
    # Build the native zoom raster, then its UI enlargement. The map origin
    # is deliberately varied through every phase of the reduced sampling grid.
    axis=np.floor((np.arange(41)-20)/pitch+.5).astype(int)*step
    for phase in range(step):
        x,y=180+phase,180+(phase*3)%step
        crop=locator.rgb[7][(y+axis)[:,None],(x+axis)[None,:]].copy()
        crop[4:7,5:8]=(255,255,255)
        result=locator.locate(crop,scales=(pitch/step,),anchor=(20,20))
        assert result.accepted,result
        assert (result.x,result.y,result.z)==(31000+x,32000+y,7)
        assert result.resolution_tiles==int(np.ceil(step/pitch))

@pytest.mark.parametrize('scale',[.125,.15625,.25,.3125,.5,.625,1.,1.25,2.5,5.])
def test_auto_retains_rendered_grid(locator,scale):
    step=locator.sample_step(scale);pitch=scale*step
    size=max(41,int(scale*24)+1);anchor=size//2
    axis=np.floor((np.arange(size)-anchor)/pitch+.5).astype(int)*step
    crop=locator.rgb[7][(180+axis)[:,None],(180+axis)[None,:]]
    assert scale in locator.infer_scales(crop)
    result=locator.locate(crop,anchor=(anchor,anchor))
    assert result.accepted,result
    assert (result.x,result.y,result.z)==(31180,32180,7)
    assert result.pixels_per_tile==scale

def test_small_scales_settings_and_cache(locator,tmp_path):
    settings=CoordinateSettings(tmp_path,(0,0,100,100),scales=(.125,.625))
    assert settings.scales==(.125,.625)
    locator.cache=tmp_path
    previous=locator._zoom_indexes.pop(2)
    rebuilt=locator.build_zoom_index(2)
    locator._zoom_indexes.pop(2)
    loaded=locator.build_zoom_index(2)
    for expected,actual,disk in zip(previous,rebuilt,loaded):
        assert np.array_equal(expected,actual)
        assert np.array_equal(expected,disk)
    locator.cache=None

def test_stride_scoring_excludes_masked_tiles(locator):
    q=locator.codes[7][100:166:2,100:166:2].copy()
    q[:12]=0
    score=locator.verify(q,[(100,100,7)],step=2)[0][0]
    assert score==1.

@pytest.mark.parametrize('filename,fixture_name,expected,scale,min_score,resolution',[
    ('zoom-out-3.png','minimap-three-levels-out-20261024.png',(32309,32245,7),.625,.65,2),
    ('zoom-max.png','minimap-maximum-zoom-20261024.png',(32308,32248,7),5.,.75,1),
])
def test_recorded_zoom_pair(filename,fixture_name,expected,scale,min_score,resolution):
    from pathlib import Path
    from PIL import Image
    from core.paths import project_path
    from tibia_client.coordinate_monitor import DEFAULT_MAPS
    if not (DEFAULT_MAPS/'manifest.json').is_file():
        pytest.skip('Local reference atlas is not installed')
    fixture=project_path('tests/fixtures/minimap/'+fixture_name)
    if not fixture.exists():
        fixture=Path(__file__).parent/filename
    loc=MinimapLocator(DEFAULT_MAPS,cache_dir=DEFAULT_MAPS.parent/'minimap-localization/cache')
    result=loc.locate(np.array(Image.open(fixture).convert('RGB')))
    # Reviewed atlas alignment, not an independent in-game player measurement.
    assert result.accepted,result
    assert (result.x,result.y,result.z)==expected
    assert result.pixels_per_tile==scale
    assert result.score>min_score
    assert result.resolution_tiles==resolution
