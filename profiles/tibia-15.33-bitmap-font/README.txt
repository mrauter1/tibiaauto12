Tibia 15.33.c9e45e Linux bitmap font evidence

Verdana10px.fnt and Verdana10px.png are unchanged resource bytes extracted from
the supplied official Linux client distribution's bin/graphics_resources.rcc.
provenance.json records the source/client SHA-256 values, source resource paths,
RCC offsets and compression, extracted-file hashes, and extractor hash. The
associated Linux client binary was hashed during this extraction, never run.
No complete client binary or RCC archive is included here.

Reproduce from a separately obtained copy of that exact client distribution:

  python -m pip install zstandard==0.25.0
  python tools/extract_tibia_font.py /path/to/bin/graphics_resources.rcc --client-binary /path/to/bin/client --out /tmp/tibia-bitmap-font

The optional zstandard dependency is used only by this offline extractor and
is not part of production requirements. The extractor checks the pinned RCC
and asset hashes, traverses Qt RCC v1/v2/v3 without loading Qt, and validates the
descriptor's page, glyph set, and atlas extents before writing known filenames.
The client argument additionally verifies the matching client binary; omitting
it records binary_verified_during_extraction=false in the generated provenance.
Identical inputs and extractor bytes reproduce the two assets and provenance.

The descriptor retains its original page reference /fonts/Verdana10px.png.
Consumers should resolve that known page to the sibling packaged PNG; do not
rewrite the descriptor or treat the resource path as a host filesystem path.
The bitmap is 128 x 64, with 67 glyphs: space, comma, hyphen, period, slash,
digits 0-9, and ASCII A-Z/a-z. It has no colon, accented letters, or other glyphs.
Its descriptor names Verdana, size=12, bold=1, outline=1, lineHeight=12, base=10;
the filename's "10px" is not a replacement for the descriptor's actual metrics.

This profile supplies evidence and templates for an optional offline experiment.
Linux resource membership does not establish that Windows battle-list text uses
this font, size, rasterization, colors, or spacing. No production reader, profile
default, application startup behavior, or live-client automation is enabled.
