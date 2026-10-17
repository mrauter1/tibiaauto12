"""Audit collection integrity without invoking a reader or generating inputs."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

from tools.benchmark_resources import FIELDS, load_manifest


ROOT = Path(__file__).resolve().parent / "fixtures/screenshots"


def test_october_collection_matches_pre_inference_annotation_freeze():
    _, entries = load_manifest(ROOT / "manifest.json")
    freeze = json.loads((ROOT / "collection-2026-10-17.json").read_text(encoding="utf-8"))
    by_id = {entry["id"]: entry for entry in entries}
    batch = [entry for entry in entries if entry.get("collection_batch") == freeze["batch"]]
    assert len(batch) == freeze["selection_counts"]["retained"] == 32
    assert sum(entry["coverage"] == "full_screenshot" for entry in batch) == 31
    assert {entry["id"] for entry in batch} == {entry["id"] for entry in freeze["entries"]}
    for frozen in freeze["entries"]:
        canonical = json.dumps(by_id[frozen["id"]], sort_keys=True, separators=(",", ":"))
        assert sha256(canonical.encode()).hexdigest() == frozen["annotation_sha256"]
    for members in freeze["source_groups"].values():
        assert len({by_id[identifier]["split"] for identifier in members}) == 1
    digests = [entry["integrity"]["sha256"] for entry in entries]
    assert len(digests) == len(set(digests))
    for entry in batch:
        assert entry["client"]["version"] is None
        assert entry["capture_fidelity"]["downloaded_bytes_preserved"] is True
        assert entry["source"]["original_filename"]
        assert entry["source"]["page_url"].startswith("https://")
        assert entry["source"]["image_url"].startswith("https://")
        assert set(entry["resource_annotations"]) == set(FIELDS)
        width, height = entry["integrity"]["width"], entry["integrity"]["height"]
        for name, annotation in entry["resource_annotations"].items():
            truth = entry["labels"][name]
            assert annotation["value_box"] == truth["box"]
            if truth["value"] is not None:
                assert int(annotation["display_text"].replace(",", "")) == truth["value"]
                assert annotation["readability"] == "readable"
            for key in ("label_box", "value_box"):
                box = annotation[key]
                if box is not None:
                    left, top, right, bottom = box
                    assert 0 <= left < right <= width
                    assert 0 <= top < bottom <= height
            if not truth["visible"]:
                assert truth["value"] is None
                assert annotation["display_text"] is None
    # Neither an unreadable clipped row nor a decimal reference is rounded or
    # filled using a nearby status bar or another screenshot.
    clipped = by_id["reddit-illustrious-mo9lbk5"]["labels"]["resource_health"]
    assert clipped["visible"] is True and clipped["value"] is None
    decimal = by_id["dfg-demolidores-4KCJ"]
    assert decimal["labels"]["resource_capacity"]["value"] is None
    assert decimal["resource_annotations"]["resource_capacity"]["display_text"] == "104966.56"
