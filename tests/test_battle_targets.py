"""Creature selection contracts, independent of OCR engine and game window."""
import pytest

from tibia_client.battle_text import BattleTextReader, bundled_target
from tibia_client.models import Quality, Reading, Target


def row(name, y=10, *, selected=False, complete=True):
    return dict(name=name, position=(50, y), box=(20, y-3, 80, y+3),
                selected=selected, complete=complete)


def read(monkeypatch, rows, pattern, *, quality=Quality.KNOWN, diagnostics=None):
    reader = BattleTextReader()
    monkeypatch.setattr(reader, "read_all", lambda *args: Reading(rows, quality, diagnostics=diagnostics or {}))
    try:
        return reader.read(None, (), pattern, 1.0)
    finally:
        reader.close()


@pytest.mark.parametrize("pattern,names", [
    ("Lizard *", ["Lizard Henchman", "Lizard Sentinel"]),
    ("lizardhenchman", ["Lizard Henchman"]),
    ("*Sentinel", ["Lizard Sentinel"]),
    ("Rat?", ["Rats", "Rat1"]),
    ("Rat[1]", ["Rat[1]"]),
])
def test_target_patterns_match_names_without_interpreting_paths(monkeypatch, pattern, names):
    rows = tuple(row(name, y=10+i*20) for i, name in enumerate(
        ("Lizard Henchman", "Lizard Sentinel", "Rat", "Rats", "Rat[1]", "Rat1")))
    reading = read(monkeypatch, rows, pattern)
    assert reading.quality is Quality.KNOWN
    assert reading.value.count == len(names)
    assert tuple(reading.diagnostics["matched_names"]) == tuple(names)


def test_wildcard_prefers_selected_matching_row_and_preserves_partial_count(monkeypatch):
    reading = read(monkeypatch, (
        row("Lizard Henchman"), row("Lizard Sentinel", 30, selected=True),
        row("Lizard Henchman", 50), row("Lizard ...", 70, complete=False),
    ), "Lizard *", quality=Quality.UNKNOWN)
    assert reading.quality is Quality.KNOWN
    assert reading.value == Target(3, (50, 30), True, False)
    assert reading.diagnostics["count_for_loot"] == 3


def test_completed_read_without_matching_creature_is_absent_even_with_other_unreadable_rows(monkeypatch):
    reading = read(monkeypatch, (row("Rat"), row("Lizard ...", complete=False)),
                   "Lizard *", quality=Quality.UNKNOWN)
    assert reading.quality is Quality.KNOWN
    assert reading.value == Target(0, None, False, False)
    assert reading.diagnostics["absence_assumed"]


@pytest.mark.parametrize("rows,diagnostics", [(None, {}), ((), {"retryable": True})])
def test_pending_or_failed_ocr_waits_instead_of_inventing_a_count(monkeypatch, rows, diagnostics):
    reading = read(monkeypatch, rows, "Lizard *", quality=Quality.UNKNOWN, diagnostics=diagnostics)
    assert reading.value is None and reading.quality is Quality.UNKNOWN


@pytest.mark.parametrize("name", ["Lizard *", "Rat?", "../Rat"])
def test_target_text_is_not_a_template_path_or_file_glob(name):
    assert bundled_target(name) is None
