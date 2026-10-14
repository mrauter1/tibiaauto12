# First evaluation of the expanded real corpus

The first complete comparison after switching acceptance to unchanged originals
is preserved as [JSON](iteration-03-real-originals.json) and
[Markdown](iteration-03-real-originals.md). All five techniques received the same
19 original images, with no generated variants. The report passed its integrity
and acceptance safety gates.

| Technique | Primary exact values | Wrong accepted values | Primary unknowns |
|---|---:|---:|---:|
| PR #4 exact-scale preset | 4 / 28 | 0 | 24 |
| Adaptive templates | 24 / 28 | 0 | 4 |
| Adaptive + label OCR | 27 / 28 | 0 | 1 |
| Adaptive + digit OCR | 24 / 28 | 0 | 4 |
| Adaptive + both OCR stages | 27 / 28 | 0 | 1 |

Both real safety controls were rejected. The two-panel control's visible truth
is retained but excluded from numeric coverage. All reference sources were also
included; no technique accepted a contradicted value on a reference.

Five new raw downloads were selected and annotated before inference. A first
serialized timing pass also evaluated the four new acceptance-role images:
Gentebra and Lutabra passed, Loyalty abstained safely, and Calmera had no layout.
That preliminary pass printed results only; it is not the timing evidence for
this PR. The complete archived comparison is the auditable first full result.

In this initial run, Calmera needed label OCR to discover the panel. It read Mana 928, Soul 100,
and Capacity 5988 exactly; Health 3755 remains unknown because its thousands
separator is weakly matched. Digit OCR alone does not recover discovery.

In this initial run, the visibly different Beta reference also needed label OCR. It read Health 1025,
Mana 330 and Capacity 1785, while Soul 100 has no verified complete value region.
This source is a reference diagnostic, not primary acceptance evidence.

Calmera and Beta were moved from heldout to train when these failures were
examined. Source pixels, values and boxes were not changed. The other three new
sources did not drive reader tuning. The small public-image sample does not
establish live gameplay reliability or an exact shared executable version.

The earlier proposal-only discovery optimization was rejected during independent
review because it could hide a second panel. Its scratch timing results are not
published as accepted performance evidence. The delivered search keeps exhaustive
global Health/Capacity seeds and rejects multiple valid panel layouts.
