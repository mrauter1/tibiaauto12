"""Offline monitor acceptance: no game window and no native input."""
from dataclasses import replace
import io
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import numpy as np
from PIL import Image
import pytest

from core.perception import CaptureError, Frame
from tibia_client.adaptive import FIELDS
from tibia_client.models import Quality, Reading
from tibia_client.resource_monitor import (
    MonitorState, ResourceMonitor, Sample, WindowSource, create_capture,
    create_reader, display_readings,
)


def picture(size=(20, 12)):
    return Frame(Image.new("RGB", size, (42, 42, 42)))


def known(value=710):
    return Reading({"count": value}, diagnostics={"effective_region": (1, 1, 10, 8)})


def sample(readings=None, **kwargs):
    return Sample(1, picture(), readings or {name: known() for name in FIELDS},
                  kwargs.get("observed_at", 10.0), 20, 0, 40, 60)


def await_condition(predicate, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.005)
    pytest.fail("Monitor did not reach the expected state")


class Capture:
    def __init__(self, frames=None):
        self.frames = iter(frames) if frames else None
        self.closed = False
        self.count = 0
        self.last_observed_at = None

    def capture(self, token):
        token.checkpoint()
        self.count += 1
        self.last_observed_at = time.monotonic()
        item = next(self.frames) if self.frames else picture()
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True


class Reader:
    def __init__(self):
        self.discoveries = 0
        self.reads = 0

    def discover(self, frame, *, checkpoint, use_ocr):
        assert not use_ocr
        checkpoint()
        self.discoveries += 1
        return SimpleNamespace(layout=object(), detail="")

    def read(self, frame, layout, *, use_ocr):
        assert not use_ocr
        self.reads += 1
        return {name: known(self.reads) for name in FIELDS}


@pytest.fixture
def launch():
    monitors = []

    def make(capture=None, reader=None, **kwargs):
        capture = capture or Capture()
        reader = reader or Reader()
        monitor = ResourceMonitor(lambda: capture, lambda: reader, interval=0.01, **kwargs)
        monitors.append(monitor)
        monitor.start()
        return monitor, capture, reader

    yield make
    for monitor in monitors:
        monitor.close()
        assert monitor.wait(), "Monitor did not clean up"


def test_freshness_expires_without_another_capture_and_rejects_future_time():
    state = MonitorState("Monitoring", sample(), running=True)
    assert display_readings(state, 10.04)[0].value == "710"
    assert display_readings(state, 10.04)[0].status == "Accepted"
    assert display_readings(state, 10.04)[0].age_ms == pytest.approx(40)
    for now in (10.501, 9.99, float("nan")):
        assert all(row.value == "N/A" and row.status == "Stale"
                   for row in display_readings(state, now))
    stopped = replace(state, phase="Stopped", running=False)
    assert all(row.value == "N/A" for row in display_readings(stopped, 10.04))


def test_unknown_error_and_malformed_counts_never_display_a_previous_value():
    readings = {
        FIELDS[0]: Reading({"count": 999}, Quality.UNKNOWN, "obscured"),
        FIELDS[1]: Reading({"count": 999}, Quality.ERROR, "failed"),
        FIELDS[2]: known(True),
        FIELDS[3]: known(-1),
    }
    rows = display_readings(MonitorState("Monitoring", sample(readings), running=True), 10.1)
    assert [row.status for row in rows] == ["N/A", "Error", "N/A", "N/A"]
    assert all(row.value == "N/A" for row in rows)
    assert rows[0].detail == "obscured"


def test_invalid_crop_does_not_crash_presentation():
    readings = {name: Reading({"count": 0}, diagnostics={"effective_region": (-1, 0, 99, 99)})
                for name in FIELDS}
    rows = display_readings(MonitorState("Monitoring", sample(readings), running=True), 10.1)
    assert all(row.region is None and row.value == "0" for row in rows)


def test_only_latest_frame_is_retained_and_each_frame_is_decoded(launch):
    monitor, capture, reader = launch()
    await_condition(lambda: (monitor.state().sample or sample()).sequence >= 3)
    current = monitor.state().sample
    assert current.readings[FIELDS[0]].value["count"] == current.sequence
    assert current.total_ms >= current.capture_ms + current.decode_ms
    assert reader.discoveries == 1
    assert capture.count >= 3
    assert not monitor.start()
    monitor.stop()
    assert monitor.state().sample is None
    assert monitor.wait()
    assert capture.closed


@pytest.mark.parametrize("failure", [
    CaptureError("window closed"),
    Frame(np.zeros((12, 20, 3), dtype=np.uint8)),
    picture((21, 12)),
])
def test_failed_black_or_resized_capture_clears_previously_accepted_values(launch, failure):
    monitor, _, _ = launch(Capture([picture(), failure]))
    await_condition(lambda: monitor.state().phase == "Error")
    state = monitor.state()
    assert state.sample is None and not state.running
    assert all(row.value == "N/A" for row in display_readings(state, time.monotonic()))


def test_stop_during_decode_rejects_late_result_and_prevents_overlapping_restart(launch):
    entered, release = threading.Event(), threading.Event()

    class SlowReader(Reader):
        def read(self, *args, **kwargs):
            entered.set()
            assert release.wait(3)
            return super().read(*args, **kwargs)

    monitor, capture, _ = launch(reader=SlowReader())
    assert entered.wait(3)
    monitor.stop()
    assert monitor.state().sample is None
    assert not monitor.start()
    release.set()
    assert monitor.wait()
    assert monitor.state().phase == "Stopped"
    assert monitor.state().sample is None
    assert capture.closed


def test_stop_interrupts_capture_and_discovery_cooperatively(launch):
    entered = threading.Event()

    class SlowDiscovery(Reader):
        def discover(self, frame, *, checkpoint, use_ocr):
            entered.set()
            while True:
                checkpoint()
                time.sleep(0.001)

    monitor, capture, _ = launch(reader=SlowDiscovery())
    assert entered.wait(3)
    monitor.close()
    assert monitor.wait()
    assert capture.closed
    assert monitor.state().sample is None
    with pytest.raises(RuntimeError, match="closed"):
        monitor.start()


def test_ambiguous_discovery_abstains_and_retries_at_a_bounded_rate(launch):
    class Ambiguous(Reader):
        def discover(self, *args, **kwargs):
            super().discover(*args, **kwargs)
            return SimpleNamespace(layout=None, detail="resource panel is ambiguous")

    monitor, _, reader = launch(reader=Ambiguous(), rediscovery_interval=0.08)
    await_condition(lambda: reader.discoveries >= 2)
    monitor.stop()
    assert monitor.wait()
    assert reader.reads == 0
    assert reader.discoveries < 5


def test_missing_rows_trigger_rediscovery(launch):
    class Missing(Reader):
        def read(self, *args, **kwargs):
            readings = super().read(*args, **kwargs)
            readings[FIELDS[-1]] = Reading(None, Quality.UNKNOWN, "row hidden")
            return readings

    monitor, _, reader = launch(reader=Missing(), rediscovery_interval=0.04)
    await_condition(lambda: reader.discoveries >= 2)
    assert monitor.state().sample.readings[FIELDS[-1]].quality is Quality.UNKNOWN


def test_capture_target_is_pinned_without_writing_config(monkeypatch):
    import tibia_client.resource_monitor as module
    import tibia_client.capture as captures

    original = '{"platform":"windows","capture_hwnd":123,"capture_size":[800,600]}'
    modes = []

    def open_config(mode, *, encoding):
        modes.append(mode)
        assert mode == "r" and encoding == "utf-8"
        return io.StringIO(original)

    config = SimpleNamespace(open=open_config)
    monkeypatch.setattr(module, "scripts_path", lambda _: config)
    monkeypatch.setattr(captures, "CaptureProcess", lambda provider, **kw: (provider, kw))
    source, options = create_capture()
    assert isinstance(source, WindowSource)
    assert source.hwnd == 123 and options["capture_size"] == [800, 600]
    assert modes == ["r"]
    explicit, options = create_capture(456)
    assert explicit.hwnd == 456 and options["capture_size"] is None


def test_imports_do_not_initialize_capture_or_input_and_cli_help_is_headless():
    code = """
import sys
import modules.ResourceMonitor
import tools.resource_monitor
assert 'tibia_client.desktop' not in sys.modules
assert 'core.Hotkey' not in sys.modules
assert 'win32gui' not in sys.modules
assert 'keyboard' not in sys.modules
assert 'pyautogui' not in sys.modules
"""
    subprocess.run([sys.executable, "-c", code], check=True, timeout=30)
    result = subprocess.run(
        [sys.executable, "-m", "tools.resource_monitor", "--help"],
        check=True, capture_output=True, text=True, timeout=30,
    )
    assert "--hwnd" in result.stdout


def test_selected_decoder_reads_fixture_and_abstains_after_value_is_obscured(launch):
    from tools.resource_reader import CurrentResourceReader

    reader = create_reader()
    assert type(reader) is CurrentResourceReader
    path = Path(__file__).parent / "fixtures/resources/paulistinha-skills.png"
    with Image.open(path) as image:
        frame = Frame(image)
    layout = reader.discover(frame, use_ocr=False).layout
    assert layout is not None
    assert [r.value["count"] for r in reader.read(frame, layout).values()] == [710, 1085, 4, 192]
    pixels = frame.pixels.copy()
    x1, y1, x2, y2 = layout.regions[FIELDS[0]]
    pixels[y1:y2, x1:x2] = 42
    changed = Frame(pixels)

    class Sequence(Capture):
        def capture(self, token):
            super().capture(token)
            return frame if self.count == 1 else changed

    monitor, _, _ = launch(Sequence(), reader)
    await_condition(lambda: monitor.state().sample is not None, timeout=20)
    first = monitor.state().sample
    # Subsequent captures may already have replaced the first. Check the live
    # loop's output after changing its pixels without sharing the reader thread.
    await_condition(lambda: monitor.state().sample.sequence >= max(2, first.sequence), timeout=20)
    assert monitor.state().sample.readings[FIELDS[0]].quality is Quality.UNKNOWN
    assert monitor.state().sample.readings[FIELDS[0]].value is None


def test_stop_cancels_a_blocked_capture_without_starting_discovery(launch):
    entered = threading.Event()

    class SlowCapture(Capture):
        def capture(self, token):
            entered.set()
            while True:
                token.checkpoint()
                time.sleep(0.001)

    monitor, capture, reader = launch(SlowCapture())
    assert entered.wait(3)
    monitor.stop()
    assert monitor.wait()
    assert capture.closed
    assert reader.discoveries == reader.reads == 0


def test_stale_discovery_result_is_not_accepted_as_current(launch):
    class HoldSecondCapture(Capture):
        def capture(self, token):
            if self.count:
                while True:
                    token.checkpoint()
                    time.sleep(0.001)
            return super().capture(token)

    class SlowDiscovery(Reader):
        def discover(self, *args, **kwargs):
            time.sleep(0.55)
            return super().discover(*args, **kwargs)

    monitor, _, _ = launch(HoldSecondCapture(), SlowDiscovery())
    await_condition(lambda: monitor.state().sample is not None)
    state = monitor.state()
    assert state.sample.sequence == 1
    rows = display_readings(state, time.monotonic())
    assert all(row.value == "N/A" and row.status == "Stale" for row in rows)
    assert state.sample.discovery_ms >= 500


def test_monitor_window_shows_values_crops_staleness_and_clears_on_stop():
    import tkinter as tk
    from modules.ResourceMonitor import ResourceMonitorWindow

    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("Tk display is unavailable")
    root.withdraw()

    class ViewMonitor:
        busy = False
        starts = 0
        current = MonitorState()

        def state(self):
            return self.current

        def start(self):
            self.starts += 1

        def stop(self):
            self.current = MonitorState()

        close = stop

    monitor = ViewMonitor()
    now = [10.04]
    ui = ResourceMonitorWindow(root, monitor=monitor, clock=lambda: now[0])
    ui.window.withdraw()

    def refresh():
        ui.window.after_cancel(ui._after)
        ui._refresh()

    try:
        assert monitor.starts == 0
        monitor.current = MonitorState("Monitoring", sample(), running=True)
        refresh()
        assert str(ui.table.item(FIELDS[0], "values")[1]) == "710"
        assert ui.table.item(FIELDS[0], "values")[2] == "Accepted"
        assert len(ui._photos) == 5  # one matching frame plus four crops
        now[0] = 10.6
        refresh()
        assert ui.table.item(FIELDS[0], "values")[1:3] == ("N/A", "Stale")
        assert "STALE" in ui.preview_title.get()
        assert "N/A" in ui.crop_boxes[FIELDS[0]].cget("text")
        ui.stop()
        refresh()
        assert ui._photos == []
        assert ui.table.item(FIELDS[0], "values")[1:3] == ("N/A", "Stopped")
    finally:
        ui.close()
        root.destroy()
