"""Keep cyclic Tk object finalization on the interpreter's owning thread."""
from __future__ import annotations

import gc
import threading


class TkGarbageCollector:
    """Workers may allocate, but only the Tk thread collects unreachable cycles.

    Tk Variables and images have Tcl-calling finalizers. Automatic collection
    inside a scheduler lock can wait for Tk while Tk waits for that same lock.
    Reference counting remains enabled; ordinary Python objects still release
    immediately. Only cyclic collection is scheduled here.
    """

    def __init__(self, window):
        self.window = window
        self.owner = threading.get_ident()
        self.was_enabled = gc.isenabled()
        self.closed = False
        self.timer = None
        gc.disable()
        self.timer = window.after(1000, self.collect)

    def collect(self):
        if threading.get_ident() != self.owner:
            raise RuntimeError("Tk garbage collection must run on the UI thread")
        self.timer = None
        if not self.closed:
            gc.collect()
            self.timer = self.window.after(1000, self.collect)

    def close(self):
        if threading.get_ident() != self.owner:
            raise RuntimeError("Tk garbage collection must close on the UI thread")
        if self.closed:
            return
        self.closed = True
        if self.timer is not None:
            self.window.after_cancel(self.timer)
            self.timer = None
        gc.collect()
        if self.was_enabled:
            gc.enable()
