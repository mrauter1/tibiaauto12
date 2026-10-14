#!/usr/bin/env python3
"""Reproducible benchmark for the adaptive resource reader.

The corpus manifest is treated as test data, not a suggestion: every listed
file is integrity checked and every entry produces a result row.  OCR is off
unless ``--ocr`` is supplied.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from copy import deepcopy
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from math import ceil, floor
from pathlib import Path
from statistics import median
import sys
from time import perf_counter
from typing import Any, Iterable, Mapping

import cv2
import numpy as np
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.perception import Frame  # noqa: E402
from tibia_client.adaptive import FIELDS, ResourceReader  # noqa: E402
from tibia_client.models import Quality  # noqa: E402

DEFAULT_MANIFEST = ROOT / "tests/fixtures/screenshots/manifest.json"
CLASSIFICATIONS = {"confirmed", "similar", "negative"}
SPLITS = {"train", "heldout"}
ORIGINS = {"real", "synthetic"}
VERIFICATIONS = {"manual_verified", "visible_value_unknown", "not_visible"}
SCALE_BANK = (
    0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 1.00, 1.05, 1.10, 1.15,
    1.20, 1.25, 1.30, 1.40, 1.50, 1.60, 1.75, 2.00, 2.25, 2.50,
)


class ManifestError(ValueError):
    """The benchmark corpus is incomplete or internally inconsistent."""


@dataclass(frozen=True)
class BenchmarkCase:
    """Technique-independent, in-memory benchmark input derived from one real file."""

    id: str
    source_id: str
    source_sha256: str
    frame: Frame
    labels: dict[str, dict[str, Any]]
    split: str
    classification: str
    transform: str
    category: str = "generated_perturbation"
    case_sha256: str | None = None
    expected_discovery: bool = True
    eligible: bool = True
    exclusion: str | None = None
    coverage: str = ""
    prior_frame: Frame | None = None
    prior_labels: dict[str, dict[str, Any]] | None = None
    sequence_kind: str | None = None
    expected_cached_unknown_fields: tuple[str, ...] = ()
    expected_final_unknown_fields: tuple[str, ...] = ()
    abstention_reason: str | None = None
    ambiguity_evidence: str | None = None


def _pixels_digest(pixels: np.ndarray) -> str:
    return sha256(np.ascontiguousarray(pixels).tobytes()).hexdigest()


def _scaled_box(box: Iterable[int], factor: float) -> list[int]:
    left, top, right, bottom = box
    return [
        floor(left * factor),
        floor(top * factor),
        ceil(right * factor),
        ceil(bottom * factor),
    ]


def _transform_labels(
    labels: dict[str, dict[str, Any]],
    transform,
) -> dict[str, dict[str, Any]]:
    altered = deepcopy(labels)
    for label in altered.values():
        if label["box"] is not None:
            label["box"] = list(transform(label["box"]))
    return altered


def generated_benchmark_cases(
    entries: list[dict[str, Any]],
    manifest_path: Path = DEFAULT_MANIFEST,
) -> Iterable[BenchmarkCase]:
    """Yield reproducible generated cases without writing transformed screenshots.

    Dense scaling uses each real positive's annotated Skills-panel ROI to keep the
    complete scale bank practical.  Translation, width reflow, representative
    full-frame scaling, duplicate-panel rejection, and cached-layout invalidation
    are generated for every eligible real positive file.
    """
    for entry in entries:
        # A real multi-panel ambiguity control must remain an untouched original.
        # Generating variants would add no supported-render evidence and could
        # accidentally turn an abstention contract into a synthetic recall test.
        if entry.get("expected_final_unknown_fields"):
            continue
        panel = entry["skills_panel"]
        verified = all(
            entry["labels"][field]["visible"]
            and entry["labels"][field]["verification"] == "manual_verified"
            and entry["labels"][field]["value"] is not None
            for field in FIELDS
        )
        eligible = (
            entry["data_origin"] == "real"
            and entry["classification"] != "negative"
            and panel["visible"]
            and verified
        )
        if not eligible:
            reason = (
                "negative screenshot" if entry["classification"] == "negative"
                else "panel or four visually verified resource values unavailable"
            )
            yield BenchmarkCase(
                id=f"{entry['id']}::excluded",
                source_id=entry["id"],
                source_sha256=entry["integrity"]["sha256"],
                frame=Frame(Image.new("RGB", (1, 1))),
                labels=deepcopy(entry["labels"]),
                split=entry["split"],
                classification=entry["classification"],
                transform="excluded",
                expected_discovery=False,
                eligible=False,
                exclusion=reason,
                coverage=entry["coverage"],
            )
            continue

        image_path = manifest_path.parent / entry["file"]
        with Image.open(image_path) as image:
            pixels = np.asarray(image.convert("RGB"))
        source_frame = Frame(Image.fromarray(pixels), source=str(image_path))
        source_digest = entry["integrity"]["sha256"]
        height, width = pixels.shape[:2]

        def make_case(
            transform: str,
            altered: np.ndarray,
            labels: dict[str, dict[str, Any]],
            *,
            expected_discovery: bool = True,
            category: str = "generated_perturbation",
            prior_frame: Frame | None = None,
            sequence_kind: str | None = None,
            expected_cached_unknown_fields: tuple[str, ...] = (),
            expected_final_unknown_fields: tuple[str, ...] = (),
            classification: str | None = None,
        ) -> BenchmarkCase:
            return BenchmarkCase(
                id=f"{entry['id']}::{transform}",
                source_id=entry["id"],
                source_sha256=source_digest,
                frame=Frame(Image.fromarray(altered)),
                labels=labels,
                split=entry["split"],
                classification=classification or entry["classification"],
                transform=transform,
                category=category,
                case_sha256=_pixels_digest(altered),
                expected_discovery=expected_discovery,
                coverage=entry["coverage"],
                prior_frame=prior_frame,
                prior_labels=deepcopy(entry["labels"]) if prior_frame is not None else None,
                sequence_kind=sequence_kind,
                expected_cached_unknown_fields=expected_cached_unknown_fields,
                expected_final_unknown_fields=expected_final_unknown_fields,
            )

        top_pad, left_pad, bottom_pad, right_pad = 31, 53, 11, 17
        translated = np.pad(
            pixels,
            ((top_pad, bottom_pad), (left_pad, right_pad), (0, 0)),
            constant_values=42,
        )
        translated_labels = _transform_labels(
            entry["labels"],
            lambda box: [box[0] + left_pad, box[1] + top_pad,
                         box[2] + left_pad, box[3] + top_pad],
        )
        yield make_case("translated", translated, translated_labels)

        value_left = min(entry["labels"][field]["box"][0] for field in FIELDS)
        panel_left = panel["box"][0]
        split_x = max(panel_left + 1, value_left - 8)
        reflow_shift = max(24, round((panel["box"][2] - panel_left) * 0.20))
        reflow = np.full((height, width + reflow_shift, 3), 42, dtype=np.uint8)
        reflow[:, :split_x] = pixels[:, :split_x]
        reflow[:, split_x + reflow_shift:] = pixels[:, split_x:]
        reflow_labels = _transform_labels(
            entry["labels"],
            lambda box: [box[0] + reflow_shift, box[1], box[2] + reflow_shift, box[3]],
        )
        yield make_case("width-reflow", reflow, reflow_labels)

        for factor in (0.75, 1.50):
            altered = cv2.resize(
                pixels, None, fx=factor, fy=factor, interpolation=cv2.INTER_LINEAR
            )
            labels = _transform_labels(entry["labels"], lambda box, f=factor: _scaled_box(box, f))
            yield make_case(f"full-scale-{factor:g}", altered, labels)

        panel_left, panel_top, panel_right, panel_bottom = panel["box"]
        roi = pixels[panel_top:panel_bottom, panel_left:panel_right]
        roi_labels = _transform_labels(
            entry["labels"],
            lambda box: [box[0] - panel_left, box[1] - panel_top,
                         box[2] - panel_left, box[3] - panel_top],
        )
        for factor in SCALE_BANK:
            altered = cv2.resize(
                roi, None, fx=factor, fy=factor, interpolation=cv2.INTER_LINEAR
            )
            labels = _transform_labels(roi_labels, lambda box, f=factor: _scaled_box(box, f))
            yield make_case(
                f"panel-scale-{factor:g}",
                altered,
                labels,
                category="lightweight_numeric_roi",
            )

        spacer = np.full((roi.shape[0], 24, 3), 42, dtype=np.uint8)
        duplicate = np.concatenate((roi, spacer, roi), axis=1)
        invisible = {
            field: {"value": None, "visible": False, "box": None,
                    "verification": "not_visible"}
            for field in FIELDS
        }
        yield make_case(
            "duplicate-panel",
            duplicate,
            invisible,
            expected_discovery=False,
            classification="negative",
        )

        wrong_size = np.pad(pixels, ((0, 1), (0, 0), (0, 0)), constant_values=42)
        yield make_case(
            "cached-wrong-size",
            wrong_size,
            deepcopy(entry["labels"]),
            prior_frame=source_frame,
            sequence_kind="cached_invalidation",
            expected_cached_unknown_fields=tuple(FIELDS),
        )

        label_occluded = pixels.copy()
        value_edge = min(entry["labels"][field]["box"][0] for field in FIELDS)
        row_top = min(entry["labels"][field]["box"][1] for field in FIELDS) - 2
        row_bottom = max(entry["labels"][field]["box"][3] for field in FIELDS) + 2
        label_occluded[row_top:row_bottom, panel_left:value_edge] = 42
        yield make_case(
            "cached-label-occlusion",
            label_occluded,
            deepcopy(entry["labels"]),
            expected_discovery=False,
            prior_frame=source_frame,
            sequence_kind="cached_invalidation_then_rediscover",
            expected_cached_unknown_fields=tuple(FIELDS),
            expected_final_unknown_fields=tuple(FIELDS),
        )

        digit_occluded = pixels.copy()
        soul_box = entry["labels"]["resource_soul"]["box"]
        digit_occluded[soul_box[1]:soul_box[3], soul_box[0]:soul_box[2]] = 72
        yield make_case(
            "cached-digit-occlusion",
            digit_occluded,
            deepcopy(entry["labels"]),
            prior_frame=source_frame,
            sequence_kind="cached_partial_occlusion_then_rediscover",
            expected_cached_unknown_fields=("resource_soul",),
            expected_final_unknown_fields=("resource_soul",),
        )

        panel_width = panel_right - panel_left
        available_left = panel_left
        available_right = width - panel_right
        if available_left >= panel_width + 8:
            new_left = 4
        elif available_right >= panel_width + 8:
            new_left = width - panel_width - 4
        else:
            new_left = None
        if new_left is not None and new_left != panel_left:
            relocated = pixels.copy()
            relocated[panel_top:panel_bottom, panel_left:panel_right] = 42
            relocated[panel_top:panel_bottom, new_left:new_left + panel_width] = roi
            dx = new_left - panel_left
            relocated_labels = _transform_labels(
                entry["labels"],
                lambda box: [box[0] + dx, box[1], box[2] + dx, box[3]],
            )
            yield make_case(
                "cached-relocation",
                relocated,
                relocated_labels,
                prior_frame=source_frame,
                sequence_kind="cached_invalidate_then_rediscover",
                expected_cached_unknown_fields=tuple(FIELDS),
            )


def _box(value: Any, *, name: str, size: tuple[int, int]) -> tuple[int, int, int, int]:
    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(type(item) is not int for item in value)
    ):
        raise ManifestError(f"{name} must be four integer xyxy coordinates")
    left, top, right, bottom = value
    width, height = size
    if not (0 <= left < right <= width and 0 <= top < bottom <= height):
        raise ManifestError(f"{name} is outside {width}x{height}: {value!r}")
    return left, top, right, bottom


def load_manifest(path: Path = DEFAULT_MANIFEST) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Load and fully validate the manifest and every referenced image."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot load {path}: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("screenshots"), list):
        raise ManifestError("manifest must contain a top-level screenshots list")
    if document.get("box_format") != "xyxy-half-open":
        raise ManifestError("manifest box_format must be 'xyxy-half-open'")
    entries = document["screenshots"]
    if not entries:
        raise ManifestError("manifest screenshots list must not be empty")
    seen: set[str] = set()
    referenced_files: set[Path] = set()
    for index, entry in enumerate(entries):
        where = f"screenshots[{index}]"
        if not isinstance(entry, dict):
            raise ManifestError(f"{where} must be an object")
        for key in (
            "id",
            "file",
            "split",
            "classification",
            "source",
            "integrity",
            "client",
            "client_class",
            "expected_discovery",
            "coverage",
            "data_origin",
            "asset_fidelity",
            "skills_panel",
            "labels",
        ):
            if key not in entry:
                raise ManifestError(f"{where} is missing {key!r}")
        identifier = entry["id"]
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            raise ManifestError(f"{where}.id must be a unique non-empty string")
        seen.add(identifier)
        if entry["split"] not in SPLITS:
            raise ManifestError(f"{identifier}: invalid split {entry['split']!r}")
        if entry["classification"] not in CLASSIFICATIONS:
            raise ManifestError(f"{identifier}: invalid classification")
        if type(entry["expected_discovery"]) is not bool:
            raise ManifestError(f"{identifier}: expected_discovery must be boolean")
        expected_unknown = entry.get("expected_final_unknown_fields", [])
        if (
            not isinstance(expected_unknown, list)
            or any(not isinstance(field, str) for field in expected_unknown)
            or len(expected_unknown) != len(set(expected_unknown))
            or not set(expected_unknown).issubset(FIELDS)
        ):
            raise ManifestError(
                f"{identifier}: expected_final_unknown_fields must contain unique resources"
            )
        abstention_reason = entry.get("abstention_reason")
        ambiguity_evidence = entry.get("ambiguity_evidence")
        if expected_unknown:
            if set(expected_unknown) != set(FIELDS):
                raise ManifestError(
                    f"{identifier}: original ambiguity control must list all four resources"
                )
            if not isinstance(abstention_reason, str) or not abstention_reason.strip():
                raise ManifestError(
                    f"{identifier}: ambiguity control needs a non-empty abstention_reason"
                )
            if not isinstance(ambiguity_evidence, str) or not ambiguity_evidence.strip():
                raise ManifestError(
                    f"{identifier}: ambiguity control needs non-empty ambiguity_evidence"
                )
            if entry["expected_discovery"]:
                raise ManifestError(
                    f"{identifier}: ambiguity control must not expect automatic layout selection"
                )
        elif abstention_reason is not None or ambiguity_evidence is not None:
            raise ManifestError(
                f"{identifier}: abstention metadata requires expected_final_unknown_fields"
            )
        if entry["data_origin"] not in ORIGINS:
            raise ManifestError(f"{identifier}: invalid data_origin")
        if entry["coverage"] not in {"full_screenshot", "supplemental_crop"}:
            raise ManifestError(f"{identifier}: invalid coverage")
        if not isinstance(entry["client_class"], str) or not entry["client_class"]:
            raise ManifestError(f"{identifier}: invalid client_class")
        if entry["asset_fidelity"] not in {
            "source_original",
            "source_transcode",
            "derived_capture",
        }:
            raise ManifestError(f"{identifier}: invalid asset_fidelity")
        if expected_unknown:
            if (
                entry["data_origin"] != "real"
                or entry["coverage"] != "full_screenshot"
                or entry["classification"] != "similar"
                or entry["asset_fidelity"]
                not in {"source_original", "source_transcode"}
            ):
                raise ManifestError(
                    f"{identifier}: ambiguity control must be a real preserved "
                    "full screenshot classified similar"
                )
            equivalence = entry.get("render_equivalence")
            if (
                not isinstance(equivalence, dict)
                or equivalence.get("status") != "visually_equivalent"
                or not isinstance(equivalence.get("evidence"), str)
                or not equivalence["evidence"].strip()
            ):
                raise ManifestError(
                    f"{identifier}: ambiguity control needs reviewed visually-equivalent evidence"
                )
        client = entry["client"]
        if not entry["source"] or not isinstance(client, dict) or not client.get("evidence"):
            raise ManifestError(f"{identifier}: source and client_version_evidence are required")
        integrity = entry["integrity"]
        if not isinstance(integrity, dict):
            raise ManifestError(f"{identifier}: integrity must be an object")
        if set(integrity) != {"sha256", "width", "height", "format"}:
            raise ManifestError(f"{identifier}: invalid integrity shape")
        size = (integrity["width"], integrity["height"])
        if any(type(item) is not int or item <= 0 for item in size):
            raise ManifestError(f"{identifier}: width and height must be positive integers")
        image_path = path.parent / entry["file"]
        resolved_image_path = image_path.resolve()
        if resolved_image_path in referenced_files:
            raise ManifestError(f"{identifier}: image file is referenced more than once")
        referenced_files.add(resolved_image_path)
        if not image_path.is_file():
            raise ManifestError(f"{identifier}: missing file {image_path}")
        digest = sha256(image_path.read_bytes()).hexdigest()
        expected_digest = integrity["sha256"]
        if expected_digest != digest:
            raise ManifestError(f"{identifier}: sha256 mismatch")
        try:
            with Image.open(image_path) as image:
                actual_size = image.size
                image.verify()
        except OSError as exc:
            raise ManifestError(f"{identifier}: unreadable image: {exc}") from exc
        if actual_size != size:
            raise ManifestError(
                f"{identifier}: declared size {size} differs from image {actual_size}"
            )
        panel = entry["skills_panel"]
        if not isinstance(panel, dict) or "visible" not in panel or "box" not in panel:
            raise ManifestError(f"{identifier}: invalid skills_panel annotation")
        if type(panel["visible"]) is not bool:
            raise ManifestError(f"{identifier}: skills_panel.visible must be boolean")
        if panel["visible"]:
            _box(panel["box"], name=f"{identifier}.skills_panel.box", size=size)
        elif panel["box"] is not None:
            raise ManifestError(f"{identifier}: invisible skills_panel must have null box")
        labels = entry["labels"]
        if not isinstance(labels, dict) or set(labels) != set(FIELDS):
            raise ManifestError(f"{identifier}.labels must contain exactly the four resources")
        for field in FIELDS:
            label = labels[field]
            if not isinstance(label, dict):
                raise ManifestError(f"{identifier}.{field} must be an object")
            if set(label) != {"value", "visible", "box", "verification"}:
                raise ManifestError(f"{identifier}.{field} has an invalid annotation shape")
            if type(label["visible"]) is not bool or label["verification"] not in VERIFICATIONS:
                raise ManifestError(f"{identifier}.{field} has invalid visibility/verification")
            value = label["value"]
            if value is not None and (type(value) is not int or value < 0):
                raise ManifestError(f"{identifier}.{field}.value must be non-negative int or null")
            if label["visible"]:
                _box(label["box"], name=f"{identifier}.{field}.box", size=size)
                if label["verification"] == "not_visible":
                    raise ManifestError(
                        f"{identifier}.{field}: visible conflicts with verification"
                    )
            elif label["box"] is not None or label["verification"] != "not_visible":
                raise ManifestError(f"{identifier}.{field}: invisible annotation is inconsistent")
            if label["verification"] == "manual_verified" and value is None:
                raise ManifestError(f"{identifier}.{field}: verified value cannot be null")
        all_resources_verified = all(
            labels[field]["visible"]
            and labels[field]["verification"] == "manual_verified"
            and labels[field]["value"] is not None
            for field in FIELDS
        )
        if expected_unknown and (not panel["visible"] or not all_resources_verified):
            raise ManifestError(
                f"{identifier}: ambiguity control must retain a visible panel and four "
                "verified values for audit"
            )
        if (
            panel["visible"]
            and all_resources_verified
            and not entry["expected_discovery"]
            and not expected_unknown
        ):
            raise ManifestError(
                f"{identifier}: readable verified panel must set expected_discovery true"
            )
        if not panel["visible"] and entry["expected_discovery"]:
            raise ManifestError(f"{identifier}: hidden panel cannot require discovery")
        if entry["classification"] == "confirmed":
            for field in FIELDS:
                label = labels[field]
                if not (
                    label["visible"]
                    and label["verification"] == "manual_verified"
                    and label["value"] is not None
                ):
                    raise ManifestError(f"{identifier}: confirmed entry needs four verified values")
    originals = path.parent / "originals"
    if not originals.is_dir():
        raise ManifestError(f"corpus originals directory is missing: {originals}")
    image_suffixes = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
    corpus_images = {candidate.resolve() for candidate in originals.iterdir()
                     if candidate.is_file() and candidate.suffix.lower() in image_suffixes}
    unreferenced = corpus_images - referenced_files
    outside_corpus = referenced_files - corpus_images
    if unreferenced:
        names = ", ".join(sorted(candidate.name for candidate in unreferenced))
        raise ManifestError(f"unreferenced image(s) in corpus originals: {names}")
    if outside_corpus:
        names = ", ".join(sorted(str(candidate) for candidate in outside_corpus))
        raise ManifestError(f"manifest image(s) outside corpus originals: {names}")
    return document, entries


def _intersection_coverage(inner: Iterable[int], outer: Iterable[int]) -> float:
    il, it, ir, ib = inner
    ol, ot, oor, ob = outer
    area = (ir - il) * (ib - it)
    overlap = max(0, min(ir, oor) - max(il, ol)) * max(0, min(ib, ob) - max(it, ot))
    return overlap / area


def region_corresponds(
    field: str,
    region: Iterable[int],
    labels: dict[str, dict[str, Any]],
    *,
    minimum_own_coverage: float = 0.90,
) -> bool:
    """Require own-glyph containment without swallowing another row's center."""
    annotation = labels[field]
    if not annotation["visible"] or annotation["box"] is None:
        return False
    if _intersection_coverage(annotation["box"], region) < minimum_own_coverage:
        return False
    left, top, right, bottom = region
    for other, other_annotation in labels.items():
        if other == field or not other_annotation["visible"]:
            continue
        box = other_annotation["box"]
        if box is None:
            continue
        center_x = (box[0] + box[2]) / 2
        center_y = (box[1] + box[3]) / 2
        if left <= center_x < right and top <= center_y < bottom:
            return False
    return True


@dataclass
class ScreenshotResult:
    id: str
    split: str
    classification: str
    data_origin: str
    discovery_ms: float
    warm_read_ms: float | None
    layout_found: bool
    candidate_count: int
    detail: str
    fields_expected: int = 0
    fields_discovered: int = 0
    locations_correspond: int = 0
    numeric_exact: int = 0
    unknown: int = 0
    incorrect_known: int = 0
    association_failures: int = 0
    error: str | None = None
    field_results: dict[str, dict[str, Any]] | None = None
    coverage: str = ""
    client_class: str = ""
    expected_discovery: bool = False
    expected_final_unknown_fields: tuple[str, ...] = ()
    abstention_reason: str | None = None
    ambiguity_evidence: str | None = None
    intentional_unknown_targets: int = 0
    safe_intentional_unknown: int = 0
    unsafe_intentional_unknown_acceptances: int = 0


def benchmark_entry(
    reader: ResourceReader,
    entry: dict[str, Any],
    manifest_path: Path,
    *,
    use_ocr: bool,
    warm_reads: int,
) -> ScreenshotResult:
    path = manifest_path.parent / entry["file"]
    with Image.open(path) as image:
        frame = Frame(image.convert("RGB"), source=str(path))
    started = perf_counter()
    try:
        discovery = reader.discover(frame, use_ocr=use_ocr)
    except Exception as exc:  # Produce a row; never silently drop a corpus item.
        elapsed = (perf_counter() - started) * 1000
        row = ScreenshotResult(
            entry["id"],
            entry["split"],
            entry["classification"],
            entry["data_origin"],
            elapsed,
            None,
            False,
            0,
            "",
            error=f"{type(exc).__name__}: {exc}",
        )
        row.coverage = entry["coverage"]
        row.client_class = entry["client_class"]
        row.expected_discovery = entry["expected_discovery"]
        row.expected_final_unknown_fields = tuple(
            entry.get("expected_final_unknown_fields") or ()
        )
        row.abstention_reason = entry.get("abstention_reason")
        row.ambiguity_evidence = entry.get("ambiguity_evidence")
        return row
    elapsed = (perf_counter() - started) * 1000
    result = ScreenshotResult(
        entry["id"],
        entry["split"],
        entry["classification"],
        entry["data_origin"],
        elapsed,
        None,
        discovery.layout is not None,
        len(discovery.candidates),
        discovery.detail,
        field_results={},
    )
    result.coverage = entry["coverage"]
    result.client_class = entry["client_class"]
    result.expected_discovery = entry["expected_discovery"]
    result.expected_final_unknown_fields = tuple(
        entry.get("expected_final_unknown_fields") or ()
    )
    result.abstention_reason = entry.get("abstention_reason")
    result.ambiguity_evidence = entry.get("ambiguity_evidence")
    layout = discovery.layout
    readings = None
    if layout is not None:
        try:
            timings = []
            for _ in range(warm_reads):
                started = perf_counter()
                readings = reader.read(frame, layout, use_ocr=use_ocr)
                timings.append((perf_counter() - started) * 1000)
            result.warm_read_ms = median(timings)
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
    expected_unknown_fields = set(entry.get("expected_final_unknown_fields") or ())
    for field in FIELDS:
        annotation = entry["labels"][field]
        field_result = {
            "verification": annotation["verification"],
            "expected": annotation["value"],
            "discovered": bool(layout is not None and field in layout.regions),
            "location_corresponds": None,
            "quality": None,
            "actual": None,
            "numeric_matches": None,
            "exact": None,
            "expected_unknown": field in expected_unknown_fields,
        }
        result.field_results[field] = field_result
        # Negative screenshots carry numeric annotations for auditability, but
        # rejecting their incompatible UI is success rather than four unknown
        # target readings. Numeric accuracy metrics cover supported/similar UI.
        if annotation["verification"] != "manual_verified" or entry["classification"] == "negative":
            continue
        if field in expected_unknown_fields:
            result.intentional_unknown_targets += 1
            reading = (
                readings.get(field)
                if readings is not None and layout is not None
                else None
            )
            known = reading is not None and reading.quality is Quality.KNOWN
            field_result["quality"] = (
                reading.quality.value if reading is not None else Quality.UNKNOWN.value
            )
            if known:
                value = reading.value
                field_result["actual"] = (
                    value.get("count") if isinstance(value, Mapping) else value
                )
                result.unsafe_intentional_unknown_acceptances += 1
            else:
                result.safe_intentional_unknown += 1
            continue
        result.fields_expected += 1
        if layout is None or field not in layout.regions:
            result.unknown += 1
            continue
        result.fields_discovered += 1
        corresponds = region_corresponds(field, layout.regions[field], entry["labels"])
        field_result["location_corresponds"] = corresponds
        if corresponds:
            result.locations_correspond += 1
        if readings is None:
            result.unknown += 1
            continue
        reading = readings[field]
        field_result["quality"] = reading.quality.value
        if reading.quality is Quality.KNOWN:
            field_result["actual"] = reading.value["count"]
            field_result["numeric_matches"] = reading.value["count"] == annotation["value"]
            field_result["exact"] = field_result["numeric_matches"] and corresponds
            if field_result["exact"]:
                result.numeric_exact += 1
            elif field_result["numeric_matches"]:
                result.association_failures += 1
            else:
                result.incorrect_known += 1
        else:
            result.unknown += 1
    return result


def summarize(rows: list[ScreenshotResult], *, max_discovery_seconds: float) -> dict[str, Any]:
    groups: dict[str, dict[str, Any]] = {}
    grouped: dict[str, list[ScreenshotResult]] = defaultdict(list)
    for row in rows:
        grouped[f"{row.data_origin}/{row.split}"].append(row)
    for name, items in sorted(grouped.items()):
        expected = sum(item.fields_expected for item in items)
        groups[name] = {
            "screenshots": len(items),
            "layouts_found": sum(item.layout_found for item in items),
            "negative_screenshots": sum(item.classification == "negative" for item in items),
            "negative_rejections": sum(
                item.classification == "negative" and not item.layout_found for item in items
            ),
            "negative_false_positives": sum(
                item.classification == "negative" and item.layout_found for item in items
            ),
            "fields_expected": expected,
            "field_discovery_rate": (
                sum(item.fields_discovered for item in items) / expected if expected else None
            ),
            "location_correspondence_rate": (
                sum(item.locations_correspond for item in items) / expected if expected else None
            ),
            "numeric_exactness_rate": (
                sum(item.numeric_exact for item in items) / expected if expected else None
            ),
            "unknown": sum(item.unknown for item in items),
            "incorrect_known": sum(item.incorrect_known for item in items),
            "association_failures": sum(item.association_failures for item in items),
            "intentional_unknown_targets": sum(
                item.intentional_unknown_targets for item in items
            ),
            "safe_intentional_unknown": sum(
                item.safe_intentional_unknown for item in items
            ),
            "unsafe_intentional_unknown_acceptances": sum(
                item.unsafe_intentional_unknown_acceptances for item in items
            ),
            "discovery_ms_median": median(item.discovery_ms for item in items),
            "warm_read_ms_median": median(
                item.warm_read_ms for item in items if item.warm_read_ms is not None
            )
            if any(item.warm_read_ms is not None for item in items)
            else None,
        }
    violations = []
    for row in rows:
        if row.error:
            violations.append(f"{row.id}: {row.error}")
        if row.discovery_ms > max_discovery_seconds * 1000:
            violations.append(
                f"{row.id}: discovery {row.discovery_ms:.1f} ms exceeds runtime guard"
            )
        if row.classification == "negative" and row.layout_found:
            violations.append(f"{row.id}: false-positive layout on negative screenshot")
        if row.intentional_unknown_targets:
            if row.layout_found:
                violations.append(
                    f"{row.id}: ambiguity control auto-selected a unique layout"
                )
            if row.unsafe_intentional_unknown_acceptances:
                violations.append(
                    f"{row.id}: {row.unsafe_intentional_unknown_acceptances} "
                    "unsafe ambiguity-control value acceptance(s)"
                )
        if row.expected_discovery:
            if not row.layout_found:
                violations.append(f"{row.id}: expected panel was not discovered")
            if row.locations_correspond != row.fields_expected:
                violations.append(f"{row.id}: one or more regions miss annotated values")
            if row.numeric_exact != row.fields_expected:
                violations.append(f"{row.id}: one or more values were not exact")
        if row.incorrect_known:
            violations.append(f"{row.id}: {row.incorrect_known} incorrect known value(s)")
        if row.association_failures:
            violations.append(
                f"{row.id}: {row.association_failures} known value association failure(s)"
            )
    per_feature = {}
    for field in FIELDS:
        verified = [
            row.field_results[field]
            for row in rows
            if row.field_results is not None
            and row.classification != "negative"
            and row.field_results[field]["verification"] == "manual_verified"
            and not row.field_results[field].get("expected_unknown", False)
        ]
        per_feature[field] = {
            "expected": len(verified),
            "discovered": sum(item["discovered"] for item in verified),
            "locations_correspond": sum(item["location_corresponds"] is True for item in verified),
            "numeric_exact": sum(item["exact"] is True for item in verified),
            "unknown": sum(
                item["quality"] not in (None, Quality.KNOWN.value) or not item["discovered"]
                for item in verified
            ),
            "incorrect_known": sum(
                item["quality"] == Quality.KNOWN.value
                and item["numeric_matches"] is False
                for item in verified
            ),
            "association_failures": sum(
                item["quality"] == Quality.KNOWN.value
                and item["numeric_matches"] is True
                and item["location_corresponds"] is False
                for item in verified
            ),
        }
    return {
        "groups": groups,
        "per_feature": per_feature,
        "runtime_guard_seconds": max_discovery_seconds,
        "violations": violations,
        "passed": not violations,
    }


def evidence_summary(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Describe evidence breadth without treating crops as client builds."""
    positives = [
        entry
        for entry in entries
        if entry["classification"] != "negative"
        and not entry.get("expected_final_unknown_fields")
    ]
    ambiguity_controls = [
        entry for entry in entries if entry.get("expected_final_unknown_fields")
    ]
    return {
        "real_files": sum(entry["data_origin"] == "real" for entry in entries),
        "full_positive_screenshots": sum(
            entry["coverage"] == "full_screenshot" for entry in positives
        ),
        "supplemental_positive_crops": sum(
            entry["coverage"] == "supplemental_crop" for entry in positives
        ),
        "negative_full_screenshots": sum(
            entry["classification"] == "negative"
            and entry["coverage"] == "full_screenshot"
            for entry in entries
        ),
        "ambiguity_full_safety_controls": sum(
            entry["coverage"] == "full_screenshot" for entry in ambiguity_controls
        ),
        "exact_client_versions": sorted(
            {entry["client"]["version"] for entry in entries if entry["client"]["version"]}
        ),
        "full_positive_dimensions": sorted(
            {
                f"{entry['integrity']['width']}x{entry['integrity']['height']}"
                for entry in positives
                if entry["coverage"] == "full_screenshot"
            }
        ),
        "limit": (
            "Supplemental crops broaden font/layout evidence but do not establish "
            "additional full-client builds or exact versions."
        ),
    }


def _score_generated_read(
    case: BenchmarkCase,
    layout,
    readings,
) -> dict[str, Any]:
    fields_expected = 0
    fields_discovered = 0
    locations_correspond = 0
    numeric_exact = 0
    unknown = 0
    incorrect_known = 0
    association_failures = 0
    field_results: dict[str, dict[str, Any]] = {}
    for field in FIELDS:
        annotation = case.labels[field]
        if (
            annotation["verification"] != "manual_verified"
            or field in case.expected_final_unknown_fields
        ):
            continue
        fields_expected += 1
        item = {
            "expected": annotation["value"],
            "discovered": bool(layout is not None and field in layout.regions),
            "location_corresponds": False,
            "quality": None,
            "actual": None,
            "numeric_matches": False,
            "exact": False,
        }
        field_results[field] = item
        if layout is None or field not in layout.regions:
            unknown += 1
            continue
        fields_discovered += 1
        item["location_corresponds"] = region_corresponds(
            field, layout.regions[field], case.labels
        )
        locations_correspond += item["location_corresponds"]
        reading = readings[field]
        item["quality"] = reading.quality.value
        if reading.quality is Quality.KNOWN:
            item["actual"] = reading.value["count"]
            item["numeric_matches"] = reading.value["count"] == annotation["value"]
            item["exact"] = item["numeric_matches"] and item["location_corresponds"]
            if item["exact"]:
                numeric_exact += 1
            elif item["numeric_matches"]:
                association_failures += 1
            else:
                incorrect_known += 1
        else:
            unknown += 1
    return {
        "fields_expected": fields_expected,
        "fields_discovered": fields_discovered,
        "locations_correspond": locations_correspond,
        "numeric_exact": numeric_exact,
        "unknown": unknown,
        "incorrect_known": incorrect_known,
        "association_failures": association_failures,
        "field_results": field_results,
    }


def benchmark_generated_case(
    reader: ResourceReader,
    case: BenchmarkCase,
    *,
    use_ocr: bool,
) -> dict[str, Any]:
    """Run one generated case and keep safety failures distinct from misses."""
    row: dict[str, Any] = {
        "id": case.id,
        "source_id": case.source_id,
        "source_sha256": case.source_sha256,
        "case_sha256": case.case_sha256,
        "split": case.split,
        "classification": case.classification,
        "coverage": case.coverage,
        "category": case.category,
        "transform": case.transform,
        "generated": True,
        "eligible": case.eligible,
        "exclusion": case.exclusion,
        "frame_size": [case.frame.width, case.frame.height],
        "expected_discovery": case.expected_discovery,
        "sequence_kind": case.sequence_kind,
        "layout_found": False,
        "candidate_count": 0,
        "discovery_ms": 0.0,
        "fields_expected": 0,
        "fields_discovered": 0,
        "locations_correspond": 0,
        "numeric_exact": 0,
        "unknown": 0,
        "incorrect_known": 0,
        "association_failures": 0,
        "cached_unknown_expected": len(case.expected_cached_unknown_fields),
        "cached_unknown": 0,
        "cached_unsafe_known": 0,
        "final_unknown_expected": len(case.expected_final_unknown_fields),
        "final_unknown": 0,
        "final_unsafe_known": 0,
        "detail": "",
        "error": None,
    }
    if not case.eligible:
        return row
    try:
        if case.prior_frame is not None:
            started = perf_counter()
            prior = reader.discover(case.prior_frame, use_ocr=use_ocr)
            row["discovery_ms"] = (perf_counter() - started) * 1000
            if prior.layout is None:
                row["detail"] = f"prior layout unavailable: {prior.detail}"
                row["unknown"] = len(case.expected_cached_unknown_fields)
                return row
            cached = reader.read(case.frame, prior.layout, use_ocr=use_ocr)
            for field in case.expected_cached_unknown_fields:
                if cached[field].quality is Quality.UNKNOWN:
                    row["cached_unknown"] += 1
                else:
                    row["cached_unsafe_known"] += 1
            if (
                case.sequence_kind != "cached_invalidate_then_rediscover"
                and not case.expected_final_unknown_fields
            ):
                row["detail"] = "cached layout invalidation/occlusion check"
                return row

        started = perf_counter()
        discovery = reader.discover(case.frame, use_ocr=use_ocr)
        row["discovery_ms"] += (perf_counter() - started) * 1000
        row["layout_found"] = discovery.layout is not None
        row["candidate_count"] = len(discovery.candidates)
        row["detail"] = discovery.detail
        if not case.expected_discovery:
            if discovery.layout is None:
                row["final_unknown"] = len(case.expected_final_unknown_fields)
            else:
                final_readings = reader.read(case.frame, discovery.layout, use_ocr=use_ocr)
                for field in case.expected_final_unknown_fields:
                    if final_readings[field].quality is Quality.UNKNOWN:
                        row["final_unknown"] += 1
                    else:
                        row["final_unsafe_known"] += 1
            return row
        readings = (
            reader.read(case.frame, discovery.layout, use_ocr=use_ocr)
            if discovery.layout is not None
            else None
        )
        for field in case.expected_final_unknown_fields:
            if discovery.layout is None or readings[field].quality is Quality.UNKNOWN:
                row["final_unknown"] += 1
            else:
                row["final_unsafe_known"] += 1
        row.update(_score_generated_read(case, discovery.layout, readings))
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
    return row


def summarize_generated(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate coverage by feature while enforcing no fabricated known values."""
    by_transform: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_transform[row["transform"]].append(row)
    features: dict[str, dict[str, Any]] = {}
    violations: list[str] = []
    for transform, items in sorted(by_transform.items()):
        eligible = [item for item in items if item["eligible"]]
        expected = sum(item["fields_expected"] for item in eligible)
        exact = sum(item["numeric_exact"] for item in eligible)
        incorrect = sum(item["incorrect_known"] for item in eligible)
        association_failures = sum(item["association_failures"] for item in eligible)
        cached_expected = sum(item["cached_unknown_expected"] for item in eligible)
        cached_unknown = sum(item["cached_unknown"] for item in eligible)
        cached_unsafe = sum(item["cached_unsafe_known"] for item in eligible)
        final_expected = sum(item["final_unknown_expected"] for item in eligible)
        final_unknown = sum(item["final_unknown"] for item in eligible)
        final_unsafe = sum(item["final_unsafe_known"] for item in eligible)
        false_positives = sum(
            not item["expected_discovery"] and item["layout_found"] for item in eligible
        )
        errors = sum(item["error"] is not None for item in eligible)
        features[transform] = {
            "cases": len(items),
            "eligible": len(eligible),
            "excluded": len(items) - len(eligible),
            "source_files": len({item["source_id"] for item in eligible}),
            "full_screenshots": sum(item["coverage"] == "full_screenshot" for item in eligible),
            "supplemental_crops": sum(item["coverage"] == "supplemental_crop" for item in eligible),
            "dimensions": sorted({"x".join(map(str, item["frame_size"])) for item in eligible}),
            "layouts_found": sum(item["layout_found"] for item in eligible),
            "fields_expected": expected,
            "numeric_exact": exact,
            "unknown": sum(item["unknown"] for item in eligible),
            "incorrect_known": incorrect,
            "association_failures": association_failures,
            "cached_unknown_expected": cached_expected,
            "cached_unknown": cached_unknown,
            "cached_unsafe_known": cached_unsafe,
            "final_unknown_expected": final_expected,
            "final_unknown": final_unknown,
            "final_unsafe_known": final_unsafe,
            "negative_false_positives": false_positives,
            "errors": errors,
        }
        if incorrect:
            violations.append(f"{transform}: {incorrect} incorrect known generated value(s)")
        if association_failures:
            violations.append(
                f"{transform}: {association_failures} known generated association failure(s)"
            )
        if cached_unsafe:
            violations.append(f"{transform}: {cached_unsafe} unsafe cached known value(s)")
        if final_unsafe:
            violations.append(f"{transform}: {final_unsafe} unsafe final known value(s)")
        if false_positives:
            violations.append(f"{transform}: {false_positives} ambiguous/negative layout false positive(s)")
        if errors:
            violations.append(f"{transform}: {errors} generated case error(s)")
        if expected and exact == 0:
            violations.append(f"{transform}: zero exact recall across {expected} expected fields")
        if cached_expected and cached_unknown == 0:
            violations.append(
                f"{transform}: zero cached invalidations across {cached_expected} expected"
            )
        if final_expected and final_unknown == 0:
            violations.append(
                f"{transform}: zero safe final unknowns across {final_expected} expected"
            )
    return {
        "features": features,
        "eligible_cases": sum(item["eligible"] for item in rows),
        "excluded_cases": sum(not item["eligible"] for item in rows),
        "violations": violations,
        "passed": not violations,
    }


def synthetic_checks(reader: ResourceReader, *, use_ocr: bool = False) -> list[dict[str, Any]]:
    """Run deterministic robustness cases, reported separately from real evidence."""
    reference_path = ROOT / "tests/fixtures/resources/paulistinha-skills.png"
    with Image.open(reference_path) as image:
        pixels = np.asarray(image.convert("RGB"))
    expected = dict(zip(FIELDS, (710, 1085, 4, 192)))
    checks: list[dict[str, Any]] = []

    def discover_exact(name: str, altered: np.ndarray, expected_scale: float | None = None):
        started = perf_counter()
        result = reader.discover(Frame(altered), use_ocr=use_ocr)
        elapsed = (perf_counter() - started) * 1000
        ok = result.layout is not None
        detail = result.detail
        if result.layout is not None:
            readings = reader.read(Frame(altered), result.layout, use_ocr=use_ocr)
            actual = {
                field: (reading.value["count"] if reading.quality is Quality.KNOWN else None)
                for field, reading in readings.items()
            }
            ok = actual == expected
            detail = f"actual={actual}"
            if expected_scale is not None:
                ok = ok and abs(result.layout.scale - expected_scale) <= 0.16
        checks.append(
            {
                "id": name,
                "data_origin": "synthetic",
                "passed": ok,
                "discovery_ms": elapsed,
                "detail": detail,
            }
        )

    discover_exact("translated", np.pad(pixels, ((73, 19), (137, 23), (0, 0)), constant_values=42))
    for factor in (0.75, 1.5):
        resized = cv2.resize(pixels, None, fx=factor, fy=factor, interpolation=cv2.INTER_LINEAR)
        discover_exact(f"scale-{factor:g}", resized, factor)
    split, shift = 99, 74
    reflow = np.full((pixels.shape[0], pixels.shape[1] + shift, 3), 42, dtype=np.uint8)
    reflow[:, :split] = pixels[:, :split]
    reflow[:, split + shift :] = pixels[:, split:]
    discover_exact("width-reflow", reflow)

    def rejected(name: str, altered: np.ndarray, *, candidates: int | None = None):
        started = perf_counter()
        result = reader.discover(Frame(altered), use_ocr=use_ocr)
        elapsed = (perf_counter() - started) * 1000
        ok = result.layout is None and (candidates is None or len(result.candidates) == candidates)
        checks.append(
            {
                "id": name,
                "data_origin": "synthetic",
                "passed": ok,
                "discovery_ms": elapsed,
                "detail": f"layout={result.layout is not None}; "
                f"candidates={len(result.candidates)}; {result.detail}",
            }
        )

    duplicate = np.concatenate(
        (pixels, np.full((pixels.shape[0], 32, 3), 42, np.uint8), pixels), axis=1
    )
    rejected("duplicate-panel", duplicate, candidates=2)
    values = pixels[:, split:]
    adjacent = np.full(
        (pixels.shape[0], pixels.shape[1] + values.shape[1] + 18, 3), 42, dtype=np.uint8
    )
    adjacent[:, : pixels.shape[1]] = pixels
    adjacent[:, pixels.shape[1] + 18 :] = values
    rejected("adjacent-numeric-column", adjacent)
    with Image.open(ROOT / "profiles/paulistinha-skills-v1/digits/1.png") as image:
        glyph = np.asarray(image.convert("RGB"))
    decoy = pixels.copy()
    decoy[25 : 25 + glyph.shape[0], 70 : 70 + glyph.shape[1]] = glyph
    started = perf_counter()
    decoy_discovery = reader.discover(Frame(decoy), use_ocr=use_ocr)
    elapsed = (perf_counter() - started) * 1000
    safe = decoy_discovery.layout is None
    detail = decoy_discovery.detail
    if decoy_discovery.layout is not None:
        mana = reader.read(Frame(decoy), decoy_discovery.layout, use_ocr=use_ocr)["resource_mana"]
        safe = mana.quality is not Quality.KNOWN
        detail = f"mana={mana.quality.value}/{mana.value}"
    checks.append(
        {
            "id": "isolated-decoy-digit",
            "data_origin": "synthetic",
            "passed": safe,
            "discovery_ms": elapsed,
            "detail": detail,
        }
    )

    base_frame = Frame(pixels)
    base = reader.discover(base_frame, use_ocr=use_ocr)
    if base.layout is None:
        checks.append(
            {
                "id": "stale-layout-guards",
                "data_origin": "synthetic",
                "passed": False,
                "discovery_ms": 0.0,
                "detail": "reference layout unavailable",
            }
        )
        return checks
    dissent = np.full((pixels.shape[0], 260, 3), 42, dtype=np.uint8)
    dissent[:, : pixels.shape[1]] = pixels
    mana_anchor = next(item for item in base.layout.anchors if item.field == "resource_mana")
    mana_top = max(0, mana_anchor.box[1] - 3)
    mana_bottom = min(dissent.shape[0], mana_anchor.box[3] + 3)
    dissent[mana_top:mana_bottom, mana_anchor.box[2] + 1 :] = 72
    x = 158
    for _ in range(7):
        dissent[mana_top + 2 : mana_top + 2 + glyph.shape[0], x : x + glyph.shape[1]] = glyph
        x += glyph.shape[1] + 2
    started = perf_counter()
    dissent_discovery = reader.discover(Frame(dissent), use_ocr=use_ocr)
    elapsed = (perf_counter() - started) * 1000
    safe = dissent_discovery.layout is None
    detail = dissent_discovery.detail
    if dissent_discovery.layout is not None:
        mana = reader.read(Frame(dissent), dissent_discovery.layout, use_ocr=use_ocr)[
            "resource_mana"
        ]
        safe = mana.quality is not Quality.KNOWN
        detail = f"mana={mana.quality.value}/{mana.value}"
    checks.append(
        {
            "id": "dissenting-value-column",
            "data_origin": "synthetic",
            "passed": safe,
            "discovery_ms": elapsed,
            "detail": detail,
        }
    )
    wrong_size = Frame(np.pad(pixels, ((0, 1), (0, 0), (0, 0)), constant_values=42))
    readings = reader.read(wrong_size, base.layout, use_ocr=use_ocr)
    checks.append(
        {
            "id": "wrong-size-capture",
            "data_origin": "synthetic",
            "passed": all(item.quality is Quality.UNKNOWN for item in readings.values()),
            "discovery_ms": 0.0,
            "detail": "cached read guard",
        }
    )
    shifted_values = pixels.copy()
    shifted_values[:, 99:] = 42
    shifted_values[:, 130:182] = pixels[:, 140:192]
    readings = reader.read(Frame(shifted_values), base.layout, use_ocr=use_ocr)
    checks.append(
        {
            "id": "same-size-value-column-move",
            "data_origin": "synthetic",
            "passed": all(item.quality is Quality.UNKNOWN for item in readings.values()),
            "discovery_ms": 0.0,
            "detail": "cached column-geometry guard",
        }
    )
    anchor = next(item for item in base.layout.anchors if item.field == "resource_soul")
    covered = pixels.copy()
    left, top, right, bottom = anchor.box
    covered[top:bottom, left:right] = 42
    readings = reader.read(Frame(covered), base.layout, use_ocr=use_ocr)
    checks.append(
        {
            "id": "occluded-label",
            "data_origin": "synthetic",
            "passed": all(item.quality is Quality.UNKNOWN for item in readings.values()),
            "discovery_ms": 0.0,
            "detail": "cached read guard",
        }
    )
    covered_digits = pixels.copy()
    left, top, right, bottom = base.layout.regions["resource_soul"]
    covered_digits[top:bottom, left:right] = 72
    readings = reader.read(Frame(covered_digits), base.layout, use_ocr=use_ocr)
    soul = readings["resource_soul"]
    checks.append(
        {
            "id": "occluded-digits",
            "data_origin": "synthetic",
            "passed": soul.quality is Quality.UNKNOWN and soul.value is None,
            "discovery_ms": 0.0,
            "detail": f"soul={soul.quality.value}/{soul.value}",
        }
    )
    edge_pixels = pixels.copy()
    edge_pixels[58:75, 99:] = 72
    edge_frame = Frame(edge_pixels[:75])
    started = perf_counter()
    edge = reader.discover(edge_frame, use_ocr=use_ocr)
    elapsed = (perf_counter() - started) * 1000
    safe = edge.layout is not None
    detail = edge.detail
    if edge.layout is not None:
        boxes = (edge.layout.bounds, *edge.layout.regions.values())
        safe = all(
            0 <= box[0] < box[2] <= edge_frame.width
            and 0 <= box[1] < box[3] <= edge_frame.height
            for box in boxes
        )
        edge_readings = reader.read(edge_frame, edge.layout, use_ocr=use_ocr)
        safe = safe and all(
            edge_readings[field].quality is Quality.KNOWN
            and edge_readings[field].value["count"] == expected[field]
            for field in FIELDS[:3]
        )
        safe = safe and edge_readings["resource_capacity"].quality is Quality.UNKNOWN
        detail = (
            f"bounds={edge.layout.bounds}; "
            f"capacity={edge_readings['resource_capacity'].quality.value}"
        )
    checks.append(
        {
            "id": "bottom-edge-blank-row",
            "data_origin": "synthetic",
            "passed": safe,
            "discovery_ms": elapsed,
            "detail": detail,
        }
    )
    clip_at = max(region[2] for region in base.layout.regions.values()) - 4
    rejected("clipped-value-column", pixels[:, :clip_at])
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--ocr", action="store_true", help="explicitly enable OCR fallbacks")
    parser.add_argument("--warm-reads", type=int, default=5)
    parser.add_argument("--max-discovery-seconds", type=float, default=15.0)
    parser.add_argument(
        "--skip-generated",
        action="store_true",
        help="run only preserved originals and the small reference-edge checks",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args(argv)
    if args.warm_reads < 1 or args.max_discovery_seconds <= 0:
        parser.error("warm reads and runtime guard must be positive")
    _, entries = load_manifest(args.manifest)
    reader = ResourceReader()
    rows = [
        benchmark_entry(reader, entry, args.manifest, use_ocr=args.ocr, warm_reads=args.warm_reads)
        for entry in entries
    ]
    generated_rows = [] if args.skip_generated else [
        benchmark_generated_case(reader, case, use_ocr=args.ocr)
        for case in generated_benchmark_cases(entries, args.manifest)
    ]
    perturbations = synthetic_checks(reader, use_ocr=args.ocr)
    summary = summarize(rows, max_discovery_seconds=args.max_discovery_seconds)
    generated_summary = summarize_generated(generated_rows)
    failed_perturbations = [item for item in perturbations if not item["passed"]]
    summary["groups"]["synthetic/perturbations"] = {
        "cases": len(perturbations),
        "passed": len(perturbations) - len(failed_perturbations),
    }
    summary["violations"].extend(
        f"{item['id']}: synthetic perturbation failed ({item['detail']})"
        for item in failed_perturbations
    )
    summary["violations"].extend(
        f"generated/{violation}" for violation in generated_summary["violations"]
    )
    summary["passed"] = not summary["violations"]
    report = {
        "manifest": str(args.manifest),
        "ocr": args.ocr,
        "evidence": evidence_summary(entries),
        "screenshots": [asdict(row) for row in rows],
        "generated_cases": generated_rows,
        "generated_summary": generated_summary,
        "perturbations": perturbations,
        "summary": summary,
    }
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"resource corpus: {len(rows)} screenshots; OCR={'on' if args.ocr else 'off'}")
        print(f"evidence: {json.dumps(report['evidence'], sort_keys=True)}")
        for row in rows:
            print(
                f"{row.id}: layout={row.layout_found} candidates={row.candidate_count} "
                f"discover={row.discovery_ms:.1f}ms warm={row.warm_read_ms}ms "
                f"located={row.locations_correspond}/{row.fields_expected} "
                f"exact={row.numeric_exact}/{row.fields_expected} unknown={row.unknown} "
                f"incorrect_known={row.incorrect_known} "
                f"association_failures={row.association_failures}"
                + (f" error={row.error}" if row.error else "")
            )
            if row.expected_discovery and row.field_results:
                for field, item in row.field_results.items():
                    problems = []
                    if not item["discovered"]:
                        problems.append("region-missing")
                    elif item["location_corresponds"] is not True:
                        problems.append("annotation-miss")
                    if item["quality"] != Quality.KNOWN.value:
                        problems.append(item["quality"] or "unread")
                    elif item["exact"] is not True:
                        problems.append(
                            f"expected-{item['expected']}-got-{item['actual']}"
                        )
                    if problems:
                        print(f"  {field}: {','.join(problems)}")
        for name, group in report["summary"]["groups"].items():
            print(f"{name}: {json.dumps(group, sort_keys=True)}")
        for field, metrics in report["summary"]["per_feature"].items():
            print(f"{field}: {json.dumps(metrics, sort_keys=True)}")
        for name, metrics in generated_summary["features"].items():
            print(f"generated/{name}: {json.dumps(metrics, sort_keys=True)}")
        for item in perturbations:
            print(
                f"synthetic/{item['id']}: passed={item['passed']} "
                f"discover={item['discovery_ms']:.1f}ms {item['detail']}"
            )
        for violation in report["summary"]["violations"]:
            print(f"FAIL: {violation}")
    return 0 if report["summary"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
