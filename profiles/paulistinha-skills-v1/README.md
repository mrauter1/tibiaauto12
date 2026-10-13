# Paulistinha Skills resource preset

Default visual resource preset authored from the supplied 1919 × 1079 desktop
screenshot on 2026-10-13. It discovers the **Hit Points**, **Mana**, **Soul Points**
and **Capacity** labels in an open Skills panel at the reference rendering scale.
Bindings are calculated from the labels in a saved client capture, never from
the desktop coordinates in the source screenshot.

In Hunt Workflow, open **Visual bindings**, load a client frame saved by
Compatibility Test, and choose **Add default resource profile**. The preset
adds `resource_health`, `resource_mana`, `resource_soul`, and `resource_capacity`
to the draft. Save the workflow and use **Inspect evidence** on the current
client. Imported values are offline readings, not live compatibility evidence.

The reference fixture reads health **710**, mana **1085**, soul **4**, and
capacity **192**. These are recognized values, not constants in the detector.
Health and mana here are absolute amounts, distinct from the SDK's percentage
bar sensors. Capacity is an integer display; fractional formats are unsupported.
Food, stamina, inventory contents, and rune production are not part of this preset.

## Assets and evidence

- `profile.json` defines the count detectors, foreground mask, confidence margins,
  and label-relative geometry. Commas must follow valid thousands grouping.
- `labels/` contains exact crops of the four labels. `digits/` contains the
  observed digit crops and alternate renderings found elsewhere in these rows.
- Digits **3** and **6** do not appear in these four readings. Their templates
  come from the repository's existing `images/TibiaSettings/StatsNumbers` atlas;
  their behavior in this client is **not verified by the supplied screenshot**.
- `provenance.json` records the source hash, crop boxes, and inherited assets.
  The repository stores only the relevant Skills-panel fixture, not the source
  desktop screenshot or chat content.
- Tests exercise the real screenshot crop, translated panels, unknown values,
  duplicate/missing labels, and an explicitly constructed alternate value. These
  checks establish offline behavior, not sustained accuracy across unseen values.

Label recognition is exact-scale. A different UI scale, font, language, column
width, or rendering may need another preset. The importer reports missing or
ambiguous anchors without changing the draft. Runtime label guards and evidence
freshness checks remain active; hidden or uncertain numbers are not treated as zero.

This profile does not identify the client implementation or establish which
capture/input methods the client accepts. Existing calibration and compatibility
checks are unchanged. Templates are referenced from this installation; they are
not embedded into an exported workflow JSON file.
