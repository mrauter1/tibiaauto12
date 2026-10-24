"""Sparse rooms/corridors need boundary retrieval without black-score inflation."""
import numpy as np
import pytest
from tibia_client.minimap_localization import MinimapLocator,palette_codes,fingerprints


def sparse_locator(duplicate=False):
    loc=MinimapLocator.__new__(MinimapLocator)
    loc.origin=(31000,32000);loc.h=loc.w=256;loc.cache=None
    loc.min_score=.5;loc.keys=loc.positions=None
    rgb=np.zeros((256,256,3),np.uint8)
    rng=np.random.default_rng(54)
    mask=np.zeros((256,256),bool)
    mask[110:142,112:114]=True
    mask[110:112,112:139]=True
    mask[129:131,112:144]=True
    mask[130:148,142:144]=True
    palette=np.array([[153,153,153],[255,102,0],[255,255,0]],np.uint8)
    rgb[mask]=palette[rng.integers(0,3,int(mask.sum()))]
    other=np.zeros_like(rgb)
    other[40:65,160:175]=palette[rng.integers(0,3,(25,15))]
    loc.rgb={7:rgb,8:rgb.copy() if duplicate else other}
    loc.codes={z:palette_codes(im) for z,im in loc.rgb.items()}
    loc.build_index()
    return loc


@pytest.mark.parametrize('scale',[1.,1.25,2.,2.5,4.,5.])
def test_sparse_corridors_match_when_dense_patterns_are_absent(scale):
    loc=sparse_locator()
    axis=np.rint((np.arange(201)-100)/scale).astype(int)+128
    rgb=loc.rgb[7][axis[:,None],axis[None,:]].copy()
    rgb[96:105,100]=255;rgb[100,96:105]=255
    _,q,_=loc.normalize(rgb,scale,(100,100))
    assert not fingerprints(q)[1].any()
    result=loc.locate(rgb,scales=(scale,),anchor=(100,100))
    assert result.accepted,result
    assert (result.x,result.y,result.z)==(31128,32128,7)


def test_duplicate_sparse_rooms_remain_ambiguous():
    loc=sparse_locator(duplicate=True)
    rgb=loc.rgb[7][78:179,78:179]
    result=loc.locate(rgb,scales=(1,))
    assert result.accepted and result.reason=='ambiguous'
    assert (result.x,result.y,result.z)==(31128,32128,7)
    assert loc.locate(rgb,scales=(1,),floor=7).accepted


def test_masks_and_unsupported_colours_are_not_black_evidence():
    loc=sparse_locator()
    rgb=loc.rgb[7][78:179,78:179].copy()
    rgb[10:18,10:18]=(255,0,255)
    normalized,q,center=loc.normalize(rgb,1)
    observed=loc.sparse_observation(normalized,q,1,center)
    assert not observed[10:18,10:18].any()
    assert not observed[46:55,46:55].any()
    assert not observed[:2].any()
    assert observed[20,20] and q[20,20]==0


def test_masked_tiles_are_excluded_from_match_percentage():
    loc=sparse_locator()
    rgb=loc.rgb[7][78:179,78:179]
    normalized,q,center=loc.normalize(rgb,1)
    candidates=[(78,78,7)]
    assert loc.verify(q,candidates)[0][0]==1.
    # Deliberately disagree at every masked/unknown tile in the atlas patch.
    patch=loc.codes[7][78:179,78:179]
    patch[q==0]=193
    assert loc.verify(q,candidates)[0][0]==1.
    yy,xx=np.nonzero(q)
    patch[yy[:10],xx[:10]]=0
    assert loc.verify(q,candidates)[0][0]==pytest.approx((len(yy)-10)/len(yy))


@pytest.mark.parametrize('kind',['black','single_marker','noise'])
def test_empty_or_marker_only_sparse_queries_are_rejected(kind):
    loc=sparse_locator()
    rgb=np.zeros((101,101,3),np.uint8)
    if kind=='single_marker':
        rgb[10:16,10:16]=(255,102,0)
        rgb[12:14,12:14]=(153,153,153)
    if kind=='noise':
        rng=np.random.default_rng(321)
        for _ in range(20):
            x,y=rng.integers(5,94,2)
            rgb[y:y+2,x:x+2]=(255,102,0) if x%2 else (153,153,153)
    assert not loc.locate(rgb,scales=(1,1.25,2,2.5,4,5)).accepted
