"""Ring-slot observations based on positive visual evidence."""

from pathlib import Path
from core.paths import project_path


def ScanRing(RingPositions):
    from core.HookWindow import LocateBoolRGBImage

    return bool(
        LocateBoolRGBImage(
            "images/PlayerStats/NoRing.png", Precision=0.9, Region=tuple(RingPositions)
        )
    )


def SearchForRing(Ring):
    from core.HookWindow import LocateCenterImage

    candidates = (
        Path("images/Rings") / f"{Ring}.png",
        Path("images/Items/None/Rings") / f"{Ring}.png",
    )
    paths = (project_path(p) for p in candidates)
    path = next((p for p in paths if p.is_file()), None)
    if path is None:
        raise FileNotFoundError(f"no image asset for ring {Ring!r}")
    return LocateCenterImage(str(path), Precision=0.9)
