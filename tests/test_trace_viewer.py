import json
import os
from pathlib import Path
import queue
import subprocess
import sys
from types import SimpleNamespace

import pytest

from core.paths import PROJECT_ROOT
from tibia_client import (
    Action,
    ActionKind,
    ActionResult,
    Event,
    FeatureStatus,
    Request,
    ResultState,
)
from tibia_client.trace import JsonlRecorder, TraceError, load_trace
from tibia_client.trace_view import (
    MAX_DETAIL_CHARS,
    MAX_PRESENTATION_LIMIT,
    event_detail,
    export_session_trace,
    filter_events,
    load_trace_document,
    session_trace_document,
)


def sample_events():
    request = Request(
        "emergency-heal",
        Action(ActionKind.PRESS, key="F1"),
        priority=100,
        reason="health below configured threshold",
    )
    return (
        Event(
            1,
            10.0,
            "status",
            "AutoHeal",
            {
                "status": FeatureStatus("AutoHeal", 1, True, "Starting"),
                "reason": "started",
            },
        ),
        Event(
            2,
            10.1,
            "request",
            "AutoHeal",
            {"request": request, "request_id": "AutoHeal:1:1", "generation": 1},
        ),
        Event(
            3,
            10.2,
            "dispatch",
            "AutoHeal",
            {
                "request_id": "AutoHeal:1:1",
                "generation": 1,
                "key": "emergency-heal",
                "action": "press",
            },
        ),
        Event(
            4,
            10.3,
            "result",
            "AutoHeal",
            {
                "result": ActionResult(
                    "AutoHeal:1:1",
                    "AutoHeal",
                    "emergency-heal",
                    ResultState.CONFIRMED,
                    10.3,
                    "health changed",
                ),
                "generation": 1,
                "deliver": True,
            },
        ),
    )


def write_trace(path, events=None):
    events = sample_events() if events is None else events
    with JsonlRecorder(path) as recorder:
        for event in events:
            recorder.record(event)


def test_load_filter_summaries_and_statistics(tmp_path):
    path = tmp_path / "session.jsonl"
    write_trace(path)

    document = load_trace_document(path)
    assert document.statistics.total_events == 4
    assert document.statistics.duration == pytest.approx(0.3)
    assert document.statistics.requests == 1
    assert document.statistics.dispatches == 1
    assert document.statistics.results == {"confirmed": 1}

    filtered = filter_events(document.events, owner="heal", request="emergency")
    assert filtered.matched == 3
    assert [row.event.kind for row in filtered.rows] == ["request", "dispatch", "result"]
    assert filtered.rows[-1].summary == "emergency-heal: confirmed; final; health changed"
    assert filter_events(document.events, kind="STATUS").matched == 1
    assert filter_events(document.events, request="confirmed").matched == 1


def test_presentation_is_bounded_and_detail_is_inert():
    events = tuple(Event(index, float(index), "tick", data={}) for index in range(1, 12))
    filtered = filter_events(events, limit=3)
    assert filtered.matched == 11
    assert len(filtered.rows) == 3
    assert filtered.truncated
    with pytest.raises(ValueError, match="limit"):
        filter_events(events, limit=MAX_PRESENTATION_LIMIT + 1)

    suspicious = Event(
        1,
        0.0,
        "note",
        data={"path": "/tmp/should-not-open", "message": "x" * (MAX_DETAIL_CHARS * 2)},
    )
    detail = event_detail(suspicious)
    assert "/tmp/should-not-open" in detail
    assert len(detail) <= MAX_DETAIL_CHARS
    assert "detail truncated" in detail or "text truncated" in detail


def test_strict_reader_errors_are_preserved(tmp_path):
    path = tmp_path / "many.jsonl"
    write_trace(path)
    with pytest.raises(TraceError, match="event limit"):
        load_trace_document(path, max_events=2)

    malformed = tmp_path / "malformed.jsonl"
    malformed.write_text(
        '{"format":"tibia-client-trace","version":1,"mode":"observations"}\n'
        '{"not":"an event"}\n',
        encoding="utf-8",
    )
    with pytest.raises(TraceError):
        load_trace_document(malformed)

    oversized = tmp_path / "oversized.jsonl"
    oversized.write_bytes(b"x" * 1100)
    with pytest.raises(TraceError):
        load_trace_document(oversized, max_bytes=1024)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO is not available")
def test_nonregular_trace_source_is_rejected_without_blocking(tmp_path):
    fifo = tmp_path / "trace.pipe"
    os.mkfifo(fifo)
    with pytest.raises(TraceError, match="regular file"):
        load_trace_document(fifo)
    with pytest.raises(TraceError, match="regular file"):
        load_trace(fifo)


@pytest.mark.skipif(os.name == "nt", reason="Windows locks open files against replacement")
def test_trace_reader_keeps_the_validated_file_when_its_path_is_replaced(tmp_path, monkeypatch):
    import tibia_client.trace as trace

    path = tmp_path / "session.jsonl"
    replacement = tmp_path / "replacement.jsonl"
    write_trace(path)
    write_trace(replacement, ())
    real_fstat = os.fstat
    replaced = []

    def replacing_fstat(descriptor):
        info = real_fstat(descriptor)
        if not replaced:
            replacement.replace(path)
            replaced.append(True)
        return info

    monkeypatch.setattr(trace.os, "fstat", replacing_fstat)
    assert load_trace_document(path).events == sample_events()
    assert load_trace(path) == ()


class FakeSession:
    def __init__(self, events):
        self._events = tuple(events)

    def events(self):
        return self._events


def test_refresh_session_supersedes_an_older_pending_file_load():
    from modules.TraceViewer import TraceViewer

    viewer = object.__new__(TraceViewer)
    viewer.session = FakeSession(sample_events())
    viewer._load_serial = 1
    viewer._load_results = queue.SimpleQueue()
    older = session_trace_document(FakeSession(()))
    viewer._load_results.put((1, Path("old.jsonl"), older, None))
    displayed = []
    viewer._set_document = lambda doc, label: displayed.append((doc, label))
    viewer.window = SimpleNamespace(after=lambda *args: None)

    assert viewer.refresh_session()
    viewer._poll_load()

    assert len(displayed) == 1
    assert displayed[0][0].events == sample_events()
    assert displayed[0][1] == "Live session history (snapshot)"


def test_session_snapshot_and_export_use_valid_trace_api(tmp_path):
    session = FakeSession(sample_events())
    assert session_trace_document(session).statistics.total_events == 4

    destination = tmp_path / "export.jsonl"
    assert export_session_trace(session, destination) == 4
    assert load_trace(destination) == sample_events()


def test_session_export_rejects_rolled_off_history_before_creating_file(tmp_path):
    destination = tmp_path / "must-not-exist.jsonl"
    session = FakeSession(sample_events()[1:])
    assert session_trace_document(session).events[0].sequence == 2
    with pytest.raises(TraceError, match="event 1 is no longer retained"):
        export_session_trace(session, destination)
    assert not destination.exists()


def test_cli_json_filters_and_imports_no_native_backend(tmp_path):
    path = tmp_path / "session.jsonl"
    write_trace(path)
    script = """
import json
import sys
from tools.trace_viewer import main
code = main([sys.argv[1], '--kind', 'result', '--json'])
assert code == 0
assert not {'tibia_client.desktop', 'core.HookWindow', 'core.MoveMouse', 'pyautogui', 'win32gui'} & set(sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(path)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    report = json.loads(result.stdout)
    assert report["ok"] is True
    assert report["matched"] == 1
    assert report["events"][0]["kind"] == "result"


def test_cli_reports_malformed_trace_without_traceback(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text("not json\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "tools.trace_viewer", str(path), "--json"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert result.stderr == ""
    report = json.loads(result.stdout)
    assert report["ok"] is False
    assert "error" in report


def test_gui_module_import_is_native_free():
    script = """
import sys
import modules.TraceViewer
assert not {'tibia_client.desktop', 'core.HookWindow', 'core.MoveMouse', 'pyautogui', 'win32gui'} & set(sys.modules)
"""
    subprocess.run([sys.executable, "-c", script], cwd=PROJECT_ROOT, check=True)
