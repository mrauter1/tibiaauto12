from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from core.perception import Frame
from modules.HuntWorkflow import (
    CropIntent,
    HuntWorkflow,
    WorkflowEditorError,
    _CropDialog,
    action_group,
    available_action_groups,
    default_workflow_document,
    inspection_text,
    load_document_file,
    parse_region,
    rebase_visual_paths,
    relative_template_path,
    status_text,
)
from tibia_client.resource_profiles import (
    ResourcePanelCandidate,
    ResourcePresetCandidates,
)


class Value:
    def __init__(self, value=None):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class FakeController:
    def __init__(self):
        self.calls = []
        self.control_epoch = 11
        self.resource_error = None
        self.adaptive_resource_error = None
        self.adaptive_resource_choices = None

    def validate(self, data, base_dir):
        self.calls.append(("validate", data, Path(base_dir)))
        return object()

    def save(self, filename, data):
        self.calls.append(("save", filename, data))

    def load(self, filename):
        self.calls.append(("load", filename))
        return self.loaded

    def start(self, data, base_dir, resume=True, expected_stop_epoch=None):
        self.calls.append(("start", data, Path(base_dir), resume, expected_stop_epoch))

    def stop(self):
        self.calls.append(("stop",))
        return True

    def inspect(self, data, base_dir, expected_stop_epoch=None):
        self.calls.append(("inspect", data, Path(base_dir), expected_stop_epoch))

    def request_return(self, now=False):
        self.calls.append(("return", now))

    def acknowledge_manual(self, operator_reconciled=False):
        self.calls.append(("ack", operator_reconciled))

    def apply_resource_preset(self, data, frame_path, base_dir):
        self.calls.append(("resource_preset", data, Path(frame_path), Path(base_dir)))
        if self.resource_error is not None:
            raise self.resource_error
        updated = deepcopy(data)
        updated["bindings"]["resource_soul"] = {
            "name": "resource_soul",
            "region": [10, 10, 30, 20],
        }
        updated["visual"]["resource_soul"] = {"kind": "count"}
        return SimpleNamespace(
            document=updated,
            readings={"resource_soul": SimpleNamespace(value={"count": 4})},
            bindings={},
        )

    def apply_adaptive_resource_preset(
        self, data, frame_path, base_dir, *, selection=None, use_ocr=False,
    ):
        self.calls.append((
            "adaptive_resource_preset", data, Path(frame_path), Path(base_dir),
            selection, use_ocr,
        ))
        if self.adaptive_resource_error is not None:
            raise self.adaptive_resource_error
        if self.adaptive_resource_choices is not None and selection is None:
            return self.adaptive_resource_choices
        updated = deepcopy(data)
        updated["bindings"]["resource_soul"] = {
            "name": "resource_soul", "region": [5, 6, 35, 26],
        }
        adaptive = {"reader": "paulistinha-skills-v1", "field": "resource_soul"}
        if use_ocr:
            adaptive["use_ocr"] = True
        updated["visual"]["resource_soul"] = {"kind": "count", "adaptive": adaptive}
        return SimpleNamespace(
            document=updated,
            readings={"resource_soul": SimpleNamespace(value={"count": 17}, diagnostics={})},
            bindings={"resource_soul": SimpleNamespace(region=(5, 6, 35, 26))},
            frame=Frame(Image.open(frame_path).convert("RGB"), source="offline:test.png"),
        )

    def apply_adaptive_resource_candidate(
        self, data, choices, candidate_index, base_dir, *, use_ocr=False,
    ):
        self.calls.append((
            "adaptive_resource_candidate", data, choices, candidate_index, Path(base_dir), use_ocr,
        ))
        selected = choices.candidates[candidate_index]
        updated = deepcopy(data)
        updated["bindings"]["resource_soul"] = {
            "name": "resource_soul", "region": list(selected.bounds),
        }
        updated["visual"]["resource_soul"] = {
            "kind": "count",
            "adaptive": {"reader": "paulistinha-skills-v1", "field": "resource_soul"},
        }
        return SimpleNamespace(
            document=updated,
            readings={"resource_soul": SimpleNamespace(value={"count": 23}, diagnostics={})},
            bindings={"resource_soul": SimpleNamespace(region=selected.bounds)},
            frame=choices.frame,
        )


def headless_ui(tmp_path):
    ui = object.__new__(HuntWorkflow)
    ui.data = default_workflow_document()
    ui.filename = str(tmp_path / "hunt.json")
    ui.controller = FakeController()
    ui._active = False
    ui._inspecting = False
    ui._busy = None
    ui._pending_epoch = None
    ui._control_serial = 0
    ui._closed = False
    ui.plan_id = Value("rats")
    ui.hunt_route = Value("hunt")
    ui.max_refill = Value("4")
    ui.outbound_route = Value("to_hunt")
    ui.return_route = Value("to_town")
    ui.resume = Value(False)
    ui.message = Value()
    ui.start_button = SimpleNamespace(configure=lambda **_kwargs: None)
    ui.stop_button = SimpleNamespace(configure=lambda **_kwargs: None)
    ui._set_config_enabled = lambda _enabled: None

    def dispatch(operation, done):
        try:
            done(operation(), None)
        except Exception as exc:
            done(None, str(exc))

    ui._dispatch_control = dispatch
    return ui


class FakeTree:
    def __init__(self):
        self.rows = {}
        self.selected = ()

    def delete(self, *items):
        for item in items:
            self.rows.pop(item, None)

    def get_children(self):
        return tuple(self.rows)

    def insert(self, _parent, _where, iid, values, tags=()):
        self.rows[iid] = {"values": values, "tags": tags}

    def selection(self):
        return self.selected

    def selection_remove(self, *_items):
        self.selected = ()

    def selection_set(self, item):
        self.selected = (item,)

    def item(self, item):
        return self.rows[item]


class FakeText:
    def __init__(self, value=""):
        self.value = value
        self.modified = False

    def get(self, *_args):
        return self.value

    def delete(self, *_args):
        self.value = ""

    def insert(self, _where, value):
        self.value = value

    def edit_modified(self, value=None):
        if value is None:
            return self.modified
        self.modified = bool(value)


class StateWidget:
    def __init__(self):
        self.state = None

    def configure(self, **values):
        if "state" in values:
            self.state = values["state"]


def realistic_editor(tmp_path):
    ui = headless_ui(tmp_path)
    ui.window = None
    ui._document_revision = 0
    ui._form_state = {}
    ui.route_phase = Value("outbound")
    ui.route_name = Value("")
    ui.supply_vars = {key: Value("") for key in (
        "id", "sensor", "return_below", "emergency_reserve", "refill_to"
    )}
    ui.supply_optional = Value(False)
    ui.action_vars = {key: Value("") for key in (
        "group", "id", "kind", "key", "target", "destination", "requires", "timeout",
        "retries", "sensor", "field", "op", "value",
    )}
    ui.action_mutating = Value(True)
    ui.binding_vars = {key: Value("") for key in ("id", "name", "region")}
    ui.visual_vars = {key: Value("") for key in (
        "id", "kind", "context_template", "context_region", "template", "region",
        "score", "context_score", "digits_dir",
    )}
    ui.visual_template = ui.visual_vars["template"]
    ui.crop_target = Value("Match template")
    ui.capture_file = Value("")
    ui.resource_panel_region = Value("")
    ui.resource_use_ocr = Value(False)
    ui.visual_mode = Value("Ordinary detector: the fields below control recognition.")
    ui.route_tree = FakeTree()
    ui.supply_tree = FakeTree()
    ui.action_tree = FakeTree()
    ui.binding_tree = FakeTree()
    ui.visual_tree = FakeTree()
    ui.action_group_box = SimpleNamespace(configure=lambda **_kwargs: None)
    ui.json_text = FakeText()
    ui._load_into_editor()
    return ui


def test_default_document_is_workflow_wrapper_without_fake_visual_evidence():
    value = default_workflow_document()
    assert value["schema"] == "tibiaauto.hunt-workflow"
    assert value["version"] == 1
    assert value["plan"]["version"] == 1
    assert value["bindings"] == {}
    assert value["visual"] == {}
    assert value["targets"] == []


def test_action_groups_cover_station_and_travel_phases():
    value = default_workflow_document()
    action_group(value, "deposit").append({"id": "store"})
    action_group(value, "outbound.before").append({"id": "door"})
    action_group(value, "return.after").append({"id": "arrive"})

    assert value["plan"]["actions"]["deposit"] == [{"id": "store"}]
    assert value["plan"]["travel"]["outbound"][0]["before"] == [{"id": "door"}]
    assert value["plan"]["travel"]["return"][0]["after"] == [{"id": "arrive"}]

    value["plan"]["travel"]["outbound"].append(
        {"route": "second_leg", "before": [], "after": []}
    )
    assert "outbound.1.before" in available_action_groups(value)
    action_group(value, "outbound.1.before").append({"id": "second_door"})
    assert value["plan"]["travel"]["outbound"][1]["before"] == [{"id": "second_door"}]


@pytest.mark.parametrize(
    "text, expected",
    [("0, 1, 20, 30", [0, 1, 20, 30]), ("5,6,7,8", [5, 6, 7, 8])],
)
def test_parse_region_uses_left_top_right_bottom(text, expected):
    assert parse_region(text) == expected


@pytest.mark.parametrize("text", ["1,2,3", "1,2,1,4", "-1,2,3,4", "a,b,c,d"])
def test_parse_region_rejects_ambiguous_or_empty_boxes(text):
    with pytest.raises(WorkflowEditorError):
        parse_region(text)


def test_bounded_loader_rejects_duplicate_keys_and_non_objects(tmp_path):
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"version": 1, "version": 2}', encoding="utf-8")
    with pytest.raises(WorkflowEditorError, match="duplicate JSON key"):
        load_document_file(duplicate)

    array = tmp_path / "array.json"
    array.write_text("[]", encoding="utf-8")
    with pytest.raises(WorkflowEditorError, match="JSON object"):
        load_document_file(array)


def test_template_paths_are_relative_to_workflow_directory(tmp_path):
    workflow = tmp_path / "profiles" / "hunt.json"
    template = workflow.parent / "templates" / "mana.png"
    template.parent.mkdir(parents=True)
    template.touch()
    assert relative_template_path(template, workflow) == "templates/mana.png"


def test_save_as_rebases_all_visual_asset_paths_without_retargeting(tmp_path):
    old_base, new_base = tmp_path / "old", tmp_path / "new"
    value = default_workflow_document()
    value["visual"] = {
        "mana": {
            "kind": "count",
            "context": {"template": "templates/mana/context.png", "region": [0, 0, 5, 5]},
            "digits": {str(number): f"templates/mana/{number}.png" for number in range(10)},
            "count_region": [0, 0, 20, 10],
        }
    }
    moved = rebase_visual_paths(value, old_base, new_base)
    context = new_base / moved["visual"]["mana"]["context"]["template"]
    digit = new_base / moved["visual"]["mana"]["digits"]["0"]
    assert context.resolve() == (old_base / "templates/mana/context.png").resolve()
    assert digit.resolve() == (old_base / "templates/mana/0.png").resolve()


def test_save_as_rebases_optional_thousands_separator_asset(tmp_path):
    old_base, new_base = tmp_path / "old", tmp_path / "new"
    value = default_workflow_document()
    value["visual"] = {
        "mana": {
            "kind": "count",
            "context": {"template": "templates/mana/context.png", "region": [0, 0, 5, 5]},
            "digits": {str(number): f"templates/mana/{number}.png" for number in range(10)},
            "digit_variants": {
                "3": ["templates/mana/3-alt.png"],
                "6": ["templates/mana/6-alt.png", "templates/mana/6-alt-2.png"],
            },
            "thousands_separator": "templates/mana/comma.png",
            "count_region": [0, 0, 20, 10],
        }
    }
    moved = rebase_visual_paths(value, old_base, new_base)
    separator = new_base / moved["visual"]["mana"]["thousands_separator"]
    variant = new_base / moved["visual"]["mana"]["digit_variants"]["6"][1]
    assert separator.resolve() == (old_base / "templates/mana/comma.png").resolve()
    assert variant.resolve() == (old_base / "templates/mana/6-alt-2.png").resolve()


def test_save_as_rebases_assets_without_changing_adaptive_metadata(tmp_path):
    old_base, new_base = tmp_path / "old", tmp_path / "new"
    value = default_workflow_document()
    value["visual"] = {
        "resource_mana": {
            "kind": "count",
            "context": {"template": "assets/context.png", "region": [0, 0, 10, 10]},
            "digits": {str(number): f"assets/{number}.png" for number in range(10)},
            "count_region": [10, 0, 40, 10],
            "adaptive": {
                "reader": "paulistinha-skills-v1",
                "field": "resource_mana",
            },
        }
    }

    moved = rebase_visual_paths(value, old_base, new_base)

    assert moved["visual"]["resource_mana"]["adaptive"] == value["visual"]["resource_mana"]["adaptive"]
    assert (new_base / moved["visual"]["resource_mana"]["digits"]["0"]).resolve() == (
        old_base / "assets/0.png"
    ).resolve()


def test_structured_visual_update_preserves_advanced_tuning(tmp_path):
    ui = headless_ui(tmp_path)
    ui.data["visual"] = {
        "corpse": {
            "kind": "template",
            "template": "old.png",
            "context": {"template": "guard.png", "region": [0, 0, 5, 5], "score": 0.9},
            "search_region": [0, 0, 20, 20],
            "score": 0.91,
            "ambiguity_margin": 0.07,
            "max_matches": 4,
        }
    }
    raw = {
        "id": "corpse", "kind": "template", "context_template": "guard2.png",
        "context_region": "1, 1, 6, 6", "template": "new.png",
        "region": "2, 2, 18, 18", "score": "0.95", "context_score": "0.96",
        "digits_dir": "",
    }
    ui.visual_vars = {key: Value(value) for key, value in raw.items()}
    ui._refresh_visual_trees = lambda: None

    assert ui._put_visual(silent=True) is True
    item = ui.data["visual"]["corpse"]
    assert item["ambiguity_margin"] == 0.07
    assert item["max_matches"] == 4
    assert item["template"] == "new.png"
    assert item["context"]["score"] == 0.96


def test_adaptive_visual_selection_locks_legacy_fields_without_changing_json(tmp_path):
    ui = realistic_editor(tmp_path)
    normal_entry, normal_combo, convert = StateWidget(), StateWidget(), StateWidget()
    ui._visual_legacy_widgets = ((normal_entry, "normal"), (normal_combo, "readonly"))
    ui._visual_convert_button = convert
    spec = {
        "kind": "count",
        "context": {"template": "guard.png", "region": [0, 0, 8, 8], "score": 0.91},
        "digits": {str(number): f"digits/{number}.png" for number in range(10)},
        "count_region": [8, 0, 48, 10],
        "score": 0.92,
        "max_digits": 5,
        "adaptive": {
            "reader": "paulistinha-skills-v1",
            "field": "resource_mana",
            "use_ocr": True,
        },
    }
    ui.data["visual"] = {"resource_mana": deepcopy(spec)}
    ui._refresh_visual_trees()
    ui.visual_tree.selected = ("resource_mana",)

    ui._select_visual()

    assert ui._visual_adaptive is True
    assert normal_entry.state == normal_combo.state == "disabled"
    assert convert.state == "normal"
    assert "bundled label and number recognition" in ui.visual_mode.get()
    before = deepcopy(ui.data)
    ui._collect_structured()
    assert ui.data == before
    assert ui.data["visual"]["resource_mana"] == spec

    # Even a programmatic Add/update cannot rewrite inactive compatibility
    # settings that the adaptive form deliberately keeps read-only.
    ui.visual_vars["score"].set("0.11")
    ui.visual_vars["digits_dir"].set("other-digits")
    assert ui._put_visual(silent=True)
    assert ui.data["visual"]["resource_mana"] == spec


def test_explicit_adaptive_conversion_removes_only_metadata_and_reenables_ordinary_fields(tmp_path):
    ui = realistic_editor(tmp_path)
    normal_entry, normal_combo, convert = StateWidget(), StateWidget(), StateWidget()
    ui._visual_legacy_widgets = ((normal_entry, "normal"), (normal_combo, "readonly"))
    ui._visual_convert_button = convert
    spec = {
        "kind": "count",
        "context": {"template": "guard.png", "region": [0, 0, 8, 8], "score": 0.91},
        "digits": {str(number): f"digits/{number}.png" for number in range(10)},
        "count_region": [8, 0, 48, 10],
        "score": 0.92,
        "foreground": [250, 250, 250],
        "tolerance": 10,
        "max_digits": 5,
        "adaptive": {"reader": "paulistinha-skills-v1", "field": "resource_mana"},
    }
    ui.data["visual"] = {"resource_mana": deepcopy(spec)}
    ui._refresh_visual_trees()
    ui.visual_tree.selected = ("resource_mana",)
    ui._select_visual()

    ui.convert_adaptive_visual()

    expected = deepcopy(spec)
    expected.pop("adaptive")
    assert ui.data["visual"]["resource_mana"] == expected
    assert ui._visual_adaptive is False
    assert normal_entry.state == "normal"
    assert normal_combo.state == "readonly"
    assert convert.state == "disabled"
    assert "ordinary count detector" in ui.message.get()

    ui.visual_vars["kind"].set("template")
    ui.visual_vars["template"].set("mana.png")
    ui.visual_vars["region"].set("8, 0, 48, 10")
    assert ui._put_visual()
    converted = ui.data["visual"]["resource_mana"]
    assert converted["kind"] == "template"
    assert "adaptive" not in converted


def test_selecting_ordinary_visual_after_adaptive_restores_legacy_controls(tmp_path):
    ui = realistic_editor(tmp_path)
    legacy, convert = StateWidget(), StateWidget()
    ui._visual_legacy_widgets = ((legacy, "normal"),)
    ui._visual_convert_button = convert
    ui.data["visual"] = {
        "adaptive": {
            "kind": "count", "context": {}, "digits": {},
            "adaptive": {"reader": "paulistinha-skills-v1", "field": "resource_health"},
        },
        "ordinary": {"kind": "count", "context": {}, "digits": {}},
    }
    ui._refresh_visual_trees()
    ui.visual_tree.selected = ("adaptive",)
    ui._select_visual()
    assert legacy.state == "disabled"

    ui.visual_tree.selected = ("ordinary",)
    ui._select_visual()

    assert ui._visual_adaptive is False
    assert legacy.state == "normal"
    assert convert.state == "disabled"
    assert "adaptive" in ui.data["visual"]["adaptive"]


def test_document_replacement_clears_stale_entity_forms(tmp_path):
    ui = realistic_editor(tmp_path)
    for variables in (ui.supply_vars, ui.action_vars, ui.binding_vars, ui.visual_vars):
        variables["id"].set("stale")

    ui._confirm_discard = lambda: True
    ui.new()
    assert all(variables["id"].get() == "" for variables in (
        ui.supply_vars, ui.action_vars, ui.binding_vars, ui.visual_vars
    ))

    ui.supply_vars["id"].set("also-stale")
    ui.json_text.value = '{"schema":"tibiaauto.hunt-workflow","version":1,"plan":{},"bindings":{},"visual":{}}'
    ui._apply_json()
    assert ui.supply_vars["id"].get() == ""
    assert ui.data["bindings"] == {}


def test_delete_then_collect_does_not_recreate_any_entity_type(tmp_path):
    ui = realistic_editor(tmp_path)
    plan = ui.data["plan"]
    plan["supplies"] = [{
        "id": "mana", "sensor": "mana_count", "emergency_reserve": 1,
        "return_below": 2, "refill_to": 3, "optional": False,
    }]
    plan["actions"]["prepare"] = [{
        "id": "food", "kind": "press", "key": "F1", "confirmation": {
            "sensor": "ready", "field": "present", "op": "eq", "value": True,
        }, "timeout": 2, "retries": 1, "mutating": True, "require_present": [],
    }]
    ui.data["bindings"] = {"mana_box": {"name": "mana_count", "region": [1, 2, 20, 30]}}
    ui.data["visual"] = {"mana_count": {
        "kind": "template", "template": "mana.png", "search_region": [0, 0, 10, 10],
        "context": {"template": "context.png", "region": [0, 0, 10, 10], "score": 0.9},
        "score": 0.9,
    }}
    ui._refresh_supply_tree()
    ui._refresh_action_tree()
    ui._refresh_visual_trees()

    ui.supply_tree.selected = ("0",)
    ui._select_supply()
    ui._remove_supply()
    ui.action_tree.selected = ("0",)
    ui._select_action()
    ui._remove_action()
    ui.binding_tree.selected = ("mana_box",)
    ui._select_binding()
    ui._remove_binding()
    ui.visual_tree.selected = ("mana_count",)
    ui._select_visual()
    ui._remove_visual()
    ui._collect_structured()

    assert plan["supplies"] == []
    assert plan["actions"]["prepare"] == []
    assert ui.data["bindings"] == {}
    assert ui.data["visual"] == {}


def test_collect_is_atomic_on_late_form_error_and_rename_targets_original(tmp_path):
    ui = realistic_editor(tmp_path)
    ui.data["bindings"] = {
        "old": {"name": "detector", "region": [0, 0, 10, 10]},
        "untouched": {"name": "other", "region": [1, 1, 2, 2]},
    }
    ui._refresh_visual_trees()
    ui.binding_tree.selected = ("old",)
    ui._select_binding()
    ui.binding_vars["id"].set("renamed")

    ui.visual_vars["id"].set("broken")
    ui.visual_vars["kind"].set("template")
    ui.visual_vars["context_region"].set("not a region")
    ui._bind_form("visual")
    ui.visual_vars["template"].set("changed.png")
    before = deepcopy(ui.data)
    with pytest.raises(WorkflowEditorError, match="Context region"):
        ui._collect_structured()
    assert ui.data == before

    ui._clear_form("visual")
    ui._collect_structured()
    assert "old" not in ui.data["bindings"]
    assert ui.data["bindings"]["renamed"]["name"] == "detector"
    assert "untouched" in ui.data["bindings"]


def test_incomplete_structured_entities_remain_savable_as_drafts(tmp_path):
    ui = realistic_editor(tmp_path)
    ui.supply_vars["id"].set("mana")
    ui.binding_vars["id"].set("mana_box")
    ui.visual_vars["id"].set("mana_count")
    ui.action_vars["id"].set("use_food")

    ui._collect_structured()

    assert ui.data["plan"]["supplies"][-1] == {"id": "mana", "sensor": "", "optional": False}
    assert ui.data["bindings"]["mana_box"] == {"name": ""}
    assert ui.data["visual"]["mana_count"]["kind"] == "template"
    assert ui.data["plan"]["actions"]["prepare"][0]["id"] == "use_food"


def test_changing_count_detector_to_template_removes_separator_assets(tmp_path):
    ui = realistic_editor(tmp_path)
    ui.data["visual"] = {
        "resource_mana": {
            "kind": "count",
            "context": {"template": "context.png", "region": [0, 0, 8, 8]},
            "digits": {str(number): f"{number}.png" for number in range(10)},
            "digit_variants": {"3": ["3-alt.png"]},
            "thousands_separator": "comma.png",
            "count_region": [8, 0, 40, 8],
        }
    }
    ui._refresh_visual_trees()
    ui.visual_tree.selected = ("resource_mana",)
    ui._select_visual()
    ui.visual_vars["kind"].set("template")
    ui.visual_vars["template"].set("mana.png")
    ui.visual_vars["region"].set("8, 0, 40, 8")

    assert ui._put_visual()
    spec = ui.data["visual"]["resource_mana"]
    assert spec["kind"] == "template"
    assert "digit_variants" not in spec
    assert "thousands_separator" not in spec


def test_switching_rows_flushes_previous_edit_and_duplicate_event_preserves_new_edit(tmp_path):
    ui = realistic_editor(tmp_path)
    ui.data["plan"]["supplies"] = [
        {"id": "a", "sensor": "old", "emergency_reserve": 1, "return_below": 2, "refill_to": 3},
        {"id": "b", "sensor": "second", "emergency_reserve": 1, "return_below": 2, "refill_to": 3},
    ]
    ui._refresh_supply_tree()
    ui.supply_tree.selected = ("0",)
    ui._select_supply()
    ui.supply_vars["sensor"].set("edited")

    ui.supply_tree.selected = ("1",)
    ui._select_supply()
    assert ui.data["plan"]["supplies"][0]["sensor"] == "edited"
    assert ui.supply_vars["id"].get() == "b"

    ui.supply_vars["sensor"].set("typed-after-first-event")
    ui._select_supply()  # queued duplicate <<TreeviewSelect>> for the same row
    assert ui.supply_vars["sensor"].get() == "typed-after-first-event"
    ui._collect_structured()
    assert ui.data["plan"]["supplies"][1]["sensor"] == "typed-after-first-event"


def test_row_switch_error_restores_previous_selection_and_buffer(tmp_path):
    ui = realistic_editor(tmp_path)
    ui.data["plan"]["supplies"] = [
        {"id": "a", "sensor": "one", "emergency_reserve": 1, "return_below": 2, "refill_to": 3},
        {"id": "b", "sensor": "two", "emergency_reserve": 1, "return_below": 2, "refill_to": 3},
    ]
    ui._refresh_supply_tree()
    ui.supply_tree.selected = ("0",)
    ui._select_supply()
    ui.supply_vars["return_below"].set("invalid")

    ui.supply_tree.selected = ("1",)
    ui._select_supply()

    assert ui.supply_tree.selection() == ("0",)
    assert ui.supply_vars["id"].get() == "a"
    assert ui.supply_vars["return_below"].get() == "invalid"
    assert ui.data["plan"]["supplies"][0]["return_below"] == 2


@pytest.mark.parametrize("entity", ["supply", "action", "binding", "visual"])
def test_rename_to_existing_id_is_rejected_without_mutation(tmp_path, entity):
    ui = realistic_editor(tmp_path)
    if entity == "supply":
        ui.data["plan"]["supplies"] = [
            {"id": "a", "sensor": "one"}, {"id": "b", "sensor": "two"},
        ]
        ui._refresh_supply_tree()
        ui.supply_tree.selected = ("0",)
        ui._select_supply()
        variables = ui.supply_vars
    elif entity == "action":
        ui.data["plan"]["actions"]["prepare"] = [
            {"id": "a", "kind": "press", "key": "F1", "confirmation": {}},
            {"id": "b", "kind": "press", "key": "F2", "confirmation": {}},
        ]
        ui._refresh_action_tree()
        ui.action_tree.selected = ("0",)
        ui._select_action()
        variables = ui.action_vars
    elif entity == "binding":
        ui.data["bindings"] = {"a": {"name": "one"}, "b": {"name": "two"}}
        ui._refresh_visual_trees()
        ui.binding_tree.selected = ("a",)
        ui._select_binding()
        variables = ui.binding_vars
    else:
        ui.data["visual"] = {
            "a": {"kind": "template", "context": {}},
            "b": {"kind": "template", "context": {}},
        }
        ui._refresh_visual_trees()
        ui.visual_tree.selected = ("a",)
        ui._select_visual()
        variables = ui.visual_vars
    variables["id"].set("b")
    before = deepcopy(ui.data)

    with pytest.raises(WorkflowEditorError, match="already in use"):
        ui._collect_structured()

    assert ui.data == before
    assert variables["id"].get() == "b"


def test_binding_rename_updates_refs_and_open_dirty_action_form(tmp_path):
    ui = realistic_editor(tmp_path)
    action = {
        "id": "move", "kind": "drag", "target": "old", "destination": "old",
        "require_present": ["old"], "confirmation": {
            "sensor": "old", "field": "present", "op": "eq", "value": True,
        }, "timeout": 2, "retries": 1,
    }
    ui.data["plan"]["actions"]["prepare"] = [action]
    ui.data["plan"]["supplies"] = [{"id": "mana", "sensor": "old"}]
    ui.data["bindings"] = {"old": {"name": "detector", "region": [0, 0, 10, 10]}}
    ui._refresh_action_tree()
    ui._refresh_visual_trees()
    ui.action_tree.selected = ("0",)
    ui._select_action()
    ui.action_vars["retries"].set("3")
    ui.binding_tree.selected = ("old",)
    ui._select_binding()
    ui.binding_vars["id"].set("new")

    assert ui._put_binding()
    assert ui.action_vars["target"].get() == "new"
    assert ui.action_vars["destination"].get() == "new"
    assert ui.action_vars["sensor"].get() == "new"
    assert ui.action_vars["requires"].get() == "new"
    ui._collect_structured()

    saved = ui.data["plan"]["actions"]["prepare"][0]
    assert saved["retries"] == 3
    assert saved["target"] == saved["destination"] == "new"
    assert saved["confirmation"]["sensor"] == "new"
    assert saved["require_present"] == ["new"]
    assert ui.data["plan"]["supplies"][0]["sensor"] == "new"


def test_edit_route_then_move_and_save_preserves_edited_name(tmp_path):
    ui = realistic_editor(tmp_path)
    ui.data["plan"]["travel"]["outbound"] = [
        {"route": "a", "before": [], "after": []},
        {"route": "b", "before": [], "after": []},
    ]
    ui._refresh_route_tree()
    ui.route_tree.selected = ("0",)
    ui._select_route()
    ui.route_name.set("a-edited")

    ui._move_route(1)
    ui._collect_structured()

    outbound = ui.data["plan"]["travel"]["outbound"]
    assert [segment["route"] for segment in outbound] == ["b", "a-edited"]


def test_open_action_follows_its_route_when_segment_moves(tmp_path):
    ui = realistic_editor(tmp_path)
    action = {
        "id": "on_a", "kind": "press", "key": "F1", "confirmation": {},
        "timeout": 2, "retries": 1, "require_present": [],
    }
    ui.data["plan"]["travel"]["outbound"] = [
        {"route": "a", "before": [action], "after": []},
        {"route": "b", "before": [], "after": []},
    ]
    ui._refresh_route_tree()
    ui._refresh_action_tree()
    ui.action_tree.selected = ("0",)
    ui._select_action()
    ui.route_tree.selected = ("0",)
    ui._select_route()

    ui._move_route(1)
    ui.action_vars["retries"].set("4")
    ui._collect_structured()

    outbound = ui.data["plan"]["travel"]["outbound"]
    assert outbound[0]["route"] == "b"
    assert outbound[0]["before"] == []
    assert outbound[1]["route"] == "a"
    assert outbound[1]["before"][0]["id"] == "on_a"
    assert outbound[1]["before"][0]["retries"] == 4


def test_removing_route_clears_owned_action_buffer_without_recreation(tmp_path):
    ui = realistic_editor(tmp_path)
    action = {
        "id": "removed_action", "kind": "press", "key": "F1", "confirmation": {},
        "timeout": 2, "retries": 1, "require_present": [],
    }
    ui.data["plan"]["travel"]["outbound"] = [
        {"route": "remove_me", "before": [action], "after": []},
        {"route": "keep_me", "before": [], "after": []},
    ]
    ui._refresh_route_tree()
    ui._refresh_action_tree()
    ui.action_tree.selected = ("0",)
    ui._select_action()
    ui.action_vars["retries"].set("5")
    ui.route_tree.selected = ("0",)
    ui._select_route()

    ui._remove_route()
    ui._collect_structured()

    outbound = ui.data["plan"]["travel"]["outbound"]
    assert [segment["route"] for segment in outbound] == ["keep_me"]
    assert ui.action_vars["id"].get() == ""
    assert all(item.get("id") != "removed_action" for _group, _index, item in ui._all_actions())


def test_open_action_follows_route_phase_change_during_collect(tmp_path):
    ui = realistic_editor(tmp_path)
    action = {
        "id": "cross_phase", "kind": "press", "key": "F1", "confirmation": {},
        "timeout": 2, "retries": 1, "require_present": [],
    }
    ui.data["plan"]["travel"] = {
        "outbound": [{"route": "a", "before": [action], "after": []}],
        "return": [{"route": "home", "before": [], "after": []}],
    }
    ui._refresh_route_tree()
    ui._refresh_action_tree()
    ui.action_tree.selected = ("0",)
    ui._select_action()
    ui.action_vars["retries"].set("6")
    ui.route_tree.selected = ("0",)
    ui._select_route()
    ui.route_phase.set("return")

    ui._collect_structured()

    assert ui.data["plan"]["travel"]["outbound"] == []
    moved = ui.data["plan"]["travel"]["return"][1]
    assert moved["route"] == "a"
    assert moved["before"][0]["id"] == "cross_phase"
    assert moved["before"][0]["retries"] == 6


def test_save_as_rebases_forms_and_later_start_keeps_new_paths(tmp_path, monkeypatch):
    ui = realistic_editor(tmp_path)
    old_file = tmp_path / "old" / "hunt.json"
    new_file = tmp_path / "new" / "hunt.json"
    ui.filename = str(old_file)
    ui.data["visual"] = {"corpse": {
        "kind": "template", "template": "templates/corpse.png", "search_region": [0, 0, 4, 4],
        "context": {"template": "templates/context.png", "region": [0, 0, 4, 4]},
    }}
    ui._load_into_editor()
    monkeypatch.setattr("modules.HuntWorkflow.filedialog.asksaveasfilename", lambda **_kwargs: str(new_file))

    ui.save_as()
    rebased = ui.data["visual"]["corpse"]["template"]
    ui.start()

    assert (new_file.parent / rebased).resolve() == (old_file.parent / "templates/corpse.png").resolve()
    assert ui.controller.calls[-1][0] == "start"
    assert ui.controller.calls[-1][1]["visual"]["corpse"]["template"] == rebased


def test_status_shows_phase_reason_supplies_and_pending_confirmation():
    text = status_text({
        "active": True,
        "phase": "refill",
        "reason": "below target",
        "supply_counts": {"mana": 17, "health": None},
        "pending_confirmation": "buy_mana",
        "manual_required": True,
        "manual_reason": "purchase result ambiguous",
    })
    assert "Phase: refill" in text
    assert "below target" in text
    assert "health=None, mana=17" in text
    assert "buy_mana" in text
    assert "purchase result ambiguous" in text


def test_inspection_preserves_known_zero_and_marks_it_observation_only():
    text = inspection_text({
        "active": False,
        "readings": {
            "mana": {"quality": "known", "value": {"present": True, "count": 0}, "detail": ""},
            "health": {"quality": "unknown", "value": None, "detail": "digit tie"},
        },
    })
    assert "observation-only" in text
    assert "'count': 0" in text
    assert "unknown" in text
    assert "digit tie" in text


def test_validate_and_start_pass_snapshot_base_directory_and_resume(tmp_path):
    ui = headless_ui(tmp_path)
    ui.validate()
    ui.start()

    validate, start = ui.controller.calls
    assert validate[0] == "validate"
    assert start[0] == "start"
    assert start[2] == tmp_path
    assert start[3] is False
    assert start[4] == 11
    assert start[1]["plan"]["id"] == "rats"
    assert start[1]["plan"]["travel"]["return"][0]["route"] == "to_town"


def test_validation_error_is_visible_and_blocks_start(tmp_path):
    ui = headless_ui(tmp_path)

    def reject_start(_data, _base, resume=True, expected_stop_epoch=None):
        del resume, expected_stop_epoch
        raise ValueError("bindings.mana references missing detector")

    ui.controller.start = reject_start
    ui.start()
    assert "bindings.mana references missing detector" in ui.message.get()
    assert not any(call[0] == "start" for call in ui.controller.calls)


def test_pending_start_uses_ui_epoch_and_can_be_cancelled_before_worker_entry(tmp_path):
    ui = headless_ui(tmp_path)
    work = []
    ui._dispatch_control = lambda operation, done: work.append((operation, done))

    ui.start()
    assert ui._pending_epoch == 11
    assert ui._busy == "Starting workflow"
    ui.stop()

    assert ui.controller.calls == []
    assert ui._pending_epoch is None
    assert ui._busy == "Cancelling pending activation"
    assert ui.message.get() == "Cancelling pending activation…"
    start_operation, start_done = work[0]
    stop_operation, stop_done = work[1]
    stop_done(stop_operation(), None)
    assert ui.controller.calls == [("stop",)]
    assert ui._busy is None
    assert ui.message.get() == "Pending workflow activation cancelled."
    # A late worker callback belongs to the cancelled control serial and cannot
    # replace the cancellation message or re-enable the stale activation.
    del start_operation
    start_done(None, "workflow activation was cancelled while preparing")
    assert ui.message.get() == "Pending workflow activation cancelled."


def test_inspection_uses_epoch_captured_before_dispatch(tmp_path):
    ui = headless_ui(tmp_path)
    ui._inspection_bindings = {}
    ui.inspect()
    call = ui.controller.calls[-1]
    assert call[0] == "inspect"
    assert call[3] == 11


def test_default_resource_profile_is_added_offline_without_saving(tmp_path):
    ui = realistic_editor(tmp_path)
    source = tmp_path / "client-frame.png"
    Image.new("RGB", (20, 20), "black").save(source)
    ui.capture_file.set(str(source))
    ui._mark_clean()
    work = []
    ui._dispatch_control = lambda operation, done: work.append((operation, done))

    ui.add_resource_profile()

    assert ui.controller.calls == []
    assert ui._busy == "Adding default resource profile"
    operation, done = work.pop()
    result = operation()
    done(result, None)

    assert ui.controller.calls[0][0] == "resource_preset"
    assert all(call[0] != "save" for call in ui.controller.calls)
    assert ui.data["bindings"]["resource_soul"]["region"] == [10, 10, 30, 20]
    assert ui.data != ui._clean_document
    assert "Soul=4" in ui.message.get()
    assert "offline only" in ui.message.get()
    assert "Inspect evidence" in ui.message.get()


def test_failed_default_resource_import_preserves_the_draft(tmp_path):
    ui = realistic_editor(tmp_path)
    source = tmp_path / "client-frame.png"
    Image.new("RGB", (20, 20), "black").save(source)
    ui.capture_file.set(str(source))
    ui.controller.resource_error = ValueError("resource_soul already exists")
    before = deepcopy(ui.data)

    ui.add_resource_profile()

    assert ui.data == before
    assert ui.controller.calls[-1][0] == "resource_preset"
    assert "resource_soul already exists" in ui.message.get()


def test_default_resource_import_is_disabled_during_live_inspection(tmp_path):
    ui = realistic_editor(tmp_path)
    source = tmp_path / "client-frame.png"
    Image.new("RGB", (20, 20), "black").save(source)
    ui.capture_file.set(str(source))
    ui._inspecting = True

    ui.add_resource_profile()

    assert ui.controller.calls == []


def test_adaptive_resource_detection_uses_optional_region_and_keeps_result_offline(tmp_path):
    ui = realistic_editor(tmp_path)
    source = tmp_path / "client-frame.png"
    Image.new("RGB", (40, 30), "black").save(source)
    ui.capture_file.set(str(source))
    ui.resource_panel_region.set("2, 3, 38, 28")
    ui.resource_use_ocr.set(True)
    ui._mark_clean()

    ui.detect_resource_panel()

    call = ui.controller.calls[-1]
    assert call[0] == "adaptive_resource_preset"
    assert call[4:] == ((2, 3, 38, 28), True)
    assert ui.data["visual"]["resource_soul"]["adaptive"] == {
        "reader": "paulistinha-skills-v1", "field": "resource_soul", "use_ocr": True,
    }
    assert ui._inspection_readings["resource_soul"].value["count"] == 17
    assert ui.data != ui._clean_document
    assert all(call[0] != "save" for call in ui.controller.calls)
    assert "offline only" in ui.message.get()


def test_adaptive_resource_detection_rejects_bad_region_without_mutating(tmp_path):
    ui = realistic_editor(tmp_path)
    source = tmp_path / "client-frame.png"
    Image.new("RGB", (40, 30), "black").save(source)
    ui.capture_file.set(str(source))
    ui.resource_panel_region.set("3, 4, 3, 20")
    before = deepcopy(ui.data)

    ui.detect_resource_panel()

    assert ui.data == before
    assert ui.controller.calls == []
    assert "Resource panel region" in ui.message.get()


def test_failed_adaptive_resource_detection_preserves_pending_edits(tmp_path):
    ui = realistic_editor(tmp_path)
    source = tmp_path / "client-frame.png"
    Image.new("RGB", (40, 30), "black").save(source)
    ui.capture_file.set(str(source))
    ui.plan_id.set("edited-before-detection")
    ui.controller.adaptive_resource_error = ValueError(
        "resource panel is ambiguous; select the intended panel area and retry"
    )

    ui.detect_resource_panel()

    assert ui.data["plan"]["id"] == "edited-before-detection"
    assert ui.controller.calls[-1][0] == "adaptive_resource_preset"
    assert "select the intended panel area" in ui.message.get()


def test_ambiguous_resource_detection_uses_one_click_choice_and_same_offline_frame(tmp_path):
    ui = realistic_editor(tmp_path)
    source = tmp_path / "client-frame.png"
    Image.new("RGB", (200, 100), "black").save(source)
    ui.capture_file.set(str(source))
    frame = Frame(Image.new("RGB", (200, 100), "navy"), source="offline:fixed.png")
    layouts = (
        SimpleNamespace(bounds=(5, 5, 95, 45)),
        SimpleNamespace(bounds=(105, 5, 195, 45)),
    )
    candidates = tuple(
        ResourcePanelCandidate(
            layout.bounds,
            {"resource_soul": (layout.bounds[0] + 40, 10, layout.bounds[2] - 5, 20)},
            1.0,
            layout,
        )
        for layout in layouts
    )
    choices = ResourcePresetCandidates("resource panel is ambiguous", candidates, frame)
    ui.controller.adaptive_resource_choices = choices
    shown = []

    def choose(value, callback):
        shown.append(value)
        callback(1)

    ui._show_resource_candidates = choose

    ui.detect_resource_panel()

    assert shown == [choices]
    assert [call[0] for call in ui.controller.calls] == [
        "adaptive_resource_preset", "adaptive_resource_candidate",
    ]
    assert ui.controller.calls[1][3] == 1
    assert ui.data["bindings"]["resource_soul"]["region"] == [105, 5, 195, 45]
    assert ui._last_inspection_frame is frame
    assert ui._inspection_offline is True
    assert "Soul=23" in ui.message.get()


def test_runtime_return_control_is_dispatched_off_the_ui_thread(tmp_path):
    ui = headless_ui(tmp_path)
    work = []
    ui._dispatch_control = lambda operation, done: work.append((operation, done))
    ui.request_return(True)
    assert ui.controller.calls == []
    assert ui._busy == "Requesting immediate return"
    operation, done = work.pop()
    done(operation(), None)
    assert ui.controller.calls == [("return", True)]
    assert ui._busy is None
    assert ui.message.get() == "Immediate return requested."


def test_close_hides_and_preserves_the_existing_workflow_panel():
    hidden = []
    ui = object.__new__(HuntWorkflow)
    ui._busy = None
    ui.message = Value()
    ui.window = SimpleNamespace(withdraw=lambda: hidden.append(True))
    ui.close()
    assert hidden == [True]
    assert "running generation are unchanged" in ui.message.get()


def test_close_cannot_hide_a_queued_activation():
    hidden = []
    ui = object.__new__(HuntWorkflow)
    ui._busy = "Starting workflow"
    ui._pending_epoch = 7
    ui.message = Value()
    ui.window = SimpleNamespace(withdraw=lambda: hidden.append(True))
    ui.close()
    assert hidden == []
    assert "Cancel the pending activation" in ui.message.get()


def test_manual_reconciliation_is_explicitly_distinct_from_recheck(tmp_path):
    ui = headless_ui(tmp_path)
    ui.acknowledge(False)
    ui.acknowledge(True)
    assert ui.controller.calls == [("ack", False), ("ack", True)]
    assert "Operator reconciliation recorded" in ui.message.get()


def test_crop_coordinates_map_back_to_original_saved_image():
    dialog = object.__new__(_CropDialog)
    dialog.image = Image.new("RGB", (400, 200))
    dialog._bounds = (10, 20, 200, 100)
    dialog._start = (60, 45)
    dialog._end_point = (110, 70)
    assert dialog.crop_box() == (100, 50, 200, 100)


@pytest.mark.parametrize(
    "purpose, filename, expected_field",
    [
        ("Context template", "context.png", "context_template"),
        ("Match template", "template.png", "template"),
        ("Digit 7", "7.png", "digits_dir"),
    ],
)
def test_crop_uses_immutable_intent_and_atomically_updates_form(
    tmp_path, monkeypatch, purpose, filename, expected_field,
):
    ui = realistic_editor(tmp_path)
    ui.visual_vars["id"].set("mana")
    ui.crop_target.set(purpose)
    intent = CropIntent(
        detector_id="mana", purpose=purpose, document_revision=ui._document_revision,
        binding_id="mana_box" if not purpose.startswith("Digit") else None,
        binding_region=(10, 10, 30, 30) if not purpose.startswith("Digit") else None,
    )
    destination = tmp_path / "templates" / "mana" / filename
    monkeypatch.setattr(
        "modules.HuntWorkflow.filedialog.asksaveasfilename",
        lambda **_kwargs: str(destination),
    )
    errors = []
    monkeypatch.setattr(
        "modules.HuntWorkflow.messagebox.showerror",
        lambda *args, **kwargs: errors.append((args, kwargs)),
    )
    destroyed = []
    dialog = object.__new__(_CropDialog)
    dialog.owner = ui
    dialog.intent = intent
    dialog.image = Image.new("RGB", (50, 50), "white")
    dialog.window = SimpleNamespace(destroy=lambda: destroyed.append(True))
    dialog.crop_box = lambda: (12, 13, 20, 21)

    dialog.save()

    assert errors == []
    assert destroyed == [True]
    assert destination.is_file()
    assert not list(destination.parent.glob(f".{destination.stem}.*.png"))
    assert ui.visual_vars[expected_field].get()
    if not purpose.startswith("Digit"):
        region_field = "context_region" if purpose == "Context template" else "region"
        assert ui.visual_vars[region_field].get() == "2, 3, 10, 11"


def test_invalid_crop_context_is_rejected_before_png_write(tmp_path, monkeypatch):
    ui = realistic_editor(tmp_path)
    ui.visual_vars["id"].set("mana")
    destination = tmp_path / "templates" / "mana" / "template.png"
    intent = CropIntent("mana", "Match template", ui._document_revision, "mana_box", (10, 10, 20, 20))
    monkeypatch.setattr(
        "modules.HuntWorkflow.filedialog.asksaveasfilename", lambda **_kwargs: str(destination),
    )
    errors = []
    monkeypatch.setattr(
        "modules.HuntWorkflow.messagebox.showerror", lambda *args, **_kwargs: errors.append(args),
    )
    dialog = object.__new__(_CropDialog)
    dialog.owner, dialog.intent = ui, intent
    dialog.image = Image.new("RGB", (50, 50), "white")
    dialog.window = SimpleNamespace(destroy=lambda: pytest.fail("invalid crop closed the dialog"))
    dialog.crop_box = lambda: (0, 0, 5, 5)

    dialog.save()

    assert errors
    assert not destination.exists()
    assert ui.visual_vars["template"].get() == ""


def test_open_crop_commits_dirty_binding_before_snapshot(tmp_path, monkeypatch):
    ui = realistic_editor(tmp_path)
    capture = tmp_path / "capture.png"
    Image.new("RGB", (50, 50)).save(capture)
    ui.capture_file.set(str(capture))
    ui.data["bindings"] = {"mana_box": {"name": "mana", "region": [10, 10, 30, 30]}}
    ui._refresh_visual_trees()
    ui.binding_tree.selected = ("mana_box",)
    ui._select_binding()
    ui.binding_vars["region"].set("20, 20, 40, 40")
    ui.visual_vars["id"].set("mana")
    ui.crop_target.set("Match template")
    captured = []
    monkeypatch.setattr(
        "modules.HuntWorkflow._CropDialog",
        lambda _owner, _source, intent: captured.append(intent),
    )

    ui._open_crop()

    assert captured[0].binding_id == "mana_box"
    assert captured[0].binding_region == (20, 20, 40, 40)
    assert ui.data["bindings"]["mana_box"]["region"] == [20, 20, 40, 40]


def test_crop_rejects_explicit_binding_for_another_detector(tmp_path):
    ui = realistic_editor(tmp_path)
    ui.data["bindings"] = {
        "selected": {"name": "health", "region": [0, 0, 10, 10]},
        "mana_box": {"name": "mana", "region": [10, 10, 20, 20]},
    }
    ui.visual_vars["id"].set("mana")
    ui.crop_target.set("Match template")

    with pytest.raises(WorkflowEditorError, match="uses detector 'health'"):
        ui._crop_intent("selected")


class FakeCanvas:
    def __init__(self):
        self.rectangles = []
        self.texts = []

    def delete(self, *_args):
        self.rectangles.clear()
        self.texts.clear()

    def winfo_width(self):
        return 110

    def winfo_height(self):
        return 110

    def create_image(self, *_args, **_kwargs):
        pass

    def create_rectangle(self, *coordinates, **options):
        self.rectangles.append((coordinates, options))

    def create_text(self, *coordinates, **options):
        self.texts.append((coordinates, options))


def test_inspection_renders_and_clears_ambiguity_candidates(monkeypatch):
    ui = object.__new__(HuntWorkflow)
    ui.inspection_canvas = FakeCanvas()
    ui._last_inspection_frame = SimpleNamespace(image=Image.new("RGB", (100, 100)))
    ui._inspection_result_bindings = {"corpse": {"region": [0, 0, 50, 50]}}
    ui._inspection_readings = {"corpse": {
        "value": {"count": 8},
        "diagnostics": {
            "type": "visual_ambiguity", "scope": "target",
            "effective_region": (5, 10, 25, 30),
            "boxes": ((10, 20, 30, 40), (40, 50, 60, 70)), "scores": (0.91, 0.90),
        }
    }}
    monkeypatch.setattr("modules.HuntWorkflow.ImageTk.PhotoImage", lambda image: image)

    ui._render_inspection_frame()
    assert [item[1]["outline"] for item in ui.inspection_canvas.rectangles] == [
        "#00e5ff", "#ff3b30", "#ff3b30",
    ]
    assert ui.inspection_canvas.rectangles[0][0] == (10.0, 15.0, 30.0, 35.0)
    assert any(item[1]["text"] == "corpse=8" for item in ui.inspection_canvas.texts)
    assert any("corpse target 0.910" in item[1]["text"] for item in ui.inspection_canvas.texts)

    ui._inspection_readings = {}
    ui._render_inspection_frame()
    assert [item[1]["outline"] for item in ui.inspection_canvas.rectangles] == ["#00e5ff"]


def test_module_import_does_not_construct_a_window_or_touch_controller():
    # Importing helpers above is the smoke test.  The controller is required
    # only when the operator explicitly constructs HuntWorkflow.
    assert not hasattr(SimpleNamespace(), "status")
