"""Calibration detectors and headless profile validation."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


BattlePositions = [0, 0, 0, 0]
MapPositions = [0, 0, 0, 0]
StatsPositions = [0, 0, 0, 0]
GameWindow = [0, 0, 0, 0]
Player = [0, 0]
SQMs = [0] * 18
SQMsSizes = [0, 0]


class CalibrationError(ValueError):
    """Calibration is missing, inconsistent, or outside the captured frame."""

    def __init__(self, problems: str | Sequence[str]):
        self.problems = [problems] if isinstance(problems, str) else list(problems)
        super().__init__("Calibration is incomplete: " + "; ".join(self.problems))


def _is_coordinate(value: Any) -> bool:
    return type(value) is int


def _entry(profile: Mapping[str, Any], group: str, name: str) -> Mapping[str, Any] | None:
    try:
        value = profile[group][name][0]
    except (KeyError, IndexError, TypeError):
        return None
    return value if isinstance(value, Mapping) else None


def validate_calibration(
    profile: Mapping[str, Any], frame_size: tuple[int, int] | None = None
) -> bool:
    """Validate a saved calibration without importing capture or GUI modules.

    Legacy detection used zero coordinates as a not-found sentinel, so zero or
    negative saved coordinates are rejected rather than marked successful.
    """
    problems: list[str] = []
    if not isinstance(profile, Mapping):
        raise CalibrationError("profile root must be a JSON object")
    if not profile.get("Stats"):
        problems.append("profile Stats is false; run calibration again")

    width = height = None
    if frame_size is not None:
        if (not isinstance(frame_size, tuple) or len(frame_size) != 2
                or not all(_is_coordinate(v) and v > 0 for v in frame_size)):
            raise CalibrationError("frame_size must be a positive (width, height) tuple")
        width, height = frame_size

    calibrated_size = profile.get("calibration_size")
    if calibrated_size is not None:
        if (not isinstance(calibrated_size, (list, tuple)) or len(calibrated_size) != 2
                or not all(_is_coordinate(v) and v > 0 for v in calibrated_size)):
            problems.append("calibration_size must contain positive integer width and height")
        elif frame_size is not None and tuple(calibrated_size) != frame_size:
            problems.append(
                f"profile was calibrated for {calibrated_size[0]}x{calibrated_size[1]}, "
                f"but the selected capture is {width}x{height}; recalibrate this profile"
            )
        elif frame_size is None:
            # Offline checks can still verify geometry against its bound extent.
            width, height = calibrated_size

    point_names = ("LifePosition", "ManaPosition", "BattlePosition", "PlayerPosition")
    for name in point_names:
        item = _entry(profile, "Positions", name)
        if not item or item.get("Stats") is not True:
            problems.append(f"Positions.{name} was not detected")
            continue
        x, y = item.get("x"), item.get("y")
        if not _is_coordinate(x) or not _is_coordinate(y) or x <= 0 or y <= 0:
            problems.append(f"Positions.{name} has missing coordinates ({x!r}, {y!r})")
        elif width is not None and (x >= width or y >= height):
            problems.append(f"Positions.{name} ({x}, {y}) is outside {width}x{height}")

    box_names = ("StatusBarBox", "BattleBox", "RingBox", "AmuletBox", "GameWindowBox", "MapBox")
    for name in box_names:
        item = _entry(profile, "Boxes", name)
        if not item or item.get("Stats") is not True:
            problems.append(f"Boxes.{name} was not detected")
            continue
        x1, y1, x2, y2 = (item.get(key) for key in ("x", "y", "w", "h"))
        if not all(_is_coordinate(v) for v in (x1, y1, x2, y2)):
            problems.append(f"Boxes.{name} has non-integer bounds")
        elif x1 <= 0 or y1 <= 0 or x2 <= x1 or y2 <= y1:
            problems.append(f"Boxes.{name} has invalid bounds ({x1}, {y1}, {x2}, {y2})")
        elif width is not None and (x2 > width or y2 > height):
            problems.append(f"Boxes.{name} exceeds the {width}x{height} capture")

    for number in range(1, 10):
        name = f"SQM{number}"
        item = _entry(profile, "SQM", name)
        if not item or item.get("Stats") is not True:
            problems.append(f"SQM.{name} was not calculated")
            continue
        x, y = item.get("x"), item.get("y")
        if not _is_coordinate(x) or not _is_coordinate(y) or x <= 0 or y <= 0:
            problems.append(f"SQM.{name} has missing coordinates ({x!r}, {y!r})")
        elif width is not None and (x >= width or y >= height):
            problems.append(f"SQM.{name} ({x}, {y}) is outside {width}x{height}")

    if "ResourceBars" in profile:
        from core.perception import validate_region, DetectionError
        for name in ("HealthBar", "ManaBar"):
            try:
                box = profile["ResourceBars"][name]
                if width is None:
                    raise ValueError("resource bars require calibration_size")
                validate_region(box, (width, height))
            except (KeyError, TypeError, ValueError, DetectionError) as exc:
                problems.append(f"ResourceBars.{name}: {exc}")
    if problems:
        raise CalibrationError(problems)
    return True


def _capture_functions():
    # Capture is deliberately imported after startup selects fresh handles.
    from core.HookWindow import LocateMatch

    def center(image, **kwargs):
        match = LocateMatch(image, **kwargs)
        return None if match is None else match.center

    def locate(image, **kwargs):
        match = LocateMatch(image, **kwargs)
        return None if match is None else match.position

    return center, locate


def _require_point(name: str, value: Any) -> tuple[int, int]:
    if (value is None or not isinstance(value, Sequence) or len(value) < 2
            or not all(isinstance(v, (int, float)) for v in value[:2])):
        raise CalibrationError(f"{name} detector returned no coordinate")
    point = int(value[0]), int(value[1])
    if point[0] < 0 or point[1] < 0:
        raise CalibrationError(f"{name} image was not found; keep the full client UI visible")
    return point


def GetAccountNamePosition():
    center, _ = _capture_functions()
    return _require_point("account name", center('images/TibiaSettings/AccountName.png', Precision=0.9))


def GetBattlePosition():
    _, locate = _capture_functions()
    x, y = _require_point("battle list", locate('images/TibiaSettings/BattleList.png', Precision=0.85))
    result = (x + 8, y, x + 155, y + 415)
    BattlePositions[:] = result
    return result


def GetHealthPosition():
    center, _ = _capture_functions()
    return _require_point("health bar", center('images/PlayerSettings/health.PNG', Precision=0.8))


def GetManaPosition():
    center, _ = _capture_functions()
    return _require_point("mana bar", center('images/PlayerSettings/mana.PNG', Precision=0.8))


def GetMapPosition():
    _, locate = _capture_functions()
    x, y = _require_point("minimap settings", locate('images/MapSettings/MapSettings.png', Precision=0.8))
    result = (x - 106, y + 1, x - 1, y + 109)
    if result[0] <= 0 or result[1] <= 0:
        raise CalibrationError("minimap bounds extend outside the captured client")
    MapPositions[:] = result
    return result


def GetStatsPosition():
    _, locate = _capture_functions()
    x, y = _require_point("status bar stop button", locate('images/TibiaSettings/Stop.png', Precision=0.8))
    result = (x - 117, y + 1, x - 12, y + 11)
    if result[0] <= 0:
        raise CalibrationError("status bar bounds extend outside the captured client")
    StatsPositions[:] = result
    return result


def _first_detected(locate, name: str, images: Sequence[str], precision: float) -> tuple[int, int]:
    for image in images:
        point = locate(image, Precision=precision)
        if point is not None:
            return _require_point(name, point)
    raise CalibrationError(f"{name} image was not found; use the supported client layout")


def GetPlayerPosition():
    _, locate = _capture_functions()
    left_x, top_y = _first_detected(locate, "left game edge", [
        "images/PlayerSettings/LeftOption1.png", "images/PlayerSettings/LeftOption2.png",
        "images/PlayerSettings/LeftOption3.png"], 0.75)
    right_x, _ = _first_detected(locate, "right game edge", [
        "images/PlayerSettings/RightOption1.png", "images/PlayerSettings/RightOption2.png",
        "images/PlayerSettings/RightOption3.png", "images/PlayerSettings/RightOption4.png"], 0.75)
    _, bottom_y = _require_point("bottom game edge", locate(
        "images/PlayerSettings/EndLocation.png", Precision=0.7))
    if right_x <= left_x or bottom_y <= top_y:
        raise CalibrationError(
            f"game area bounds are inverted ({left_x}, {top_y}, {right_x}, {bottom_y})"
        )
    player = (int((left_x + right_x) / 2), int((top_y + bottom_y) / 2))
    GameWindow[:] = (left_x, top_y, right_x, bottom_y)
    Player[:] = player
    return player[0], player[1], *GameWindow


def SetSQMs(player: Sequence[int] | None = None, game_window: Sequence[int] | None = None):
    """Calculate the surrounding grid once; never retries recursively."""
    selected_player = tuple(player if player is not None else Player)
    selected_window = tuple(game_window if game_window is not None else GameWindow)
    if len(selected_player) != 2 or len(selected_window) != 4:
        raise CalibrationError("player/game window values have the wrong shape")
    px, py = selected_player
    left, top, right, bottom = selected_window
    if px <= 0 or py <= 0 or left <= 0 or top <= 0 or right <= left or bottom <= top:
        raise CalibrationError("player or game area was not detected; rerun calibration with the full game visible")
    sqm_width = int((right - left) / 15)
    sqm_height = int((bottom - top) / 11)
    if sqm_width <= 0 or sqm_height <= 0:
        raise CalibrationError("game area is too small to calculate SQM positions")
    SQMsSizes[:] = (sqm_width, sqm_height)
    values = (
        px - sqm_width, py + sqm_height, px, py + sqm_height, px + sqm_width, py + sqm_height,
        px - sqm_width, py, px, py, px + sqm_width, py,
        px - sqm_width, py - sqm_height, px, py - sqm_height, px + sqm_width, py - sqm_height,
    )
    if any(value <= 0 for value in values):
        raise CalibrationError("calculated SQM positions extend outside the captured client")
    SQMs[:] = values
    return values
