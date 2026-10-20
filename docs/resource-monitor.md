# Observation-only resource monitor

Open **Readings** in the launcher, then **Start monitoring**. Alternatively:

```console
.venv\Scripts\python.exe -m tools.resource_monitor
```

The standalone command does not start the automation app. Opening the window
does not capture until Start is pressed. It uses the configured capture window
and size from `scripts/conf.json`. To test capture without a calibrated automation
profile, explicitly supply the capture window's current HWND:

```console
.venv\Scripts\python.exe -m tools.resource_monitor --hwnd 0x12345
```

Replace the example handle with the actual game or OBS projector window handle.
The target is pinned for each run; Stop and Start again after changing the
selected capture window. Use an unscaled, uncropped capture and keep the Skills
panel's resource rows visible. The monitor uses
`tibia_client.resource_reader.ResourceReader`, the same benchmark-selected reader
used by adaptive resource tracking and profile setup, with OCR fallback disabled.

## Reading the display

- **Accepted:** a nonnegative integer passed the reader's current-pixel checks.
  This is not an independently confirmed value or a calibrated confidence score.
- **N/A:** the row, panel or digits could not be read reliably. The detail explains
  the rejection; no previous value is substituted.
- **Stale:** local capture-begin age exceeds 500 ms. The value becomes N/A even
  if capture, discovery or decoding has stopped producing results.
- **Error / Stopped:** current values and images are cleared.

The preview shows the exact frame used by the readings, with colored boxes around
their verified value regions. Enlarged crops use nearest-neighbor scaling.
Stale crops remain visibly marked Stale for inspection, and their numeric labels
become N/A. The timestamp is local; identical frames do not prove a frozen stream,
and continuous OBS/game freshness is unverified.

Capture plus transfer, discovery and decoding are measured separately. The total
is **capture request to decoded result**, including process startup when it occurs,
but excluding initial reader construction and UI rendering. A result over the
100 ms budget is marked **OVER BUDGET** independently of readability. Initial
discovery can exceed that budget and may produce an already-stale first result.

Only the latest result is retained. A single worker performs capture and decoding
off the UI thread; frames never accumulate in a queue. Missing or unknown rows
trigger bounded rediscovery attempts. Stop clears displayed readings immediately,
cancels capture/discovery, and discards a decoder result that finishes later.
Restart waits for the previous worker to finish. A failed, black, minimized or
resized capture clears readings and requires an explicit restart.

The monitor owns only capture and reading, has no input adapter, does not start
automation, and does not write configuration or save screenshots. Existing
automation modules are independent; use STOP ALL before a strictly
observation-only trial. Start with capture and reading comparisons against the
visible client, then obscure values, move the panel, resize the capture and stop
during a read. These live checks remain separate from offline fixture tests.
