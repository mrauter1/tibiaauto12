"""Offline pixels only: no capture, input, native windows, or GUI operations."""

from dataclasses import replace
import json

import numpy as np
from PIL import Image, PngImagePlugin
import pytest

from tibia_client import probe_known_dialog as dialog
from tools.evaluate_known_dialog import main


BOX = (15, 20, 45, 36)
VIEWPORT = (5, 5, 95, 75)


@pytest.fixture
def frames(tmp_path):
    generator = np.random.default_rng(271)
    patch = generator.integers(0, 256, (16, 30, 3), dtype=np.uint8)
    source = generator.integers(0, 70, (80, 100, 3), dtype=np.uint8)
    source[20:36, 15:45] = patch
    moved = generator.integers(0, 70, (80, 100, 3), dtype=np.uint8)
    moved[40:56, 50:80] = patch
    clean = generator.integers(0, 70, (80, 100, 3), dtype=np.uint8)
    paths = {}
    for name, pixels in (("source", source), ("moved", moved), ("clean", clean)):
        path = tmp_path / f"{name}.png"
        Image.fromarray(pixels).save(path)
        paths[name] = path
    paths["patch"] = patch
    return paths


def template(frames):
    return dialog.create_template(frames["source"], BOX, viewport=VIEWPORT, name="known_dialog")


def test_moved_heldout_header_vetoes_and_retains_method_hashes_and_coordinates(frames):
    result = dialog.evaluate_frame(template(frames), frames["moved"])
    assert result["verdict"] == "blocked_known_dialog"
    assert result["score"] >= .99
    assert result["location"] == [50, 40]
    assert result["match_box"] == [50, 40, 80, 56]
    assert result["method"] == "cv2.TM_CCOEFF_NORMED"
    assert result["threshold"] == .90
    assert result["source_leakage"] is False
    assert result["heldout_eligible"] is True
    assert result["safety_status"] == "unknown"
    assert len(result["frame"]["file_sha256"]) == 64
    assert len(result["frame"]["rgb_sha256"]) == 64
    assert len(result["frame"]["viewport_rgb_sha256"]) == 64
    assert result["template"]["template_box"] == list(BOX)
    assert json.loads(json.dumps(result)) == result


def test_clean_heldout_frame_has_no_known_veto_and_never_safe(frames):
    result = dialog.evaluate_frame(template(frames), frames["clean"])
    assert result["verdict"] == "no_known_veto"
    assert result["score"] < .90
    assert result["safety_status"] == "unknown"
    assert result["heldout_eligible"] is True


def test_same_source_match_is_flagged_and_cannot_pass_dataset(frames):
    selected = template(frames)
    result = dialog.evaluate_frame(selected, frames["source"])
    assert result["verdict"] == "blocked_known_dialog"
    assert result["source_leakage"] is True
    assert result["heldout_eligible"] is False
    assert "identical_full_source_rgb" in result["source_leakage_basis"]
    report = dialog.evaluate_dataset(selected, [frames["source"]], [frames["clean"]])
    assert report["summary"]["status"] == "fail"
    assert report["summary"]["invalid_or_leaked_cases"] == 1


def test_different_png_encoding_does_not_hide_source_pixel_leakage(frames, tmp_path):
    path = tmp_path / "metadata-reencoded.png"
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("note", "same RGB, different filename and encoded file")
    with Image.open(frames["source"]) as source:
        source.save(path, pnginfo=metadata)
    selected = template(frames)
    result = dialog.evaluate_frame(selected, path)
    assert result["frame"]["file_sha256"] != selected.source_file_sha256
    assert result["source_leakage"] is True


def test_border_only_changes_do_not_hide_identical_viewport_leakage(frames, tmp_path):
    path = tmp_path / "different-host-border.png"
    with Image.open(frames["source"]) as source:
        source.putpixel((0, 0), (255, 255, 255))
        source.save(path)
    result = dialog.evaluate_frame(template(frames), path)
    assert result["source_leakage_basis"] == ["identical_source_viewport_rgb"]
    assert result["heldout_eligible"] is False


def test_both_genuinely_heldout_labels_can_pass_only_specific_veto_evaluation(frames):
    report = dialog.evaluate_dataset(template(frames), [frames["moved"]], [frames["clean"]])
    assert report["summary"]["status"] == "pass"
    assert report["summary"]["eligible_heldout_cases"] == 2
    assert report["overall_safety"] == "unknown"
    assert report["production_wiring"] == "none"
    assert report["threshold_matches_default"] is True
    assert "not independently verified" in report["threshold_policy"]


def test_same_label_duplicate_cases_are_reported_without_inflating_unique_counts(frames):
    report = dialog.evaluate_dataset(template(frames), [frames["moved"], frames["moved"]],
                                      [frames["clean"]])
    assert report["summary"]["eligible_heldout_cases"] == 3
    assert report["summary"]["unique_eligible_rgb_frames"] == 2
    assert report["summary"]["unique_eligible_viewports"] == 2
    assert report["summary"]["duplicate_eligible_viewports"] == 1
    assert report["summary"]["unique_positive_viewports"] == 1
    assert report["summary"]["unique_negative_viewports"] == 1


def test_contradictory_identical_viewport_labels_cannot_pass(frames):
    report = dialog.evaluate_dataset(template(frames), [frames["moved"]], [frames["moved"]])
    assert report["summary"]["status"] == "fail"
    assert report["summary"]["contradictory_viewport_labels"] is True


def test_missing_negative_cannot_claim_heldout_validation(frames):
    report = dialog.evaluate_dataset(template(frames), [frames["moved"]], [])
    assert report["summary"]["status"] == "fail"
    assert report["summary"]["both_labels_required"] is True
    assert report["summary"]["both_labels_present"] is False


def test_dataset_stops_consuming_unbounded_iterables_at_the_case_cap(frames):
    def unlimited():
        while True:
            yield frames["moved"]

    with pytest.raises(ValueError, match="at most"):
        dialog.evaluate_dataset(template(frames), unlimited(), [])


def test_template_appearing_only_outside_search_viewport_is_not_found(frames, tmp_path):
    with Image.open(frames["clean"]) as image:
        image.paste(Image.fromarray(frames["patch"]), (0, 0))
        image.save(tmp_path / "outside.png")
    selected = replace(template(frames), viewport=(35, 5, 95, 75))
    result = dialog.evaluate_frame(selected, tmp_path / "outside.png")
    assert result["verdict"] == "no_known_veto"
    assert result["location"][0] >= 35


@pytest.mark.parametrize("score,verdict", [(.89, "no_known_veto"), (.9, "blocked_known_dialog"),
                                          (.91, "blocked_known_dialog")])
def test_threshold_boundary_is_fixed_and_explicit(frames, monkeypatch, score, verdict):
    monkeypatch.setattr(dialog.cv2, "matchTemplate", lambda *arguments: np.array([[score]], dtype=np.float64))
    result = dialog.evaluate_frame(template(frames), frames["moved"], threshold=.90)
    assert result["verdict"] == verdict
    assert result["score"] == score


@pytest.mark.parametrize("threshold", [0, -1, 1.1, True, float("nan"), float("inf"), "0.9"])
def test_invalid_thresholds_are_rejected_before_scoring(frames, threshold):
    with pytest.raises(ValueError, match="threshold"):
        dialog.evaluate_frame(template(frames), frames["moved"], threshold=threshold)


@pytest.mark.parametrize("color", [(0, 0, 0), (255, 255, 255), (255, 0, 0)])
def test_constant_template_colors_are_invalid(tmp_path, color):
    path = tmp_path / "constant.png"
    Image.new("RGB", (100, 80), color).save(path)
    with pytest.raises(ValueError, match="constant|variation"):
        dialog.create_template(path, BOX, viewport=VIEWPORT)


@pytest.mark.parametrize("box,viewport", [
    ((15, 20, 20, 23), VIEWPORT),
    ((-1, 20, 45, 36), VIEWPORT),
    ((15, 20, 1000, 36), VIEWPORT),
    ((True, 20, 45, 36), VIEWPORT),
    (BOX, (20, 20, 95, 75)),
    (BOX, (5, 5, 500, 75)),
])
def test_invalid_template_and_viewport_boxes_reject(frames, box, viewport):
    with pytest.raises(ValueError):
        dialog.create_template(frames["source"], box, viewport=viewport)


def test_changed_frame_dimensions_are_unknown_without_resizing(frames, tmp_path):
    path = tmp_path / "resized.png"
    Image.new("RGB", (200, 160)).save(path)
    result = dialog.evaluate_frame(template(frames), path)
    assert result["verdict"] == "unknown"
    assert result["heldout_eligible"] is False
    assert "dimensions differ" in result["error"]


def test_missing_or_malformed_frame_is_unknown(frames, tmp_path):
    result = dialog.evaluate_frame(template(frames), tmp_path / "missing.png")
    assert result["verdict"] == "unknown"
    assert result["score"] is None
    path = tmp_path / "bad.png"
    path.write_text("not pixels", encoding="utf-8")
    result = dialog.evaluate_frame(template(frames), path)
    assert result["verdict"] == "unknown"


def test_invalid_frame_path_argument_is_explicit_unknown(frames):
    result = dialog.evaluate_frame(template(frames), None)
    assert result["verdict"] == "unknown" and result["heldout_eligible"] is False


def test_nonfinite_matcher_score_is_unknown(frames, monkeypatch):
    monkeypatch.setattr(dialog.cv2, "minMaxLoc", lambda scores: (0, float("inf"), (0, 0), (0, 0)))
    result = dialog.evaluate_frame(template(frames), frames["moved"])
    assert result["verdict"] == "unknown" and result["heldout_eligible"] is False


def test_cli_writes_explicit_offline_report(frames, tmp_path, capsys):
    output = tmp_path / "evaluation.json"
    result = main(["--template-source", str(frames["source"]), "--template-box", *map(str, BOX),
                   "--viewport", *map(str, VIEWPORT), "--positive", str(frames["moved"]),
                   "--negative", str(frames["clean"]), "--output", str(output)])
    assert result == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["status"] == "pass"
    assert report["overall_safety"] == "unknown"
    assert json.loads(capsys.readouterr().out)["report"] == str(output.resolve())


def test_cli_invalid_template_preserves_unknown_report(frames, tmp_path, capsys):
    output = tmp_path / "unknown.json"
    result = main(["--template-source", str(frames["source"]), "--template-box", "0", "0", "1", "1",
                   "--viewport", *map(str, VIEWPORT), "--output", str(output)])
    assert result == 2
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["verdict"] == "unknown" and report["overall_safety"] == "unknown"


def test_cli_never_overwrites_input_evidence(frames, capsys):
    before = frames["source"].read_bytes()
    result = main(["--template-source", str(frames["source"]), "--template-box", *map(str, BOX),
                   "--viewport", *map(str, VIEWPORT), "--output", str(frames["source"])])
    assert result == 2 and frames["source"].read_bytes() == before
    assert "differ from every input" in capsys.readouterr().out


def test_cli_preserves_preexisting_report(frames, tmp_path, capsys):
    output = tmp_path / "existing.json"
    output.write_text("earlier report", encoding="utf-8")
    result = main(["--template-source", str(frames["source"]), "--template-box", *map(str, BOX),
                   "--viewport", *map(str, VIEWPORT), "--positive", str(frames["moved"]),
                   "--negative", str(frames["clean"]), "--output", str(output)])
    assert result == 2 and output.read_text(encoding="utf-8") == "earlier report"
    assert "output exists" in capsys.readouterr().out


@pytest.mark.parametrize("frame_name", ["source", "moved", "clean"])
def test_memory_image_evaluation_preserves_exact_file_matcher_semantics(frames, frame_name):
    selected = template(frames)
    file_result = dialog.evaluate_frame(selected, frames[frame_name])
    with Image.open(frames[frame_name]) as saved:
        image = saved.convert("RGB").copy()
    memory_result = dialog.evaluate_image(selected, image)
    for key in ("verdict", "score", "location", "match_box", "source_leakage", "heldout_eligible"):
        assert memory_result[key] == file_result[key]
    for key in ("rgb_sha256", "viewport_rgb_sha256", "size"):
        assert memory_result["frame"][key] == file_result["frame"][key]
    assert memory_result["frame_path"] is None
    assert memory_result["frame"]["file_sha256"] is None
    assert memory_result["frame"]["source_kind"] == "in_memory_rgb"
    assert memory_result["safety_status"] == "unknown"


def test_memory_evaluation_does_not_reopen_source_or_candidate_files(frames, monkeypatch):
    selected = template(frames)
    with Image.open(frames["moved"]) as saved:
        image = saved.convert("RGB").copy()

    def forbidden(*arguments):
        raise AssertionError("memory evaluator must not read files")

    monkeypatch.setattr(dialog, "_read", forbidden)
    result = dialog.evaluate_image(selected, image)
    assert result["verdict"] == "blocked_known_dialog"


def test_invalid_memory_or_changed_geometry_remains_unknown(frames):
    selected = template(frames)
    assert dialog.evaluate_image(selected, None)["verdict"] == "unknown"
    result = dialog.evaluate_image(selected, Image.new("RGB", (99, 80)))
    assert result["verdict"] == "unknown" and result["heldout_eligible"] is False
    assert "dimensions differ" in result["error"]


@pytest.fixture
def known_report(frames, tmp_path):
    report = dialog.evaluate_dataset(template(frames), [frames["moved"]], [frames["clean"]])
    path = tmp_path / "known-dialog.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return path, report


def test_explicit_known_report_load_verifies_template_source_and_frozen_threshold(known_report):
    path, report = known_report
    loaded, threshold, provenance = dialog.load_template_report(path)
    assert threshold == .90 and loaded.metadata() == report["template"]
    assert provenance["source_hashes_verified"] is True
    assert provenance["threshold_enforced"] == .90
    assert len(provenance["report_file_sha256"]) == 64
    assert provenance["report_does_not_authorize_input"] is True
    assert json.loads(json.dumps(provenance)) == provenance


@pytest.mark.parametrize("field", ["source_file_sha256", "source_rgb_sha256", "source_viewport_rgb_sha256",
                                  "template_rgb_sha256", "source_size", "template_size"])
def test_known_report_metadata_tampering_is_rejected(known_report, field):
    path, report = known_report
    report["template"][field] = "changed"
    path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError, match="mismatch"):
        dialog.load_template_report(path)


def test_known_report_changed_source_file_is_rejected(known_report, frames):
    path, report = known_report
    with Image.open(frames["source"]) as saved:
        saved.putpixel((0, 0), (255, 255, 255))
        saved.save(frames["source"])
    with pytest.raises(ValueError, match="mismatch"):
        dialog.load_template_report(path)


@pytest.mark.parametrize("field,value", [("threshold", .89), ("method", "different"),
                                        ("schema", "different"), ("version", 2)])
def test_known_report_wrong_policy_is_rejected(known_report, field, value):
    path, report = known_report
    report[field] = value
    path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError):
        dialog.load_template_report(path)


def test_failed_offline_report_cannot_supply_the_live_candidate(known_report):
    path, report = known_report
    report["summary"]["status"] = "fail"
    path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError, match="passed"):
        dialog.load_template_report(path)
