# Readings: resources, creatures and map

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
`tibia_client.retained_resources.RetainedResourceReader`, which discovers labels
with Windows OCR and retains the row geometry for ordinary numeric reads.
Adaptive resource tracking and profile setup retain the SDK default
`tibia_client.resource_reader.ResourceReader`. The
[expanded visual benchmark](visual-detection-benchmark.md) evaluates these two
production paths separately.

The table now includes:

- HP and mana values from Skills, soul points and capacity.
- HP and mana bar percentages, separately from Skills values.
- Every recognized battle-list name, its row and selected-target state.
- Enabled CaveBot targets and their counts, using the same OCR or bundled
  template detector as the bot.
- The calibrated minimap region and all 20 supported marker types: match
  counts, positions, scores and center-overlap evidence.
- Haste, hunger, protection-zone, empty ring and empty amulet indicators.

Visual regions come from the selected calibration profile. Missing or invalid
regions appear as unavailable; the monitor does not guess them. The minimap
region entry describes calibration geometry, not automatic map detection.
Use **Recalibrate** to reload saved visual calibration/target settings and
restart Skills discovery. It does not open the image-calibration wizard.

## Reading the display

**Stale time (ms)** defaults to **2000**. Enter a value from 100 to 60000 and
click **Apply** (or press Enter). It applies to Readings and the bot's resource,
combat and navigation observations. The value is saved in `scripts/readings.json`;
a separately running bot reloads it within about one second. Changing this limit
does not change capture frequency or make an unknown reading valid.

- **Accepted:** the detector returned a supported value from the current pixels.
  This is not an independently confirmed value or a calibrated confidence score.
- **N/A:** the row, panel or digits could not be read reliably. The detail explains
  the rejection; no previous value is substituted.
- **Stale:** local capture-begin age exceeds the configured stale time. The value becomes N/A even
  if capture, discovery or decoding has stopped producing results.
- **Error:** that detector failed; unrelated readings continue.
- **Stopped:** all current readings and images are cleared.

Select any table row to inspect its enlarged source crop and diagnostic evidence,
including source frame, coordinates, OCR fragments or template scores. Creature
crops include the adjacent icon so selection borders can be inspected. Marker
crops outline detected matches. Use the table scrollbar for additional entries.
The left preview follows capture independently. Its region outline is drawn only
when it is the same frame as the selected reading. The selected crop always comes
from that reading's exact source frame. Enlarged crops use nearest-neighbor scaling.
Stale crops remain visibly marked Stale for inspection, and their numeric labels
become N/A. The timestamp is local; identical frames do not prove a frozen stream,
and continuous OBS/game freshness is unverified.

Capture plus transfer, discovery and decoding are measured separately. The total
is **capture request to decoded result**, including process startup when it occurs,
but excluding initial reader construction and UI rendering. A result over the
100 ms budget is marked **OVER BUDGET** independently of readability. Initial
discovery can exceed that budget and may produce an already-stale first result.

Capture, resource decoding, visual decoding and Skills discovery run in separate
workers. Each decoder consumes only the latest frame; there is no frame queue.
Their readings have separate ages. Missing numeric values retain their layout;
only missing layout evidence triggers background recovery. Stop clears readings immediately,
cancels capture/discovery, and discards a decoder result that finishes later.
Restart waits for the previous worker to finish. Capture failures clear readings;
transport errors retry capture, while invalid frame size/black frames stop the run.

Creature OCR joins aligned fragments into complete names. All configured OCR
targets share the same prepared name pixels and cached OCR result. Missing text
rows get at most four cropped OCR retries, with a cooperative 120 ms scheduling
budget; one native OCR call can exceed that budget. Clipped boundary rows cannot
be recovered by retrying. A complete, exact OCR name can still supply a target
to CaveBot when another row is unreadable. The Targets group shows partial counts
with a `+` (for example, `3+` means at least three). CaveBot uses the recognized
integer directly for loot: `3+` to `2+` is a decrease from three to two. A completed
reading with no matching recognized names is treated as absence, even if other
text bands are uncertain. When the last configured target disappears for the
configured stable-sample count (two by default), CaveBot assumes combat ended,
loots if enabled, then resumes its route. This is a count heuristic, not corpse
confirmation. Pending OCR, OCR exceptions and stale/missing frames still wait;
they never become zero counts. Diagnostics expose `count_for_loot` and
`absence_assumed`. An **Unverified text band** is an uncertain region
of neutral pixels, not necessarily an unreadable creature. Borders can join
several distinct name rows into one band; that does not invalidate the recognized
names or trigger another broad OCR pass. Only competing OCR boxes on the same
baseline mark a recognized row as ambiguous. Diagnostics include original OCR
boxes and each recognized row's completeness/ambiguity flags.
Unavailable target readings leave CaveBot enabled and
waiting, without consuming its combat timeout. Priorities apply to the currently
readable targets. The last count baseline survives reading gaps so the next
completed reading can establish a decrease or disappearance.
OCR matches also take precedence for names with bundled templates. Templates
remain a fallback for selecting a target, with an incomplete count that also
drives loot. A template miss alone cannot finish a pending or failed OCR read.
Exact name matching is retained; partial names are not guessed. Unchanged failed pixels
are cached, while transient OCR exceptions may retry after one second.

The automation session and Readings have separate capture workers. A valid
health bar in Readings does not prove that Auto Heal's session is capturing.
Recoverable automation capture timeouts and worker disconnects now show
**Waiting for capture; retrying** while features stay enabled. They discard
old observations and queued input, restart the stopped worker on a later tick
(after a 250 ms retry delay), and require a new frame before deciding again.
This capture deadline is separate from the configurable stale time. Invalid
geometry, malformed capture data and a worker that cannot be stopped still
require explicit recovery. Auto Heal only acts below an enabled threshold.

Readings and the bot share the calibration's saved `ui_scale` for minimap markers,
bundled creature names, attack borders, follow controls, status and equipment
templates. Each detector uses that one scale; it does not search multiple scales
on every frame. Diagnostics show the applied scale. Regions and click coordinates
are already capture pixels and are not scaled a second time. Health/mana bars use
their calibrated rectangles; Skills numbers retain their calibrated font scale.

Marker results are cached only for identical minimap pixels, geometry and scale;
a changed map is scanned again. A 0.80 marker match can be clicked by CaveBot;
the existing center-overlap predicate uses 0.70. Center overlap alone does not
prove waypoint arrival or successful movement.

CaveBot's marker-search, arrival and combat timeouts renew the current operation
instead of disabling the feature. Arrival attempt batches repeat on the same
waypoint until stopped. A loot timeout retains the next unsent square and resumes
the sweep, rather than truncating it or restarting at square one. These retries
still require fresh readings and stop immediately when CaveBot is turned off.

After a count drop, CaveBot disables follow to hold the current tile, selects a
remaining configured target, then loots adjacent squares while the client keeps
attacking. Target selection is checked before every loot click. Another death
during the sweep selects the next target first and schedules a further sweep for
new corpses on already visited tiles. Counts are tracked per species and sampled
once per frame. When looting finishes, CaveBot restores follow if enabled,
independently of whether a target is selected, absent, or temporarily unreadable.
Follow and selection can run in the same tick without a follow cooldown. With
no targets, CaveBot resumes the route while continuing to retry follow if needed.
Follow changes use the calibrated UI scale and require a positively identified
red standing or green following control; missing control evidence waits.

Enabled healing and mana refill remain independent of this loot/combat state.
The session sends a needed health hotkey, mana hotkey and follow control before
its ordinary combat/loot action in the same tick, provided each reading is still
fresh. This applies follow-off before a simultaneous attack or loot click.
Input calls are serialized; resource actions do not wait for the loot sweep to
finish, and resources and follow do not consume the ordinary action slot.
Game cooldowns still apply to resource hotkeys.

The monitor owns only capture and reading, has no input adapter, does not start
automation or save screenshots. Applying stale time updates the shared reading
setting; it may also persist the retained Skills layout cache. Existing
automation modules are independent; use STOP ALL before a strictly
observation-only trial. Start with capture and reading comparisons against the
visible client, then obscure values, move the panel, resize the capture and stop
during a read. These live checks remain separate from offline fixture tests.
