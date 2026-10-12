def ScanHur(StatsPositions):
    """Return True only after a successful frame shows no haste icon."""
    from core.HookWindow import LocateBoolRGBImage

    has_haste = LocateBoolRGBImage(
        "images/PlayerStats/Hur.png", Precision=0.9, Region=tuple(StatsPositions)
    )
    return not bool(has_haste)
