#!/usr/bin/env python3
"""Compare legacy and adaptive resource readers on one immutable case set.

The manifest owns ground truth and the corpus module owns generated image
transformations.  Reader adapters receive pixels and options only; expected
values and annotated regions are used after inference, exclusively for scoring.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass, field, fields as dataclass_fields
from hashlib import sha256
import importlib.metadata
import json
import platform
from pathlib import Path
import shutil
import subprocess
import sys
from time import perf_counter
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

import cv2
import numpy as np
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.perception import Frame  # noqa: E402
from tibia_client.adaptive import FIELDS, ResourceReader  # noqa: E402
from tibia_client.adaptive_runtime import bounded_adaptive_count  # noqa: E402
from tibia_client.models import Quality, Reading  # noqa: E402
from tibia_client.resource_profiles import (  # noqa: E402
    ResourceProfileError,
    apply_resource_preset,
)
from tools import benchmark_resources as corpus  # noqa: E402


SCHEMA_VERSION = 1
CATEGORIES = (
    "original_full",
    "supplemental_crop",
    "generated_perturbation",
    "lightweight_numeric_roi",
)
with (ROOT / "profiles/paulistinha-skills-v1/profile.json").open(encoding="utf-8") as _profile:
    _RESOURCE_MAX_DIGITS = {
        name: int(spec["detector"]["max_digits"])
        for name, spec in json.load(_profile)["resources"].items()
    }


@dataclass(frozen=True)
class BenchmarkCase:
    """One image and its ground truth, shared unchanged by every technique."""

    id: str
    source_id: str
    source_sha256: str
    case_sha256: str
    category: str
    split: str
    classification: str
    frame: Frame = field(repr=False, compare=False)
    labels: Mapping[str, Mapping[str, Any]] = field(repr=False, compare=False)
    expected_discovery: bool = True
    transform: str = "original"
    eligible: bool = True
    exclusion: str | None = None
    prior_frame: Frame | None = field(default=None, repr=False, compare=False)
    sequence_kind: str | None = None
    expected_cached_unknown_fields: tuple[str, ...] = ()
    expected_final_unknown_fields: tuple[str, ...] = ()
    source_coverage: str | None = None
    data_origin: str | None = None
    asset_fidelity: str | None = None
    render_equivalence_status: str = "unknown"
    render_equivalence_evidence: str = ""
    render_equivalence_scope: str = ""
    render_equivalence_reviewed_at: str = ""
    abstention_reason: str | None = None
    ambiguity_evidence: str | None = None

    def __post_init__(self) -> None:
        if not self.id or not self.source_id:
            raise ValueError("benchmark case IDs must be non-empty")
        if self.category not in CATEGORIES:
            raise ValueError(f"unsupported benchmark category {self.category!r}")
        if not isinstance(self.frame, Frame):
            raise TypeError("benchmark case frame must be a Frame")
        if self.prior_frame is not None and not isinstance(self.prior_frame, Frame):
            raise TypeError("benchmark prior frame must be a Frame")
        if set(self.labels) != set(FIELDS):
            raise ValueError("benchmark case labels must contain every resource field")
        if self.source_coverage not in {None, "full_screenshot", "supplemental_crop"}:
            raise ValueError(f"unsupported source coverage {self.source_coverage!r}")
        if self.render_equivalence_status not in {
            "target_family",
            "visually_equivalent",
            "visibly_different",
            "unknown",
        }:
            raise ValueError(
                f"unsupported render equivalence {self.render_equivalence_status!r}"
            )


@dataclass(frozen=True, slots=True)
class InferenceCase:
    """Pixels and sequence context exposed to a technique, without ground truth."""

    frame: Frame
    prior_frame: Frame | None = None


@dataclass(frozen=True)
class TechniqueObservation:
    """Technique output before any comparison with ground truth."""

    panel_status: str
    values: Mapping[str, int | None]
    regions: Mapping[str, Sequence[int]] = field(default_factory=dict)
    qualities: Mapping[str, str] = field(default_factory=dict)
    cold_total_ms: float | None = None
    discovery_ms: float | None = None
    cached_ms: float | None = None
    recovery_ms: float | None = None
    cached_unknown_fields: tuple[str, ...] = ()
    detail: str = ""
    field_details: Mapping[str, str] = field(default_factory=dict)
    field_diagnostics: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.panel_status not in {"found", "missing", "ambiguous", "error"}:
            raise ValueError(f"invalid panel status {self.panel_status!r}")
        extra = set(self.values) - set(FIELDS)
        if extra:
            raise ValueError(f"unknown resource fields: {sorted(extra)!r}")
        extra_details = set(self.field_details) - set(FIELDS)
        extra_diagnostics = set(self.field_diagnostics) - set(FIELDS)
        if extra_details or extra_diagnostics:
            raise ValueError(
                "unknown resource fields in diagnostics: "
                f"{sorted(extra_details | extra_diagnostics)!r}"
            )
        if self.panel_status != "found" and (self.values or self.regions):
            raise ValueError(
                "non-found panel observations cannot expose values or regions"
            )


class Technique(Protocol):
    id: str
    options: Mapping[str, Any]
    supports_cached_layout: bool

    def evaluate(self, case: InferenceCase, *, warm_reads: int) -> TechniqueObservation:
        ...


def _reading_value(reading: Reading | None) -> int | None:
    if reading is None or reading.quality is not Quality.KNOWN:
        return None
    value = reading.value
    if isinstance(value, Mapping):
        value = value.get("count")
    return value if type(value) is int and value >= 0 else None


def _qualities(readings: Mapping[str, Reading]) -> dict[str, str]:
    return {
        name: readings[name].quality.value if name in readings else Quality.UNKNOWN.value
        for name in FIELDS
    }


def _reading_details(readings: Mapping[str, Reading]) -> dict[str, str]:
    return {
        name: readings[name].detail
        for name in FIELDS
        if name in readings and readings[name].detail
    }


def _reading_diagnostics(
    readings: Mapping[str, Reading],
) -> dict[str, Mapping[str, Any]]:
    return {
        name: readings[name].diagnostics
        for name in FIELDS
        if name in readings and readings[name].diagnostics
    }


class LegacyTechnique:
    """The shipped exact-scale PR4 preset, invoked through its public API."""

    id = "legacy_exact_scale_pr4"
    supports_cached_layout = False
    options: Mapping[str, Any] = {
        "preset_api": "apply_resource_preset",
        "ocr": False,
        "supports_cached_layout": False,
    }

    def evaluate(self, case: InferenceCase, *, warm_reads: int) -> TechniqueObservation:
        del warm_reads  # The legacy preset has no cached tracking API.
        started = perf_counter()
        try:
            result = apply_resource_preset(
                {}, case.frame, calibration_size=case.frame.size, base_dir=ROOT
            )
        except ResourceProfileError as exc:
            elapsed = (perf_counter() - started) * 1000
            detail = str(exc)
            status = "ambiguous" if "more than once" in detail else "missing"
            return TechniqueObservation(status, {}, cold_total_ms=elapsed, detail=detail)
        except Exception as exc:  # A benchmark row must never disappear on reader failure.
            elapsed = (perf_counter() - started) * 1000
            return TechniqueObservation(
                "error", {}, cold_total_ms=elapsed,
                detail=f"{type(exc).__name__}: {exc}",
            )
        elapsed = (perf_counter() - started) * 1000
        readings = dict(result.readings)
        return TechniqueObservation(
            "found",
            {name: _reading_value(readings.get(name)) for name in FIELDS},
            {name: tuple(binding.region) for name, binding in result.bindings.items()},
            _qualities(readings),
            cold_total_ms=elapsed,
            field_details=_reading_details(readings),
            field_diagnostics=_reading_diagnostics(readings),
        )


class AdaptiveTechnique:
    id: str

    def __init__(
        self,
        *,
        use_ocr: bool | None = None,
        label_ocr: bool | None = None,
        digit_ocr: bool | None = None,
    ) -> None:
        if use_ocr is not None:
            if label_ocr is not None or digit_ocr is not None:
                raise ValueError("use_ocr cannot be combined with split OCR options")
            label_ocr = digit_ocr = use_ocr
        self.label_ocr = bool(label_ocr)
        self.digit_ocr = bool(digit_ocr)
        self.use_ocr = self.label_ocr and self.digit_ocr
        self.supports_cached_layout = True
        if self.label_ocr and self.digit_ocr:
            self.id = "adaptive_templates_ocr"
        elif self.label_ocr:
            self.id = "adaptive_labels_ocr"
        elif self.digit_ocr:
            self.id = "adaptive_digits_ocr"
        else:
            self.id = "adaptive_templates"
        self.options = {
            "reader": "ResourceReader",
            "ocr": self.use_ocr,
            "label_ocr": self.label_ocr,
            "digit_ocr": self.digit_ocr,
            "selection": None,
            "supports_cached_layout": True,
            "max_digits": dict(_RESOURCE_MAX_DIGITS),
        }

    @staticmethod
    def _bounded(readings: Mapping[str, Reading]) -> dict[str, Reading]:
        return {
            name: bounded_adaptive_count(readings[name], _RESOURCE_MAX_DIGITS[name])
            for name in FIELDS
            if name in readings
        }

    def evaluate(self, case: InferenceCase, *, warm_reads: int) -> TechniqueObservation:
        # A fresh reader/layout per case prevents case-order leakage.  Discovery
        # always searches the complete frame; annotated ROIs are never passed in.
        cold_started = perf_counter()
        reader = ResourceReader()
        if case.prior_frame is not None:
            return self._evaluate_sequence(reader, case, cold_started=cold_started)
        started = perf_counter()
        try:
            discovery = reader.discover(case.frame, use_ocr=self.label_ocr)
        except Exception as exc:
            elapsed = (perf_counter() - started) * 1000
            return TechniqueObservation(
                "error", {}, cold_total_ms=(perf_counter() - cold_started) * 1000,
                discovery_ms=elapsed,
                detail=f"{type(exc).__name__}: {exc}",
            )
        discovery_ms = (perf_counter() - started) * 1000
        if discovery.layout is None:
            status = "ambiguous" if discovery.candidates else "missing"
            return TechniqueObservation(
                status, {}, cold_total_ms=(perf_counter() - cold_started) * 1000,
                discovery_ms=discovery_ms, detail=discovery.detail
            )

        timings: list[float] = []
        readings: Mapping[str, Reading] = {}
        cold_total_ms = None
        for _ in range(max(1, warm_reads)):
            started = perf_counter()
            readings = self._bounded(
                reader.read(case.frame, discovery.layout, use_ocr=self.digit_ocr)
            )
            timings.append((perf_counter() - started) * 1000)
            if cold_total_ms is None:
                cold_total_ms = (perf_counter() - cold_started) * 1000
        cached_ms = _median(timings)
        return TechniqueObservation(
            "found",
            {name: _reading_value(readings.get(name)) for name in FIELDS},
            dict(discovery.layout.regions),
            _qualities(readings),
            cold_total_ms=cold_total_ms,
            discovery_ms=discovery_ms,
            cached_ms=cached_ms,
            detail=discovery.detail,
            field_details=_reading_details(readings),
            field_diagnostics=_reading_diagnostics(readings),
        )

    def _evaluate_sequence(
        self, reader: ResourceReader, case: InferenceCase, *, cold_started: float
    ) -> TechniqueObservation:
        """Measure cached invalidation and synchronous recovery on a frame pair."""
        started = perf_counter()
        try:
            prior = reader.discover(case.prior_frame, use_ocr=self.label_ocr)
        except Exception as exc:
            elapsed = (perf_counter() - started) * 1000
            return TechniqueObservation(
                "error", {}, cold_total_ms=(perf_counter() - cold_started) * 1000,
                discovery_ms=elapsed,
                detail=f"prior discovery failed: {type(exc).__name__}: {exc}",
            )
        discovery_ms = (perf_counter() - started) * 1000
        if prior.layout is None:
            status = "ambiguous" if prior.candidates else "missing"
            return TechniqueObservation(
                status, {}, cold_total_ms=(perf_counter() - cold_started) * 1000,
                discovery_ms=discovery_ms,
                detail=f"prior frame: {prior.detail}",
            )

        # Complete the comparable cold path on the prior frame before timing
        # stale-layout invalidation and recovery as separate phases.
        reader.read(case.prior_frame, prior.layout, use_ocr=self.digit_ocr)
        cold_total_ms = (perf_counter() - cold_started) * 1000

        started = perf_counter()
        stale = reader.read(case.frame, prior.layout, use_ocr=self.digit_ocr)
        cached_ms = (perf_counter() - started) * 1000
        cached_unknown = tuple(
            name for name in FIELDS
            if name not in stale or stale[name].quality is not Quality.KNOWN
        )

        started = perf_counter()
        try:
            recovered = reader.discover(case.frame, use_ocr=self.label_ocr)
            readings = (
                self._bounded(reader.read(
                    case.frame, recovered.layout, use_ocr=self.digit_ocr
                ))
                if recovered.layout is not None
                else {}
            )
        except Exception as exc:
            recovery_ms = (perf_counter() - started) * 1000
            return TechniqueObservation(
                "error", {}, discovery_ms=discovery_ms, cached_ms=cached_ms,
                cold_total_ms=cold_total_ms,
                recovery_ms=recovery_ms, cached_unknown_fields=cached_unknown,
                detail=f"recovery failed: {type(exc).__name__}: {exc}",
            )
        recovery_ms = (perf_counter() - started) * 1000
        if recovered.layout is None:
            status = "ambiguous" if recovered.candidates else "missing"
            return TechniqueObservation(
                status, {}, discovery_ms=discovery_ms, cached_ms=cached_ms,
                cold_total_ms=cold_total_ms,
                recovery_ms=recovery_ms, cached_unknown_fields=cached_unknown,
                detail=f"recovery: {recovered.detail}",
            )
        return TechniqueObservation(
            "found",
            {name: _reading_value(readings.get(name)) for name in FIELDS},
            dict(recovered.layout.regions),
            _qualities(readings),
            cold_total_ms=cold_total_ms,
            discovery_ms=discovery_ms,
            cached_ms=cached_ms,
            recovery_ms=recovery_ms,
            cached_unknown_fields=cached_unknown,
            detail="generated frame sequence",
            field_details=_reading_details(readings),
            field_diagnostics=_reading_diagnostics(readings),
        )


def _image_hash(frame: Frame) -> str:
    return sha256(frame.pixels.tobytes()).hexdigest()


def _original_case(entry: Mapping[str, Any], manifest_path: Path) -> BenchmarkCase:
    image_path = manifest_path.parent / entry["file"]
    # load_manifest has already checked existence, dimensions, and source hash.
    with Image.open(image_path) as image:
        frame = Frame(image.convert("RGB"), source=str(image_path))
    category = (
        "original_full" if entry["coverage"] == "full_screenshot" else "supplemental_crop"
    )
    return BenchmarkCase(
        id=entry["id"],
        source_id=entry["id"],
        source_sha256=entry["integrity"]["sha256"],
        case_sha256=_image_hash(frame),
        category=category,
        split=entry["split"],
        classification=entry["classification"],
        frame=frame,
        labels=entry["labels"],
        expected_discovery=entry["expected_discovery"],
        expected_final_unknown_fields=tuple(
            entry.get("expected_final_unknown_fields") or ()
        ),
        source_coverage=entry["coverage"],
        data_origin=entry.get("data_origin"),
        asset_fidelity=entry.get("asset_fidelity"),
        render_equivalence_status=entry.get("render_equivalence", {}).get(
            "status", "unknown"
        ),
        render_equivalence_evidence=entry.get("render_equivalence", {}).get(
            "evidence", ""
        ),
        render_equivalence_scope=entry.get("render_equivalence", {}).get("scope", ""),
        render_equivalence_reviewed_at=entry.get("render_equivalence", {}).get(
            "reviewed_at", ""
        ),
        abstention_reason=entry.get("abstention_reason"),
        ambiguity_evidence=entry.get("ambiguity_evidence"),
    )


def _coerce_generated_case(
    raw: Any, source: BenchmarkCase | None = None
) -> BenchmarkCase:
    """Accept the corpus module's public dataclass or mapping representation."""
    value = (
        {item.name: getattr(raw, item.name) for item in dataclass_fields(raw)}
        if hasattr(raw, "__dataclass_fields__")
        else dict(raw)
    )
    frame = value["frame"]
    if not isinstance(frame, Frame):
        frame = Frame(frame, source=f"generated:{value['id']}")
    return BenchmarkCase(
        id=value["id"],
        source_id=value["source_id"],
        source_sha256=value["source_sha256"],
        case_sha256=value.get("case_sha256") or _image_hash(frame),
        category=value.get("category", "generated_perturbation"),
        split=value["split"],
        classification=value.get("classification", "similar"),
        frame=frame,
        labels=value["labels"],
        expected_discovery=value.get("expected_discovery", True),
        transform=value.get("transform", value["id"].split("::")[-1]),
        eligible=value.get("eligible", True),
        exclusion=value.get("exclusion"),
        prior_frame=value.get("prior_frame"),
        sequence_kind=value.get("sequence_kind"),
        expected_cached_unknown_fields=tuple(
            value.get("expected_cached_unknown_fields") or ()
        ),
        expected_final_unknown_fields=tuple(
            value.get("expected_final_unknown_fields") or ()
        ),
        source_coverage=value.get("coverage") or None,
        data_origin=value.get("data_origin") or (source.data_origin if source else None),
        asset_fidelity=value.get("asset_fidelity")
        or (source.asset_fidelity if source else None),
        render_equivalence_status=value.get("render_equivalence_status")
        or (source.render_equivalence_status if source else "unknown"),
        render_equivalence_evidence=value.get("render_equivalence_evidence")
        or (source.render_equivalence_evidence if source else ""),
        render_equivalence_scope=value.get("render_equivalence_scope")
        or (source.render_equivalence_scope if source else ""),
        render_equivalence_reviewed_at=value.get("render_equivalence_reviewed_at")
        or (source.render_equivalence_reviewed_at if source else ""),
        abstention_reason=value.get("abstention_reason")
        or (source.abstention_reason if source else None),
        ambiguity_evidence=value.get("ambiguity_evidence")
        or (source.ambiguity_evidence if source else None),
    )


def load_cases(
    manifest_path: Path,
    *,
    include_generated: bool = False,
) -> tuple[dict[str, Any], list[BenchmarkCase]]:
    """Validate the corpus and materialize one ordered, duplicate-free case set."""
    document, entries = corpus.load_manifest(manifest_path)
    cases = [_original_case(entry, manifest_path) for entry in entries]
    if include_generated:
        factory = getattr(corpus, "generated_benchmark_cases", None)
        if factory is None:
            raise RuntimeError(
                "generated cases requested but tools.benchmark_resources does not expose "
                "generated_benchmark_cases(entries, manifest_path)"
            )
        source_cases = {case.source_id: case for case in cases}
        for item in factory(entries, manifest_path):
            source_id = (
                item.source_id if hasattr(item, "source_id") else item["source_id"]
            )
            cases.append(_coerce_generated_case(item, source_cases.get(source_id)))
    ids = [case.id for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("benchmark case IDs must be globally unique")
    _validate_lineage(cases)
    return document, cases


def _validate_lineage(cases: Sequence[BenchmarkCase]) -> dict[str, Any]:
    """Verify derived cases retain their source hash and historical split."""
    sources = {
        case.id: case
        for case in cases
        if case.id == case.source_id and case.transform == "original"
    }
    derived = [case for case in cases if case.id != case.source_id]
    sha_splits: dict[str, set[str]] = defaultdict(set)
    for source in sources.values():
        sha_splits[source.source_sha256].add(source.split)
    conflicting_hashes = [
        digest for digest, splits in sha_splits.items() if len(splits) > 1
    ]
    if conflicting_hashes:
        raise ValueError(
            "identical source sha256 is assigned to both train and heldout splits"
        )
    heldout_derived = 0
    for case in derived:
        if not case.id.startswith(f"{case.source_id}::"):
            raise ValueError(f"{case.id}: generated case ID does not preserve source ID")
        source = sources.get(case.source_id)
        if source is None:
            raise ValueError(f"{case.id}: generated case has no source case")
        if case.source_sha256 != source.source_sha256:
            raise ValueError(f"{case.id}: generated case source sha256 changed")
        if case.split != source.split:
            raise ValueError(f"{case.id}: generated case changed source split")
        if (
            case.source_coverage is not None
            and case.source_coverage != source.source_coverage
        ):
            raise ValueError(f"{case.id}: generated case changed source coverage")
        heldout_derived += case.split == "heldout"
    return {
        "validated": True,
        "source_cases": len(sources),
        "derived_cases": len(derived),
        "heldout_source_cases": sum(case.split == "heldout" for case in sources.values()),
        "heldout_derived_cases": heldout_derived,
        "checks": [
            "derived_id_prefix",
            "source_exists",
            "source_sha256",
            "source_split",
            "source_coverage_when_available",
            "source_sha256_not_shared_across_splits",
        ],
    }


def _bounded_diagnostic(value: Any, *, depth: int = 0) -> Any:
    """Convert reader diagnostics to deterministic, size-bounded JSON values."""
    if depth >= 4:
        return "<max-depth>"
    if value is None or isinstance(value, (bool, int, str)):
        return value[:240] if isinstance(value, str) else value
    if isinstance(value, (float, np.floating)):
        number = float(value)
        return number if np.isfinite(number) else str(number)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, Mapping):
        items = list(value.items())
        result = {
            str(key)[:80]: _bounded_diagnostic(item, depth=depth + 1)
            for key, item in items[:24]
        }
        if len(items) > 24:
            result["_truncated_items"] = len(items) - 24
        return result
    if isinstance(value, (list, tuple)):
        result = [_bounded_diagnostic(item, depth=depth + 1) for item in value[:12]]
        if len(value) > 12:
            result.append({"_truncated_items": len(value) - 12})
        return result
    return repr(value)[:240]


def _raw_reader_score(diagnostics: Mapping[str, Any]) -> float | None:
    value = diagnostics.get("score")
    if isinstance(value, bool) or not isinstance(value, (int, float, np.number)):
        return None
    number = float(value)
    return number if np.isfinite(number) else None


def _evidence_group(case: BenchmarkCase) -> str:
    if case.category == "lightweight_numeric_roi":
        return "synthetic_cropped_stress"
    if case.category == "generated_perturbation":
        if case.source_coverage == "full_screenshot":
            return "synthetic_full_frame_stress"
        if case.source_coverage == "supplemental_crop":
            return "synthetic_cropped_stress"
        return "synthetic_unknown_coverage_stress"
    if case.category == "supplemental_crop":
        return "reference_supplemental_crop"
    if (
        case.expected_final_unknown_fields
        and case.render_equivalence_status == "visually_equivalent"
        and case.render_equivalence_evidence.strip()
    ):
        return "ambiguous_original_full_safety_control"
    if case.classification == "negative":
        return "negative_original_full_safety_control"
    if case.render_equivalence_status == "target_family":
        return "acceptance_target_family_original_full"
    if case.render_equivalence_status == "visually_equivalent":
        return "acceptance_visually_equivalent_original_full"
    if case.render_equivalence_status == "visibly_different":
        return "reference_visibly_different_original_full"
    return "reference_unknown_equivalence_original_full"


def _acceptance_role(case: BenchmarkCase) -> tuple[str, str]:
    """Classify evidence without inferring equivalence from filenames or versions."""
    if case.id != case.source_id or case.transform != "original":
        return "generated_diagnostic", "derived or transformed case"
    if case.category != "original_full" or case.source_coverage != "full_screenshot":
        return "reference_diagnostic", "not a preserved full screenshot"
    if case.data_origin != "real":
        return "reference_diagnostic", "not documented as a real capture"
    if case.asset_fidelity not in {"source_original", "source_transcode"}:
        return "reference_diagnostic", "capture fidelity is not preserved source evidence"
    if case.expected_final_unknown_fields:
        if case.render_equivalence_status != "visually_equivalent":
            return (
                "reference_diagnostic",
                f"ambiguity control render equivalence is {case.render_equivalence_status}",
            )
        if not case.render_equivalence_evidence.strip():
            return (
                "reference_diagnostic",
                "ambiguity control has no reviewed render-equivalence evidence",
            )
        return (
            "ambiguous_safety_control",
            "real original full screenshot with multiple valid resource panels; automatic "
            "layout selection and field acceptance are forbidden",
        )
    if case.classification == "negative":
        return (
            "negative_safety_control",
            "real original full screenshot with no expected resource panel",
        )
    if case.render_equivalence_status not in {
        "target_family",
        "visually_equivalent",
    }:
        return (
            "reference_diagnostic",
            f"render equivalence is {case.render_equivalence_status}",
        )
    if not case.render_equivalence_evidence.strip():
        return "reference_diagnostic", "render equivalence has no reviewed evidence"
    return (
        "positive_acceptance",
        "reviewed target-family or visually equivalent real original full screenshot",
    )


def _intersection_coverage(inner: Sequence[int], outer: Sequence[int]) -> float:
    il, it, ir, ib = inner
    ol, ot, oor, ob = outer
    area = max(0, ir - il) * max(0, ib - it)
    if not area:
        return 0.0
    overlap = max(0, min(ir, oor) - max(il, ol)) * max(0, min(ib, ob) - max(it, ot))
    return overlap / area


def score_case(
    case: BenchmarkCase,
    observation: TechniqueObservation,
    *,
    supports_cached_layout: bool = False,
) -> dict[str, Any]:
    """Score accepted values separately from abstentions and layout geometry."""
    acceptance_role, acceptance_reason = _acceptance_role(case)
    fields: dict[str, dict[str, Any]] = {}
    for name in FIELDS:
        label = case.labels[name]
        expected_unknown = case.eligible and name in case.expected_final_unknown_fields
        eligible = (
            case.eligible
            and not expected_unknown
            and case.classification != "negative"
            and label.get("verification") == "manual_verified"
            and type(label.get("value")) is int
        )
        actual = observation.values.get(name)
        accepted = type(actual) is int and actual >= 0
        region = observation.regions.get(name)
        detail = observation.field_details.get(name, "")
        if not detail and observation.panel_status != "found":
            detail = observation.detail
        raw_diagnostics = observation.field_diagnostics.get(name, {})
        diagnostics = _bounded_diagnostic(raw_diagnostics)
        location = None
        location_coverage = None
        other_field_overlap_max = None
        other_field_centers_contained = None
        if eligible and region is not None and label.get("box") is not None:
            try:
                region_left, region_top, region_right, region_bottom = region
                other_boxes = [
                    other["box"]
                    for other_name, other in case.labels.items()
                    if other_name != name
                    and other.get("visible")
                    and other.get("box") is not None
                ]
                location_coverage = _intersection_coverage(label["box"], region)
                other_overlaps = [
                    _intersection_coverage(other_box, region)
                    for other_box in other_boxes
                ]
                other_field_overlap_max = max(other_overlaps, default=0.0)
                other_field_centers_contained = sum(
                    region_left <= (other_box[0] + other_box[2]) / 2 < region_right
                    and region_top <= (other_box[1] + other_box[3]) / 2 < region_bottom
                    for other_box in other_boxes
                )
                location = corpus.region_corresponds(name, region, case.labels)
            except (TypeError, ValueError):
                location = False
        # A numerically equal value from the wrong row is not an exact read.
        # Tight annotations need only be contained by the detector's wider row.
        exact = bool(
            eligible and accepted and actual == label["value"] and location is True
        )
        numeric_mismatch = bool(
            eligible and accepted and actual != label["value"]
        )
        location_mismatch = bool(
            eligible and accepted and actual == label["value"] and location is not True
        )
        incorrect = bool(eligible and accepted and not exact)
        if observation.panel_status == "error":
            rejection_stage = "reader_error"
        elif observation.panel_status != "found":
            rejection_stage = "panel_discovery"
        elif accepted:
            rejection_stage = None
        elif raw_diagnostics.get("numeric_eligible") is True or region is not None:
            rejection_stage = "numeric_read"
        else:
            rejection_stage = "value_region"
        fields[name] = {
            "eligible": eligible,
            "expected_unknown": expected_unknown,
            "expected": label.get("value") if eligible or expected_unknown else None,
            "actual": actual if accepted else None,
            "quality": observation.qualities.get(
                name, Quality.KNOWN.value if accepted else Quality.UNKNOWN.value
            ),
            "detail": detail,
            "diagnostics": diagnostics,
            "rejection_stage": rejection_stage,
            "raw_reader_score": _raw_reader_score(raw_diagnostics),
            "calibrated_correctness_probability": None,
            "accepted": accepted,
            "exact": exact,
            "incorrect_accepted": incorrect,
            "numeric_mismatch_accepted": numeric_mismatch,
            "accepted_location_mismatch": location_mismatch,
            "unknown": bool(eligible and not accepted),
            "safe_expected_unknown": bool(expected_unknown and not accepted),
            "unsafe_expected_unknown_acceptance": bool(expected_unknown and accepted),
            "expected_region": label.get("box") if eligible or expected_unknown else None,
            "detected_region": list(region) if region is not None else None,
            "location_coverage": location_coverage,
            "other_field_overlap_max": other_field_overlap_max,
            "other_field_centers_contained": other_field_centers_contained,
            "location_corresponds": location,
        }
    expected_panel = case.eligible and case.expected_discovery
    false_panel_acceptance = bool(not expected_panel and observation.panel_status == "found")
    return {
        "case_id": case.id,
        "source_id": case.source_id,
        "source_sha256": case.source_sha256,
        "case_sha256": case.case_sha256,
        "category": case.category,
        "split": case.split,
        "classification": case.classification,
        "source_coverage": case.source_coverage,
        "data_origin": case.data_origin,
        "asset_fidelity": case.asset_fidelity,
        "render_equivalence": {
            "status": case.render_equivalence_status,
            "evidence": case.render_equivalence_evidence,
            "scope": case.render_equivalence_scope,
            "reviewed_at": case.render_equivalence_reviewed_at,
        },
        "acceptance_role": acceptance_role,
        "acceptance_reason": acceptance_reason,
        "abstention_reason": case.abstention_reason,
        "ambiguity_evidence": case.ambiguity_evidence,
        "evidence_group": _evidence_group(case),
        "transform": case.transform,
        "eligible": case.eligible,
        "exclusion": case.exclusion,
        "expected_panel": expected_panel,
        "panel_status": observation.panel_status,
        "panel_failure_stage": (
            "reader_error"
            if observation.panel_status == "error"
            else "panel_discovery"
            if observation.panel_status != "found"
            else None
        ),
        "false_panel_acceptance": false_panel_acceptance,
        "cold_total_ms": observation.cold_total_ms,
        "discovery_ms": observation.discovery_ms,
        "cached_ms": observation.cached_ms,
        "recovery_ms": observation.recovery_ms,
        "sequence_kind": case.sequence_kind,
        "expected_cached_unknown_fields": list(case.expected_cached_unknown_fields),
        "expected_final_unknown_fields": list(case.expected_final_unknown_fields),
        "cached_unknown_fields": list(observation.cached_unknown_fields),
        "cached_invalidation_ok": (
            set(case.expected_cached_unknown_fields).issubset(
                observation.cached_unknown_fields
            )
            if supports_cached_layout
            and case.expected_cached_unknown_fields
            and observation.panel_status != "error"
            else None
        ),
        "detail": observation.detail,
        "fields": fields,
    }


def _median(values: Iterable[float | None]) -> float | None:
    ordered = sorted(float(item) for item in values if item is not None)
    if not ordered:
        return None
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def _percentile(
    values: Iterable[float | None], percentile: float
) -> float | None:
    if not 0.0 <= percentile <= 1.0:
        raise ValueError("percentile must be between zero and one")
    ordered = sorted(float(item) for item in values if item is not None)
    if not ordered:
        return None
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _latency_metrics(prefix: str, values: Iterable[float | None]) -> dict[str, Any]:
    samples = list(values)
    present = [float(item) for item in samples if item is not None]
    return {
        f"{prefix}_median": _median(present),
        f"{prefix}_p95": _percentile(present, 0.95),
        f"{prefix}_max": max(present) if present else None,
        f"{prefix}_samples": len(present),
    }


def _metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    active_rows = [row for row in rows if row["eligible"]]
    ordinary_rows = [row for row in active_rows if row["sequence_kind"] is None]
    sequence_rows = [row for row in active_rows if row["sequence_kind"] is not None]
    eligible_fields = [
        field
        for row in active_rows
        for field in row["fields"].values()
        if field["eligible"]
    ]
    expected_unknown_fields = [
        field
        for row in active_rows
        for field in row["fields"].values()
        if field["expected_unknown"]
    ]
    expected = len(eligible_fields)
    exact = sum(field["exact"] for field in eligible_fields)
    incorrect = sum(field["incorrect_accepted"] for field in eligible_fields)
    numeric_mismatch = sum(
        field["numeric_mismatch_accepted"] for field in eligible_fields
    )
    location_mismatch = sum(
        field["accepted_location_mismatch"] for field in eligible_fields
    )
    unknown = sum(field["unknown"] for field in eligible_fields)
    accepted = exact + incorrect
    expected_panels = sum(row["expected_panel"] for row in active_rows)
    unexpected_panels = sum(not row["expected_panel"] for row in active_rows)
    found_expected = sum(
        row["expected_panel"] and row["panel_status"] == "found" for row in active_rows
    )
    locations_scored = sum(
        field["location_corresponds"] is not None for field in eligible_fields
    )
    locations_correct = sum(
        field["location_corresponds"] is True for field in eligible_fields
    )
    accepted_at_correct_location = sum(
        field["location_corresponds"] is True and field["accepted"]
        for field in eligible_fields
    )
    exact_at_correct_location = sum(
        field["location_corresponds"] is True
        and field["accepted"]
        and field["actual"] == field["expected"]
        for field in eligible_fields
    )
    rejection_stages: dict[str, int] = defaultdict(int)
    for field in eligible_fields:
        if field["unknown"] and field["rejection_stage"] is not None:
            rejection_stages[field["rejection_stage"]] += 1
    return {
        "cases": len(rows),
        "eligible_cases": len(active_rows),
        "excluded_cases": len(rows) - len(active_rows),
        "eligible_fields": expected,
        "numeric_exact": exact,
        "unknown": unknown,
        "incorrect_accepted": incorrect,
        "numeric_mismatch_accepted": numeric_mismatch,
        "accepted_location_mismatch": location_mismatch,
        "intentional_unknown_targets": len(expected_unknown_fields),
        "safe_intentional_unknown": sum(
            field["safe_expected_unknown"] for field in expected_unknown_fields
        ),
        "unsafe_intentional_unknown_acceptances": sum(
            field["unsafe_expected_unknown_acceptance"] for field in expected_unknown_fields
        ),
        "unsafe_intentional_unknown_acceptance_rate": (
            sum(
                field["unsafe_expected_unknown_acceptance"]
                for field in expected_unknown_fields
            )
            / len(expected_unknown_fields)
            if expected_unknown_fields
            else None
        ),
        "accepted": accepted,
        "exact_coverage": exact / expected if expected else None,
        "numeric_acceptance_coverage": accepted / expected if expected else None,
        # Undefined on zero accepted values: abstaining everywhere is never
        # presented as 100% accuracy.
        "accepted_value_precision": exact / accepted if accepted else None,
        "false_acceptance_share": incorrect / accepted if accepted else None,
        "incorrect_acceptance_rate": incorrect / expected if expected else None,
        "expected_panels": expected_panels,
        "found_expected_panels": found_expected,
        "missing_panels": sum(row["panel_status"] == "missing" for row in active_rows),
        "ambiguous_panels": sum(row["panel_status"] == "ambiguous" for row in active_rows),
        "reader_errors": sum(row["panel_status"] == "error" for row in active_rows),
        "false_panel_acceptances": sum(row["false_panel_acceptance"] for row in active_rows),
        "unexpected_panel_cases": unexpected_panels,
        "false_panel_acceptance_rate": (
            sum(row["false_panel_acceptance"] for row in active_rows)
            / unexpected_panels
            if unexpected_panels
            else None
        ),
        "recovery_cases": sum(row["sequence_kind"] is not None for row in active_rows),
        "cached_invalidation_failures": sum(
            row["cached_invalidation_ok"] is False for row in active_rows
        ),
        "locations_scored": locations_scored,
        "locations_correspond": locations_correct,
        "location_correspondence_rate": (
            locations_correct / expected if expected else None
        ),
        "correct_location_coverage": (
            locations_correct / expected if expected else None
        ),
        "numeric_acceptance_given_correct_location": (
            accepted_at_correct_location / locations_correct
            if locations_correct
            else None
        ),
        "numeric_exact_given_correct_location": (
            exact_at_correct_location / locations_correct
            if locations_correct
            else None
        ),
        "rejection_stage_failures": dict(sorted(rejection_stages.items())),
        **_latency_metrics(
            "cold_total_ms", (row["cold_total_ms"] for row in ordinary_rows)
        ),
        **_latency_metrics(
            "discovery_ms", (row["discovery_ms"] for row in ordinary_rows)
        ),
        **_latency_metrics(
            "cached_steady_ms", (row["cached_ms"] for row in ordinary_rows)
        ),
        **_latency_metrics(
            "cached_invalidation_ms", (row["cached_ms"] for row in sequence_rows)
        ),
        **_latency_metrics(
            "recovery_ms", (row["recovery_ms"] for row in sequence_rows)
        ),
    }


def summarize(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_category: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    by_split: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    by_category_split: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    by_source: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    by_transform_group: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    by_evidence_group: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        by_category[row["category"]].append(row)
        by_split[row["split"]].append(row)
        by_category_split[f"{row['category']}/{row['split']}"].append(row)
        by_source[row["source_id"]].append(row)
        by_evidence_group[row["evidence_group"]].append(row)
        transform = row["transform"]
        if transform == "original":
            group = "original"
        elif transform.startswith("full-scale-"):
            group = "full_scale"
        elif transform.startswith("panel-scale-"):
            group = "dense_roi_scale"
        elif transform.startswith("cached-"):
            group = transform.replace("cached-", "cached_").replace("-", "_")
        else:
            group = transform.replace("-", "_")
        by_transform_group[group].append(row)
    per_feature: dict[str, dict[str, Any]] = {}
    for name in FIELDS:
        eligible = [row["fields"][name] for row in rows if row["fields"][name]["eligible"]]
        expected_unknown = [
            row["fields"][name]
            for row in rows
            if row["fields"][name]["expected_unknown"]
        ]
        expected = len(eligible)
        exact = sum(item["exact"] for item in eligible)
        incorrect = sum(item["incorrect_accepted"] for item in eligible)
        accepted = exact + incorrect
        per_feature[name] = {
            "eligible": expected,
            "exact": exact,
            "unknown": sum(item["unknown"] for item in eligible),
            "incorrect_accepted": incorrect,
            "numeric_mismatch_accepted": sum(
                item["numeric_mismatch_accepted"] for item in eligible
            ),
            "accepted_location_mismatch": sum(
                item["accepted_location_mismatch"] for item in eligible
            ),
            "intentional_unknown_targets": len(expected_unknown),
            "safe_intentional_unknown": sum(
                item["safe_expected_unknown"] for item in expected_unknown
            ),
            "unsafe_intentional_unknown_acceptances": sum(
                item["unsafe_expected_unknown_acceptance"] for item in expected_unknown
            ),
            "exact_coverage": exact / expected if expected else None,
            "numeric_acceptance_coverage": accepted / expected if expected else None,
            "accepted_value_precision": exact / accepted if accepted else None,
            "locations_correspond": sum(
                item["location_corresponds"] is True for item in eligible
            ),
            "location_correspondence_rate": (
                sum(item["location_corresponds"] is True for item in eligible) / expected
                if expected
                else None
            ),
        }
    scorecards: dict[str, Any] = {}
    macro_rate_keys = (
        "exact_coverage",
        "numeric_acceptance_coverage",
        "accepted_value_precision",
        "correct_location_coverage",
        "numeric_acceptance_given_correct_location",
        "numeric_exact_given_correct_location",
        "incorrect_acceptance_rate",
        "false_panel_acceptance_rate",
        "unsafe_intentional_unknown_acceptance_rate",
    )
    for group, group_rows in sorted(by_evidence_group.items()):
        source_rows: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in group_rows:
            source_rows[row["source_id"]].append(row)
        source_metrics = {
            name: _metrics(items) for name, items in sorted(source_rows.items())
        }
        macro: dict[str, float | None] = {}
        sources_with_denominator: dict[str, int] = {}
        for key in macro_rate_keys:
            values = [
                float(metric[key])
                for metric in source_metrics.values()
                if metric[key] is not None
            ]
            macro[key] = sum(values) / len(values) if values else None
            sources_with_denominator[key] = len(values)
        safety_keys = (
            "incorrect_accepted",
            "unsafe_intentional_unknown_acceptances",
            "false_panel_acceptances",
            "reader_errors",
            "cached_invalidation_failures",
        )
        sources_with_safety_violation = sum(
            any(metric[key] for key in safety_keys)
            for metric in source_metrics.values()
        )
        scorecards[group] = {
            "source_count": len(source_metrics),
            "macro_per_source": macro,
            "macro_sources_with_denominator": sources_with_denominator,
            "sources_with_safety_violation": sources_with_safety_violation,
            "source_safety_incidence": (
                sources_with_safety_violation / len(source_metrics)
                if source_metrics
                else None
            ),
            "pooled": _metrics(group_rows),
            "per_source": source_metrics,
        }
    acceptance_rows = [
        row
        for row in rows
        if row["acceptance_role"]
        in {
            "positive_acceptance",
            "negative_safety_control",
            "ambiguous_safety_control",
        }
    ]
    positive_acceptance_rows = [
        row for row in rows if row["acceptance_role"] == "positive_acceptance"
    ]
    negative_control_rows = [
        row for row in rows if row["acceptance_role"] == "negative_safety_control"
    ]
    ambiguous_control_rows = [
        row for row in rows if row["acceptance_role"] == "ambiguous_safety_control"
    ]
    reference_rows = [
        row for row in rows if row["acceptance_role"] == "reference_diagnostic"
    ]
    generated_rows = [
        row for row in rows if row["acceptance_role"] == "generated_diagnostic"
    ]
    return {
        "aggregate": _metrics(rows),
        "acceptance": _metrics(acceptance_rows),
        "positive_acceptance": _metrics(positive_acceptance_rows),
        "negative_safety_controls": _metrics(negative_control_rows),
        "ambiguous_safety_controls": _metrics(ambiguous_control_rows),
        "reference_diagnostics": _metrics(reference_rows),
        "generated_diagnostics": _metrics(generated_rows),
        "by_category": {name: _metrics(items) for name, items in sorted(by_category.items())},
        "by_split": {name: _metrics(items) for name, items in sorted(by_split.items())},
        "by_category_split": {
            name: _metrics(items) for name, items in sorted(by_category_split.items())
        },
        "per_source": {name: _metrics(items) for name, items in sorted(by_source.items())},
        "by_transform_group": {
            name: _metrics(items) for name, items in sorted(by_transform_group.items())
        },
        "evidence_scorecards": scorecards,
        "per_feature": per_feature,
    }


def _version(distribution: str) -> str | None:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return None


def environment() -> dict[str, Any]:
    tesseract = shutil.which("tesseract")
    tesseract_version = None
    if tesseract:
        try:
            output = subprocess.run(
                [tesseract, "--version"], capture_output=True, text=True, check=False, timeout=5
            ).stdout.splitlines()
            tesseract_version = output[0] if output else None
        except (OSError, subprocess.TimeoutExpired):
            pass
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "opencv": cv2.__version__,
        "numpy": np.__version__,
        "pillow": _version("Pillow"),
        "pytesseract": _version("pytesseract"),
        "tesseract": tesseract_version,
    }


def implementation_hashes() -> dict[str, Any]:
    files = (
        "tibia_client/adaptive.py",
        "tibia_client/adaptive_text.py",
        "tibia_client/adaptive_runtime.py",
        "tibia_client/resource_profiles.py",
        "tibia_client/visual.py",
        "tools/benchmark_resources.py",
        "tools/compare_resource_readers.py",
    )
    code = {
        name: sha256((ROOT / name).read_bytes()).hexdigest()
        for name in files
    }
    profile_root = ROOT / "profiles/paulistinha-skills-v1"
    profile_hasher = sha256()
    asset_files = sorted(path for path in profile_root.rglob("*") if path.is_file())
    for path in asset_files:
        relative = path.relative_to(profile_root).as_posix().encode("utf-8")
        payload = path.read_bytes()
        profile_hasher.update(len(relative).to_bytes(4, "big"))
        profile_hasher.update(relative)
        profile_hasher.update(len(payload).to_bytes(8, "big"))
        profile_hasher.update(payload)
    return {
        "code_files": code,
        "profile_bundle_sha256": profile_hasher.hexdigest(),
        "profile_file_count": len(asset_files),
    }


def run_benchmark(
    cases: Sequence[BenchmarkCase],
    techniques: Sequence[Technique],
    *,
    warm_reads: int = 3,
    progress: Callable[[str, int, int, str], None] | None = None,
) -> dict[str, Any]:
    if type(warm_reads) is not int or warm_reads <= 0:
        raise ValueError("warm_reads must be a positive integer")
    if not cases:
        raise ValueError("benchmark needs at least one case")
    if not techniques:
        raise ValueError("benchmark needs at least one technique")
    case_ids = [case.id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("benchmark case IDs must be unique")
    technique_ids = [technique.id for technique in techniques]
    if len(technique_ids) != len(set(technique_ids)):
        raise ValueError("benchmark technique IDs must be unique")
    output: dict[str, Any] = {}
    for technique in techniques:
        rows = []
        for index, case in enumerate(cases, 1):
            if progress is not None:
                progress(technique.id, index, len(cases), case.id)
            inference = InferenceCase(
                frame=case.frame,
                prior_frame=case.prior_frame,
            )
            try:
                observation = technique.evaluate(inference, warm_reads=warm_reads)
            except Exception as exc:
                observation = TechniqueObservation(
                    "error", {}, detail=f"{type(exc).__name__}: {exc}"
                )
            rows.append(score_case(
                case,
                observation,
                supports_cached_layout=bool(
                    getattr(technique, "supports_cached_layout", False)
                ),
            ))
        # Keeping IDs makes identical case-set comparison directly auditable.
        if [row["case_id"] for row in rows] != case_ids:
            raise AssertionError("technique result order differs from the shared case set")
        output[technique.id] = {
            "options": dict(technique.options),
            "case_ids": list(case_ids),
            "summary": summarize(rows),
            "cases": rows,
        }
    return output


def build_report(
    manifest_path: Path,
    *,
    techniques: Sequence[Technique],
    include_generated: bool,
    warm_reads: int,
    progress: Callable[[str, int, int, str], None] | None = None,
) -> dict[str, Any]:
    initial_manifest_hash = sha256(manifest_path.read_bytes()).hexdigest()
    initial_implementation = implementation_hashes()
    document, cases = load_cases(manifest_path, include_generated=include_generated)
    lineage_validation = _validate_lineage(cases)
    results = run_benchmark(
        cases, techniques, warm_reads=warm_reads, progress=progress
    )
    baseline_name = techniques[0].id
    baseline = results[baseline_name]["summary"]["aggregate"]
    acceptance_baseline = results[baseline_name]["summary"]["acceptance"]
    deltas = {}
    acceptance_deltas = {}
    for technique in techniques[1:]:
        current = results[technique.id]["summary"]["aggregate"]
        deltas[technique.id] = {
            "baseline": baseline_name,
            "exact_coverage_points": (
                current["exact_coverage"] - baseline["exact_coverage"]
                if current["exact_coverage"] is not None
                and baseline["exact_coverage"] is not None
                else None
            ),
            "acceptance_coverage_points": (
                current["numeric_acceptance_coverage"]
                - baseline["numeric_acceptance_coverage"]
                if current["numeric_acceptance_coverage"] is not None
                and baseline["numeric_acceptance_coverage"] is not None
                else None
            ),
            "incorrect_accepted_change": (
                current["incorrect_accepted"] - baseline["incorrect_accepted"]
            ),
            "numeric_mismatch_change": (
                current["numeric_mismatch_accepted"]
                - baseline["numeric_mismatch_accepted"]
            ),
            "location_mismatch_change": (
                current["accepted_location_mismatch"]
                - baseline["accepted_location_mismatch"]
            ),
            "unknown_change": current["unknown"] - baseline["unknown"],
            "false_panel_acceptance_change": (
                current["false_panel_acceptances"] - baseline["false_panel_acceptances"]
            ),
        }
        acceptance_current = results[technique.id]["summary"]["acceptance"]
        acceptance_deltas[technique.id] = {
            "baseline": baseline_name,
            "exact_coverage_points": (
                acceptance_current["exact_coverage"]
                - acceptance_baseline["exact_coverage"]
                if acceptance_current["exact_coverage"] is not None
                and acceptance_baseline["exact_coverage"] is not None
                else None
            ),
            "acceptance_coverage_points": (
                acceptance_current["numeric_acceptance_coverage"]
                - acceptance_baseline["numeric_acceptance_coverage"]
                if acceptance_current["numeric_acceptance_coverage"] is not None
                and acceptance_baseline["numeric_acceptance_coverage"] is not None
                else None
            ),
            "incorrect_accepted_change": (
                acceptance_current["incorrect_accepted"]
                - acceptance_baseline["incorrect_accepted"]
            ),
            "unknown_change": (
                acceptance_current["unknown"] - acceptance_baseline["unknown"]
            ),
            "false_panel_acceptance_change": (
                acceptance_current["false_panel_acceptances"]
                - acceptance_baseline["false_panel_acceptances"]
            ),
        }
    final_manifest_hash = sha256(manifest_path.read_bytes()).hexdigest()
    final_implementation = implementation_hashes()
    post_validation_error = None
    try:
        corpus.load_manifest(manifest_path)
    except (OSError, ValueError) as exc:
        post_validation_error = f"{type(exc).__name__}: {exc}"
    run_valid = (
        initial_manifest_hash == final_manifest_hash
        and initial_implementation == final_implementation
        and post_validation_error is None
    )
    safety_violations = []
    technique_gates = {}
    generated_diagnostic_gates = {}

    def violations_for(metric: Mapping[str, Any]) -> list[str]:
        violations = []
        for key in (
            "incorrect_accepted",
            "unsafe_intentional_unknown_acceptances",
            "false_panel_acceptances",
            "reader_errors",
            "cached_invalidation_failures",
        ):
            if metric[key]:
                violations.append(f"{metric[key]} {key.replace('_', ' ')}")
        return violations

    for technique in techniques:
        metric = results[technique.id]["summary"]["acceptance"]
        violations = violations_for(metric)
        diagnostic_metric = results[technique.id]["summary"]["generated_diagnostics"]
        diagnostic_violations = violations_for(diagnostic_metric)
        technique_gates[technique.id] = {
            "role": "baseline" if technique.id == baseline_name else "candidate",
            "passed": not violations,
            "violations": violations,
            "safety": {
                "passed": not violations,
                "violations": violations,
            },
            "coverage": {
                "status": "not_defined",
                "thresholds": {},
            },
        }
        generated_diagnostic_gates[technique.id] = {
            "included": include_generated,
            "passed": not diagnostic_violations,
            "violations": diagnostic_violations,
            "acceptance_effect": "none",
        }
        safety_violations.extend(f"{technique.id}: {item}" for item in violations)
    if not run_valid:
        safety_violations.append("benchmark inputs changed or failed validation during the run")
    generated_diagnostic_violations = [
        f"{name}: {violation}"
        for name, gate in generated_diagnostic_gates.items()
        for violation in gate["violations"]
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "corpus": {
            "manifest_schema_version": document.get(
                "schema_version", document.get("version")
            ),
            "manifest_sha256": initial_manifest_hash,
            "case_count": len(cases),
            "case_ids": [case.id for case in cases],
            "lineage_validation": lineage_validation,
        },
        "environment": environment(),
        "implementation": initial_implementation,
        "post_run_verification": {
            "run_valid": run_valid,
            "manifest_sha256": final_manifest_hash,
            "implementation": final_implementation,
            "corpus_validation_error": post_validation_error,
        },
        "options": {
            "include_generated": include_generated,
            "include_generated_diagnostics": include_generated,
            "generated_diagnostics_affect_acceptance": False,
            "warm_reads": warm_reads,
            "techniques": [technique.id for technique in techniques],
            "reader_selection_uses_ground_truth": False,
            "truth_derived_case_construction": (
                "dense ROI scale stress cases are cropped from annotated panel boxes before "
                "being frozen; readers receive only pixels and never receive those boxes"
            ),
        },
        "timing_semantics": {
            "cold_total_ms": (
                "reader construction plus cold full-frame discovery/import plus first guarded "
                "numeric read; missing/ambiguous panels stop after discovery"
            ),
            "discovery_ms": (
                "adaptive full-frame search phase only; null for the legacy public API, which "
                "does not expose discovery separately"
            ),
            "cached_steady_ms_median": (
                "median of per-case guarded-read timings on ordinary unchanged frames"
            ),
            "cached_invalidation_ms_median": (
                "median one-shot stale-layout validation read on generated sequence frames"
            ),
            "recovery_ms": (
                "synchronous ResourceReader rediscovery plus first guarded read on the changed "
                "frame; null without a generated sequence"
            ),
            "comparability": (
                "Legacy has no cached-layout API. Comparable cold/search/cache aggregates "
                "exclude generated sequence cases, whose adaptive cold path uses the prior "
                "frame. Compare cold totals across techniques; "
                "do not compare legacy null cached timing with adaptive cached reads. This "
                "does not measure the background tracker's retry/backoff wall-clock delay."
            ),
        },
        "comparison_deltas": deltas,
        "acceptance_comparison_deltas": acceptance_deltas,
        "aggregate_semantics": {
            "role": "legacy_schema_v1_comparability",
            "representative_deployment_estimate": False,
            "scope": (
                "All loaded cases, including nonacceptance references and, only when opted "
                "in, generated diagnostics."
            ),
        },
        "acceptance_semantics": {
            "positive": (
                "Real preserved full screenshots annotated target_family or "
                "visually_equivalent with reviewed evidence. Source transcodes may qualify "
                "but do not prove native resolution or DPI."
            ),
            "negative_safety_control": (
                "Real original full screenshots with no expected resource panel; no hidden "
                "panel equivalence is asserted."
            ),
            "ambiguous_safety_control": (
                "Real original full screenshots with multiple valid resource panels. Every "
                "listed field must remain unknown and automatic unique-layout selection is a "
                "false panel acceptance."
            ),
            "excluded": (
                "Crops, visibly different or unknown render equivalence, and every generated "
                "case are reference diagnostics only."
            ),
        },
        "evidence_group_semantics": {
            "acceptance_target_family_original_full": (
                "Preserved full screenshot confirmed only to the target client family; this "
                "does not assert an exact build or a native, unresized capture pipeline."
            ),
            "acceptance_visually_equivalent_original_full": (
                "Preserved full screenshot with reviewed visual-equivalence evidence; no "
                "numeric version or native capture properties are inferred."
            ),
            "reference_visibly_different_original_full": (
                "Preserved full screenshot reviewed as visibly different from supported render."
            ),
            "reference_unknown_equivalence_original_full": (
                "Preserved full screenshot without enough evidence to establish equivalence."
            ),
            "reference_supplemental_crop": (
                "Preserved source crop; excluded from acceptance regardless of visual similarity."
            ),
            "synthetic_full_frame_stress": (
                "Deterministic transformation derived from a full-screenshot source; "
                "correlated with that source capture."
            ),
            "synthetic_cropped_stress": (
                "Deterministic transformation derived from a supplemental crop, or a dense "
                "numeric-ROI crop derived from any source; correlated with its source capture."
            ),
            "synthetic_unknown_coverage_stress": (
                "Deterministic transformation whose source coverage is unavailable; kept in "
                "an explicit unknown bucket."
            ),
            "negative_original_full_safety_control": (
                "Preserved full screenshot without an expected resource panel; contributes to "
                "safety metrics, not numeric coverage denominators."
            ),
            "ambiguous_original_full_safety_control": (
                "Preserved full screenshot containing multiple legitimate resource panels; "
                "requires layout and value abstention and contributes only to safety metrics."
            ),
        },
        "score_semantics": {
            "raw_reader_score": (
                "Technique-specific matcher evidence copied from Reading.diagnostics.score; "
                "it is not comparable across reader types unless the reader defines that."
            ),
            "calibrated_correctness_probability": (
                "Always null in this schema because no probability calibration study exists."
            ),
        },
        "stage_semantics": {
            "panel_discovery": (
                "No unique verified panel and value-column layout. The current reader detail "
                "does not reliably separate label-constellation failure from column-geometry "
                "failure, so the benchmark does not invent that attribution."
            ),
            "value_region": "Panel found, but this field had no numeric region to read.",
            "numeric_read": "A numeric region existed, but no value was accepted.",
            "reader_error": "The technique raised an error before producing a valid result.",
        },
        "evidence_limitations": {
            "historical_reference_sources_inspected_during_diagnosis": (
                manifest_path.resolve() == corpus.DEFAULT_MANIFEST.resolve()
            ),
            "legacy_aggregate": (
                "Retained for schema-v1 and historical report comparison; it includes "
                "nonacceptance references and, when opted in, is dominated by correlated "
                "synthetic stress variants. It is not a representative deployment estimate."
            ),
            "heldout_independence": "not_claimed",
            "heldout_reason": (
                "The original fourteen sources, including historical heldout failures, "
                "were inspected during diagnosis. Five additional sources were manually "
                "annotated before inference on 2026-10-14; retain first-evaluation results "
                "and disclose later tuning. Historical splits do not establish independent "
                "generalization."
            ),
            "source_independence": (
                "Transforms of one source are correlated; evidence scorecards macro-average "
                "source-level rates but do not create additional independent captures."
            ),
        },
        "run_valid": run_valid,
        "safety_passed": not safety_violations,
        "safety_violations": safety_violations,
        "safety_gate": {
            "scope": (
                "acceptance originals plus negative and real multi-panel ambiguity safety "
                "controls only"
            ),
            "passed": not safety_violations,
            "violations": safety_violations,
            "metrics": [
                "incorrect_accepted",
                "unsafe_intentional_unknown_acceptances",
                "false_panel_acceptances",
                "reader_errors",
                "cached_invalidation_failures",
            ],
        },
        "coverage_gate": {
            "scope": "acceptance originals only",
            "status": "not_defined",
            "thresholds": {},
            "reason": (
                "No coverage threshold was established before this benchmark run; coverage "
                "is reported for comparison and is not folded into the safety decision."
            ),
        },
        "technique_gates": technique_gates,
        "generated_diagnostic_gates": generated_diagnostic_gates,
        "generated_diagnostic_safety_passed": (
            not generated_diagnostic_violations if include_generated else None
        ),
        "generated_diagnostic_safety_violations": generated_diagnostic_violations,
        "techniques": results,
    }


def _percent(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _milliseconds(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f} ms"


def render_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# Resource reader benchmark",
        "",
        "All techniques ran on the same ordered case IDs. Precision is undefined when a "
        "technique accepts no numeric values; abstention is reported through coverage.",
        "The headline table uses only reviewed real full-screenshot acceptance evidence and "
        "real negative and multi-panel ambiguity safety controls. Crops, unknown or different "
        "renders, and generated cases do not affect acceptance.",
        "The historical heldout split is known-case regression evidence after diagnosis; "
        "this report makes no independent heldout generalization claim.",
        "",
        "| Technique | Positive originals | Negative controls | Ambiguity controls | Exact coverage | Acceptance coverage | Accepted precision | "
        "Value wrong | Location fail | Unknown | Missing | Ambiguous |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    if "safety_passed" in report:
        lines[6:6] = [
            f"**Acceptance safety gate:** "
            f"{'PASS' if report['safety_passed'] else 'FAIL'}",
            "",
        ]
    for name, result in report["techniques"].items():
        metric = result["summary"]["acceptance"]
        positive_cases = result["summary"]["positive_acceptance"]["cases"]
        negative_controls = result["summary"]["negative_safety_controls"]["cases"]
        ambiguous_controls = result["summary"]["ambiguous_safety_controls"]["cases"]
        lines.append(
            f"| `{name}` | {positive_cases} | {negative_controls} | "
            f"{ambiguous_controls} | "
            f"{_percent(metric['exact_coverage'])} | "
            f"{_percent(metric['numeric_acceptance_coverage'])} | "
            f"{_percent(metric['accepted_value_precision'])} | "
            f"{metric['numeric_mismatch_accepted']} | "
            f"{metric['accepted_location_mismatch']} | {metric['unknown']} | "
            f"{metric['missing_panels']} | {metric['ambiguous_panels']} |"
        )
    lines.extend(["", "## Results by evidence category", ""])
    lines.append("| Technique | Category | Cases | Exact coverage | Incorrect accepted | Unknown |")
    lines.append("|---|---|---:|---:|---:|---:|")
    for name, result in report["techniques"].items():
        for category, metric in result["summary"]["by_category"].items():
            lines.append(
                f"| `{name}` | `{category}` | {metric['cases']} | "
                f"{_percent(metric['exact_coverage'])} | {metric['incorrect_accepted']} | "
                f"{metric['unknown']} |"
            )
    lines.extend(["", "## Macro scorecards by source", ""])
    lines.append(
        "| Technique | Evidence group | Sources | Exact macro sources | Exact | Acceptance | "
        "Correct location | Numeric accepted given location | Sources failing safety | "
        "Pooled incorrect accepted |"
    )
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for name, result in report["techniques"].items():
        for group, scorecard in result["summary"].get("evidence_scorecards", {}).items():
            macro = scorecard["macro_per_source"]
            pooled = scorecard["pooled"]
            lines.append(
                f"| `{name}` | `{group}` | {scorecard['source_count']} | "
                f"{scorecard['macro_sources_with_denominator']['exact_coverage']} | "
                f"{_percent(macro['exact_coverage'])} | "
                f"{_percent(macro['numeric_acceptance_coverage'])} | "
                f"{_percent(macro['correct_location_coverage'])} | "
                f"{_percent(macro['numeric_acceptance_given_correct_location'])} | "
                f"{scorecard['sources_with_safety_violation']} | "
                f"{pooled['incorrect_accepted']} |"
            )
    lines.extend(
        [
            "",
            "Macro rates average source-level rates. JSON records the number of sources with "
            "a defined denominator for every macro; undefined sources are never silently "
            "counted as observations. Safety is shown as source incidence, while the final "
            "column is explicitly a pooled event count.",
            "",
            "## Offline latency by evidence group",
            "",
            "These are case-weighted replay diagnostics within each evidence group, not live "
            "deployment latency estimates. Synthetic rows remain correlated with their source.",
            "",
            "| Technique | Evidence group | Phase | Samples | p50 | p95 | Max |",
            "|---|---|---|---:|---:|---:|---:|",
        ]
    )
    latency_phases = (
        ("cold_total_ms", "cold total"),
        ("discovery_ms", "discovery"),
        ("cached_steady_ms", "cached steady"),
        ("cached_invalidation_ms", "cached invalidation"),
        ("recovery_ms", "recovery"),
    )
    for name, result in report["techniques"].items():
        for group, scorecard in result["summary"].get("evidence_scorecards", {}).items():
            metric = scorecard["pooled"]
            for prefix, label in latency_phases:
                samples = metric[f"{prefix}_samples"]
                if not samples:
                    continue
                lines.append(
                    f"| `{name}` | `{group}` | {label} | {samples} | "
                    f"{_milliseconds(metric[f'{prefix}_median'])} | "
                    f"{_milliseconds(metric[f'{prefix}_p95'])} | "
                    f"{_milliseconds(metric[f'{prefix}_max'])} |"
                )
    lines.extend(["", "## Results by robustness family", ""])
    lines.append(
        "| Technique | Family | Cases | Exact coverage | Value wrong | Location fail | Unknown |"
    )
    lines.append("|---|---|---:|---:|---:|---:|---:|")
    for name, result in report["techniques"].items():
        for family, metric in result["summary"]["by_transform_group"].items():
            lines.append(
                f"| `{name}` | `{family}` | {metric['cases']} | "
                f"{_percent(metric['exact_coverage'])} | "
                f"{metric['numeric_mismatch_accepted']} | "
                f"{metric['accepted_location_mismatch']} | {metric['unknown']} |"
            )
    acceptance_deltas = report.get(
        "acceptance_comparison_deltas", report.get("comparison_deltas")
    )
    if acceptance_deltas:
        lines.extend(
            [
                "",
                "## Change from baseline",
                "",
                "| Technique | Baseline | Exact coverage | Acceptance coverage | "
                "Incorrect accepted | Unknown | False panels |",
                "|---|---|---:|---:|---:|---:|---:|",
            ]
        )
        for name, delta in acceptance_deltas.items():
            exact = delta["exact_coverage_points"]
            accepted = delta["acceptance_coverage_points"]
            lines.append(
                f"| `{name}` | `{delta['baseline']}` | "
                f"{'—' if exact is None else f'{exact * 100:+.1f} pp'} | "
                f"{'—' if accepted is None else f'{accepted * 100:+.1f} pp'} | "
                f"{delta['incorrect_accepted_change']:+d} | {delta['unknown_change']:+d} | "
                f"{delta['false_panel_acceptance_change']:+d} |"
            )
    lines.extend(
        [
            "",
            "The schema-v1 all-case aggregate remains in JSON under `summary.aggregate` for "
            "historical comparison only. Historical heldout names are regression labels, not "
            "an untouched validation claim. Generated diagnostics and supplemental crops are "
            "separated in the evidence scorecards.",
            "",
        ]
    )
    return "\n".join(lines)


def _techniques(
    names: Sequence[str],
    include_ocr: bool,
    ocr_ablations: Sequence[str] = (),
) -> list[Technique]:
    result: list[Technique] = []
    for name in names:
        if name == "legacy":
            result.append(LegacyTechnique())
        elif name == "adaptive":
            result.append(AdaptiveTechnique(use_ocr=False))
        else:
            raise ValueError(f"unknown technique {name!r}")
    if include_ocr:
        result.append(AdaptiveTechnique(use_ocr=True))
    for ablation in ocr_ablations:
        if ablation == "labels-only":
            result.append(AdaptiveTechnique(label_ocr=True, digit_ocr=False))
        elif ablation == "digits-only":
            result.append(AdaptiveTechnique(label_ocr=False, digit_ocr=True))
        else:
            raise ValueError(f"unknown OCR ablation {ablation!r}")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=corpus.DEFAULT_MANIFEST)
    parser.add_argument(
        "--technique", action="append", choices=("legacy", "adaptive"),
        help="repeat to choose techniques (default: legacy and adaptive)",
    )
    parser.add_argument("--ocr", action="store_true", help="also run adaptive with Tesseract")
    parser.add_argument(
        "--ocr-ablation",
        action="append",
        choices=("labels-only", "digits-only"),
        default=[],
        help="repeat to add a split label/digit OCR ablation",
    )
    parser.add_argument(
        "--include-generated-diagnostics",
        action="store_true",
        help=(
            "opt in to historical artificial stress cases; they remain diagnostics and "
            "never affect acceptance gates"
        ),
    )
    parser.add_argument("--warm-reads", type=int, default=3)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--markdown-out", type=Path)
    args = parser.parse_args(argv)
    try:
        report = build_report(
            args.manifest,
            techniques=_techniques(
                args.technique or ("legacy", "adaptive"),
                args.ocr,
                args.ocr_ablation,
            ),
            include_generated=args.include_generated_diagnostics,
            warm_reads=args.warm_reads,
            progress=lambda technique, index, total, case_id: print(
                f"[{index}/{total}] {technique} {case_id}", file=sys.stderr, flush=True
            ),
        )
    except (OSError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    markdown = render_markdown(report)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(encoded, encoding="utf-8")
    if args.markdown_out:
        args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_out.write_text(markdown, encoding="utf-8")
    if not args.json_out and not args.markdown_out:
        print(markdown, end="")
    return 0 if report["safety_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
