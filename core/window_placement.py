"""Placement of this application's own windows on another monitor."""

def _other_screen_bounds(window, work_areas):
    areas = list(dict.fromkeys(tuple(area) for area in work_areas))
    if len(areas) < 2:
        raise ValueError("Only one distinct screen was detected")
    left, top, right, bottom = window

    def overlap(area):
        return max(0, min(right, area[2]) - max(left, area[0])) * max(
            0, min(bottom, area[3]) - max(top, area[1]))

    current = max(range(len(areas)), key=lambda index: overlap(areas[index]))
    target = areas[(current + 1) % len(areas)]
    return (target[0] + 8, target[1] + 8,
            min(right - left, target[2] - target[0] - 16),
            min(bottom - top, target[3] - target[1] - 16))


def move_to_other_screen(root):
    """Move this app's own window; never inspect or move the game window."""
    import ctypes
    from ctypes import wintypes

    class MonitorInfo(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("monitor", wintypes.RECT),
                    ("work", wintypes.RECT), ("flags", wintypes.DWORD)]

    api = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE, wintypes.HDC,
                                      ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    api.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), callback_type, wintypes.LPARAM]
    api.EnumDisplayMonitors.restype = wintypes.BOOL
    api.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
    api.GetMonitorInfoW.restype = wintypes.BOOL
    api.GetAncestor.argtypes, api.GetAncestor.restype = [wintypes.HWND, wintypes.UINT], wintypes.HWND
    api.GetWindowRect.argtypes, api.GetWindowRect.restype = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL
    api.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT]
    api.SetWindowPos.restype = wintypes.BOOL
    areas = []

    def collect(monitor, dc, rect, data):
        info = MonitorInfo()
        info.size = ctypes.sizeof(info)
        if api.GetMonitorInfoW(monitor, ctypes.byref(info)):
            areas.append((info.work.left, info.work.top, info.work.right, info.work.bottom))
        return True

    if not api.EnumDisplayMonitors(None, None, callback_type(collect), 0):
        raise OSError("Could not enumerate screen work areas")
    root.update_idletasks()
    hwnd = api.GetAncestor(root.winfo_id(), 2)
    rect = wintypes.RECT()
    if not hwnd or not api.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise OSError("Could not read the application window position")
    bounds = _other_screen_bounds((rect.left, rect.top, rect.right, rect.bottom), areas)
    if not api.SetWindowPos(hwnd, None, *bounds, 0x0014):
        raise OSError("Could not move the application to the other screen")
    return {"work_areas": areas, "window_bounds": bounds}
