"""Experimental one-operation reuse of before-action pixel evidence.

This module neither captures nor sends input. The caller supplies focus, known
state, and control-patch assessments from the same immutable frame, together
with its RGB hash and acquisition times. Every input boundary must call
``check`` inside ``action``; the supplied cheap callback must affirm current
native focus/geometry/point checks. Key/button releases remain unconditional
in the input backend and must never depend on this lease succeeding.

One frame cannot observe guest focus or dialog changes between input phases.
Neither a lease nor ``no_known_veto`` establishes general game readiness.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import math
from numbers import Integral, Real
import time


OPERATIONS = frozenset({"ctrl_o", "escape", "left_click", "right_click", "move", "readiness"})
_MOUSE = frozenset({"left_click", "right_click", "move"})
KNOWN_STATES = frozenset({"no_known_veto", "blocked_known_dialog", "unknown"})


class FrameLeaseError(RuntimeError):
    """The one-operation experimental evidence can no longer be used."""


def _text(value, label, limit=128):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{label} must contain bounded nonempty text")
    return value


def _digest(value, label):
    if (not isinstance(value, str) or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _number(value, label, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or result < 0 or (positive and result == 0):
        raise ValueError(f"{label} must be a finite {'positive' if positive else 'nonnegative'} number")
    return result


@dataclass(frozen=True)
class ProbeIntent:
    """Bind evidence to one exact operation, target, and capture policy."""

    hwnd: int
    target_fingerprint: str
    capture_method: str
    backend: str
    operation: str
    point: tuple[int, int] | None = None
    expected_dialog: str | None = None

    def __post_init__(self):
        if (isinstance(self.hwnd, bool) or not isinstance(self.hwnd, Integral)
                or not 0 < self.hwnd < 2**64):
            raise ValueError("hwnd must be a positive native handle")
        object.__setattr__(self, "hwnd", int(self.hwnd))
        _digest(self.target_fingerprint, "target_fingerprint")
        _text(self.capture_method, "capture_method")
        _text(self.backend, "backend")
        if not isinstance(self.operation, str) or self.operation not in OPERATIONS:
            raise ValueError("unsupported one-operation frame lease intent")
        if self.operation in _MOUSE:
            if (not isinstance(self.point, (list, tuple)) or len(self.point) != 2
                    or any(isinstance(value, bool) or not isinstance(value, Integral)
                           or not 0 <= value <= 32767 for value in self.point)):
                raise ValueError("mouse intent requires two bounded host-client point coordinates")
            object.__setattr__(self, "point", tuple(int(value) for value in self.point))
        elif self.point is not None:
            raise ValueError("only mouse intent accepts a point")
        if self.expected_dialog is not None:
            _text(self.expected_dialog, "expected_dialog", 120)
            if self.operation != "escape":
                raise ValueError("only explicit Escape may expect a recognized dialog")

    def metadata(self):
        return {
            "hwnd": self.hwnd, "target_fingerprint": self.target_fingerprint,
            "capture_method": self.capture_method, "backend": self.backend,
            "operation": self.operation,
            "point": None if self.point is None else list(self.point),
            "expected_dialog": self.expected_dialog,
        }


class FrameLease:
    """Use one supplied before-frame only during one bounded action scope.

    ``cheap_check`` takes no arguments and must return exactly ``True`` after
    checking the bound host HWND/root, foreground, visibility, physical client
    rectangle, and any input point. It must not capture pixels, refresh this
    evidence, or send input. Failure/expiry permanently revokes the lease.
    """

    def __init__(self, intent: ProbeIntent, *, capture_started_at, capture_completed_at,
                 frame_rgb_sha256, focus_verified, known_state, point_patch_verified,
                 cheap_check, known_dialog_name=None, clock=time.monotonic,
                 max_frame_age_ms=1000, max_action_scope_ms=250):
        if not isinstance(intent, ProbeIntent):
            raise TypeError("intent must be a ProbeIntent")
        self._intent = intent
        self.capture_started_at = _number(capture_started_at, "capture_started_at")
        self.capture_completed_at = _number(capture_completed_at, "capture_completed_at")
        if self.capture_completed_at < self.capture_started_at:
            raise ValueError("capture completion cannot precede acquisition start")
        self.frame_rgb_sha256 = _digest(frame_rgb_sha256, "frame_rgb_sha256")
        if focus_verified is not None and type(focus_verified) is not bool:
            raise ValueError("focus_verified must be true, false, or unknown")
        if not isinstance(known_state, str) or known_state not in KNOWN_STATES:
            raise ValueError("known_state is not a recognized veto assessment")
        if point_patch_verified is not None and type(point_patch_verified) is not bool:
            raise ValueError("point_patch_verified must be true, false, or unknown")
        if known_dialog_name is not None:
            _text(known_dialog_name, "known_dialog_name", 120)
        if not callable(cheap_check) or not callable(clock):
            raise TypeError("cheap_check and clock must be callable")
        self.focus_verified = focus_verified
        self.known_state = known_state
        self.known_dialog_name = known_dialog_name
        self.point_patch_verified = point_patch_verified
        self.cheap_check, self.clock = cheap_check, clock
        self.max_frame_age_ms = _number(max_frame_age_ms, "max_frame_age_ms", positive=True)
        self.max_action_scope_ms = _number(max_action_scope_ms, "max_action_scope_ms", positive=True)
        if self.max_frame_age_ms > 1000 or self.max_action_scope_ms > 250:
            raise ValueError("experimental budgets cannot exceed 1000 ms frame age / 250 ms action scope")
        self._state = "prepared"
        self._reason = None
        self._scope_started_at = None
        self._last_observed_at = None
        self._max_observed_frame_age_ms = None
        self._frame_age_at_scope_start_ms = None
        self._check_count = self._cheap_check_count = 0

    @property
    def intent(self):
        return self._intent

    def _refuse(self, reason):
        self._state = "revoked"
        self._reason = str(reason)[:2048]
        raise FrameLeaseError(self._reason)

    def _now(self):
        try:
            now = _number(self.clock(), "lease clock")
        except Exception as exc:
            self._refuse(f"lease clock unavailable: {type(exc).__name__}: {exc}")
        if self._last_observed_at is not None and now < self._last_observed_at:
            self._refuse("lease clock moved backwards")
        self._last_observed_at = now
        return now

    def _validate_time(self, now):
        if now < self.capture_completed_at:
            self._refuse("capture timestamps are in the future")
        age_ms = (now - self.capture_started_at) * 1000
        self._max_observed_frame_age_ms = max(self._max_observed_frame_age_ms or 0, age_ms)
        if age_ms > self.max_frame_age_ms:
            self._refuse("before-action frame exceeded its maximum acquisition age")
        if (self._scope_started_at is not None
                and (now - self._scope_started_at) * 1000 > self.max_action_scope_ms):
            self._refuse("one-operation action scope exceeded its time budget")

    def _validate_visual_evidence(self):
        if self.focus_verified is not True:
            self._refuse("before-action focus evidence is false or unknown")
        if self.intent.expected_dialog is not None:
            if (self.known_state != "blocked_known_dialog"
                    or self.known_dialog_name != self.intent.expected_dialog):
                self._refuse("explicit Escape requires the exact expected recognized dialog")
        elif self.known_state != "no_known_veto":
            self._refuse("before-action known-state veto is present or unknown")
        if self.intent.operation in _MOUSE and self.point_patch_verified is not True:
            self._refuse("before-action selected-control patch is changed or unknown")

    def check(self, intent: ProbeIntent):
        """Revalidate the active operation without acquiring another frame."""
        self._check_count += 1
        if self._state != "active":
            self._refuse("frame lease is not inside its single active action scope")
        if intent != self.intent:
            self._refuse("operation, point, target, or capture policy differs from the leased intent")
        self._validate_time(self._now())
        self._validate_visual_evidence()
        self._cheap_check_count += 1
        try:
            result = self.cheap_check()
        except Exception as exc:
            self._refuse(f"native boundary check failed: {type(exc).__name__}: {exc}")
        if result is not True:
            self._refuse("native boundary check did not affirm current host readiness")
        # A slow callback cannot refresh the timestamp or consume an unnoticed
        # time budget while reporting that the operation may still proceed.
        self._validate_time(self._now())
        return True

    @contextmanager
    def action(self, intent: ProbeIntent):
        """Enter once; scope exit closes evidence even when dispatch raises."""
        if self._state != "prepared":
            self._refuse("frame lease was already used or revoked; recapture and re-arm explicitly")
        self._state = "active"
        try:
            self._scope_started_at = self._now()
            self._frame_age_at_scope_start_ms = (
                self._scope_started_at - self.capture_started_at) * 1000
            self.check(intent)
            yield self
            # Endpoint telemetry and best-effort restoration may catch their
            # own guard exceptions. A revoked boundary must still fail the
            # enclosing action outcome even if its caller swallowed it.
            if self._state != "active":
                raise FrameLeaseError(self._reason or "frame lease was revoked during the action")
            self._validate_time(self._now())
        except BaseException as exc:
            if self._state != "revoked":
                self._state = "revoked"
                self._reason = f"action scope interrupted: {type(exc).__name__}: {exc}"[:2048]
            raise
        finally:
            if self._state == "active":
                self._state = "closed"

    def metadata(self):
        """Return recorded evidence only; telemetry never refreshes the lease."""
        scope_ms = None
        if self._scope_started_at is not None and self._last_observed_at is not None:
            scope_ms = max(0, (self._last_observed_at - self._scope_started_at) * 1000)
        return {
            "schema": "tibiaauto.experimental-action-frame-lease", "version": 1,
            "intent": self.intent.metadata(), "status": self._state, "reason": self._reason,
            "frame_rgb_sha256": self.frame_rgb_sha256,
            "capture_started_at": self.capture_started_at,
            "capture_completed_at": self.capture_completed_at,
            "max_frame_age_ms": self.max_frame_age_ms,
            "max_action_scope_ms": self.max_action_scope_ms,
            "frame_age_at_scope_start_ms": self._frame_age_at_scope_start_ms,
            "max_observed_frame_age_ms": self._max_observed_frame_age_ms,
            "scope_elapsed_ms": scope_ms, "check_count": self._check_count,
            "cheap_check_count": self._cheap_check_count,
            "focus_verified": self.focus_verified, "known_state": self.known_state,
            "known_dialog_name": self.known_dialog_name,
            "point_patch_verified": self.point_patch_verified,
            "visual_assessment": "before_action_only",
            "same_frame_assessments": "caller_supplied_bound_to_rgb_hash",
            "guest_changes_between_input_phases": "unverified",
            "overall_safety": "unknown", "production_wiring": "none",
        }


__all__ = ["FrameLease", "FrameLeaseError", "ProbeIntent"]
