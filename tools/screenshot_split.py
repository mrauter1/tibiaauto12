"""Freeze source-group development/test membership without reading model outputs."""
from __future__ import annotations

from collections import defaultdict
from hashlib import sha256
import json
from pathlib import Path


def text_digest(path):
    """Match Git text blobs across LF/CRLF checkouts; never use on image bytes."""
    return sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def annotation_digest(entry):
    return sha256(json.dumps(entry, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def source_group(entry):
    return entry.get("lineage", {}).get("group") or "source:" + (
        entry["source"].get("page_url") or entry["id"]
    )


def group_assignments(entries):
    """Include older relatives whose entries predate explicit lineage metadata."""
    assigned = {entry["id"]: source_group(entry) for entry in entries}
    for entry in entries:
        lineage = entry.get("lineage", {})
        for relative in lineage.get("known_regression_relatives", []) + lineage.get("related_ids", []):
            if relative not in assigned:
                raise ValueError("Unknown source relative: " + relative)
            previous = assigned[relative]
            current = assigned[entry["id"]]
            for identifier, group in list(assigned.items()):
                if group == previous:
                    assigned[identifier] = current
    return assigned


def make_split(entries, *, batch, seed):
    """Keep previously examined sources in development; reserve new whole groups.

    SHA-256 ordering and first-reachable subset selection make the exact-size
    assignment reproducible across Python versions. Values and reader outcomes
    never participate in selection. Crops are outside the full-screenshot ratio.
    """
    full = [e for e in entries if e["coverage"] == "full_screenshot"]
    prior = [e for e in entries if e.get("collection_batch") != batch]
    assigned = group_assignments(entries)
    known_groups = {assigned[e["id"]] for e in prior}
    groups = defaultdict(list)
    for entry in full:
        if entry.get("collection_batch") == batch and assigned[entry["id"]] not in known_groups:
            groups[assigned[entry["id"]]].append(entry["id"])
    order = sorted(groups, key=lambda g: (sha256((seed + "\n" + g).encode()).hexdigest(), g))
    target = round(len(full) * 0.2)
    reachable = {0: ()}
    for group in order:
        for count, selected in list(reachable.items()):
            total = count + len(groups[group])
            if total <= target and total not in reachable:
                reachable[total] = selected + (group,)
    if target not in reachable:
        raise ValueError("Cannot achieve the requested test count with untouched whole groups")
    test = {identifier for g in reachable[target] for identifier in groups[g]}
    members = {"development": [], "test": [], "reference": []}
    all_groups = defaultdict(list)
    for entry in entries:
        role = ("reference" if entry["coverage"] != "full_screenshot" else
                "test" if entry["id"] in test else "development")
        members[role].append(entry["id"])
        all_groups[assigned[entry["id"]]].append(entry["id"])
    return {
        "schema": "tibiaauto.screenshot-split", "version": 1,
        "batch": batch, "seed": seed,
        "algorithm": "SHA-256(seed + newline + group), ascending; first reachable whole-group subset of round(0.2 * full count). No values or reader results used.",
        "scope": "All full screenshots, including safety controls and different-interface references. Supplemental crops excluded from the 80/20 denominator.",
        "historical_split_policy": "Manifest train/heldout labels are historical. This file is authoritative for current development/test membership.",
        "test_policy": "New source groups only; no reader changes or parameter selection after seeing this test evaluation. Refresh the test set when its failures guide tuning.",
        "target_test_fraction": 0.2,
        "full_screenshots": len(full), "test_images": target,
        "previously_inspected_ids": sorted(e["id"] for e in prior),
        "candidate_group_order": order,
        "selected_test_groups": list(reachable[target]),
        "partitions": {key: sorted(value) for key, value in members.items()},
        "source_groups": {key: sorted(value) for key, value in sorted(all_groups.items())},
        "annotations": {e["id"]: annotation_digest(e) for e in entries},
    }


def validate_split(document, entries):
    by_id = {entry["id"]: entry for entry in entries}
    partitions = document["partitions"]
    if set(partitions) != {"development", "test", "reference"}:
        raise ValueError("Unexpected split partitions")
    flat = [identifier for values in partitions.values() for identifier in values]
    if len(flat) != len(set(flat)) or set(flat) != set(by_id):
        raise ValueError("Split must cover every image exactly once")
    membership = {identifier: role for role, values in partitions.items() for identifier in values}
    for identifier, entry in by_id.items():
        if (membership[identifier] == "reference") != (entry["coverage"] != "full_screenshot"):
            raise ValueError("Full screenshots and supplemental crops have inconsistent membership")
        if document["annotations"].get(identifier) != annotation_digest(entry):
            raise ValueError("Annotation changed after split freeze: " + identifier)
    actual_groups = defaultdict(set)
    assigned = group_assignments(entries)
    for entry in entries:
        actual_groups[assigned[entry["id"]]].add(entry["id"])
    if {g: set(ids) for g, ids in document["source_groups"].items()} != dict(actual_groups):
        raise ValueError("Source-group metadata differs from the manifest")
    for ids in actual_groups.values():
        roles = {membership[identifier] for identifier in ids}
        if "test" in roles and roles != {"test"}:
            raise ValueError("Source group leaks across test and development/reference")
    if set(document["previously_inspected_ids"]) & set(partitions["test"]):
        raise ValueError("Previously inspected evidence cannot enter the untouched test set")
    full_count = len(partitions["development"]) + len(partitions["test"])
    if full_count != document["full_screenshots"] or len(partitions["test"]) != round(full_count * 0.2):
        raise ValueError("Split does not match the declared 80/20 full-screenshot count")
    expected = make_split(entries, batch=document["batch"], seed=document["seed"])
    if partitions != expected["partitions"] or document["selected_test_groups"] != expected["selected_test_groups"]:
        raise ValueError("Partition differs from its deterministic predeclared algorithm")
    return membership


def load_split(path: Path, entries):
    document = json.loads(path.read_text(encoding="utf-8"))
    return document, validate_split(document, entries)
