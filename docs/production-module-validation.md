# Progressive validation in the real bot

Target: Paladjinn in Paulistinha OT, displayed by Windows Sandbox. Tests are
started from `start.py` and actual module controls. Native API completion alone
is not a pass; compare the visible game response before moving to the next stage.

## Results (2026-10-21)

| Function | Observed result | Status / next step |
| --- | --- | --- |
| Sandbox setup capture | Automatic activation revealed the game; saved viewport `(2,71,1518,794)` in host client `1518x855` | Passed for this layout |
| Image calibration | Old icon templates and fixed 100-pixel bars did not match OT; image-region fallback selected all eight actual regions and saved NewConfig.json | Full bot loads and reloads this profile; actual-width green health and right-aligned mana supported |
| Keyboard module startup | Actual Timed Spells opened without a fabricated calibration profile | Passed |
| Scheduled Ctrl+O | Initially stopped with `unsupported key: 'O'`; fixed case-insensitive native lookup; actual module then opened Open Channel | Passed after repair; OFF stopped module |
| HP/mana/soul/capacity readings | Resource monitor matched 375, 321, 100, 412 | Values passed; capture+decode typically 250–312 ms exceeds 100 ms budget, occasional stale samples |
| Timed gameplay spell | F3 verified as Magic Patch (`exura infir`); actual module at 10 s interval reduced mana 375 → 369 → 363, later 351; HP stayed 375 | Gameplay effect and repetition observed; OFF stopped module |
| Global Stop All shortcut | F12 polling and event latch both failed with Sandbox focused. Native registered Ctrl+Shift+F11 works with bot focused but did not stop from Sandbox | Sandbox global stop remains unverified/failed; use visible OFF / STOP ALL |
| UI lifetime after module close/reopen | Bot froze; py-spy showed worker in Tk Variable destructor under session lock while UI waited in feature_status | UI-thread cyclic collection implemented; repeated module closes/reopens and bot restarts succeeded live |
| Food | User bound fish to Shift+F12. Actual Food Eater consumed the last fish; food timer changed 00:00 → 01:02 and module reported Hungry: False | Passed action, status transition and OFF; fish stock exhausted |
| Auto Mana | Actual module read 82% for 309/375, sent nothing at 70% threshold; at 90% with F2, client reached 375/375 and potion count 19→17; module then read 100% and ceased requests | Passed action, no-action and OFF on live client |
| Auto Heal | First attempt misread partially occluded bar as 43%; stopped immediately. Added valid-row checks and delayed native window placement until after mapping. Retest: control window on second screen, HP100% matching375/375, no action | Full-health no-action passed; damaged-health recovery pending |
| Status icon scale | Hungry icon visibly present but 1× template scored .684; at 1.25× score .935 | Saved ui_scale=1.25 without lowering .90 threshold; Food and Auto Hur now passed live |
| Auto Hur | Actual module sent F6: mana 375 → 315, speed 136 → 164, Hasted: True. Visible STOP ALL changed module to OFF | Passed cast, status recognition and visible STOP ALL |
| Equipment | Replacement not yet tested; saved F11/F12 are currently rope/shovel in client | Configure actual equipment bindings before enabling replacement |
| Navigation / target selection | Not yet tested | Require valid map, player and battle-list geometry first |

Idle disconnection was observed before testing and Paladjinn was reconnected.
No test result was inferred from the disconnected screen. Failed stages are
stopped and repaired before dependent automation is enabled.

## Custom creature names

Target fields are editable comboboxes. The bundled names are suggestions;
typed names survive close/reopen, preference restoration and hunt-pack import
and export. Enter the complete singular name shown in the battle list, such as
`Lizard Henchman`. Matching ignores case and spacing, but does not use fuzzy or
partial-name matches.

Names without a bundled template use local Windows OCR on the calibrated battle
list. One background worker handles recognition without blocking the capture or
healing loop. Results are keyed to the current name pixels: changing/reordered
rows require recognition again. Pending or failed recognition returns an unknown
observation, so the feature stays enabled and waits without sending a click.
Selection is checked against the current row's red/pink icon border.

The Windows OCR bindings are installed in this workspace's virtual environment
and pinned in requirements.txt. This addition has been source-reviewed only;
the UI and live creature matching have not been exercised because desktop input
remains stopped. No automated tests were run. Restart the bot to load the change.

After these checks, all features were stopped. Restored the original Timed Spells
settings and the 70% mana threshold. Mana hotkeys now use F2; healing stages that
incorrectly used the mana potion now use the verified F3 healing spell. Food
remains Shift+F12 and haste F6. Damaged-health recovery, equipment replacement,
navigation and a native host client remain unverified. No automated suite was run.
