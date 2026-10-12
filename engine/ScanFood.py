def scan_food(stats_positions):
    from core.HookWindow import LocateBoolRGBImage

    return bool(
        LocateBoolRGBImage(
            "images/PlayerStats/Starving.png", Precision=0.9, Region=tuple(stats_positions)
        )
    )
