import threading
import time
import unittest
import math

from conf.Hotkeys import Hotkey
from core import runtime
from core.MoveMouse import InputUnavailableError, MoveMouse
from core.MoveMouseLinux import MoveLinuxMouse
from core.SendToClient import SendToClient


def _wait_for(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.002)
    return bool(predicate())


class FakePhysicalBackend:
    def __init__(self):
        self.valid = True
        self.foreground = 41
        self.cursor = (130, 260)
        self.events = []
        self.cancel_on_first_down = False
        self.lose_focus_after_move = False
        self.size = (800, 600)
        self.origin = (100, 200)

    def is_window(self, hwnd):
        return self.valid and hwnd == 41

    def foreground_window(self):
        return self.foreground

    def client_to_screen(self, hwnd, position):
        self.events.append(("client_to_screen", hwnd, position))
        return position[0] + self.origin[0], position[1] + self.origin[1]

    def screen_to_client(self, hwnd, position):
        return position[0] - self.origin[0], position[1] - self.origin[1]

    def cursor_position(self):
        return self.cursor

    def set_cursor_position(self, position):
        self.events.append(("move", position))
        self.cursor = position
        if self.lose_focus_after_move:
            self.foreground = 99

    def key_event(self, key, key_up):
        self.events.append(("key", key, key_up))
        if self.cancel_on_first_down and not key_up:
            self.cancel_on_first_down = False
            runtime.current_token().cancel()

    def mouse_event(self, flags):
        self.events.append(("mouse", flags))

    def client_size(self, hwnd):
        return self.size


class FakeMessageBackend:
    def __init__(self):
        self.valid = True
        self.sent = []
        self.posted = []
        self.fail_down_message = None
        self.size = (800, 600)

    def is_window(self, hwnd):
        return self.valid and hwnd in (41, 42)

    def is_iconic(self, hwnd):
        return False

    def foreground_window(self):
        return 41

    def set_foreground(self, hwnd):
        self.sent.append(("foreground", hwnd))

    def window_rect(self, hwnd):
        return 10, 20, 810, 620

    def client_size(self, hwnd):
        return self.size

    def cursor_position(self):
        return 110, 220

    def screen_to_client(self, hwnd, position):
        return position[0] - 10, position[1] - 20

    def make_long(self, x, y):
        return (x & 0xFFFF) | ((y & 0xFFFF) << 16)

    def send_message(self, hwnd, message, wparam, lparam, timeout_ms):
        self.sent.append((hwnd, message, wparam, lparam, timeout_ms))
        if message == self.fail_down_message:
            self.fail_down_message = None
            raise TimeoutError("simulated hung window")

    def post_message(self, hwnd, message, wparam, lparam):
        self.posted.append((hwnd, message, wparam, lparam))


class FakeLinuxExecutor:
    def __init__(self):
        self.active = "41"
        self.cursor = (-20, 900)
        self.commands = []
        self.active_calls = 0
        self.cancel_on_active_call = None

    def __call__(self, command):
        self.commands.append(tuple(command))
        if command[1] == "getactivewindow":
            self.active_calls += 1
            if self.active_calls == self.cancel_on_active_call:
                runtime.current_token().cancel()
            return self.active + "\n"
        if command[1] == "getmouselocation":
            return f"X={self.cursor[0]}\nY={self.cursor[1]}\n"
        if command[1] == "getwindowgeometry":
            return "X=100\nY=200\nWIDTH=800\nHEIGHT=600\n"
        if command[1] == "mousemove":
            self.cursor = int(command[-2]), int(command[-1])
            return ""
        raise AssertionError(f"unexpected command: {command!r}")


class InputTests(unittest.TestCase):
    def tearDown(self):
        runtime.stop_all(timeout=1)

    def test_every_support_hotkey_dispatches_and_releases_on_both_windows_backends(self):
        from conf.HexMapKeys import KeyToHex
        from modules.support_ui import hotkeys

        for mode in (0, 1):
            for key in hotkeys():
                with self.subTest(mode=mode, key=key):
                    backend = FakeMessageBackend() if mode == 0 else FakePhysicalBackend()
                    cls = SendToClient if mode == 0 else MoveMouse
                    facade = Hotkey(mode, backend=cls(backend=backend, hwnd_resolver=lambda: 41))
                    facade.Press(key)
                    parts = [part.strip() for part in key.split("+")]
                    codes = [KeyToHex[part.lower() if index == 0 and len(parts) == 2 else part]
                             for index, part in enumerate(parts)]
                    expected = [(code, False) for code in codes]
                    expected += [(code, True) for code in reversed(codes)]
                    if mode == 0:
                        actual = [(event[2], event[1] == SendToClient.WM_KEYUP)
                                  for event in backend.sent]
                    else:
                        actual = [(event[1], event[2]) for event in backend.events]
                    self.assertEqual(actual, expected)

    def test_physical_coordinates_are_client_local(self):
        backend = FakePhysicalBackend()
        mouse = MoveMouse(backend=backend, hwnd_resolver=lambda: 41)

        self.assertEqual(mouse.Position(), (30, 60))
        mouse.LeftClick((7, 9))

        self.assertIn(("client_to_screen", 41, (7, 9)), backend.events)
        self.assertIn(("move", (107, 209)), backend.events)
        self.assertEqual(backend.events[-2:], [
            ("mouse", MoveMouse.MOUSEEVENTF_LEFTDOWN),
            ("mouse", MoveMouse.MOUSEEVENTF_LEFTUP),
        ])

    def test_screen_cursor_restore_handles_every_side_and_window_movement(self):
        positions = ((99, 260), (900, 260), (130, 199), (130, 800))
        for position in positions:
            backend = FakePhysicalBackend()
            mouse = MoveMouse(backend=backend, hwnd_resolver=lambda: 41)
            backend.cursor = position
            snapshot = mouse.ScreenPosition()
            backend.origin = (400, 500)
            mouse.RestoreScreenPosition(snapshot)
            self.assertEqual(backend.cursor, position)
            self.assertEqual(
                [event[1] for event in backend.events if event[0] == "move"],
                [position],
            )

    def test_screen_cursor_restore_rechecks_focus_before_moving(self):
        backend = FakePhysicalBackend()
        mouse = MoveMouse(backend=backend, hwnd_resolver=lambda: 41)
        snapshot = mouse.ScreenPosition()
        backend.foreground = 99

        with self.assertRaises(InputUnavailableError):
            mouse.RestoreScreenPosition(snapshot)
        self.assertFalse(any(event[0] == "move" for event in backend.events))

    def test_linux_screen_cursor_restore_uses_absolute_signed_coordinates(self):
        executor = FakeLinuxExecutor()
        mouse = MoveLinuxMouse(executor=executor, window_resolver=lambda: "41")

        snapshot = mouse.ScreenPosition()
        executor.cursor = (300, 400)
        mouse.RestoreScreenPosition(snapshot)

        self.assertEqual(snapshot, (-20, 900))
        self.assertEqual(executor.cursor, snapshot)
        self.assertIn(
            ("xdotool", "mousemove", "--", "-20", "900"),
            executor.commands,
        )

    def test_linux_screen_cursor_restore_rechecks_focus(self):
        executor = FakeLinuxExecutor()
        mouse = MoveLinuxMouse(executor=executor, window_resolver=lambda: "41")
        executor.active = "99"

        with self.assertRaises(InputUnavailableError):
            mouse.RestoreScreenPosition((-20, 900))

        self.assertFalse(any(command[1] == "mousemove" for command in executor.commands))

    def test_linux_screen_cursor_restore_honors_cancellation_before_move(self):
        executor = FakeLinuxExecutor()
        executor.cancel_on_active_call = 2
        mouse = MoveLinuxMouse(executor=executor, window_resolver=lambda: "41")
        token = runtime.CancellationToken("linux-restore", 1)

        with runtime.use_token(token), self.assertRaises(runtime.CancelledError):
            mouse.RestoreScreenPosition((-20, 900))

        self.assertFalse(any(command[1] == "mousemove" for command in executor.commands))

    def test_physical_input_rejects_unfocused_window_without_side_effects(self):
        backend = FakePhysicalBackend()
        backend.foreground = 99
        mouse = MoveMouse(backend=backend, hwnd_resolver=lambda: 41)

        with self.assertRaises(InputUnavailableError):
            mouse.Press("F1")
        self.assertEqual(backend.events, [])

    def test_physical_readiness_waits_without_sending_input(self):
        backend = FakePhysicalBackend()
        backend.foreground = 99
        hotkey = Hotkey(
            1, backend=MoveMouse(backend=backend, hwnd_resolver=lambda: 41)
        )
        waiting = threading.Event()
        ready = threading.Event()
        name = "readiness-focus"

        def target():
            waiting.set()
            hotkey.wait_until_ready(timeout=1)
            ready.set()
            runtime.wait(30)

        thread = runtime.start(name, target)
        self.assertTrue(waiting.wait(1))
        self.assertTrue(_wait_for(
            lambda: runtime.status(name)["phase"] == "Waiting for game focus"
        ))
        self.assertEqual(backend.events, [])

        backend.foreground = 41
        self.assertTrue(ready.wait(1))
        self.assertEqual(runtime.status(name)["phase"], "Running")
        self.assertEqual(backend.events, [])
        self.assertTrue(runtime.cancel(name, timeout=1))
        thread.join(1)

    def test_physical_readiness_timeout_and_cancel_send_no_input(self):
        backend = FakePhysicalBackend()
        backend.foreground = 99
        hotkey = Hotkey(
            1, backend=MoveMouse(backend=backend, hwnd_resolver=lambda: 41)
        )

        with self.assertRaises(TimeoutError):
            hotkey.wait_until_ready(timeout=0.01)

        name = "readiness-cancel"
        thread = runtime.start(name, lambda: hotkey.wait_until_ready(timeout=30))
        self.assertTrue(_wait_for(
            lambda: runtime.status(name)["phase"] == "Waiting for game focus"
        ))
        self.assertTrue(runtime.cancel(name, timeout=1))
        thread.join(1)
        self.assertEqual(backend.events, [])

    def test_message_readiness_only_validates_target(self):
        backend = FakeMessageBackend()
        hotkey = Hotkey(
            0, backend=SendToClient(backend=backend, hwnd_resolver=lambda: 41)
        )

        self.assertTrue(hotkey.wait_until_ready(timeout=0))
        self.assertEqual(backend.sent, [])
        self.assertEqual(backend.posted, [])

    def test_readiness_rejects_nonfinite_timeout(self):
        hotkey = Hotkey(1, backend=object())
        for timeout in (math.inf, -math.inf, math.nan):
            with self.assertRaises(ValueError):
                hotkey.wait_until_ready(timeout)

    def test_key_and_modifier_names_are_validated_before_input(self):
        backend = FakePhysicalBackend()
        mouse = MoveMouse(backend=backend, hwnd_resolver=lambda: 41)

        with self.assertRaises(ValueError):
            mouse.Press("not-a-key")
        with self.assertRaises(ValueError):
            mouse.PressHotkey("F1", "F2")
        self.assertEqual(backend.events, [])

    def test_physical_resize_rejects_keys_before_any_input(self):
        backend = FakePhysicalBackend()
        mouse = MoveMouse(
            backend=backend,
            hwnd_resolver=lambda: 41,
            expected_size_resolver=lambda: (800, 600),
        )
        backend.size = (801, 600)

        with self.assertRaises(InputUnavailableError):
            mouse.Press("F1")
        self.assertEqual(backend.events, [])

    def test_physical_modifier_is_released_on_cancellation(self):
        backend = FakePhysicalBackend()
        backend.cancel_on_first_down = True
        mouse = MoveMouse(backend=backend, hwnd_resolver=lambda: 41)
        name = "cancel-input"

        thread = runtime.start(name, lambda: mouse.PressHotkey("ctrl", "F1"))
        thread.join(1)

        self.assertEqual(backend.events, [
            ("key", 0x11, False),
            ("key", 0x11, True),
        ])

    def test_physical_drag_validates_destination_before_any_input(self):
        backend = FakePhysicalBackend()
        mouse = MoveMouse(backend=backend, hwnd_resolver=lambda: 41)

        with self.assertRaises(ValueError):
            mouse.DragTo((1, 2), (900, 2))
        self.assertEqual(backend.events, [])

    def test_physical_click_rechecks_focus_before_button_down(self):
        backend = FakePhysicalBackend()
        backend.lose_focus_after_move = True
        mouse = MoveMouse(backend=backend, hwnd_resolver=lambda: 41)

        with self.assertRaises(InputUnavailableError):
            mouse.LeftClick((7, 9))
        self.assertFalse(any(event[0] == "mouse" for event in backend.events))

    def test_message_click_uses_local_coordinates_and_bounded_send(self):
        backend = FakeMessageBackend()
        client = SendToClient(
            backend=backend, hwnd_resolver=lambda: 41, timeout_ms=123
        )
        client.LeftClick((7, 9))
        packed = backend.make_long(7, 9)

        self.assertEqual(backend.posted[0][3], packed)
        self.assertEqual(backend.sent[0],
                         (41, client.WM_LBUTTONDOWN, client.MK_LBUTTON, packed, 123))
        self.assertEqual(backend.sent[1],
                         (41, client.WM_LBUTTONUP, 0, packed, 123))

    def test_message_button_release_is_attempted_after_down_timeout(self):
        backend = FakeMessageBackend()
        client = SendToClient(backend=backend, hwnd_resolver=lambda: 41)
        backend.fail_down_message = client.WM_LBUTTONDOWN

        with self.assertRaises(TimeoutError):
            client.LeftClick((1, 2))

        self.assertEqual([entry[1] for entry in backend.sent], [
            client.WM_LBUTTONDOWN, client.WM_LBUTTONUP
        ])

    def test_message_drag_validates_destination_before_any_input(self):
        backend = FakeMessageBackend()
        client = SendToClient(backend=backend, hwnd_resolver=lambda: 41)

        for destination in ((800, 2), (-1, 2), (True, 2), (1.5, 2)):
            with self.assertRaises(ValueError):
                client.DragTo((1, 2), destination)
        self.assertEqual(backend.posted, [])
        self.assertEqual(backend.sent, [])

    def test_message_resize_rejects_keys_before_any_input(self):
        backend = FakeMessageBackend()
        client = SendToClient(
            backend=backend,
            hwnd_resolver=lambda: 41,
            expected_size_resolver=lambda: [800, 600],
        )
        backend.size = (800, 599)

        with self.assertRaises(InputUnavailableError):
            client.Press("F1")
        self.assertEqual(backend.posted, [])
        self.assertEqual(backend.sent, [])

    def test_hwnd_is_resolved_for_each_action(self):
        backend = FakeMessageBackend()
        handles = iter((41, 42))
        client = SendToClient(backend=backend, hwnd_resolver=lambda: next(handles))

        client.Press("F1")
        client.Press("F2")

        self.assertEqual([entry[0] for entry in backend.sent], [41, 41, 42, 42])

    def test_hotkey_facade_parses_and_serializes_whole_actions(self):
        class Backend:
            def __init__(self):
                self.calls = []
                self.active = 0
                self.maximum = 0
                self.lock = threading.Lock()

            def PressHotkey(self, modifier, key):
                with self.lock:
                    self.active += 1
                    self.maximum = max(self.maximum, self.active)
                try:
                    self.calls.append((modifier, key))
                    time.sleep(0.02)
                finally:
                    with self.lock:
                        self.active -= 1

        backend = Backend()
        hotkey = Hotkey(1, backend=backend)
        threads = [
            threading.Thread(target=hotkey.Press, args=("Ctrl + F1",)),
            threading.Thread(target=hotkey.Press, args=("Shift + F2",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(1)

        self.assertEqual(backend.maximum, 1)
        self.assertCountEqual(backend.calls, [("ctrl", "F1"), ("shift", "F2")])


if __name__ == "__main__":
    unittest.main()
