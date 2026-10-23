"""Tk readings view for a shared session or an explicit standalone monitor."""
from __future__ import annotations

import time
import pprint
import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageTk

from tibia_client.resource_monitor import (
    BUDGET_MS, FIELD_NAMES, ResourceMonitor, display_readings,
)
from tibia_client.freshness import get_stale_ms, set_stale_ms, MIN_STALE_MS, MAX_STALE_MS

COLORS = {"Accepted": "#16804a", "N/A": "#a56800", "Stale": "#a56800", "Error": "#bb2929"}


class ResourceMonitorWindow:
    def __init__(self, parent=None, *, monitor=None, client=None, clock=time.monotonic):
        if monitor is not None and client is not None:
            raise ValueError("supply either monitor or client")
        if monitor is None and client is not None:
            from tibia_client.session_monitor import SessionResourceMonitor

            monitor = SessionResourceMonitor(client)
        self.monitor = monitor if monitor is not None else ResourceMonitor()
        self.clock = clock
        self.window = tk.Tk() if parent is None else tk.Toplevel(getattr(parent, "windowID", parent))
        self.window.title("Readings — resources, creatures and map")
        self.window.geometry("1160x850")
        self.window.minsize(850, 680)
        self._closed = False
        self._photos = []
        self._render_key = None
        self._after = None
        body = ttk.Frame(self.window, padding=12)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Readings", font=("TkDefaultFont", 15, "bold")).pack(anchor="w")
        ttk.Label(
            body, text="Resources • Creatures and targets • Minimap markers • Status and equipment • Observation only",
        ).pack(anchor="w", pady=(3, 8))
        controls = ttk.Frame(body)
        controls.pack(fill="x")
        self.start_button = ttk.Button(controls, text="Start monitoring", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(controls, text="Stop", command=self.stop)
        self.stop_button.pack(side="left", padx=6)
        ttk.Button(controls, text="Other screen", command=self.other_screen).pack(side="left")
        ttk.Button(controls, text="Recalibrate", command=self.monitor.recalibrate).pack(side="left", padx=6)
        self.source = tk.StringVar(value="Uses the selected capture window")
        ttk.Label(controls, textvariable=self.source).pack(side="left", padx=8)
        settings = ttk.Frame(body)
        settings.pack(fill='x', pady=(6, 0))
        ttk.Label(settings, text='Stale time (ms):').pack(side='left')
        self.stale_time = tk.StringVar(value=str(get_stale_ms()))
        self.stale_spin = ttk.Spinbox(settings, from_=MIN_STALE_MS, to=MAX_STALE_MS,
                                      increment=100, width=8, textvariable=self.stale_time)
        self.stale_spin.pack(side='left', padx=6)
        ttk.Button(settings, text='Apply', command=self.apply_stale_time).pack(side='left')
        self.stale_feedback = tk.StringVar(value=f'{get_stale_ms()} ms active · applies to Readings and bot')
        ttk.Label(settings, textvariable=self.stale_feedback).pack(side='left', padx=8)
        self.stale_spin.bind('<Return>', self.apply_stale_time)
        self.status = tk.StringVar()
        ttk.Label(body, textvariable=self.status, wraplength=870).pack(fill="x", pady=8)
        table_frame = ttk.Frame(body)
        table_frame.pack(fill='both', expand=False)
        columns = ("group", "field", "value", "status", "age", "detail")
        self.table = ttk.Treeview(table_frame, columns=columns, show="headings", height=12, selectmode='browse')
        for name, title, width in zip(
            columns, ("Group", "Field", "Value", "Status", "Age", "Detail"), (110, 145, 180, 75, 70, 440)
        ):
            self.table.heading(name, text=title)
            self.table.column(name, width=width, minwidth=50, stretch=name == "detail")
        for status, color in COLORS.items():
            self.table.tag_configure(status, foreground=color)
        for field in FIELD_NAMES:
            self.table.insert("", "end", iid=field)
        self.table.pack(side='left', fill='both', expand=True)
        scrollbar = ttk.Scrollbar(table_frame, orient='vertical', command=self.table.yview)
        scrollbar.pack(side='right', fill='y')
        self.table.configure(yscrollcommand=scrollbar.set)
        self.table.bind('<<TreeviewSelect>>', self._resize)
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
        self.crop_box = ttk.LabelFrame(crops, text='Selected reading — exact source pixels', padding=4)
        self.crop_box.pack(fill='both', expand=True)
        self.crop = tk.Canvas(self.crop_box, bg='#20242a', height=130, highlightthickness=0)
        self.crop.pack(fill='both', expand=True)
        self.evidence = tk.Text(crops, height=8, width=36, wrap='word', font=('Consolas', 9))
        self.evidence.pack(fill='both', expand=True, pady=(6, 0))
        self.evidence.configure(state='disabled')
        self._evidence_text = None
        ttk.Label(
            body,
            text="Select a row to inspect its source pixels, coordinates and detector evidence. Stale time is configurable above.\n"
                 "Age is local capture age. Template absence and accepted OCR are detector results, not independent verification.",
            wraplength=870,
        ).pack(fill="x", pady=(8, 0))
        self.preview.bind("<Configure>", self._resize)
        self.crop.bind("<Configure>", self._resize)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self._refresh()

    def apply_stale_time(self, _event=None):
        try:
            value = set_stale_ms(int(self.stale_time.get()))
        except (ValueError, OSError) as exc:
            self.stale_feedback.set(str(exc))
            return
        self.stale_time.set(str(value))
        self.stale_feedback.set(f'{value} ms active · saved for Readings and bot')
        self._render_key = None

    def other_screen(self):
        from core.window_placement import move_to_other_screen
        try:
            move_to_other_screen(self.window)
        except (OSError, ValueError) as exc:
            self.status.set(str(exc))

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
        stale_ms = get_stale_ms()
        rows = display_readings(state, self.clock(), stale_ms=stale_ms)
        busy = self.monitor.busy
        self.start_button.configure(state="disabled" if busy else "normal")
        self.stop_button.configure(state="normal" if state.running else "disabled")
        self.source.set(state.source or "Uses the selected capture window")
        self.status.set(f"{state.phase}{': ' + state.detail if state.detail else ''}"
                        + (f" · {state.recovery}" if state.recovery else ""))
        for row in rows:
            if not self.table.exists(row.field):
                self.table.insert('', 'end', iid=row.field)
            age = "—" if row.age_ms is None else f"{row.age_ms:.0f} ms"
            self.table.item(
                row.field, values=(row.group, row.label, row.value, row.status, age, row.detail),
                tags=(row.status,),
            )
        active = {row.field for row in rows}
        for iid in self.table.get_children():
            if iid not in active:
                self.table.delete(iid)
        desired_order = tuple(row.field for row in rows)
        if self.table.get_children() != desired_order:
            for index, iid in enumerate(desired_order):
                self.table.move(iid, '', index)
        selected = self.table.selection()
        selected_row = next((row for row in rows if row.field in selected), rows[0])
        self.detail.set(f"{selected_row.label}: "
                        f"{selected_row.detail or 'Current pixels passed the decoder checks.'}")
        sample = state.sample
        if sample is None:
            self.timing.set("Timing: waiting for a capture")
            self.timing_label.configure(foreground="")
            self.preview_title.set("No current captured frame")
        else:
            over_budget = sample.total_ms > BUDGET_MS
            self.timing.set(
                f"Capture + transfer {sample.capture_ms:.1f} ms  •  Last background search {sample.discovery_ms:.1f} ms"
                f"  •  Decode {sample.decode_ms:.1f} ms\n"
                f"Capture request → result {sample.total_ms:.1f} ms"
                f"  /  {BUDGET_MS:.0f} ms budget — {'OVER BUDGET' if over_budget else 'within budget'}"
                + (f"  •  Visual decode {state.visual.decode_ms:.1f} ms" if state.visual else '')
            )
            self.timing_label.configure(foreground=COLORS["N/A"] if over_budget else "")
            age = rows[0].age_ms
            suffix = "age unavailable" if age is None else f"local age {age:.0f} ms"
            self.preview_title.set(
                f"Last captured frame #{sample.sequence} • {suffix}"
                + (" • STALE" if rows[0].status == "Stale" else "")
            )
        preview = state.preview
        if preview is not None:
            age = max(0, (self.clock()-preview.observed_at)*1000)
            self.preview_title.set(f"Live preview #{preview.sequence} · age {age:.0f} ms"
                                   + (" · STALE" if age > stale_ms else ""))
        selected_sample = sample if selected_row.field in FIELD_NAMES else state.visual
        self.crop_box.configure(text=f'{selected_row.label} · {selected_row.value} · {selected_row.status}')
        if selected_sample is not None:
            reading = selected_sample.readings.get(selected_row.field)
            evidence = (f'Source frame #{selected_sample.sequence}\nRegion: {selected_row.region}\n'
                        + (pprint.pformat(dict(reading.diagnostics), width=55, sort_dicts=False) if reading else ''))
        else:
            evidence = selected_row.detail
        if evidence != self._evidence_text:
            self.evidence.configure(state='normal')
            self.evidence.delete('1.0', 'end')
            self.evidence.insert('1.0', evidence)
            self.evidence.configure(state='disabled')
            self._evidence_text = evidence
        key = (id(preview), id(selected_sample), selected_row.field, selected_row.status)
        if key != self._render_key:
            self._render(selected_sample, selected_row, preview)
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

    def _render(self, sample, row, preview=None):
        self._photos.clear()
        self.preview.delete("all")
        self.crop.delete('all')
        if sample is None and preview is None:
            self.preview.create_text(12, 12, anchor="nw", fill="white", text="No current capture")
            return
        image = (preview or sample).frame.image
        left, top, scale = self._image(self.preview, image)
        if sample is None:
            return
        canvas = self.crop
        color = COLORS.get(row.status, '#9aa0a6')
        if row.region is None:
            canvas.create_text(8, 8, anchor='nw', fill='white', text=f'{row.value} • No source region')
            return
        x1, y1, x2, y2 = row.region
        # Never draw old coordinates on a different live frame.
        if preview is None or preview.sequence == sample.sequence:
            self.preview.create_rectangle(
                left + x1 * scale, top + y1 * scale,
                left + x2 * scale, top + y2 * scale, outline=color, width=2,
            )
        crop = Image.fromarray(sample.frame.pixels[y1:y2, x1:x2])
        crop_left, crop_top, crop_scale = self._image(canvas, crop, maximum_scale=4.0)
        reading = sample.readings.get(row.field)
        if reading:
            for box in reading.diagnostics.get('boxes', ()):
                canvas.create_rectangle(crop_left+(box[0]-x1)*crop_scale, crop_top+(box[1]-y1)*crop_scale,
                                        crop_left+(box[2]-x1)*crop_scale, crop_top+(box[3]-y1)*crop_scale,
                                        outline=color, width=2)
        canvas.create_rectangle(1, 1, max(2, canvas.winfo_width())-1, max(2, canvas.winfo_height())-1,
                                outline=color, width=2)

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
        self.status.set("Closing readings…")
        self._finish_close()

    def _finish_close(self):
        if self.monitor.busy:
            self.window.after(50, self._finish_close)
        else:
            self.window.destroy()
