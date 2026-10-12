"""Pure policies shared by the support automation modules.

This module deliberately has no Tk, screen-capture, or input dependencies so
the decisions can be verified offline with small fakes.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from numbers import Real
from typing import Iterable, Optional, Sequence


@dataclass(frozen=True)
class Threshold:
    enabled: bool
    percent: int
    hotkey: str

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("threshold enabled setting must be true or false")
        if type(self.percent) is not int or not 0 <= self.percent <= 100:
            raise ValueError("threshold must be a whole percentage between 0 and 100")
        if not isinstance(self.hotkey, str):
            raise ValueError("threshold hotkey must be text")
        if self.enabled and not self.hotkey.strip():
            raise ValueError("enabled threshold needs a hotkey")


def choose_threshold(value: Optional[int], rules: Iterable[Threshold]) -> Optional[Threshold]:
    """Return the most urgent satisfied rule.

    A lower configured threshold is more urgent.  Unknown sensor values never
    satisfy a rule.
    """
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= 100:
        raise ValueError("sensor percentage must be a whole number between 0 and 100")
    candidates = [rule for rule in rules if rule.enabled and value <= rule.percent]
    return min(candidates, key=lambda rule: rule.percent, default=None)


@dataclass(frozen=True)
class EquipmentPolicy:
    require_below: bool = False
    threshold: int = 100

    def __post_init__(self) -> None:
        if type(self.require_below) is not bool:
            raise ValueError("life condition setting must be true or false")
        if type(self.threshold) is not int or not 0 <= self.threshold <= 100:
            raise ValueError("life threshold must be a whole percentage between 0 and 100")

    def should_equip(self, empty_slot: Optional[bool], life: Optional[int]) -> bool:
        if empty_slot is not True:
            return False
        if not self.require_below:
            return True
        return type(life) is int and 0 <= life <= self.threshold


@dataclass(frozen=True)
class TimedSpell:
    hotkey: str
    interval: float

    def __post_init__(self) -> None:
        if not isinstance(self.hotkey, str):
            raise ValueError("spell hotkey must be text")
        if not self.hotkey.strip():
            raise ValueError("spell hotkey cannot be empty")
        if (
            not isinstance(self.interval, Real)
            or isinstance(self.interval, bool)
            or not math.isfinite(self.interval)
            or self.interval <= 0
        ):
            raise ValueError("spell interval must be greater than zero")


def parse_spells(rows: Iterable[Sequence[object]]) -> tuple[TimedSpell, ...]:
    result = []
    for row_number, row in enumerate(rows, 1):
        if len(row) < 2:
            raise ValueError(f"spell row {row_number} is incomplete")
        if isinstance(row[1], bool):
            raise ValueError(f"spell row {row_number} has an invalid interval")
        try:
            interval = float(row[1])
        except (TypeError, ValueError):
            raise ValueError(f"spell row {row_number} has an invalid interval") from None
        result.append(TimedSpell(str(row[0]).strip(), interval))
    return tuple(result)
