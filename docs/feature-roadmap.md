# Feature roadmap: from CaveBot to a complete hunting assistant

Research date: **2026-10-12**. Code baseline: **`f4c8212`**, on `reliability/client-foundation`.

**Status: product and engineering proposal, not implemented functionality.** This roadmap compares public primary documentation and selected source files. It does not establish a performance ranking or verify any competitor against a live client.

## Product direction

Make one hunting trip work exceptionally well: configure a hunt, reach it, fight and loot, recognize when to leave, return, deposit or sell, refill, and resume. Give the operator a clear explanation and control at every stage.

The strongest opportunity is the combination of a complete hunt lifecycle and an excellent assisted mode. Route editing, diagnostics, manual handoffs, and useful stop reasons can ship before every interaction is autonomous. Each completed slice should remove a real interruption from a hunting session.

Keep the shared SDK. The recent architecture already supplies coherent observations, request ownership, priorities, cancellation, and replay. The next investment is better game-state evidence and useful workflows built on those contracts.

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

## Starting point in this repository

| Already implemented | Remaining gap |
|---|---|
| Shared session, immutable observations, fresh-evidence requirements, prioritized requests, bounded native operations | Broader observations: inventory, containers, shops, floor changes, and individual creatures |
| Healing, mana, haste, food, ring, amulet, timed spells | Shared cooldown groups, condition-aware rules, richer presets and rule explanations |
| Marker navigation and observed marker arrival, Walk waypoints with configurable stand-after-arrival, target species priorities, follow | Movement-progress history, explicit recovery branches, tools, travel and richer combat positioning |
| Bounded looting triggered by a target-count drop | Corpse/container recognition and confirmed loot transfer; a disappeared target is not a confirmed kill |
| Validated route persistence and fingerprinted resume checkpoints | Convenient route authoring, portable hunt packs and full lifecycle checkpoints |
| JSONL recording and native-free replay | A user-facing timeline, diagnostic overlays and trustworthy hunt statistics |

`ActionResult.EXECUTED` means the native call ran. It does not prove the game accepted a spell, bought an item, or changed floors. Existing marker arrival is already an observed outcome; additional workflows need similarly explicit evidence.

## Delivery sequence

### M1 — Route Studio and Hunt Console

**User outcome:** configure, inspect, run and diagnose an existing hunt without editing files or guessing why it stopped.

- Duplicate, reorder, validate and import/export routes; preview waypoint markers and their calibrated regions.
- Add a guided setup check with capture previews and detected-region overlays. Explain incorrect geometry, missing templates and unreadable sensors before activation. Any input probe is a separate operator-started step; a successful OS call alone does not establish client acceptance.
- Introduce an explicit adapter capability declaration before enabling capability-dependent packs. The current adapter protocol has no such API; file validation alone cannot prove runtime support.
- Package the route, target list, support settings, supply policy and required capabilities as a versioned **hunt pack**. Keep machine-specific window identities and calibration bindings outside portable packs; require explicit local binding on import.
- Show the current phase, waypoint, last recognized evidence, pending action, cooldown, timeout/retry budget and reason for waiting. Add a small read-only diagnostics contract or feature projections: existing `FeatureStatus` and events do not expose all these fields.
- Provide distinct **Pause navigation** and **Stop all automation** controls, so users can choose whether valid support features continue. Stopping cancels future work across the session; an OS call already in progress may finish. Restart requires explicit activation and fresh evidence.
- Add pause-after-step and resume-from-recognized-anchor. Ambiguous anchors require operator selection; proximity in a list is not evidence of location.
- Export a bounded diagnostic trace and optional selected frames. Trace v1 is observation-only; frame files are a separate opt-in diagnostic artifact, not screenshots embedded in existing JSONL. Recorder callbacks remain read-only; UI commands go through the application/session boundary.
- Add navigation progress detection and bounded recovery. Distinguish waiting for combat, movement still progressing, missing marker and no progress. Retry or take a configured fallback only when supported by fresh evidence.
- Begin assisted route recording from recognized landmarks and operator annotations. Automatic recording of doors, tools and transitions waits for the corresponding detectors.

**Exit gate:** one existing hunt can be set up and diagnosed through the UI; invalid packs are rejected before activation; interruptions, missing markers and recovery limits have deterministic replay cases. Controlled live checks establish whether progress detection works on the selected client profile.

**Offline work available now:** capability and diagnostics contracts, route/preset UI, schema validation, status presentation, trace browser, replay scenarios and progress policies over fixtures. Live capture is needed to validate the new detectors, not to design these workflows.

### M2 — Supply-aware hunts and verified looting

**User outcome:** the bot knows when supplies are running low and can finish a hunt deliberately.

- Read the minimum useful inventory state: selected consumable counts, available capacity where readable, container identity and open/closed state. Add fields only for the first supported workflow.
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
- If the intended client offers a supported extension interface, evaluate a structured adapter for inventory, positions and events while retaining the desktop adapter.

**Early decision gate:** investigate structured-client access before investing heavily in complex pixel inventory/combat perception. A small read-only capability experiment may change M2–M4 effort substantially. If the real target client offers no suitable interface, continue with the pixel plan. Do not assume an OTClient module can run in an unrelated client.

## Minimal architecture changes

Keep three responsibilities clear:

1. **Adapter:** observe the client and execute validated low-level input; expose which observations/actions it can actually support.
2. **SDK/session:** coherent evidence, action arbitration, cooldown/resource reservations where shared, cancellation, confirmations and trace delivery.
3. **Features:** decide what should happen next. CaveBot, support and the hunt lifecycle compose bounded steps through the same session.

The UI edits configuration and displays state. It should not become a second decision engine.

| Change | Add when | Avoid |
|---|---|---|
| Client capability declaration | Hunt packs need to state their prerequisites | A promise that every adapter supports every feature |
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
| 1 | Hunt Console, portable presets, route editing and trace inspection | Immediate reduction in setup and troubleshooting effort | Existing SDK/UI integration |
| 2 | Navigation progress and bounded recovery | Fewer unexplained interruptions; enables trustworthy return routes | Live marker/progress fixtures |
| 3 | Supply awareness, verified loot and return intent | Longer useful sessions with deliberate termination | Inventory/container perception |
| 4 | Refill/depot/return cycle | Largest increase in end-to-end autonomy | Confirmed interactions and resumable phases |
| 5 | Combat rules, offensive rotation and positioning | Better hunting efficiency | Increasingly rich combat observations |
| 6 | Analytics, supervision and extensions | Better decisions and reuse across hunts | Stable events, verified data and capability contracts |

This orders dependencies and user value, not calendar estimates. Perception work dominates uncertainty, so schedule estimates should follow a capability experiment on the intended client.

Defer an unrestricted script marketplace, broad automatic client detection, multi-client farming, speculative adaptive combat and unattended login recovery. They would expand the failure surface before the core hunt experience is complete. A language model could eventually help draft hunt packs, but runtime decisions should remain bounded and replayable; generated plans still require validation.

## How to know the project is improving

The primary measure is **manual interventions per completed hunt trip**, reported alongside the trip completion rate. Track setup time for a new hunt, time lost to travel/refill/stalls, explanation coverage for stops, and the proportion of state-changing actions with confirmed outcomes. Record baselines before setting improvement targets; this research does not substantiate a numerical “10x” claim.

Offline gates include schema/import migration, pure-policy tests with virtual time, replay consistency and detector fixtures. For each stateful workflow exercise success, unknown/error/stale evidence, cancellation, readiness loss, timeout, exhausted retries and restart reconciliation.

Live release gates declare the actual client version, layout, DPI, capture and input mode. Exercise both success and deliberately ambiguous/failing situations. Verify no new dispatch is authorized after readiness loss is observed; an OS call already in progress cannot always be interrupted. Require bounded attempts and visible failure reasons, and inspect traces for confirmed transitions. Offline tests cannot establish live-client compatibility.

## Recommended next implementation slice

Build **M1 plus the smallest navigation-progress slice**, while collecting the observations needed for M2:

1. Adapter capability declarations, then hunt-pack schema and import/export with local calibration binding.
2. Read-only diagnostics contract, Hunt Console, trace timeline and clearly scoped pause/stop controls.
3. Route editing and recognized-anchor resume.
4. Explicit progress/no-progress states with bounded recovery.
5. Saved-image fixture collection for supply counts and open-container state.

This delivers useful functionality immediately and resolves the largest uncertainty in the full-cycle roadmap. The next slice is supply-aware return; then the first complete refill/depot loop.

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
