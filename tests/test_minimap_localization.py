import unittest
import cv2
import numpy as np
from tibia_client.minimap_localization import MinimapLocator,palette_codes


def fixture(duplicate=False):
    rng=np.random.default_rng(12)
    palette=np.array([[0,204,0],[153,153,153],[255,102,0],[255,255,0]],np.uint8)
    rgb=palette[rng.integers(0,4,(128,128))]
    loc=MinimapLocator.__new__(MinimapLocator)
    loc.origin=(31000,32000);loc.h=loc.w=128;loc.cache=None
    loc.rgb={7:rgb,8:rgb.copy() if duplicate else palette[rng.integers(0,4,(128,128))]}
    loc.codes={z:palette_codes(im) for z,im in loc.rgb.items()}
    loc.gray={z:cv2.cvtColor(im,cv2.COLOR_RGB2GRAY) for z,im in loc.rgb.items()}
    loc.small={z:cv2.resize(im,(32,32),interpolation=cv2.INTER_AREA) for z,im in loc.gray.items()}
    loc.min_score=.92;loc.keys=loc.positions=None
    loc.build_index()
    return loc


class LocatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cv2.setNumThreads(1);cls.loc=fixture()

    def test_exact_coordinates_and_floor(self):
        crop=self.loc.rgb[7][45:76,30:61].copy()
        r=self.loc.locate(crop,scales=(1,))
        self.assertTrue(r.accepted,r)
        self.assertEqual((r.x,r.y,r.z),(31045,32060,7))

    def test_off_center_anchor_is_not_viewport_center(self):
        crop=self.loc.rgb[7][30:91,20:81].copy()
        r=self.loc.locate(crop,scales=(1,),anchor=(25,30))
        self.assertEqual((r.x,r.y,r.z),(31045,32060,7))

    def test_even_sized_crop_has_no_duplicated_sample_rows(self):
        crop=self.loc.rgb[7][30:90,20:80].copy()
        normalized,q,center=self.loc.normalize(crop,1)
        self.assertTrue(np.array_equal(normalized,crop[1:,1:]))
        r=self.loc.locate(crop,scales=(1,))
        self.assertEqual((r.x,r.y,r.z),(31050,32060,7))

    def test_masked_crosshair_and_unknown_border(self):
        crop=self.loc.rgb[7][30:91,20:81].copy()
        crop[:,:10]=0;crop[28:33,28:33]=(255,255,255)
        r=self.loc.locate(crop,scales=(1,))
        self.assertEqual((r.x,r.y,r.z),(31050,32060,7))

    def test_scaled_sampling(self):
        for scale in (1,1.25,2,2.5,4,5):
            size=121;anchor=60
            axis=np.rint((np.arange(size)-anchor)/scale).astype(int)
            crop=self.loc.rgb[7][(64+axis)[:,None],(64+axis)[None,:]]
            r=self.loc.locate(crop,scales=(scale,),anchor=(anchor,anchor))
            self.assertEqual((r.x,r.y,r.z),(31064,32064,7),(scale,r))

    def test_duplicate_floors_show_a_stable_coordinate_with_ambiguity_tag(self):
        loc=fixture(duplicate=True)
        crop=loc.rgb[7][45:76,30:61]
        r=loc.locate(crop,scales=(1,))
        self.assertTrue(r.accepted)
        self.assertEqual(r.reason,'ambiguous')
        self.assertEqual((r.x,r.y,r.z),(31045,32060,7))
        self.assertEqual(r.score,r.runner_up)
        r=loc.locate(crop,scales=(1,),floor=7)
        self.assertTrue(r.accepted)
        self.assertEqual(r.reason,'matched')

    def test_uninformative_rejection(self):
        for color in ((0,0,0),(0,204,0),(51,102,153)):
            crop=np.zeros((108,105,3),np.uint8);crop[:]=color
            self.assertFalse(self.loc.locate(crop,scales=(1,2,4)).accepted)

    def test_close_unequal_scores_show_winner_even_when_display_rounds_equal(self):
        loc=fixture(duplicate=True)
        crop=loc.rgb[8][10:111,10:111].copy()
        # One differing visible tile separates two otherwise identical floors.
        loc.codes[7][25,25]=0
        loc.build_index()
        r=loc.locate(crop,scales=(1,))
        self.assertTrue(r.accepted,r)
        self.assertEqual(r.reason,'matched')
        self.assertEqual((r.x,r.y,r.z),(31060,32060,8))
        self.assertGreater(r.score,r.runner_up)
        self.assertLess(r.score-r.runner_up,.04)
        self.assertEqual(f'{r.score:.1%}',f'{r.runner_up:.1%}')

    def test_duplicate_zoom_hypotheses_at_same_coordinate_are_not_ambiguous(self):
        crop=self.loc.rgb[7][45:76,30:61].copy()
        r=self.loc.locate(crop,scales=(1,1))
        self.assertTrue(r.accepted,r)
        self.assertEqual(r.reason,'matched')

    def test_invalid_inputs(self):
        with self.assertRaises(ValueError):self.loc.locate(np.zeros((10,10)),scales=(1,))
        with self.assertRaises(ValueError):self.loc.locate(self.loc.rgb[7],scales=(.0625,))
        with self.assertRaises(ValueError):self.loc.locate(self.loc.rgb[7],anchor=(-1,0))
        with self.assertRaises(ValueError):self.loc.locate(self.loc.rgb[7],floor=20)
        with self.assertRaises(ValueError):self.loc.locate(self.loc.rgb[7],method='local')
        with self.assertRaises(ValueError):self.loc.locate(self.loc.rgb[7],method='local',floor=8,prior=(31050,32060,7))

    def test_local_tracking_and_teleport_rejection(self):
        crop=self.loc.rgb[7][45:76,30:61]
        r=self.loc.locate(crop,scales=(1,),method='local',prior=(31048,32063,7))
        self.assertEqual((r.x,r.y,r.z),(31045,32060,7))
        r=self.loc.locate(crop,scales=(1,),method='local',prior=(31100,32100,7))
        self.assertFalse(r.accepted)

    def test_frame_adapter(self):
        class Frame: pixels=self.loc.rgb[7]
        r=self.loc.locate_frame(Frame(),(30,45,61,76),scales=(1,))
        self.assertEqual((r.x,r.y,r.z),(31045,32060,7))
        with self.assertRaises(ValueError):self.loc.locate_frame(Frame(),(-1,0,10,10))


if __name__=='__main__':unittest.main(verbosity=2)
