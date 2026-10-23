"""CaveBot detectors using client-local coordinates and coherent frames."""

from __future__ import annotations

import math
from typing import Optional

from core.HookWindow import Frame, LocateMatch, LocateMatches, Match, frame_scope


def _target_template(monster: str) -> str:
    return f"images/Targets/Names/{monster}.png"


def NumberOfTargets(BattlePosition, Monster, frame: Optional[Frame] = None, scale: float = 1.0):
    with frame_scope(frame) as snapshot:
        return len(
            LocateMatches(
                _target_template(Monster),
                Precision=0.8,
                Region=BattlePosition,
                frame=snapshot,
                Scale=scale,
            )
        )


def locate_target(BattlePosition, Monster, frame: Optional[Frame] = None, scale: float = 1.0) -> Optional[Match]:
    """Return a target match in client-local coordinates."""

    with frame_scope(frame) as snapshot:
        relative = LocateMatch(
            _target_template(Monster),
            Precision=0.86,
            Region=BattlePosition,
            frame=snapshot,
            Scale=scale,
        )
    if relative is None:
        return None
    # LocateMatch is crop-relative. This is the sole coordinate transform.
    return relative.translated(int(BattlePosition[0]), int(BattlePosition[1]))


def ScanTarget(BattlePosition, Monster, frame: Optional[Frame] = None, scale: float = 1.0):
    target = locate_target(BattlePosition, Monster, frame=frame, scale=scale)
    return target.center if target is not None else (0, 0)


def CheckWaypoint(image, map_positions, frame: Optional[Frame] = None, scale: float = 1.0):
    """Check whether a marker overlaps the minimap's geometric center."""

    with frame_scope(frame) as snapshot:
        marker = LocateMatch(
            f"images/MapSettings/{image}.png",
            Precision=0.7,
            Region=map_positions,
            frame=snapshot,
            Scale=scale,
        )
    if marker is None:
        return False

    map_width = int(map_positions[2]) - int(map_positions[0])
    map_height = int(map_positions[3]) - int(map_positions[1])
    center_x, center_y = map_width / 2.0, map_height / 2.0
    marker_x = marker.x + marker.width / 2.0
    marker_y = marker.y + marker.height / 2.0
    # A marker at the center may straddle either center pixel for even-sized
    # maps. Deriving tolerance from its own geometry avoids a fixed map inset.
    return (
        abs(marker_x - center_x) <= math.ceil(marker.width / 2.0)
        and abs(marker_y - center_y) <= math.ceil(marker.height / 2.0)
    )


_ATTACK_STYLES = ("Red", "BlackRed", "Pink", "BlackPink")
_ATTACK_SIDES = ("Left", "Top", "Right", "Bottom")


def _same_border(top: Match, bottom: Match, left: Match, right: Match, scale: float = 1.0) -> bool:
    top_cx, top_cy = top.center
    bottom_cx, bottom_cy = bottom.center
    left_cx, left_cy = left.center
    right_cx, right_cy = right.center
    vertical_span = bottom_cy - top_cy
    horizontal_span = right_cx - left_cx
    if not (12*scale <= vertical_span <= 40*scale and 12*scale <= horizontal_span <= 45*scale):
        return False
    if abs(top_cx - bottom_cx) > 4*scale or abs(left_cy - right_cy) > 4*scale:
        return False
    horizontal_center = (left_cx + right_cx) / 2.0
    vertical_center = (top_cy + bottom_cy) / 2.0
    return abs(top_cx - horizontal_center) <= 5*scale and abs(left_cy - vertical_center) <= 5*scale


def _coherent_border_centers(matches: dict[str, list[Match]], scale: float = 1.0) -> list[tuple[float, float]]:
    # Too many edge candidates means the templates are matching background
    # texture; fail conservatively instead of combining arbitrary rows.
    if any(len(found) > 100 for found in matches.values()):
        return []
    centers: list[tuple[float, float]] = []
    for top in matches["Top"]:
        for bottom in matches["Bottom"]:
            if bottom.y <= top.y or abs(top.center[0] - bottom.center[0]) > 4*scale:
                continue
            row_center = (top.center[1] + bottom.center[1]) / 2.0
            nearby_left = [item for item in matches["Left"] if abs(item.center[1] - row_center) <= 5*scale]
            nearby_right = [item for item in matches["Right"] if abs(item.center[1] - row_center) <= 5*scale]
            if any(_same_border(top, bottom, left, right, scale) for left in nearby_left for right in nearby_right):
                centers.append(((top.center[0] + bottom.center[0]) / 2.0, row_center))
    return centers


def target_selected(
    BattlePosition,
    frame: Optional[Frame] = None,
    target: Optional[str] = None,
    precision: float = 0.8,
    scale: float = 1.0,
) -> bool:
    """Return True only for associated border edges in the requested row.

    If ``target`` is a monster name, a matching name must occupy the same
    battle-list row. With no target, this reports any conservatively verified
    selected row and retains the behavior expected by legacy callers.
    """

    with frame_scope(frame) as snapshot:
        target_rows: Optional[list[int]] = None
        if target is not None:
            target_rows = [
                match.center[1]
                for match in LocateMatches(
                    _target_template(target),
                    Precision=0.8,
                    Region=BattlePosition,
                    frame=snapshot,
                    Scale=scale,
                )
            ]
            if not target_rows:
                return False
        for style in _ATTACK_STYLES:
            edges = {
                side: LocateMatches(
                    f"images/MonstersAttack/{side}{style}.png",
                    Precision=precision,
                    Region=BattlePosition,
                    frame=snapshot,
                    Scale=scale,
                )
                for side in _ATTACK_SIDES
            }
            border_centers = _coherent_border_centers(edges, scale)
            if target_rows is None and border_centers:
                return True
            if target_rows is not None and any(
                abs(border_y - target_y) <= 15*scale
                for _, border_y in border_centers
                for target_y in target_rows
            ):
                return True
    return False


def IsAttacking(BattlePosition, frame: Optional[Frame] = None, target: Optional[str] = None, scale: float = 1.0):
    """Legacy inverted predicate: False means an attack target is selected."""

    return not target_selected(BattlePosition, frame=frame, target=target, scale=scale)


def NeedFollow(frame: Optional[Frame] = None, scale: float = 1.0):
    with frame_scope(frame) as snapshot:
        return LocateMatch("images/TibiaSettings/Idle.png", Precision=0.7, frame=snapshot, Scale=scale) is not None


def NeedIdle(frame: Optional[Frame] = None, scale: float = 1.0):
    with frame_scope(frame) as snapshot:
        return LocateMatch("images/TibiaSettings/Following.png", Precision=0.7, frame=snapshot, Scale=scale) is not None


__all__ = [
    "NumberOfTargets",
    "locate_target",
    "ScanTarget",
    "CheckWaypoint",
    "target_selected",
    "IsAttacking",
    "NeedFollow",
    "NeedIdle",
]
