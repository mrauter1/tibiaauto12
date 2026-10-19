"""Safety and independent-row contracts for the opt-in structural reader."""
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
import pytest

from core.perception import Frame
from tibia_client.adaptive import FIELDS
from tibia_client.models import Quality
from tools.resource_reader_experiments import ExperimentalResourceReader, VARIANTS
from tools.resource_row_geometry import StructuralRowMixin


class RowReader(StructuralRowMixin, ExperimentalResourceReader):
    pass


@pytest.fixture
def reader():
    cv2.setNumThreads(1)
    return RowReader(VARIANTS['accelerated_fast'])


def _single_row(*, chrome=True, foreign=False):
    with Image.open(Path(__file__).parent / 'fixtures/resources/paulistinha-skills.png') as source:
        original = np.asarray(source.convert('RGB'))
    canvas = np.full((95, 240, 3), 42, dtype=np.uint8)
    # A lossless copy of an approved real label/value pair.  These are guard
    # tests only; generated canvases are never included in the real benchmark.
    canvas[10:34, 12:12 + original.shape[1]] = original[:24]
    if chrome:
        canvas[:80, 5] = 180
        canvas[:80, 210] = 180
    if foreign:
        # A second value placed between the caption and right-aligned value
        # must remain visible to the numeric decoder, never be cropped away.
        canvas[10:34, 111:142] = original[:24, 160:191]
    return Frame(canvas)


def test_independent_visible_row_can_be_read_without_hidden_siblings(reader):
    frame = _single_row()
    discovery = reader.discover(frame)
    assert discovery.layout is not None, discovery.detail
    assert len(discovery.layout.anchors) == 1
    readings = reader.read(frame, discovery.layout)
    assert readings[FIELDS[0]].quality is Quality.KNOWN
    assert readings[FIELDS[0]].value['count'] == 710
    assert all(readings[key].quality is Quality.UNKNOWN for key in FIELDS[1:])


def test_semantic_single_row_without_chrome_does_not_authorize_panel(reader):
    assert reader.discover(_single_row(chrome=False)).layout is None


def test_one_missing_value_does_not_hide_another_visible_row(reader):
    pixels = _single_row().pixels.copy()
    with Image.open(Path(__file__).parent / 'fixtures/resources/paulistinha-skills.png') as source:
        original = np.asarray(source.convert('RGB'))
    pixels[34:52, 12:111] = original[24:42, :99]
    frame = Frame(pixels)
    discovery = reader.discover(frame)
    assert discovery.layout is not None, discovery.detail
    assert {anchor.field for anchor in discovery.layout.anchors} == set(FIELDS[:2])
    readings = reader.read(frame, discovery.layout)
    assert readings[FIELDS[0]].value['count'] == 710
    assert readings[FIELDS[1]].quality is Quality.UNKNOWN


def test_single_row_does_not_discard_foreign_number_prefix(reader):
    frame = _single_row(foreign=True)
    discovery = reader.discover(frame)
    if discovery.layout is not None:
        reading = reader.read(frame, discovery.layout)[FIELDS[0]]
        assert reading.quality is Quality.UNKNOWN


def test_current_identity_and_visibility_are_checked_without_discovery(reader):
    frame = _single_row()
    discovery = reader.discover(frame)
    assert discovery.layout is not None, discovery.detail
    anchor = discovery.layout.anchors[0]
    pixels = frame.pixels.copy()
    x0, y0, x1, y1 = anchor.box
    pixels[y0:y1, x0:x1] = 42
    readings = reader.read(Frame(pixels), discovery.layout)
    assert all(item.quality is Quality.UNKNOWN for item in readings.values())


def test_duplicate_independent_rows_are_ambiguous(reader):
    single = _single_row().pixels
    pixels = np.concatenate((single, np.full((single.shape[0], 80, 3), 42, np.uint8), single), axis=1)
    discovery = reader.discover(Frame(pixels))
    assert discovery.layout is None
    assert len(discovery.candidates) == 2
    assert 'ambiguous' in discovery.detail


def test_nearby_stacked_independent_rows_cannot_hide_each_other(reader):
    single = _single_row().pixels
    pixels = np.concatenate((single, single), axis=0)
    discovery = reader.discover(Frame(pixels))
    assert discovery.layout is None
    assert len(discovery.candidates) == 2
    assert 'ambiguous' in discovery.detail


def test_complete_baseline_panel_keeps_all_values(reader):
    with Image.open(Path(__file__).parent / 'fixtures/resources/paulistinha-skills.png') as image:
        frame = Frame(image)
    discovery = reader.discover(frame)
    assert discovery.layout is not None, discovery.detail
    readings = reader.read(frame, discovery.layout)
    assert [readings[key].value['count'] for key in FIELDS] == [710, 1085, 4, 192]


def test_cached_row_does_not_repeat_a_value_when_value_disappears(reader):
    frame = _single_row()
    discovery = reader.discover(frame)
    assert discovery.layout is not None, discovery.detail
    x0, y0, x1, y1 = discovery.layout.regions[FIELDS[0]]
    pixels = frame.pixels.copy()
    pixels[y0:y1, x0:x1] = 42
    readings = reader.read(Frame(pixels), discovery.layout)
    assert readings[FIELDS[0]].quality is Quality.UNKNOWN
    assert readings[FIELDS[0]].value is None


def test_single_row_geometry_does_not_depend_on_digit_side_bearing(reader):
    frame = _single_row()
    discovery = reader.discover(frame)
    assert discovery.layout is not None, discovery.detail
    x0, y0, x1, y1 = discovery.layout.regions[FIELDS[0]]
    pixels = frame.pixels.copy()
    pixels[y0:y1, x0:x1 - 1] = frame.pixels[y0:y1, x0 + 1:x1]
    pixels[y0:y1, x1 - 1] = 42
    reading = reader.read(Frame(pixels), discovery.layout)[FIELDS[0]]
    assert reading.quality is Quality.KNOWN
    assert reading.value['count'] == 710
