"""Tk view of the capture-only resource monitor. No desktop input operations."""
from __future__ import annotations

import time
import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageTk

from tibia_client.resource_monitor import (
    BUDGET_MS, FIELD_NAMES, ResourceMonitor, display_readings,
)

COLORS = {"Accepted": "#16804a", "N/A": "#a56800", "Stale": "#a56800", "Error": "#bb2929"}


class ResourceMonitorWindow:
    def __init__(self, parent=None, *, monitor=None, clock=time.monotonic):
        self.monitor = monitor if monitor is not None else ResourceMonitor()
        self.clock = clock
        self.window = tk.Tk() if parent is None else tk.Toplevel(getattr(parent, "windowID", parent))
        self.window.title("Resource monitor — observation only")
        self.window.geometry("920x700")
        self.window.minsize(780, 620)
        self._closed = False
        self._photos = []
        self._render_key = None
        self._after = None
        body = ttk.Frame(self.window, padding=12)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Resource monitor", font=("TkDefaultFont", 15, "bold")).pack(anchor="w")
        ttk.Label(
            body, text="Observation only • HP, Mana, Soul and Capacity • No game input",
        ).pack(anchor="w", pady=(3, 8))
        controls = ttk.Frame(body)
        controls.pack(fill="x")
        self.start_button = ttk.Button(controls, text="Start monitoring", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(controls, text="Stop", command=self.stop)
        self.stop_button.pack(side="left", padx=6)
        self.source = tk.StringVar(value="Uses the selected capture window")
        ttk.Label(controls, textvariable=self.source).pack(side="left", padx=8)
        self.status = tk.StringVar()
        ttk.Label(body, textvariable=self.status, wraplength=870).pack(fill="x", pady=8)
        columns = ("field", "value", "status", "age", "detail")
        self.table = ttk.Treeview(body, columns=columns, show="headings", height=4)
        for name, title, width in zip(
            columns, ("Field", "Value", "Status", "Age", "Detail"), (70, 90, 90, 80, 420)
        ):
            self.table.heading(name, text=title)
            self.table.column(name, width=width, minwidth=50, stretch=name == "detail")
        for status, color in COLORS.items():
            self.table.tag_configure(status, foreground=color)
        for field in FIELD_NAMES:
            self.table.insert("", "end", iid=field)
        self.table.pack(fill="x")
        self.table.selection_set(next(iter(FIELD_NAMES)))
        self.detail = tk.StringVar()
        ttk.Label(body, textvariable=self.detail, wraplength=870).pack(fill="x", pady=(4, 0))
        self.timing = tk.StringVar(value="Timing: waiting for a capture")
        self.timing_label = ttk.Label(body, textvariable=self.timing, wraplength=870)
        self.timing_label.pack(fill="x", pady=8)
        self.preview_title = tk.StringVar(value="No captured frame")
        ttk.Label(body, textvariable=self.preview_title).pack(anchor="w")
        images = ttk.Frame(body)
        images.pack(fill="both", expand=True, pady=4)
        images.columnconfigure(0, weight=3)
        images.columnconfigure(1, weight=2)
        images.rowconfigure(0, weight=1)
        self.preview = tk.Canvas(images, bg="#20242a", highlightthickness=0, height=280)
        self.preview.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        crops = ttk.Frame(images)
        crops.grid(row=0, column=1, sticky="nsew")
        self.crops = {}
        self.crop_boxes = {}
        for index, (field, name) in enumerate(FIELD_NAMES.items()):
            box = ttk.LabelFrame(crops, text=name, padding=4)
            box.grid(row=index, column=0, sticky="nsew", pady=2)
            canvas = tk.Canvas(box, bg="#20242a", height=48, highlightthickness=0)
            canvas.pack(fill="both", expand=True)
            self.crops[field] = canvas
            self.crop_boxes[field] = box
            crops.rowconfigure(index, weight=1)
        crops.columnconfigure(0, weight=1)
        ttk.Label(
            body,
            text="Accepted means the decoder accepted these pixels. Values become N/A after 500 ms.\n"
                 "Age is local capture age; continuous OBS/game freshness is unverified.",
            wraplength=870,
        ).pack(fill="x", pady=(8, 0))
        self.preview.bind("<Configure>", self._resize)
        for canvas in self.crops.values():
            canvas.bind("<Configure>", self._resize)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self._refresh()

    def start(self):
        try:
            self.monitor.start()
        except Exception as exc:
            self.status.set(f"Could not start: {exc}")

    def stop(self):
        self.monitor.stop()
        self._render_key = None
        if self._after is not None:
            self.window.after_cancel(self._after)
        self._refresh()

    def _resize(self, _event=None):
        self._render_key = None

    def _refresh(self):
        if self._closed:
            return
        state = self.monitor.state()
        rows = display_readings(state, self.clock())
        busy = self.monitor.busy
        self.start_button.configure(state="disabled" if busy else "normal")
        self.stop_button.configure(state="normal" if state.running else "disabled")
        self.source.set(state.source or "Uses the selected capture window")
        self.status.set(f"{state.phase}{': ' + state.detail if state.detail else ''}")
        for row in rows:
            age = "—" if row.age_ms is None else f"{row.age_ms:.0f} ms"
            self.table.item(
                row.field, values=(FIELD_NAMES[row.field], row.value, row.status, age, row.detail),
                tags=(row.status,),
            )
            self.crop_boxes[row.field].configure(
                text=f"{FIELD_NAMES[row.field]} · {row.value} · {row.status}"
            )
        selected = self.table.selection()
        selected_row = next((row for row in rows if row.field in selected), rows[0])
        self.detail.set(f"{FIELD_NAMES[selected_row.field]}: "
                        f"{selected_row.detail or 'Current pixels passed the decoder checks.'}")
        sample = state.sample
        if sample is None:
            self.timing.set("Timing: waiting for a capture")
            self.timing_label.configure(foreground="")
            self.preview_title.set("No current captured frame")
        else:
            over_budget = sample.total_ms > BUDGET_MS
            self.timing.set(
                f"Capture + transfer {sample.capture_ms:.1f} ms  •  Discovery {sample.discovery_ms:.1f} ms"
                f"  •  Decode {sample.decode_ms:.1f} ms\n"
                f"Capture request → result {sample.total_ms:.1f} ms"
                f"  /  {BUDGET_MS:.0f} ms budget — {'OVER BUDGET' if over_budget else 'within budget'}"
            )
            self.timing_label.configure(foreground=COLORS["N/A"] if over_budget else "")
            age = rows[0].age_ms
            suffix = "age unavailable" if age is None else f"local age {age:.0f} ms"
            self.preview_title.set(
                f"Last captured frame #{sample.sequence} • {suffix}"
                + (" • STALE" if rows[0].status == "Stale" else "")
            )
        key = (id(sample), tuple(row.status for row in rows))
        if key != self._render_key:
            self._render(sample, rows)
            self._render_key = key
        self._after = self.window.after(100, self._refresh)

    def _image(self, canvas, image, *, maximum_scale=1.0):
        width, height = max(2, canvas.winfo_width()), max(2, canvas.winfo_height())
        scale = min((width - 2) / image.width, (height - 2) / image.height, maximum_scale)
        size = max(1, round(image.width * scale)), max(1, round(image.height * scale))
        photo = ImageTk.PhotoImage(image.resize(size, Image.Resampling.NEAREST), master=self.window)
        self._photos.append(photo)
        left, top = (width - size[0]) / 2, (height - size[1]) / 2
        canvas.create_image(left, top, anchor="nw", image=photo)
        return left, top, scale

    def _render(self, sample, rows):
        self._photos.clear()
        self.preview.delete("all")
        for canvas in self.crops.values():
            canvas.delete("all")
        if sample is None:
            self.preview.create_text(12, 12, anchor="nw", fill="white", text="No current capture")
            return
        image = sample.frame.image
        left, top, scale = self._image(self.preview, image)
        for row in rows:
            canvas = self.crops[row.field]
            color = COLORS.get(row.status, "#9aa0a6")
            if row.region is None:
                canvas.create_text(
                    8, 8, anchor="nw", fill="white", text=f"{row.value} • No verified value region"
                )
                continue
            x1, y1, x2, y2 = row.region
            self.preview.create_rectangle(
                left + x1 * scale, top + y1 * scale,
                left + x2 * scale, top + y2 * scale, outline=color, width=2,
            )
            self._image(canvas, image.crop(row.region), maximum_scale=4.0)
            canvas.create_rectangle(
                1, 1, max(2, canvas.winfo_width()) - 1, max(2, canvas.winfo_height()) - 1,
                outline=color, width=2,
            )

    def close(self):
        if self._closed:
            return
        self.stop()
        self._closed = True
        self.monitor.close()
        if self._after is not None:
            self.window.after_cancel(self._after)
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="disabled")
        self.status.set("Closing capture…")
        self._finish_close()

    def _finish_close(self):
        if self.monitor.busy:
            self.window.after(50, self._finish_close)
        else:
            self.window.destroy()
