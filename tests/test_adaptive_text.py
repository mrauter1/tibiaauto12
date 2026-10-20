from pathlib import Path
import json
import subprocess

import cv2
import numpy as np
from PIL import Image
import pytest

from tibia_client.adaptive_text import NumberReader, OCRBox, TesseractOCR
from tibia_client.models import Quality


ROOT = Path(__file__).parents[1]
PROFILE = ROOT / "profiles/paulistinha-skills-v1"
FIXTURE = Path(__file__).parent / "fixtures/resources/paulistinha-skills.png"


def fixture_number(top, bottom):
    with Image.open(FIXTURE) as source:
        return np.asarray(source.convert("RGB"))[top:bottom, 116:193].copy()


def rendered(text):
    paths = [PROFILE / ("comma.png" if token == "," else f"digits/{token}.png") for token in text]
    glyphs = [np.asarray(Image.open(path).convert("RGB")) for path in paths]
    height = max(glyph.shape[0] for glyph in glyphs) + 8
    width = sum(glyph.shape[1] for glyph in glyphs) + 2 * (len(glyphs) - 1) + 8
    canvas = np.full((height, width, 3), 72, dtype=np.uint8)
    x = 4
    for token, glyph in zip(text, glyphs):
        y = height - glyph.shape[0] - (2 if token == "," else 4)
        canvas[y : y + glyph.shape[0], x : x + glyph.shape[1]] = glyph
        x += glyph.shape[1] + 2
    return canvas


@pytest.mark.parametrize(
    ("name", "bounds", "expected", "maximum"),
    [
        ("health", (3, 24), 710, 6),
        ("mana", (21, 41), 1085, 6),
        ("soul", (39, 58), 4, 3),
        ("capacity", (56, 76), 192, 6),
    ],
)
def test_reads_all_resource_values_from_real_fixture(name, bounds, expected, maximum):
    reading = NumberReader().read(fixture_number(*bounds), max_digits=maximum)

    assert reading.quality is Quality.KNOWN, (name, reading.detail)
    assert reading.value == expected
    assert reading.diagnostics["reader"] == "templates"
    assert reading.diagnostics["text"].replace(",", "") == str(expected)
    assert 0.0 <= reading.diagnostics["score"] <= 1.0


def test_manually_boxed_existing_regression_values_are_exact_transfer_checks():
    corpus = Path(__file__).parent / "fixtures/screenshots"
    manifest = json.loads((corpus / "manifest.json").read_text(encoding="utf-8"))
    checked = 0
    reader = NumberReader()
    for entry in manifest["screenshots"]:
        # This historical crop-based unit check is not acceptance evidence for
        # newly collected full originals; their misses belong in the baseline.
        if entry.get("collection_batch"):
            continue
        with Image.open(corpus / entry["file"]) as source:
            pixels = np.asarray(source.convert("RGB"))
        visible_boxes = [
            item["box"]
            for item in entry.get("labels", {}).values()
            if item.get("visible") and item.get("box") is not None
        ]
        for key, label in entry.get("labels", {}).items():
            if not label.get("visible") or label.get("box") is None:
                continue
            left, top, right, bottom = label["box"]
            confirmed = entry["classification"] == "confirmed"
            # One panel has one UI scale.  Tight glyph boxes can differ by a
            # pixel because of digit ascenders/descenders and resampling phase;
            # using one row's height can therefore select the wrong scale bank.
            scale = (
                1.0
                if confirmed
                else float(
                    np.median(
                        [(other_bottom - other_top) / 10.0
                         for _, other_top, _, other_bottom in visible_boxes]
                    )
                )
            )
            row_gaps = [
                gap
                for _, other_top, _, other_bottom in visible_boxes
                for gap in (top - other_bottom, other_top - bottom)
                if gap > 0
            ]
            available_pad = min(row_gaps) - 1 if row_gaps else 4
            vertical_pad = min(4 if confirmed else 3, available_pad)
            # Boxes are tight annotations. Preserve a small left margin for
            # antialiasing while stopping before the panel's right border.
            crop = pixels[
                max(0, top - vertical_pad) : min(pixels.shape[0], bottom + vertical_pad),
                max(0, left - 8) : min(pixels.shape[1], right + 3),
            ]
            reading = reader.read(
                crop,
                scale=scale,
                max_digits=9,
                allow_grouping=key != "resource_soul",
            )
            diagnostic_only = bool(entry.get("expected_final_unknown_fields")) or (
                entry.get("render_equivalence", {}).get("status") == "visibly_different"
            )
            if diagnostic_only:
                assert reading.quality is Quality.UNKNOWN or reading.value == label["value"], (
                    entry["id"],
                    key,
                    reading.detail,
                )
            else:
                assert reading.value == label["value"], (
                    entry["id"],
                    key,
                    reading.detail,
                )
                checked += 1
    assert checked >= 36


def test_touching_digit_fallback_requires_clear_unoccluded_evidence():
    corpus = Path(__file__).parent / "fixtures/screenshots"
    manifest = json.loads((corpus / "manifest.json").read_text(encoding="utf-8"))
    entry = next(item for item in manifest["screenshots"] if item["id"] == "dfg-yonabra-ek195")
    label = entry["labels"]["resource_soul"]
    with Image.open(corpus / entry["file"]) as source:
        pixels = np.asarray(source.convert("RGB"))
    left, top, right, bottom = label["box"]
    crop = pixels[top - 3 : bottom + 3, left - 8 : right + 3].copy()
    reader = NumberReader()

    baseline = reader.read(crop, scale=0.7, max_digits=3, allow_grouping=False)
    assert baseline.quality is Quality.KNOWN
    assert baseline.value == label["value"]
    assert baseline.diagnostics["segmentation"] == "touching_digits"

    # Remove a small part of the leading digit. The resulting run still has a
    # superficially plausible cell count, but no longer clears the per-glyph
    # and whole-string margins required by the touching-digit fallback.
    crop[3:5, 8:10] = 72
    ambiguous = reader.read(crop, scale=0.7, max_digits=3, allow_grouping=False)
    assert ambiguous.quality is Quality.UNKNOWN
    assert ambiguous.value is None


@pytest.mark.parametrize("scale", [0.75, 1.25, 1.5, 2.0])
def test_resampled_pixel_font_uses_scale_and_retains_glyph_structure(scale):
    source = fixture_number(21, 41)
    scaled = cv2.resize(source, None, fx=scale, fy=scale, interpolation=cv2.INTER_LINEAR)

    reading = NumberReader().read(scaled, scale=scale, max_digits=6)

    assert reading.quality is Quality.KNOWN, reading.detail
    assert reading.value == 1085


def test_grouped_diagnostic_score_includes_separator_evidence():
    scale = 1.25
    source = fixture_number(21, 41)
    scaled = cv2.resize(source, None, fx=scale, fy=scale, interpolation=cv2.INTER_LINEAR)

    reading = NumberReader().read(scaled, scale=scale, max_digits=6)
    selected = next(
        item for item in reading.diagnostics["alternatives"] if item["text"] == "1,085"
    )

    assert reading.quality is Quality.KNOWN
    assert reading.diagnostics["score"] == pytest.approx(selected["score"])


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("01", "leading zero"),
        ("12,34", "malformed thousands grouping"),
        ("1,23,456", "malformed thousands grouping"),
        (",123", "malformed thousands grouping"),
        ("123,", "malformed thousands grouping"),
    ],
)
def test_rejects_malformed_integer_syntax(text, reason):
    reading = NumberReader().read(rendered(text), max_digits=8)

    assert reading.quality is Quality.UNKNOWN
    assert reading.value is None
    assert reason in reading.detail
    assert reading.diagnostics["text"] == text


def test_grouping_must_be_explicitly_allowed():
    reading = NumberReader().read(rendered("1,085"), max_digits=6, allow_grouping=False)

    assert reading.quality is Quality.UNKNOWN
    assert "grouping" in reading.detail or "punctuation" in reading.detail


def test_short_noise_is_not_accepted_as_a_thousands_separator():
    image = rendered("1,085")
    # The rendered comma asset occupies x=13:17. Replace it with a compact
    # bright block that is short enough to look like punctuation but has no
    # comma tail or aspect ratio.
    image[:, 13:18] = 72
    image[14:17, 13:18] = 192

    reading = NumberReader().read(image, max_digits=6)

    assert reading.quality is Quality.UNKNOWN
    assert reading.value is None
    assert "separator" in reading.detail

    fallback = NumberReader(ocr=FakeOCR("1,085")).read(image, max_digits=6, use_ocr=True)
    assert fallback.quality is Quality.UNKNOWN


def test_rejects_clipped_foreground_and_noise():
    source = fixture_number(3, 24)
    clipped = source[:, 45:]
    clipped_reading = NumberReader().read(clipped, max_digits=6)
    assert clipped_reading.quality is Quality.UNKNOWN
    assert "clipped" in clipped_reading.detail

    noise = np.full((20, 40, 3), 72, dtype=np.uint8)
    noise[7:11, 18:22] = 192
    noise_reading = NumberReader().read(noise)
    assert noise_reading.quality is Quality.UNKNOWN
    assert noise_reading.value is None

    other_font = np.full((30, 120, 3), 72, dtype=np.uint8)
    cv2.putText(
        other_font,
        "1085",
        (4, 20),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.5,
        (192, 192, 192),
        1,
        cv2.LINE_8,
    )
    font_reading = NumberReader().read(other_font, max_digits=6)
    assert font_reading.quality is Quality.UNKNOWN


def test_rejects_an_isolated_decoy_digit_before_the_aligned_number_run():
    with Image.open(FIXTURE) as source:
        pixels = np.asarray(source.convert("RGB")).copy()
    decoy = np.asarray(Image.open(PROFILE / "digits/1.png").convert("RGB"))
    pixels[24 : 24 + decoy.shape[0], 70 : 70 + decoy.shape[1]] = decoy

    reading = NumberReader().read(pixels[21:41, 60:192], max_digits=9)

    assert reading.quality is Quality.UNKNOWN
    assert reading.value is None
    assert "separated" in reading.detail


def test_enforces_digit_limit_without_returning_a_partial_value():
    reading = NumberReader().read(rendered("1234"), max_digits=3)

    assert reading.quality is Quality.UNKNOWN
    assert reading.value is None
    assert "unsupported number" in reading.detail or "more digits" in reading.detail


class FakeOCR:
    def __init__(self, text):
        self.text = text
        self.calls = 0

    def read_text_boxes(self, image, *, psm=7, timeout=3.0):
        self.calls += 1
        return (OCRBox(self.text, (1, 1, image.shape[1] - 1, image.shape[0] - 1), 0.98),)


def test_opted_in_ocr_can_resolve_a_template_tie_only_with_matching_evidence():
    ocr = FakeOCR("710")
    reader = NumberReader(ocr=ocr, ambiguity_margin=1.0)

    reading = reader.read(fixture_number(3, 24), max_digits=6, use_ocr=True)

    assert ocr.calls == 1
    assert reading.quality is Quality.KNOWN
    assert reading.value == 710
    assert reading.diagnostics["reader"] == "tesseract_with_template_evidence"
    assert reading.diagnostics["template_rejection"] == "number contains an ambiguous digit"


def test_ocr_and_template_candidate_disagreement_remains_unknown():
    ocr = FakeOCR("718")
    reader = NumberReader(ocr=ocr, ambiguity_margin=1.0)

    reading = reader.read(fixture_number(3, 24), max_digits=6, use_ocr=True)

    assert ocr.calls == 1
    assert reading.quality is Quality.UNKNOWN
    assert reading.value is None
    assert "ambiguous" in reading.detail
    assert "sufficient matching template evidence" in reading.diagnostics["ocr"]


def test_ocr_cannot_promote_an_unrelated_font_with_weak_template_matches():
    other_font = np.full((30, 120, 3), 72, dtype=np.uint8)
    cv2.putText(
        other_font,
        "1085",
        (4, 22),
        cv2.FONT_HERSHEY_TRIPLEX,
        0.6,
        (192, 192, 192),
        1,
        cv2.LINE_8,
    )
    ocr = FakeOCR("1085")

    reading = NumberReader(ocr=ocr).read(
        other_font, max_digits=6, use_ocr=True
    )

    assert reading.quality is Quality.UNKNOWN
    assert reading.value is None
    assert "weakly matched digit" in reading.detail
    # Weak template evidence is rejected before paying subprocess OCR latency.
    assert ocr.calls == 0


def test_split_ocr_boxes_are_joined_as_one_bounded_line():
    class SplitOCR:
        def read_text_boxes(self, image, *, psm=7, timeout=3.0):
            return (
                OCRBox("7", (1, 3, 7, 15), 0.91),
                OCRBox("10", (8, 3, 22, 15), 0.93),
            )

    reader = NumberReader(ocr=SplitOCR(), ambiguity_margin=1.0)
    reading = reader.read(fixture_number(3, 24), max_digits=6, use_ocr=True)

    assert reading.quality is Quality.KNOWN
    assert reading.value == 710
    assert reading.diagnostics["ocr_boxes"] == ((1, 3, 7, 15), (8, 3, 22, 15))


def test_default_reader_never_invokes_ocr(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("subprocess OCR must be explicitly configured")

    monkeypatch.setattr(subprocess, "run", unexpected)
    assert NumberReader().read(fixture_number(3, 24), max_digits=6).value == 710


def test_per_read_ocr_opt_in_keeps_configured_backend_off_fast_path():
    ocr = FakeOCR("710")
    reader = NumberReader(ocr=ocr, ambiguity_margin=1.0)

    reading = reader.read(fixture_number(3, 24), max_digits=6)

    assert reading.quality is Quality.UNKNOWN
    assert ocr.calls == 0


def test_tesseract_helper_is_bounded_and_parses_normalized_box_scores(monkeypatch):
    tsv = (
        b"level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        b"5\t1\t1\t1\t1\t1\t2\t3\t12\t7\t87.5\t1,085\n"
    )
    observed = {}

    def completed(command, **kwargs):
        observed["command"] = command
        observed.update(kwargs)
        return subprocess.CompletedProcess(command, 0, tsv, b"")

    monkeypatch.setattr(subprocess, "run", completed)
    monkeypatch.setattr("tibia_client.adaptive_text.shutil.which", lambda executable: executable)
    helper = TesseractOCR(executable="/usr/bin/tesseract")
    boxes = helper.read_text_boxes(np.full((20, 30, 3), 72, dtype=np.uint8), timeout=30)

    assert helper.available
    assert boxes == (OCRBox("1,085", (2, 3, 14, 10), 0.875),)
    assert observed["shell"] is False
    assert observed["timeout"] == 10.0
    assert observed["command"][:3] == ["/usr/bin/tesseract", "stdin", "stdout"]
    assert observed["input"].startswith(b"\x89PNG")


def test_tesseract_numeric_reader_uses_a_digit_and_comma_whitelist(monkeypatch):
    observed = {}

    def completed(command, **kwargs):
        observed["command"] = command
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", completed)
    monkeypatch.setattr("tibia_client.adaptive_text.shutil.which", lambda executable: executable)
    helper = TesseractOCR(executable="/usr/bin/tesseract")

    assert helper.read_number_boxes(np.full((20, 30, 3), 72, dtype=np.uint8)) == ()
    assert observed["command"] == [
        "/usr/bin/tesseract",
        "stdin",
        "stdout",
        "--psm",
        "7",
        "-c",
        "tessedit_char_whitelist=0123456789,",
        "tsv",
    ]


def test_tesseract_unavailable_is_graceful_and_does_not_spawn(monkeypatch):
    monkeypatch.setattr("tibia_client.adaptive_text.shutil.which", lambda executable: None)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("unavailable helper must not spawn"),
    )
    helper = TesseractOCR()

    assert not helper.available
    assert helper.read_text_boxes(np.zeros((3, 4, 3), dtype=np.uint8)) == ()


def test_tesseract_rejects_unbounded_or_malformed_requests():
    helper = TesseractOCR(executable="/usr/bin/tesseract")
    with pytest.raises(ValueError, match="pixel budget"):
        helper.read_text_boxes(np.zeros((2000, 2100, 3), dtype=np.uint8))
    with pytest.raises(ValueError, match="psm"):
        helper.read_text_boxes(np.zeros((2, 2, 3), dtype=np.uint8), psm=7_000)
