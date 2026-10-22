"""A Windows-owned stop shortcut with an event queue independent of Tk polling."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import threading


class WindowsStopHotkey:
    def __init__(self, callback):
        self.callback = callback
        self.ready = threading.Event()
        self.error = None
        self.thread_id = None
        self.api = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        self.api.RegisterHotKey.restype = wintypes.BOOL
        self.api.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        self.api.UnregisterHotKey.restype = wintypes.BOOL
        self.api.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        self.api.GetMessageW.restype = ctypes.c_int
        self.api.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.api.PostThreadMessageW.restype = wintypes.BOOL
        self.kernel.GetCurrentThreadId.restype = wintypes.DWORD
        self.thread = threading.Thread(target=self._run, name="stop-hotkey", daemon=True)
        self.thread.start()
        if not self.ready.wait(2):
            raise RuntimeError("Stop shortcut registration timed out")
        if self.error is not None:
            raise self.error

    def _run(self):
        self.thread_id = self.kernel.GetCurrentThreadId()
        # CONTROL | SHIFT | NOREPEAT, VK_F11. F12 is reserved for Windows debuggers.
        registered = self.api.RegisterHotKey(None, 1, 0x4006, 0x7A)
        if not registered:
            self.error = ctypes.WinError(ctypes.get_last_error())
        self.ready.set()
        if not registered:
            return
        try:
            message = wintypes.MSG()
            while self.api.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message == 0x0312 and message.wParam == 1:
                    self.callback()
        finally:
            self.api.UnregisterHotKey(None, 1)

    def close(self):
        if self.thread.is_alive():
            if not self.api.PostThreadMessageW(self.thread_id, 0x0012, 0, 0):
                raise ctypes.WinError(ctypes.get_last_error())
            self.thread.join(2)
