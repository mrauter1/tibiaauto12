"""Offline RGB minimap localization. No desktop, input, network or client imports.

The anchor is a point IN the supplied crop. World coordinates refer to that
anchor, not automatically to the player. Pass the actual player crosshair or
use a centered, current-floor minimap. Sizes and scales are explicit.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
import json
import hashlib
import time

import cv2
import numpy as np
from PIL import Image


DEFAULT_MIN_SCORE = .50
AUTO_SCALES = tuple(sorted({base*2.**power for base in (1.,1.25,1.5,1.75)
                           for power in range(-3,5) if .125 <= base*2.**power <= 16}))


PATTERN = ((0, 0), (0, 3), (0, 6), (3, 0), (3, 6), (6, 0), (6, 3), (6, 6))


def palette_codes(rgb):
    """Quantize the client's 6-level RGB palette; exclude off-palette overlays."""
    rgb = np.asarray(rgb)
    if rgb.dtype != np.uint8 or rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError('Expected uint8 RGB pixels, not BGR or RGBA')
    levels = np.rint(rgb.astype(np.float32) / 51).astype(np.uint16)
    good = np.max(np.abs(rgb.astype(np.int16) - levels.astype(np.int16)*51), axis=2) <= 22
    code = levels[:,:,0]*36 + levels[:,:,1]*6 + levels[:,:,2] + 1
    # Dark pixels share code zero with masks here; sparse_observation retains
    # their actual RGB identity for floor-aware black agreement checks.
    code[(~good) | (code == 1)] = 0
    return code.astype(np.uint8)


def fingerprints(code, step=1):
    h, w = code.shape
    border=6*step
    if min(h, w) <= border:
        return np.zeros((0, 0), np.uint64), np.zeros((0, 0), bool)
    key = np.zeros((h-border, w-border), np.uint64)
    valid = np.ones(key.shape, bool)
    lo = np.full(key.shape,255,np.uint8)
    hi = np.zeros(key.shape,np.uint8)
    for dy, dx in PATTERN:
        v = code[dy*step:dy*step+h-border, dx*step:dx*step+w-border]
        key = (key << np.uint64(8)) | v.astype(np.uint64)
        valid &= v != 0
        lo = np.minimum(lo,v)
        hi = np.maximum(hi,v)
    return key, valid & (lo != hi)


def boundary_fingerprints(code, observed=None):
    """3x3 coloured boundaries, allowing actual black but not masked overlays.

    Dense fingerprints require a fully explored 7x7 neighbourhood. These
    smaller keys retain the shape and colours of isolated rooms and corridors.
    """
    h, w = code.shape
    if min(h, w) < 3:
        return np.zeros((0, 0), np.uint64), np.zeros((0, 0), bool)
    key = np.zeros((h-2, w-2), np.uint64)
    known = np.zeros(key.shape, np.uint8)
    valid = code[1:-1,1:-1] != 0
    for dy, dx in ((0,0),(0,1),(0,2),(1,0),(1,2),(2,0),(2,1),(2,2)):
        values = code[dy:dy+h-2, dx:dx+w-2]
        key = (key << np.uint64(8)) | values.astype(np.uint64)
        known += values != 0
        if observed is not None:
            valid &= observed[dy:dy+h-2, dx:dx+w-2]
    if observed is not None:
        valid &= observed[1:-1,1:-1]
    return key, valid & (known >= 2) & (known <= 6)


@dataclass(frozen=True)
class MatchResult:
    accepted: bool
    x: int | None
    y: int | None
    z: int | None
    pixels_per_tile: float | None
    score: float
    runner_up: float
    support: int
    reason: str
    elapsed_ms: float
    resolution_tiles: int = 1


class MinimapLocator:
    """Preload once, reuse across frames; all match operations are read-only.

    Similarity counts normalized pixels, with water capped at 20% of weight.
    It is NOT a probability.
    Exact score ties among retrieved coordinates are displayed as ambiguous.
    A trusted previous coordinate restricts local searches to its floor/radius.
    """
    def __init__(self, map_dir, *, build_index=True, min_score=DEFAULT_MIN_SCORE, cache_dir=None, checkpoint=None):
        if not np.isfinite(min_score) or not 0 <= min_score <= 1:
            raise ValueError('Minimum match must be between 0 and 100 percent')
        start = time.perf_counter()
        self._checkpoint = checkpoint or (lambda: None)
        self._checkpoint()
        self.map_dir = Path(map_dir)
        manifest = json.loads((self.map_dir/'manifest.json').read_text())
        self.cache = Path(cache_dir) if cache_dir else None
        fingerprint=hashlib.sha256((self.map_dir/'manifest.json').read_bytes())
        fingerprint.update(b'color-index-v1')
        for item in manifest['floors']:
            self._checkpoint()
            fingerprint.update((self.map_dir/item['file']).read_bytes())
        self.fingerprint=fingerprint.hexdigest()
        self.cache = self.cache/self.fingerprint if self.cache else None
        if self.cache:self.cache.mkdir(parents=True,exist_ok=True)
        self.origin = (manifest['origin_x'], manifest['origin_y'])
        self.codes = {}
        self.rgb = {}
        self.gray = {}
        self.small = {}
        for item in manifest['floors']:
            self._checkpoint()
            z = item['floor']
            if self.cache and (self.cache/f'small{z}.npy').exists():
                for name in ('rgb','codes','gray','small'):
                    getattr(self,name)[z]=np.load(self.cache/f'{name}{z}.npy',mmap_mode='r',allow_pickle=False)
                continue
            rgb = np.array(Image.open(self.map_dir/item['file']).convert('RGB'))
            self.rgb[z] = rgb
            self.codes[z] = palette_codes(rgb)
            self.gray[z] = cv2.cvtColor(rgb,cv2.COLOR_RGB2GRAY)
            self.small[z] = cv2.resize(self.gray[z],None,fx=.25,fy=.25,interpolation=cv2.INTER_AREA)
            if self.cache:
                for name in ('rgb','codes','gray','small'):
                    np.save(self.cache/f'{name}{z}.npy',getattr(self,name)[z],allow_pickle=False)
        self.h,self.w = next(iter(self.codes.values())).shape
        self.min_score = min_score
        self._ensure_palette()
        self.load_ms = (time.perf_counter()-start)*1000
        self.index_ms = 0
        self.keys = self.positions = None
        if build_index:
            self.build_index()
            self.build_boundary_index()

    def build_zoom_index(self, step):
        """Index every sampling phase, not just one aligned pyramid image.

        Positions remain full-resolution world pixels. A step of two observes
        one atlas tile every two tiles, as in a half-size minimap.
        """
        if step == 1:
            return self.keys, self.positions
        if not hasattr(self, '_zoom_indexes'):
            self._zoom_indexes = {}
        if step in self._zoom_indexes:
            return self._zoom_indexes[step]
        cache=getattr(self,'cache',None)
        stem=f'zoom-stride-{step}-v1'
        if cache and (cache/f'{stem}-positions.npy').exists():
            result=tuple(np.load(cache/f'{stem}-{name}.npy',mmap_mode='r',allow_pickle=False)
                         for name in ('keys','positions'))
            self._zoom_indexes[step]=result
            return result
        keys,positions=[],[]
        for z,code in self.codes.items():
            getattr(self,'_checkpoint',lambda:None)()
            k,valid=fingerprints(code,step)
            yy,xx=np.nonzero(valid)
            keys.append(k[valid])
            positions.append((z*self.h*self.w+yy*self.w+xx).astype(np.int32))
        keys,positions=np.concatenate(keys),np.concatenate(positions)
        order=np.argsort(keys,kind='stable')
        keys,positions=keys[order],positions[order]
        _,counts=np.unique(keys,return_counts=True)
        keep=np.repeat(counts<=48,counts)
        result=(keys[keep],positions[keep])
        self._zoom_indexes[step]=result
        if cache:
            for name,data in zip(('keys','positions'),result):
                np.save(cache/f'{stem}-{name}.npy',data,allow_pickle=False)
        return result

    def prepare_scales(self, scales=AUTO_SCALES):
        for step in sorted({self.sample_step(s) for s in scales}):
            getattr(self,'_checkpoint',lambda:None)()
            self.build_zoom_index(step)

    @staticmethod
    def sample_step(scale):
        if not np.isfinite(scale) or not .125 <= scale <= 16:
            raise ValueError('Pixels per tile must be between 0.125 and 16')
        return 2**max(0,int(np.ceil(np.log2(1/scale))))

    def infer_scales(self, rgb, capture_codes=None):
        """Measure pixel-grid compatibility before searching atlas zoom levels.

        Colour boundaries should lie on a rasterized tile grid when tiles are
        larger than a pixel. Below a pixel, the grid alone cannot identify world
        scale: the stride indexes resolve that using actual terrain patterns.
        """
        q=(self.quantize_capture(rgb) if capture_codes is None else capture_codes).copy()
        q[:3]=0;q[-3:]=0;q[:,:3]=0;q[:,-3:]=0
        histograms=(np.r_[0,((q[:,1:]!=q[:,:-1])&(q[:,1:]>0)&(q[:,:-1]>0)).sum(axis=0)],
                    np.r_[0,((q[1:]!=q[:-1])&(q[1:]>0)&(q[:-1]>0)).sum(axis=1)])
        histograms=[hist for hist in histograms if hist.sum()>=80]
        if not histograms:
            return AUTO_SCALES
        if not hasattr(self,'_grid_models'):
            self._grid_models={}
        # Keep the client's established zoom families even when overlays or
        # resampling blur their edges. Grid evidence only prunes extra UI scales.
        compatible={1.,1.25,2.,2.5,4.,5.}
        grid_scores=[]
        for pitch in sorted({scale*self.sample_step(scale) for scale in AUTO_SCALES}):
            if pitch==1:continue
            evidence=[]
            for hist in histograms:
                key=(len(hist),pitch)
                if key not in self._grid_models:
                    if len(self._grid_models)>=128:self._grid_models.clear()
                    coordinates=np.arange(len(hist))[None,:]
                    phases=np.arange(0,pitch,.125)[:,None]
                    boundaries=np.floor(np.floor((coordinates-phases)/pitch+.5)*pitch+phases+.5)
                    masks=boundaries==coordinates
                    masks[:,:3]=False;masks[:,-3:]=False
                    self._grid_models[key]=(masks,masks[:,3:-3].mean(axis=1))
                masks,expected=self._grid_models[key]
                support=(masks*hist).sum(axis=1)/hist.sum()
                evidence.append(float(np.max(support-expected)))
            if min(evidence)>=.02:
                compatible.add(pitch)
            grid_scores.append((min(evidence),pitch))
        grid_scores.sort(reverse=True)
        if (len(grid_scores)>1 and grid_scores[0][0]>.35 and
                grid_scores[0][0]-grid_scores[1][0]>.10):
            # Clear large-pixel grids rule out unrelated extra UI-scale
            # families. Retain power-of-two relatives: a terrain boundary may
            # cover several tiles, so grid spacing alone is not world scale.
            best=grid_scores[0][1]
            compatible={p for p in compatible if p in (1.,1.25,2.,2.5,4.,5.) or
                        abs(np.log2(p/best)-round(np.log2(p/best)))<1e-8}
        return tuple(scale for scale in AUTO_SCALES if scale*self.sample_step(scale) in compatible)

    def build_boundary_index(self):
        if hasattr(self, 'boundary_keys'):
            return
        cache = getattr(self, 'cache', None)
        if cache and (cache/'boundary-v1-positions.npy').exists():
            self.boundary_keys = np.load(cache/'boundary-v1-keys.npy', mmap_mode='r', allow_pickle=False)
            self.boundary_positions = np.load(cache/'boundary-v1-positions.npy', mmap_mode='r', allow_pickle=False)
            return
        keys, positions = [], []
        for z, code in self.codes.items():
            getattr(self, '_checkpoint', lambda: None)()
            k, valid = boundary_fingerprints(code)
            yy, xx = np.nonzero(valid)
            keys.append(k[valid])
            positions.append((z*self.h*self.w+yy*self.w+xx).astype(np.int32))
        keys, positions = np.concatenate(keys), np.concatenate(positions)
        order = np.argsort(keys, kind='stable')
        keys, positions = keys[order], positions[order]
        _, counts = np.unique(keys, return_counts=True)
        keep = np.repeat(counts <= 256, counts)
        self.boundary_keys, self.boundary_positions = keys[keep], positions[keep]
        if cache:
            np.save(cache/'boundary-v1-keys.npy', self.boundary_keys, allow_pickle=False)
            np.save(cache/'boundary-v1-positions.npy', self.boundary_positions, allow_pickle=False)

    def build_index(self):
        start=time.perf_counter()
        checkpoint=getattr(self, '_checkpoint', lambda: None)
        checkpoint()
        if self.cache and (self.cache/'positions.npy').exists():
            self.keys=np.load(self.cache/'keys.npy',mmap_mode='r',allow_pickle=False)
            self.positions=np.load(self.cache/'positions.npy',mmap_mode='r',allow_pickle=False)
            self.index_ms=(time.perf_counter()-start)*1000
            return
        keys,positions=[],[]
        for z,code in self.codes.items():
            checkpoint()
            k,v=fingerprints(code)
            yy,xx=np.nonzero(v)
            keys.append(k[v])
            positions.append((z*self.h*self.w+yy*self.w+xx).astype(np.int32))
        keys=np.concatenate(keys)
        positions=np.concatenate(positions)
        order=np.argsort(keys,kind='stable')
        checkpoint()
        keys,positions=keys[order],positions[order]
        unique,first,count=np.unique(keys,return_index=True,return_counts=True)
        # Repeated patterns are poor evidence and would produce huge vote lists.
        keep=np.repeat(count<=48,count)
        self.keys,self.positions=keys[keep],positions[keep]
        if self.cache:
            np.save(self.cache/'keys.npy',self.keys,allow_pickle=False)
            np.save(self.cache/'positions.npy',self.positions,allow_pickle=False)
        self.index_ms=(time.perf_counter()-start)*1000

    def _ensure_palette(self):
        if hasattr(self, 'capture_palette'):
            return
        cache = getattr(self, 'cache', None)
        palette_path = cache/'palette_ids.npy' if cache else None
        if palette_path is not None and palette_path.exists():
            ids = np.load(palette_path, allow_pickle=False)
        else:
            counts = np.zeros(256, np.int64)
            for code in self.codes.values():
                getattr(self, '_checkpoint', lambda: None)()
                counts += np.bincount(code.ravel(), minlength=256)
            ids = np.unique(np.r_[np.uint8(0), np.flatnonzero(counts).astype(np.uint8)])
            if palette_path is not None:
                np.save(palette_path, ids, allow_pickle=False)
        values = ids.astype(np.int16)-1
        palette = np.stack((values//36*51, values//6%6*51, values%6*51), axis=1)
        palette[ids == 0] = 0
        self.capture_palette_ids = ids
        self.capture_palette = palette.astype(np.float32)

    def quantize_capture(self, rgb):
        """Snap rendered colours to colours actually present in this atlas.

        Do not fabricate all 216 cube colours: UI scaling and the capture source
        can introduce thousands of blended RGB values. Far-off colours are masked.
        """
        self._ensure_palette()
        pixels = rgb.astype(np.float32)
        nearest = np.full(rgb.shape[:2], np.inf, np.float32)
        codes = np.zeros(rgb.shape[:2], np.uint8)
        for code, color in zip(self.capture_palette_ids, self.capture_palette):
            distance = sum((pixels[:,:,channel]-color[channel])**2 for channel in range(3))
            better = distance < nearest
            codes[better] = code
            nearest[better] = distance[better]
        codes[nearest > 70**2] = 0
        return codes

    def normalize(self, rgb, scale, anchor=None, *, capture_codes=None):
        rgb=np.asarray(rgb)
        if rgb.ndim != 3 or rgb.shape[2] != 3 or rgb.dtype != np.uint8:
            raise ValueError('Expected uint8 RGB image')
        if not np.isfinite(scale) or scale < 1:
            raise ValueError('Exact tile detection requires pixels_per_tile >= 1')
        h,w=rgb.shape[:2]
        ax,ay=anchor if anchor is not None else ((w-1)/2,(h-1)/2)
        if not (0 <= ax < w and 0 <= ay < h):
            raise ValueError('Anchor outside crop')
        rx=int(min(ax,w-1-ax)//scale)
        ry=int(min(ay,h-1-ay)//scale)
        if min(rx,ry)<5:
            raise ValueError('Too few visible tiles around anchor')
        # Half-up is essential: np.rint uses ties-to-even. At a half-pixel
        # anchor, that duplicates/skips alternating rows at integer zoom.
        xs=np.floor(ax+np.arange(-rx,rx+1)*scale+.5).astype(int)
        ys=np.floor(ay+np.arange(-ry,ry+1)*scale+.5).astype(int)
        normalized=np.ascontiguousarray(rgb[ys[:,None],xs[None,:]])
        q=(self.quantize_capture(normalized) if capture_codes is None else
           capture_codes[ys[:,None],xs[None,:]].copy())
        # Calibration boxes can include a one-pixel frame. It is not terrain.
        q[:2]=0; q[-2:]=0; q[:,:2]=0; q[:,-2:]=0
        # Crosshair is typically 5-9 physical pixels. Mask its support in tile units.
        r=max(1,int(np.ceil(4/scale)))
        q[ry-r:ry+r+1,rx-r:rx+r+1]=0
        return normalized,q,(rx,ry)

    def index_candidates(self,q,floor=None,step=1,query_fingerprint=None):
        if self.keys is None: raise RuntimeError('Build the colour index first')
        k,valid=fingerprints(q) if query_fingerprint is None else query_fingerprint
        keys,positions=self.build_zoom_index(step)
        return self._index_votes(q, k, valid, keys, positions, floor, step=step)

    def boundary_candidates(self, q, observed, floor=None):
        self.build_boundary_index()
        k, valid = boundary_fingerprints(q, observed)
        return self._index_votes(q, k, valid, self.boundary_keys, self.boundary_positions, floor, limit=64)

    def _index_votes(self, q, k, valid, keys, positions, floor, limit=24, step=1):
        yy,xx=np.nonzero(valid)
        if not len(yy): return []
        # Bound query work, with samples spread over the entire visible map.
        take=np.linspace(0,len(yy)-1,min(768,len(yy))).astype(int)
        yy,xx=yy[take],xx[take]
        k=k[yy,xx]
        order=np.argsort(k,kind='stable')
        k,yy,xx=k[order],yy[order],xx[order]
        left=np.searchsorted(keys,k,'left')
        right=np.searchsorted(keys,k,'right')
        counts=right-left
        total=int(counts.sum())
        if not total:return []
        # Expand ragged posting lists in one NumPy operation, rather than
        # allocating several small arrays for each of up to 768 query keys.
        offsets=np.repeat(left,counts)+np.arange(total)-np.repeat(np.cumsum(counts)-counts,counts)
        pos=positions[offsets].astype(np.int64)
        z=pos//(self.h*self.w)
        py=pos//self.w%self.h-np.repeat(yy,counts)*step
        px=pos%self.w-np.repeat(xx,counts)*step
        ok=(px>=0)&(py>=0)&(px+(q.shape[1]-1)*step<self.w)&(py+(q.shape[0]-1)*step<self.h)
        if floor is not None:ok &= z==floor
        votes=(z*self.h*self.w+py*self.w+px)[ok]
        values,counts=np.unique(votes,return_counts=True)
        order=np.argsort(-counts,kind='stable')[:limit]
        return [(int(v%self.w),int(v//self.w%self.h),int(v//(self.w*self.h)))
                for v,n in zip(values[order],counts[order]) if n>=1]

    def template_candidates(self,rgb,q,method='coarse',floor=None,prior=None,radius=12):
        gray=cv2.cvtColor(rgb,cv2.COLOR_RGB2GRAY)
        result=[]
        floors=[floor] if floor is not None else sorted(self.gray)
        for z in floors:
            if z not in self.gray: continue
            ref=self.gray[z]
            if prior is not None:
                px=prior[0]-self.origin[0]-q.shape[1]//2
                py=prior[1]-self.origin[1]-q.shape[0]//2
                x0=max(0,px-radius);y0=max(0,py-radius)
                region=ref[y0:min(self.h,py+radius+q.shape[0]),x0:min(self.w,px+radius+q.shape[1])]
                if region.shape[0]<q.shape[0] or region.shape[1]<q.shape[1]:continue
                scores=cv2.matchTemplate(region,gray,cv2.TM_SQDIFF,mask=(q>0).astype(np.uint8))
                for _ in range(8):
                    _,_,(x,y),_=cv2.minMaxLoc(scores)
                    result.append((x+x0,y+y0,z));scores[y,x]=np.inf
            elif method=='gray':
                scores=cv2.matchTemplate(ref,gray,cv2.TM_CCOEFF_NORMED)
                for _ in range(4):
                    _,_,_,(x,y)=cv2.minMaxLoc(scores)
                    result.append((x,y,z))
                    scores[max(0,y-2):y+3,max(0,x-2):x+3]=-1
            else:
                tiny=cv2.resize(gray,(max(2,q.shape[1]//4),max(2,q.shape[0]//4)),interpolation=cv2.INTER_AREA)
                scores=cv2.matchTemplate(self.small[z],tiny,cv2.TM_CCOEFF_NORMED)
                for _ in range(4):
                    _,_,_,(cx,cy)=cv2.minMaxLoc(scores)
                    scores[max(0,cy-2):cy+3,max(0,cx-2):cx+3]=-1
                    x0=max(0,cx*4-8);y0=max(0,cy*4-8)
                    region=ref[y0:min(self.h,cy*4+8+q.shape[0]),x0:min(self.w,cx*4+8+q.shape[1])]
                    if region.shape[0]<q.shape[0] or region.shape[1]<q.shape[1]: continue
                    fine=cv2.matchTemplate(region,gray,cv2.TM_SQDIFF,mask=(q>0).astype(np.uint8))
                    for _ in range(2):
                        _,_,(x,y),_=cv2.minMaxLoc(fine)
                        result.append((x0+x,y0+y,z));fine[y,x]=np.inf
        return result

    def sparse_observation(self, normalized, q, scale, center):
        # Actual dark pixels are context; zeros caused by masking/unsupported
        # overlay colours must never masquerade as a black room boundary.
        observed = (q != 0) | (np.max(normalized, axis=2) <= 30)
        observed[:2]=False; observed[-2:]=False
        observed[:,:2]=False; observed[:,-2:]=False
        ax, ay = center
        r=max(1,int(np.ceil(4/scale)))
        observed[ay-r:ay+r+1,ax-r:ax+r+1]=False
        return observed

    def verify(self,q,candidates,step=1,*,black_mask=None,strict_surface_black=False):
        yy,xx=np.nonzero(q)
        if len(yy)<48:return []
        values=q[yy,xx]
        # Each observed terrain tile contributes equally. Inverse-frequency
        # weighting over-amplifies rare marker colours relative to real terrain.
        weights=np.ones(len(values), np.float64)
        # Large water regions must not turn an unrelated island into an accepted match.
        # Keep water at no more than 20% of the score and require terrain evidence.
        land = values != 52
        if np.count_nonzero(land) < 48 or np.count_nonzero(np.bincount(values[land],minlength=256)>=8)<2:
            return []
        water_weight = weights[~land].sum()
        if water_weight:
            weights[~land] *= min(1., weights[land].sum()*.25/water_weight)
        denominator=weights.sum()
        land_mask=(q!=0)&(q!=52)
        water_mask=q==52
        water_unit=min(1.,np.count_nonzero(land_mask)*.25/max(1,np.count_nonzero(water_mask)))
        black_count=0 if black_mask is None else int(np.count_nonzero(black_mask))
        ranked=[]
        for x,y,z in set(candidates):
            ref=np.asarray(self.codes[z])
            if x<0 or y<0 or x+(q.shape[1]-1)*step>=self.w or y+(q.shape[0]-1)*step>=self.h:continue
            patch=ref[y:y+q.shape[0]*step:step,x:x+q.shape[1]*step:step]
            eq=patch==q
            matched=np.count_nonzero(eq & land_mask)+water_unit*np.count_nonzero(eq & water_mask)
            score=min(1.,float(matched/denominator))
            if black_count and (z!=7 or strict_surface_black):
                # Black is an observed colour, not a masked overlay. Matching
                # black participates in the full comparison, but cannot raise
                # the score above terrain agreement and rescue an unrelated room.
                black_matches=np.count_nonzero((patch==0)&black_mask)
                score=min(score,float((matched+black_matches)/(denominator+black_count)))
            ranked.append((score,x,y,z))
        return sorted(ranked,reverse=True)

    def locate(self,rgb,*,scales=AUTO_SCALES,anchor=None,floor=None,method='index',prior=None,radius=12):
        start=time.perf_counter()
        if method=='hybrid':
            fast=self.locate(rgb,scales=scales,anchor=anchor,floor=floor,method='index')
            if fast.accepted:return fast
            native_scales=tuple(s for s in scales if s>=1)
            if not native_scales:return fast
            fallback=self.locate(rgb,scales=native_scales,anchor=anchor,floor=floor,method='coarse')
            return MatchResult(fallback.accepted,fallback.x,fallback.y,fallback.z,fallback.pixels_per_tile,
                               fallback.score,fallback.runner_up,fallback.support,fallback.reason,
                               (time.perf_counter()-start)*1000,fallback.resolution_tiles)
        rgb=np.asarray(rgb)
        if rgb.ndim!=3 or rgb.shape[2]!=3 or rgb.dtype!=np.uint8:
            raise ValueError('Expected uint8 RGB image')
        if not scales:raise ValueError('At least one scale is required')
        if anchor is not None and not (0<=anchor[0]<rgb.shape[1] and 0<=anchor[1]<rgb.shape[0]):
            raise ValueError('Anchor outside crop')
        if floor is not None and floor not in self.codes: raise ValueError('Floor absent from atlas')
        if prior is not None and (len(prior)!=3 or prior[2] not in self.codes):
            raise ValueError('Invalid trusted previous coordinate')
        if prior is not None and floor is not None and prior[2]!=floor:
            raise ValueError('Previous coordinate conflicts with displayed floor')
        if method not in ('index','gray','coarse','local'):
            raise ValueError('Unknown matching method')
        if method=='local' and prior is None:raise ValueError('Local search requires a trusted prior')
        ranked=[]
        support=0
        capture_codes=self.quantize_capture(rgb)
        if tuple(scales)==AUTO_SCALES and method=='index':
            scales=self.infer_scales(rgb,capture_codes)
        normalized_cache={}
        for scale in scales:
            getattr(self, '_checkpoint', lambda: None)()
            step=self.sample_step(scale)
            pitch=scale*step
            if step>1 and method!='index':
                raise ValueError('Zoomed-out maps require the indexed matcher')
            try:
                if pitch not in normalized_cache:
                    normalized,q,center=self.normalize(rgb,pitch,anchor,capture_codes=capture_codes)
                    normalized_cache[pitch]=(normalized,q,center,fingerprints(q))
                normalized,q,(ax,ay),query_fingerprint=normalized_cache[pitch]
            except ValueError:
                # Invalid pixel contracts must not be swallowed.
                if np.asarray(rgb).dtype!=np.uint8:raise
                continue
            n=int(np.count_nonzero(q));support=max(support,n)
            colors=np.bincount(q[q>0],minlength=256)
            if n<48 or np.count_nonzero(colors>=8)<2:continue
            observed = self.sparse_observation(normalized, q, pitch, (ax,ay))
            black_mask=observed & (q==0)
            if method=='index':
                candidates=self.index_candidates(q,floor,step,query_fingerprint)
                if step==1 and np.count_nonzero(black_mask) >= q.size*.10:
                    candidates += self.boundary_candidates(q, observed, floor)
            else:candidates=self.template_candidates(normalized,q,method,prior[2] if prior else floor,prior,radius)
            # In Auto, a predominantly black view must not bypass geometry by
            # selecting a ground-floor candidate. Explicit floor 7 preserves
            # the existing unexplored-ground masking policy. This is a visual
            # heuristic, not a reliable observation of the displayed floor.
            strict_surface_black=(floor is None and
                                  np.count_nonzero(black_mask)>=np.count_nonzero(observed)*.25)
            for score,x,y,z in self.verify(q,candidates,step,black_mask=black_mask,
                                           strict_surface_black=strict_surface_black):
                ranked.append((score,x+ax*step+self.origin[0],y+ay*step+self.origin[1],z,float(scale),n))
        # Multiple scale hypotheses can nominate the same world coordinate.
        unique={}
        for r in ranked:
            key=r[1:4]
            if key not in unique or r[0]>unique[key][0]:unique[key]=r
        # Exact ties use a stable XYZ order, so repeated frames do not alternate
        # arbitrarily between equally scoring places.
        ranked=sorted(unique.values(),key=lambda row: (-row[0],row[1],row[2],row[3]))
        elapsed=(time.perf_counter()-start)*1000
        if not ranked:return MatchResult(False,None,None,None,None,0,0,support,'insufficient distinctive evidence',elapsed)
        best=ranked[0];second=ranked[1][0] if len(ranked)>1 else 0.
        score,x,y,z,scale,n=best
        accepted=score>=self.min_score
        tied=len(ranked)>1 and score==second
        reason=('ambiguous' if tied else 'matched') if accepted else 'low agreement'
        return MatchResult(accepted,x if accepted else None,y if accepted else None,z if accepted else None,
                           scale if accepted else None,score,second,n,reason,elapsed,
                           int(np.ceil(1/scale)) if scale<1 else 1)

    def locate_frame(self,frame,box,**kwargs):
        """tibiaauto12 core.perception.Frame adapter. xyxy client-local bounds."""
        l,t,r,b=box
        pixels=frame.pixels
        if not (0<=l<r<=pixels.shape[1] and 0<=t<b<=pixels.shape[0]):
            raise ValueError('Invalid client-local minimap box')
        return self.locate(pixels[t:b,l:r],**kwargs)
