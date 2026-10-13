# Feature roadmap: from CaveBot to a complete hunting assistant

Research date: **2026-10-12**. Roadmap implementation update: **2026-10-13 (UTC)**. Research baseline: **`01699e7`**; runtime-code baseline: **`f4c8212`**, on `reliability/client-foundation`.

**Status: M0 software, M1 authoring/inspection tools, and a configurable M1–M3 hunt workflow are implemented. Environment-specific live validation remains open.** The new workflow covers progress tracking, finite travel segments, guarded visual supply/container readings, verified transfers, and a resumable return/deposit/sell/refill cycle. Broader recognition and combat capabilities remain planned. This roadmap compares public primary documentation and selected source files. It does not establish a performance ranking or verify any competitor against a live client.

## Product direction

Make one hunting trip work exceptionally well: configure a hunt, reach it, fight and loot, recognize when to leave, return, deposit or sell, refill, and resume. Give the operator a clear explanation and control at every stage.

The strongest opportunity is the combination of a complete hunt lifecycle and an excellent assisted mode. Route editing, diagnostics, manual handoffs, and useful stop reasons can ship before every interaction is autonomous. Each completed slice should remove a real interruption from a hunting session.

Keep the shared SDK. The recent architecture already supplies coherent observations, request ownership, priorities, cancellation, and replay. The next investment is better game-state evidence and useful workflows built on those contracts.

### Target and integration boundary

The primary target is **unofficial OT servers using the official Tibia client with BattlEye**, as specified by the project owner. Preserve the original TibiaAuto12 integration: OBS Game Capture presents a projector image; TibiaAuto captures that window with `PrintWindow`, interprets its pixels, and controls the foreground game window through desktop keyboard/mouse input. The original README documents this path. Its support claim is historical evidence, not a current-client compatibility result. [T1]

- **Primary capture:** an OBS projector selected separately from the game input window. Direct client capture is an optional, separately validated profile.
- **Primary input:** foreground physical input using the existing `SetCursorPos`, `keybd_event` and `mouse_event` API family. Window-message input is a separately tested option; an OT server does not imply that the official client accepts those messages. The original implementation itself warned about that limitation. [T2]
- **Game state:** visual observations with explicit known/unknown/error results. No game-process memory reader/writer, custom injected automation, binary patching or packet interception is part of this roadmap.
- **OBS boundary:** OBS Game Capture uses an OBS-owned capture hook. Do not describe the end-to-end pipeline as hook-free. Keep OBS as a normal capture dependency; its documented anti-cheat compatibility can vary by version and game. [B1–B2]
- **Extensions:** the SDK can support other adapters later, but OTClient APIs and memory-derived competitor features are not prerequisites or shortcuts for this target. Port their user workflows only where visual evidence supports them.

Server policy and client compatibility are separate questions. This roadmap does not establish that an OT deployment permits automation or that a particular client/BattlEye configuration accepts it. Milestone 0 records scoped technical evidence; it cannot certify immunity from detection or future compatibility.

## What the benchmarks contribute

These are complementary references, not interchangeable products. Features documented for an embedded OTClient bot can rely on information unavailable to a desktop pixel adapter.

| Reference | Best ideas to study | How to use the evidence |
|---|---|---|
| **ZeroBot** | Recorded routes with a debug display, typed waypoints, generated refill/sell steps, configurable luring and restricted areas | Current official documentation is a strong feature-design reference. Client/server requirements still apply. [Z1–Z4] |
| **OldBot** | Marker/image navigation, waypoint recording, and an action catalog connecting supplies, NPC trading, deposits and route branches | Uses both pixels and memory reading; its recorder requires memory-derived coordinates. The author no longer actively maintains it, and its official site excludes Real/Global Tibia. Borrow workflows, not compatibility assumptions. [O1–O4] |
| **vBot / OTClientV8** | Supply-driven hunt branches, purchasing to target quantities, depot automation, target selection using distance and health | Selected source files demonstrate these workflows and direct client APIs. They illustrate what becomes possible with structured inventory, creature, and container state. [V1–V4] |
| **nExBot** | Integrated route, combat, container, and hunt-analysis workflows; coordinated movement and target commitment | An additional open-source design reference. Its documentation describes capabilities; it does not prove market leadership or measured superiority. [N1–N4] |
| **WindBot** | Reusable supply presets, recovery thresholds for healing, combat positioning modes, script options and HUDs | Historical product-design reference only: the public changelog's latest listed release is September 12, 2017. [W1–W2] |

The recurring lesson is that value comes from connecting modules. A supply threshold should select the return route; arrival should enable a confirmed shop interaction; replenishment should enable the trip back. Merely adding a refill button does not complete that experience.

## Baseline before this implementation

| Already implemented | Remaining gap |
|---|---|
| Separate capture/input windows, `PrintWindow` capture, physical and message input backends | Recorded compatibility evidence for the intended OBS/client profile and verified visual/input alignment |
| Shared session, immutable observations, fresh-evidence requirements, prioritized requests, bounded capture/message operations and serialized physical input | Broader observations: inventory, containers, shops, floor changes, and individual creatures |
| Healing, mana, haste, food, ring, amulet, timed spells | Shared cooldown groups, condition-aware rules, richer presets and rule explanations |
| Marker navigation and observed marker arrival, Walk waypoints with configurable stand-after-arrival, target species priorities, follow | Movement-progress history, explicit recovery branches, tools, travel and richer combat positioning |
| Bounded looting triggered by a target-count drop | Corpse/container recognition and confirmed loot transfer; a disappeared target is not a confirmed kill |
| Validated route persistence and fingerprinted resume checkpoints | Convenient route authoring, portable hunt packs and full lifecycle checkpoints |
| JSONL recording and native-free replay | A user-facing timeline, diagnostic overlays and trustworthy hunt statistics |

`ActionResult.EXECUTED` means the native call ran. It does not prove the game accepted a spell, bought an item, or changed floors. Existing marker arrival is already an observed outcome; additional workflows need similarly explicit evidence.

Current observation timestamps measure the local capture operation. A frozen OBS projector can still produce newly captured screenshots, so this freshness contract does not establish upstream game-frame freshness. M0 must expose and handle that distinction.

## Implementation status — 2026-10-13

The M0 software workflow, M1's parallel authoring/inspection tools and a locally
configured complete hunt workflow are implemented. This is an implementation status, not a real-client compatibility
claim: the live evidence gate below remains open for each intended environment.

| Available now | Remaining validation or scope |
|---|---|
| Operator-started compatibility studio, captured preview, separate dispatch/confirmation records, bounded evidence export | Collect actual client/OBS operation, alignment and visible-change evidence |
| Context-bound profiles, capability requirements and current-session window-binding gate | Verify each chosen client build, renderer, input mode and OBS configuration |
| Shared capture health, runtime console, navigation pause/resume and Stop All | Exercise native focus, resize, capture failure and interruption scenarios |
| Route list editing, marker preview, stable waypoint IDs, portable hunt packs and local binding | Automatic route recording and broader waypoint types remain planned |
| Bounded navigation progress history, finite travel and recognized-anchor recovery API | Validate marker/progress behavior on the intended layout; anchor recovery requires fresh unambiguous evidence |
| Context-guarded template and digit observations, supply thresholds and return intents | Capture and validate real item/container/digit templates; unknown state is not a zero count |
| Ordered travel segments, guarded interactions, deposit/sell/refill loops and restart checkpoints | Demonstrate one configured full cycle; no universal NPC, shop or depot recognizer is bundled |
| Structured workflow editor, saved-frame template cropping, capture-only detector inspection and manual reconciliation | Validate detector accuracy and interaction postconditions on the intended client |
| Read-only filtered trace timeline, statistics and CLI inspection | Real detector fixtures and environment-specific replay cases depend on collected evidence |

Start with the [desktop compatibility guide](desktop-compatibility.md), then use
[Hunt Studio](hunt-studio.md), the [hunt workflow guide](hunt-workflow.md), and the
[trace tools](recording-replay.md). Live rollout remains gated on M0 acceptance
and workflow-specific evidence. Digit-template recognition is deliberately
narrow; universal OCR, automatic interaction discovery and advanced spatial
combat are not implemented. Portable hunt-pack v1 remains unchanged; workflow
documents additionally contain local visual bindings and template paths.

## Delivery sequence

### M0 — Desktop compatibility and observability

**User outcome:** establish a reproducible OBS/foreground-input configuration, see what has actually been verified, and stop automation predictably when its required evidence or readiness fails.

Implement a narrow compatibility tool first, collect evidence, then build the profile and runtime checks around what the intended client demonstrates. Reuse the current capture, input, cancellation and trace machinery; do not create a second scheduler or redesign the SDK.

| Order | Deliverable | Required behavior |
|---:|---|---|
| 1 | Operator-started compatibility tool | Exercise one selected operation at a time; show intended target/coordinates; record OS dispatch separately from the observed game response |
| 2 | Validated desktop profile and small capability declaration | Bind capture/input selection, calibration, geometry, input mode and tested operations; reject unsupported feature requirements before activation |
| 3 | Capture-health handling | Separate transport success, capture age, visual validity and upstream freshness evidence; invalidate affected observations when a failure is established |
| 4 | Runtime diagnostics and controls | Explain readiness/blocking reasons; provide distinct Pause navigation and Stop all automation through the shared application/session boundary |

**Compatibility operations and evidence**

- Verify the original OBS projector path with the intended game window in the foreground. Check direct client capture or message input only as separate configurations, never as an automatic fallback.
- Test plain and modified hotkeys, left/right clicks, dragging, cursor restoration and battle-list target selection individually. Require only the operations a profile enables; record other operations as unverified rather than blocking unrelated features.
- Use an operator-triggered visible change to demonstrate that the captured view updates and belongs to the intended game window. Verify mapped points across the view; equal window dimensions alone do not prove alignment.
- Exercise focus loss, window movement, resizing, capture-window closure, capture failure and Stop. Include an interrupted or frozen relay scenario and record what can and cannot be detected from the available evidence.
- Export environment/profile metadata, a bounded action trace and opt-in selected frame files. Trace v1 remains observation-only; do not silently embed images or change its schema. Record automatic confirmation and operator confirmation distinctly, with pass/fail/unverified outcomes.
- Use these frames as detector fixtures and build replay cases from the recorded observations. Saved-image tests verify perception against those images; they do not prove current live compatibility.

**Profile and activation contract**

Record client build, OT server, Windows version, renderer where known, OBS version and source/projector configuration, DPI/scaling, layout, capture/input dimensions, input mode and test date. Record the reported or observed BattlEye state with its provenance; an installed component is not proof that protection is active. Keep credentials out of the bundle.

Window handles are runtime bindings, not durable client identities. Rebind and validate both windows at startup. Calibration must refer to the captured content and its mapping into game-client coordinates, with no unnoticed scaling, cropping or letterboxing. The first supported profile requires a 1:1 mapping; arbitrary rescaling is not an M0 requirement. Invalidate calibration on relevant geometry changes and mark compatibility evidence for revalidation after relevant client, OBS, renderer or input-mode changes.

Declare the small set of sensors and actions the active adapter/profile supports. Track implementation support separately from observed compatibility results; an OS call returning successfully cannot promote an operation to verified. Gate activation on the selected profile's required calibration, readiness and operation checks, with an actionable explanation for missing evidence.

**Capture health and bounded failure**

Expose local screenshot age separately from any evidence about the game's source image. Detect observable failures such as failed capture, invalid layout, changed geometry or a failed expected visual transition. Cancel dependent queued actions when their evidence is invalidated; retain independently valid feature behavior only where its own requirements remain satisfied. Use bounded outcome waits and retries rather than repeatedly issuing an action from unchanged visual state.

Identical frames alone cannot distinguish a frozen relay from a static game scene. An initial visible-change check does not guarantee continuous liveness. If upstream freshness cannot be established, expose it as unverified; never fabricate a source timestamp from the local capture time. M0's gate requires honest diagnostics and handling of detectable failures, not a claim of perfect freeze detection.

The minimal status view shows active capture/input mode, profile/alignment status, capture health, target readiness, last action/result and blocking reason. Add a shared Stop all automation control through the application/session lifecycle, reusing existing feature cancellation and closure primitives to prevent new dispatch, cancel queued work and disable feature generations. An OS call already executing may finish. Restart is explicit and rechecks the profile/readiness/evidence. Pause navigation has narrower scope and clearly indicates which support features remain active.

**Compatibility differences to check, not blindly revert**

The rewrite changed battle-list click placement, duplicate-match suppression, message-mode modifier delivery/timing, capture failure handling and coordinate transforms. Check the intended client response before altering them. Preserve client-local coordinates, validated window ownership, serialized input, bounded waits and the absence of automatic focus stealing. Historical magic offsets and unbounded retries are not the integration contract.

**Exit gate:** one declared OBS/client configuration demonstrates aligned capture and accepted input for its enabled operations. Focus loss, capture failure and Stop prevent further affected dispatch once observed; resize invalidates calibration; recovery requires fresh evidence. The evidence bundle distinguishes local capture freshness from source freshness, includes reproducible failure cases, and lists unsupported/unverified operations. This is a scoped compatibility result for the recorded environment, not a BattlEye guarantee.

**Offline versus live:** the tool, profile contracts, diagnostic UI, fixtures and cancellation/error scenarios can be implemented offline. Actual capture quality, alignment, detector accuracy, input acceptance and timing require controlled checks on the intended installation.

**Parallel work:** M1's hunt-pack schema, route editor, trace browser and replay scenarios can proceed while M0 is being built or awaiting live evidence. M0 gates live feature rollout for each profile; it does not block all roadmap coding. Inventory perception, depot workflows, advanced combat and alternative adapters are outside this prerequisite milestone.

### M1 — Route Studio and Hunt Console

**User outcome:** configure, inspect, run and diagnose an existing hunt without editing files or guessing why it stopped.

- Duplicate, reorder, validate and import/export routes; preview waypoint markers and their calibrated regions.
- Integrate M0's guided setup checks, capture previews and compatibility evidence into the route workflow; add route-specific marker and region overlays.
- Use M0's adapter/profile capability declaration to validate hunt-pack requirements. File validation alone cannot prove runtime support.
- Package the route, target list, support settings, supply policy and required capabilities as a versioned **hunt pack**. Keep machine-specific window identities and calibration bindings outside portable packs; require explicit local binding on import.
- Extend M0's read-only diagnostics with the current phase, waypoint, last recognized evidence, pending action, cooldown and timeout/retry budget. Use feature projections where needed; existing `FeatureStatus` and events do not expose all these fields.
- Integrate M0's **Pause navigation** and **Stop all automation** controls, preserving their scope and explicit restart behavior.
- Add pause-after-step and resume-from-recognized-anchor. Ambiguous anchors require operator selection; proximity in a list is not evidence of location.
- Add a user-facing timeline over M0's diagnostic export. Frame files remain separate opt-in artifacts. Recorder callbacks remain read-only; UI commands go through the application/session boundary.
- Add navigation progress detection and bounded recovery. Distinguish waiting for combat, movement still progressing, missing marker and no progress. Retry or take a configured fallback only when supported by fresh evidence.
- Begin assisted route recording from recognized landmarks and operator annotations. Automatic recording of doors, tools and transitions waits for the corresponding detectors.

**Exit gate:** on a profile that passed M0 for the required operations, one existing hunt can be set up and diagnosed through the UI; invalid packs are rejected before activation; interruptions, missing markers and recovery limits have deterministic replay cases. Controlled live checks establish whether progress detection works on that profile.

**Offline work available in parallel with M0:** route/preset UI, schema validation, status presentation, trace browser, replay scenarios and progress policies over fixtures. Live capture is needed to validate new detectors and release the profile, not to design these workflows.

### M2 — Supply-aware hunts and verified looting

**User outcome:** the bot knows when supplies are running low and can finish a hunt deliberately.

- Read the minimum useful inventory state from captured pixels, using templates or OCR as appropriate: selected consumable counts, available capacity where readable, container identity and open/closed state. Add fields only for the first supported workflow; unsupported observations remain unavailable rather than triggering memory/API access.
- Configure a refill target, return threshold and emergency reserve per supply. Separate optional supplies from required ones; use hysteresis to prevent repeated return/continue decisions.
- Keep **observed**, **estimated** and **unknown** quantities distinct. Confirmed consumption can support forecasts; missing observations never become zero. An estimate can advise an early return but cannot authorize a purchase or transfer.
- Provide **Return after this fight**, **Return now**, and **Stop after return** intents. These select a verified exit route; they do not promise that any location is safe. If the route cannot be established, stop affected movement and report the problem while preserving independently valid support behavior.
- Replace the blind surrounding-tile loot sweep with bounded corpse/open-container evidence and confirmed transfer where available. When only container opening is observable, report that limit and do not claim an item was collected.
- Add data-only route labels, conditions and reusable segments. Every loop and interaction has a time/attempt budget and an explicit failure destination.

**Supply-return exit gate:** on one declared client/layout and hunt, controlled depletion produces one explained return decision; supply uncertainty cannot trigger speculative inventory actions. This milestone can ship while verified looting is still in development.

**Parallel loot-track exit gate:** verified loot attempts finish or report why they could not. Existing heuristic loot remains explicitly selectable during migration, never presented as verified. Corpse detection is not a prerequisite for supply-aware return; confirmed container transfers remain necessary for later automated deposits.

### M3 — Complete refill, depot and return cycle

**User outcome:** one supported hunt runs from preparation through replenishment and back to hunting.

- Compose reusable steps for reach vendor/depot, recognize dialog or container, select item, buy/sell/deposit, verify the resulting state, and continue.
- Share supply and loot lists across the monitor, shop and depot workflows. Protect equipped items and required reserves; distinguish sale inventory from retained supplies.
- Add rope, shovel, doors, stairs and travel only where the route needs them. Confirm a map/floor/dialog transition before advancing.
- Persist a lifecycle checkpoint containing the hunt-pack revision, phase and intended operation. After interruption, observe the actual state again before deciding what remains to do.
- Reconcile quantities before retrying purchases or transfers. A local request ID cannot make a game action idempotent, and there is no general rollback for a completed purchase.
- Release as an explicit support matrix: one client profile, one character configuration, one route, one vendor/depot flow first. Expand with evidence-backed packs.

**Exit gate:** the demonstrated cycle is **prepare → hunt → return → deposit/sell → refill → resume**. Every inventory-changing step has a checked postcondition or pauses for review. Inject interruptions around each step and verify that resumption does not blindly repeat the previous action.

This is the flagship milestone. It should take priority over a large collection of isolated convenience modules.

### M4 — Smarter combat and looting

**User outcome:** improve combat efficiency while keeping decisions understandable.

| Capability | First useful version | Required evidence |
|---|---|---|
| Healing and condition rules | Vocation presets, separate trigger/recovery thresholds, priority and overlap warnings | Reliable HP/MP and relevant status indicators |
| Shared cooldowns | Cooldown groups, minimum action gaps and backoff after unconfirmed actions | Configured rules first; observed cast/cooldown feedback where available |
| Target commitment | Minimum commitment, retarget delay, per-target timeout and explained priority | Reliable selected-target state; individual identity before instance-specific locks |
| Offensive rotation | Ordered spells/runes by resources and observed target conditions | Fresh resources/target evidence and relevant cooldowns |
| Positioning | Hold, approach or keep distance; bounded lure start/stop rules | Individual creature positions, local traversability and progress |
| Area attacks | Evaluate an attack shape against visible hostiles; veto uncertain or friendly occupants | Entity classification, position and orientation where relevant |
| Smart loot | Allow/deny lists, destination backpack, rare-item alerts; optional value/weight preferences | Identified items, quantity, destination capacity and confirmed moves |

Ship healing-rule UX and configured cooldown groups earlier when they help M1–M3. Keep kiting, luring and area attacks gated behind perception: a species count and battle-list click position are insufficient to plan spatial combat.

**Exit gate:** replay fixtures show stable target decisions, support priority, bounded unreachable-target handling and no area cast from ambiguous occupancy. Controlled live scenarios confirm positioning and action outcomes for the supported profile.

### M5 — Hunt intelligence and supervision

**User outcome:** understand results, choose when to stop and intervene quickly.

- Report hunting time, travel/refill downtime, completed trips, interventions and reasons for stops from existing trace events.
- Add consumption, loot value, XP and profit only when their sources are available. Show measured versus estimated totals; dispatched actions are not confirmed uses, and target-count drops are not confirmed kills.
- Compare two hunt-pack revisions against the same replay scenarios. Later compare live sessions with their client/profile and observation-coverage context.
- Add duration, supply-reserve and observable progression goals, with a clear return/stop policy. Bestiary/task completion requires a progress source, not a guessed kill counter.
- Offer local alarms first: stalled route, low supplies, unreadable critical state and readiness loss. Include the last relevant evidence and the available action.
- Add opt-in remote notifications and authenticated current-session Pause/Stop commands later. Stop disables automation; it is not a guaranteed retreat. Remote resume needs additional location/readiness validation.

**Exit gate:** metrics reproduce from the trace and disclose missing data; alarms are rate-limited; stale remote commands cannot operate a new session.

### M6 — Extensions, party support and broader clients

**User outcome:** reuse the same policies across richer scenarios without fragmenting the core.

- Add trusted Python feature packs with versioned configuration and capability requirements after the built-in extension pattern has stabilized. Data-only hunt packs come first. Ordinary Python plugins are not a security sandbox; a manifest alone cannot prevent native access.
- Party healing/follow requires unambiguous member identity, fresh health/position evidence and self-preservation priority.
- Reconnect/login recovery requires recognized client states, bounded attempts and a deliberate credential design. A network error must not cause blind clicks through unknown screens.
- Multiple clients require independent configuration, calibration, ownership and sessions. Physical input needs explicit focus arbitration; separate sessions alone do not make simultaneous control correct.
- Optionally evaluate a structured adapter for a different client with a publisher-supported interface or an owned/authorized OTClient build. This is a separate future target; it does not replace the OBS/foreground-input path or supply hidden prerequisites for the current roadmap.

**Scope boundary:** the early decision gate is M0's desktop compatibility evidence. Structured-client access is not on the critical path. Features in M2–M5 must be feasible through validated visual observations and desktop actions on the intended official-client/OT-server configuration; narrow or defer features whose required evidence cannot be obtained reliably.

## Minimal architecture changes

Keep three responsibilities clear:

1. **Adapter:** observe the client and execute validated low-level input; expose which observations/actions it can actually support.
2. **SDK/session:** coherent evidence, action arbitration, cooldown/resource reservations where shared, cancellation, confirmations and trace delivery.
3. **Features:** decide what should happen next. CaveBot, support and the hunt lifecycle compose bounded steps through the same session.

The UI edits configuration and displays state. It should not become a second decision engine.

| Change | Add when | Avoid |
|---|---|---|
| Desktop profile and capability declaration | M0 validates the selected integration; M1 checks hunt-pack prerequisites | Equating implemented support, OS dispatch and observed client acceptance |
| Capture health and read-only diagnostics | M0 needs readiness, blocking reasons and truthful freshness reporting | Treating local screenshot time as a source-frame timestamp |
| Inventory/container observations | M2 needs the first counts and container identity | A universal item database or fabricated inventory truth |
| Marker positions and floor fingerprints | Navigation recovery and transition steps need them | Invented absolute world coordinates or temporal policy inside the adapter |
| Entity observations | Instance targeting and positioning are ready to use them | Treating species count as creature identity |
| Richer confirmation predicates | Multiple workflows need baseline-change or multi-sensor checks | Equating input dispatch with game success |
| Lifecycle state machine | M3 connects hunting, travel and inventory work | A second scheduler or a generic workflow platform |
| Cooldown groups/reservations | Multiple features compete for the same action resource | Independent timers that knowingly conflict |

Reuse `Quality`, observation age, epoch, request requirements and existing replay contracts. Add field-level quality when partial inventory observations require it. Template-match scores are diagnostic scores, not calibrated probabilities.

CaveBot derives progress/no-progress over time from recorded marker observations. The adapter supplies observations, while the feature owns the history and recovery decision so replay can reconstruct why it acted.

Start route logic with typed data: move to recognized anchor, interact, wait for evidence, branch, call segment, return and stop. Keep loops bounded. There is no need to introduce a second scripting language merely because competitors expose Lua.

## Priorities and tradeoffs

| Order | Investment | Expected value | Principal dependency |
|---:|---|---|---|
| 0 | Compatibility tool, desktop profile, capture health and operator controls | Establishes the usable OBS/input path and exposes its limits | Existing SDK plus evidence from the intended client |
| 1 | Hunt Console, portable presets, route editing and trace inspection | Immediate reduction in setup and troubleshooting effort | M0 contracts; M0 verification before live activation |
| 2 | Navigation progress and bounded recovery | Fewer unexplained interruptions; enables trustworthy return routes | Live marker/progress fixtures |
| 3 | Supply awareness, verified loot and return intent | Longer useful sessions with deliberate termination | Inventory/container perception |
| 4 | Refill/depot/return cycle | Largest increase in end-to-end autonomy | Confirmed interactions and resumable phases |
| 5 | Combat rules, offensive rotation and positioning | Better hunting efficiency | Increasingly rich combat observations |
| 6 | Analytics, supervision and extensions | Better decisions and reuse across hunts | Stable events, verified data and capability contracts |

This orders dependencies and user value, not calendar estimates. Perception work dominates uncertainty, so schedule estimates should follow M0's compatibility checks and visual fixtures from the intended client. M1's offline work can run in parallel.

Defer an unrestricted script marketplace, broad automatic client detection, multi-client farming, speculative adaptive combat and unattended login recovery. They would expand the failure surface before the core hunt experience is complete. A language model could eventually help draft hunt packs, but runtime decisions should remain bounded and replayable; generated plans still require validation.

## How to know the project is improving

The primary measure is **manual interventions per completed hunt trip**, reported alongside the trip completion rate. Track setup time for a new hunt, time lost to travel/refill/stalls, explanation coverage for stops, and the proportion of state-changing actions with confirmed outcomes. Record baselines before setting improvement targets; this research does not substantiate a numerical “10x” claim.

Offline gates include schema/import migration, pure-policy tests with virtual time, replay consistency and detector fixtures. For each stateful workflow exercise success, unknown/error/stale evidence, cancellation, readiness loss, timeout, exhausted retries and restart reconciliation.

Live release gates extend M0's profile evidence with the feature-specific checks. Declare the actual client/OBS version, renderer where known, OT server, layout, DPI, capture and input mode. Exercise both success and deliberately ambiguous/failing situations. Verify no new dispatch is authorized after readiness loss is observed; an OS call already in progress cannot always be interrupted. Require bounded attempts and visible failure reasons, and inspect traces for confirmed transitions. Report source-freshness uncertainty explicitly. Offline tests cannot establish live-client compatibility.

## Recommended next implementation slice

Use the implemented tools to validate **one complete configured hunt**:

1. Collect M0 capture, input and alignment evidence on the intended installation.
2. Save representative frames and create guarded supply, container, item and digit detectors in Hunt workflow.
3. Inspect known, absent, zero, ambiguous and unreadable cases before enabling actions.
4. Configure finite outbound/return segments and confirmed deposit/sell/refill steps; exercise interruption and checkpoint reconciliation around each mutation.
5. Record a complete trip and its failure cases, then publish the supported client/layout/route combination with its evidence.

Continue offline tests and authoring improvements while live access is unavailable, keeping compatibility unverified. After the first validated trip, prioritize shared cooldowns, richer support rules and trace-derived trip metrics. Spatial combat still depends on richer perception.

## Primary sources

All sources below were consulted for this roadmap on 2026-10-12. Vendor descriptions are attributed feature claims; selected open-source files were inspected, not executed. Branch URLs can change after this research date.

- **Z1:** [ZeroBot general modules](https://docs.zerobot.net/general/).
- **Z2:** [ZeroBot CaveBot options](https://docs.zerobot.net/cavebot/interface/cavebot_options/) — recording, debug display and refill/sell helpers.
- **Z3:** [ZeroBot waypoint types](https://docs.zerobot.net/cavebot/interface/waypoint_types/) — typed navigation and interactions.
- **Z4:** [ZeroBot CaveBot settings](https://docs.zerobot.net/cavebot/interface/cavebot_settings/) — lure settings and scoped areas.
- **O1:** [OldBot official site](https://www.oldbot.com.br/) — current project direction and explicit compatibility limits.
- **O2:** [OldBot source repository](https://github.com/alfredomtx/oldbot).
- **O3:** [OldBot action catalog](https://github.com/alfredomtx/oldbot/blob/main/__Files/action_scripts_list.ahk) — registered supply, shop, depot, condition and route actions.
- **O4:** [OldBot waypoint recorder](https://github.com/alfredomtx/oldbot/blob/main/__Files/Classes/Waypoint/_WaypointRecorder.ahk) — recording controls and coordinate prerequisite.
- **V1:** [vBot supply decisions](https://github.com/Vithrax/vBot/blob/main/cavebot/supply_check.lua).
- **V2:** [vBot purchasing](https://github.com/Vithrax/vBot/blob/main/cavebot/buy_supplies.lua).
- **V3:** [vBot depot workflow](https://github.com/Vithrax/vBot/blob/main/cavebot/depositor.lua).
- **V4:** [vBot target priority](https://github.com/Vithrax/vBot/blob/main/targetbot/creature_priority.lua).
- **N1:** [nExBot CaveBot](https://github.com/mCodex/nExBot/blob/main/docs/CAVEBOT.md).
- **N2:** [nExBot TargetBot](https://github.com/mCodex/nExBot/blob/main/docs/TARGETBOT.md).
- **N3:** [nExBot AttackBot](https://github.com/mCodex/nExBot/blob/main/docs/ATTACKBOT.md).
- **N4:** [nExBot SmartHunt](https://github.com/mCodex/nExBot/blob/main/docs/SMARTHUNT.md) — analysis and the documented sources of its metrics.
- **W1:** [WindBot feature catalog](https://www.tibiawindbot.com/features.html).
- **W2:** [WindBot changelog](https://www.tibiawindbot.com/change-log.html) — historical status.
- **T1:** [Original TibiaAuto12 README at `0261932`](https://github.com/mrauter1/tibiaauto12/blob/02619320f03a6d5bd30623f73056d8714276043e/README.md) — OBS projector setup and physical-input path.
- **T2:** [Original message-input implementation at `0261932`](https://github.com/mrauter1/tibiaauto12/blob/02619320f03a6d5bd30623f73056d8714276043e/core/SendToClient.py) — historical client limitation.
- **B1:** [OBS Game Capture Source](https://obsproject.com/kb/game-capture-source) — capture modes and OBS-owned hook.
- **B2:** [OBS Capture Hook Certificate Update](https://obsproject.com/kb/capture-hook-certificate-update) — version/game-specific anti-cheat compatibility; not a Tibia validation result.
