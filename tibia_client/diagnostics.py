"""Immutable, read-only runtime diagnostics for operator status views."""

from __future__ import annotations

from dataclasses import dataclass


SOURCE_FRESHNESS_UNVERIFIED = "unverified"


@dataclass(frozen=True)
class AdapterCapabilities:
    """Operations and sensors implemented by an adapter.

    Implementation support is deliberately separate from compatibility-profile
    evidence.  Presence here never means an operation was accepted by a game.
    """

    operations: tuple[str, ...] = ()
    sensors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        operations = tuple(self.operations)
        sensors = tuple(self.sensors)
        if any(not isinstance(item, str) or not item for item in operations + sensors):
            raise ValueError("capability names must be non-empty text")
        object.__setattr__(self, "operations", operations)
        object.__setattr__(self, "sensors", sensors)


@dataclass(frozen=True)
class CaptureHealth:
    transport_ok: bool | None = None
    captured_at: float | None = None
    captured_age: float | None = None
    visual_valid: bool | None = None
    source_freshness: str = SOURCE_FRESHNESS_UNVERIFIED
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class ActionDiagnostic:
    owner: str
    key: str
    operation: str
    at: float


@dataclass(frozen=True)
class ResultDiagnostic:
    owner: str
    key: str
    state: str
    reason: str
    at: float
    final: bool


@dataclass(frozen=True)
class SessionDiagnostics:
    ready: bool
    readiness_reason: str | None
    profile_allowed: bool | None
    profile_reasons: tuple[str, ...]
    active_features: tuple[str, ...]
    paused_features: tuple[str, ...]
    capture: CaptureHealth
    last_action: ActionDiagnostic | None
    last_result: ResultDiagnostic | None
    failure_reasons: tuple[str, ...]
    capabilities: AdapterCapabilities
    closed: bool


__all__ = [
    "ActionDiagnostic",
    "AdapterCapabilities",
    "CaptureHealth",
    "ResultDiagnostic",
    "SOURCE_FRESHNESS_UNVERIFIED",
    "SessionDiagnostics",
]
