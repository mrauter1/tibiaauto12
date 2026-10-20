"""Selected opt-in resource reader with current-frame row and panel guards.

The retained numeric backend uses threshold consensus and bounded glyph caches.
Values always come from the current frame; unsupported readings remain UNKNOWN.
"""
from __future__ import annotations

from tools.resource_numeric_vector import VectorNumberReader
from tools.resource_panel_guard import PanelGuardMixin
from tools.resource_reader_experiments import ExperimentalResourceReader, VARIANTS
from tools.resource_row_geometry import StructuralRowMixin


FEATURES = VARIANTS["accelerated_robust"]


class CurrentNumberReader(VectorNumberReader):
    """The single selected numeric configuration."""

    supports_font_family = True

    def __init__(self, **kwargs):
        super().__init__(FEATURES, **kwargs)


class CurrentResourceReader(PanelGuardMixin, StructuralRowMixin, ExperimentalResourceReader):
    """Verify complete current rows, reject duplicate panels, then decode."""

    def __init__(self, *, number_reader=None, **kwargs):
        super().__init__(
            FEATURES,
            number_reader=number_reader if number_reader is not None else CurrentNumberReader(),
            **kwargs,
        )
