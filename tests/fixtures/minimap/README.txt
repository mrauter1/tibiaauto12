Recorded minimap crops

These 136 x 135 RGB PNGs are crops of the user's client captures, kept separate
from the frozen resource-reading screenshot corpus. They contain only minimap
pixels, including the client crosshair and markers where present.

island-minimap-20261023.png: island view at 1.25 captured pixels per world tile.
minimap-maximum-zoom-20261024.png: maximum zoom, 5 captured pixels per world tile.
minimap-three-levels-out-20261024.png: three steps out, 0.625 pixels per world tile.

The zoom-pair alignments are reviewed atlas matches, not independent in-game
coordinate measurements. The recorded-image tests require the external local
atlas and skip if it is absent. Synthetic atlas tests run without that data.
