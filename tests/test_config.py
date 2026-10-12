from __future__ import annotations

import json
import threading

import pytest

import conf.conf_manager as manager_module
from conf.conf_manager import ConfManager, ConfigError
from core.paths import PROJECT_ROOT, SCRIPTS_DIR, project_path, scripts_path


@pytest.fixture
def isolated_scripts(tmp_path, monkeypatch):
    def resolve(filename):
        # ConfManager's filename rules are tested independently below.
        return tmp_path / str(filename)
    monkeypatch.setattr(manager_module, "scripts_path", resolve)
    return tmp_path


def test_project_paths_are_anchored_when_cwd_changes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert PROJECT_ROOT.name == "tibiaauto12"
    assert SCRIPTS_DIR == PROJECT_ROOT / "scripts"
    assert project_path("scripts/conf.json") == PROJECT_ROOT / "scripts" / "conf.json"


@pytest.mark.parametrize("name", [
    "../secret.json", "folder/a.json", "..\\secret.json", "C:\\temp\\a.json",
    "/tmp/a.json", "plain.txt", ".json",
])
def test_scripts_path_rejects_escape_and_non_json_names(name):
    with pytest.raises(ValueError):
        scripts_path(name)


def test_invalid_json_has_file_and_location_context(isolated_scripts):
    path = isolated_scripts / "broken.json"
    path.write_text('{"x":', encoding="utf-8")
    with pytest.raises(ConfigError, match=r"broken\.json at line 1, column"):
        ConfManager.get("broken.json")


def test_ensure_defaults_preserves_existing_values(isolated_scripts):
    (isolated_scripts / "settings.json").write_text(
        json.dumps({"saved": 7, "nested": {"choice": "mine"}}), encoding="utf-8"
    )
    result = ConfManager.ensure_defaults(
        {"saved": 99, "new": True, "nested": {"choice": "default", "added": 3}},
        "settings.json",
    )
    assert result == {"saved": 7, "new": True, "nested": {"choice": "mine", "added": 3}}


def test_locked_atomic_update_does_not_lose_concurrent_changes(isolated_scripts):
    ConfManager.set({"count": 0}, "counter.json")

    def increment_many():
        for _ in range(50):
            def increment(data):
                data["count"] += 1
            ConfManager.update("counter.json", increment)

    threads = [threading.Thread(target=increment_many) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert all(not thread.is_alive() for thread in threads)
    assert ConfManager.get("counter.json") == {"count": 400}
    assert not list(isolated_scripts.glob("*.tmp"))


def test_update_section_is_atomic_convenience(isolated_scripts):
    ConfManager.set({"outer": {"value": 1}}, "section.json")
    ConfManager.update_section("section.json", ("outer", "value"), 2)
    assert ConfManager.get("section.json")["outer"]["value"] == 2


def test_failed_atomic_write_preserves_previous_json(isolated_scripts):
    ConfManager.set({"valid": True}, "stable.json")
    with pytest.raises(ConfigError, match="atomically write"):
        ConfManager.set({"invalid": {object()}}, "stable.json")
    assert ConfManager.get("stable.json") == {"valid": True}
