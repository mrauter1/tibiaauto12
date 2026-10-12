"""Amulet-slot observations based on the explicit empty-slot asset."""

from pathlib import Path
from core.paths import project_path


def ScanAmulet(AmuletPositions, Amulet=None, Precision=0.9):
    del Amulet, Precision
    from core.HookWindow import LocateBoolRGBImage

    return bool(
        LocateBoolRGBImage(
            "images/PlayerStats/NoAmulet.png", Precision=0.9, Region=tuple(AmuletPositions)
        )
    )


def SearchForAmulet(Amulet):
    from core.HookWindow import LocateCenterImage

    candidates = (
        Path("images/Amulets") / f"{Amulet}.png",
        Path("images/Items/None/Amulets") / f"{Amulet}.png",
    )
    paths = (project_path(p) for p in candidates)
    path = next((p for p in paths if p.is_file()), None)
    if path is None:
        raise FileNotFoundError(f"no image asset for amulet {Amulet!r}")
    return LocateCenterImage(str(path), Precision=0.9)
