"""Non-modal error window helpers."""
from __future__ import annotations

import os
import tkinter as tk
from PIL import Image, ImageTk

from core.GUI import GUI, application_root, rgb
from core.paths import project_path


class ExceptionsWindow:
    def __init__(self):
        self.windowID = None
        self.name = "TibiaAuto Error"
        self._gui = GUI("error", self.name)

    def ErrorWindow(self, BackgroundImage):
        self._gui.MainWindow(None, [370, 233], [2, 2])
        self.windowID = self._gui.windowID
        self.windowID.configure(background="#001000", takefocus=True)
        self.windowID.overrideredirect(True)
        self.windowID.wm_attributes("-topmost", True)
        if BackgroundImage is not None:
            image = Image.open(project_path(f"{BackgroundImage}.png"))
            photo = ImageTk.PhotoImage(image, master=self.windowID)
            label = tk.Label(self.windowID, image=photo, bg="#001000")
            label.image = photo
            label.pack()
        try:
            self.windowID.attributes("-transparentcolor", "#001000")
        except tk.TclError:
            pass

    def loop(self):
        return self._gui.loop()

    def Protocol(self, Function):
        return self.windowID.protocol("WM_DELETE_WINDOW", Function)

    def destroyWindow(self):
        return self._gui.destroyWindow()

    def UpdateWindow(self, X, Y):
        self.windowID.geometry("370x233+%d+%d" % (X - 185, Y - 187))
        self.windowID.update_idletasks()

    def addLabel(self, textOfLabel, position):
        label = tk.Label(self.windowID, text=textOfLabel, bg=rgb((114, 94, 48)), fg="white")
        label.place(x=position[0], y=position[1])
        return label

    def addImage(self, image, position):
        label = tk.Label(self.windowID, image=image, bg=rgb((114, 94, 48)), fg="white")
        label.place(x=position[0], y=position[1])
        return label

    def PositionOfWindow(self, value):
        if value == "X":
            return self.windowID.winfo_x()
        if value == "Y":
            return self.windowID.winfo_y()
        raise ValueError("position must be 'X' or 'Y'")


def GetSys():
    if os.name == "nt":
        import ctypes
        return ctypes.windll.user32.GetSystemMetrics(0), ctypes.windll.user32.GetSystemMetrics(1)
    root = application_root()
    if root is None:
        raise OSError("screen metrics require an active GUI on this platform")
    return root.winfo_screenwidth(), root.winfo_screenheight()


def CalculatingX(X):
    return (X / 2) - X * 0.18


def CalculatingY(Y):
    return (Y / 2) - Y * 0.15


def MousePosition():
    import pyautogui
    return tuple(pyautogui.position())


def OpenImage(image, size):
    opened = Image.open(project_path(image))
    opened = opened.resize((size[0], size[1]), Image.Resampling.LANCZOS)
    return ImageTk.PhotoImage(opened, master=application_root())
