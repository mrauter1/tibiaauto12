"""Keep offline checks independent of the operator's saved reading settings."""
import pytest


@pytest.fixture(scope="session", autouse=True)
def deterministic_image_workers():
    import cv2

    previous = cv2.getNumThreads()
    cv2.setNumThreads(1)
    yield
    cv2.setNumThreads(previous)


@pytest.fixture(autouse=True)
def isolated_reading_settings(monkeypatch, tmp_path):
    from tibia_client import freshness

    monkeypatch.setattr(freshness, "scripts_path", lambda name: tmp_path / name)
    monkeypatch.setattr(freshness, "_value", freshness.DEFAULT_STALE_MS)
    monkeypatch.setattr(freshness, "_checked_at", float("-inf"))
    monkeypatch.setattr(freshness, "_signature", None)
