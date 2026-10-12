import threading
import time
import unittest
import uuid
import math

from core import runtime
from core.ThreadManager import ThreadManager


def _name(prefix):
    return f"{prefix}-{uuid.uuid4().hex}"


def _eventually(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.002)
    return bool(predicate())


class RuntimeTests(unittest.TestCase):
    def tearDown(self):
        runtime.stop_all(timeout=1.0)

    def test_current_token_wait_and_cancel(self):
        name = _name("token")
        observed = {}
        started = threading.Event()

        def target():
            observed["token"] = runtime.current_token()
            started.set()
            runtime.wait(30)

        thread = runtime.start(name, target)
        self.assertTrue(started.wait(1))
        self.assertTrue(runtime.cancel(name, timeout=1))
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertTrue(observed["token"].cancelled)

    def test_concurrent_starts_reserve_one_worker(self):
        name = _name("barrier")
        callers = 24
        barrier = threading.Barrier(callers)
        release = threading.Event()
        state_lock = threading.Lock()
        calls = 0
        active = 0
        maximum = 0
        returned = []

        def target():
            nonlocal calls, active, maximum
            with state_lock:
                calls += 1
                active += 1
                maximum = max(maximum, active)
            try:
                release.wait(1)
                runtime.checkpoint()
            finally:
                with state_lock:
                    active -= 1

        def caller():
            barrier.wait()
            returned.append(runtime.start(name, target))

        threads = [threading.Thread(target=caller) for _ in range(callers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(1)

        self.assertEqual(calls, 1)
        self.assertEqual(maximum, 1)
        self.assertEqual(len({id(thread) for thread in returned}), 1)
        release.set()
        self.assertTrue(runtime.cancel(name, timeout=1))

    def test_cancel_then_restart_waits_for_uncooperative_generation(self):
        name = _name("restart")
        old_started = threading.Event()
        allow_old_exit = threading.Event()
        new_started = threading.Event()
        active = 0
        maximum = 0
        lock = threading.Lock()

        def old_target():
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            old_started.set()
            try:
                allow_old_exit.wait(1)
            finally:
                with lock:
                    active -= 1

        def new_target():
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            new_started.set()
            try:
                runtime.wait(30)
            finally:
                with lock:
                    active -= 1

        runtime.start(name, old_target)
        self.assertTrue(old_started.wait(1))
        self.assertFalse(runtime.cancel(name, timeout=0.01))
        runtime.start(name, new_target)
        self.assertFalse(new_started.wait(0.03))
        allow_old_exit.set()
        self.assertTrue(new_started.wait(1))
        self.assertEqual(maximum, 1)
        self.assertTrue(runtime.cancel(name, timeout=1))

    def test_barrier_start_and_cancel_calls_are_race_safe(self):
        name = _name("start-cancel")
        callers = 20
        barrier = threading.Barrier(callers)
        errors = []
        state_lock = threading.Lock()
        active = 0
        maximum = 0
        began = threading.Event()

        def target():
            nonlocal active, maximum
            with state_lock:
                active += 1
                maximum = max(maximum, active)
            began.set()
            try:
                while True:
                    runtime.wait(0.001)
            finally:
                with state_lock:
                    active -= 1

        runtime.start(name, target)
        self.assertTrue(began.wait(1))

        def caller(index):
            try:
                barrier.wait()
                if index % 2:
                    runtime.start(name, target)
                else:
                    runtime.cancel(name, timeout=0.02)
            except BaseException as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=caller, args=(index,))
            for index in range(callers)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(1)

        runtime.cancel(name, timeout=1)
        self.assertEqual(errors, [])
        self.assertEqual(maximum, 1)

    def test_exception_is_exposed_in_status(self):
        name = _name("failure")

        def target():
            raise LookupError("worker failed")

        thread = runtime.start(name, target)
        thread.join(1)
        worker_status = runtime.status(name)
        self.assertIsInstance(worker_status["error"], LookupError)
        self.assertIn("worker failed", worker_status["traceback"])
        self.assertFalse(worker_status["alive"])

    def test_worker_phase_is_exposed_and_stale_generation_cannot_update_it(self):
        name = _name("phase")
        running = threading.Event()

        def target():
            self.assertTrue(runtime.set_phase("Waiting for game focus"))
            running.set()
            runtime.wait(30)

        thread = runtime.start(name, target)
        self.assertTrue(running.wait(1))
        self.assertEqual(runtime.status(name)["phase"], "Waiting for game focus")
        self.assertTrue(runtime.cancel(name, timeout=1))
        thread.join(1)
        self.assertEqual(runtime.status(name)["phase"], "Stopped")
        self.assertFalse(runtime.set_phase("Running"))

    def test_nonfinite_runtime_durations_are_rejected(self):
        for duration in (math.inf, -math.inf, math.nan):
            with self.assertRaises(ValueError):
                runtime.wait(duration)
            with self.assertRaises(ValueError):
                runtime.cancel(_name("duration"), timeout=duration)
            with self.assertRaises(ValueError):
                runtime.stop_all(timeout=duration)
            with self.assertRaises(ValueError):
                runtime.CancellationToken("duration", 1).wait(duration)

    def test_input_waiter_is_cancelled_while_lock_is_held(self):
        name = _name("input-wait")
        began = threading.Event()

        def target():
            began.set()
            with runtime.input_transaction("blocked worker"):
                self.fail("cancelled worker entered locked transaction")

        with runtime.input_transaction("test holder"):
            thread = runtime.start(name, target)
            self.assertTrue(began.wait(1))
            self.assertTrue(runtime.cancel(name, timeout=1))
        thread.join(1)
        self.assertFalse(thread.is_alive())

    def test_one_hundred_pause_restart_cycles_never_overlap(self):
        name = _name("toggle")
        manager = ThreadManager(name)
        lock = threading.Lock()
        active = 0
        maximum = 0
        starts = 0

        def target():
            nonlocal active, maximum, starts
            with lock:
                active += 1
                starts += 1
                maximum = max(maximum, active)
            try:
                while True:
                    runtime.wait(0.001)
            finally:
                with lock:
                    active -= 1

        manager.NewThread(target)
        for _ in range(100):
            self.assertTrue(manager.PauseThread(timeout=0.2))
            manager.UnPauseThread()
            self.assertTrue(_eventually(
                lambda: (runtime.status(name) or {}).get("alive", False),
                timeout=0.2,
            ))
        self.assertTrue(manager.KillThread(timeout=1))
        self.assertEqual(maximum, 1)
        self.assertGreaterEqual(starts, 100)


if __name__ == "__main__":
    unittest.main()
