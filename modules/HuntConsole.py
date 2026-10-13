"""Operator controls and read-only SDK health. All Tk work stays on Tk's thread."""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import ttk

_windows = {}


def run_control(parent, operation, on_complete):
    """Keep bounded SDK quiescence waits off Tk, deliver completion on Tk."""
    results = queue.Queue(maxsize=1)

    def worker():
        try:
            results.put((operation(), None))
        except Exception as exc:
            results.put((None, str(exc)))

    def poll():
        try:
            value, error = results.get_nowait()
        except queue.Empty:
            try:
                parent.after(50, poll)
            except tk.TclError:
                pass
            return
        try:
            if parent.winfo_exists():
                on_complete(value, error)
        except tk.TclError:
            pass

    threading.Thread(target=worker, name="operator-control", daemon=True).start()
    parent.after(50, poll)


class HuntConsole:
    def __init__(self, parent, session, controller=None):
        key = id(session)
        old = _windows.get(key)
        if old is not None and old.window.winfo_exists():
            old.window.deiconify()
            old.window.lift()
            self.window = old.window
            return
        self.session, self.controller, self._key = session, controller, key
        self.window = tk.Toplevel(getattr(parent, "windowID", parent))
        self.window.title("Hunt Console")
        self.window.geometry("790x620")
        self.window.minsize(660, 500)
        self._pending = None
        self._control_serial = 0
        self._closed = False
        _windows[key] = self
        body = ttk.Frame(self.window, padding=12)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Session controls", font=("TkDefaultFont", 13, "bold")).pack(anchor="w")
        controls = ttk.Frame(body)
        controls.pack(fill="x", pady=8)
        self.stop = ttk.Button(controls, text="STOP ALL", command=lambda: self._control(
            lambda: session.stop_all("operator Stop All"),
            "All automation stopped. Restart modules explicitly.", is_stop=True))
        self.stop.pack(side="left")
        self.pause = ttk.Button(controls, text="Pause navigation", command=lambda: self._control(
            session.pause_navigation, "Navigation paused; support modules continue."))
        self.pause.pack(side="left", padx=6)
        self.resume = ttk.Button(controls, text="Resume navigation", command=lambda: self._control(
            session.resume_navigation, "Navigation resume requested; fresh observations are required."))
        self.resume.pack(side="left")
        self.message = tk.StringVar(value="Stop All cancels queued input and confirmations. It does not close the client session.")
        ttk.Label(body, textvariable=self.message, wraplength=740).pack(anchor="w", pady=4)
        self.view = tk.Text(body, wrap="word", width=95, height=25, state="disabled")
        self.view.pack(fill="both", expand=True, pady=8)
        ttk.Label(body, text="Capture age is local only; OBS/game freshness is unverified. External client/OBS settings are operator-reported; rerun tests after changing them.",
                  wraplength=740).pack(anchor="w")
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self._refresh()

    def _control(self, operation, success, *, is_stop=False):
        if self._pending == "stop" or (self._pending and not is_stop):
            return
        self._control_serial += 1
        serial = self._control_serial
        self._pending = "stop" if is_stop else "navigation"
        self.message.set("Applying control…")
        # The visible stop control can interrupt a delayed navigation hook.
        self.stop.configure(state="disabled" if is_stop else "normal")
        for button in (self.pause, self.resume):
            button.configure(state="disabled")

        def done(value, error):
            if serial != self._control_serial or self._closed:
                return
            self._pending = None
            if error:
                self.message.set(f"Control failed: {error}")
            elif value is False:
                self.message.set("Navigation unchanged; CaveBot may already be paused, stopped, or changing state.")
            else:
                self.message.set(success)
            for button in (self.stop, self.pause, self.resume):
                button.configure(state="normal")
        run_control(self.window, operation, done)

    def _refresh(self):
        if self._closed:
            return
        try:
            info = self.session.diagnostics()
            profile = self.controller.status() if self.controller is not None else {}
            capture = info.capture
            age = "unavailable" if capture.captured_age is None else f"{capture.captured_age:.2f}s"
            lines = [
                f"Session: {'closed' if info.closed else 'open'}",
                f"Ready for input: {info.ready} — {info.readiness_reason or 'no readiness error'}",
                f"Profile: {'context accepted' if profile.get('ready') else 'blocked / unverified'}",
                *[f"  • {reason}" for reason in profile.get("problems", ())],
                f"Verified operations / supported sensors: {', '.join(profile.get('capabilities', ())) or 'none'}",
                "", f"Active: {', '.join(info.active_features) or 'none'}",
                f"Paused: {', '.join(info.paused_features) or 'none'}",
                "", f"Capture transport: {capture.transport_ok}",
                f"Local capture age: {age}", f"Visual validity: {capture.visual_valid}",
                f"OBS/game source freshness: {capture.source_freshness}",
                *[f"  • {reason}" for reason in capture.reasons],
                "", f"Last action: {info.last_action or 'none'}",
                f"Last result: {info.last_result or 'none'}",
                "", "Recent failure reasons:",
                *[f"  • {reason}" for reason in info.failure_reasons],
            ]
            text = "\n".join(lines)
            if text != getattr(self, "_last_text", None):
                self.view.configure(state="normal")
                self.view.delete("1.0", "end")
                self.view.insert("1.0", text)
                self.view.configure(state="disabled")
                self._last_text = text
        except Exception as exc:
            self.message.set(f"Status unavailable: {exc}")
        self.window.after(250, self._refresh)

    def close(self):
        self._closed = True
        _windows.pop(self._key, None)
        self.window.destroy()
