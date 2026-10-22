"""One-shot experimental input probes, deliberately separate from bot setup.

The caller must explicitly arm each invocation. A spawn worker imports/builds
the exact repository input facade, announces readiness, then waits for a single
parent command. There is no retry, text-entry API, focus change, or game loop.
Dispatch success is never evidence that the game accepted the input.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import math
import multiprocessing
from numbers import Integral, Real
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import uuid


_LEASE_BACKEND = "frame_lease_region_gdi_overlay_aware"
_ERROR_ONLY_BACKEND = "error_only_physical"
BACKENDS = frozenset({"physical", "guarded_physical", "guarded_physical_overlay_aware",
                      "guarded_region_gdi_overlay_aware", _LEASE_BACKEND, "messages", _ERROR_ONLY_BACKEND})
_GUARDED = frozenset({"guarded_physical", "guarded_physical_overlay_aware",
                      "guarded_region_gdi_overlay_aware", _LEASE_BACKEND})
CAPTURE_METHODS = frozenset({"visible_screen", "visible_screen_overlay_aware",
                            "visible_region_gdi_overlay_aware", "direct_visible_region_gdi"})
OPERATIONS = frozenset({"ctrl_o", "escape", "left_click", "right_click", "move", "readiness"})
_MOUSE = frozenset({"left_click", "right_click", "move"})
_POLL = 0.025
_MAX_ERROR = 2048
_SETTLE_SECONDS = 0.35
_quarantine_lock = threading.RLock()
_quarantined = set()


def _detail(exc):
    return f"{type(exc).__name__}: {exc}"[:_MAX_ERROR]


def _timeout(value, name):
    if isinstance(value, bool) or not isinstance(value, Real):
        raise TypeError(f"{name} must be a real number")
    value = float(value)
    if not math.isfinite(value) or not 0 < value <= 60:
        raise ValueError(f"{name} must be finite, positive, and at most 60 seconds")
    return value


def _point(value):
    if (not isinstance(value, (tuple, list)) or len(value) != 2
            or any(isinstance(v, bool) or not isinstance(v, Integral) for v in value)):
        raise ValueError("point must contain two integral host-client coordinates")
    point = tuple(int(v) for v in value)
    if any(v < 0 or v > 32767 for v in point):
        raise ValueError("point is outside the supported host-client coordinate bounds")
    return point


def _expected_size(value):
    if value is None:
        return None
    if (not isinstance(value, (tuple, list)) or len(value) != 2
            or any(isinstance(v, bool) or not isinstance(v, Integral) for v in value)
            or min(value) <= 0 or max(value) > 16384 or value[0] * value[1] > 33_554_432):
        raise ValueError("expected_size must contain two bounded positive integral dimensions")
    return tuple(int(v) for v in value)


def _point_patch(value, point, expected_size=None):
    if value is None:
        return None
    if point is None:
        raise ValueError("point_patch requires a selected mouse point")
    if not isinstance(value, Mapping) or set(value) != {"box", "sha256_rgb"}:
        raise ValueError("point_patch must contain exactly box and sha256_rgb")
    box, digest = value["box"], value["sha256_rgb"]
    if (not isinstance(box, (tuple, list)) or len(box) != 4
            or any(isinstance(v, bool) or not isinstance(v, Integral) for v in box)):
        raise ValueError("point_patch.box must contain four integral coordinates")
    left, top, right, bottom = (int(v) for v in box)
    if (not 0 <= left < right <= 16384 or not 0 <= top < bottom <= 16384
            or (right - left) * (bottom - top) > 65_536):
        raise ValueError("point_patch.box is outside the supported pixel bounds")
    if not (left <= point[0] < right and top <= point[1] < bottom):
        raise ValueError("point_patch.box must contain the selected host-client point")
    if expected_size is not None and (right > expected_size[0] or bottom > expected_size[1]):
        raise ValueError("point_patch.box is outside expected_size")
    if (not isinstance(digest, str) or len(digest) != 64
            or any(c not in "0123456789abcdefABCDEF" for c in digest)):
        raise ValueError("point_patch.sha256_rgb must be a SHA-256 hexadecimal digest")
    return {"box": [left, top, right, bottom], "sha256_rgb": digest.lower()}


def _check_point_patch(image, patch):
    """Compare exactly the displayed RGB patch, without selecting UI meaning."""
    box = patch["box"]
    expected = patch["sha256_rgb"]
    result = {"status": "changed", "box": list(box), "expected_sha256_rgb": expected,
              "actual_sha256_rgb": None, "error": None}
    if box[2] > image.width or box[3] > image.height:
        result["error"] = "selected point patch is outside the fresh host-client capture"
        return result
    actual = hashlib.sha256(image.convert("RGB").crop(box).tobytes()).hexdigest()
    result["actual_sha256_rgb"] = actual
    if actual == expected:
        result["status"] = "matched"
    else:
        result["error"] = "selected point patch changed after preview; select a fresh harmless control"
    return result


def _process_observation(process):
    try:
        pid = process.pid
    except (AttributeError, ValueError):
        pid = None
    try:
        alive = bool(process.is_alive())
    except BaseException:
        alive = None
    return {"pid": pid, "alive": alive, "reaped": False}


def _reject_quarantined():
    """Keep a strong reference until a dead worker is successfully joined."""
    blocked = []
    with _quarantine_lock:
        for process in tuple(_quarantined):
            observation = _process_observation(process)
            if observation["alive"] is False:
                try:
                    process.join(0)
                    if process.is_alive():
                        raise RuntimeError("worker still alive after join")
                    process.close()
                except BaseException as exc:
                    observation["error"] = _detail(exc)
                else:
                    _quarantined.discard(process)
                    continue
            blocked.append(observation)
    return blocked


def _mapped_point(backend, target, point):
    """Input points are always selected on the full host-client screenshot."""
    if (point is None or target is None or target.kind != "sandbox"
            or backend not in _GUARDED | {_ERROR_ONLY_BACKEND}):
        return point
    left, top, right, bottom = target.viewport
    if not (left <= point[0] < right and top <= point[1] < bottom):
        raise ValueError("selected host-client point is outside the calibrated viewport")
    return point[0] - left, point[1] - top


class _TrackedPhysical:
    """Report possibly-held controls *before* the native DOWN can run."""

    def __init__(self, native, emit, key_hold_ms=0, trace_key_target=False):
        self.native, self.emit = native, emit
        self.key_hold_ms, self.key_down_times = key_hold_ms, {}
        self.trace_key_target = trace_key_target

    def __getattr__(self, name):
        return getattr(self.native, name)

    def key_event(self, key, key_up):
        if self.trace_key_target:
            try:
                self.emit("key_dispatch_target", key=int(key), key_up=bool(key_up),
                          foreground_hwnd=int(self.native.foreground_window()))
            except Exception as exc:
                self.emit("key_dispatch_target", key=int(key), key_up=bool(key_up),
                          diagnostic_error=_detail(exc))
        if not key_up:
            self.emit("held", kind="key", code=int(key), held=True)
            if self.key_hold_ms:
                self.key_down_times[key] = time.monotonic()
        if key_up and self.key_hold_ms and key in self.key_down_times:
            remaining = self.key_hold_ms / 1000 - (time.monotonic() - self.key_down_times.pop(key))
            try:
                if remaining > 0:
                    time.sleep(remaining)
            finally:
                self.native.key_event(key, key_up)
        else:
            self.native.key_event(key, key_up)
        if key_up:
            self.emit("held", kind="key", code=int(key), held=False)

    def mouse_event(self, flags):
        pairs = {2: (2, True), 4: (2, False), 8: (8, True), 16: (8, False)}
        code, down = pairs[flags]
        if down:
            self.emit("held", kind="button", code=code, held=True)
        self.native.mouse_event(flags)
        if not down:
            self.emit("held", kind="button", code=code, held=False)


class _ForegroundMessages:
    """Use SendToClient unchanged, but forbid its background delivery here."""

    def __init__(self, native, hwnd, emit):
        self.native, self.hwnd, self.emit = native, hwnd, emit

    def __getattr__(self, name):
        return getattr(self.native, name)

    def _foreground(self):
        if (not self.native.is_window(self.hwnd) or self.native.is_iconic(self.hwnd)
                or self.native.foreground_window() != self.hwnd):
            raise RuntimeError("message probe requires the selected window to remain foreground")

    def send_message(self, hwnd, message, wparam, lparam, timeout_ms=250):
        releases = {0x0101: ("key", int(wparam)), 0x0202: ("button", 2),
                    0x0205: ("button", 8)}
        downs = {0x0100: ("key", int(wparam)), 0x0201: ("button", 2),
                 0x0204: ("button", 8)}
        if message not in releases:
            self._foreground()
        if message in downs:
            kind, code = downs[message]
            self.emit("held", kind=kind, code=code, held=True)
        result = self.native.send_message(hwnd, message, wparam, lparam, timeout_ms)
        if message in releases:
            kind, code = releases[message]
            self.emit("held", kind=kind, code=code, held=False)
        return result

    def post_message(self, hwnd, message, wparam, lparam):
        self._foreground()
        return self.native.post_message(hwnd, message, wparam, lparam)


def _check_ready(check, emit):
    emit("guard_start")
    try:
        result = check()
    except BaseException as exc:
        emit("guard_complete", ready=False, error=_detail(exc))
        raise
    emit("guard_complete", ready=bool(result))
    return result


def _build_facade(spec, emit):
    # Native imports and native adapter construction run only in the child.
    from core.MoveMouse import MoveMouse, _WindowsPhysicalBackend
    from tibia_client.window_target import TargetInput, WindowTarget, _native_backend

    hwnd = spec["hwnd"]
    data = spec["target"]
    target = None if data is None else WindowTarget.from_config({"window_target": data})
    geometry = {"size": spec["expected_size"]}
    error_diagnostics = []

    def diagnose(hwnd, error):
        from tibia_client.direct_capture import diagnose_client_availability

        detail = diagnose_client_availability(hwnd)
        error_diagnostics.append(detail)
        emit("availability_diagnostic", source="input_error", error=_detail(error), result=detail)
        return detail

    lease_state = {"lease": None, "intent": None}
    template = threshold = template_provenance = None
    if target is not None and target.kind == "sandbox":
        geometry["size"] = target.host_size
    if spec["backend"] == _LEASE_BACKEND:
        from tibia_client.probe_known_dialog import DEFAULT_THRESHOLD, load_template_report

        template, threshold, template_provenance = load_template_report(spec["known_dialog_report"])
        if (template.name != "open_channel" or threshold != DEFAULT_THRESHOLD
                or template.source_size != target.host_size or template.viewport != target.viewport):
            raise ValueError("frame lease requires the frozen Open Channel template matching target size and viewport")
        emit("known_dialog_config", provenance=template_provenance, template=template.metadata(),
             threshold=threshold)
    if spec["backend"] == "messages":
        from core.SendToClient import SendToClient, _WindowsMessageBackend
        native = _ForegroundMessages(_WindowsMessageBackend(), hwnd, emit)
        engine = SendToClient(backend=native, hwnd_resolver=lambda: hwnd,
                              expected_size_resolver=lambda: geometry["size"], preflight=True)
    else:
        native = _TrackedPhysical(_WindowsPhysicalBackend(), emit,
                                  trace_key_target=spec.get("trace_key_target", False))
        physical = MoveMouse(backend=native, hwnd_resolver=lambda: hwnd,
                             expected_size_resolver=lambda: geometry["size"],
                             key_hold_ms=spec.get("key_hold_ms", 0),
                             preflight=spec["backend"] != _ERROR_ONLY_BACKEND,
                             error_diagnostic=diagnose if spec["backend"] == _ERROR_ONLY_BACKEND else None)
        engine = physical
        if spec["backend"] == _ERROR_ONLY_BACKEND and target is not None:
            engine = TargetInput(physical, target, lambda: hwnd, preflight=False)
        if spec["backend"] in _GUARDED:
            facade_type = TargetInput
            if spec["backend"] == _LEASE_BACKEND:
                class LeasedTargetInput(TargetInput):
                    def check_ready(self):
                        if lease_state["lease"] is None:
                            raise RuntimeError("no active one-operation frame lease")
                        return lease_state["lease"].check(lease_state["intent"])

                facade_type = LeasedTargetInput
            elif spec["backend"] in {"guarded_physical_overlay_aware", "guarded_region_gdi_overlay_aware"}:
                if spec["backend"] == "guarded_region_gdi_overlay_aware":
                    from tibia_client.probe_region_gdi import capture_client_region_gdi_overlay_aware
                    guard_capture = capture_client_region_gdi_overlay_aware
                else:
                    from tibia_client.probe_overlay import capture_client_overlay_aware
                    guard_capture = capture_client_overlay_aware
                from tibia_client.window_target import check_focus_guard

                class OverlayAwareTargetInput(TargetInput):
                    def check_ready(self):
                        if self.target.kind == "sandbox":
                            image = guard_capture(
                                self._hwnd_resolver(), self.target.host_size)
                            check_focus_guard(image, self.target)
                            return True
                        return super().check_ready()

                facade_type = OverlayAwareTargetInput
            engine = facade_type(physical, target, lambda: hwnd, preflight=True)
            original_check = engine.check_ready

            def guard():
                return _check_ready(original_check, emit)

            # Instrument every original guard call, including TargetInput's
            # outer dispatch and MoveMouse's checks between compound phases.
            engine.check_ready = guard
            physical.readiness_guard = guard
    return SimpleNamespace(engine=engine, target=target, geometry=geometry,
                           pixels=_native_backend().physical_pixels, lease_state=lease_state,
                           template=template, threshold=threshold,
                           template_provenance=template_provenance, error_diagnostics=error_diagnostics)


def _run_error_only(bundle, spec, emit):
    """Dispatch first; acquire optional visual evidence only after the action."""
    point = _mapped_point(spec["backend"], bundle.target, spec["point"])
    try:
        with bundle.pixels():
            result = _perform_input(bundle.engine, spec["operation"], point, emit)
    except Exception as exc:
        result = {"error": _detail(exc), "cursor_restore": {"status": "not_attempted"}}
    diagnostic = (bundle.error_diagnostics[-1] if bundle.error_diagnostics
                  else getattr(bundle.engine, "last_error_diagnostic", None))
    capture_error = None
    try:
        from tibia_client.direct_capture import capture_direct_host_client

        if result["error"] is None:
            emit("settle_start", requested_ms=_SETTLE_SECONDS * 1000)
            time.sleep(_SETTLE_SECONDS)
            emit("settle_complete")
        emit("after_capture_start")
        after = capture_direct_host_client(spec["hwnd"], bundle.geometry["size"])
        after.save(spec["after_path"])
        emit("after_capture_complete", path=spec["after_path"], size=list(after.size))
    except Exception as exc:
        capture_error = _detail(exc)
        emit("after_capture_failed", error=capture_error)
        diagnostic = getattr(exc, "client_availability", diagnostic)
        if getattr(exc, "client_availability", None) is not None:
            bundle.error_diagnostics.append(exc.client_availability)
            emit("availability_diagnostic", source="capture_error", error=capture_error,
                 result=exc.client_availability)
    restore_failed = result.get("cursor_restore", {}).get("status") == "failed"
    return {**result, "status": "failed" if result["error"] or capture_error or restore_failed else "completed",
            "capture_error": capture_error, "availability_policy": "on_error",
            "client_availability": diagnostic, "availability_diagnostics": list(bundle.error_diagnostics),
            "pre_action_capture": False}


def _run_frame_lease(bundle, spec, emit):
    """One fresh frame, then bounded native checks; no recapture inside input."""
    from tibia_client.probe_frame_lease import FrameLease, ProbeIntent
    from tibia_client.probe_known_dialog import evaluate_image
    from tibia_client.probe_lease_native import bind
    from tibia_client.probe_region_gdi import capture_client_region_gdi_overlay_aware
    from tibia_client.window_target import check_focus_guard

    before = lease = checker = None
    dispatch_seen = dispatch_completed = False
    result = {"error": None, "capture_error": None, "frame_lease": None,
              "frame_assessment": None, "known_dialog": None, "cheap_checks": None,
              "known_dialog_config": bundle.template_provenance}
    readiness = spec["operation"] == "readiness"

    def action_emit(stage, **fields):
        nonlocal dispatch_seen, dispatch_completed
        dispatch_seen = dispatch_seen or stage == "dispatch_start"
        dispatch_completed = dispatch_completed or stage == "dispatch_complete"
        emit(stage, **fields)

    try:
        target = bundle.target
        host_point = spec["point"]
        checker = bind(spec["hwnd"], target.host_size, point=host_point,
                       regions=[target.viewport, target.focus_guard["region"]])
        emit("before_capture_start")
        capture_started = time.monotonic()
        before = capture_client_region_gdi_overlay_aware(spec["hwnd"], target.host_size)
        capture_completed = time.monotonic()
        before = before.convert("RGB").copy()
        emit("before_capture_complete", path=None, size=list(before.size),
             capture_started_at=capture_started, capture_completed_at=capture_completed,
             persistence="deferred until after action or refusal")
        checker.recheck()
        focus_error = None
        try:
            check_focus_guard(before, target)
            focus_verified = True
        except Exception as exc:
            focus_verified, focus_error = False, _detail(exc)
        emit("known_dialog_start")
        known = evaluate_image(bundle.template, before, threshold=bundle.threshold)
        result["known_dialog"] = known
        emit("known_dialog_complete", result=known)
        patch_verified = None
        if host_point is not None:
            emit("point_patch_start")
            patch_result = _check_point_patch(before, spec["point_patch"])
            patch_verified = patch_result["status"] == "matched"
            emit("point_patch_complete", result=patch_result)
        frame_hash = hashlib.sha256(before.tobytes()).hexdigest()
        result["frame_assessment"] = {
            "frame_rgb_sha256": frame_hash, "focus_verified": focus_verified,
            "focus_error": focus_error, "known_state": known["verdict"],
            "point_patch_verified": patch_verified, "same_frame": True,
            "visual_assessment": "before_action_only",
            "guest_changes_between_input_phases": "unverified"}
        intent = ProbeIntent(spec["hwnd"], target.fingerprint, spec["capture_method"],
                             spec["backend"], spec["operation"], host_point,
                             spec.get("expected_dialog"))
        lease = FrameLease(intent, capture_started_at=capture_started,
                           capture_completed_at=capture_completed, frame_rgb_sha256=frame_hash,
                           focus_verified=focus_verified, known_state=known["verdict"],
                           point_patch_verified=patch_verified, cheap_check=checker.recheck,
                           known_dialog_name=(bundle.template.name if known["verdict"] == "blocked_known_dialog" else None),
                           clock=time.monotonic)
        bundle.lease_state.update(lease=lease, intent=intent)
        with bundle.pixels():
            with lease.action(intent):
                if readiness:
                    emit("readiness_start")
                    bundle.engine.check_ready()
                    emit("readiness_complete", ready=True, error=None)
                else:
                    result.update(_perform_input(bundle.engine, spec["operation"],
                                                  _mapped_point(spec["backend"], target, host_point),
                                                  action_emit))
    except Exception as exc:
        result["error"] = _detail(exc)
        if readiness:
            emit("readiness_complete", ready=False, error=result["error"])
    finally:
        bundle.lease_state.update(lease=None, intent=None)
        if lease is not None:
            result["frame_lease"] = lease.metadata()
            emit("frame_lease_complete", result=result["frame_lease"])
        if checker is not None:
            result["cheap_checks"] = {"baseline": checker.baseline,
                                      "checks": list(checker.telemetry)}
        if before is not None:
            emit("before_persist_start")
            try:
                before.save(spec["before_path"])
                emit("before_capture_persisted", path=spec["before_path"], size=list(before.size))
            except Exception as exc:
                result["capture_error"] = _detail(exc)
                emit("before_persist_failed", error=result["capture_error"])
    if dispatch_seen:
        try:
            if dispatch_completed:
                emit("settle_start", requested_ms=_SETTLE_SECONDS * 1000)
                time.sleep(_SETTLE_SECONDS)
                emit("settle_complete")
            emit("after_capture_start")
            after = capture_client_region_gdi_overlay_aware(spec["hwnd"], bundle.target.host_size)
            after.save(spec["after_path"])
            emit("after_capture_complete", path=spec["after_path"], size=list(after.size))
        except Exception as exc:
            result["capture_error"] = _detail(exc)
            emit("after_capture_failed", error=result["capture_error"])
    result["status"] = "failed" if result["error"] or result["capture_error"] else "completed"
    if readiness:
        result["status"] = "completed"
        result["readiness"] = {"ready": result["error"] is None, "error": result["error"]}
    return result


def _perform_input(engine, operation, point, emit, *, restore_cursor=True):
    """Small injectable dispatcher used by the worker and headless tests."""
    if operation not in OPERATIONS - {"readiness"}:
        raise ValueError("unsupported one-shot input operation")
    if operation in _MOUSE:
        point = _point(point)
    restoration = {"status": "not_applicable", "original": None, "error": None}
    destination = {"status": "not_applicable", "screen_position": None, "error": None,
                   "evidence": "cursor position does not prove game acceptance"}
    original = None
    if operation in _MOUSE and restore_cursor:
        emit("cursor_snapshot_start")
        original = engine.ScreenPosition()
        restoration.update(status="pending", original=list(original))
        emit("cursor_snapshot_complete", position=list(original))
    dispatch_error = None
    try:
        emit("dispatch_start")
        if operation == "ctrl_o":
            engine.PressHotkey("ctrl", "o")
        elif operation == "escape":
            engine.Press("esc")
        elif operation == "left_click":
            engine.LeftClick(point)
        elif operation == "right_click":
            engine.RightClick(point)
        else:
            engine.MoveTo(*point)
        emit("dispatch_complete")
        if operation in _MOUSE and restore_cursor:
            emit("cursor_destination_start")
            try:
                destination["screen_position"] = list(engine.ScreenPosition())
                destination["status"] = "completed"
                emit("cursor_destination_complete", position=destination["screen_position"])
            except BaseException as exc:
                destination.update(status="failed", error=_detail(exc))
                emit("cursor_destination_failed", error=destination["error"])
    except BaseException as exc:
        dispatch_error = _detail(exc)
        emit("dispatch_failed", error=dispatch_error)
    finally:
        if original is not None:
            emit("cursor_restore_start")
            try:
                engine.RestoreScreenPosition(original)
                restoration["status"] = "completed"
                emit("cursor_restore_complete")
            except BaseException as exc:
                restoration.update(status="failed", error=_detail(exc))
                emit("cursor_restore_failed", error=restoration["error"])
    return {"error": dispatch_error, "cursor_restore": restoration,
            "cursor_destination": destination}


def _worker(connection, spec):
    started = time.monotonic()

    def emit(stage, **data):
        connection.send({"type": "event", "stage": stage,
                         "worker_elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                         **data})

    try:
        target_data = spec.get("target")
        target_fingerprint = None
        if target_data is not None:
            payload = json.dumps(target_data, sort_keys=True, separators=(",", ":"))
            target_fingerprint = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        emit("calibrated_target", window_target=target_data,
             target_fingerprint=target_fingerprint)
        emit("backend_build_start")
        bundle = _build_facade(spec, emit)
        emit("backend_build_complete")
        connection.send({"type": "ready"})
        # Never capture or dispatch before the explicit parent command.
        if connection.recv() != "run_once":
            return
        if spec.get("backend") == _ERROR_ONLY_BACKEND:
            connection.send({"type": "result", **_run_error_only(bundle, spec, emit)})
            return
        if spec.get("backend") == _LEASE_BACKEND:
            connection.send({"type": "result", **_run_frame_lease(bundle, spec, emit)})
            return
        if spec["operation"] == "readiness":
            emit("readiness_start")
            error = None
            try:
                ready = bool(bundle.engine.check_ready())
            except BaseException as exc:
                ready, error = False, _detail(exc)
            emit("readiness_complete", ready=ready, error=error)
            connection.send({"type": "result", "status": "completed",
                             "readiness": {"ready": ready, "error": error}})
            return

        if spec.get("capture_method", "visible_screen") == "visible_region_gdi_overlay_aware":
            from tibia_client.probe_region_gdi import capture_client_region_gdi_overlay_aware
            capture_client = capture_client_region_gdi_overlay_aware
        elif spec.get("capture_method", "visible_screen") == "visible_screen_overlay_aware":
            from tibia_client.probe_overlay import capture_client_overlay_aware
            capture_client = capture_client_overlay_aware
        else:
            from tibia_client.window_target import capture_host_client
            capture_client = capture_host_client
        emit("before_capture_start")
        before = capture_client(spec["hwnd"], bundle.geometry["size"])
        bundle.geometry["size"] = tuple(before.size)
        before.save(spec["before_path"])
        emit("before_capture_complete", path=spec["before_path"], size=list(before.size))
        point = spec["point"]
        if point is not None and not (point[0] < before.width and point[1] < before.height):
            raise ValueError("selected point is outside the current host client capture")
        if spec.get("point_patch") is not None:
            emit("point_patch_start")
            patch_result = _check_point_patch(before, spec["point_patch"])
            emit("point_patch_complete", result=patch_result)
            if patch_result["status"] != "matched":
                raise ValueError(patch_result["error"])
        point = _mapped_point(spec["backend"], bundle.target, point)
        with bundle.pixels():
            result = _perform_input(bundle.engine, spec["operation"], point, emit,
                                    restore_cursor=spec["backend"] != "messages")
        capture_error = None
        try:
            if result["error"] is None:
                emit("settle_start", requested_ms=_SETTLE_SECONDS * 1000)
                time.sleep(_SETTLE_SECONDS)
                emit("settle_complete")
            emit("after_capture_start")
            after = capture_client(spec["hwnd"], bundle.geometry["size"])
            after.save(spec["after_path"])
            emit("after_capture_complete", path=spec["after_path"], size=list(after.size))
        except BaseException as exc:
            capture_error = _detail(exc)
            emit("after_capture_failed", error=capture_error)
        connection.send({"type": "result", "status": "failed" if result["error"] else "completed",
                         **result, "capture_error": capture_error})
    except BaseException as exc:
        try:
            connection.send({"type": "result", "status": "failed", "error": _detail(exc)})
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        connection.close()


def _release_key_parameter(code, map_virtual_key):
    scan = int(map_virtual_key(code, 0)) & 0xFF
    return 1 | (scan << 16) | (1 << 30) | (1 << 31)


def _cleanup_held(hwnd, backend, held, point):
    """Best-effort targeted UPs, never focus or cursor movement.

    Only controls with a worker DOWN-attempt event and no confirmed UP qualify.
    Native physical releases are nonblocking; messages have a 250 ms deadline.
    """
    results = []
    if not held:
        return {"attempted": False, "results": results}
    import ctypes
    from ctypes import wintypes

    api = ctypes.WinDLL("user32", use_last_error=True)
    if backend == "messages":
        send = api.SendMessageTimeoutW
        send.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t,
                         wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
        send.restype = ctypes.c_ssize_t
        api.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
        api.MapVirtualKeyW.restype = wintypes.UINT
    else:
        api.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]
        api.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.DWORD, ctypes.c_size_t]
    # Release the hotkey's ordinary key before its Ctrl modifier, as the exact
    # facade does on its normal finally path.
    for kind, code in sorted(held, key=lambda item: (item[0], item[1] == 0x11, item[1])):
        # The fixed operation allowlist bounds this further; guard again here.
        if (kind == "key" and code not in {0x11, 0x4F, 0x1B}) or (
                kind == "button" and code not in {2, 8}):
            continue
        item = {"kind": kind, "code": code, "status": "completed", "error": None}
        try:
            if backend == "messages":
                message = 0x0101 if kind == "key" else (0x0202 if code == 2 else 0x0205)
                parameter = (_release_key_parameter(code, api.MapVirtualKeyW) if kind == "key"
                             else ((point[0] & 0xFFFF) | ((point[1] & 0xFFFF) << 16)))
                response = ctypes.c_size_t()
                if not send(hwnd, message, code if kind == "key" else 0, parameter,
                            2, 250, ctypes.byref(response)):
                    raise OSError("bounded release message failed or timed out")
            elif kind == "key":
                api.keybd_event(code, 0, 2, 0)
            else:
                api.mouse_event(4 if code == 2 else 16, 0, 0, 0, 0)
        except BaseException as exc:
            item.update(status="failed", error=_detail(exc))
        results.append(item)
    return {"attempted": bool(results), "results": results}


def _stop_and_reap(process):
    if process.is_alive():
        process.terminate()
    process.join(0.35)
    if process.is_alive():
        process.kill()
        process.join(0.35)
    return not process.is_alive()


def _timing_summary(events):
    """Derive timings from stage evidence, never turn an unfinished phase into 0."""
    def difference(start, end):
        field = ("worker_elapsed_ms" if "worker_elapsed_ms" in start
                 and "worker_elapsed_ms" in end else "elapsed_ms")
        return round(max(0.0, float(end[field]) - float(start[field])), 3)

    def phase(start_name, end_names):
        start = None
        for item in events:
            if item["stage"] == start_name:
                start = item
            elif item["stage"] in end_names and start is not None:
                return difference(start, item)
        return None

    pending, completed_guard_ms, guard_calls = [], 0.0, 0
    for item in events:
        if item["stage"] == "guard_start":
            guard_calls += 1
            pending.append(item)
        elif item["stage"] == "guard_complete" and pending:
            completed_guard_ms += difference(pending.pop(), item)
    startup = next((item["elapsed_ms"] for item in events
                    if item["stage"] == "worker_ready"), None)
    return {"startup_wall_ms": startup,
            "dispatch_ms": phase("dispatch_start", {"dispatch_complete", "dispatch_failed"}),
            "before_capture_ms": phase("before_capture_start", {"before_capture_complete"}),
            "after_capture_ms": phase("after_capture_start", {"after_capture_complete",
                                                            "after_capture_failed"}),
            "readiness_ms": phase("readiness_start", {"readiness_complete"}),
            "settle_ms": phase("settle_start", {"settle_complete"}),
            "guard_calls": guard_calls, "guard_incomplete_calls": len(pending),
            "total_guard_ms": None if pending else round(completed_guard_ms, 3)}


def run_input_probe(hwnd, backend, operation, output_dir, *, target=None, point=None,
                    token=None, startup_timeout=15, action_timeout=10, expected_size=None,
                    point_patch=None, capture_method="visible_screen", known_dialog_report=None,
                    expected_dialog=None, key_hold_ms=0, trace_key_target=False):
    """Run exactly one explicitly armed probe and return JSON-safe evidence.

    ``point`` is a current full host-client screenshot coordinate. This engine
    checks geometry, not UI meaning; the operator must select a harmless control.
    ``readiness`` accepts the strict or experimental guarded backend and sends no input. Its
    ``ready=False`` result is a measured guard rejection, not a harness failure.
    """
    if isinstance(hwnd, bool) or not isinstance(hwnd, Integral) or not 0 < hwnd < 2**64:
        raise ValueError("hwnd must be a positive integer")
    if not isinstance(backend, str) or backend not in BACKENDS:
        raise ValueError("unsupported probe backend")
    if not isinstance(operation, str) or operation not in OPERATIONS:
        raise ValueError("unsupported one-shot probe operation")
    if type(key_hold_ms) is not int or not 0 <= key_hold_ms <= 250:
        raise ValueError("probe key_hold_ms must be an integer from 0 to 250")
    if type(trace_key_target) is not bool:
        raise ValueError("trace_key_target must be boolean")
    if not isinstance(capture_method, str) or capture_method not in CAPTURE_METHODS:
        raise ValueError("input probe capture_method must be a supported visible-screen candidate")
    if operation == "readiness" and backend not in _GUARDED:
        raise ValueError("readiness requires a guarded physical backend")
    startup_timeout = _timeout(startup_timeout, "startup_timeout")
    action_timeout = _timeout(action_timeout, "action_timeout")
    expected_size = _expected_size(expected_size)
    if token is not None and not callable(getattr(token, "checkpoint", None)):
        raise TypeError("token must expose checkpoint()")
    if operation in _MOUSE:
        point = _point(point)
    elif point is not None:
        raise ValueError("point is only accepted for mouse operations")
    if target is not None or backend in _GUARDED:
        from tibia_client.window_target import WindowTarget
        if not isinstance(target, WindowTarget):
            raise ValueError("target must be a WindowTarget")
    if backend in _GUARDED and target.kind != "sandbox":
        raise ValueError("guarded physical input requires a calibrated Sandbox WindowTarget")
    if (target is not None and target.kind == "sandbox" and expected_size is not None
            and expected_size != target.host_size):
        raise ValueError("expected_size differs from the calibrated Sandbox host_size")
    if point is not None and expected_size is not None and not (
            point[0] < expected_size[0] and point[1] < expected_size[1]):
        raise ValueError("selected point is outside expected_size")
    _mapped_point(backend, target, point)
    patch_size = (target.host_size if target is not None and target.kind == "sandbox"
                  else expected_size)
    point_patch = _point_patch(point_patch, point, patch_size)
    if backend == _ERROR_ONLY_BACKEND:
        if capture_method != "direct_visible_region_gdi":
            raise ValueError("error-only input requires direct_visible_region_gdi evidence capture")
        if point_patch is not None:
            raise ValueError("error-only input does not use a pre-action control patch")
    if backend == _LEASE_BACKEND:
        if capture_method != "visible_region_gdi_overlay_aware":
            raise ValueError("frame lease requires the explicit region-GDI capture method")
        if not isinstance(known_dialog_report, (str, Path)) or not str(known_dialog_report).strip():
            raise ValueError("frame lease requires an explicit known_dialog_report path")
        known_dialog_report = str(Path(known_dialog_report).resolve())
        if operation in _MOUSE and point_patch is None:
            raise ValueError("frame lease mouse input requires a selected-control point_patch")
    elif known_dialog_report is not None or expected_dialog is not None:
        raise ValueError("known-dialog lease settings apply only to the frame-lease backend")
    if expected_dialog is not None and (
            operation != "escape" or not isinstance(expected_dialog, str)
            or not expected_dialog.strip() or len(expected_dialog) > 120
            or not expected_dialog.isprintable()):
        raise ValueError("expected_dialog requires an explicit bounded name for Escape only")
    directory = Path(output_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    identifier = uuid.uuid4().hex
    spec = {"hwnd": int(hwnd), "backend": backend, "operation": operation,
            "key_hold_ms": key_hold_ms,
            "trace_key_target": trace_key_target,
            "capture_method": capture_method,
            "known_dialog_report": known_dialog_report, "expected_dialog": expected_dialog,
            "expected_size": expected_size,
            "point_patch": point_patch,
            "point": point, "target": None if target is None else target.to_dict(),
            "before_path": str(directory / f"input-{identifier}-before.png"),
            "after_path": str(directory / f"input-{identifier}-after.png")}
    report = {"schema_version": 1, "method": f"{backend}:{operation}", "hwnd": int(hwnd),
              "capture_method": capture_method,
              "known_dialog_report": known_dialog_report, "expected_dialog": expected_dialog,
              "frame_lease": None, "frame_assessment": None, "known_dialog": None,
              "known_dialog_config": None, "cheap_checks": None,
              "window_target": spec["target"],
              "target_fingerprint": None if target is None else target.fingerprint,
              "backend": backend, "operation": operation,
              "point": None if point is None else list(point), "status": "starting",
              "expected_size": None if expected_size is None else list(expected_size),
              "dispatch": "not_attempted", "input_acceptance": "unknown",
              "operator_confirmation": "unknown", "before_path": None, "after_path": None,
              "elapsed_ms": 0, "events": [], "error": None, "capture_error": None,
              "readiness": None, "ready": None, "cursor_restore": {"status": "not_applicable"},
              "cursor_destination": {"status": "not_applicable", "screen_position": None,
                                     "error": None,
                                     "evidence": "cursor position does not prove game acceptance"},
              "point_patch": {"status": "not_supplied" if point_patch is None else "pending",
                              "box": None if point_patch is None else point_patch["box"],
                              "expected_sha256_rgb": None if point_patch is None else point_patch["sha256_rgb"],
                              "actual_sha256_rgb": None, "error": None},
              "cleanup": {"attempted": False, "results": []}, "worker_reaped": True,
              "worker_pid": None, "worker_live": False, "quarantined": False,
              "quarantined_workers": [],
              "startup_timeout": startup_timeout, "action_timeout": action_timeout,
              "startup_deadline_basis": "after_process_start_returns",
              "process_start_duration_ms": None}
    report.update(availability_policy="on_error" if backend == _ERROR_ONLY_BACKEND else "legacy_preflight",
                  pre_action_capture=backend != _ERROR_ONLY_BACKEND, client_availability=None,
                  availability_diagnostics=[], key_hold_ms=key_hold_ms, trace_key_target=trace_key_target)
    started = time.monotonic()
    blocked = _reject_quarantined()
    if blocked:
        report.update(status="quarantined", quarantined=True, quarantined_workers=blocked,
                      worker_pid=blocked[0]["pid"], worker_live=blocked[0]["alive"],
                      worker_reaped=False,
                      error="an earlier input probe worker is not confirmed dead and reaped; no new worker was started")
        report["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
        report["timings"] = _timing_summary([])
        return report
    held = set()
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=True)
    process = context.Process(target=_worker, args=(child, spec), name="tibia-input-probe")
    process.daemon = False
    launched = False
    ready = False

    def consume(message):
        nonlocal ready
        kind = message.get("type")
        if kind == "ready":
            if ready:
                raise RuntimeError("worker sent a duplicate ready handshake")
            ready = True
            report["events"].append({"stage": "worker_ready",
                                     "elapsed_ms": round((time.monotonic() - started) * 1000, 3)})
            return "ready"
        if kind == "result":
            for field in ("status", "error", "readiness", "cursor_restore", "capture_error",
                          "cursor_destination", "frame_lease", "frame_assessment", "known_dialog",
                          "known_dialog_config", "cheap_checks", "client_availability",
                          "availability_policy", "pre_action_capture", "availability_diagnostics"):
                if field in message:
                    report[field] = message[field]
            if report["readiness"] is not None:
                report["ready"] = report["readiness"]["ready"]
            return "result"
        if kind != "event":
            raise RuntimeError("invalid probe worker message")
        event = {key: value for key, value in message.items() if key != "type"}
        event["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
        report["events"].append(event)
        stage = event["stage"]
        if stage == "held":
            control = event["kind"], event["code"]
            if event["held"]:
                held.add(control)
            else:
                held.discard(control)
        elif stage in {"dispatch_start", "dispatch_complete", "dispatch_failed"}:
            report["dispatch"] = {"dispatch_start": "started", "dispatch_complete": "completed",
                                  "dispatch_failed": "failed"}[stage]
        elif stage in {"before_capture_complete", "after_capture_complete"}:
            report["before_path" if stage.startswith("before") else "after_path"] = event["path"]
        elif stage == "before_capture_persisted":
            report["before_path"] = event["path"]
        elif stage == "known_dialog_config":
            report["known_dialog_config"] = event["provenance"]
        elif stage == "known_dialog_complete":
            report["known_dialog"] = event["result"]
        elif stage == "frame_lease_complete":
            report["frame_lease"] = event["result"]
        elif stage == "availability_diagnostic":
            report["client_availability"] = event["result"]
            report["availability_diagnostics"].append(event["result"])
        elif stage == "cursor_snapshot_complete":
            report["cursor_restore"] = {"status": "pending", "original": event["position"]}
        elif stage in {"cursor_restore_complete", "cursor_restore_failed"}:
            report["cursor_restore"].update(status="completed" if stage.endswith("complete") else "failed",
                                            error=event.get("error"))
        elif stage == "cursor_destination_complete":
            report["cursor_destination"].update(status="completed", screen_position=event["position"])
        elif stage == "cursor_destination_failed":
            report["cursor_destination"].update(status="failed", error=event["error"])
        elif stage == "point_patch_complete":
            report["point_patch"] = event["result"]
        return None

    try:
        if token is not None:
            token.checkpoint()
        spawn_started = time.monotonic()
        report["events"].append({"stage": "spawn_start",
                                 "elapsed_ms": round((spawn_started - started) * 1000, 3)})
        try:
            process.start()
            launched = True
        finally:
            spawn_completed = time.monotonic()
            report["process_start_duration_ms"] = round((spawn_completed - spawn_started) * 1000, 3)
            report["events"].append({"stage": "spawn_complete", "succeeded": launched,
                                     "duration_ms": report["process_start_duration_ms"],
                                     "elapsed_ms": round((spawn_completed - started) * 1000, 3)})
        report["worker_pid"] = _process_observation(process)["pid"]
        report["worker_live"] = True
        child.close()
        deadline = time.monotonic() + startup_timeout
        while True:
            if token is not None:
                token.checkpoint()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                report.update(status="action_timeout" if ready else "startup_timeout",
                              error="probe deadline exceeded; no retry was attempted")
                break
            if parent.poll(min(_POLL, remaining)):
                result = consume(parent.recv())
                if result == "ready":
                    if token is not None:
                        token.checkpoint()
                    parent.send("run_once")
                    deadline = time.monotonic() + action_timeout
                elif result == "result":
                    break
            elif not process.is_alive():
                report.update(status="failed", error="probe worker exited without a result")
                break
    except BaseException as exc:
        cancelled = token is not None and (bool(getattr(token, "cancelled", False))
                                         or "cancel" in type(exc).__name__.lower()
                                         or "cancel" in str(exc).lower())
        report.update(status="cancelled" if cancelled else "failed", error=_detail(exc))
    finally:
        if launched:
            try:
                report["worker_reaped"] = _stop_and_reap(process)
            except BaseException as exc:
                report["worker_reaped"] = False
                report["stop_error"] = _detail(exc)
            # The child may have sent DOWN telemetry immediately before timeout.
            try:
                while parent.poll(0):
                    message = parent.recv()
                    if message.get("type") == "event":
                        consume(message)
            except BaseException as exc:
                if not isinstance(exc, (EOFError, OSError)):
                    report["drain_error"] = _detail(exc)
            if held:
                try:
                    report["cleanup"] = _cleanup_held(int(hwnd), backend, held, point)
                except BaseException as exc:
                    report["cleanup"] = {"attempted": True, "results": [], "error": _detail(exc)}
            if not report["worker_reaped"]:
                with _quarantine_lock:
                    _quarantined.add(process)
                report["quarantined"] = True
                report["quarantined_workers"] = [_process_observation(process)]
                report.update(status="failed", error="probe worker could not be stopped and reaped")
            report["worker_live"] = _process_observation(process)["alive"]
        parent.close()
        child.close()
        if launched and report["worker_reaped"]:
            process.close()
        report["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
        report["timings"] = _timing_summary(report["events"])
    return report


__all__ = ["BACKENDS", "CAPTURE_METHODS", "OPERATIONS", "run_input_probe"]
