"""Safety and decoding checks; synthetic coverage is not real-crop accuracy."""
from pathlib import Path
import shutil

import numpy as np
import pytest

from tibia_client.glyph_text import DEFAULT_FONT, GlyphReader, foreground


@pytest.fixture(scope='module')
def reader():
    return GlyphReader()


@pytest.mark.parametrize('name', ['Qzvx 742', 'Moth Quill', 'Agyp QZ', 'Ili 901',
                                 'aaa', 'cwm', 'nn', 'xy'])
def test_unseen_character_sequences(reader, name):
    result = reader.read_line(reader.render(name))
    assert result.text == name
    assert result.score > .99
    assert len(result.boxes) == len(name.replace(' ', ''))


def test_bounds_and_coordinates_include_crop_offset(reader):
    rgb = reader.render('Fury')
    padded = np.pad(rgb, ((7, 4), (11, 5), (0, 0)))
    original = reader.read_line(rgb)
    result = reader.read_line(padded)
    assert result.text == 'Fury'
    assert result.boxes == tuple((l+11, t+7, r+11, b+7) for l, t, r, b in original.boxes)
    for box in result.boxes:
        assert 0 <= box[0] < box[2] <= padded.shape[1]
        assert 0 <= box[1] < box[3] <= padded.shape[0]


def test_clipped_input_cannot_have_boxes_beyond_crop(reader):
    rgb = reader.render('Fury')
    last_ink = np.nonzero(foreground(rgb))[1].max()
    cropped = rgb[:, :last_ink]
    result = reader.read_line(cropped)
    assert all(box[2] <= cropped.shape[1] for box in result.boxes)
    # Cropping is not always visually distinguishable from a different glyph;
    # the contract is bounded boxes, not a promise to detect every truncation.


def test_two_text_rows_abstain(reader):
    rgb = reader.render('Fury')
    result = reader.read_line(np.concatenate([rgb, np.zeros_like(rgb), rgb]))
    assert result.text is None
    assert result.reason == 'no single text row'


def test_empty_chrome_and_colored_regions_abstain(reader):
    blank = np.zeros((20, 100, 3), np.uint8)
    assert reader.read_line(blank).text is None
    blank[12:14] = 192
    assert reader.read_line(blank).text is None
    blank[:] = [255, 0, 0]
    assert reader.read_line(blank).text is None


@pytest.mark.parametrize('scale', [None, True, float('nan'), float('inf'), .1, 4])
def test_invalid_scale_rejected(reader, scale):
    with pytest.raises(ValueError):
        reader.read_line(reader.render('Rat'), scale=scale)


@pytest.mark.parametrize('rgb', [np.zeros((0, 2, 3), np.uint8),
                               np.zeros((10, 10), np.uint8),
                               np.zeros((10, 10, 4), np.uint8),
                               np.zeros((10, 10, 3), np.float32),
                               np.zeros((1000, 1000, 3), np.uint8)])
def test_invalid_crops_rejected(reader, rgb):
    with pytest.raises(ValueError):
        reader.read_line(rgb)


def test_font_map_rejects_invalid_atlas_extent(tmp_path):
    for name in ('Verdana10px.fnt', 'Verdana10px.png'):
        shutil.copyfile(DEFAULT_FONT / name, tmp_path / name)
    path = tmp_path / 'Verdana10px.fnt'
    data = path.read_text()
    path.write_text(data.replace('scaleW=128', 'scaleW=127'))
    with pytest.raises(ValueError, match='dimensions'):
        GlyphReader(tmp_path)


def test_unsupported_render_characters_fail_explicitly(reader):
    with pytest.raises(ValueError, match='absent'):
        reader.render('René')


def test_production_has_no_import_of_experimental_reader():
    # This is deliberately architectural: the experiment must not activate
    # through an existing entry point as a side effect of importing a module.
    root = Path(__file__).resolve().parents[1] / 'tibia_client'
    for path in root.glob('*.py'):
        if path.name != 'glyph_text.py':
            assert 'import glyph_text' not in path.read_text()
            assert 'from .glyph_text' not in path.read_text()
            assert 'from tibia_client.glyph_text' not in path.read_text()
