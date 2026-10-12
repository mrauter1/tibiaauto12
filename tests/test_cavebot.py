def test_duplicate_cavebot_window_reuses_existing_owner(monkeypatch):
    import modules.CaveBot as cavebot_module

    class FakeWindow:
        def __init__(self):
            self.lifts = 0
            self.focuses = 0

        def lift(self):
            self.lifts += 1

        def focus_force(self):
            self.focuses += 1

    existing = object.__new__(cavebot_module.CaveBot)
    existing._closed = False
    existing.window = FakeWindow()
    monkeypatch.setattr(cavebot_module, "_ACTIVE_CAVEBOT", existing)
    duplicate = cavebot_module.CaveBot.__new__(cavebot_module.CaveBot)
    assert duplicate is existing
    assert existing.window.lifts == 1
    assert existing.window.focuses == 1
