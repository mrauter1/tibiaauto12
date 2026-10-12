import pytest
from engine.support import (
    Cooldown,
    EquipmentPolicy,
    Threshold,
    TimedSchedule,
    TimedSpell,
    arm_input,
    choose_threshold,
    parse_spells,
)
from core.runtime import CancelledError


class Clock:
    def __init__(self, value=0):
        self.value = value

    def __call__(self):
        return self.value


def test_arming_blocks_policy_actions_until_sender_is_ready():
    events = []

    class Sender:
        def wait_until_ready(self, timeout):
            events.append(("wait", timeout))
            events.append(("ready", None))

    arm_input(Sender())
    events.append(("action", None))
    assert events == [("wait", 30.0), ("ready", None), ("action", None)]


def test_arming_cancellation_prevents_following_action():
    events = []

    class Sender:
        def wait_until_ready(self, timeout):
            events.append(("wait", timeout))
            raise CancelledError("stopped")

    with pytest.raises(CancelledError):
        arm_input(Sender())
        events.append(("action", None))
    assert events == [("wait", 30.0)]


def test_unknown_percentage_never_triggers():
    assert choose_threshold(None, (Threshold(True, 80, "F1"),)) is None


def test_threshold_chooses_lowest_satisfied_regardless_of_ui_order():
    rules = (Threshold(True, 85, "F1"), Threshold(True, 60, "F2"), Threshold(True, 40, "F3"))
    assert choose_threshold(35, rules).hotkey == "F3"
    assert choose_threshold(55, rules).hotkey == "F2"
    assert choose_threshold(90, rules) is None


def test_cooldown_is_deterministic():
    clock = Clock(10)
    gate = Cooldown(0.5, clock)
    assert gate.ready()
    gate.mark()
    assert not gate.ready()
    clock.value = 10.5
    assert gate.ready()


def test_equipment_requires_positive_empty_evidence_and_known_life():
    policy = EquipmentPolicy(True, 30)
    assert not policy.should_equip(None, 10)
    assert not policy.should_equip(True, None)
    assert policy.should_equip(True, 30)
    assert not policy.should_equip(False, 1)


def test_schedule_uses_deadlines_skips_missed_cycles_and_serializes_due_items():
    clock = Clock()
    schedule = TimedSchedule((TimedSpell("F1", 2), TimedSpell("F2", 2)), spacing=0.2, clock=clock)
    clock.value = 2
    assert schedule.due().hotkey == "F1"
    assert schedule.due() is None
    clock.value = 2.2
    assert schedule.due().hotkey == "F2"
    clock.value = 10
    assert schedule.due().hotkey == "F1"
    assert schedule.deadlines[0] == 12


def test_intervals_must_be_finite_and_positive():
    for value in (0, -1, float("inf"), float("nan")):
        with pytest.raises(ValueError):
            TimedSpell("F1", value)
    with pytest.raises(ValueError, match="invalid interval"):
        parse_spells((("F1", "abc"),))
