# Experimental battle-list glyph reader

This opt-in experiment does not change any production imports, reader defaults,
configuration, or game input. Instantiate `tibia_client.glyph_text.GlyphReader`
explicitly or select it in the offline benchmark. It reads characters, without
a creature-name dictionary, and returns text plus character boxes relative to
the supplied RGB name-row crop. `text=None` means abstention; `candidate` is only
a diagnostic hypothesis. Pixel scores and runner-up margins are not calibrated
probabilities.

```python
from PIL import Image
import numpy as np
from tibia_client.glyph_text import GlyphReader

reader = GlyphReader()
result = reader.read_line(np.asarray(Image.open('name-row.png').convert('RGB')),
                          scale=1.0)
print(result.text, result.boxes, result.reason)
```

The font profile contains the original `.fnt` and PNG bytes extracted from the
official Linux 15.33.c9e45e distribution's `graphics_resources.rcc`, alongside
binary/resource/asset hashes and extraction provenance. See
[`profiles/tibia-15.33-bitmap-font/README.txt`](../profiles/tibia-15.33-bitmap-font/README.txt)
for the reproducible extraction command. Finding a font in the client resources
does not establish that the battle list uses it. This particular atlas contains
67 glyphs; accents and other Unicode characters are unsupported.

The reader thresholds neutral light foreground, rejects horizontal chrome,
locates a single text band, and searches character sequences against scaled
atlas glyphs. It preserves BMFont bearings, advances and baseline offsets.
Reference preparation is cached; recognized strings are not. Work is bounded
by crop dimensions, scale range and beam width. Multiple plausible text bands
are rejected. Clipped text, compression, noninteger scaling, different font
hinting and unsupported characters can cause rejection or incorrect output.
Visually ambiguous clipping cannot always be detected. Boxes describe matched
foreground glyphs, not spaces. Callers must add the crop origin for screen
coordinates. No screen-zone detector is added here.

Run the real screenshot comparison on Windows with the project's pinned
dependencies:

```powershell
python -m pip install -r requirements-dev.txt
python tools/benchmark_battle_glyphs.py --readers both --repeats 10 --json-out reports/battle-glyph-benchmark.json --text-out reports/battle-glyph-benchmark.txt
```

Linux can run `--readers glyph`; it cannot measure Windows OCR. The draft-PR
workflow `Experimental battle glyph benchmark` runs both on one Windows runner
and uploads raw measurements, OCR availability diagnostics and test results.
It only runs for `codex/glyph-reader*` PR branches or manual dispatch.

The frozen corpus supplies 24 name rows across nine screenshots and seven
empty battle panels. Two clipped/previously excluded rows are listed explicitly
in each report. Images, labels and exclusions are not altered. Both readers get
the same pixels and UI scale; truth is used only for scoring. Windows OCR uses
the production foreground mask, 3x nearest-neighbor enlargement, native
`Windows.Media.Ocr`, and production row joining. This compares recognition
primitives, not the asynchronous production reader's cache/retry pipeline.

Reports separate first calls from repeated **uncached** calls, and include
preprocessing. They show exact full-name coverage, precision among accepted
readings, abstentions, errors, character error rate and empty-panel false
positives. Precision is undefined when no readings are accepted. A second
normalized score ignores case/whitespace like the existing name matcher. These
small, single-annotator, heterogeneous screenshots do not establish performance
on every font, UI scale or possible name. Synthetic atlas renders are unit
checks only and are never substituted for the real-image accuracy benchmark.

The initial Linux experiment rejected all 24 real names, despite decoding
synthetic unseen strings from this atlas. Visual inspection shows differences
in battle-list letter shapes and spacing. Keep the reader experimental until a
matching battle-list font/rendering profile is established and evaluated on
additional held-out screenshots. The Windows report is the source for the
same-machine latency and accuracy comparison.

For full-screen latency, run `python tools/benchmark_fullscreen_ocr.py --repeats
20 --output-prefix reports/battle-glyph-fullscreen` on Windows. This compares
native color and the production neutral mask, each at 1x and 3x, using the entire
1536x864 `reddit-valouran-pl7tust` screenshot. A derived 1024x768 workload fits
the whole screenshot to 1024x576 with black padding; it is not a native client
capture at that resolution. A 133x20 name crop supplies a same-run control.
The script verifies source hashes and checks the masked 3x output against the
unchanged production OCR function after timing. Twenty fresh recognition calls
per case follow a separately reported first call, with rotated invocation order.
Durations include preprocessing, COM and engine setup, bitmap transfer, awaited
recognition, and text/box extraction. Capture, disk decoding, input-canvas fitting
and JSON serialization are excluded. OS/model caches may remain warm even in a
fresh Python process. Raw first-call words/boxes and all per-call timings are
preserved. Full-screen ground truth is unavailable: word counts are diagnostic,
not accuracy scores. The neutral mask intentionally removes colored pixels and
should not be assumed to recover every text color in a complete game window.
