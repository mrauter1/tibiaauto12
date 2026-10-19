"""Adapter integration and policy traces; generated evidence is not gameplay."""
from types import SimpleNamespace

import numpy as np
import pytest

from core.perception import Frame
from tibia_client.models import Quality, Reading
from tools.resource_bar_evidence import BarEvidence, FreshMaximum, frame_token
from tools.resource_fusion import FusionResourceReader
from tools.benchmark_resource_fusion_sequences import build_report, run_trace, scenarios

HP, MP = "resource_health", "resource_mana"
LAYOUT = SimpleNamespace(fingerprint="layout", bounds=(0, 0, 10, 10))


def image(at=1, *, source="camera"):
    return Frame(np.full((10, 10, 3), round(at * 100) % 256, dtype=np.uint8),
                 captured_at=at, source=source)


def reading(value, *, valid_layout=True):
    return Reading(None if value is None else {"count": value},
                   Quality.UNKNOWN if value is None else Quality.KNOWN,
                   diagnostics={"numeric_eligible": valid_layout,
                                "layout_fingerprint": "layout" if valid_layout else None})


class CurrentDecoder:
    value = 500
    valid_layout = True

    def read(self, frame, layout, **kwargs):
        return {HP: reading(self.value, valid_layout=self.valid_layout),
                MP: reading(200, valid_layout=self.valid_layout),
                "resource_soul": reading(100, valid_layout=self.valid_layout),
                "resource_capacity": reading(2755, valid_layout=self.valid_layout)}


class MeasuredBars:
    fraction = .5

    def measure(self, frame, layout):
        self.layout = layout
        return {field: BarEvidence(
            field, (fraction - .005, fraction + .005) if layout is not None else None,
            (0, 0, 10, 10) if layout is not None else None, (1, 1, 9, 9),
            "left_to_right", frame.captured_at, "layout", frame_token(frame))
            for field, fraction in ((HP, self.fraction), (MP, .2))}


class IndependentCaption:
    hp_current = 500
    available = True
    last_diagnostics = {"known": True}

    def read(self, frame, evidence):
        if not self.available:
            return None
        return FreshMaximum(1000, frame.captured_at, "layout", frame_token(frame), True,
                            evidence.field, self.hp_current if evidence.field == HP else 200)


def adapter(variant):
    decoder, bars, caption = CurrentDecoder(), MeasuredBars(), IndependentCaption()
    reader = FusionResourceReader(variant, capture_session="capture-one", reader=decoder,
                                  bar_reader=bars, caption_reader=caption)
    return reader, decoder, bars, caption


@pytest.mark.parametrize("variant", ("bar", "combined"))
def test_unknown_never_promoted_even_when_bar_caption_provides_exact_count(variant):
    reader, decoder, _, _ = adapter(variant)
    decoder.value = None
    result = reader.read(image(), LAYOUT)
    assert result[HP].quality is Quality.UNKNOWN
    assert result[HP].value is None
    assert reader.last_diagnostics[HP]["fraction_interval"] == (.495, .505)
    assert reader.last_diagnostics[HP]["caption_current"] == 500


@pytest.mark.parametrize("variant", ("bar", "combined"))
def test_reliable_bar_contradiction_vetoes_wrong_primary_digit(variant):
    reader, decoder, _, _ = adapter(variant)
    decoder.value = 900
    result = reader.read(image(), LAYOUT)
    assert result[HP].quality is Quality.UNKNOWN
    assert reader.last_diagnostics[HP]["vetoed"] is True
    assert result["resource_capacity"].value["count"] == 2755


@pytest.mark.parametrize("variant", ("bar", "combined"))
def test_caption_bar_internal_conflict_cannot_discard_good_primary_number(variant):
    reader, _, _, caption = adapter(variant)
    caption.hp_current = 900
    result = reader.read(image(), LAYOUT)
    assert result[HP].value["count"] == 500
    assert reader.last_diagnostics[HP]["maximum_available"] is False


def test_temporal_confirms_current_damage_without_returning_old_value():
    reader, decoder, _, _ = adapter("temporal")
    assert reader.read(image(), LAYOUT)[HP].value["count"] == 500
    decoder.value = 250
    pending = reader.read(image(1.05), LAYOUT)[HP]
    assert pending.quality is Quality.UNKNOWN and pending.value is None
    confirmed = reader.read(image(1.1), LAYOUT)[HP]
    assert confirmed.value["count"] == 250
    assert confirmed.diagnostics["temporal"]["confirmation_delay_s"] == pytest.approx(.05)


def test_combined_confirms_damage_in_same_frame_with_independent_bar_change():
    reader, decoder, bars, caption = adapter("combined")
    reader.read(image(), LAYOUT)
    decoder.value = caption.hp_current = 250
    bars.fraction = .25
    result = reader.read(image(1.05), LAYOUT)[HP]
    assert result.value["count"] == 250
    assert result.diagnostics["temporal"]["action"] == "bar_corroborated_current_change"


def test_unverified_maximum_cannot_bypass_temporal_confirmation():
    reader, decoder, bars, caption = adapter("combined")
    reader.read(image(), LAYOUT)
    caption.available = False
    decoder.value = 250
    bars.fraction = .25
    assert reader.read(image(1.05), LAYOUT)[HP].quality is Quality.UNKNOWN
    assert reader.read(image(1.1), LAYOUT)[HP].value["count"] == 250


@pytest.mark.parametrize("variant", ("temporal", "combined"))
def test_repeated_capture_timestamp_is_rejected(variant):
    reader, _, _, _ = adapter(variant)
    frame = image()
    assert reader.read(frame, LAYOUT)[HP].quality is Quality.KNOWN
    assert reader.read(frame, LAYOUT)[HP].quality is Quality.UNKNOWN


def test_current_layout_failure_invalidates_bar_identity_and_temporal_history():
    reader, decoder, bars, _ = adapter("combined")
    reader.read(image(), LAYOUT)
    decoder.valid_layout = False
    result = reader.read(image(1.05), LAYOUT)
    assert bars.layout is None
    assert result[HP].quality is Quality.UNKNOWN


def test_new_capture_session_discards_clock_and_history():
    reader, decoder, _, _ = adapter("temporal")
    reader.read(image(100), LAYOUT)
    reader.reset(capture_session="capture-two")
    decoder.value = 250
    assert reader.read(image(1), LAYOUT)[HP].value["count"] == 250


def test_generated_trace_reports_tradeoffs_without_claiming_native_evidence():
    report = build_report()
    assert report["native_sequence_count"] == 0
    assert report["origin"] == "generated_decision_inputs"
    summary = report["summaries"]
    assert summary["robust"]["incorrect_known"] > 0
    assert summary["temporal"]["incorrect_known"] > 0  # persistent errors survive agreement
    assert summary["bar"]["incorrect_known"] == summary["combined"]["incorrect_known"] == 0
    assert all(row["unsafe_unknown_promotions"] == 0 for row in summary.values())
    trace = report["traces"]["rapid_damage"]
    assert all(item["unknown"] for item in trace["temporal"][1:])
    assert not any(item["unknown"] for item in trace["combined"])


def test_persistence_cannot_override_a_current_bar_contradiction():
    outputs = run_trace(scenarios()["persistent_wrong_digit"], "combined")
    assert [item["current_output"] for item in outputs] == [500, None, None]
