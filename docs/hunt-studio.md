# Route Studio and hunt packs

Route Studio is the route editor inside **Cave Bot**. It edits a route in memory and keeps
editing controls disabled while CaveBot is running or navigation is paused. The editor
does not capture the client or send input.

## Edit a route

1. Enter a route name and choose **Load**. A name with no existing route opens an empty
   route.
2. Choose a bundled marker template. Use **Add after** to insert it after the selected row.
3. Select a row in the route list. **Edit marker**, **Duplicate**, **Up**, **Down**, and
   **Remove** operate on that row. A duplicated waypoint always receives a new ID.
4. Check the marker image in the bundled asset preview. Route Studio accepts only the marker
   templates shipped in `images/MapSettings` when it saves or exports a route.
5. Choose **Save** to write the named route and its selected-waypoint checkpoint. Edits remain
   in memory until this explicit action. Start is blocked while the displayed route
   differs from its saved file.

**Duplicate route** asks for a new route name, copies the ordered marker list, and assigns new
IDs to every waypoint. It does not write the new route until **Save** is pressed.

Only the `Walk` action is supported. Rope and Shovel remain unavailable because the current
client integration cannot confirm their outcome reliably.

## Export a hunt pack

Choose **Export pack** to package the route currently shown in the editor, the four target
rows, CaveBot walking/follow/loot/stand settings, and saved healing, mana, haste, food, and
timed-spell settings. Export validates every marker and target against a bundled template and
derives the minimum required client capabilities. Replacing an existing export requires a
separate confirmation.

The export does not require the route to have been saved first. This is useful for reviewing a
draft pack, but the CaveBot start action still loads its route from the normal route file, so
press **Save** before activation.

## Import and bind a hunt pack

1. Choose **Import pack** and select a JSON hunt pack. Import performs bounded, strict schema
   validation and stages its route, target rows, and CaveBot settings in memory. It does not
   overwrite a route file or local support configuration.
2. Run **Test client** from the main window and complete the compatibility checks needed by the
   pack on the selected official-client/OBS profile.
3. Return to Cave Bot and choose **Bind local**. Binding verifies the current profile and
   calibration identity, alignment readiness, and every required operation and sensor. A
   missing or unverified capability is reported and no settings are applied.
4. If all checks pass, Bind local applies the CaveBot and support settings through one atomic
   configuration update. It refuses to do this while a support feature is active.
5. Open support configuration windows refresh immediately with the imported settings; reopening
   them is unnecessary. Start each support feature explicitly when ready. Import and binding
   never start automation.
6. Choose **Save** to write the imported route, then start CaveBot. Activation rechecks that the
   same local profile and calibration are still selected. A profile change, resize that
   invalidates calibration, or newly missing capability requires binding again.

You may save an imported route before binding when you only want a standalone route copy. This
does not change the active saved route selection; bind the pack and save again to select it. If a
different local route already uses the imported name, Route Studio asks before replacing that route
and its checkpoint. Rename the route or use **Duplicate route** to keep both.

The CaveBot OFF button stops the feature. Its configured pause hotkey pauses or resumes only
navigation through the shared session; a paused CaveBot keeps Route Studio locked.

## Portable boundary and validation

A hunt pack is data, not a desktop profile or an executable extension. Version 1 includes:

- a versioned route containing unique waypoint IDs, bundled marker names, and `Walk` actions;
- up to four unique bundled target templates with priorities;
- CaveBot and support settings;
- an explicitly inactive future supply policy; and
- the complete capability declaration implied by those settings.

It excludes window handles, profile and calibration data, client-local coordinates, machine
paths, route checkpoints, compatibility evidence, trace frames, and executable code. Those
values are neither imported nor exported. The parser rejects unknown fields, duplicate JSON
keys, non-finite numbers, wrong types, missing bundled assets, undeclared setting requirements,
and documents larger than 512 KiB. A pack may declare extra future capabilities; the selected
local profile must provide all declared capabilities before binding.

Supply, refill, depot, seller, banker, and loot-sorting policies are not implemented. The v1
schema records `supply_policy.state` only as `inactive` so a pack cannot imply those workflows
are available.

## Compact version 1 example

This example is emitted by `HuntPack.to_json()` from the validated test fixture. It has one
waypoint and one target to keep the example small.

```json
{
  "cavebot": {
    "follow": false,
    "loot": false,
    "stand_seconds": 2,
    "walking": true
  },
  "name": "rat-loop",
  "required_capabilities": [
    "battle_list_click",
    "left_click",
    "modified_press",
    "press",
    "sensor.hasted",
    "sensor.hungry",
    "sensor.marker",
    "sensor.target"
  ],
  "revision": 1,
  "route": {
    "name": "rat-loop",
    "version": 1,
    "waypoints": [
      {
        "action": "Walk",
        "id": "entrance",
        "mark": "Flag"
      }
    ]
  },
  "schema": "tibiaauto.hunt-pack",
  "supply_policy": {
    "state": "inactive"
  },
  "support": {
    "food_hotkey": "Shift + F12",
    "haste_hotkey": "F6",
    "healing": [
      {"enabled": false, "hotkey": "F1", "percent": 85},
      {"enabled": false, "hotkey": "F2", "percent": 60},
      {"enabled": false, "hotkey": "F3", "percent": 40}
    ],
    "mana": [
      {"enabled": false, "hotkey": "F3", "percent": 70},
      {"enabled": false, "hotkey": "F4", "percent": 45}
    ],
    "timed_spells": [],
    "timed_spells_immediate": false
  },
  "targets": [
    {"name": "Rat", "priority": 1}
  ],
  "version": 1
}
```
