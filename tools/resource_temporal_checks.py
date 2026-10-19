"""Opt-in, short temporal checks over independently decoded current readings.

This module neither reads pixels nor produces a number from history.  A caller
must decode every current frame, supply a capture/session and current geometry
identity, and use a strictly advancing capture timestamp.  The result contains
the current input value or N/A; it never contains an older accepted value.

The default policy only confirms explicitly unstable current evidence.  The
``confirm_changes=True`` experiment additionally holds a changed number until
two fresh current decodes agree within 250 ms.  This suppresses isolated numeric
glitches, but also holds real damage/healing that is still changing.  It has no
timeout that turns an unconfirmed candidate into a known value.  A correctly
bound, independently checked bar movement can corroborate a real change without
that delay, provided both frames have the same independently verified maximum.

Static screenshot replays can test this policy and its overhead; they are not
evidence about live OCR error persistence or the frequency of real damage.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import math
from numbers import Integral, Real
from typing import Iterable, Mapping

from tibia_client.models import Quality, Reading


def _finite_number(value: object) -> bool:
    return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(value)


def _integer(value: object) -> bool:
    return isinstance(value, Integral) and not isinstance(value, bool)


@dataclass(frozen=True, slots=True)
class TemporalIdentity:
    """Current capture identity plus verified panel geometry, never a pixel hash.

    ``capture_session`` changes when the capture process/clock is restarted.
    Bounds are (left, top, right, bottom).  A panel move, resize, scroll/layout
    change, source change, or new capture session discards the previous history.
    The caller is responsible for validating this identity against this frame.
    """

    source: str
    capture_session: str
    frame_size: tuple[int, int]
    layout_fingerprint: str
    panel_bounds: tuple[int, int, int, int]

    def __post_init__(self):
        for name in ("source", "capture_session", "layout_fingerprint"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name} must be a nonempty string")
        size, bounds = tuple(self.frame_size), tuple(self.panel_bounds)
        if len(size) != 2 or any(not _integer(item) or item <= 0 for item in size):
            raise ValueError("frame_size must contain two positive integers")
        if len(bounds) != 4 or any(not _integer(item) for item in bounds):
            raise ValueError("panel_bounds must contain four integers")
        left, top, right, bottom = bounds
        if not (0 <= left < right <= size[0] and 0 <= top < bottom <= size[1]):
            raise ValueError("panel_bounds must lie inside frame_size")
        object.__setattr__(self, "frame_size", size)
        object.__setattr__(self, "panel_bounds", bounds)


@dataclass(frozen=True, slots=True)
class TemporalBarInterval:
    """A current-frame fraction interval, optionally with a verified maximum.

    ``frame_token`` is an extra binding to the exact current pixels, not a
    substitute for TemporalIdentity or a unique capture timestamp.  The adapter
    must set ``maximum_verified`` only after independently establishing that
    ``maximum`` belongs to this field, frame, and layout.  Never derive it from
    the resource number being checked or carry it over from a previous frame.
    """

    lower: float
    upper: float
    captured_at: float
    layout_fingerprint: str
    frame_token: str
    maximum: int | None = None
    maximum_verified: bool = False

    def __post_init__(self):
        if not all(_finite_number(item) for item in (self.lower, self.upper, self.captured_at)):
            raise ValueError("bar bounds and timestamp must be finite numbers")
        if not (0 <= self.lower <= self.upper <= 1) or self.captured_at < 0:
            raise ValueError("bar interval must lie in [0, 1] and timestamp must be nonnegative")
        if not self.layout_fingerprint or not isinstance(self.layout_fingerprint, str):
            raise ValueError("bar layout_fingerprint must be a nonempty string")
        if not self.frame_token or not isinstance(self.frame_token, str):
            raise ValueError("bar frame_token must be a nonempty string")
        if self.maximum is not None and (not _integer(self.maximum) or self.maximum <= 0):
            raise ValueError("bar maximum must be a positive integer or None")
        if not isinstance(self.maximum_verified, bool):
            raise ValueError("maximum_verified must be boolean")
        if self.maximum_verified and self.maximum is None:
            raise ValueError("a verified maximum must have a value")


@dataclass(frozen=True, slots=True)
class TemporalPolicy:
    """Fixed, annotation-independent policy; no learned numeric jump threshold."""

    confirm_changes: bool = False
    confirmation_window_s: float = 0.250
    min_separation_s: float = 0.001
    max_gap_s: float = 0.500

    def __post_init__(self):
        if not isinstance(self.confirm_changes, bool):
            raise ValueError("confirm_changes must be boolean")
        if not all(_finite_number(item) for item in (
            self.confirmation_window_s, self.min_separation_s, self.max_gap_s,
        )):
            raise ValueError("temporal durations must be finite numbers")
        if not 0 < self.min_separation_s <= self.confirmation_window_s <= 1:
            raise ValueError("confirmation requires 0 < separation <= window <= 1 second")
        if not self.confirmation_window_s <= self.max_gap_s <= 5:
            raise ValueError("capture gap must be between the confirmation window and 5 seconds")


@dataclass(frozen=True, slots=True)
class _Sample:
    value: int
    captured_at: float
    bar: TemporalBarInterval | None


@dataclass(slots=True)
class _FieldHistory:
    accepted: _Sample | None = None
    pending: _Sample | None = None
    quarantined: bool = False


def _current_value(reading: Reading) -> int | None:
    value = reading.value
    if isinstance(value, Mapping):
        if value.get("present", True) is not True:
            return None
        value = value.get("count")
    return int(value) if _integer(value) and value >= 0 else None


class TemporalResourceGuard:
    """At most two samples per field, for at most four current resource fields.

    ``check`` is synchronous and has no sleeps.  Confirmation latency is the
    arrival time of a second agreeing *current* decode, not a scheduled delay.
    Current UNKNOWN/ERROR clears that field's history.  Missing fields also
    disappear from history.  An unconfirmed candidate expires after the short
    window; it never becomes known just because time passed.
    """

    def __init__(self, policy: TemporalPolicy | None = None):
        self.policy = policy or TemporalPolicy()
        if not isinstance(self.policy, TemporalPolicy):
            raise ValueError("policy must be a TemporalPolicy")
        self.reset()

    def reset(self) -> None:
        """Explicitly start a new observation stream, including its clock."""
        self._history: dict[str, _FieldHistory] = {}
        self._identity: TemporalIdentity | None = None
        self._clock_scope: tuple[str, str] | None = None
        self._last_timestamp: float | None = None
        self._clock_fault = False

    @property
    def history_size(self) -> int:
        return sum(int(item.accepted is not None) + int(item.pending is not None)
                   for item in self._history.values())

    @property
    def tracked_fields(self) -> tuple[str, ...]:
        return tuple(self._history)

    def _result(self, reading: Reading, action: str, *, known: bool,
                reset_reason: str | None, bar_status: str = "unavailable", **extra) -> Reading:
        diagnostics = dict(reading.diagnostics)
        diagnostics["temporal"] = {
            "action": action,
            "confirm_changes": self.policy.confirm_changes,
            "reset_reason": reset_reason,
            "bar_status": bar_status,
            **extra,
        }
        if known:
            return replace(reading, diagnostics=diagnostics)
        quality = Quality.ERROR if reading.quality is Quality.ERROR else Quality.UNKNOWN
        return Reading(
            value=None, quality=quality,
            detail=f"temporal check: {action}" + (f"; {reading.detail}" if reading.detail else ""),
            diagnostics=diagnostics,
        )

    @staticmethod
    def _bound_bar(bar, captured_at, identity, frame_token):
        if bar is None:
            return None, "unavailable"
        if not isinstance(bar, TemporalBarInterval):
            raise ValueError("bars must contain TemporalBarInterval values")
        if (bar.captured_at != captured_at or bar.layout_fingerprint != identity.layout_fingerprint
                or frame_token is None or bar.frame_token != frame_token):
            return None, "unbound_or_stale"
        return bar, "verified_maximum" if bar.maximum_verified else "no_verified_maximum"

    def _bar_corroborates_change(self, previous: _Sample, current: _Sample) -> bool:
        before, after = previous.bar, current.bar
        if (before is None or after is None or not before.maximum_verified
                or not after.maximum_verified or before.maximum != after.maximum
                or before.frame_token == after.frame_token
                or not 0 < current.captured_at - previous.captured_at
                <= self.policy.confirmation_window_s):
            return False
        if current.value > previous.value:
            return after.lower > before.upper
        if current.value < previous.value:
            return after.upper < before.lower
        return False

    def check(self, readings: Mapping[str, Reading], *, captured_at: float,
              identity: TemporalIdentity | None, frame_token: str | None = None,
              unstable_fields: Iterable[str] = (),
              bars: Mapping[str, TemporalBarInterval] | None = None) -> dict[str, Reading]:
        """Check freshly decoded numbers; all output numbers come from ``readings``.

        A repeated/out-of-order timestamp rejects the batch and clears numeric
        history.  Its high-water clock remains, and fresh frames must establish
        confirmation again.  Only an explicit source/session change or reset()
        restarts that clock.  Other geometry changes clear history but cannot
        make an old timestamp current.  ``identity=None`` rejects the batch.
        """
        if not isinstance(readings, Mapping) or len(readings) > 4 or any(
            not isinstance(key, str) or not key or not isinstance(value, Reading)
            for key, value in readings.items()
        ):
            raise ValueError("readings must map at most four nonempty field names to Reading")
        if identity is not None and not isinstance(identity, TemporalIdentity):
            raise ValueError("identity must be TemporalIdentity or None")
        if frame_token is not None and (not isinstance(frame_token, str) or not frame_token):
            raise ValueError("frame_token must be a nonempty string or None")
        unstable = frozenset(unstable_fields)
        bars = {} if bars is None else bars
        if not isinstance(bars, Mapping):
            raise ValueError("bars must be a mapping")

        if not _finite_number(captured_at) or captured_at < 0:
            self._history.clear()
            self._clock_fault = True
            return {key: self._result(value, "invalid_timestamp", known=False,
                                     reset_reason="invalid_timestamp")
                    for key, value in readings.items()}
        captured_at = float(captured_at)
        if identity is None:
            self._history.clear()
            self._identity = None
            if self._last_timestamp is None or captured_at > self._last_timestamp:
                self._last_timestamp = captured_at
            return {key: self._result(value, "missing_current_identity", known=False,
                                     reset_reason="missing_current_identity")
                    for key, value in readings.items()}

        scope = (identity.source, identity.capture_session)
        reset_reason = None
        if scope != self._clock_scope:
            reset_reason = "initial_capture" if self._clock_scope is None else "capture_identity_changed"
            self._history.clear()
            self._last_timestamp = None
            self._clock_fault = False
            self._clock_scope = scope
        if self._last_timestamp is not None and captured_at <= self._last_timestamp:
            self._history.clear()
            self._clock_fault = True
            return {key: self._result(value, "nonadvancing_timestamp", known=False,
                                     reset_reason="nonadvancing_timestamp")
                    for key, value in readings.items()}
        if self._last_timestamp is not None and captured_at - self._last_timestamp > self.policy.max_gap_s:
            self._history.clear()
            reset_reason = "capture_gap"
        if identity != self._identity:
            self._history.clear()
            reset_reason = reset_reason or "layout_identity_changed"
        self._identity = identity
        self._last_timestamp = captured_at
        clock_fault = self._clock_fault
        self._clock_fault = False
        self._history = {key: value for key, value in self._history.items() if key in readings}

        results = {}
        for field, reading in readings.items():
            if reading.quality is not Quality.KNOWN:
                self._history.pop(field, None)
                results[field] = self._result(reading, "current_unavailable", known=False,
                                              reset_reason=reset_reason)
                continue
            value = _current_value(reading)
            if value is None:
                self._history.pop(field, None)
                results[field] = self._result(reading, "invalid_current_number", known=False,
                                              reset_reason=reset_reason)
                continue
            bar, bar_status = self._bound_bar(bars.get(field), captured_at, identity, frame_token)
            state = self._history.setdefault(field, _FieldHistory(quarantined=clock_fault))
            if state.accepted and captured_at - state.accepted.captured_at > self.policy.max_gap_s:
                state.accepted = None
            if state.pending and captured_at - state.pending.captured_at > self.policy.confirmation_window_s:
                state.pending = None
            current = _Sample(value, captured_at, bar)
            if (bar is not None and bar.maximum_verified
                    and not (bar.lower - 1 / bar.maximum <= value / bar.maximum
                             <= bar.upper + 1 / bar.maximum)):
                # A contradictory sample is not eligible to confirm a later one.
                # Match the bar-only cross-check's one-integer rounding tolerance.
                self._history[field] = _FieldHistory(quarantined=True)
                results[field] = self._result(reading, "current_bar_contradiction", known=False,
                                              reset_reason=reset_reason, bar_status="contradiction",
                                              current_candidate=value)
                continue

            explicit_unstable = field in unstable
            action = None
            if not explicit_unstable and not state.quarantined and state.accepted is None:
                action = "initial_current"
            elif not explicit_unstable and state.accepted and value == state.accepted.value:
                action = "unchanged_current"
            elif not explicit_unstable and not self.policy.confirm_changes and not state.quarantined:
                action = "trusted_current_change"
            elif (not explicit_unstable and state.accepted
                  and self._bar_corroborates_change(state.accepted, current)):
                action = "bar_corroborated_current_change"

            extra = {}
            if action is None and state.pending and state.pending.value == value:
                age = captured_at - state.pending.captured_at
                if self.policy.min_separation_s <= age <= self.policy.confirmation_window_s:
                    action = "fresh_current_confirmation"
                    extra["confirmation_delay_s"] = age
            if action is not None:
                state.accepted, state.pending, state.quarantined = current, None, False
                results[field] = self._result(reading, action, known=True, reset_reason=reset_reason,
                                              bar_status=bar_status, **extra)
                continue

            if state.pending is None or state.pending.value != value:
                state.pending = current
            state.quarantined = True
            results[field] = self._result(
                reading, "awaiting_fresh_confirmation", known=False, reset_reason=reset_reason,
                bar_status=bar_status, current_candidate=value,
                pending_age_s=captured_at - state.pending.captured_at,
            )
        return results
