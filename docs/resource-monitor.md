# Readings: resources, creatures and map

Open **Readings** in the launcher, then **Start monitoring**. Alternatively:

```console
.venv\Scripts\python.exe -m tools.resource_monitor
```

The launcher's Readings window subscribes to the same `ClientSession` as Auto
Heal, Auto Mana and CaveBot. Starting monitoring uses their capture worker and
reuses matching detector results; it does not enable automation, activate input or create a
second capture process. Opening the window alone is passive. Closing it or
pressing Stop removes only its subscription and leaves automation running.
STOP ALL also stops this subscription and clears the displayed values; press
Start monitoring to resume it.

The session and adapter objects are created when the main menu opens. Their
scheduler and capture process start lazily when monitoring or a feature starts.
When there is no observation or automation demand, the session does not capture.
Additional monitor-only fields still require detector work on the shared frame,
so monitoring can increase CPU use and the time before the next automation tick.
Values, capture timestamps and source crops come from the same session batch.

The standalone command keeps its own capture and reader workers and does not
start the automation app. Opening its window also waits for Start. Both modes
use the configured capture window and size from `scripts/conf.json`. To test
standalone capture without a calibrated automation
profile, explicitly supply the capture window's current HWND:

```console
.venv\Scripts\python.exe -m tools.resource_monitor --hwnd 0x12345
```

Replace the example handle with the actual game or OBS projector window handle.
The standalone target is pinned for each run; Stop and Start it again after
changing the selected capture window. The launcher follows the selected client
session. Use an unscaled, uncropped capture and keep the Skills
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
The launcher preview and rows advance together with the subscribed observation.
The standalone preview follows capture independently. Its region outline is drawn only
when it is the same frame as the selected reading. The selected crop always comes
from that reading's exact source frame. Enlarged crops use nearest-neighbor scaling.
Stale crops remain visibly marked Stale for inspection, and their numeric labels
become N/A. The timestamp is local; identical frames do not prove a frozen stream,
and continuous OBS/game freshness is unverified.

Capture plus transfer, discovery and decoding are measured separately. The total
is **capture request to decoded result**, including capture-process startup and
foreground detector setup when they occur, but excluding UI rendering. Numeric
reader construction and panel discovery run in the background; their old pixels
never become a new reading. A proposed layout must pass checks against the current
frame. A result over the 100 ms budget is marked **OVER BUDGET** independently of
readability. In shared mode, decode time includes all requested sensors; visual
decode time reports the additional inspection work within that total.

In the launcher, the session schedules the requested readings on its shared
capture; Skills discovery and asynchronous OCR keep their own bounded background
work. The standalone monitor keeps separate capture, resource and visual decoder
workers, each consuming only the latest frame, without a frame queue. Its resource
and visual readings can have separate ages. Missing numeric values retain their
layout; only missing layout evidence triggers background recovery. Both views
clear readings immediately on Stop and discard later results for that stopped
subscription or run. Standalone Stop also cancels its capture/discovery workers;
its restart waits for them to finish. The launcher leaves the session available
to active automation. Capture failures clear readings;
transport errors retry capture, while invalid frame size/black frames stop the run.

Creature target names accept `*` (zero or more characters) and `?` (one character).
For example, enter `Lizard *` in a CaveBot target field to match Lizard Henchman,
Lizard Magician and other names starting with Lizard. Matching ignores case and
spacing, as exact names already do; other punctuation, including brackets, stays
literal. A name without wildcards still matches the whole normalized name.
Counts combine all matching complete rows, and a matching creature already under
attack remains selected. Priorities still apply to target entries, so a specific
name can take priority over a broader pattern by assigning it a lower number.
The same pattern and aggregate count are used by CaveBot and Readings, including
loot decisions. Pattern matching reuses the cached OCR rows without extra captures,
OCR calls or per-creature template scans. The Targets detail lists matching names;
diagnostics expose `target_pattern`, `match_mode` and `matched_names`.

Creature OCR joins aligned fragments into complete names. All configured OCR
targets share the same prepared name pixels and cached OCR result. Missing text
rows get at most four cropped OCR retries, with a cooperative 120 ms scheduling
budget; one native OCR call can exceed that budget. Clipped boundary rows cannot
be recovered by retrying. A complete, matching OCR name can still supply a target
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

The main-menu Readings window shares the automation session's capture and health
bar detector. The standalone command has a separate capture worker, so a valid
bar there does not prove that Auto Heal's session is capturing.
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
With Follow enabled, every CaveBot policy cycle checks the follow control using
the shared captured frame (default poll interval: 200 ms, subject to capture
latency). Outside the loot phase it re-enables inactive follow, including during
route waits, pending attacks and unreadable creature rows. Only an actual loot
sweep holds follow off; a tentative count drop does not latch it off. No extra
capture or cooldown is added for this check.
Follow changes use the calibrated UI scale and require a positively identified
red standing or green following control; missing control evidence waits.

Enabled healing and mana refill remain independent of this loot/combat state.
The session sends a needed health hotkey, mana hotkey and follow control before
its ordinary combat/loot action in the same tick, provided each reading is still
fresh. This applies follow-off before a simultaneous attack or loot click.
Input calls are serialized; resource actions do not wait for the loot sweep to
finish, and resources and follow do not consume the ordinary action slot.
Game cooldowns still apply to resource hotkeys.

Monitoring never starts an automation feature or sends input. The launcher view
owns only a read-only subscription; the standalone monitor owns capture and
reading without an input adapter. Neither saves screenshots. Applying stale time
updates the shared setting; reading may also persist the retained Skills layout
cache. Existing automation can continue while either monitor runs; use STOP ALL
before a strictly observation-only trial, then start monitoring. Start with
capture and reading comparisons against the
visible client, then obscure values, move the panel, resize the capture and stop
during a read. These live checks remain separate from offline fixture tests.
