"""Experimental desktop probe. It never configures or starts the bot.

Launch with ``python -m tools.desktop_probe``. Capture transport, native input
dispatch, and visually confirmed game acceptance are separate evidence.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from numbers import Real
import os
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk
import uuid

from PIL import Image, ImageTk

from core.paths import PROJECT_ROOT


_stop_key_state = None
_CAPTURE_CHOICES = ("printwindow", "printwindow_physical", "visible_screen",
                    "visible_screen_overlay_aware", "visible_region_gdi_overlay_aware",
                    "direct_visible_region_gdi")
_BACKEND_CHOICES = ("physical", "guarded_physical", "messages", "guarded_physical_overlay_aware",
                    "guarded_region_gdi_overlay_aware", "frame_lease_region_gdi_overlay_aware",
                    "error_only_physical")
_LEASE_BACKEND = "frame_lease_region_gdi_overlay_aware"
_ERROR_ONLY_BACKEND = "error_only_physical"


def _countdown_seconds(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 30:
        raise ValueError("probe countdown must be an integer from 0 to 30 seconds")
    return value


def _other_screen_bounds(window, work_areas):
    areas = list(dict.fromkeys(tuple(area) for area in work_areas))
    if len(areas) < 2:
        raise ValueError("Only one distinct screen was detected")
    left, top, right, bottom = window

    def overlap(area):
        return max(0, min(right, area[2]) - max(left, area[0])) * max(
            0, min(bottom, area[3]) - max(top, area[1]))

    current = max(range(len(areas)), key=lambda index: overlap(areas[index]))
    target = areas[(current + 1) % len(areas)]
    return (target[0] + 8, target[1] + 8,
            min(right - left, target[2] - target[0] - 16),
            min(bottom - top, target[3] - target[1] - 16))


def _move_probe_to_other_screen(root):
    """Move this app's own window; never inspect or move the game window."""
    import ctypes
    from ctypes import wintypes

    class MonitorInfo(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("monitor", wintypes.RECT),
                    ("work", wintypes.RECT), ("flags", wintypes.DWORD)]

    api = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE, wintypes.HDC,
                                      ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    api.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), callback_type, wintypes.LPARAM]
    api.EnumDisplayMonitors.restype = wintypes.BOOL
    api.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
    api.GetMonitorInfoW.restype = wintypes.BOOL
    api.GetAncestor.argtypes, api.GetAncestor.restype = [wintypes.HWND, wintypes.UINT], wintypes.HWND
    api.GetWindowRect.argtypes, api.GetWindowRect.restype = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL
    api.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT]
    api.SetWindowPos.restype = wintypes.BOOL
    areas = []

    def collect(monitor, dc, rect, data):
        info = MonitorInfo()
        info.size = ctypes.sizeof(info)
        if api.GetMonitorInfoW(monitor, ctypes.byref(info)):
            areas.append((info.work.left, info.work.top, info.work.right, info.work.bottom))
        return True

    if not api.EnumDisplayMonitors(None, None, callback_type(collect), 0):
        raise OSError("Could not enumerate screen work areas")
    root.update_idletasks()
    hwnd = api.GetAncestor(root.winfo_id(), 2)
    rect = wintypes.RECT()
    if not hwnd or not api.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise OSError("Could not read the probe window position")
    bounds = _other_screen_bounds((rect.left, rect.top, rect.right, rect.bottom), areas)
    if not api.SetWindowPos(hwnd, None, *bounds, 0x0014):
        raise OSError("Could not move the probe to the other screen")
    return {"work_areas": areas, "probe_bounds": bounds}


def _activate_input_target(hwnd):
    """One focus action from the Arm-button owner, not an availability preflight."""
    from core.MoveMouse import MoveMouse

    MoveMouse(hwnd_resolver=lambda: hwnd).Activate()
_OPERATION_CHOICES = ("ctrl_o", "escape", "left_click", "right_click", "move")
_DEFAULT_RESPONSES = {"ctrl_o": "Channel chooser opens", "escape": "Local dialog/menu closes",
                      "move": "Destination cursor coordinates match the selected point; no click"}


def _response_text(value):
    if (not isinstance(value, str) or not value.strip() or len(value.strip()) > 500
            or not value.strip().isprintable()):
        raise ValueError("expected response must be 1–500 printable characters")
    return value.strip()


def _validated_defaults(capture_method="visible_screen", backend="physical", operation="ctrl_o",
                        expected_response=None):
    for name, value, choices in (("capture_method", capture_method, _CAPTURE_CHOICES),
                                 ("backend", backend, _BACKEND_CHOICES),
                                 ("operation", operation, _OPERATION_CHOICES)):
        if not isinstance(value, str) or value not in choices:
            raise ValueError(f"unsupported initial {name}: {value!r}")
    return {"capture_method": capture_method, "backend": backend, "operation": operation,
            "expected_response": (_DEFAULT_RESPONSES.get(operation, "") if expected_response is None
                                  else _response_text(expected_response))}


def _preview_age(observed, now):
    if (isinstance(observed, bool) or not isinstance(observed, Real)
            or not math.isfinite(float(observed)) or float(observed) < 0
            or not math.isfinite(float(now)) or float(observed) > float(now)):
        raise ValueError("capture the selected window again (preview expired: acquisition time is unknown or future)")
    return float(now) - float(observed)


def _requested_handle(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 2**64:
        raise ValueError("target HWND must be a positive integer smaller than 2**64")
    return value


def _handle_argument(value):
    return _requested_handle(int(value, 16 if value.lower().startswith("0x") else 10))


def _input_startup_seconds(value):
    if (isinstance(value, bool) or not isinstance(value, Real)
            or not math.isfinite(float(value)) or not 0 < float(value) <= 60):
        raise ValueError("input startup timeout must be finite, positive, and at most 60 seconds")
    return float(value)


def _known_dialog_config(path):
    from tibia_client.probe_known_dialog import DEFAULT_THRESHOLD, load_template_report

    template, threshold, provenance = load_template_report(path)
    if template.name != "open_channel" or threshold != DEFAULT_THRESHOLD:
        raise ValueError("frame-lease experiment requires the frozen Open Channel template report")
    return {"path": str(Path(path).resolve()), "name": template.name,
            "threshold": threshold, "provenance": provenance}


class StopToken:
    def __init__(self):
        self.event = threading.Event()

    def checkpoint(self):
        if self.event.is_set():
            raise RuntimeError("probe stopped by operator")

    @property
    def cancelled(self):
        return self.event.is_set()


def _load_stop_state_reader():
    """Resolve one read-only, nonblocking API without keyboard hooks or WMI."""
    if os.name != "nt":
        raise RuntimeError("Windows emergency-stop key polling is unavailable on this OS")
    import ctypes

    api = ctypes.WinDLL("user32", use_last_error=True)
    reader = api.GetAsyncKeyState
    reader.argtypes = [ctypes.c_int]
    reader.restype = ctypes.c_short
    return reader


def emergency_stop_pressed():
    """Read Ctrl+Shift+F12's current high bits, including while hidden.

    Exactly three nonblocking calls are made. The shared low 'pressed since'
    bits are ignored. API availability failures propagate so arming is rejected
    and a running probe is cancelled by the existing UI fail-closed paths.
    """
    global _stop_key_state

    if os.name != "nt":
        raise RuntimeError("Windows emergency-stop key polling is unavailable on this OS")
    try:
        if _stop_key_state is None:
            _stop_key_state = _load_stop_state_reader()
        states = [int(_stop_key_state(key)) for key in (0x11, 0x10, 0x7B)]
    except Exception as exc:
        _stop_key_state = None
        raise RuntimeError(f"Windows emergency-stop key polling is unavailable: {exc}") from exc
    return all(state & 0x8000 for state in states)


def load_target_report(path):
    """Read only a reproducible Sandbox calibration; never restore arming state."""
    from tibia_client.window_target import WindowTarget

    path = Path(path).resolve()
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("target report exceeds the 16 MiB size limit")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("target report must contain a JSON object")
    reports = data.get("reports", [data])
    if not isinstance(reports, list) or len(reports) > 10000 or any(
            not isinstance(report, dict) for report in reports):
        raise ValueError("target report entries must be JSON objects")
    for report in reversed(reports):
        candidates = []
        if report.get("window_target") is not None:
            candidates.append(report)
        events = report.get("events", [])
        if not isinstance(events, list):
            raise ValueError("target report events must be an array")
        candidates.extend(event for event in events if isinstance(event, dict)
                          and event.get("stage") == "calibrated_target"
                          and event.get("window_target") is not None)
        if not candidates:
            continue
        selection = report.get("selection", {})
        if not isinstance(selection, dict):
            raise ValueError("target report selection must be an object")
        hwnd = report.get("hwnd", selection.get("hwnd"))
        if isinstance(hwnd, bool) or not isinstance(hwnd, int) or not 0 < hwnd < 2**64:
            raise ValueError("target report must identify a positive HWND")
        selection_hwnd = selection.get("hwnd", hwnd)
        if isinstance(selection_hwnd, bool) or not isinstance(selection_hwnd, int) or selection_hwnd != hwnd:
            raise ValueError("target report HWND disagrees with its selection")
        parsed = []
        for candidate in candidates:
            target = WindowTarget.from_config({"window_target": candidate["window_target"]})
            if target.kind != "sandbox":
                raise ValueError("target report must contain a calibrated Sandbox target")
            digest = candidate.get("target_fingerprint")
            if not isinstance(digest, str) or digest != target.fingerprint:
                raise ValueError("target report calibration fingerprint is missing or mismatched")
            parsed.append(target)
        if len({target.fingerprint for target in parsed}) != 1:
            raise ValueError("target report contains conflicting calibration metadata")
        target = parsed[0]
        return {"source": str(path), "hwnd": hwnd, "window_target": target.to_dict(),
                "target_fingerprint": target.fingerprint}
    raise ValueError("target report contains no calibrated Sandbox target")


class DesktopProbe:
    def __init__(self, output_dir, *, target_report=None, capture_method="direct_visible_region_gdi",
                 backend="error_only_physical", operation="ctrl_o", expected_response=None,
                 reacquire_control=False, target_hwnd=None, known_dialog_report=None,
                 expected_dialog=None, input_startup_timeout=15, countdown_seconds=None, key_hold_ms=0,
                 other_screen=False, trace_key_target=False):
        from tibia_client.desktop_probe import inventory

        defaults = _validated_defaults(capture_method, backend, operation, expected_response)
        startup_seconds = _input_startup_seconds(input_startup_timeout)
        self.countdown_seconds = _countdown_seconds(3 if countdown_seconds is None else countdown_seconds)
        self.input_countdown_seconds = 0 if countdown_seconds is None else self.countdown_seconds
        if type(key_hold_ms) is not int or not 0 <= key_hold_ms <= 250:
            raise ValueError("probe key hold must be 0..250 milliseconds")
        self.key_hold_ms = key_hold_ms
        self.trace_key_target = trace_key_target
        if type(reacquire_control) is not bool:
            raise ValueError("reacquire_control must be an explicit boolean mode selection")
        if target_hwnd is not None:
            target_hwnd = _requested_handle(target_hwnd)
        saved_target = None if target_report is None else load_target_report(target_report)
        known_config = None if known_dialog_report is None else _known_dialog_config(known_dialog_report)
        if expected_dialog is not None and (
                backend != _LEASE_BACKEND or operation != "escape" or expected_dialog != "open_channel"):
            raise ValueError("expected-dialog applies only to explicit frame-lease Escape for open_channel")
        self.output = Path(output_dir).resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        self.root = tk.Tk()
        self.root.title("TibiaAuto12 experimental desktop probe")
        self.root.geometry("1050x820")
        self.root.minsize(800, 650)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.closed = False
        self.busy = False
        self.keep_visible = False
        self._withdrawn_for_probe = False
        self.token = None
        self.results = queue.Queue()
        self.windows = {}
        self.requested_hwnd = target_hwnd
        self.manual_target = self.manual_target_label = None
        self.known_dialog_report = None if known_config is None else known_config["path"]
        self.target = None
        self.target_hwnd = None
        self.point = None
        self.frame = None
        self.preview_hwnd = None
        self.preview_loaded_at = None
        self.preview_source = None
        self.photo = None
        self.last_input = None
        self.reports = []
        self.input_blocked = False
        self._armed_mouse_settings = None
        self.created_utc = datetime.now(timezone.utc).isoformat()
        self._inventory = inventory
        self._emergency_stop = emergency_stop_pressed

        body = ttk.Frame(self.root, padding=10)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Experimental probe — no bot modules or profile changes",
                  font=("TkDefaultFont", 13, "bold")).pack(anchor="w")
        selector = ttk.Frame(body)
        selector.pack(fill="x", pady=6)
        self.choice = tk.StringVar()
        self.selector = ttk.Combobox(selector, textvariable=self.choice, state="readonly")
        self.selector.pack(side="left", fill="x", expand=True)
        self.selector.bind("<<ComboboxSelected>>", self.changed_target)
        ttk.Button(selector, text="Refresh windows", command=self.refresh).pack(side="left", padx=5)
        ttk.Button(selector, text="Other screen", command=self.move_to_other_screen).pack(side="left")

        capture = ttk.Frame(body)
        capture.pack(fill="x")
        self.method = tk.StringVar(value=defaults["capture_method"])
        ttk.Combobox(capture, textvariable=self.method,
                     values=_CAPTURE_CHOICES,
                     state="readonly", width=18).pack(side="left")
        ttk.Button(capture, text=f"Capture 5 frames ({self.countdown_seconds}s)", command=self.capture).pack(side="left", padx=4)
        ttk.Button(capture, text=f"Compare all methods ({self.countdown_seconds}s)",
                   command=lambda: self.capture(both=True)).pack(side="left", padx=4)
        ttk.Button(capture, text="Sandbox viewport / focus samples…",
                   command=self.configure_target).pack(side="left", padx=4)
        ttk.Label(body, text="The probe stays visible on the other screen; otherwise it hides while running. "
                  "Error-only input activates the selected client once when armed. "
                  "For capture, bring the client forward during the countdown. Ctrl+Shift+F12 stops a running probe. "
                  "Select only a harmless control in the captured preview.",
                  wraplength=1000).pack(fill="x", pady=5)
        self.canvas = tk.Canvas(body, height=350, background="#19202b", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _e: self.render())
        self.canvas.bind("<Button-1>", self.select_point)

        controls = ttk.Frame(body)
        controls.pack(fill="x", pady=5)
        self.backend = tk.StringVar(value=defaults["backend"])
        ttk.Label(controls, text="Native backend:").pack(side="left")
        ttk.Combobox(controls, textvariable=self.backend,
                     values=_BACKEND_CHOICES,
                     state="readonly", width=20).pack(side="left", padx=3)
        self.operation = tk.StringVar(value=defaults["operation"])
        operations = ttk.Combobox(controls, textvariable=self.operation,
                     values=_OPERATION_CHOICES,
                     state="readonly", width=14)
        operations.pack(side="left", padx=3)
        operations.bind("<<ComboboxSelected>>", self.operation_changed)
        self.point_label = tk.StringVar(value="No selected point")
        ttk.Label(controls, textvariable=self.point_label).pack(side="left", padx=5)
        self.safe = tk.BooleanVar(value=False)
        ttk.Checkbutton(body, variable=self.safe,
                        text="Tibia is logged in; the selected operation uses a harmless local control"
                        ).pack(anchor="w")
        self.reacquire_control = tk.BooleanVar(value=reacquire_control)
        ttk.Checkbutton(body, variable=self.reacquire_control,
                        text="Reacquire and verify selected control before testing (stable template; up to 300s)"
                        ).pack(anchor="w")
        self.arm_on_point = tk.BooleanVar(value=False)
        ttk.Checkbutton(body, variable=self.arm_on_point, command=self.arm_next_point,
                        text="Arm ONE mouse operation on the next preview click"
                        ).pack(anchor="w")
        response = ttk.Frame(body)
        response.pack(fill="x", pady=3)
        ttk.Label(response, text="Expected visible response:").pack(side="left")
        self.expected_response = tk.StringVar(value=defaults["expected_response"])
        ttk.Entry(response, textvariable=self.expected_response).pack(side="left", fill="x", expand=True)
        self.expected_dialog = tk.StringVar(value=expected_dialog or "")
        ttk.Label(response, text="Recognized Escape dialog:").pack(side="left", padx=4)
        ttk.Entry(response, textvariable=self.expected_dialog, width=15).pack(side="left")
        if known_config is not None:
            ttk.Label(body, text=f"Frame-lease known-dialog config: {known_config['path']}",
                      wraplength=1000).pack(anchor="w")

        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=4)
        ttk.Button(actions, text="Arm ONE input operation", command=self.input).pack(side="left")
        self.expected = tk.StringVar(value="blocked")
        ttk.Label(actions, text="Expected guard:").pack(side="left", padx=(12, 3))
        ttk.Combobox(actions, textvariable=self.expected, values=("ready", "blocked"),
                     state="readonly", width=9).pack(side="left")
        ttk.Button(actions, text=f"Probe readiness only ({self.countdown_seconds}s)", command=self.readiness).pack(side="left", padx=4)
        self.input_startup_timeout = tk.StringVar(value=f"{startup_seconds:g}")
        ttk.Label(actions, text="Startup timeout (s):").pack(side="left", padx=(8, 3))
        ttk.Entry(actions, textvariable=self.input_startup_timeout, width=5).pack(side="left")
        ttk.Button(actions, text="STOP", command=self.stop).pack(side="right")
        confirmations = ttk.Frame(body)
        confirmations.pack(fill="x", pady=4)
        ttk.Label(confirmations, text="Last input's expected UI response:").pack(side="left")
        for label, value in (("Observed", "pass"), ("Not observed", "fail"), ("Unclear", "unknown")):
            ttk.Button(confirmations, text=label,
                       command=lambda v=value: self.confirm(v)).pack(side="left", padx=4)
        self.status = tk.StringVar(value="Select a target and compare capture methods first.")
        ttk.Label(body, textvariable=self.status, wraplength=1000).pack(fill="x", pady=3)
        self.log = tk.Text(body, height=8, wrap="word")
        self.log.pack(fill="x")
        self.log.insert("end", f"Evidence directory: {self.output}\n"
                        "Nonblack pixels and successful API dispatch do not establish game compatibility.\n")
        self.refresh()
        for variable in (self.backend, self.method, self.safe, self.operation, self.expected_response,
                         self.reacquire_control, self.expected_dialog):
            variable.trace_add("write", self.mouse_settings_changed)
        self.input_startup_timeout.trace_add("write", self.mouse_settings_changed)
        if known_config is not None:
            self.reports.append({"kind": "loaded_known_dialog_config", **known_config,
                                 "input_armed": False, "capture_started": False})
        if saved_target is not None:
            self.apply_saved_target(saved_target)
        self.save()
        self.root.after(100, self.poll)
        if other_screen:
            self.root.after(0, self.move_to_other_screen)

    def move_to_other_screen(self):
        try:
            placement = _move_probe_to_other_screen(self.root)
            self.keep_visible = True
            self.reports.append({"kind": "probe_window_placement", **placement})
            self.save()
            self.status.set("Probe moved to the other screen and will remain visible during tests. Input is unarmed.")
        except Exception as exc:
            self.status.set(f"Could not move probe: {exc}")

    def refresh(self):
        if self.busy:
            return
        previous = self.choice.get()
        items = None
        try:
            items = list(self._inventory())
            self.windows = {f"{w['title']} [HWND {w['hwnd']}]": w for w in items}
        except Exception as exc:
            self.windows = {}
            self.reports.append({"kind": "inventory_error", "error": str(exc)})
            self.status.set(f"Window inventory failed: {exc}")
        self.manual_target = self.manual_target_label = None
        if self.requested_hwnd is not None:
            diagnostic = self.verify_explicit_target(items)
            self.reports.append(diagnostic)
            if diagnostic.get("status") == "verified":
                self.manual_target = dict(diagnostic["selection"])
                self.manual_target_label = (f"Verified manual target: {self.manual_target['title']} "
                                            f"[HWND {self.requested_hwnd}]")
                selected = self.manual_target_label
            else:
                selected = ""
            values = tuple(self.windows) + ((self.manual_target_label,) if self.manual_target_label else ())
            self.selector.configure(values=values)
            self.choice.set(selected)
            self.changed_target()
            self.save()  # A rejected handle is evidence too; never fall back to another window.
            self.status.set("Verified manual target selected; capture and input remain unarmed."
                            if selected else "Requested HWND was rejected; no target is selected. Inspect its diagnostic.")
            return
        self.selector.configure(values=tuple(self.windows))
        selected = previous if previous in self.windows else next(
            (label for label, w in self.windows.items()
             if "windows sandbox" in w["title"].casefold()), "")
        self.choice.set(selected)
        if selected != previous:
            self.changed_target()

    def verify_explicit_target(self, inventory_items):
        """Use a fresh helper result, preserving real inventory provenance."""
        try:
            from tibia_client.desktop_probe import verify_known_handle

            diagnostic = verify_known_handle(self.requested_hwnd, inventory_items=inventory_items)
            if not isinstance(diagnostic, dict):
                raise ValueError("known-handle helper returned malformed evidence")
            if (diagnostic.get("kind") != "known_handle_diagnostic"
                    or diagnostic.get("status") not in ("verified", "rejected")
                    or type(diagnostic.get("requested_hwnd")) is not int
                    or diagnostic["requested_hwnd"] != self.requested_hwnd):
                raise ValueError("known-handle evidence must identify the exact requested HWND")
            if diagnostic.get("status") == "verified":
                selection = diagnostic.get("selection")
                if (type(diagnostic.get("requested_hwnd")) is not int
                        or diagnostic["requested_hwnd"] != self.requested_hwnd
                        or type(diagnostic.get("selected_hwnd")) is not int
                        or diagnostic["selected_hwnd"] != self.requested_hwnd
                        or not isinstance(selection, dict) or type(selection.get("hwnd")) is not int
                        or selection["hwnd"] != self.requested_hwnd or not selection.get("title")
                        or selection.get("selection_method") != "explicit_verified_hwnd"):
                    raise ValueError("verified manual selection must match the exact requested HWND")
            return diagnostic
        except Exception as exc:
            return {"kind": "known_handle_diagnostic", "requested_hwnd": self.requested_hwnd,
                    "selected_hwnd": None, "selection": None, "status": "rejected",
                    "reasons": [f"known-handle verification failed: {exc}"], "inspection": {},
                    "selection_method": "explicit_verified_hwnd",
                    "inventory_presence": "unknown" if inventory_items is None else (
                        "present" if any(item.get("hwnd") == self.requested_hwnd for item in inventory_items)
                        else "absent")}

    def changed_target(self, _event=None):
        self.disarm_mouse()
        self.target = self.target_hwnd = self.point = self.frame = None
        self.preview_hwnd = self.preview_loaded_at = None
        self.preview_source = None
        self.safe.set(False)
        self.last_input = None
        self.point_label.set("No selected point")
        self.render()

    def selected(self):
        if self.manual_target is not None and self.choice.get() == self.manual_target_label:
            return self.manual_target
        item = self.windows.get(self.choice.get())
        if item is None:
            raise ValueError("select a returned or verified manual application window")
        return item

    def operation_changed(self, _event=None):
        self.disarm_mouse()
        self.expected_response.set(_DEFAULT_RESPONSES.get(self.operation.get(), ""))
        self.expected_dialog.set("")

    def run(self, label, work):
        if self.busy:
            return
        if self.input_blocked:
            self.status.set("A native worker was not reaped; further probes in this session are blocked.")
            return
        try:
            if self._emergency_stop():
                self.status.set("Release Ctrl+Shift+F12 before arming a probe.")
                return
        except Exception as exc:
            self.status.set(f"Emergency stop monitoring is unavailable: {exc}")
            return
        self.busy = True
        self.token = StopToken()
        token = self.token
        results = self.results
        countdown = getattr(self, "countdown_seconds", 3)
        target_hwnd = getattr(work, "input_hwnd", None)
        if target_hwnd is not None:
            countdown = getattr(self, "input_countdown_seconds", 0)
            try:
                _activate_input_target(target_hwnd)
                self.reports.append({"kind": "input_target_activation", "hwnd": target_hwnd,
                                     "status": "requested", "availability_preflight": False})
            except Exception as exc:
                self.busy = False
                self.status.set(f"Could not start input: {exc}")
                self.reports.append({"kind": "input_target_activation", "hwnd": target_hwnd,
                                     "status": "failed", "error": str(exc),
                                     "client_availability": getattr(exc, "client_availability", None)})
                self.save()
                return
        self.status.set(f"{label}: begins in {countdown} seconds; leave the selected client active."
                        if countdown else f"{label}: starting worker; leave the selected client active.")
        self._withdrawn_for_probe = not getattr(self, "keep_visible", False)
        if self._withdrawn_for_probe:
            self.root.withdraw()

        def begin():
            if self.closed or token.event.is_set():
                self.busy = False
                if not self.closed and self._withdrawn_for_probe:
                    self.root.deiconify()
                return

            self.status.set(f"{label}: worker starting / one operation in progress…")

            def worker():
                try:
                    value = work(token)
                    results.put((label, value, None))
                except Exception as exc:
                    results.put((label, None, str(exc)))

            threading.Thread(target=worker, name="experimental-probe", daemon=True).start()

        self.root.after(countdown * 1000, begin)

    def run_dir(self, prefix):
        return self.output / f"{prefix}-{uuid.uuid4().hex[:8]}"

    def capture(self, both=False):
        try:
            selected = dict(self.selected())
            methods = _CAPTURE_CHOICES if both else (self.method.get(),)
            directory = self.run_dir("capture")
        except ValueError as exc:
            self.status.set(str(exc))
            return

        def work(token):
            from tibia_client.desktop_probe import collect_capture

            reports = []
            geometry = selected.get("physical_pixels", {})
            size = geometry.get("client_size") if isinstance(geometry, dict) else None
            for method in methods:
                token.checkpoint()
                reports.append(collect_capture(selected["hwnd"], method, directory / method,
                                               samples=5, token=token, expected_size=size))
            return {"kind": "capture", "selection": selected, "methods": reports}

        self.run("Capture comparison" if both else "Capture", work)

    def configure_target(self):
        if self.busy:
            return
        self.disarm_mouse()
        if self.input_blocked:
            self.status.set("A native worker was not reaped; setup captures are blocked in this session.")
            return
        try:
            selected = self.selected()
            from modules.SandboxSetup import setup_sandbox

            provider = None
            if self.method.get() == "visible_region_gdi_overlay_aware":
                from tibia_client.probe_region_gdi import capture_client_region_gdi_overlay_aware
                provider = capture_client_region_gdi_overlay_aware
            elif self.method.get() == "visible_screen_overlay_aware":
                from tibia_client.probe_overlay import capture_client_overlay_aware
                provider = capture_client_overlay_aware
            candidate = setup_sandbox(self.root, selected["hwnd"], capture_provider=provider,
                                      countdown_seconds=30,
                                      require_focus_samples=self.backend.get() != _ERROR_ONLY_BACKEND)
            if candidate is not None:
                self.target, self.target_hwnd = candidate, selected["hwnd"]
                self.status.set("Experimental target selected; production configuration is unchanged.")
        except Exception as exc:
            self.status.set(f"Experimental target setup failed: {exc}")

    def _input_controls(self, *, readiness=False):
        if self.input_blocked:
            raise ValueError("a probe worker was not reaped; further native probes are blocked")
        self.selected_startup_timeout()
        from tibia_client.input_probe import BACKENDS

        selected = dict(self.selected())
        guarded = ("guarded_physical", "guarded_physical_overlay_aware",
                   "guarded_region_gdi_overlay_aware", _LEASE_BACKEND)
        backend = self.backend.get()
        if backend not in BACKENDS:
            raise ValueError("select a supported native input backend")
        if readiness and backend not in guarded:
            backend = "guarded_physical"
        operation = "readiness" if readiness else self.operation.get()
        if not readiness and operation not in ("ctrl_o", "escape", "move", "left_click", "right_click"):
            raise ValueError("select a supported one-shot input operation")
        capture_method = ("visible_screen" if readiness and backend != _LEASE_BACKEND else self.method.get())
        if capture_method not in ("visible_screen", "visible_screen_overlay_aware",
                                   "visible_region_gdi_overlay_aware", "direct_visible_region_gdi"):
            raise ValueError("select a visible-screen capture method before probing input")
        if backend == _ERROR_ONLY_BACKEND and capture_method != "direct_visible_region_gdi":
            raise ValueError("error-only input uses direct_visible_region_gdi for after-action evidence")
        if not readiness and operation != "move" and not self.safe.get():
            raise ValueError("confirm that Tibia is logged in and the operation uses a harmless control")
        if backend in guarded and (
            self.target is None or self.target_hwnd != selected["hwnd"]
        ):
            raise ValueError("configure experimental Sandbox viewport / focus samples first")
        if backend == _LEASE_BACKEND:
            if not self.known_dialog_report:
                raise ValueError("start the frame-lease experiment with an explicit --known-dialog-report")
            if capture_method != "visible_region_gdi_overlay_aware":
                raise ValueError("frame-lease experiment requires the explicit region-GDI capture method")
        expected_name = self.expected_dialog.get().strip()
        if not readiness and expected_name and (
                backend != _LEASE_BACKEND or operation != "escape" or expected_name != "open_channel"):
            raise ValueError("recognized-dialog intent is permitted only for explicit frame-lease Escape")
        description = None if readiness else self.expected_response.get().strip()
        if not readiness and not description:
            raise ValueError("describe the intended visible local-control response first")
        from tibia_client.window_target import WindowTarget

        target = (WindowTarget.from_config({"window_target": self.target})
                  if backend in guarded or (backend == _ERROR_ONLY_BACKEND
                                            and self.target is not None
                                            and self.target_hwnd == selected["hwnd"]) else None)
        if target is not None and target.kind != "sandbox":
            raise ValueError("configure a calibrated Sandbox target for guarded input")
        return selected, backend, operation, capture_method, description, target

    def selected_startup_timeout(self):
        try:
            return _input_startup_seconds(float(self.input_startup_timeout.get()))
        except (TypeError, ValueError) as exc:
            raise ValueError("input startup timeout must be finite, positive, and at most 60 seconds") from exc

    def _input_work(self, *, readiness=False):
        selected, backend, operation, capture_method, description, target = self._input_controls(
            readiness=readiness)
        mouse = operation in ("move", "left_click", "right_click")
        if mouse and self.point is None:
            raise ValueError("select a harmless point in a fresh capture preview first")
        if mouse and self.preview_hwnd != selected["hwnd"]:
            raise ValueError("capture the selected window again before choosing a point (preview expired)")
        selection_evidence = None
        if mouse and backend != _ERROR_ONLY_BACKEND:
            if not isinstance(self.frame, Image.Image):
                raise ValueError("a captured preview image is required before choosing a mouse point")
            age = _preview_age(self.preview_loaded_at, time.monotonic())
            reacquire = bool(self.reacquire_control.get())
            age_limit = 300 if reacquire else 30
            if age > age_limit:
                raise ValueError("capture the selected window again before choosing a point "
                                 f"({'stable template' if reacquire else 'preview'} expired)")
            if reacquire:
                if not self.safe.get():
                    raise ValueError("confirm the selected stable-template point is a harmless logged-in control")
                geometry = selected.get("physical_pixels", {})
                current_size = geometry.get("client_size") if isinstance(geometry, dict) else None
                if (not isinstance(current_size, (list, tuple)) or len(current_size) != 2
                        or any(type(v) is not int for v in current_size)
                        or tuple(current_size) != self.frame.size):
                    raise ValueError("stable template geometry differs from the selected target; capture and refresh again")
            selection_evidence = {
                "mode": "stable_template_reacquisition" if reacquire else "fresh_preview",
                "template_captured_monotonic": float(self.preview_loaded_at),
                "template_age_at_arming_ms": round(age * 1000, 3),
                "template_age_at_worker_request_ms": None, "max_template_age_ms": age_limit * 1000,
                "fresh_before_patch_required": True,
                "reason": ("explicit operator mode rechecks an older control template against fresh pixels"
                           if reacquire else "preview acquisition is within the ordinary 30-second limit"),
            }
        point = self.point if mouse else None
        patch = None
        if point is not None and backend != _ERROR_ONLY_BACKEND:
            x, y = point
            box = (max(0, x - 16), max(0, y - 16),
                   min(self.frame.width, x + 17), min(self.frame.height, y + 17))
            patch = {"box": list(box), "sha256_rgb": hashlib.sha256(
                self.frame.convert("RGB").crop(box).tobytes()).hexdigest()}
            if box[2] <= box[0] or box[3] <= box[1] or not (0 <= x < self.frame.width and 0 <= y < self.frame.height):
                raise ValueError("selected control point must lie inside its template image")
        size = self.frame.size if self.frame is not None and self.preview_hwnd == selected["hwnd"] else None
        if target is not None:
            if size is not None and tuple(size) != target.host_size:
                raise ValueError("preview size differs from Sandbox setup; capture and configure again")
            size = target.host_size
        expectation = self.expected.get() if readiness else None
        directory = self.run_dir(operation)
        preview_source = self.preview_source
        known_report = self.known_dialog_report if backend == _LEASE_BACKEND else None
        expected_dialog = self.expected_dialog.get().strip() if backend == _LEASE_BACKEND and not readiness else None
        startup_timeout = self.selected_startup_timeout()

        def work(token):
            from tibia_client.input_probe import run_input_probe

            if selection_evidence is not None:
                token.checkpoint()
                request_age = _preview_age(selection_evidence["template_captured_monotonic"], time.monotonic())
                if request_age * 1000 > selection_evidence["max_template_age_ms"]:
                    raise ValueError("selected control template expired before the fresh capture request")
                if patch is None:
                    raise ValueError("mouse input requires an exact fresh-before point patch check")
                selection_evidence["template_age_at_worker_request_ms"] = round(request_age * 1000, 3)
            lease_settings = ({"known_dialog_report": known_report,
                               "expected_dialog": expected_dialog or None} if backend == _LEASE_BACKEND else {})
            report = run_input_probe(selected["hwnd"], backend, operation, directory,
                                     target=target, point=point, token=token, expected_size=size,
                                     point_patch=patch, capture_method=capture_method,
                                     startup_timeout=startup_timeout,
                                     **({"key_hold_ms": self.key_hold_ms} if getattr(self, "key_hold_ms", 0) else {}),
                                     **({"trace_key_target": True} if getattr(self, "trace_key_target", False) else {}),
                                     **lease_settings)
            report.update(kind="readiness" if readiness else "input", selection=selected)
            report["selected_input_startup_timeout_s"] = startup_timeout
            if patch is not None:
                report["point_preview_source"] = preview_source
                report["point_selection"] = dict(selection_evidence)
            if description:
                report["expected_visible_response"] = description
            if readiness:
                report["expected_readiness"] = expectation
                actual = report.get("ready")
                report["expectation_result"] = (
                    "unknown" if actual is None else
                    "pass" if actual == (expectation == "ready") else "fail"
                )
            return report

        if not readiness and backend == _ERROR_ONLY_BACKEND:
            work.input_hwnd = selected["hwnd"]
        return work

    def input(self):
        self.disarm_mouse()
        try:
            work = self._input_work()
        except ValueError as exc:
            self.status.set(str(exc))
            return
        self.run("One native input probe", work)

    def readiness(self):
        self.disarm_mouse()
        try:
            work = self._input_work(readiness=True)
        except ValueError as exc:
            self.status.set(str(exc))
            return
        self.run("Readiness measurement (no input)", work)

    def poll(self):
        if self.closed:
            return
        if self.busy and self.token is not None and not self.token.cancelled:
            try:
                if self._emergency_stop():
                    self.stop()
            except Exception as exc:
                self.stop()
                self.log.insert("end", f"Stop monitoring failed; probe cancelled: {exc}\n")
        try:
            label, value, error = self.results.get_nowait()
        except queue.Empty:
            self.root.after(100, self.poll)
            return
        self.busy = False
        if getattr(self, "_withdrawn_for_probe", True):
            self.root.deiconify()
        if error:
            self.status.set(f"{label} failed: {error}")
            self.log.insert("end", f"{label}: {error}\n")
            self.reports.append({"kind": "harness_error", "label": label, "error": error})
            self.save()
        else:
            self.reports.append(value)
            if value.get("worker_reaped") is False:
                self.input_blocked = True
            if value.get("kind") == "input":
                self.last_input = value
            self.save()
            self.status.set(f"{label} complete. Inspect original frames and confirm visible results separately.")
            self.log.insert("end", json.dumps(value, indent=2) + "\n")
            self.load_preview(value)
        self.log.see("end")
        self.root.after(100, self.poll)

    def load_preview(self, report):
        candidates = []
        if report.get("kind") == "capture":
            for method in report.get("methods", []):
                for frame in method.get("frames", []):
                    path = frame.get("path")
                    if path:
                        candidates.append((path, frame.get("captured_monotonic"), method.get("method")))
        else:
            for path in (report.get("before_path"), report.get("after_path")):
                if path:
                    candidates.append((path, None, "visible_screen"))
        for path, observed, source in reversed(candidates):
            try:
                with Image.open(path) as image:
                    self.frame = image.convert("RGB").copy()
                self.point = None
                self.preview_hwnd = report.get("selection", {}).get("hwnd")
                # Unknown acquisition time cannot establish a fresh mouse target.
                self.preview_loaded_at = observed
                self.preview_source = {"path": path, "method": source,
                                       "captured_monotonic": observed}
                self.point_label.set("No selected point")
                self.render()
                return
            except (OSError, TypeError):
                continue

    def render(self):
        self.canvas.delete("all")
        if self.frame is None:
            return
        from modules.SandboxSetup import PreviewMapping

        width, height = self.canvas.winfo_width(), self.canvas.winfo_height()
        if min(width, height) < 24:
            return
        mapping = PreviewMapping((width, height), self.frame.size)
        left, top, w, h = mapping.bounds
        self.photo = ImageTk.PhotoImage(self.frame.resize((w, h), Image.Resampling.LANCZOS))
        self.canvas.create_image(left, top, anchor="nw", image=self.photo)
        if self.point is not None:
            x = left + self.point[0] / self.frame.width * w
            y = top + self.point[1] / self.frame.height * h
            self.canvas.create_line(x - 8, y, x + 8, y, fill="#ffbd36", width=2)
            self.canvas.create_line(x, y - 8, x, y + 8, fill="#ffbd36", width=2)

    def select_point(self, event):
        if self.busy or self.frame is None:
            return
        from modules.SandboxSetup import PreviewMapping

        mapping = PreviewMapping((self.canvas.winfo_width(), self.canvas.winfo_height()), self.frame.size)
        if not mapping.contains(event.x, event.y):
            return
        x, y = mapping.image_edge(event.x, event.y)
        self.point = min(x, self.frame.width - 1), min(y, self.frame.height - 1)
        self.point_label.set(f"Host client point: {self.point}")
        self.render()
        if self.arm_on_point.get():
            armed_settings = self._armed_mouse_settings
            self.disarm_mouse()
            try:
                if self.mouse_arm_signature() != armed_settings:
                    raise ValueError("mouse operation settings changed; arm the next preview click again")
            except ValueError as exc:
                self.status.set(str(exc))
                return
            self.input()

    def disarm_mouse(self):
        self._armed_mouse_settings = None
        variable = getattr(self, "arm_on_point", None)
        if variable is not None:
            variable.set(False)

    def mouse_settings_changed(self, *_args):
        if self.arm_on_point.get():
            self.disarm_mouse()
            self.status.set("Mouse click arming cleared because the operation settings changed.")

    def mouse_arm_signature(self):
        selected, backend, operation, capture_method, description, target = self._input_controls()
        if operation not in ("move", "left_click", "right_click"):
            raise ValueError("next-preview-click arming accepts only move, left_click or right_click")
        if not self.safe.get():
            raise ValueError("confirm that Tibia is logged in and the selected point is a harmless control")
        return (selected["hwnd"], backend, operation, capture_method, description,
                None if target is None else target.fingerprint, bool(self.safe.get()),
                bool(self.reacquire_control.get()), self.known_dialog_report,
                self.expected_dialog.get().strip(), self.selected_startup_timeout())

    def arm_next_point(self):
        if not self.arm_on_point.get():
            self.disarm_mouse()
            return
        try:
            if self.busy:
                raise ValueError("wait for the current probe before arming a preview click")
            self._armed_mouse_settings = self.mouse_arm_signature()
        except ValueError as exc:
            self.disarm_mouse()
            self.status.set(str(exc))
            return
        self.status.set("Armed ONE mouse operation: capture a fresh preview, then click a harmless control. "
                        "That preview click starts the selected one-shot operation.")

    def apply_saved_target(self, saved):
        """Bind validated saved calibration only to a returned current HWND."""
        from tibia_client.window_target import WindowTarget

        target = WindowTarget.from_config({"window_target": saved["window_target"]})
        if target.kind != "sandbox" or target.fingerprint != saved["target_fingerprint"]:
            raise ValueError("saved Sandbox calibration fingerprint is invalid")
        if self.requested_hwnd is not None and self.requested_hwnd != saved["hwnd"]:
            self.status.set("Saved calibration belongs to another HWND; the explicit target selection is unchanged.")
            return False
        if self.manual_target is not None and self.manual_target["hwnd"] == saved["hwnd"]:
            matching = [(self.manual_target_label, self.manual_target)]
        else:
            matching = [(label, window) for label, window in self.windows.items()
                        if window["hwnd"] == saved["hwnd"]]
        if len(matching) != 1:
            self.status.set("Saved calibration HWND is absent from current inventory; configure the target again.")
            return False
        label, selected = matching[0]
        geometry = selected.get("physical_pixels", {})
        size = geometry.get("client_size") if isinstance(geometry, dict) else None
        if (not isinstance(size, (tuple, list)) or len(size) != 2
                or any(isinstance(v, bool) or not isinstance(v, int) for v in size)):
            self.status.set("Current target dimensions are unavailable; configure the target again.")
            return False
        if tuple(size) != target.host_size:
            self.status.set("Saved calibration dimensions differ from the current target; configure it again.")
            return False
        self.choice.set(label)
        self.changed_target()
        self.target, self.target_hwnd = target.to_dict(), saved["hwnd"]
        self.reports.append({"kind": "loaded_target", "source": saved["source"],
                             "selection": dict(selected), "window_target": target.to_dict(),
                             "target_fingerprint": target.fingerprint})
        self.status.set("Saved Sandbox calibration loaded. Verify the current layout before probing; input remains unarmed.")
        return True

    def confirm(self, status):
        if self.busy or self.last_input is None:
            self.status.set("Complete one input probe before recording its visible response.")
            return
        if status not in ("pass", "fail", "unknown"):
            raise ValueError("unsupported visual confirmation")
        if status == "pass" and (
            self.last_input.get("dispatch") != "completed"
            or not self.last_input.get("after_path")
        ):
            self.status.set("A failed/unverified dispatch cannot be confirmed as accepted input.")
            return
        self.last_input["operator_confirmation"] = status
        self.last_input["confirmation_basis"] = "operator-assessed visible client response"
        self.save()
        self.status.set(f"Recorded visible-response assessment: {status}. No production profile was changed.")

    def save(self):
        from tibia_client.desktop_probe import write_report

        try:
            startup_seconds = self.selected_startup_timeout()
        except ValueError:
            startup_seconds = None
        write_report(self.output / "session.json", {
            "schema": "tibiaauto.experimental-desktop-probe", "version": 1,
            "created_utc": self.created_utc,
            "reports": self.reports,
            "overall_compatibility": "unknown",
            "input_startup_timeout_s": startup_seconds,
        })

    def stop(self):
        self.disarm_mouse()
        if self.token is not None:
            self.token.event.set()
        self.status.set("Stop requested; the isolated probe will be reaped.")

    def close(self):
        self.stop()
        self.closed = True
        self.root.destroy()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", action="store_true", help="list native window targets; no capture/input")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / ".work" / "desktop-probe")
    parser.add_argument("--target-report", type=Path,
                        help="load validated Sandbox calibration from saved probe evidence; does not arm input")
    parser.add_argument("--target-hwnd", type=_handle_argument,
                        help="verify and select this exact host HWND as a labeled manual target; does not arm input")
    parser.add_argument("--capture-method", choices=_CAPTURE_CHOICES,
                        help="initial capture selection only; does not start capture or arm input")
    parser.add_argument("--backend", choices=_BACKEND_CHOICES,
                        help="initial input backend selection only; does not arm input")
    parser.add_argument("--operation", choices=_OPERATION_CHOICES,
                        help="initial one-shot operation selection only; does not arm input")
    parser.add_argument("--expected-response", type=_response_text,
                        help="initial expected visible response (1–500 printable characters)")
    parser.add_argument("--reacquire-control", action="store_true",
                        help="select stable-template mode only (up to 300s, exact fresh-pixel recheck); does not arm input")
    parser.add_argument("--known-dialog-report", type=Path,
                        help="validated frozen Open Channel evaluation report for the frame-lease experiment")
    parser.add_argument("--expected-dialog", choices=("open_channel",),
                        help="explicit recognized-dialog intent for frame-lease Escape only; does not arm input")
    parser.add_argument("--input-startup-timeout", type=lambda value: _input_startup_seconds(float(value)),
                        help="input-worker startup diagnosis only: 0<seconds<=60; default15, freshness/action limits unchanged")
    parser.add_argument("--countdown-seconds", type=lambda value: _countdown_seconds(int(value)),
                        help="override probe countdown (0..30 seconds; default error-only input0, capture/legacy3)")
    parser.add_argument("--key-hold-ms", type=int,
                        help="experimental minimum key pulse duration (0..250ms; default0)")
    parser.add_argument("--other-screen", action="store_true", help="place this probe UI on another monitor")
    parser.add_argument("--trace-key-target", action="store_true",
                        help="one-off failure diagnosis: log foreground recipient without gating input")
    args = parser.parse_args(argv)
    if args.inventory:
        from tibia_client.desktop_probe import inventory
        items = inventory()
        if args.target_hwnd is not None:
            from tibia_client.desktop_probe import verify_known_handle, write_report

            diagnostic = verify_known_handle(args.target_hwnd, inventory_items=items)
            write_report(args.output / "known-handle-diagnostic.json", diagnostic)
            print(json.dumps({"inventory": items, "known_handle_diagnostic": diagnostic}, indent=2))
            return 0 if diagnostic.get("status") == "verified" else 1
        print(json.dumps(items, indent=2))
        return 0
    try:
        defaults = {name: getattr(args, name) for name in
                    ("capture_method", "backend", "operation", "expected_response")
                    if getattr(args, name) is not None}
        if args.reacquire_control:
            defaults["reacquire_control"] = True
        if args.target_hwnd is not None:
            defaults["target_hwnd"] = args.target_hwnd
        if args.known_dialog_report is not None:
            defaults["known_dialog_report"] = args.known_dialog_report
        if args.expected_dialog is not None:
            defaults["expected_dialog"] = args.expected_dialog
        if args.input_startup_timeout is not None:
            defaults["input_startup_timeout"] = args.input_startup_timeout
        if args.countdown_seconds is not None:
            defaults["countdown_seconds"] = args.countdown_seconds
        if args.key_hold_ms is not None:
            defaults["key_hold_ms"] = args.key_hold_ms
        if args.other_screen:
            defaults["other_screen"] = True
        if args.trace_key_target:
            defaults["trace_key_target"] = True
        app = DesktopProbe(args.output, target_report=args.target_report, **defaults)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    app.root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
