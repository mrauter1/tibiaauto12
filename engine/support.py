"""Pure policies shared by the support automation modules.

This module deliberately has no Tk, screen-capture, or input dependencies so
the decisions can be verified offline with small fakes.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import Iterable, Optional, Sequence


def arm_input(sender, timeout: float = 30.0) -> None:
    """Wait until the configured client can safely receive input.

    The injected sender owns foreground detection and cooperative cancellation;
    this helper deliberately performs no capture and acquires no input lock.
    """
    if timeout <= 0:
        raise ValueError("arming timeout must be greater than zero")
    sender.wait_until_ready(timeout=timeout)


@dataclass(frozen=True)
class Threshold:
    enabled: bool
    percent: int
    hotkey: str

    def __post_init__(self) -> None:
        if not 0 <= self.percent <= 100:
            raise ValueError("threshold must be between 0 and 100")
        if self.enabled and not self.hotkey.strip():
            raise ValueError("enabled threshold needs a hotkey")


def choose_threshold(value: Optional[int], rules: Iterable[Threshold]) -> Optional[Threshold]:
    """Return the most urgent satisfied rule.

    A lower configured threshold is more urgent.  Unknown sensor values never
    satisfy a rule.
    """
    if value is None:
        return None
    if not 0 <= value <= 100:
        raise ValueError("sensor percentage must be between 0 and 100")
    candidates = [rule for rule in rules if rule.enabled and value <= rule.percent]
    return min(candidates, key=lambda rule: rule.percent, default=None)


class Cooldown:
    def __init__(self, seconds: float, clock=time.monotonic) -> None:
        if seconds < 0:
            raise ValueError("cooldown must be non-negative")
        self.seconds = float(seconds)
        self.clock = clock
        self._last: Optional[float] = None

    def ready(self, now: Optional[float] = None) -> bool:
        now = self.clock() if now is None else now
        return self._last is None or now - self._last >= self.seconds

    def mark(self, now: Optional[float] = None) -> None:
        self._last = self.clock() if now is None else now


@dataclass(frozen=True)
class EquipmentPolicy:
    require_below: bool = False
    threshold: int = 100

    def __post_init__(self) -> None:
        if not 0 <= self.threshold <= 100:
            raise ValueError("life threshold must be between 0 and 100")

    def should_equip(self, empty_slot: Optional[bool], life: Optional[int]) -> bool:
        if empty_slot is not True:
            return False
        if not self.require_below:
            return True
        return life is not None and life <= self.threshold


@dataclass(frozen=True)
class TimedSpell:
    hotkey: str
    interval: float

    def __post_init__(self) -> None:
        if not self.hotkey.strip():
            raise ValueError("spell hotkey cannot be empty")
        if not math.isfinite(self.interval) or self.interval <= 0:
            raise ValueError("spell interval must be greater than zero")


class TimedSchedule:
    """Monotonic deadline scheduler which emits at most one spell per poll."""

    def __init__(
        self,
        spells: Sequence[TimedSpell],
        *,
        immediate: bool = False,
        spacing: float = 0.15,
        clock=time.monotonic,
    ) -> None:
        if not spells:
            raise ValueError("add at least one timed spell")
        if spacing < 0:
            raise ValueError("spacing must be non-negative")
        self.spells = tuple(spells)
        self.clock = clock
        self.spacing = float(spacing)
        now = clock()
        self.deadlines = [now if immediate else now + spell.interval for spell in spells]
        self.available_at = now

    def due(self, now: Optional[float] = None) -> Optional[TimedSpell]:
        now = self.clock() if now is None else now
        if now < self.available_at:
            return None
        due = [i for i, deadline in enumerate(self.deadlines) if deadline <= now]
        if not due:
            return None
        index = min(due, key=lambda i: (self.deadlines[i], i))
        spell = self.spells[index]
        missed = math.floor((now - self.deadlines[index]) / spell.interval) + 1
        self.deadlines[index] += missed * spell.interval
        self.available_at = now + self.spacing
        return spell

    def wait_time(self, now: Optional[float] = None, maximum: float = 0.25) -> float:
        now = self.clock() if now is None else now
        next_time = max(self.available_at, min(self.deadlines))
        return max(0.01, min(maximum, next_time - now))


def parse_spells(rows: Iterable[Sequence[object]]) -> tuple[TimedSpell, ...]:
    result = []
    for row_number, row in enumerate(rows, 1):
        if len(row) < 2:
            raise ValueError(f"spell row {row_number} is incomplete")
        try:
            interval = float(row[1])
        except (TypeError, ValueError):
            raise ValueError(f"spell row {row_number} has an invalid interval") from None
        result.append(TimedSpell(str(row[0]).strip(), interval))
    return tuple(result)
