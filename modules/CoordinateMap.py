"""Read-only atlas viewer for the optional coordinate monitor."""
from concurrent.futures import ThreadPoolExecutor
import json
import math
from pathlib import Path
import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageTk


def load_floor(folder, floor):
    folder = Path(folder).resolve()
    manifest = json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    item = next((item for item in manifest['floors'] if item['floor'] == floor), None)
    if item is None:
        raise ValueError(f'Floor {floor} is absent from this atlas')
    path = (folder/item['file']).resolve()
    if not path.is_relative_to(folder):
        raise ValueError('Floor image must be inside the map folder')
    with Image.open(path) as source:
        image = source.convert('RGB')
    return image, (int(manifest['origin_x']), int(manifest['origin_y']))


class CoordinateMap(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent, padding=(0, 14, 14, 14))
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='coordinate-map')
        self._future = None
        self._loading = self._wanted = self._loaded = None
        self._image = self._photo = None
        self._origin = (0, 0)
        self._point = None
        self._reference = ''
        self._folder = self._view_folder = None
        self._status = 'Disabled'
        self._error = ''
        self._zoom = 1.
        self._center = (0., 0.)
        self._fitted = True
        self._drag = None
        self._closed = False
        self._draw_id = None
        self.follow = tk.BooleanVar(self, value=True)
        self.floor = tk.StringVar(self, value='Auto')
        self.caption = tk.StringVar(self, value='Enable prediction to locate the player on the world map.')
        self.cursor = tk.StringVar(self, value='Wheel: zoom  |  Drag: pan')
        ttk.Label(self, text='World map', font=('TkDefaultFont', 14, 'bold')).pack(anchor='w')
        toolbar = ttk.Frame(self)
        toolbar.pack(fill='x', pady=(8, 5))
        ttk.Checkbutton(toolbar, text='Follow', variable=self.follow,
                        command=self._follow).pack(side='left')
        ttk.Button(toolbar, text='Fit world', command=self.fit, width=9).pack(side='left', padx=4)
        ttk.Button(toolbar, text='−', command=lambda: self.zoom(.5), width=3).pack(side='left')
        ttk.Button(toolbar, text='+', command=lambda: self.zoom(2.), width=3).pack(side='left', padx=3)
        floors = ttk.Frame(self)
        floors.pack(fill='x', pady=(0, 6))
        ttk.Label(floors, text='Floor / Z').pack(side='left')
        self.floor_entry = ttk.Combobox(floors, textvariable=self.floor,
            values=('Auto', *map(str, range(16))), state='readonly', width=6)
        self.floor_entry.pack(side='left', padx=6)
        self.floor_entry.bind('<<ComboboxSelected>>', self._select_floor)
        ttk.Button(floors, text='Up', command=lambda: self._step_floor(-1), width=5).pack(side='left')
        ttk.Button(floors, text='Down', command=lambda: self._step_floor(1), width=5).pack(side='left', padx=3)
        self.canvas = tk.Canvas(self, width=380, height=390, bg='#14191f',
                                highlightthickness=0, cursor='crosshair')
        self.canvas.pack(fill='both', expand=True)
        ttk.Label(self, textvariable=self.caption, wraplength=360).pack(anchor='w', pady=(8, 4))
        ttk.Label(self, textvariable=self.cursor).pack(anchor='w')
        self.canvas.bind('<Configure>', self._resize)
        self.canvas.bind('<MouseWheel>', self._wheel)
        self.canvas.bind('<Button-4>', lambda e: self.zoom(1.5, (e.x, e.y)))
        self.canvas.bind('<Button-5>', lambda e: self.zoom(1/1.5, (e.x, e.y)))
        self.canvas.bind('<ButtonPress-1>', self._pan_start)
        self.canvas.bind('<B1-Motion>', self._pan)
        self.canvas.bind('<ButtonRelease-1>', lambda _e: setattr(self, '_drag', None))
        self.canvas.bind('<Motion>', self._motion)
        self._poll_id = self.after(75, self._poll)

    def set_prediction(self, folder, sample, status):
        """Only fresh, accepted display states may supply a visible marker."""
        if self._closed:
            return
        self._folder = str(Path(folder).resolve())
        self._status = status
        result = sample.result if sample is not None else None
        valid = status in ('Predicted', 'Map-center estimate', 'Ambiguous') and result is not None and result.accepted
        point = (result.x, result.y, result.z) if valid else None
        reference = sample.reference if valid else ''
        changed = point != self._point or reference != self._reference
        self._point, self._reference = point, reference
        self._select_floor()
        if valid and changed and self.follow.get() and self._image is not None:
            self._center_on_point()
        if changed:
            self._queue_draw()

    def _select_floor(self, _event=None):
        if self._closed or self._folder is None:
            return
        selection = self.floor.get()
        if selection == 'Auto':
            z = self._point[2] if self._point is not None else self._wanted[1] if self._wanted else None
        else:
            z = int(selection)
        if z is not None:
            key = (self._folder, z)
            if key != self._wanted:
                self._wanted = key
                self._image = self._photo = None
                self._loaded = None
                self._error = ''
                self.canvas.delete('all')
                self._start_load()
        self._caption()

    def _step_floor(self, delta):
        selection = self.floor.get()
        base = (self._point[2] if self._point is not None else
                self._wanted[1] if self._wanted else 7) if selection == 'Auto' else int(selection)
        self.floor.set(str(max(0, min(15, base+delta))))
        self._select_floor()

    def _start_load(self):
        if self._future is None and self._wanted is not None and not self._closed:
            self._loading = self._wanted
            self._future = self._executor.submit(load_floor, *self._loading)

    def _poll(self):
        if self._closed:
            return
        if self._future is not None and self._future.done():
            future, key = self._future, self._loading
            self._future = None
            try:
                image, origin = future.result()
                if key == self._wanted:
                    same_atlas = self._view_folder == key[0]
                    center_shift = (self._origin[0]-origin[0], self._origin[1]-origin[1])
                    self._image, self._origin, self._loaded = image, origin, key
                    self._view_folder = key[0]
                    if same_atlas:
                        self._center = (self._center[0]+center_shift[0], self._center[1]+center_shift[1])
                        self._queue_draw()
                    else:
                        self.fit()
                    if self.follow.get():
                        self._center_on_point()
                    self._caption()
            except Exception as exc:
                if key == self._wanted:
                    self._error = f'Map unavailable: {exc}'
                    self._caption()
            if key != self._wanted:
                self._start_load()
        self._poll_id = self.after(75, self._poll)

    def _caption(self):
        tag = 'Ambiguous · ' if self._status == 'Ambiguous' and self._point is not None else ''
        if self._error:
            self.caption.set(self._error)
        elif self._wanted is not None and self._image is None:
            self.caption.set(f'Loading floor {self._wanted[1]}…')
        elif self._loaded is not None and self._point is not None and self._point[2] != self._loaded[1]:
            self.caption.set(f'{tag}Viewing floor {self._loaded[1]} · prediction is on floor {self._point[2]}')
        elif self._point is not None:
            x, y, z = self._point
            label = 'Map-center estimate' if self._reference == 'Map center' else 'Predicted player'
            self.caption.set(f'{tag}{label}: X {x}  Y {y}  Floor {z}')
        else:
            floor = f'Viewing floor {self._loaded[1]} · ' if self._loaded else ''
            self.caption.set(f'{floor}{self._status} — no current position')

    def _center_on_point(self):
        if self.point_on_canvas() is not None:
            self._center = (self._point[0]-self._origin[0]+.5,
                            self._point[1]-self._origin[1]+.5)
            self._fitted = False
            self._queue_draw()

    def _follow(self):
        if self.follow.get():
            self._center_on_point()

    def fit(self):
        if self._image is None:
            return
        w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        self._zoom = max(.02, min(max(1, w-20)/self._image.width,
                                 max(1, h-20)/self._image.height))
        self._center = (self._image.width/2, self._image.height/2)
        self._fitted = True
        self._queue_draw()

    def _resize(self, _event):
        if self._fitted:
            self.fit()
        else:
            self._queue_draw()

    def zoom(self, factor, anchor=None):
        if self._image is None:
            return
        w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        px, py = anchor if anchor is not None else (w/2, h/2)
        old = self._zoom
        new = max(.02, min(16., old*factor))
        self._center = (self._center[0]+(px-w/2)*(1/old-1/new),
                        self._center[1]+(py-h/2)*(1/old-1/new))
        self._zoom, self._fitted = new, False
        self._queue_draw()

    def _wheel(self, event):
        if event.delta:
            self.zoom(1.5 if event.delta > 0 else 1/1.5, (event.x, event.y))
        return 'break'

    def _pan_start(self, event):
        self._drag = (event.x, event.y, self._center)

    def _pan(self, event):
        if self._drag is not None and self._image is not None:
            x, y, center = self._drag
            self.follow.set(False)
            self._fitted = False
            self._center = (center[0]-(event.x-x)/self._zoom,
                            center[1]-(event.y-y)/self._zoom)
            self._queue_draw()

    def _motion(self, event):
        if self._image is not None:
            x = self._center[0]+(event.x-self.canvas.winfo_width()/2)/self._zoom
            y = self._center[1]+(event.y-self.canvas.winfo_height()/2)/self._zoom
            self.cursor.set(f'X {self._origin[0]+math.floor(x)}  Y {self._origin[1]+math.floor(y)}'
                            f'  |  {self._zoom:g} px/tile')

    def point_on_canvas(self):
        if (self._point is None or self._image is None or self._loaded != self._wanted
                or self._point[2] != self._loaded[1]):
            return None
        return ((self._point[0]-self._origin[0]+.5-self._center[0])*self._zoom+self.canvas.winfo_width()/2,
                (self._point[1]-self._origin[1]+.5-self._center[1])*self._zoom+self.canvas.winfo_height()/2)

    def _queue_draw(self):
        if not self._closed and self._draw_id is None:
            self._draw_id = self.after_idle(self._draw)

    def _draw(self):
        self._draw_id = None
        self.canvas.delete('all')
        if self._image is None or self._closed:
            return
        w, h = max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())
        # Render only the visible viewport; never allocate a zoomed world image.
        image = self._image.transform((w, h), Image.Transform.AFFINE,
            (1/self._zoom, 0, self._center[0]-w/(2*self._zoom),
             0, 1/self._zoom, self._center[1]-h/(2*self._zoom)),
            resample=Image.Resampling.NEAREST, fillcolor='#14191f')
        self._photo = ImageTk.PhotoImage(image, master=self)
        self.canvas.create_image(0, 0, anchor='nw', image=self._photo)
        point = self.point_on_canvas()
        if point is not None:
            x, y = point
            self.canvas.create_oval(x-8, y-8, x+8, y+8, outline='white', width=4, tags='position')
            self.canvas.create_oval(x-8, y-8, x+8, y+8, outline='#ff3030', width=2, tags='position')
            self.canvas.create_line(x-13, y, x+13, y, fill='#ff3030', width=2, tags='position')
            self.canvas.create_line(x, y-13, x, y+13, fill='#ff3030', width=2, tags='position')

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.after_cancel(self._poll_id)
        if self._draw_id is not None:
            self.after_cancel(self._draw_id)
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._image = self._photo = None
        # Release Tcl variables on the UI thread. Otherwise a later allocation
        # in the capture worker can trigger cyclic GC and their Tcl destructors.
        self.follow = self.floor = self.caption = self.cursor = None
