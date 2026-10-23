import gc
import threading

from core.tk_lifecycle import TkGarbageCollector


def test_unreachable_tk_style_cycles_finalize_on_ui_thread():
    class Window:
        def after(self, delay, callback):
            self.callback = callback
            return "timer"
        def after_cancel(self, timer):
            assert timer == "timer"

    finalized = []
    class Cycle:
        def __init__(self): self.reference = self
        def __del__(self): finalized.append(threading.get_ident())

    was_enabled = gc.isenabled()
    window = Window()
    owner = TkGarbageCollector(window)
    try:
        worker = threading.Thread(target=Cycle)
        worker.start()
        worker.join()
        assert not finalized
        assert not gc.isenabled()
        window.callback()
        assert finalized == [threading.get_ident()]
    finally:
        owner.close()
    assert gc.isenabled() == was_enabled
