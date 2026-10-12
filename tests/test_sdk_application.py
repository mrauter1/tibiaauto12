from __future__ import annotations

import pytest

from tibia_client.application import close_client, configure_client, get_client


class Adapter:
    def __init__(self):
        self.closed = 0

    def ready(self):
        return True

    def observe(self, sensors, token):
        raise AssertionError("test does not capture")

    def execute(self, action, token):
        raise AssertionError("test does not send input")

    def close(self):
        self.closed += 1


@pytest.fixture(autouse=True)
def _close_shared_client():
    close_client()
    yield
    close_client()


def test_failed_reconfiguration_preserves_active_client_and_closes_rejected_adapter():
    first_adapter = Adapter()
    first = configure_client(0, adapter=first_adapter, autostart=False)
    rejected = Adapter()

    with pytest.raises(ValueError, match="tick interval"):
        configure_client(0, adapter=rejected, tick_interval=0, autostart=False)

    assert get_client() is first
    assert first_adapter.closed == 0
    assert rejected.closed == 1


def test_successful_reconfiguration_closes_previous_client_once():
    first_adapter = Adapter()
    configure_client(0, adapter=first_adapter, autostart=False)
    second_adapter = Adapter()
    second = configure_client(1, adapter=second_adapter, autostart=False)

    assert get_client() is second
    assert first_adapter.closed == 1
    assert second_adapter.closed == 0
