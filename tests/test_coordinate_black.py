import numpy as np
import pytest
from tibia_client.minimap_localization import MinimapLocator,palette_codes

def fixture():
    rng=np.random.default_rng(963)
    palette=np.array([[153,153,153],[255,51,0],[255,255,0]],np.uint8)
    image=np.zeros((192,192,3),np.uint8)
    image[60:130,85:105]=palette[rng.integers(0,3,(70,20))]
    image[75:90,65:145]=palette[rng.integers(0,3,(15,80))]
    surface=image.copy();surface[np.max(surface,axis=2)==0]=(0,204,0)
    correct=image.copy();correct[70,90]=(0,204,0)
    loc=MinimapLocator.__new__(MinimapLocator)
    loc.origin=(31000,32000);loc.h=loc.w=192;loc.cache=None
    loc.rgb={6:correct,7:surface}
    loc.codes={z:palette_codes(im) for z,im in loc.rgb.items()}
    loc.min_score=.4;loc.keys=loc.positions=None;loc.build_index()
    return loc,image[46:147,46:147].copy()

def test_auto_black_geometry_beats_better_colored_surface_candidate():
    loc,crop=fixture()
    r=loc.locate(crop,scales=(1,))
    assert (r.x,r.y,r.z)==(31096,32096,6),r
    assert r.score>.99

def test_explicit_surface_keeps_unexplored_black_policy():
    loc,crop=fixture()
    r=loc.locate(crop,scales=(1,),floor=7)
    assert r.accepted and r.z==7 and r.score==1.

@pytest.mark.parametrize('step',[1,2,4])
def test_real_black_is_in_non_surface_percentage(step):
    loc,_=fixture()
    q=loc.codes[6][46:147:step,46:147:step].copy()
    black=q==0
    # Deliberately colour every expected black tile in the reference.
    patch=loc.codes[6][46:147:step,46:147:step]
    patch[black]=25
    colored=np.count_nonzero(q)
    assert loc.verify(q,[(46,46,6)],step=step)[0][0]==1.
    score=loc.verify(q,[(46,46,6)],step=step,black_mask=black)[0][0]
    assert score==pytest.approx(colored/q.size)

def test_masks_cannot_become_black_and_black_cannot_inflate_agreement():
    loc,crop=fixture()
    crop[10:20,10:20]=(255,0,255)
    normalized,q,center=loc.normalize(crop,1)
    observed=loc.sparse_observation(normalized,q,1,center)
    black=observed&(q==0)
    assert not black[10:20,10:20].any()
    assert not black[:2].any()
    assert not black[46:55,46:55].any()
    candidates=[(46,46,6)]
    terrain=loc.verify(q,candidates)[0][0]
    score=loc.verify(q,candidates,black_mask=black)[0][0]
    assert score<=terrain

def test_boundary_retrieval_for_partially_black_maps():
    loc,crop=fixture()
    normalized,q,center=loc.normalize(crop,1)
    assert np.count_nonzero(q)>q.size*.20
    original=loc.boundary_candidates
    calls=[]
    def spy(*args,**kwargs):
        calls.append(True)
        return original(*args,**kwargs)
    loc.boundary_candidates=spy
    assert loc.locate(crop,scales=(1,)).accepted
    assert calls

def test_black_only_does_not_locate():
    loc,_=fixture()
    assert not loc.locate(np.zeros((101,101,3),np.uint8),scales=(1,),floor=6).accepted
