"""Open the observation-only monitor, without starting the automation app."""
from __future__ import annotations

import argparse
from functools import partial
import multiprocessing


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--hwnd", type=lambda value: int(value, 0),
        help="capture window handle (decimal or 0x hex); otherwise use the configured capture window",
    )
    args = parser.parse_args(argv)
    if args.hwnd is not None and args.hwnd <= 0:
        parser.error("--hwnd must be a positive window handle")
    from modules.ResourceMonitor import ResourceMonitorWindow
    from tibia_client.resource_monitor import ResourceMonitor, create_capture

    monitor = (ResourceMonitor() if args.hwnd is None else
               ResourceMonitor(capture_factory=partial(create_capture, args.hwnd)))
    window = ResourceMonitorWindow(monitor=monitor)
    try:
        window.window.mainloop()
    finally:
        monitor.close()
        monitor.wait()
    return 0


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
