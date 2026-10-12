"""Small Tk wrapper with one application root and one event loop."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from PIL import Image, ImageTk

from core.paths import project_path


def rgb(value):
    return "#%02x%02x%02x" % value


_application_root = None
_mainloop_running = False


def application_root():
    return _application_root


def _exists(widget) -> bool:
    try:
        return bool(widget.winfo_exists())
    except (AttributeError, tk.TclError):
        return False


def shutdown() -> None:
    """Destroy the application interpreter and all child windows."""
    global _application_root, _mainloop_running
    root = _application_root
    _application_root = None
    _mainloop_running = False
    if _exists(root):
        try:
            root.quit()
        finally:
            root.destroy()


class GUI:
    def __init__(self, windowID, name):
        self.windowID = windowID
        self.name = name
        self._owns_root = False

    def _new_window(self):
        global _application_root
        if not _exists(_application_root):
            self.windowID = tk.Tk()
            _application_root = self.windowID
            self._owns_root = True
        else:
            self.windowID = tk.Toplevel(_application_root)
            self._owns_root = False
        return self.windowID

    def _configure(self, background_image, sizes, positions):
        window = self._new_window()
        w, h = sizes
        x = int((window.winfo_screenwidth() - w) / positions[0])
        y = int((window.winfo_screenheight() - h) / positions[1])
        window.geometry(f"{w}x{h}+{x}+{y}")
        window.title(self.name)
        window.resizable(width=False, height=False)
        window.configure(background="#000", takefocus=True)
        try:
            window.iconbitmap(str(project_path("images/icon.ico")))
        except tk.TclError:
            pass
        if background_image:
            image = Image.open(project_path(f"images/Modules/{background_image}.png"))
            photo = ImageTk.PhotoImage(image, master=window)
            label = tk.Label(window, image=photo, bg="#000")
            label.image = photo
            label.pack()

    def MainWindow(self, BackgroundImage, sizes, positions):
        self._configure(BackgroundImage, sizes, positions)

    def DefaultWindow(self, BackgroundImage, sizes, positions):
        self._configure(BackgroundImage, sizes, positions)
        self.windowID.focus_set()

    def InvisibleWindow(self, BackgroundImage):
        self._new_window()
        self.windowID.resizable(width=False, height=False)
        self.windowID.geometry("130x130")
        self.windowID.image = tk.PhotoImage(
            file=str(project_path(f"images/BackgroundImages/{BackgroundImage}.png")),
            master=self.windowID,
        )
        tk.Label(self.windowID, image=self.windowID.image, bg="black").place(x=0, y=0)
        self.windowID.overrideredirect(True)
        self.windowID.wm_attributes("-topmost", True)
        try:
            self.windowID.attributes("-transparentcolor", "black")
        except tk.TclError:
            pass

    def loop(self):
        """Run Tk once for the root; child windows reuse that loop."""
        global _mainloop_running
        if not self._owns_root or _mainloop_running or not _exists(self.windowID):
            return
        _mainloop_running = True
        try:
            self.windowID.mainloop()
        finally:
            _mainloop_running = False

    def Protocol(self, Function):
        return self.windowID.protocol("WM_DELETE_WINDOW", Function)

    def UpdateWindow(self, X, Y):
        self.windowID.geometry("130x130+%d+%d" % (X - 65, Y - 65))
        self.windowID.update_idletasks()

    def destroyWindow(self):
        if _exists(self.windowID):
            if self._owns_root:
                self.windowID.withdraw()
            else:
                self.windowID.destroy()

    def withdraw(self):
        if _exists(self.windowID):
            self.windowID.withdraw()

    def PositionOfWindow(self, value):
        if value == "X":
            return self.windowID.winfo_x()
        if value == "Y":
            return self.windowID.winfo_y()
        raise ValueError("position must be 'X' or 'Y'")

    def addButton(self, textOfButton, command, sizes, positions):
        button = tk.Button(self.windowID, text=textOfButton,
            font=("Microsoft Sans Serif", 10), bg=rgb((114, 0, 0)), fg="white",
            command=command, cursor="hand2", activebackground=rgb((103, 13, 5)))
        button.place(w=sizes[0], h=sizes[1], x=positions[0], y=positions[1])
        return button

    def addCheck(self, variable, position, selected, textOfButton="", image=None):
        button = tk.Checkbutton(self.windowID, bg=rgb((114, 0, 3)),
            activebackground=rgb((114, 0, 3)), activeforeground="white",
            text=textOfButton, variable=variable, fg="white", selectcolor=rgb((114, 0, 3)),
            cursor="hand2", onvalue=True, offvalue=False, image=image)
        button.place(x=position[0], y=position[1])
        button.select() if selected else button.deselect()
        return button

    def addLabel(self, textOfLabel, position):
        label = tk.Label(self.windowID, text=textOfLabel, bg=rgb((114, 0, 0)), fg="white")
        label.place(x=position[0], y=position[1])
        return label

    def addList(self, columns, height, sizes, position):
        style = ttk.Style(self.windowID)
        try:
            style.theme_use("winnative")
        except tk.TclError:
            pass
        style.configure("Treeview.Heading", background="gray")
        frame = tk.Frame(self.windowID, height=sizes[1], width=sizes[0])
        frame.place(x=position[0], y=position[1])
        table = ttk.Treeview(self.windowID, columns=columns, height=height, show="headings")
        table.place(x=position[0], y=position[1])
        return table

    def addScrollbar(self):
        scrollbar = tk.Scrollbar(self.windowID, orient=tk.VERTICAL)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        return scrollbar

    def addMinimalLabel(self, textOfLabel, position, h=16):
        label = tk.Label(self.windowID, text=textOfLabel, bg=rgb((114, 0, 0)), fg="white")
        label.place(x=position[0], y=position[1], h=h)
        return label

    def addImage(self, image, position):
        label = tk.Label(self.windowID, image=image, bg=rgb((114, 0, 0)), fg="white")
        label.place(x=position[0], y=position[1])
        return label

    def addEntry(self, position, var, width=12):
        entry = tk.Entry(self.windowID, width=width, textvariable=var,
            bg=rgb((114, 0, 0)), borderwidth=2, foreground=rgb((81, 216, 0)))
        entry.place(x=position[0], y=position[1])
        return entry

    def addOption(self, variable, options, position, width=4):
        option = tk.OptionMenu(self.windowID, variable, *options)
        option.configure(bg=rgb((114, 0, 0)), fg="white",
            activebackground=rgb((103, 13, 5)), highlightthickness=0,
            width=width, cursor="hand2")
        option.place(x=position[0], y=position[1])
        return option

    def addRadio(self, text, variable, value, position, command=None):
        radio = tk.Radiobutton(self.windowID, text=text, variable=variable, value=value,
            fg="white", selectcolor=rgb((114, 0, 3)), cursor="hand2",
            bg=rgb((114, 0, 3)), command=command,
            activebackground=rgb((114, 0, 3)), activeforeground="white")
        radio.place(x=position[0], y=position[1])
        return radio

    def addRadioImage(self, text, variable, value, position, command=None, image=None):
        radio = tk.Radiobutton(self.windowID, text=text, variable=variable, value=value,
            fg="white", selectcolor=rgb((114, 0, 3)), cursor="hand2",
            bg=rgb((114, 0, 3)), command=command, image=image,
            activebackground=rgb((114, 0, 3)), activeforeground="white")
        radio.place(x=position[0], y=position[1])
        return radio

    def After(self, Time, Function):
        return self.windowID.after(Time, Function)

    def deiconify(self):
        return self.windowID.deiconify()

    @staticmethod
    def openImage(image, size):
        opened = Image.open(project_path(image))
        opened = opened.resize((size[0], size[1]), Image.Resampling.LANCZOS)
        return ImageTk.PhotoImage(opened, master=application_root())
