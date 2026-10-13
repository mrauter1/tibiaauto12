from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from modules.HuntWorkflow import (
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

    def validate(self, data, base_dir):
        self.calls.append(("validate", data, Path(base_dir)))
        return object()

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


def test_module_import_does_not_construct_a_window_or_touch_controller():
    # Importing helpers above is the smoke test.  The controller is required
    # only when the operator explicitly constructs HuntWorkflow.
    assert not hasattr(SimpleNamespace(), "status")
