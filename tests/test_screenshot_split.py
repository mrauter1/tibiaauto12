"""Protect untouched evaluation membership and manual N/A semantics."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from tools.benchmark_resources import load_manifest
from tools.screenshot_split import annotation_digest, load_split, text_digest, validate_split

CORPUS = Path(__file__).parent / "fixtures/screenshots"


@pytest.fixture(scope="module")
def frozen():
    _, entries = load_manifest(CORPUS / "manifest.json")
    document, membership = load_split(CORPUS / "split-2026-10-17-latest.json", entries)
    return entries, document, membership


def test_latest_collection_and_split_match_pre_inference_freeze(frozen):
    entries, split, membership = frozen
    freeze = json.loads((CORPUS / "collection-2026-10-17-latest.json").read_text())
    batch = [e for e in entries if e.get("collection_batch") == freeze["batch"]]
    by_id = {e["id"]: e for e in entries}
    assert len(entries) == 81 and len(batch) == 30
    assert {role: len(ids) for role, ids in split["partitions"].items()} == {
        "development": 57, "test": 14, "reference": 10,
    }
    assert len(split["previously_inspected_ids"]) == 51
    assert all(membership[e["id"]] != "test" for e in entries if e not in batch)
    assert len(freeze["source_groups"]) == 20
    assert all(e["coverage"] == "full_screenshot" for e in batch)
    assert sum(v["value"] is not None for e in batch for v in e["labels"].values()) == 76
    assert sum(v["value"] is None for e in batch for v in e["labels"].values()) == 44
    assert {f["id"] for f in freeze["entries"]} == {e["id"] for e in batch}
    for frozen_entry in freeze["entries"]:
        assert annotation_digest(by_id[frozen_entry["id"]]) == frozen_entry["annotation_sha256"]
    for name, digest in freeze["annotation_files_sha256"].items():
        assert text_digest(CORPUS / name) == digest
    for entry in batch:
        assert entry["client"]["version"] is None
        assert entry["capture_fidelity"]["downloaded_bytes_preserved"] is True
        width, height = entry["integrity"]["width"], entry["integrity"]["height"]
        for field, annotation in entry["resource_annotations"].items():
            truth = entry["labels"][field]
            assert annotation["value_box"] == truth["box"]
            if truth["value"] is not None:
                assert int(annotation["display_text"].replace(",", "")) == truth["value"]
                assert annotation["readability"] in {"readable", "clipped_but_readable"}
            else:
                assert annotation["display_text"] is None
                assert annotation["readability"] not in {"readable", "clipped_but_readable"}
            for key in ("value_box", "label_box"):
                box = annotation[key]
                if box is not None:
                    l, t, r, b = box
                    assert 0 <= l < r <= width and 0 <= t < b <= height


def test_split_rejects_group_leakage_and_annotation_changes(frozen):
    entries, split, _ = frozen
    bad = deepcopy(split)
    group = next(ids for ids in split["source_groups"].values()
                 if len(ids) > 1 and set(ids).issubset(split["partitions"]["test"]))
    identifier = group[0]
    bad["partitions"]["test"].remove(identifier)
    bad["partitions"]["development"].append(identifier)
    with pytest.raises(ValueError, match="leaks"):
        validate_split(bad, entries)
    changed = deepcopy(entries)
    target = next(e for e in changed if e["id"] in split["partitions"]["test"]
                  and e["labels"]["resource_health"]["value"] is not None)
    target["labels"]["resource_health"]["value"] += 1
    with pytest.raises(ValueError, match="Annotation changed"):
        validate_split(split, changed)


def test_partial_fields_are_scored_individually_without_inventing_hidden_values(frozen):
    entries, _, _ = frozen
    partial = [e for e in entries if e.get("collection_batch") == "2026-10-17-latest"
               and e["source"]["author"] == "musquitu96"]
    assert len(partial) == 4
    for entry in partial:
        assert entry["classification"] == "similar"
        assert entry["labels"]["resource_health"]["value"] is not None
        assert not entry["expected_discovery"]  # Four-row automatic binding is unavailable.
        assert all(entry["labels"][name]["value"] is None for name in
                   ("resource_mana", "resource_soul", "resource_capacity"))
    clipped = next(e for e in entries if e["id"].startswith("reddit-aernithrawaxe-"))
    assert clipped["labels"]["resource_health"]["visible"] is True
    assert clipped["labels"]["resource_health"]["value"] is None
    assert clipped["resource_annotations"]["resource_health"]["readability"] == "clipped_unreadable"


def test_text_freeze_is_portable_but_still_detects_content_changes(tmp_path):
    path = tmp_path / "metadata.json"
    path.write_bytes(b'{\n  "value": 100\n}\n')
    expected = text_digest(path)
    path.write_bytes(b'{\r\n  "value": 100\r\n}\r\n')
    assert text_digest(path) == expected
    path.write_bytes(b'{\r\n  "value": 101\r\n}\r\n')
    assert text_digest(path) != expected
