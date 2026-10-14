from types import SimpleNamespace
from time import sleep

import numpy as np
import pytest

from core.perception import Frame
from tibia_client.adaptive import FIELDS
from tibia_client.models import Reading
from tools import benchmark_resource_latency as latency
from tools.compare_resource_readers import BenchmarkCase


def make_case(
    *,
    identifier="positive",
    classification="confirmed",
    transform="original",
    ambiguous=False,
):
    labels = {
        field: {
            "value": 10 + index,
            "visible": True,
            "verification": "manual_verified",
            "box": [10, 2 + index * 4, 16, 5 + index * 4],
        }
        for index, field in enumerate(FIELDS)
    }
    return BenchmarkCase(
        id=identifier if transform == "original" else f"{identifier}::{transform}",
        source_id=identifier,
        source_sha256="a" * 64,
        case_sha256="b" * 64,
        category="original_full" if transform == "original" else "generated_perturbation",
        split="train",
        classification=classification,
        frame=Frame(np.zeros((20, 20, 3), dtype=np.uint8)),
        labels=labels,
        expected_discovery=classification != "negative" and not ambiguous,
        transform=transform,
        source_coverage="full_screenshot",
        data_origin="real",
        asset_fidelity="source_original",
        render_equivalence_status="visually_equivalent" if ambiguous else "target_family",
        render_equivalence_evidence="reviewed test evidence",
        expected_final_unknown_fields=tuple(FIELDS) if ambiguous else (),
        abstention_reason="two legitimate resource panels" if ambiguous else None,
        ambiguity_evidence="reviewed Skills dock and Character dialog" if ambiguous else None,
    )


class FakeReader:
    def __init__(self, case, *, layout=True, wrong_regions=False):
        self.case = case
        regions = {field: tuple(case.labels[field]["box"]) for field in FIELDS}
        if wrong_regions:
            shifted = list(regions.values())[1:] + list(regions.values())[:1]
            regions = dict(zip(FIELDS, shifted))
        self.layout = SimpleNamespace(regions=regions) if layout else None
        self.read_calls = 0

    def discover(self, frame, *, use_ocr):
        assert frame is self.case.frame
        assert use_ocr is False
        return SimpleNamespace(layout=self.layout, candidates=(), detail="fake discovery")

    def read(self, frame, layout, *, use_ocr):
        self.read_calls += 1
        assert frame is self.case.frame
        assert layout is self.layout
        assert use_ocr is False
        return {
            field: Reading({"count": self.case.labels[field]["value"]})
            for field in FIELDS
        }


def test_selection_uses_only_real_original_acceptance_and_negative_controls():
    positive = make_case()
    negative = make_case(identifier="negative", classification="negative")
    ambiguous = make_case(identifier="ambiguous", classification="similar", ambiguous=True)
    generated = make_case(identifier="positive", transform="translated")

    selected, excluded = latency.select_original_cases(
        [positive, negative, ambiguous, generated]
    )

    assert [case.id for case in selected] == ["positive", "negative", "ambiguous"]
    assert excluded == [{"case_id": "positive::translated", "reason": "derived or transformed case"}]


def test_positive_case_records_every_warm_timing_and_strong_row_scoring():
    case = make_case()

    result = latency.benchmark_case(
        case,
        warm_reads=50,
        reading_budget_ms=None,
        reader_factory=lambda: FakeReader(case),
    )

    assert result["panel_status"] == "found"
    assert result["warm_read_timings"]["sample_count"] == 50
    assert len(result["warm_read_timings"]["samples_ms"]) == 50
    assert len(result["warm_reads"]) == 50
    assert all(item["all_exact"] for item in result["warm_reads"])
    assert all(item["all_rows_correspond"] for item in result["warm_reads"])
    assert result["all_warm_reads_exact_and_corresponding"] is True
    assert result["passed"] is True


def test_equal_numbers_from_wrong_rows_fail_correspondence():
    case = make_case()

    result = latency.benchmark_case(
        case,
        warm_reads=50,
        reading_budget_ms=None,
        reader_factory=lambda: FakeReader(case, wrong_regions=True),
    )

    assert all(not item["all_exact"] for item in result["warm_reads"])
    assert all(not item["all_rows_correspond"] for item in result["warm_reads"])
    assert result["passed"] is False


def test_negative_original_is_cold_safety_check_without_repeated_reads():
    case = make_case(identifier="negative", classification="negative")

    safe = latency.benchmark_case(
        case,
        warm_reads=50,
        reading_budget_ms=None,
        reader_factory=lambda: FakeReader(case, layout=False),
    )
    unsafe = latency.benchmark_case(
        case,
        warm_reads=50,
        reading_budget_ms=None,
        reader_factory=lambda: FakeReader(case, layout=True),
    )

    assert safe["passed"] is True
    assert safe["warm_read_timings"]["sample_count"] == 0
    assert unsafe["false_panel_acceptance"] is True
    assert unsafe["passed"] is False


def test_ambiguous_original_requires_layout_abstention_and_never_reads():
    case = make_case(identifier="ambiguous", classification="similar", ambiguous=True)
    safe_reader = FakeReader(case, layout=False)
    unsafe_reader = FakeReader(case, layout=True)

    safe = latency.benchmark_case(
        case,
        warm_reads=50,
        reading_budget_ms=None,
        reader_factory=lambda: safe_reader,
    )
    unsafe = latency.benchmark_case(
        case,
        warm_reads=50,
        reading_budget_ms=None,
        reader_factory=lambda: unsafe_reader,
    )

    assert safe["acceptance_role"] == "ambiguous_safety_control"
    assert safe["expected_final_unknown_fields"] == list(FIELDS)
    assert safe["abstention_passed"] is True
    assert safe["accepted_read_count"] == 0
    assert safe["warm_read_timings"]["sample_count"] == 0
    assert safe_reader.read_calls == 0
    assert safe["passed"] is True
    assert unsafe["abstention_passed"] is False
    assert unsafe["false_panel_acceptance"] is True
    assert unsafe["accepted_read_count"] == 0
    assert unsafe_reader.read_calls == 0
    assert unsafe["passed"] is False


def test_declared_reading_budget_checks_every_warm_sample():
    case = make_case()

    class SlowReader(FakeReader):
        def read(self, frame, layout, *, use_ocr):
            sleep(0.001)
            return super().read(frame, layout, use_ocr=use_ocr)

    result = latency.benchmark_case(
        case,
        warm_reads=50,
        reading_budget_ms=0.1,
        reader_factory=lambda: SlowReader(case),
    )

    assert result["budget_met"] is False
    assert result["warm_read_timings"]["sample_count"] == 50
    assert result["passed"] is False


@pytest.mark.parametrize("count", [0, 49, 1001])
def test_warm_read_count_is_bounded(count):
    case = make_case()

    with pytest.raises(ValueError, match="warm_reads"):
        latency.benchmark_case(
            case,
            warm_reads=count,
            reading_budget_ms=None,
            reader_factory=lambda: FakeReader(case),
        )
