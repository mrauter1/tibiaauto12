"""Default resource reader shared by the application and selected benchmark.

These are aliases to the tested composition, not subclasses or a second copy.
The implementation modules retain their existing locations so promotion changes
only selection. ``tibia_client.adaptive.ResourceReader`` remains the baseline
implementation used by the comparison tools and as the composition's base class.
"""
from tools.resource_reader import (
    CurrentNumberReader as NumberReader,
    CurrentResourceReader as ResourceReader,
)

__all__ = ["NumberReader", "ResourceReader"]
