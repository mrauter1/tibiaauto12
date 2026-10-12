import json

import pytest

from engine.CaveBot import routes
from engine.CaveBot.routes import (
    Route,
    RouteError,
    UnsupportedRouteAction,
    Waypoint,
    WaypointAction,
    load_checkpoint,
    load_route,
    route_from_data,
    save_checkpoint,
    save_route,
)


@pytest.fixture
def route_paths(tmp_path, monkeypatch):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    image_dir = tmp_path / "images" / "MapSettings"
    image_dir.mkdir(parents=True)
    (image_dir / "CheckMark.png").write_bytes(b"template")
    monkeypatch.setattr(routes, "scripts_path", lambda name: scripts / name)
    monkeypatch.setattr(routes, "project_path", lambda relative: tmp_path / relative)
    return scripts


def test_legacy_route_import_is_immutable_and_keeps_active_cursor_separate(route_paths):
    data = [
        {"mark": "CheckMark", "type": 1, "status": False},
        {"mark": "CheckMark", "type": 1, "status": True},
    ]
    route = route_from_data("hunt", data)
    assert isinstance(route.waypoints, tuple)
    assert routes.import_legacy_active_index(data, route) == 1
    assert "status" not in route.as_dict()["waypoints"][0]


def test_load_route_accepts_shipped_legacy_list(route_paths):
    path = route_paths / "hunt.json"
    path.write_text(json.dumps([
        {"mark": "CheckMark", "type": 1, "status": True},
    ]))
    route = load_route("hunt")
    assert route.waypoints[0].mark == "CheckMark"


def test_empty_new_route_can_be_saved_but_has_no_checkpoint(route_paths):
    route = Route("new-route", ())
    save_route(route)
    assert load_route("new-route").waypoints == ()
    with pytest.raises(RouteError, match="empty route"):
        save_checkpoint(route, 0)


@pytest.mark.parametrize("action", [2, 3, "Rope", "Shovel", "Use"])
def test_unsupported_actions_are_rejected_instead_of_walked(route_paths, action):
    with pytest.raises(UnsupportedRouteAction):
        route_from_data("hunt", [{"mark": "CheckMark", "type": action}])


def test_invalid_schema_and_asset_are_contextual(route_paths):
    with pytest.raises(RouteError, match="waypoint 1"):
        route_from_data("hunt", [123])
    with pytest.raises(RouteError, match="not a path"):
        route_from_data("hunt", [{"mark": "../CheckMark", "type": 1}], validate_assets=False)
    with pytest.raises(RouteError, match="missing waypoint image"):
        route_from_data("hunt", [{"mark": "Missing", "type": 1}])
    with pytest.raises(RouteError, match="version"):
        route_from_data("hunt", {"version": True, "waypoints": []})


def test_checkpoint_resumes_same_waypoint_by_identity(route_paths):
    first = Waypoint("a", "CheckMark", WaypointAction.WALK)
    second = Waypoint("b", "CheckMark", WaypointAction.WALK)
    route = Route("hunt", (first, second))
    save_route(route)
    save_checkpoint(route, 1)
    assert load_checkpoint(route) == 1


def test_changed_route_rejects_stale_checkpoint(route_paths):
    route = Route("hunt", (Waypoint("a", "CheckMark"),))
    save_checkpoint(route, 0)
    changed = Route("hunt", (Waypoint("a", "CheckMark"), Waypoint("b", "CheckMark")))
    with pytest.raises(RouteError, match="definition has changed"):
        load_checkpoint(changed)


def test_invalid_checkpoint_types_fail_closed(route_paths):
    route = Route("hunt", (Waypoint("a", "CheckMark"),))
    path = routes.checkpoint_file("hunt")
    path.write_text(json.dumps({
        "version": 1,
        "route": "hunt",
        "fingerprint": route.fingerprint,
        "index": True,
        "waypoint_id": "a",
    }))
    with pytest.raises(RouteError, match="outside the route"):
        load_checkpoint(route)


@pytest.mark.parametrize(
    "name",
    ["conf", "Json", "GUILoader", "DefaultWalk", "Void", ".json", "hunt.json.json", "hunt.state"],
)
def test_reserved_or_ambiguous_route_names_are_rejected(route_paths, name):
    with pytest.raises(RouteError):
        Route(name, ())


def test_save_route_never_overwrites_non_route_json(route_paths):
    path = route_paths / "profile.json"
    original = {"configured": True, "platform": "windows"}
    path.write_text(json.dumps(original))
    route = Route("profile", (Waypoint("a", "CheckMark"),))
    with pytest.raises(RouteError, match="refusing to overwrite non-route"):
        save_route(route)
    assert json.loads(path.read_text()) == original
