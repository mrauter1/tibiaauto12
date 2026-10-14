"""Corpus and adversarial tests for the adaptive, screen-only reader."""

from __future__ import annotations

from hashlib import sha256
import math
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
import pytest

from core.perception import Frame
import tibia_client.adaptive as adaptive_module
from tibia_client.adaptive import Anchor, FIELDS, ResourceReader
from tibia_client.adaptive_text import NumberReader, OCRBox
from tibia_client.models import Quality
from tools.benchmark_resources import benchmark_entry, generated_benchmark_cases, load_manifest


ROOT = Path(__file__).parents[1]
MANIFEST = ROOT / "tests/fixtures/screenshots/manifest.json"
REFERENCE = ROOT / "tests/fixtures/resources/paulistinha-skills.png"
EXPECTED = {
    "resource_health": 710,
    "resource_mana": 1085,
    "resource_soul": 4,
    "resource_capacity": 192,
}


def _reference(offset: tuple[int, int] = (0, 0), extra: tuple[int, int] = (0, 0)) -> Frame:
    with Image.open(REFERENCE) as source:
        pixels = np.asarray(source.convert("RGB"))
    dx, dy = offset
    canvas = np.full(
        (pixels.shape[0] + dy + extra[1], pixels.shape[1] + dx + extra[0], 3), 42, dtype=np.uint8
    )
    canvas[dy : dy + pixels.shape[0], dx : dx + pixels.shape[1]] = pixels
    # Moving/reflow tests must copy the known frame losslessly.
    assert np.array_equal(canvas[dy : dy + pixels.shape[0], dx : dx + pixels.shape[1]], pixels)
    return Frame(canvas)


@pytest.fixture(scope="module")
def reader() -> ResourceReader:
    return ResourceReader()


def _assert_exact(readings, expected=EXPECTED):
    assert set(readings) == set(FIELDS)
    for field, value in expected.items():
        reading = readings[field]
        assert reading.quality is Quality.KNOWN, (field, reading.detail)
        assert reading.value["count"] == value


def _discover_and_read(reader: ResourceReader, frame: Frame):
    discovered = reader.discover(frame, use_ocr=False)
    assert discovered.layout is not None, discovered.detail
    return discovered, reader.read(frame, discovered.layout, use_ocr=False)


def _manifest_cases():
    _, entries = load_manifest(MANIFEST)
    return [(entry["id"], entry) for entry in entries]


CORPUS_CASES = _manifest_cases()


def test_corpus_manifest_is_complete_and_has_real_heldout_evidence():
    document, entries = load_manifest(MANIFEST)
    assert document["box_format"] == "xyxy-half-open"
    assert any(item["data_origin"] == "real" for item in entries)
    assert any(item["data_origin"] == "real" and item["split"] == "heldout" for item in entries)
    assert any(item["coverage"] == "full_screenshot" for item in entries)
    assert any(item["coverage"] == "supplemental_crop" for item in entries)
    assert all((MANIFEST.parent / item["file"]).is_file() for item in entries)
    referenced = {(MANIFEST.parent / item["file"]).resolve() for item in entries}
    originals = MANIFEST.parent / "originals"
    present = {path.resolve() for path in originals.iterdir() if path.is_file()}
    assert referenced == present


@pytest.mark.parametrize(
    "case_id,entry", CORPUS_CASES, ids=[case_id for case_id, _ in CORPUS_CASES]
)
def test_every_corpus_screenshot_has_safe_reader_behavior(reader, case_id, entry):
    # warm_reads=1 keeps the ordinary suite bounded. The standalone benchmark
    # performs repeated warm reads and reports latency for every corpus entry.
    row = benchmark_entry(reader, entry, MANIFEST, use_ocr=False, warm_reads=1)
    assert row.id == case_id
    assert row.error is None
    # Performance budgets belong to the separately measured latency benchmark;
    # heterogeneous CI machines must still run every correctness case.
    assert math.isfinite(row.discovery_ms) and row.discovery_ms >= 0
    visibly_different = (
        entry.get("render_equivalence", {}).get("status") == "visibly_different"
    )
    if entry["expected_discovery"] and not visibly_different:
        assert row.layout_found, row.detail
        assert row.fields_discovered == row.fields_expected == len(FIELDS)
        assert row.locations_correspond == row.fields_expected
        assert row.numeric_exact == row.fields_expected
        assert row.unknown == 0
    elif entry["classification"] == "negative":
        assert not row.layout_found, "negative screenshot produced a false resource panel"
    elif entry.get("expected_final_unknown_fields"):
        assert not row.layout_found, "ambiguous screenshot produced a panel acceptance"
        assert row.intentional_unknown_targets == len(entry["expected_final_unknown_fields"])
        assert row.safe_intentional_unknown == row.intentional_unknown_targets
        assert row.unsafe_intentional_unknown_acceptances == 0
    # No screenshot class may emit a plausible but contradicted value.
    assert row.incorrect_known == 0


def test_calmera_resampled_primary_font_is_discovered_and_read_exactly(reader):
    _, entries = load_manifest(MANIFEST)
    entry = next(item for item in entries if item["id"] == "dfg-calmera-ek246-D9BJ")
    assert sha256((MANIFEST.parent / entry["file"]).read_bytes()).hexdigest() == (
        "aae80a066c5a28958a593d7ad3c56df59334936026825d7b2226b7c6b1e6b660"
    )

    row = benchmark_entry(reader, entry, MANIFEST, use_ocr=False, warm_reads=1)

    assert row.layout_found, row.detail
    assert row.locations_correspond == row.fields_expected == len(FIELDS)
    assert row.numeric_exact == row.fields_expected
    assert row.unknown == 0
    assert row.incorrect_known == 0


def test_panel_translation_preserves_locations_and_values(reader):
    dx, dy = 173, 211
    base, _ = _discover_and_read(reader, _reference())
    moved, readings = _discover_and_read(reader, _reference((dx, dy), (19, 23)))
    _assert_exact(readings)
    for field in FIELDS:
        assert moved.layout.regions[field] == tuple(
            coordinate + (dx if index % 2 == 0 else dy)
            for index, coordinate in enumerate(base.layout.regions[field])
        )


@pytest.mark.parametrize("factor", [0.75, 1.5])
def test_scaled_panel_is_discovered_and_read_exactly(reader, factor):
    pixels = _reference().pixels
    scaled = cv2.resize(pixels, None, fx=factor, fy=factor, interpolation=cv2.INTER_LINEAR)
    discovered, readings = _discover_and_read(reader, Frame(scaled))
    assert discovered.layout.scale == pytest.approx(factor, abs=0.16)
    _assert_exact(readings)


def test_small_scale_topology_loss_cannot_emit_a_different_known_value(reader):
    pixels = _reference().pixels
    scaled = cv2.resize(pixels, None, fx=0.65, fy=0.65, interpolation=cv2.INTER_LINEAR)

    discovered = reader.discover(Frame(scaled), use_ocr=False)

    assert discovered.layout is not None, discovered.detail
    readings = reader.read(Frame(scaled), discovered.layout, use_ocr=False)
    capacity = readings["resource_capacity"]
    assert capacity.quality is Quality.UNKNOWN
    assert capacity.value is None
    assert "unstable" in capacity.detail or "ambiguous" in capacity.detail
    assert all(
        reading.quality is not Quality.KNOWN
        or reading.value["count"] == EXPECTED[field]
        for field, reading in readings.items()
    )


def test_continuous_panel_divider_is_not_a_second_numeric_column(reader):
    _, entries = load_manifest(MANIFEST)
    case = next(
        item
        for item in generated_benchmark_cases(entries, MANIFEST)
        if item.id == "dfg-retro-pvp-rp535::panel-scale-2"
    )
    assert case.case_sha256 == "69a1c9362a7850cbd74acfc8f28defadf5fd5c575cb50f3ca589178c9a9c4c17"

    discovery = reader.discover(case.frame, use_ocr=False)

    assert discovery.layout is not None, discovery.detail
    readings = reader.read(case.frame, discovery.layout, use_ocr=False)
    _assert_exact(
        readings,
        {
            "resource_health": 5455,
            "resource_mana": 7995,
            "resource_soul": 100,
            "resource_capacity": 4250,
        },
    )


def test_alternate_font_family_survives_panel_resampling(reader):
    _, entries = load_manifest(MANIFEST)
    case = next(
        item
        for item in generated_benchmark_cases(entries, MANIFEST)
        if item.id == "dfg-yonabra-ek195::panel-scale-2"
    )
    assert case.case_sha256 == "e6c9d3d74b44d596af4db315594da7a58538e58edafcc35356f6d13746c9ebb3"

    discovery = reader.discover(case.frame, use_ocr=False)

    assert discovery.layout is not None, discovery.detail
    assert all(anchor.method == "alternate_template" for anchor in discovery.layout.anchors)
    readings = reader.read(case.frame, discovery.layout, use_ocr=False)
    _assert_exact(
        readings,
        {
            "resource_health": 2969,
            "resource_mana": 1025,
            "resource_soul": 200,
            "resource_capacity": 1676,
        },
    )
    assert all(reading.diagnostics["font_family"] == "alternate" for reading in readings.values())


def test_legacy_injected_number_reader_does_not_receive_family_keyword():
    class LegacyNumberReader:
        def __init__(self):
            self.delegate = NumberReader()
            self.calls = 0

        def read(self, image, *, scale, max_digits, allow_grouping, use_ocr):
            self.calls += 1
            return self.delegate.read(
                image,
                scale=scale,
                max_digits=max_digits,
                allow_grouping=allow_grouping,
                use_ocr=use_ocr,
            )

    _, entries = load_manifest(MANIFEST)
    case = next(
        item
        for item in generated_benchmark_cases(entries, MANIFEST)
        if item.id == "dfg-yonabra-ek195::panel-scale-2"
    )
    number_reader = LegacyNumberReader()
    compatible = ResourceReader(number_reader=number_reader)
    discovery = compatible.discover(case.frame, use_ocr=False)
    assert discovery.layout is not None, discovery.detail

    readings = compatible.read(case.frame, discovery.layout, use_ocr=False)

    assert set(readings) == set(FIELDS)
    assert number_reader.calls == len(FIELDS)


def test_legacy_number_reader_subclass_must_explicitly_opt_in_to_family_keyword():
    class LegacySubclass(NumberReader):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def read(self, image, *, scale, max_digits, allow_grouping, use_ocr):
            self.calls += 1
            return super().read(
                image,
                scale=scale,
                max_digits=max_digits,
                allow_grouping=allow_grouping,
                use_ocr=use_ocr,
            )

    _, entries = load_manifest(MANIFEST)
    case = next(
        item
        for item in generated_benchmark_cases(entries, MANIFEST)
        if item.id == "dfg-yonabra-ek195::panel-scale-2"
    )
    number_reader = LegacySubclass()
    compatible = ResourceReader(number_reader=number_reader)
    discovery = compatible.discover(case.frame, use_ocr=False)
    assert discovery.layout is not None, discovery.detail

    readings = compatible.read(case.frame, discovery.layout, use_ocr=False)

    assert set(readings) == set(FIELDS)
    assert number_reader.calls == len(FIELDS)


@pytest.mark.parametrize(
    "case_id,case_sha256,region,scale,incorrect_value",
    [
        (
            "portal-tibia-2016-02-VtukRkf::panel-scale-0.9",
            "fb57041c9a99370c30c34ad26f8119854d7c75c9d0489ee2956717433ca79f4f",
            (49, 94, 125, 107),
            0.7136363636363636,
            304,
        ),
        (
            "dfg-pvp-ek133::panel-scale-1.1",
            "8125c1443615bae8fe989192ae9db834641af8d7eefbd35b2cd13a459a84e79f",
            (82, 103, 172, 121),
            0.9128787878787878,
            7060,
        ),
        (
            "dfg-pvp-ek133::panel-scale-1.3",
            "2346826e1a203066eb75b5f5a280834e463b2a89601d4a9fbe0ad0993a331879",
            (97, 122, 203, 142),
            1.0916666666666668,
            7060,
        ),
    ],
)
def test_resampled_narrow_digit_leads_remain_ambiguous(
    reader, case_id, case_sha256, region, scale, incorrect_value
):
    _, entries = load_manifest(MANIFEST)
    case = next(
        item
        for item in generated_benchmark_cases(entries, MANIFEST)
        if item.id == case_id
    )
    assert case.case_sha256 == case_sha256
    left, top, right, bottom = region

    reading = reader.number_reader.read(
        case.frame.pixels[top:bottom, left:right],
        scale=scale,
        max_digits=7,
        use_ocr=False,
    )

    assert reading.quality is Quality.UNKNOWN
    assert reading.value is None
    assert reading.detail == "number contains an ambiguous digit"
    assert int(str(reading.diagnostics["text"]).replace(",", "")) == incorrect_value


def test_anisotropic_label_resampling_is_recovered_locally(reader):
    frame = _reference()
    original, _ = _discover_and_read(reader, frame)
    pixels = frame.pixels.copy()
    anchor = next(item for item in original.layout.anchors
                  if item.field == "resource_soul")
    left, top, right, bottom = anchor.box
    label = pixels[top:bottom, left:right]
    new_height = round(label.shape[0] * 0.75)
    reshaped = cv2.resize(label, (label.shape[1], new_height), interpolation=cv2.INTER_LINEAR)
    pixels[top:bottom, left:right] = 72
    paste_top = top + (bottom - top - new_height) // 2
    pixels[paste_top:paste_top + new_height, left:right] = reshaped
    altered = Frame(pixels)
    primary = reader._anchors(altered, (0, 0, altered.width, altered.height), lambda: None)
    assert not any(item.field == "resource_soul" for item in primary)
    discovery, readings = _discover_and_read(reader, altered)
    assert any(item.field == "resource_soul" for item in discovery.layout.anchors)
    _assert_exact(readings)


def test_ocr_can_complete_one_missing_anchor_without_replacing_template_anchors():
    frame = _reference()
    original, _ = _discover_and_read(ResourceReader(), frame)
    soul = next(item for item in original.layout.anchors
                if item.field == "resource_soul")
    pixels = frame.pixels.copy()
    left, top, right, bottom = soul.box
    pixels[top:bottom, left:right] = pixels[top:bottom, left:right, :][:, ::-1]

    class SoulOCR:
        available = True

        def read_text_boxes(self, image, *, psm=7, timeout=3.0):
            return (OCRBox("Soul Points", soul.box, 0.99),)

    mixed_reader = ResourceReader(ocr=SoulOCR())
    altered = Frame(pixels)
    without_ocr = mixed_reader.discover(altered, use_ocr=False)
    assert without_ocr.layout is not None
    without_readings = mixed_reader.read(altered, without_ocr.layout, use_ocr=False)
    assert without_readings["resource_soul"].quality is Quality.UNKNOWN
    assert all(anchor.method == "template" for anchor in without_ocr.layout.anchors)

    with_ocr = mixed_reader.discover(altered, use_ocr=True)
    assert with_ocr.layout is not None
    methods = {anchor.field: anchor.method for anchor in with_ocr.layout.anchors}
    assert methods["resource_soul"] == "ocr"
    assert all(methods[field] == "template" for field in set(FIELDS) - {"resource_soul"})
    _assert_exact(mixed_reader.read(altered, with_ocr.layout, use_ocr=True))


def test_ocr_score_cannot_displace_a_template_anchor_in_a_constellation(reader):
    anchors = [
        Anchor("resource_health", (10, 10, 45, 20), 0.81, 1.0),
        Anchor("resource_health", (10, 10, 45, 20), 0.99, 1.0, "ocr"),
        Anchor("resource_mana", (10, 28, 38, 38), 0.86, 1.0),
        Anchor("resource_soul", (10, 46, 42, 56), 0.84, 1.0),
    ]

    groups = reader._constellations(anchors)

    assert groups
    assert all(
        next(item for item in group if item.field == "resource_health").method
        == "template"
        for group in groups
    )


def test_wider_reflow_uses_the_new_value_column(reader):
    pixels = _reference().pixels
    split, shift = 99, 74
    widened = np.full((pixels.shape[0], pixels.shape[1] + shift, 3), 42, dtype=np.uint8)
    widened[:, :split] = pixels[:, :split]
    widened[:, split + shift :] = pixels[:, split:]
    assert np.array_equal(widened[:, :split], pixels[:, :split])
    assert np.array_equal(widened[:, split + shift :], pixels[:, split:])
    discovered, readings = _discover_and_read(reader, Frame(widened))
    _assert_exact(readings)
    assert min(region[0] for region in discovered.layout.regions.values()) < split + shift
    assert min(region[2] for region in discovered.layout.regions.values()) > split + shift


def test_duplicate_panels_are_rejected_as_ambiguous(reader):
    pixels = _reference().pixels
    duplicated = np.concatenate(
        (pixels, np.full((pixels.shape[0], 32, 3), 42, np.uint8), pixels), axis=1
    )
    discovery = reader.discover(Frame(duplicated), use_ocr=False)
    assert discovery.layout is None
    assert len(discovery.candidates) == 2
    assert "ambiguous" in discovery.detail


def test_far_duplicate_panel_cannot_be_hidden_by_component_proposals(reader, monkeypatch):
    pixels = _reference().pixels
    separation = 320
    duplicated = np.concatenate(
        (
            pixels,
            np.full((pixels.shape[0], separation, 3), 42, np.uint8),
            pixels,
        ),
        axis=1,
    )
    original = adaptive_module._component_matches

    def omit_right_panel(mask, template, components, threshold):
        matches = original(mask, template, components, threshold)
        if mask.shape[1] == duplicated.shape[1]:
            return [item for item in matches if item[1] < pixels.shape[1]]
        return matches

    monkeypatch.setattr(adaptive_module, "_component_matches", omit_right_panel)

    discovery = reader.discover(Frame(duplicated), use_ocr=False)

    assert discovery.layout is None
    assert len(discovery.candidates) == 2
    assert "ambiguous" in discovery.detail


def test_nearby_larger_panel_cannot_be_hidden_by_seed_deduplication(reader):
    pixels = _reference().pixels
    scaled = cv2.resize(pixels, None, fx=2.5, fy=2.5, interpolation=cv2.INTER_LINEAR)
    canvas = np.full((520, 650, 3), 42, np.uint8)
    canvas[20 : 20 + pixels.shape[0], 20 : 20 + pixels.shape[1]] = pixels
    canvas[220 : 220 + scaled.shape[0], 45 : 45 + scaled.shape[1]] = scaled

    discovery = reader.discover(Frame(canvas), use_ocr=False)

    assert discovery.layout is None
    assert len(discovery.candidates) == 2
    assert "ambiguous" in discovery.detail


def test_adjacent_complete_numeric_column_is_rejected(reader):
    pixels = _reference().pixels
    value_column = pixels[:, 99:]
    decoy_gap = 18
    altered = np.full(
        (pixels.shape[0], pixels.shape[1] + value_column.shape[1] + decoy_gap, 3),
        42,
        dtype=np.uint8,
    )
    altered[:, : pixels.shape[1]] = pixels
    altered[:, pixels.shape[1] + decoy_gap :] = value_column
    discovery = reader.discover(Frame(altered), use_ocr=False)
    assert discovery.layout is None, "reader chose one of two plausible numeric columns"


def test_isolated_decoy_digit_before_value_column_cannot_prefix_mana(reader):
    pixels = _reference().pixels.copy()
    with Image.open(ROOT / "profiles/paulistinha-skills-v1/digits/1.png") as image:
        glyph = np.asarray(image.convert("RGB"))
    height, width = glyph.shape[:2]
    pixels[25 : 25 + height, 70 : 70 + width] = glyph
    frame = Frame(pixels)
    discovery = reader.discover(frame, use_ocr=False)
    if discovery.layout is None:
        assert "verified resource panel" in discovery.detail or "ambiguous" in discovery.detail
    else:
        reading = reader.read(frame, discovery.layout, use_ocr=False)["resource_mana"]
        assert reading.quality is Quality.UNKNOWN
        assert reading.value is None


def test_dissenting_value_column_cannot_be_partially_read(reader):
    base = _reference().pixels
    pixels = np.full((base.shape[0], 260, 3), 42, dtype=np.uint8)
    pixels[:, : base.shape[1]] = base
    discovery = reader.discover(Frame(pixels), use_ocr=False)
    assert discovery.layout is not None
    anchor = next(item for item in discovery.layout.anchors if item.field == "resource_mana")
    top = max(0, anchor.box[1] - 3)
    bottom = min(pixels.shape[0], anchor.box[3] + 3)
    pixels[top:bottom, anchor.box[2] + 1 :] = 72
    with Image.open(ROOT / "profiles/paulistinha-skills-v1/digits/1.png") as image:
        glyph = np.asarray(image.convert("RGB"))
    x = 158
    for _ in range(7):
        height, width = glyph.shape[:2]
        pixels[top + 2 : top + 2 + height, x : x + width] = glyph
        x += width + 2
    altered = Frame(pixels)
    result = reader.discover(altered, use_ocr=False)
    if result.layout is None:
        assert "verified resource panel" in result.detail or "ambiguous" in result.detail
    else:
        mana = reader.read(altered, result.layout, use_ocr=False)["resource_mana"]
        assert mana.quality is Quality.UNKNOWN
        assert mana.value is None


def test_stale_layout_rejects_an_occluded_label(reader):
    frame = _reference()
    discovery, _ = _discover_and_read(reader, frame)
    target = next(anchor for anchor in discovery.layout.anchors if anchor.field == "resource_soul")
    pixels = frame.pixels.copy()
    left, top, right, bottom = target.box
    pixels[top:bottom, left:right] = 42
    readings = reader.read(Frame(pixels), discovery.layout, use_ocr=False)
    assert all(reading.quality is Quality.UNKNOWN for reading in readings.values())
    assert all(reading.value is None for reading in readings.values())


def test_occluded_digits_do_not_become_a_plausible_value(reader):
    frame = _reference()
    discovery, baseline = _discover_and_read(reader, frame)
    pixels = frame.pixels.copy()
    left, top, right, bottom = discovery.layout.regions["resource_soul"]
    pixels[top:bottom, left:right] = 72
    readings = reader.read(Frame(pixels), discovery.layout, use_ocr=False)
    assert readings["resource_soul"].quality is Quality.UNKNOWN
    assert readings["resource_soul"].value is None
    for field in set(FIELDS) - {"resource_soul"}:
        assert readings[field] == baseline[field]


def test_wrong_size_capture_invalidates_every_field(reader):
    frame = _reference()
    discovery, _ = _discover_and_read(reader, frame)
    enlarged = np.pad(frame.pixels, ((0, 1), (0, 0), (0, 0)), constant_values=42)
    readings = reader.read(Frame(enlarged), discovery.layout, use_ocr=False)
    assert all(
        reading.quality is Quality.UNKNOWN and reading.value is None
        for reading in readings.values()
    )
    assert all("rediscovery required" in reading.detail for reading in readings.values())


def test_same_size_value_column_move_invalidates_cached_layout(reader):
    frame = _reference()
    discovery, _ = _discover_and_read(reader, frame)
    pixels = frame.pixels.copy()
    pixels[:, 99:] = 42
    pixels[:, 130:182] = frame.pixels[:, 140:192]
    moved = Frame(pixels)
    assert not reader.validate(moved, discovery.layout)
    readings = reader.read(moved, discovery.layout, use_ocr=False)
    assert all(
        reading.quality is Quality.UNKNOWN and reading.value is None
        for reading in readings.values()
    )


def test_clipped_value_column_is_not_discovered(reader):
    frame = _reference()
    discovery, _ = _discover_and_read(reader, frame)
    clip_at = max(region[2] for region in discovery.layout.regions.values()) - 4
    clipped = Frame(frame.pixels[:, :clip_at])
    result = reader.discover(clipped, use_ocr=False)
    assert result.layout is None


def test_bottom_edge_blank_row_never_emits_out_of_frame_bounds(reader):
    pixels = _reference().pixels.copy()
    pixels[58:75, 99:] = 72
    frame = Frame(pixels[:75])
    result = reader.discover(frame, use_ocr=False)
    assert result.layout is not None, result.detail
    left, top, right, bottom = result.layout.bounds
    assert 0 <= left < right <= frame.width
    assert 0 <= top < bottom <= frame.height
    for region in result.layout.regions.values():
        left, top, right, bottom = region
        assert 0 <= left < right <= frame.width
        assert 0 <= top < bottom <= frame.height
    readings = reader.read(frame, result.layout, use_ocr=False)
    for field in ("resource_health", "resource_mana", "resource_soul"):
        assert readings[field].quality is Quality.KNOWN
        assert readings[field].value["count"] == EXPECTED[field]
    assert readings["resource_capacity"].quality is Quality.UNKNOWN
    assert readings["resource_capacity"].value is None


def test_changed_value_is_read_from_current_pixels(reader):
    frame = _reference()
    discovery, _ = _discover_and_read(reader, frame)
    pixels = frame.pixels.copy()
    left, top, right, bottom = discovery.layout.regions["resource_soul"]
    pixels[top:bottom, left:right] = 72
    glyphs = []
    for digit in "25":
        with Image.open(ROOT / f"profiles/paulistinha-skills-v1/digits/{digit}.png") as image:
            glyphs.append(np.asarray(image.convert("RGB")))
    x = right - 4 - sum(glyph.shape[1] + 2 for glyph in glyphs)
    for glyph in glyphs:
        height, width = glyph.shape[:2]
        pixels[top + 2 : top + 2 + height, x : x + width] = glyph
        x += width + 2
    readings = reader.read(Frame(pixels), discovery.layout, use_ocr=False)
    assert readings["resource_soul"].quality is Quality.KNOWN
    assert readings["resource_soul"].value["count"] == 25
    for field in set(FIELDS) - {"resource_soul"}:
        assert readings[field].value["count"] == EXPECTED[field]


def test_unequal_label_widths_do_not_clip_leading_digits(reader):
    frame = _reference()
    original, _ = _discover_and_read(reader, frame)
    pixels = frame.pixels.copy()
    displayed = {
        "resource_health": "710",
        "resource_mana": "1111111",
        "resource_soul": "4",
        "resource_capacity": "192",
    }
    rendered_left = {}
    right_edge = 143
    for anchor in original.layout.anchors:
        top = max(0, anchor.box[1] - 3)
        bottom = min(pixels.shape[0], anchor.box[3] + 3)
        pixels[top:bottom, anchor.box[2] + 1 : 193] = 72
        glyphs = []
        for digit in displayed[anchor.field]:
            path = ROOT / f"profiles/paulistinha-skills-v1/digits/{digit}.png"
            with Image.open(path) as image:
                glyphs.append(np.asarray(image.convert("RGB")))
        total_width = sum(glyph.shape[1] for glyph in glyphs) + 2 * (len(glyphs) - 1)
        x = right_edge - total_width
        rendered_left[anchor.field] = x
        for glyph in glyphs:
            height, width = glyph.shape[:2]
            pixels[top + 2 : top + 2 + height, x : x + width] = glyph
            x += width + 2
    discovery, readings = _discover_and_read(reader, Frame(pixels))
    assert len(discovery.candidates) == 1
    assert discovery.layout.regions["resource_mana"][0] <= rendered_left["resource_mana"]
    assert readings["resource_mana"].quality is Quality.KNOWN
    assert readings["resource_mana"].value["count"] == 1_111_111
    for field in set(FIELDS) - {"resource_mana"}:
        assert readings[field].value["count"] == EXPECTED[field]
