"""Offline Windows OCR timing on a complete corpus screenshot; no live input."""
from __future__ import annotations

import argparse
import asyncio
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import time

import cv2
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.benchmark_battle_glyphs import (  # noqa: E402
    CORPUS, DEFAULT_FONT, load_corpus, production_mask, provenance, text_fingerprint,
)

SOURCE_ID = 'reddit-valouran-pl7tust'
MODES = (('rgb', 1), ('rgb', 3), ('neutral-mask', 1), ('neutral-mask', 3))


def fit_canvas(rgb, width=1024, height=768):
    """Preserve the complete frame and aspect ratio; explicitly report padding."""
    h, w = rgb.shape[:2]
    scale = min(width/w, height/h)
    nw, nh = round(w*scale), round(h*scale)
    resized = cv2.resize(rgb, (nw, nh), interpolation=cv2.INTER_AREA)
    x, y = (width-nw)//2, (height-nh)//2
    canvas = np.zeros((height, width, 3), np.uint8)
    canvas[y:y+nh, x:x+nw] = resized
    return canvas, {'scale': scale, 'content_box': [x, y, x+nw, y+nh],
                    'interpolation': 'INTER_AREA', 'padding': 'black',
                    'note': 'Derived workload, not a native 1024x768 client capture.'}


def prepare(rgb, mode, scale):
    if mode not in ('rgb', 'neutral-mask') or scale not in (1, 3):
        raise ValueError('Unsupported benchmark mode or enlargement')
    if mode == 'neutral-mask':
        pixels = np.where(production_mask(rgb), 0, 255).astype(np.uint8)
        conversion = cv2.COLOR_GRAY2BGRA
    else:
        pixels = rgb
        conversion = cv2.COLOR_RGB2BGRA
    if scale != 1:
        pixels = cv2.resize(pixels, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
    return cv2.cvtColor(pixels, conversion)


async def _recognize(bgra, scale):
    from winrt.windows.globalization import Language
    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter

    language = Language('en-US')
    engine = (OcrEngine.try_create_from_language(language)
              if OcrEngine.is_language_supported(language) else
              OcrEngine.try_create_from_user_profile_languages())
    if engine is None:
        raise RuntimeError('Windows OCR has no recognition language')
    if max(bgra.shape[:2]) > OcrEngine.max_image_dimension:
        raise ValueError('Image exceeds Windows OCR limit')
    with DataWriter() as writer:
        writer.write_bytes(bgra.tobytes())
        with SoftwareBitmap.create_copy_from_buffer(
            writer.detach_buffer(), BitmapPixelFormat.BGRA8, bgra.shape[1], bgra.shape[0]
        ) as bitmap:
            result = await engine.recognize_async(bitmap)
            lines = []
            for line in result.lines:
                words = []
                for word in line.words:
                    b = word.bounding_rect
                    words.append({'text': word.text, 'box': [b.x/scale, b.y/scale,
                                 (b.x+b.width)/scale, (b.y+b.height)/scale]})
                if words:
                    boxes = [word['box'] for word in words]
                    lines.append({'text': line.text, 'words': words,
                                  'box': [min(b[0] for b in boxes), min(b[1] for b in boxes),
                                          max(b[2] for b in boxes), max(b[3] for b in boxes)]})
            return lines


def recognize(bgra, scale):
    if os.name != 'nt':
        raise RuntimeError('This benchmark requires native Windows OCR')
    from winrt.runtime import ApartmentType, init_apartment, uninit_apartment
    init_apartment(ApartmentType.MULTI_THREADED)
    try:
        return asyncio.run(_recognize(bgra, scale))
    finally:
        uninit_apartment()


def stats(values):
    return {'n': len(values), 'p50_ms': float(np.percentile(values, 50)),
            'p95_ms': float(np.percentile(values, 95)), 'worst_ms': max(values)} if values else None


def benchmark(inputs, repeats, decoder=recognize):
    cases = []
    for name, rgb, detail in inputs:
        for mode, scale in MODES:
            cases.append({'id': f'{name}/{mode}/{scale}x', 'input': name,
                          'mode': mode, 'enlargement': scale,
                          'input_size': [rgb.shape[1], rgb.shape[0]],
                          'ocr_size': [rgb.shape[1]*scale, rgb.shape[0]*scale],
                          'input_rgb_sha256': sha256(rgb.tobytes()).hexdigest(),
                          'transform': detail, 'calls': [], 'first_result': None})
    pixels = {name: rgb for name, rgb, _ in inputs}
    order = []
    for iteration in range(repeats+1):
        # Rotate cases to reduce a systematic late/early-run advantage.
        offset = iteration % len(cases)
        for case in cases[offset:]+cases[:offset]:
            start = time.perf_counter_ns()
            error, lines, prepared_ms, bgra = None, [], None, None
            try:
                bgra = prepare(pixels[case['input']], case['mode'], case['enlargement'])
                prepared_ms = (time.perf_counter_ns()-start)/1e6
                lines = decoder(bgra, case['enlargement'])
            except Exception as exc:
                error = f'{type(exc).__name__}: {exc}'
            bgra = None
            elapsed = (time.perf_counter_ns()-start)/1e6
            # Result serialization and statistics are outside recognition timing.
            fingerprint = sha256(json.dumps(lines, sort_keys=True).encode()).hexdigest()
            case['calls'].append({'pass': iteration, 'total_ms': elapsed,
                                  'preprocessing_ms': prepared_ms, 'error': error,
                                  'lines': len(lines), 'words': sum(len(x['words']) for x in lines),
                                  'result_sha256': fingerprint})
            if iteration == 0:
                case['first_result'] = lines
            order.append({'case': case['id'], 'pass': iteration})
    for case in cases:
        calls = case['calls']
        valid = [c for c in calls[1:] if c['error'] is None]
        case['repeat_latency'] = stats([c['total_ms'] for c in valid])
        case['repeat_preprocessing'] = stats([c['preprocessing_ms'] for c in valid])
        case['errors'] = sum(c['error'] is not None for c in calls)
        case['stable_results'] = len({(c['error'], c['result_sha256']) for c in calls}) == 1
    return {'cases': cases, 'invocation_order': order}


def load_inputs():
    manifest = json.loads((CORPUS/'manifest.json').read_text(encoding='utf-8'))
    source = next(s for s in manifest['screenshots'] if s['id'] == SOURCE_ID)
    path = CORPUS/source['file']
    if sha256(path.read_bytes()).hexdigest() != source['integrity']['sha256']:
        raise ValueError('Source screenshot hash mismatch')
    rgb = np.asarray(Image.open(path).convert('RGB'))
    fitted, transform = fit_canvas(rgb)
    # Reuse the unchanged, hash-verified row selection as a same-run control.
    rows, audit = load_corpus(CORPUS/'visual-annotations-2026-10-22.json', CORPUS/'manifest.json')
    row = next(c for c in rows if c.screenshot_id == SOURCE_ID and c.kind == 'positive_row')
    return [('full-original', rgb, {'note': 'Unchanged entire source screenshot.'}),
            ('full-fit-1024x768', fitted, transform),
            ('name-row-control', row.pixels, {'source_box': row.metadata['box']})], {
                'source_id': SOURCE_ID, 'source_file': str(path.relative_to(ROOT)),
                'source_sha256': source['integrity']['sha256'],
                'annotation_freeze': audit['freeze_verification']}


def format_report(report):
    lines = ['Full screenshot Windows OCR latency', f"Commit: {report['environment']['commit']}",
             f"Source: {report['source']['source_id']}",
             'All durations include preprocessing, COM/engine setup, buffer transfer, recognition and extraction.',
             'Disk decoding, canvas fitting, capture and JSON serialization are excluded.',
             'First = first call for that case, not independent cold OS/model initialization.',
             'Repeats are fresh OCR calls; no result cache. Word counts are NOT accuracy scores.',
             '1024x768 = full source fitted to 1024x576 with black bars; not a native client capture.',
             '', 'Case | OCR pixels | First ms | Repeat p50/p95 ms | Prep p50 ms | Lines/words | Errors']
    for case in report['cases']:
        timing, prep, first = case['repeat_latency'], case['repeat_preprocessing'], case['calls'][0]
        latency = f"{timing['p50_ms']:.3f}/{timing['p95_ms']:.3f}" if timing else 'unavailable'
        preparation = f"{prep['p50_ms']:.3f}" if prep else 'unavailable'
        lines.append(f"{case['id']} | {case['ocr_size']} | {first['total_ms']:.3f} | {latency} | "
                     f"{preparation} | {first['lines']}/{first['words']} | {case['errors']}")
    lines += ['', 'Production neutral-mask 3x equivalence: '+str(report['production_equivalence']),
              'One heterogeneous screenshot; full-screen ground truth is unavailable. No accuracy claim.']
    return '\n'.join(lines)+'\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repeats', type=int, default=20)
    parser.add_argument('--output-prefix', type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 100:
        parser.error('--repeats must be between 1 and 100')
    inputs, source = load_inputs()
    report = benchmark(inputs, args.repeats)
    report.update(source=source, repeats=args.repeats, environment=provenance(DEFAULT_FONT))
    report['environment']['code_fingerprints']['tools/benchmark_fullscreen_ocr.py'] = text_fingerprint(Path(__file__))
    report['production_equivalence'] = {}
    if os.name == 'nt':
        from tibia_client.battle_text import _read_lines
        for name, rgb, _ in inputs:
            case = next(c for c in report['cases'] if c['id'] == f'{name}/neutral-mask/3x')
            try:
                expected = [(label, list(box)) for label, box in _read_lines(production_mask(rgb))]
                actual = [(line['text'], line['box']) for line in case['first_result']]
                report['production_equivalence'][name] = actual == expected
            except Exception as exc:
                report['production_equivalence'][name] = f'{type(exc).__name__}: {exc}'
    args.output_prefix.parent.mkdir(parents=True, exist_ok=True)
    args.output_prefix.with_suffix('.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    text = format_report(report)
    args.output_prefix.with_suffix('.txt').write_text(text, encoding='utf-8')
    print(text)
    return 1 if (any(c['errors'] for c in report['cases']) or
                 any(v is not True for v in report['production_equivalence'].values())) else 0


if __name__ == '__main__':
    raise SystemExit(main())
