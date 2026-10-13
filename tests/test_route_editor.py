import pytest

from engine.CaveBot.editor import RouteEditor
from engine.CaveBot.routes import Route, RouteError, Waypoint


def route():
    return Route("loop", (
        Waypoint("a", "Flag"),
        Waypoint("b", "Star"),
        Waypoint("c", "Skull"),
    ))


def test_selection_marker_edit_reorder_and_remove():
    editor = RouteEditor(route(), selected_index=1)
    editor.edit_marker("Cross")
    assert editor.selected == Waypoint("b", "Cross")

    editor.move_up()
    assert [waypoint.id for waypoint in editor.route.waypoints] == ["b", "a", "c"]
    assert editor.selected_index == 0
    editor.move_up()  # Boundary is a safe no-op.
    assert [waypoint.id for waypoint in editor.route.waypoints] == ["b", "a", "c"]

    removed = editor.remove()
    assert removed.id == "b"
    assert [waypoint.id for waypoint in editor.route.waypoints] == ["a", "c"]
    assert editor.selected.id == "a"


def test_duplicate_waypoint_and_route_always_generate_unique_ids():
    generated = iter(("a", "new-1", "new-2", "new-3", "new-4", "new-5"))
    editor = RouteEditor(route(), id_factory=lambda: next(generated))
    editor.select(0)
    duplicate = editor.duplicate_waypoint()
    assert duplicate.id == "new-1"
    assert len({item.id for item in editor.route.waypoints}) == 4

    copied = editor.duplicate_route("loop-copy")
    assert copied.name == "loop-copy"
    assert len(copied.waypoints) == len(editor.route.waypoints)
    assert not ({item.id for item in copied.waypoints} & {item.id for item in editor.route.waypoints})
    assert [item.mark for item in copied.waypoints] == [item.mark for item in editor.route.waypoints]


def test_add_after_tracks_list_selection_and_empty_editor():
    ids = iter(("first", "second"))
    editor = RouteEditor(Route("new", ()), id_factory=lambda: next(ids))
    assert editor.selected is None
    editor.add_after("Flag")
    editor.add_after("Star")
    assert [item.id for item in editor.route.waypoints] == ["first", "second"]
    assert editor.selected_index == 1


def test_editor_rejects_invalid_selection_and_requires_a_selected_waypoint():
    editor = RouteEditor(Route("new", ()))
    with pytest.raises(RouteError, match="select"):
        editor.remove()
    editor = RouteEditor(route())
    with pytest.raises(RouteError, match="outside"):
        editor.select(4)
    with pytest.raises(RouteError, match="one row"):
        editor.move(True)


def test_editor_rejects_wrong_route_and_exhausted_id_source():
    with pytest.raises(RouteError, match="requires a Route"):
        RouteEditor("not a route")
    editor = RouteEditor(route(), id_factory=iter(()).__next__)
    with pytest.raises(RouteError, match="unique waypoint id"):
        editor.duplicate_waypoint()
