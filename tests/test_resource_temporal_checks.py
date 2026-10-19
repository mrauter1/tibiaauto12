"""Generated temporal policy diagnostics, not acceptance evidence from live Tibia."""
from __future__ import annotations

from dataclasses import replace

import pytest

from tibia_client.models import Quality, Reading
from tools.resource_temporal_checks import (
    TemporalBarInterval,
    TemporalIdentity,
    TemporalPolicy,
    TemporalResourceGuard,
)


HP = "resource_health"
MANA = "resource_mana"
IDENTITY = TemporalIdentity("capture-window-1", "session-1", (1920, 1080),
                            "verified-layout-1", (1700, 100, 1900, 700))


def known(value, marker="current"):
    return Reading({"present": True, "count": value, "score": 0.99},
                   diagnostics={"frame_marker": marker})


def check(guard, value, timestamp, *, identity=IDENTITY, unstable=False, bar=None,
          token=None, marker=None):
    reading = value if isinstance(value, Reading) else known(value, marker or str(timestamp))
    result = guard.check({HP: reading}, captured_at=timestamp, identity=identity,
                         unstable_fields=(HP,) if unstable else (),
                         bars={HP: bar} if bar is not None else {}, frame_token=token)[HP]
    if result.quality is Quality.KNOWN:
        # The returned number and marker must belong to this exact input call.
        assert result.value == reading.value
        assert result.diagnostics["frame_marker"] == reading.diagnostics["frame_marker"]
    else:
        assert result.value is None
    return result


def interval(lower, upper, timestamp, token, *, maximum=100, verified=True, identity=IDENTITY):
    return TemporalBarInterval(lower, upper, timestamp, identity.layout_fingerprint,
                               token, maximum, verified)


def change_guard(**kwargs):
    return TemporalResourceGuard(TemporalPolicy(confirm_changes=True, **kwargs))


def action(reading):
    return reading.diagnostics["temporal"]["action"]


def test_default_policy_allows_trustworthy_damage_and_healing_immediately():
    guard = TemporalResourceGuard()
    readings = [check(guard, value, 10 + i / 25) for i, value in enumerate((100, 3, 0, 900, 750))]
    assert all(item.quality is Quality.KNOWN for item in readings)
    assert [item.value["count"] for item in readings] == [100, 3, 0, 900, 750]
    assert action(readings[1]) == "trusted_current_change"


def test_numeric_confirmation_suppresses_isolated_glitch_without_returning_old_value():
    guard = change_guard()
    before = check(guard, 100, 1)
    glitch = check(guard, 700, 1.04)
    recovered = check(guard, 100, 1.08)
    assert before.value["count"] == recovered.value["count"] == 100
    assert glitch.quality is Quality.UNKNOWN and glitch.value is None
    assert action(recovered) == "unchanged_current"


def test_numeric_repetition_cannot_identify_a_persistent_known_misread():
    # Diagnostic limitation: the synthetic true value is100, but the independent
    # decoder falsely claims900 twice. Repetition alone cannot disprove it.
    guard = change_guard()
    check(guard, 100, 1)
    assert check(guard, 900, 1.04).quality is Quality.UNKNOWN
    repeated_error = check(guard, 900, 1.08)
    assert repeated_error.quality is Quality.KNOWN
    assert repeated_error.value["count"] == 900


@pytest.mark.parametrize("next_value", [0, 1, 40, 1000])
def test_changed_value_recovers_on_second_current_decode_with_one_frame_latency(next_value):
    guard = change_guard()
    check(guard, 100, 1)
    assert check(guard, next_value, 1.04).quality is Quality.UNKNOWN
    current = check(guard, next_value, 1.08)
    assert current.quality is Quality.KNOWN and current.value["count"] == next_value
    assert current.diagnostics["temporal"]["confirmation_delay_s"] == pytest.approx(0.04)


def test_repeated_real_damage_exposes_numeric_confirmation_tradeoff():
    guard = change_guard()
    values = tuple(range(100, 70, -1))
    results = [check(guard, value, 1 + i * 0.04)
               for i, value in enumerate(values)]
    assert results[0].quality is Quality.KNOWN
    assert all(item.quality is Quality.UNKNOWN for item in results[1:])
    # Time alone never legitimizes an unresolved candidate, even when both the
    # 250ms pending window and500ms accepted-reference retention have expired.
    recovered = check(guard, values[-1], 1 + len(values) * 0.04)
    assert recovered.quality is Quality.KNOWN and recovered.value["count"] == values[-1]
    assert recovered.diagnostics["temporal"]["confirmation_delay_s"] == pytest.approx(0.04)


def test_unstable_initial_candidate_requires_two_fresh_decodes():
    guard = TemporalResourceGuard()
    assert check(guard, 100, 1, unstable=True).quality is Quality.UNKNOWN
    assert check(guard, 100, 1.04, unstable=True).quality is Quality.KNOWN


@pytest.mark.parametrize("quality", [Quality.UNKNOWN, Quality.ERROR])
def test_unknown_and_error_clear_history_and_never_promote_a_stored_value(quality):
    guard = change_guard()
    check(guard, 100, 1)
    check(guard, 40, 1.04)
    unavailable = check(guard, Reading(value=40, quality=quality), 1.08)
    assert unavailable.quality is quality and unavailable.value is None
    assert guard.history_size == 0 and guard.tracked_fields == ()
    assert check(guard, 40, 1.12, unstable=True).quality is Quality.UNKNOWN
    assert check(guard, 40, 1.16, unstable=True).quality is Quality.KNOWN


def test_pending_number_is_not_used_as_a_missing_current_reading():
    guard = change_guard()
    check(guard, 100, 1)
    check(guard, 40, 1.04)
    result = guard.check({}, captured_at=1.08, identity=IDENTITY)
    assert result == {} and guard.history_size == 0


def test_confirmation_window_expires_without_promoting_the_candidate():
    guard = change_guard()
    check(guard, 100, 1)
    check(guard, 40, 1.04)
    expired = check(guard, 40, 1.30)
    assert expired.quality is Quality.UNKNOWN
    assert expired.diagnostics["temporal"]["pending_age_s"] == 0
    assert check(guard, 40, 1.34).quality is Quality.KNOWN


def test_fast_duplicate_calls_do_not_satisfy_minimum_fresh_separation():
    guard = change_guard(min_separation_s=0.01)
    assert check(guard, 40, 1, unstable=True).quality is Quality.UNKNOWN
    assert check(guard, 40, 1.005, unstable=True).quality is Quality.UNKNOWN
    result = check(guard, 40, 1.011, unstable=True)
    assert result.quality is Quality.KNOWN
    assert result.diagnostics["temporal"]["confirmation_delay_s"] == pytest.approx(0.011)


@pytest.mark.parametrize("bad_timestamp", [1.04, 1.03, 0.0])
def test_equal_or_out_of_order_capture_clears_history_without_clock_rewind(bad_timestamp):
    guard = change_guard()
    check(guard, 100, 1)
    check(guard, 40, 1.04)
    bad = check(guard, 40, bad_timestamp)
    assert action(bad) == "nonadvancing_timestamp" and guard.history_size == 0
    assert action(check(guard, 40, 1.02)) == "nonadvancing_timestamp"
    assert check(guard, 40, 1.08).quality is Quality.UNKNOWN
    assert check(guard, 40, 1.12).quality is Quality.KNOWN


@pytest.mark.parametrize("bad_timestamp", [float("nan"), float("inf"), -1, True, "1.08"])
def test_invalid_timestamp_fails_closed_and_requires_fresh_confirmation(bad_timestamp):
    guard = change_guard()
    check(guard, 100, 1)
    assert action(check(guard, 40, bad_timestamp)) == "invalid_timestamp"
    assert check(guard, 40, 1.04).quality is Quality.UNKNOWN
    assert check(guard, 40, 1.08).quality is Quality.KNOWN


def test_gap_reset_discards_pending_confirmation_and_accepts_new_trusted_baseline():
    guard = change_guard()
    check(guard, 100, 1)
    check(guard, 40, 1.04)
    new = check(guard, 30, 1.60)
    assert new.quality is Quality.KNOWN and action(new) == "initial_current"
    assert new.diagnostics["temporal"]["reset_reason"] == "capture_gap"
    guard.reset()
    check(guard, 40, 1, unstable=True)
    assert check(guard, 40, 1.60, unstable=True).quality is Quality.UNKNOWN


@pytest.mark.parametrize("changed", [
    replace(IDENTITY, source="different-window"),
    replace(IDENTITY, capture_session="session-2"),
    replace(IDENTITY, layout_fingerprint="scrolled-layout"),
    replace(IDENTITY, panel_bounds=(1690, 100, 1890, 700)),
    replace(IDENTITY, frame_size=(2560, 1440)),
])
def test_identity_change_cannot_confirm_a_candidate_from_the_old_panel(changed):
    guard = change_guard()
    check(guard, 40, 1, unstable=True)
    assert check(guard, 40, 1.04, unstable=True, identity=changed).quality is Quality.UNKNOWN
    assert check(guard, 40, 1.08, unstable=True, identity=changed).quality is Quality.KNOWN


def test_geometry_change_cannot_reclassify_an_old_capture_as_current():
    guard = change_guard()
    check(guard, 100, 10)
    moved = replace(IDENTITY, panel_bounds=(1690, 100, 1890, 700))
    assert action(check(guard, 40, 9, identity=moved)) == "nonadvancing_timestamp"
    restarted = replace(moved, capture_session="restarted-clock")
    assert check(guard, 40, 0, identity=restarted).quality is Quality.KNOWN


def test_missing_current_panel_identity_does_not_use_previous_identity():
    guard = change_guard()
    check(guard, 100, 1)
    result = check(guard, 40, 1.04, identity=None)
    assert action(result) == "missing_current_identity" and guard.history_size == 0
    assert check(guard, 40, 1.08, unstable=True).quality is Quality.UNKNOWN


def test_verified_bar_direction_allows_immediate_damage_and_healing():
    guard = change_guard()
    sequence = [(80, 1, 0.79, 0.81), (40, 1.04, 0.39, 0.41), (90, 1.08, 0.89, 0.91)]
    for index, (value, timestamp, lower, upper) in enumerate(sequence):
        token = f"pixels-{index}"
        result = check(guard, value, timestamp, token=token,
                       bar=interval(lower, upper, timestamp, token))
        assert result.quality is Quality.KNOWN
        if index:
            assert action(result) == "bar_corroborated_current_change"
            assert "confirmation_delay_s" not in result.diagnostics["temporal"]


@pytest.mark.parametrize("change", [
    {"captured_at": 1.03}, {"layout_fingerprint": "other-panel"},
    {"frame_token": "other-frame"}, {"maximum_verified": False}, {"maximum": 200},
])
def test_stale_unbound_or_changed_maximum_bar_cannot_bypass_confirmation(change):
    guard = change_guard()
    check(guard, 80, 1, token="first", bar=interval(0.79, 0.81, 1, "first"))
    bar = interval(0.19, 0.41, 1.04, "second")
    result = check(guard, 40, 1.04, token="second", bar=replace(bar, **change))
    assert result.quality is Quality.UNKNOWN
    assert action(result) == "awaiting_fresh_confirmation"


def test_missing_maximum_in_previous_bar_cannot_corroborate_current_damage():
    guard = change_guard()
    check(guard, 80, 1, token="first", bar=interval(0.79, 0.81, 1, "first", verified=False))
    assert check(guard, 40, 1.04, token="second",
                 bar=interval(0.39, 0.41, 1.04, "second")).quality is Quality.UNKNOWN


def test_overlapping_bar_intervals_do_not_establish_direction():
    guard = change_guard()
    check(guard, 80, 1, token="first", bar=interval(0.75, 0.85, 1, "first"))
    assert check(guard, 79, 1.04, token="second",
                 bar=interval(0.74, 0.84, 1.04, "second")).quality is Quality.UNKNOWN


def test_identical_pixel_token_cannot_claim_bar_movement():
    guard = change_guard()
    check(guard, 80, 1, token="same", bar=interval(0.79, 0.81, 1, "same"))
    assert check(guard, 40, 1.04, token="same",
                 bar=interval(0.39, 0.41, 1.04, "same")).quality is Quality.UNKNOWN


def test_old_bar_direction_is_not_used_beyond_confirmation_window():
    guard = change_guard()
    check(guard, 80, 1, token="first", bar=interval(0.79, 0.81, 1, "first"))
    assert check(guard, 40, 1.30, token="second",
                 bar=interval(0.39, 0.41, 1.30, "second")).quality is Quality.UNKNOWN


def test_repeated_current_bar_contradiction_never_wins_through_persistence():
    guard = change_guard()
    for index in range(12):
        timestamp, token = 1 + index * 0.04, str(index)
        result = check(guard, 80, timestamp, token=token,
                       bar=interval(0.2, 0.3, timestamp, token))
        assert action(result) == "current_bar_contradiction"
        assert guard.history_size == 0
    first_safe = check(guard, 80, 1.48, token="safe1", bar=interval(0.79, 0.81, 1.48, "safe1"))
    second_safe = check(guard, 80, 1.52, token="safe2", bar=interval(0.79, 0.81, 1.52, "safe2"))
    assert first_safe.quality is Quality.UNKNOWN
    assert second_safe.quality is Quality.KNOWN


@pytest.mark.parametrize("lower,upper", [(0.49, 0.49), (0.51, 0.51)])
def test_temporal_bar_veto_matches_one_integer_unit_rounding_tolerance(lower, upper):
    guard = change_guard()
    result = check(guard, 50, 1, token="rounded", bar=interval(lower, upper, 1, "rounded"))
    assert result.quality is Quality.KNOWN
    rejected = check(guard, 53, 1.04, token="beyond", bar=interval(0.49, 0.49, 1.04, "beyond"))
    assert action(rejected) == "current_bar_contradiction"


def test_bar_cannot_promote_current_unknown_or_explicit_instability_immediately():
    guard = change_guard()
    check(guard, 80, 1, token="first", bar=interval(0.79, 0.81, 1, "first"))
    current = check(guard, 40, 1.04, token="second", unstable=True,
                    bar=interval(0.39, 0.41, 1.04, "second"))
    assert current.quality is Quality.UNKNOWN
    unavailable = check(guard, Reading(quality=Quality.UNKNOWN), 1.08, token="third",
                        bar=interval(0.39, 0.41, 1.08, "third"))
    assert unavailable.quality is Quality.UNKNOWN and guard.history_size == 0


@pytest.mark.parametrize("value", [True, -1, 1.0, "100", None])
def test_malformed_current_number_never_uses_a_previous_number(value):
    guard = change_guard()
    check(guard, 100, 1)
    assert action(check(guard, known(value), 1.04)) == "invalid_current_number"
    assert guard.history_size == 0


def test_direct_integer_reading_and_zero_are_supported():
    guard = TemporalResourceGuard()
    current = Reading(0, diagnostics={"frame_marker": "zero"})
    assert check(guard, current, 1).value == 0


def test_histories_are_independent_and_bounded_for_four_fields():
    guard = change_guard()
    fields = (HP, MANA, "resource_soul", "resource_capacity")
    for index in range(1000):
        readings = {key: known(100 + (index % 2), str(index)) for key in fields}
        result = guard.check(readings, captured_at=1 + index * 0.04, identity=IDENTITY)
        assert guard.history_size <= 8
        if index and index % 2:
            assert all(item.quality is Quality.UNKNOWN for item in result.values())
    result = guard.check({HP: Reading(quality=Quality.UNKNOWN), MANA: known(100)},
                         captured_at=42, identity=IDENTITY)
    assert result[HP].quality is Quality.UNKNOWN
    assert result[MANA].quality is Quality.KNOWN
    assert guard.tracked_fields == (MANA,)


@pytest.mark.parametrize("kwargs", [
    {"confirmation_window_s": 0}, {"confirmation_window_s": 1.01},
    {"max_gap_s": 0.1}, {"max_gap_s": float("inf")}, {"min_separation_s": 0},
    {"min_separation_s": 0.3}, {"confirm_changes": "yes"},
])
def test_invalid_policy_is_rejected(kwargs):
    with pytest.raises(ValueError):
        TemporalPolicy(**kwargs)


def test_invalid_identity_bar_and_unbounded_field_count_are_rejected():
    with pytest.raises(ValueError):
        replace(IDENTITY, source="")
    with pytest.raises(ValueError):
        replace(IDENTITY, panel_bounds=(0, 0, 4000, 2000))
    with pytest.raises(ValueError):
        interval(0.8, 0.2, 1, "bad")
    with pytest.raises(ValueError):
        interval(0.1, 0.2, 1, "bad", maximum=None)
    with pytest.raises(ValueError):
        TemporalResourceGuard().check({str(i): known(i) for i in range(5)},
                                      captured_at=1, identity=IDENTITY)
