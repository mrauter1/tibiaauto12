"""Optional Tk coordinate display. Workers never access Tk or send game input."""
from __future__ import annotations

from pathlib import Path
import time
import tkinter as tk
from tkinter import filedialog, ttk

from PIL import Image, ImageTk
from modules.CoordinateMap import CoordinateMap

from tibia_client.coordinate_monitor import (
    AUTO_SCALES, DEFAULT_MAPS, DEFAULT_MIN_SCORE, CoordinateMonitor, CoordinateSettings, display_coordinate,
)


class CoordinateMonitorWindow:
    def __init__(self, parent=None, *, box=(0, 0, 0, 0),
                 monitor_factory=CoordinateMonitor, clock=time.monotonic):
        self.window = tk.Tk() if parent is None else tk.Toplevel(getattr(parent, 'windowID', parent))
        self.window.title('Predicted coordinates')
        self.window.geometry('1120x700')
        self.window.minsize(1000, 660)
        self.clock = clock
        self._factory = monitor_factory
        self.monitor = None
        self._closed = False
        self._after = None
        self._sample = None
        self._photo = None
        self.enabled = tk.BooleanVar(master=self.window, value=False)
        self.use_center = tk.BooleanVar(master=self.window, value=False)
        self.maps = tk.StringVar(master=self.window, value=str(DEFAULT_MAPS))
        self.bounds = tk.StringVar(master=self.window, value=' '.join(map(str, box)))
        self.scale = tk.StringVar(master=self.window, value='Auto')
        self.floor = tk.StringVar(master=self.window, value='Auto')
        self.minimum_match = tk.StringVar(master=self.window, value=f'{DEFAULT_MIN_SCORE*100:g}')
        self.coordinate = tk.StringVar(master=self.window, value='X —   Y —   Z —')
        self.status = tk.StringVar(master=self.window, value='Disabled')
        self.detail = tk.StringVar(master=self.window, value='Enable coordinate prediction to begin.')
        self.timing = tk.StringVar(master=self.window, value='No captured frame')
        panes = ttk.Panedwindow(self.window, orient='horizontal')
        panes.pack(fill='both', expand=True)
        body = ttk.Frame(panes, padding=14)
        panes.add(body, weight=0)
        self.world_map = CoordinateMap(panes)
        panes.add(self.world_map, weight=1)
        ttk.Label(body, text='Predicted coordinates', font=('TkDefaultFont', 16, 'bold')).pack(anchor='w')
        self.toggle = ttk.Checkbutton(body, text='Enable coordinate prediction',
                                      variable=self.enabled, command=self._toggle)
        self.toggle.pack(anchor='w', pady=(10, 12))
        self.value_label = ttk.Label(body, textvariable=self.coordinate, font=('Consolas', 23, 'bold'))
        self.value_label.pack(anchor='w')
        self.status_label = ttk.Label(body, textvariable=self.status, font=('TkDefaultFont', 11, 'bold'))
        self.status_label.pack(anchor='w', pady=(4, 0))
        ttk.Label(body, textvariable=self.detail, wraplength=650).pack(anchor='w', pady=(3, 6))
        ttk.Label(body, textvariable=self.timing).pack(anchor='w')
        self.preview = tk.Canvas(body, height=170, bg='#20242a', highlightthickness=0)
        self.preview.pack(fill='x', pady=10)
        self.preview.bind('<Configure>', lambda _event: self._invalidate_preview())
        options = ttk.LabelFrame(body, text='Minimap settings', padding=10)
        options.pack(fill='x')
        options.columnconfigure(1, weight=1)
        ttk.Label(options, text='Map folder').grid(row=0, column=0, sticky='w')
        self.map_entry = ttk.Entry(options, textvariable=self.maps)
        self.map_entry.grid(row=0, column=1, sticky='ew', padx=6)
        self.browse = ttk.Button(options, text='Browse…', command=self._browse)
        self.browse.grid(row=0, column=2)
        ttk.Label(options, text='Minimap bounds').grid(row=1, column=0, sticky='w', pady=6)
        self.box_entry = ttk.Entry(options, textvariable=self.bounds)
        self.box_entry.grid(row=1, column=1, columnspan=2, sticky='ew', padx=6)
        ttk.Label(options, text='Left Top Right Bottom — pixels in the captured client').grid(
            row=2, column=1, columnspan=2, sticky='w', padx=6)
        ttk.Label(options, text='Pixels per tile').grid(row=3, column=0, sticky='w', pady=6)
        self.scale_entry = ttk.Combobox(options, textvariable=self.scale,
                                        values=('Auto', '0.125', '0.15625', '0.25', '0.3125', '0.5', '0.625', '0.75', '1', '1.25', '1.5', '2', '2.5', '3', '4', '5', '6', '8', '10', '16'), width=12)
        self.scale_entry.grid(row=3, column=1, sticky='w', padx=6)
        ttk.Label(options, text='Displayed floor').grid(row=4, column=0, sticky='w')
        self.floor_entry = ttk.Combobox(options, textvariable=self.floor,
                                        values=('Auto', *map(str, range(16))), width=12)
        self.floor_entry.grid(row=4, column=1, sticky='w', padx=6)
        ttk.Label(options, text='Minimum match (%)').grid(row=5, column=0, sticky='w', pady=6)
        self.threshold_entry = ttk.Entry(options, textvariable=self.minimum_match, width=14)
        self.threshold_entry.grid(row=5, column=1, sticky='w', padx=6)
        self.center_check = ttk.Checkbutton(options, text='Use map center instead of the player crosshair (estimate)',
                                            variable=self.use_center)
        self.center_check.grid(row=6, column=0, columnspan=3, sticky='w', pady=(9, 0))
        self._inputs = (self.map_entry, self.box_entry, self.browse, self.scale_entry,
                        self.floor_entry, self.threshold_entry, self.center_check)
        ttk.Label(body, text='Unknown or stale results show no coordinate. Map data may be incomplete.\n'
                  'Observation only: this module does not move the character. '
                  'Capture age does not verify that an OBS/game image is live.', wraplength=650).pack(
                      anchor='w', pady=(12, 0))
        self.window.protocol('WM_DELETE_WINDOW', self.close)
        self._refresh()

    def _browse(self):
        path = filedialog.askdirectory(parent=self.window, title='Select minimap-world folder',
                                        initialdir=self.maps.get())
        if path:
            self.maps.set(path)

    def _settings(self):
        box = tuple(int(value) for value in self.bounds.get().replace(',', ' ').split())
        scale = self.scale.get().strip()
        floor = self.floor.get().strip()
        try:
            threshold = float(self.minimum_match.get().strip())/100
        except ValueError:
            raise ValueError('Minimum match must be a number between 0 and 100 percent') from None
        return CoordinateSettings(Path(self.maps.get().strip()), box,
                                  AUTO_SCALES if scale.lower() == 'auto' else (float(scale),),
                                  None if floor.lower() == 'auto' else int(floor),
                                  self.use_center.get(), min_score=threshold)

    def _toggle(self):
        if not self.enabled.get():
            self.stop()
            return
        try:
            if self.monitor is not None and self.monitor.busy:
                raise RuntimeError('Previous capture is still stopping; try again shortly.')
            settings = self._settings()
            if self.monitor is not None:
                self.monitor.close()
            self.monitor = self._factory(settings)
            self.monitor.start()
        except Exception as exc:
            if self.monitor is not None and not self.monitor.busy:
                self.monitor.close()
                self.monitor = None
            self.enabled.set(False)
            self.coordinate.set('X —   Y —   Z —')
            self.status.set('Cannot enable')
            self.detail.set(str(exc))

    def stop(self):
        self.enabled.set(False)
        if self.monitor is not None:
            self.monitor.stop()
        self.coordinate.set('X —   Y —   Z —')
        self.status.set('Disabled')
        self.detail.set('Coordinate prediction is disabled.')
        self.preview.delete('all')
        self._sample = None
        self.world_map.set_prediction(self.maps.get(), None, 'Disabled')

    def _invalidate_preview(self):
        self._sample = None

    def _refresh(self):
        if self._closed:
            return
        monitor = self.monitor
        busy = monitor is not None and monitor.busy
        state = monitor.state() if monitor is not None else None
        running = bool(state and state.running)
        if state is not None:
            reading = display_coordinate(state, self.clock())
            self.coordinate.set(reading.value)
            self.status.set(reading.status)
            self.detail.set(reading.detail)
            if not running:
                self.enabled.set(False)
            color = '#16804a' if reading.status == 'Predicted' else '#ad7200' if reading.status in (
                'Unknown', 'Stale', 'Map-center estimate', 'Ambiguous') else '#bb2929' if reading.status == 'Error' else ''
            self.status_label.configure(foreground=color)
            sample = state.sample
            folder = getattr(getattr(monitor, 'settings', None), 'maps', self.maps.get())
            self.world_map.set_prediction(folder, sample, reading.status)
            if sample is None:
                self.timing.set('No current captured frame')
                self.preview.delete('all')
                self._sample = None
            else:
                age = 'unavailable' if reading.age_ms is None else f'{reading.age_ms:.0f} ms'
                self.timing.set(f'Capture age: {age} · Capture to result: {sample.total_ms:.1f} ms')
                if self._sample is not sample:
                    self._render(sample)
                    self._sample = sample
        else:
            self.world_map.set_prediction(self.maps.get(), None, 'Disabled')
        for widget in self._inputs:
            widget.configure(state='disabled' if busy or running else 'normal')
        self.toggle.configure(state='disabled' if busy and not running else 'normal')
        self._after = self.window.after(100, self._refresh)

    def _render(self, sample):
        self.preview.delete('all')
        crop = sample.frame.crop(sample.box)
        factor = min(3., max(1, self.preview.winfo_width()-8)/crop.width,
                     max(1, self.preview.winfo_height()-8)/crop.height)
        size = (max(1, round(crop.width*factor)), max(1, round(crop.height*factor)))
        self._photo = ImageTk.PhotoImage(crop.resize(size, Image.Resampling.NEAREST), master=self.window)
        left, top = (self.preview.winfo_width()-size[0])/2, (self.preview.winfo_height()-size[1])/2
        self.preview.create_image(left, top, image=self._photo, anchor='nw')
        if sample.anchor is not None:
            x, y = left+sample.anchor[0]*factor, top+sample.anchor[1]*factor
            self.preview.create_oval(x-5, y-5, x+5, y+5, outline='#ff4040', width=2)

    def close(self):
        if self._closed:
            return
        self.stop()
        self._closed = True
        self.world_map.close()
        if self.monitor is not None:
            self.monitor.close()
        if self._after is not None:
            self.window.after_cancel(self._after)
        self.status.set('Closing capture…')
        self.toggle.configure(state='disabled')
        self._finish_close()

    def _finish_close(self):
        if self.monitor is not None and self.monitor.busy:
            self.window.after(50, self._finish_close)
        else:
            self.window.destroy()
