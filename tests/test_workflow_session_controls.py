"""Regression coverage for configuration/start races and operator generations."""
from __future__ import annotations

import threading

import pytest

from tibia_client import ClientSession, Step
from tibia_client.compatibility import ProbeFeature


class Adapter:
    def __init__(self):
        self.profiles = []

    def configure_visual_profile(self, profile):
        self.profiles.append(profile)

    def ready(self):
        raise AssertionError("configuration must not call native readiness")

    def observe(self, *args):
        raise AssertionError("configuration must not capture")

    def execute(self, *args):
        raise AssertionError("configuration must not send input")

    def close(self):
        pass


class Feature:
    name = "CaveBot"
    interval = .1

    def __init__(self):
        self.commands = []

    def sensors(self):
        return ()

    def tick(self, context):
        return Step()

    def on_result(self, result):
        pass

    def request_return(self, *, now=False):
        self.commands.append(now)

    def acknowledge_manual(self, *, operator_reconciled=False):
        return operator_reconciled

    def request_recheck(self):
        self.commands.append("recheck")

    def select_recovery_anchor(self, *, waypoint_index):
        self.commands.append(("anchor", waypoint_index))


def test_detector_install_is_idle_only_and_invalidates_prepared_starts():
    adapter = Adapter()
    session = ClientSession(adapter, autostart=False)
    epoch = session.control_epoch
    installed = session.configure_visual_profile("one", expected_stop_epoch=epoch)
    assert installed > epoch
    assert adapter.profiles == ["one"]
    with pytest.raises(RuntimeError, match="controls changed"):
        session.start_feature(Feature(), expected_stop_epoch=epoch)
    session.start_feature(Feature(), expected_stop_epoch=installed)
    with pytest.raises(RuntimeError, match="stop all features"):
        session.configure_visual_profile("two")
    assert session.pause_navigation()
    with pytest.raises(RuntimeError, match="stop all features"):
        session.configure_visual_profile("two")
    assert adapter.profiles == ["one"]
    session.close()


def test_stop_all_cancels_preparation_before_configuration_or_activation():
    adapter = Adapter()
    session = ClientSession(adapter, autostart=False)
    epoch = session.control_epoch
    session.stop_all("operator cancelled while parsing")
    with pytest.raises(RuntimeError, match="controls changed"):
        session.configure_visual_profile("late", expected_stop_epoch=epoch)
    assert adapter.profiles == []
    epoch = session.configure_visual_profile("one")
    session.stop_all("operator cancelled after configuration")
    with pytest.raises(RuntimeError, match="controls changed"):
        session.start_feature(Feature(), expected_stop_epoch=epoch)
    with pytest.raises(RuntimeError, match="controls changed"):
        session.start_probe(ProbeFeature(None, None), expected_stop_epoch=epoch)
    assert not session.is_active("CaveBot")
    session.close()


def test_inflight_old_registry_validation_cannot_commit_after_reconfiguration():
    entered, release = threading.Event(), threading.Event()
    errors = []

    def guard(_feature):
        entered.set()
        assert release.wait(2)
        return True

    session = ClientSession(Adapter(), autostart=False, activation_guard=guard)

    def start():
        try:
            session.start_feature(Feature())
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=start)
    worker.start()
    try:
        assert entered.wait(2)
        session.configure_visual_profile("replacement")
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], RuntimeError)
    assert not session.is_active("CaveBot")
    session.close()


def test_operator_commands_are_generation_bound_and_propagate_declined_intent():
    session = ClientSession(Adapter(), autostart=False)
    first, second = Feature(), Feature()
    first_generation = session.start_feature(first)
    assert session.feature_command(first.name, first_generation, "request_return", now=True)
    assert first.commands == [True]
    assert not session.feature_command(first.name, first_generation, "acknowledge_manual")
    second_generation = session.start_feature(second)
    assert not session.feature_command(first.name, first_generation, "request_return")
    assert first.commands == [True] and second.commands == []
    assert session.feature_command(second.name, second_generation, "request_return")
    assert second.commands == [False]
    session.stop_all()
    assert not session.feature_command(second.name, second_generation, "request_return")
    with pytest.raises(ValueError, match="unsupported"):
        session.feature_command(second.name, second_generation, "tick")
    session.close()


@pytest.mark.parametrize("revision", [True, -1, "0"])
def test_control_revisions_are_strict_integers(revision):
    session = ClientSession(Adapter(), autostart=False)
    with pytest.raises(ValueError, match="non-negative integer"):
        session.configure_visual_profile("bad", expected_stop_epoch=revision)
    session.close()


def test_configuration_requested_during_stop_all_cannot_wait_then_install():
    entered, release = threading.Event(), threading.Event()

    class Clock:
        blocking = False

        def monotonic(self):
            if self.blocking:
                entered.set()
                assert release.wait(2)
            return 10.0

    clock = Clock()
    adapter = Adapter()
    session = ClientSession(adapter, clock=clock, autostart=False)
    session.start_feature(Feature())
    clock.blocking = True
    worker = threading.Thread(target=session.stop_all)
    worker.start()
    try:
        assert entered.wait(2)
        with pytest.raises(RuntimeError, match="stopping"):
            session.configure_visual_profile("late", expected_stop_epoch=session.control_epoch)
    finally:
        release.set()
        worker.join(2)
        clock.blocking = False
    assert not worker.is_alive()
    assert adapter.profiles == []
    session.close()


def test_operator_command_does_not_coerce_boolean_generation():
    session = ClientSession(Adapter(), autostart=False)
    feature = Feature()
    assert session.start_feature(feature) == 1
    with pytest.raises(ValueError, match="generation"):
        session.feature_command(feature.name, True, "request_return")
    assert feature.commands == []
    session.close()


def test_recovery_commands_remain_bound_to_the_original_generation():
    session = ClientSession(Adapter(), autostart=False)
    first, replacement = Feature(), Feature()
    generation = session.start_feature(first)
    assert session.feature_command(first.name, generation, "request_recheck")
    assert session.feature_command(
        first.name, generation, "select_recovery_anchor", waypoint_index=2
    )
    assert first.commands == ["recheck", ("anchor", 2)]
    new_generation = session.start_feature(replacement)
    for command, options in (
        ("request_recheck", {}), ("select_recovery_anchor", {"waypoint_index": 2})
    ):
        assert not session.feature_command(first.name, generation, command, **options)
    assert replacement.commands == []
    session.stop_all()
    assert not session.feature_command(replacement.name, new_generation, "request_recheck")
    session.close()
