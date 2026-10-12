from __future__ import annotations

import numpy as np

from core.HookWindow import Frame, Match
from engine.CaveBot import Scanners


FRAME = Frame(np.zeros((120, 180, 3), dtype=np.uint8))


def test_scan_target_applies_one_crop_to_client_transform(monkeypatch):
    relative = Match(0, 0, 20, 8, 1.0)
    monkeypatch.setattr(Scanners, "LocateMatch", lambda *args, **kwargs: relative)

    target = Scanners.locate_target((50, 30, 170, 100), "Orc", frame=FRAME)
    assert target.position == (50, 30)
    assert Scanners.ScanTarget((50, 30, 170, 100), "Orc", frame=FRAME) == (60, 34)


def test_waypoint_uses_center_geometry_and_accepts_origin_match(monkeypatch):
    monkeypatch.setattr(
        Scanners,
        "LocateMatch",
        lambda *args, **kwargs: Match(0, 0, 7, 7, 1.0),
    )
    assert Scanners.CheckWaypoint("Flag", (20, 40, 34, 54), frame=FRAME)


def test_number_of_targets_uses_nms_result(monkeypatch):
    found = [Match(0, 0, 5, 5, 1.0), Match(10, 0, 5, 5, 1.0)]
    monkeypatch.setattr(Scanners, "LocateMatches", lambda *args, **kwargs: found)
    assert Scanners.NumberOfTargets((0, 0, 100, 100), "Orc", frame=FRAME) == 2


def _coherent_edges(path, **kwargs):
    if "Top" in path:
        return [Match(10, 10, 24, 2, 1.0)]
    if "Bottom" in path:
        return [Match(10, 32, 24, 2, 1.0)]
    if "Left" in path:
        return [Match(8, 10, 3, 23, 1.0)]
    if "Right" in path:
        return [Match(33, 10, 3, 23, 1.0)]
    return []


def test_target_selected_associates_edges_and_legacy_is_inverted(monkeypatch):
    monkeypatch.setattr(Scanners, "LocateMatches", _coherent_edges)
    battle = (0, 0, 100, 100)
    assert Scanners.target_selected(battle, frame=FRAME)
    assert Scanners.IsAttacking(battle, frame=FRAME) is False


def test_target_selected_does_not_combine_edges_from_different_rows(monkeypatch):
    def unrelated(path, **kwargs):
        if "Top" in path:
            return [Match(10, 5, 24, 2, 1.0)]
        if "Bottom" in path:
            return [Match(10, 75, 24, 2, 1.0)]
        if "Left" in path:
            return [Match(8, 38, 3, 23, 1.0)]
        if "Right" in path:
            return [Match(33, 38, 3, 23, 1.0)]
        return []

    monkeypatch.setattr(Scanners, "LocateMatches", unrelated)
    battle = (0, 0, 100, 100)
    assert not Scanners.target_selected(battle, frame=FRAME)
    assert Scanners.IsAttacking(battle, frame=FRAME) is True


def test_target_selected_can_require_the_requested_monster_row(monkeypatch):
    def selected_or_names(path, **kwargs):
        if "Targets/Names/Orc" in path:
            return [Match(45, 12, 20, 8, 1.0)]  # center y=16, selected row
        if "Targets/Names/Rat" in path:
            return [Match(45, 62, 20, 8, 1.0)]  # another row
        return _coherent_edges(path, **kwargs)

    monkeypatch.setattr(Scanners, "LocateMatches", selected_or_names)
    battle = (0, 0, 100, 100)
    assert Scanners.target_selected(battle, frame=FRAME, target="Orc")
    assert not Scanners.target_selected(battle, frame=FRAME, target="Rat")


def test_follow_detectors_accept_a_match_at_true_origin(monkeypatch):
    monkeypatch.setattr(
        Scanners,
        "LocateMatch",
        lambda *args, **kwargs: Match(0, 0, 4, 4, 1.0),
    )
    assert Scanners.NeedFollow(frame=FRAME)
    assert Scanners.NeedIdle(frame=FRAME)
