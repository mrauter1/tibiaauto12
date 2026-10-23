"""Benchmark validity checks, independent of WinRT or the experimental decoder."""
from hashlib import sha256
import json
from types import SimpleNamespace

import numpy as np
from PIL import Image
import pytest

from tools import benchmark_battle_glyphs as bench


def prediction(text):
    return {"text": text, "candidate": text or "", "reason": "fake"}


def observation(expected, text, error=None):
    return {"score": bench.score_observation(expected, prediction(text), error),
            "elapsed_ms": 1.0, "error": error}


def test_metrics_preserve_abstentions_errors_and_case_sensitive_failures():
    calls = [observation("Sea Serpent", "Sea Serpent"),
             observation("Sea Serpent", "seaserpent"),
             observation("Rat", None), observation("Rat", None, "engine failed")]
    result = bench.aggregate(calls, positive=True)
    assert result["cases"] == 4
    assert result["accepted_precision"] == .5
    assert result["normalized_accepted_precision"] == 1
    assert result["exact_coverage"] == .25
    assert result["normalized_exact_coverage"] == .5
    assert (result["false_accepted"], result["abstentions"], result["errors"]) == (1, 1, 1)
    assert result["cer_expected_characters"] == 25
    assert result["character_error_rate"] == 6 / 25
    assert result["successful_latency"]["samples"] == 3
    assert result["error_latency"]["samples"] == 1
    assert bench.aggregate([], positive=True)["accepted_precision"] is None


def test_negative_acceptance_is_never_a_correct_name():
    result = bench.aggregate([observation(None, "Rat"), observation(None, None)], positive=False)
    assert result["false_accepted"] == 1
    assert result["false_acceptance_rate"] == .5
    assert result["accepted_precision"] is None
    assert result["character_error_rate"] is None


def test_every_repeat_reinvokes_decoder_with_pixels_only_and_alternates_order():
    calls = []
    pixels = np.zeros((12, 20, 3), dtype=np.uint8)
    def decoder(name):
        def read(rgb, scale):
            assert isinstance(rgb, np.ndarray) and rgb.shape == pixels.shape
            assert not rgb.any()  # Another reader must not mutate our input.
            assert scale == 1.0
            calls.append(name)
            rgb[:] = 1
            return prediction("Rat")
        return read
    cases = [bench.Case(str(i), "screen", "positive_row", "Rat", pixels) for i in range(2)]
    readers = {name: decoder(name) for name in ("glyph", "windows-ocr")}
    availability = {name: {"status": "available"} for name in readers}
    report = bench.benchmark(cases, readers, availability, repeats=2)
    assert calls == ["glyph", "windows-ocr", "windows-ocr", "glyph",
                     "windows-ocr", "glyph", "glyph", "windows-ocr",
                     "glyph", "windows-ocr", "windows-ocr", "glyph"]
    assert len(report["invocation_order"]) == 12
    assert report["readers"]["glyph"]["primary_rows"]["repeat_uncached"]["cases"] == 4
    assert not pixels.any()


def test_unavailable_reader_is_null_and_decoder_errors_are_separate():
    def broken(rgb, scale):
        raise RuntimeError("no language")
    cases = [bench.Case("r", "s", "positive_row", "Rat", np.zeros((12, 20, 3), np.uint8))]
    availability = {"glyph": {"status": "unavailable", "error": "asset missing"},
                    "windows-ocr": {"status": "available"}}
    report = bench.benchmark(cases, {"windows-ocr": broken}, availability, 1)
    assert report["readers"]["glyph"]["primary_rows"] is None
    metrics = report["readers"]["windows-ocr"]["primary_rows"]["first"]
    assert (metrics["errors"], metrics["abstentions"], metrics["exact_coverage"]) == (1, 0, 0)
    assert "RuntimeError: no language" == report["cases"][0]["readers"]["windows-ocr"][0]["error"]


def test_windows_adapter_uses_production_threshold_and_join_rows(monkeypatch):
    from tibia_client import battle_text
    rgb = np.array([[[100, 100, 139], [100, 100, 140], [99, 100, 100], [255, 255, 255]]], dtype=np.uint8)
    received = []
    def read_lines(mask):
        received.append(mask.copy())
        return (("Sea", (0, 1, 10, 10)), ("Serpent", (11, 1, 40, 10)))
    monkeypatch.setattr(battle_text, "_read_lines", read_lines)
    result = bench.windows_decode(rgb, 1.0)
    assert received[0].tolist() == [[True, False, False, True]]
    assert result["text"] == "Sea Serpent"
    monkeypatch.setattr(battle_text, "_read_lines", lambda _: (("Rat", (0, 1, 10, 8)), ("Rat", (0, 20, 10, 28))))
    assert bench.windows_decode(rgb, 1.0)["text"] is None


def test_glyph_adapter_has_no_truth_argument_and_optional_mask_is_explicit():
    calls = []
    class FakeGlyph:
        def read_line(self, rgb, *, scale):
            calls.append((rgb.copy(), scale))
            return SimpleNamespace(text=None, candidate="Raf", score=.8, margin=.01,
                                   boxes=((1, 2, 3, 4),), reason="ambiguous")
    rgb = np.array([[[110, 110, 110], [100, 0, 0]]], np.uint8)
    raw = bench.glyph_decoder(FakeGlyph())(rgb, 1.25)
    bench.glyph_decoder(FakeGlyph(), masked=True)(rgb, 1.25)
    np.testing.assert_array_equal(calls[0][0], rgb)
    assert calls[1][0].tolist() == [[[255, 255, 255], [0, 0, 0]]]
    assert raw["text"] is None and raw["candidate"] == "Raf"


def fixture_corpus(tmp_path):
    image = tmp_path / "source.png"
    Image.fromarray(np.zeros((40, 60, 3), np.uint8)).save(image)
    source_hash = sha256(image.read_bytes()).hexdigest()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"screenshots": [{"id": "s", "file": "source.png",
        "integrity": {"sha256": source_hash}, "split": "heldout"}]}))
    annotation = tmp_path / "labels.json"
    annotation.write_text(json.dumps({"schema": "tibiaauto.visual-annotations", "box_format": "xyxy-half-open",
        "screenshots": [{"id": "s", "source_sha256": source_hash, "battle_list": {
            "status": "reviewed", "region": [0, 0, 60, 35], "partial_names": True,
            "excluded_queries": ["Nightmare"], "rows": [
                {"name": "Rat", "box": [20, 0, 60, 10]},
                {"name": "Nightmare", "box": [20, 10, 60, 20]},
                {"name": "Rat", "box": [20, 30, 60, 40]}]}}]}, indent=2))
    sidecar = tmp_path / "labels.json.sha256"
    sidecar.write_text(sha256(annotation.read_bytes().replace(b"\n", b"\r\n")).hexdigest())
    return annotation, manifest


def test_corpus_freeze_accepts_only_newline_equivalence_and_selection_is_fixed(tmp_path):
    annotation, manifest = fixture_corpus(tmp_path)
    cases, audit = bench.load_corpus(annotation, manifest)
    assert len(cases) == 1 and cases[0].expected == "Rat"
    assert cases[0].pixels.shape == (10, 40, 3)  # No name-informed trimming.
    assert audit["freeze_verification"]["match_mode"] == "newline_only_crlf"
    assert audit["freeze_verification"]["raw_hash_matches"] is False
    excluded = audit["selection_audit"][0]["rows"][1:]
    assert excluded[0]["exclusion_reasons"] == ["name listed in excluded_queries"]
    assert excluded[1]["exclusion_reasons"] == ["row box outside declared battle region"]
    annotation.write_text(annotation.read_text().replace('"Rat"', '"Bat"'))
    with pytest.raises(ValueError, match="pre-inference freeze"):
        bench.load_corpus(annotation, manifest)


def test_corpus_rejects_changed_source_bytes(tmp_path):
    annotation, manifest = fixture_corpus(tmp_path)
    (tmp_path / "source.png").write_bytes(b"changed")
    with pytest.raises(ValueError, match="Source hash mismatch"):
        bench.load_corpus(annotation, manifest)
