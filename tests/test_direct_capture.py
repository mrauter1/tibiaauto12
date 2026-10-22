"""Direct capture and error diagnostics with substitutes; no native actions."""

from contextlib import contextmanager

from PIL import Image
import pytest

from tibia_client import direct_capture as capture
from tibia_client.window_target import WindowTarget, WindowTargetError


class Native:
    def __init__(self, failure=None):
        self.calls = []
        self.failure = failure

    @contextmanager
    def physical_pixels(self):
        self.calls.append("dpi_enter")
        try:
            yield
        finally:
            self.calls.append("dpi_exit")

    def client_rectangle(self, hwnd):
        self.calls.append(("geometry", hwnd))
        return -20, 10, 60, 70

    def grab(self, rect):
        self.calls.append(("grab", rect))
        if self.failure:
            raise self.failure
        return Image.new("RGB", (80, 60), (20, 30, 40))


def test_success_captures_once_without_any_availability_or_focus_check(monkeypatch):
    native = Native()
    monkeypatch.setattr(capture, "_DirectClientBackend", lambda: native)
    monkeypatch.setattr(capture, "diagnose_client_availability", lambda *_: pytest.fail("preflight"))
    image = capture.capture_direct_host_client(41, expected_size=(80, 60))
    assert image.size == (80, 60) and image.mode == "RGB"
    assert native.calls == ["dpi_enter", ("geometry", 41),
                            ("grab", (-20, 10, 60, 70)), "dpi_exit"]


def test_failed_capture_preserves_original_exception_and_diagnoses_after_cleanup(monkeypatch):
    failure = OSError("BitBlt failed")
    native = Native(failure)
    monkeypatch.setattr(capture, "_DirectClientBackend", lambda: native)

    def diagnose(hwnd):
        native.calls.append(("diagnose", hwnd))
        return {"is_window": False}

    monkeypatch.setattr(capture, "diagnose_client_availability", diagnose)
    with pytest.raises(OSError) as caught:
        capture.capture_direct_host_client(41)
    assert caught.value is failure
    assert failure.client_availability == {"is_window": False}
    assert native.calls[-2:] == ["dpi_exit", ("diagnose", 41)]
    assert sum(isinstance(value, tuple) and value[0] == "grab" for value in native.calls) == 1


def test_diagnostic_failure_does_not_replace_capture_error(monkeypatch):
    failure = OSError("capture failed")
    monkeypatch.setattr(capture, "_DirectClientBackend", lambda: Native(failure))

    def diagnostic_failure(hwnd):
        raise RuntimeError("diagnostic failed")

    monkeypatch.setattr(capture, "diagnose_client_availability", diagnostic_failure)
    with pytest.raises(OSError) as caught:
        capture.capture_direct_host_client(41)
    assert caught.value is failure


def test_changed_pixel_extent_is_an_error_after_capture_not_a_preflight(monkeypatch):
    native = Native()
    monkeypatch.setattr(capture, "_DirectClientBackend", lambda: native)
    monkeypatch.setattr(capture, "diagnose_client_availability", lambda hwnd: {"hwnd": hwnd})
    with pytest.raises(WindowTargetError, match="size changed") as caught:
        capture.capture_direct_host_client(41, (81, 60))
    assert native.calls[-1] == "dpi_exit"
    assert caught.value.client_availability == {"hwnd": 41}


def test_sandbox_crops_once_without_using_stored_focus_samples(monkeypatch):
    native = Native()
    monkeypatch.setattr(capture, "_DirectClientBackend", lambda: native)
    monkeypatch.setattr(capture, "diagnose_client_availability", lambda *_: pytest.fail("preflight"))
    target = WindowTarget.build((80, 60), (10, 5, 70, 55))
    image = capture.capture_direct_target(41, target)
    assert image.size == (60, 50)
    assert len([value for value in native.calls if isinstance(value, tuple) and value[0] == "grab"]) == 1


@pytest.mark.parametrize("hwnd", [None, 0, -1, True, "41"])
def test_invalid_configuration_handle_does_not_query_the_desktop(hwnd, monkeypatch):
    monkeypatch.setattr(capture, "_DirectClientBackend", lambda: pytest.fail("native API"))
    monkeypatch.setattr(capture, "diagnose_client_availability", lambda *_: pytest.fail("diagnostic"))
    with pytest.raises(ValueError):
        capture.capture_direct_host_client(hwnd)
