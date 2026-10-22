"""Calibrate unfamiliar client layouts against the exact captured pixels."""
from copy import deepcopy
import tkinter as tk
from tkinter import ttk, messagebox
from PIL import ImageTk, Image

from conf.conf_manager import ConfManager
from core.Getters import SetSQMs, validate_calibration
from core.perception import validate_region
from modules.SandboxSetup import PreviewMapping


REGIONS = (
    ("HealthBar", "Health: drag the entire inner bar, including its empty portion"),
    ("ManaBar", "Mana: drag the entire inner bar, including its empty portion"),
    ("GameWindowBox", "Game: drag the rendered 15 × 11 tile area, excluding grey margins"),
    ("MapBox", "Minimap: drag the map image, excluding its buttons"),
    ("BattleBox", "Battle list: drag the open list contents, excluding its title"),
    ("StatusBarBox", "Status: drag the row of condition icons below equipment"),
    ("RingBox", "Ring: drag the equipment ring slot"),
    ("AmuletBox", "Amulet: drag the equipment necklace slot"),
)


def build_profile(frame, regions):
    from modules.ChooseConfig import _set_box, _set_point
    from engine.ScanStages import read_bar_region

    profile = deepcopy(ConfManager.get("Json.json"))
    for name, _ in REGIONS:
        validate_region(regions[name], frame.size)
    for name, kind in (("HealthBar", "health"), ("ManaBar", "mana")):
        if read_bar_region(frame, regions[name], kind) is None:
            raise ValueError(f"{name}: no readable coloured bar; select its full inner rectangle")
    for name, _ in REGIONS[2:]:
        _set_box(profile, name, regions[name])
    profile["ResourceBars"] = {name: list(regions[name]) for name in ("HealthBar", "ManaBar")}
    slot_width = sum(regions[name][2]-regions[name][0] for name in ("RingBox", "AmuletBox"))/2
    profile["ui_scale"] = max(.5, min(3, round(slot_width/32*4)/4))
    for point, bar in (("LifePosition", "HealthBar"), ("ManaPosition", "ManaBar")):
        l, t, r, b = regions[bar]
        _set_point(profile, point, (l, (t+b)//2))
    game = regions["GameWindowBox"]
    l, t, r, b = game
    if abs((r-l)/(b-t) - 15/11) > .05:
        raise ValueError("Game area must contain the rendered 15 × 11 tiles, not the grey margins")
    player = ((l+r)//2, (t+b)//2)
    _set_point(profile, "PlayerPosition", player)
    _set_point(profile, "BattlePosition", regions["BattleBox"][:2])
    sqms = SetSQMs(player, game)
    for number in range(1, 10):
        profile["SQM"][f"SQM{number}"][0].update(
            x=sqms[(number-1)*2], y=sqms[(number-1)*2+1], Stats=True)
    profile.update(Stats=True, calibration_size=list(frame.size), calibration_method="image-regions-v1")
    validate_calibration(profile, frame.size)
    return profile


class ImageCalibration:
    def __init__(self, parent, frame, reason):
        self.frame, self.regions, self.result = frame, {}, None
        self.window = tk.Toplevel(parent)
        self.window.title("Calibrate client image")
        self.window.resizable(False, False)
        self.window.protocol("WM_DELETE_WINDOW", self.window.destroy)
        ttk.Label(self.window, text="The automatic templates do not match this layout. Select the actual regions.").pack(anchor="w", padx=8)
        ttk.Label(self.window, text=reason, wraplength=990).pack(anchor="w", padx=8)
        self.choice = ttk.Combobox(self.window, state="readonly", width=120,
                                  values=[label for _, label in REGIONS])
        self.choice.current(0)
        self.choice.pack(fill="x", padx=8, pady=4)
        self.choice.bind("<<ComboboxSelected>>", lambda _: self.draw())
        self.mapping = PreviewMapping((1000, 480), frame.size, padding=4)
        self.canvas = tk.Canvas(self.window, width=1000, height=480, highlightthickness=0)
        self.canvas.pack(padx=8)
        x, y, w, h = self.mapping.bounds
        self.photo = ImageTk.PhotoImage(frame.image.resize((w,h), Image.Resampling.LANCZOS), master=self.window)
        self.canvas.create_image(x, y, anchor="nw", image=self.photo)
        self.canvas.bind("<ButtonPress-1>", self.start)
        self.canvas.bind("<B1-Motion>", self.move)
        self.canvas.bind("<ButtonRelease-1>", self.finish)
        self.anchor = None
        self.status = tk.StringVar(value="Drag each region. Selections advance automatically; choose a row above to correct it.")
        ttk.Label(self.window, textvariable=self.status).pack(anchor="w", padx=8, pady=4)
        self.save = ttk.Button(self.window, text="Save calibration", command=self.accept, state="disabled")
        self.save.pack(side="right", padx=8, pady=6)
        ttk.Button(self.window, text="Cancel", command=self.window.destroy).pack(side="right", pady=6)
        self.window.lift()
        self.window.grab_set()

    def start(self, event):
        if self.mapping.contains(event.x, event.y):
            self.anchor = self.mapping.image_edge(event.x, event.y)

    def move(self, event):
        if self.anchor is None:
            return
        x, y = self.mapping.image_edge(event.x, event.y, clamp=True)
        a, b = self.anchor
        self.pending = (min(a,x), min(b,y), max(a,x), max(b,y))
        self.canvas.delete("pending")
        self.canvas.create_rectangle(*self.mapping.canvas_rectangle(self.pending), outline="#ffff00", width=2, tags="pending")

    def finish(self, event):
        if self.anchor is None:
            return
        self.move(event)
        self.anchor = None
        l,t,r,b = self.pending
        if r-l < 3 or b-t < 2:
            self.status.set("Rectangle is too small; drag again.")
            return
        index = self.choice.current()
        self.regions[REGIONS[index][0]] = self.pending
        self.choice.current(min(index+1, len(REGIONS)-1))
        self.draw()
        self.status.set(f"{len(self.regions)}/8 regions selected. Saved coordinates refer to the original capture.")
        self.save.configure(state="normal" if len(self.regions)==8 else "disabled")

    def draw(self):
        self.canvas.delete("pending")
        self.canvas.delete("selection")
        for name, box in self.regions.items():
            bounds = self.mapping.canvas_rectangle(box)
            self.canvas.create_rectangle(*bounds, outline="#00ffff", width=1, tags="selection")
            self.canvas.create_text(bounds[0]+2, bounds[1]+2, text=name, anchor="nw", fill="#ffff00", tags="selection")

    def accept(self):
        try:
            self.result = build_profile(self.frame, self.regions)
        except (ValueError, KeyError) as exc:
            messagebox.showerror("Check selection", str(exc), parent=self.window)
            return
        self.window.destroy()

    def run(self):
        self.window.wait_window()
        return self.result
