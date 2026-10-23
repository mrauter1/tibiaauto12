"""Window selection flow. No windows are enumerated or created at import time."""
from __future__ import annotations

from conf.conf_manager import ConfManager, ConfigError


def _display_name(hwnd: int, title: str) -> str:
    return f"{title}  [HWND {hwnd}]"


def choose_capture_window():
    import tkinter as tk
    from tkinter import messagebox

    from conf.WindowTitles import get_titles
    from core.Getters import CalibrationError
    from core.GetHWND import WindowLookupError, get_client_size, validate_window_pair
    from core.GUI import GUI, rgb, shutdown
    from core.runtime import stop_all

    ui = GUI("window chooser", "Choose capture and game windows")
    ui.MainWindow(None, [480, 285], [2, 2.36])
    window = ui.windowID
    window.configure(background=rgb((120, 98, 51)))

    capture_choice = tk.StringVar(master=window)
    input_choice = tk.StringVar(master=window)
    menus = []
    handles_by_label = {}
    initial = ConfManager.get("conf.json")
    from tibia_client.window_target import WindowTarget

    saved_target = WindowTarget.from_config(initial)
    target_mode = tk.StringVar(master=window, value=saved_target.kind)
    sandbox_target = saved_target.to_dict() if saved_target.kind == "sandbox" else None
    sandbox_hwnd = initial.get("input_hwnd") if sandbox_target else None

    tk.Label(window, text="Capture source (client or OBS)", bg=rgb((120, 98, 51))).place(x=15, y=15)
    tk.Label(window, text="Game input target", bg=rgb((120, 98, 51))).place(x=15, y=70)

    def close():
        stop_all(1.0)
        shutdown()

    def refresh():
        nonlocal handles_by_label
        try:
            titles = get_titles()
        except OSError as exc:
            messagebox.showerror("Window enumeration failed", str(exc), parent=window)
            return
        own_handle = window.winfo_id()
        handles_by_label = {
            _display_name(hwnd, title): hwnd
            for hwnd, title in titles.items()
            if hwnd != own_handle and title != window.title()
        }
        labels = list(handles_by_label)
        for option, variable in ((menus[0], capture_choice), (menus[1], input_choice)):
            menu = option["menu"]
            menu.delete(0, "end")
            for label in labels:
                menu.add_command(label=label, command=tk._setit(variable, label))
        config = ConfManager.get("conf.json")

        def preferred(key):
            handle = config.get(key) or config.get("hwnd")
            for label, candidate in handles_by_label.items():
                if candidate == handle:
                    return label
            return labels[0] if labels else ""

        capture_choice.set(preferred("capture_hwnd"))
        input_choice.set(preferred("input_hwnd"))
        if not labels:
            messagebox.showwarning(
                "No windows found", "No visible application windows were found. Open the game or OBS and click Refresh.",
                parent=window,
            )

    capture_menu = tk.OptionMenu(window, capture_choice, "")
    capture_menu.place(x=15, y=37, width=450, height=27)
    input_menu = tk.OptionMenu(window, input_choice, "")
    input_menu.place(x=15, y=92, width=450, height=27)
    menus.extend((capture_menu, input_menu))

    tk.Radiobutton(window, text="Local Tibia", variable=target_mode,
                   value="local").place(x=15, y=127)
    tk.Radiobutton(window, text="Windows Sandbox", variable=target_mode,
                   value="sandbox").place(x=140, y=127)
    target_status = tk.StringVar(master=window, value=(
        "Saved Sandbox viewport; reconfigure if its guest layout changed."
        if sandbox_target else "Select Local Tibia, or select Sandbox and configure its game area."
    ))
    tk.Label(window, textvariable=target_status, wraplength=445,
             justify="left").place(x=15, y=185, width=450, height=42)

    def configure_sandbox():
        nonlocal sandbox_target, sandbox_hwnd
        try:
            hwnd = handles_by_label[input_choice.get()]
            from modules.SandboxSetup import setup_sandbox

            candidate = setup_sandbox(window, hwnd)
            if candidate is not None:
                sandbox_target, sandbox_hwnd = candidate, hwnd
                target_mode.set("sandbox")
                capture_choice.set(input_choice.get())
                target_status.set("Sandbox area selected. Continue to calibrate.")
        except (KeyError, RuntimeError, ValueError, OSError) as exc:
            messagebox.showerror("Sandbox setup failed", str(exc), parent=window)

    tk.Button(window, text="Configure Sandbox area…",
              command=configure_sandbox).place(x=15, y=155, width=450, height=27)

    def continue_to_app(force_config=False):
        try:
            capture_hwnd = handles_by_label[capture_choice.get()]
            input_hwnd = handles_by_label[input_choice.get()]
            capture_title = capture_choice.get().rsplit("  [HWND ", 1)[0]
            input_title = input_choice.get().rsplit("  [HWND ", 1)[0]
            if target_mode.get() == "sandbox":
                if sandbox_target is None or input_hwnd != sandbox_hwnd:
                    raise WindowLookupError("Configure Sandbox area for the selected input window")
                selected_target = WindowTarget.from_config({"window_target": sandbox_target})
                # Read native dimensions under the same physical-pixel context
                # used by the capture and physical-input adapters.
                from tibia_client.window_target import _native_backend

                with _native_backend().physical_pixels():
                    host_size = get_client_size(input_hwnd)
                if host_size != selected_target.host_size:
                    raise WindowLookupError("Sandbox size changed; configure its area and focus again")
                capture_hwnd, capture_title = input_hwnd, input_title
                size = selected_target.size
            else:
                selected_target = WindowTarget()
                size = validate_window_pair(capture_hwnd, input_hwnd)

            def select(current):
                old_target = WindowTarget.from_config(current)
                current.update(
                    platform="windows", capture_hwnd=capture_hwnd, input_hwnd=input_hwnd,
                    hwnd=input_hwnd, capture_title=capture_title, input_title=input_title,
                    capture_size=list(size),
                )
                if selected_target.kind == "sandbox":
                    current["window_target"] = selected_target.to_dict()
                else:
                    current.pop("window_target", None)
                if force_config or old_target.fingerprint != selected_target.fingerprint:
                    current["configured"] = False

            config = ConfManager.update("conf.json", select)
        except (KeyError, ConfigError, WindowLookupError, OSError, ValueError) as exc:
            messagebox.showerror("Window selection is invalid", str(exc), parent=window)
            refresh()
            return

        ui.withdraw()
        try:
            if config.get("configured") and not force_config:
                from modules.Root import root
                root(input_title, config.get("preferences_name", "NewConfig.json"))
            else:
                from modules.ChooseConfig import ChooseConfig
                ChooseConfig(input_title)
        except (CalibrationError, ConfigError, OSError, ValueError, KeyError, TypeError) as exc:
            window.deiconify()
            messagebox.showerror(
                "Startup configuration is invalid",
                f"{exc}\n\nSelect Reconfigure to repair the profile.",
                parent=window,
            )

    tk.Button(window, text="Refresh", command=refresh).place(x=15, y=240, width=90, height=28)
    tk.Button(window, text="Reconfigure", command=lambda: continue_to_app(True)).place(x=180, y=240, width=110, height=28)
    tk.Button(window, text="Continue", command=continue_to_app).place(x=375, y=240, width=90, height=28)
    window.protocol("WM_DELETE_WINDOW", close)
    refresh()
    ui.loop()
