import pytest
from engine.support import (
    EquipmentPolicy,
    Threshold,
    TimedSpell,
    choose_threshold,
    parse_spells,
)


def test_unknown_percentage_never_triggers():
    assert choose_threshold(None, (Threshold(True, 80, "F1"),)) is None


def test_threshold_chooses_lowest_satisfied_regardless_of_ui_order():
    rules = (Threshold(True, 85, "F1"), Threshold(True, 60, "F2"), Threshold(True, 40, "F3"))
    assert choose_threshold(35, rules).hotkey == "F3"
    assert choose_threshold(55, rules).hotkey == "F2"
    assert choose_threshold(90, rules) is None


def test_equipment_requires_positive_empty_evidence_and_known_life():
    policy = EquipmentPolicy(True, 30)
    assert not policy.should_equip(None, 10)
    assert not policy.should_equip(True, None)
    assert not policy.should_equip(True, True)
    assert not policy.should_equip(True, -1)
    assert policy.should_equip(True, 30)
    assert not policy.should_equip(False, 1)


def test_intervals_must_be_finite_and_positive():
    for value in (True, 0, -1, float("inf"), float("nan"), "2"):
        with pytest.raises(ValueError):
            TimedSpell("F1", value)
    with pytest.raises(ValueError, match="invalid interval"):
        parse_spells((("F1", "abc"),))
    with pytest.raises(ValueError, match="invalid interval"):
        parse_spells((("F1", True),))


@pytest.mark.parametrize(
    "enabled,percent,hotkey",
    ((1, 50, "F1"), (True, True, "F1"), (True, 50.0, "F1"), (True, 50, None)),
)
def test_threshold_configuration_has_strict_types(enabled, percent, hotkey):
    with pytest.raises(ValueError):
        Threshold(enabled, percent, hotkey)


@pytest.mark.parametrize(
    "require_below,threshold",
    ((1, 50), (True, True), (True, 50.0), (True, -1), (True, 101)),
)
def test_equipment_policy_configuration_has_strict_types(require_below, threshold):
    with pytest.raises(ValueError):
        EquipmentPolicy(require_below, threshold)


@pytest.mark.parametrize("value", (True, 50.0, -1, 101, "50"))
def test_threshold_matching_rejects_non_integer_percentages(value):
    with pytest.raises(ValueError):
        choose_threshold(value, (Threshold(True, 50, "F1"),))
