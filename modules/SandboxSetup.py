"""Explicit, capture-only setup for a game displayed inside Windows Sandbox.

Only ``setup_sandbox``'s returned dictionary is eligible for persistence by the
caller. Opening, capturing, selecting and cancelling never write configuration
or send input to either the host window or the guest.
"""
from __future__ import annotations

from dataclasses import dataclass
import queue
import threading
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable

from PIL import Image, ImageTk

from tibia_client.direct_capture import capture_direct_host_client
from tibia_client.window_target import WindowTarget, make_focus_guard


Rectangle = tuple[int, int, int, int]


def capture_active_client(hwnd, *, expected_size=None):
    """Activate once for an explicit setup capture, then let the window repaint."""
    from core.MoveMouse import MoveMouse
    from core.runtime import wait

    MoveMouse(hwnd_resolver=lambda: hwnd).Activate()
    wait(0.35)
    return capture_direct_host_client(hwnd, expected_size=expected_size)


@dataclass(frozen=True)
class PreviewMapping:
    """Map image *edges* to preview edges, retaining exclusive right/bottom."""

    canvas_size: tuple[int, int]
    frame_size: tuple[int, int]
    padding: int = 10

    def __post_init__(self) -> None:
        if any(type(n) is not int or n <= 0 for n in (*self.canvas_size, *self.frame_size)):
            raise ValueError("preview and frame sizes must be positive integers")
        if type(self.padding) is not int or self.padding < 0:
            raise ValueError("padding must be a non-negative integer")
        if min(self.canvas_size) <= 2 * self.padding:
            raise ValueError("preview is too small")

    @property
    def bounds(self) -> Rectangle:
        cw, ch = self.canvas_size
        fw, fh = self.frame_size
        scale = min((cw - self.padding * 2) / fw, (ch - self.padding * 2) / fh)
        width, height = max(1, round(fw * scale)), max(1, round(fh * scale))
        return (cw - width) // 2, (ch - height) // 2, width, height

    def contains(self, x: float, y: float) -> bool:
        left, top, width, height = self.bounds
        return left <= x <= left + width and top <= y <= top + height

    def image_edge(self, x: float, y: float, *, clamp: bool = False) -> tuple[int, int]:
        if not clamp and not self.contains(x, y):
            raise ValueError("start the selection inside the captured image")
        left, top, width, height = self.bounds
        fw, fh = self.frame_size
        return (
            round(min(1, max(0, (x - left) / width)) * fw),
            round(min(1, max(0, (y - top) / height)) * fh),
        )

    def canvas_rectangle(self, rectangle: Rectangle) -> tuple[float, float, float, float]:
        left, top, width, height = self.bounds
        fw, fh = self.frame_size
        l, t, r, b = rectangle
        return left + l / fw * width, top + t / fh * height, \
            left + r / fw * width, top + b / fh * height


def _rectangle(start: tuple[int, int], end: tuple[int, int]) -> Rectangle:
    left, right = sorted((start[0], end[0]))
    top, bottom = sorted((start[1], end[1]))
    if right - left < 2 or bottom - top < 2:
        raise ValueError("drag a rectangle at least 2 by 2 image pixels")
    return left, top, right, bottom


class SandboxSelection:
    """In-memory draft; failed samples leave the previous draft intact."""

    def __init__(self, *, require_focus_samples: bool = False) -> None:
        if type(require_focus_samples) is not bool:
            raise TypeError("require_focus_samples must be a boolean")
        self.require_focus_samples = require_focus_samples
        self.active: Image.Image | None = None
        self.inactive: Image.Image | None = None
        self.viewport: Rectangle | None = None
        self.focus_region: Rectangle | None = None

    def set_active(self, image: Image.Image) -> None:
        if not isinstance(image, Image.Image) or min(image.size) <= 0:
            raise ValueError("capture did not return a valid image")
        frame = image.convert("RGB").copy()
        if self.active is not None and self.active.size != frame.size:
            # Old rectangles cannot describe a changed host coordinate space.
            self.viewport = self.focus_region = None
        self.active = frame
        self.inactive = None

    def select(self, mode: str, rectangle: Rectangle) -> None:
        if self.active is None:
            raise ValueError("capture active Tibia first")
        left, top, right, bottom = rectangle
        width, height = self.active.size
        if any(type(n) is not int for n in rectangle) or not (
            0 <= left < right <= width and 0 <= top < bottom <= height
        ):
            raise ValueError("selection must lie inside the captured host client")
        if mode == "viewport":
            self.viewport = rectangle
        elif mode == "focus":
            self.focus_region = rectangle
        else:
            raise ValueError("unknown selection mode")

    def set_inactive(self, image: Image.Image) -> None:
        if self.active is None or self.focus_region is None:
            raise ValueError("capture active Tibia and select its title-bar patch first")
        if not isinstance(image, Image.Image) or image.size != self.active.size:
            raise ValueError("host client size changed; capture active Tibia again")
        # Validate before replacing a previous good sample.
        make_focus_guard(self.active, image, self.focus_region)
        self.inactive = image.convert("RGB").copy()

    def build_target(self) -> dict[str, Any]:
        if self.active is None:
            raise ValueError("capture active Tibia first")
        if self.viewport is None:
            raise ValueError("select the guest Tibia client area")
        if not self.require_focus_samples:
            return WindowTarget.build(self.active.size, self.viewport).to_dict()
        if self.focus_region is None:
            raise ValueError("select a stable active/inactive guest Tibia title-bar patch")
        if self.inactive is None:
            raise ValueError("capture the inactive Tibia sample")
        candidate = {
            "kind": "sandbox", "host_size": list(self.active.size),
            "viewport": list(self.viewport),
            "focus_guard": make_focus_guard(self.active, self.inactive, self.focus_region),
        }
        return WindowTarget.from_config({"window_target": candidate}).to_dict()


class SandboxSetup:
    """Modal capture wizard; all Tk access stays on its owning thread."""

    def __init__(self, parent: object, hwnd: int, *,
                 capture_provider: Callable[..., Image.Image] | None = None,
                 countdown_seconds: int = 3,
                 require_focus_samples: bool = False) -> None:
        if type(hwnd) is not int or hwnd <= 0:
            raise ValueError("select the Windows Sandbox host window first")
        if capture_provider is not None and not callable(capture_provider):
            raise TypeError("capture_provider must be callable")
        if type(countdown_seconds) is not int or not 1 <= countdown_seconds <= 30:
            raise ValueError("countdown_seconds must be an integer from 1 to 30")
        if type(require_focus_samples) is not bool:
            raise TypeError("require_focus_samples must be a boolean")
        self.hwnd = hwnd
        self._capture_provider = capture_active_client if capture_provider is None else capture_provider
        self.countdown_seconds = countdown_seconds
        self.selection = SandboxSelection(require_focus_samples=require_focus_samples)
        self.result: dict[str, Any] | None = None
        self._closed = self._busy = False
        self._generation = 0
        self._timer: str | None = None
        self._queue: queue.Queue = queue.Queue()
        self._drag_start: tuple[int, int] | None = None
        self._drag_rectangle: Rectangle | None = None
        self._photo: ImageTk.PhotoImage | None = None
        master = getattr(parent, "windowID", parent)
        self.window = tk.Toplevel(master)
        self.window.title("Windows Sandbox — viewport setup")
        self.window.geometry("1080x800" if require_focus_samples else "1080x700")
        self.window.minsize(780, 670 if require_focus_samples else 570)
        self.window.transient(master)
        self.window.protocol("WM_DELETE_WINDOW", self.cancel)
        self.window.bind("<Escape>", lambda _event: self.cancel())

        body = ttk.Frame(self.window, padding=12)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(6, weight=1)
        self._wrapped_labels: list[ttk.Label] = []

        def wrapped_label(**options):
            label = ttk.Label(body, wraplength=1020, justify="left", **options)
            self._wrapped_labels.append(label)
            return label

        ttk.Label(body, text="Set up Tibia inside Windows Sandbox",
                  font=("TkDefaultFont", 14, "bold")).grid(row=0, column=0, sticky="w")
        wrapped_label(
            text="1. Keep the Sandbox host size and guest layout fixed, "
                 "with Tibia active in the guest. Click Capture Tibia; Sandbox is brought forward automatically. "
                 "This wizard hides during capture.",
        ).grid(row=1, column=0, sticky="ew", pady=(6, 4))
        self.active_button = ttk.Button(
            body, text="1. Capture Tibia",
            command=lambda: self.capture("active"),
        )
        self.active_button.grid(row=2, column=0, sticky="w")
        wrapped_label(
            text="2. Drag the guest Tibia client area, excluding its guest title bar, Sandbox "
                 "toolbar and guest taskbar. The selection uses original image pixels.",
        ).grid(row=3, column=0, sticky="ew", pady=(8, 3))
        modes = ttk.Frame(body)
        modes.grid(row=4, column=0, sticky="ew")
        self.mode = tk.StringVar(value="viewport")
        ttk.Radiobutton(modes, text="Tibia client area (cyan)", variable=self.mode,
                        value="viewport").pack(side="left")
        if require_focus_samples:
            ttk.Radiobutton(modes, text="Legacy guest focus patch (orange)", variable=self.mode,
                            value="focus").pack(side="left", padx=12)
        self.coordinates = tk.StringVar(value="No captured image")
        ttk.Label(body, textvariable=self.coordinates).grid(row=5, column=0, sticky="w", pady=3)
        self.canvas = tk.Canvas(body, background="#20242a", height=360, highlightthickness=0)
        self.canvas.grid(row=6, column=0, sticky="nsew")
        self.canvas.bind("<Configure>", lambda _event: self._render())
        self.canvas.bind("<ButtonPress-1>", self._drag_begin)
        self.canvas.bind("<B1-Motion>", self._drag_move)
        self.canvas.bind("<ButtonRelease-1>", self._drag_end)
        self.inactive_button = None
        if require_focus_samples:
            wrapped_label(
                text="Legacy guard experiment: select a stable title-bar patch, then click "
                     "Capture inactive. Foreground Sandbox and click its guest taskbar "
                     f"within {countdown_seconds} seconds. Leave Tibia in the same position.",
            ).grid(row=7, column=0, sticky="ew", pady=(8, 4))
            self.inactive_button = ttk.Button(
                body, text=f"3. Capture inactive Tibia ({countdown_seconds} seconds)",
                command=lambda: self.capture("inactive"),
            )
            self.inactive_button.grid(row=8, column=0, sticky="w")
        self.status = tk.StringVar(value="Capture Tibia to begin. Existing settings are unchanged.")
        wrapped_label(textvariable=self.status).grid(row=9, column=0, sticky="ew", pady=6)
        wrapped_label(text="Review the selected client area, then save the viewport.").grid(
            row=10, column=0, sticky="ew", pady=(0, 6),
        )
        controls = ttk.Frame(body)
        controls.grid(row=11, column=0, sticky="ew")
        self.save_button = ttk.Button(controls, text="Save Sandbox setup", command=self.save)
        self.save_button.pack(side="right")
        ttk.Button(controls, text="Cancel", command=self.cancel).pack(side="right", padx=8)
        self._refresh_controls()
        body.bind("<Configure>", self._resize_text, add="+")
        self.window.grab_set()

    def _resize_text(self, event: Any) -> None:
        if not self._closed:
            # Body padding is twelve pixels on each side. Wrapping the text to
            # its available width keeps guidance and Save visible when narrow.
            for label in self._wrapped_labels:
                label.configure(wraplength=max(1, event.width - 24))

    def _mapping(self) -> PreviewMapping | None:
        if self.selection.active is None:
            return None
        width, height = self.canvas.winfo_width(), self.canvas.winfo_height()
        if min(width, height) <= 20:
            return None
        return PreviewMapping((width, height), self.selection.active.size)

    def _render(self) -> None:
        if self._closed:
            return
        self.canvas.delete("all")
        mapping = self._mapping()
        if mapping is None:
            return
        left, top, width, height = mapping.bounds
        self._photo = ImageTk.PhotoImage(self.selection.active.resize(
            (width, height), Image.Resampling.LANCZOS,
        ))
        self.canvas.create_image(left, top, image=self._photo, anchor="nw")
        for mode, rectangle, color in (
            ("viewport", self.selection.viewport, "#2dd4e7"),
            ("focus", self.selection.focus_region, "#ffb347"),
        ):
            if self._drag_rectangle is not None and self.mode.get() == mode:
                rectangle = self._drag_rectangle
            if rectangle:
                self.canvas.create_rectangle(*mapping.canvas_rectangle(rectangle),
                                             outline=color, width=2)
        focus_detail = (f"   Focus patch: {self.selection.focus_region or 'select'}"
                        if self.selection.require_focus_samples else "")
        self.coordinates.set(
            f"Host client: {self.selection.active.width} × {self.selection.active.height} px   "
            f"Tibia area: {self.selection.viewport or 'select'}{focus_detail}"
        )

    def _drag_begin(self, event: Any) -> None:
        mapping = self._mapping()
        if self._busy or mapping is None or not mapping.contains(event.x, event.y):
            return
        self._drag_start = mapping.image_edge(event.x, event.y)

    def _drag_move(self, event: Any) -> None:
        mapping = self._mapping()
        if self._drag_start is None or mapping is None:
            return
        try:
            self._drag_rectangle = _rectangle(
                self._drag_start, mapping.image_edge(event.x, event.y, clamp=True),
            )
        except ValueError:
            self._drag_rectangle = None
        self._render()

    def _drag_end(self, event: Any) -> None:
        if self._drag_start is None:
            return
        self._drag_move(event)
        rectangle = self._drag_rectangle
        self._drag_start = self._drag_rectangle = None
        if rectangle:
            self.selection.select(self.mode.get(), rectangle)
        self._refresh_controls()
        self._render()

    def _refresh_controls(self) -> None:
        if self._closed:
            return
        self.active_button.configure(state="disabled" if self._busy else "normal")
        can_capture_inactive = (
            not self._busy and self.selection.active is not None
            and self.selection.focus_region is not None
        )
        if self.inactive_button is not None:
            self.inactive_button.configure(state="normal" if can_capture_inactive else "disabled")
        try:
            self.selection.build_target()
            detail = "Review the selected viewport, then Save Sandbox setup."
            ready = True
        except (ValueError, RuntimeError) as exc:
            detail, ready = str(exc), False
        self.save_button.configure(state="normal" if ready and not self._busy else "disabled")
        if not self._busy:
            self.status.set(detail)

    def capture(self, kind: str) -> None:
        if self._closed or self._busy:
            return
        if kind not in ("active", "inactive"):
            raise ValueError("unknown capture type")
        if kind == "inactive" and (
            self.selection.active is None or self.selection.focus_region is None
        ):
            self.status.set("Capture active Tibia and select its title-bar patch first.")
            return
        self._busy = True
        self._generation += 1
        generation = self._generation
        self._refresh_controls()
        self.window.grab_release()
        self.window.withdraw()
        delay = 0 if kind == "active" and self._capture_provider is capture_active_client else self.countdown_seconds
        self._countdown(kind, generation, delay)

    def _countdown(self, kind: str, generation: int, remaining: int) -> None:
        if self._closed or generation != self._generation:
            return
        if remaining:
            self.status.set(f"Capture {kind} in {remaining}… foreground Sandbox and set guest focus.")
            self._timer = self.window.after(
                1000, lambda: self._countdown(kind, generation, remaining - 1),
            )
            return
        self._timer = None
        expected_size = self.selection.active.size if kind == "inactive" else None
        # Copy the native arguments and queue into the worker closure. No Tk
        # object or variable is read, written or scheduled by that thread.
        hwnd, results, capture_provider = self.hwnd, self._queue, self._capture_provider

        def work() -> None:
            try:
                image = capture_provider(hwnd, expected_size=expected_size)
                results.put((generation, kind, image, None))
            except Exception as exc:
                results.put((generation, kind, None, str(exc)))

        threading.Thread(target=work, name="sandbox-setup-capture", daemon=True).start()
        self._timer = self.window.after(50, self._poll_capture)

    def _poll_capture(self) -> None:
        self._timer = None
        if self._closed:
            return
        try:
            generation, kind, image, error = self._queue.get_nowait()
        except queue.Empty:
            self._timer = self.window.after(50, self._poll_capture)
            return
        if generation != self._generation:
            self._timer = self.window.after(50, self._poll_capture)
            return
        self._busy = False
        if error is None:
            try:
                if kind == "active":
                    self.selection.set_active(image)
                else:
                    self.selection.set_inactive(image)
            except (ValueError, RuntimeError) as exc:
                error = str(exc)
        self.window.deiconify()
        self.window.grab_set()
        self._refresh_controls()
        self._render()
        if error is not None:
            self.status.set(f"Capture was not accepted: {error}. Previous selections are retained.")

    def save(self) -> None:
        if self._closed or self._busy:
            return
        try:
            candidate = self.selection.build_target()
        except (ValueError, RuntimeError) as exc:
            self.status.set(f"Cannot save: {exc}")
            return
        self.result = candidate
        self._close()

    def cancel(self) -> None:
        self.result = None
        self._close()

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._generation += 1
        if self._timer is not None:
            self.window.after_cancel(self._timer)
            self._timer = None
        self.window.grab_release()
        self.window.destroy()


def setup_sandbox(parent: object, hwnd: int, *,
                  capture_provider: Callable[..., Image.Image] | None = None,
                  countdown_seconds: int = 3,
                  require_focus_samples: bool = False) -> dict[str, Any] | None:
    """Wait for Save, returning a draft or ``None``; alternate capture is explicit.

    Providers accept ``(hwnd, expected_size=...)``. The default setup captures
    the host client directly and saves only its viewport geometry. Paired focus
    samples are available only for an explicitly selected legacy experiment.
    """
    wizard = SandboxSetup(parent, hwnd, capture_provider=capture_provider,
                          countdown_seconds=countdown_seconds,
                          require_focus_samples=require_focus_samples)
    wizard.window.wait_window()
    return wizard.result
