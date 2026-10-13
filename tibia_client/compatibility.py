"""Desktop compatibility profiles, probe policy and bounded evidence storage.

Compatibility evidence is deliberately scoped to an exact, durable desktop
context.  Runtime window handles are bindings and never become part of that
identity.  This module is headless and performs no native capture or input;
``ProbeFeature`` expresses its one action through the shared SDK scheduler.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Sequence

from conf.conf_manager import ConfManager
from conf.HexMapKeys import KeyToHex
from core.Getters import validate_calibration
from core.paths import scripts_path
from tibia_client.models import (
    Action,
    ActionKind,
    ActionResult,
    Confirmation,
    FeatureContext,
    Observation,
    Priority,
    Request,
    Requirement,
    ResultState,
    Sensor,
    SensorKind,
    Step,
)


SCHEMA = "tibiaauto.desktop-compatibility"
SCHEMA_VERSION = 1
MAX_PROFILE_BYTES = 256 * 1024
MAX_EVIDENCE = 256
MAX_TEXT = 512
MAX_FRAMES = 16
MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_EXPORT_BYTES = 32 * 1024 * 1024

OPERATIONS = (
    "press",
    "modified_press",
    "left_click",
    "right_click",
    "drag",
    "cursor_restore",
    "battle_list_click",
)

_PHYSICAL_CURSOR_OPERATIONS = frozenset({
    "left_click", "right_click", "drag", "battle_list_click",
})


class CompatibilityError(ValueError):
    """A profile, evidence item or export is unsafe or inconsistent."""


class Outcome(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNVERIFIED = "unverified"


class EvidenceSource(str, Enum):
    LIVE = "live"
    SIMULATION = "simulation"


def _text(value: object, label: str, *, optional: bool = False) -> str:
    if not isinstance(value, str):
        raise CompatibilityError(f"{label} must be text")
    value = value.strip()
    if not optional and not value:
        raise CompatibilityError(f"{label} must not be empty")
    if len(value) > MAX_TEXT:
        raise CompatibilityError(f"{label} is longer than {MAX_TEXT} characters")
    return value


def _positive_pair(value: object, label: str) -> tuple[int, int]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise CompatibilityError(f"{label} must be [width, height]")
    pair = tuple(value)
    if any(type(item) is not int or item <= 0 for item in pair):
        raise CompatibilityError(f"{label} must contain positive integers")
    return pair  # type: ignore[return-value]


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise CompatibilityError(f"value is not bounded JSON data: {exc}") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CompatibilityError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _invalid_constant(value: str):
    raise CompatibilityError(f"nonfinite JSON number: {value}")


def _today(value: str) -> str:
    value = _text(value, "test_date")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise CompatibilityError("test_date must use YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise CompatibilityError("test_date must use YYYY-MM-DD") from exc
    return value


def key_operation(key: str) -> str:
    """Return the compatibility operation represented by a supported hotkey.

    Keys come from the native-free backend key map. Spacing and modifier case
    follow the input facade's parser; key names remain backend-exact.
    """

    if not isinstance(key, str) or not key.strip():
        raise CompatibilityError("probe hotkey must be non-empty text")
    parts = [part.strip() for part in key.split("+")]
    if len(parts) == 1 and parts[0] in KeyToHex:
        return "press"
    if (
        len(parts) == 2
        and parts[0].lower() in {"ctrl", "shift"}
        and parts[1] in KeyToHex
    ):
        return "modified_press"
    raise CompatibilityError(
        f"unsupported probe hotkey {key!r}; choose a backend-supported key"
    )


@dataclass(frozen=True)
class EnvironmentMetadata:
    """Durable identity of the tested client/OBS/input environment."""

    client_build: str
    ot_server: str
    windows_version: str
    renderer: str
    obs_version: str
    obs_configuration: str
    dpi_scaling: str
    layout: str
    input_mode: str
    capture_mode: str = "obs_projector"
    test_date: str = field(default_factory=lambda: date.today().isoformat())
    battleye_state: str = "unknown"
    battleye_provenance: str = "operator-reported"
    capture_title: str = ""
    input_title: str = ""

    def __post_init__(self) -> None:
        for name in (
            "client_build", "ot_server", "windows_version", "renderer",
            "obs_version", "obs_configuration", "dpi_scaling", "layout",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if self.input_mode not in ("physical", "messages"):
            raise CompatibilityError("input_mode must be 'physical' or 'messages'")
        if self.capture_mode not in ("obs_projector", "direct_client"):
            raise CompatibilityError("capture_mode must be 'obs_projector' or 'direct_client'")
        object.__setattr__(self, "test_date", _today(self.test_date))
        object.__setattr__(
            self, "battleye_state", _text(self.battleye_state, "battleye_state")
        )
        object.__setattr__(
            self,
            "battleye_provenance",
            _text(self.battleye_provenance, "battleye_provenance"),
        )
        object.__setattr__(
            self, "capture_title", _text(self.capture_title, "capture_title", optional=True)
        )
        object.__setattr__(
            self, "input_title", _text(self.input_title, "input_title", optional=True)
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EnvironmentMetadata":
        if not isinstance(data, Mapping):
            raise CompatibilityError("environment must be an object")
        allowed = {item.name for item in cls.__dataclass_fields__.values()}
        unknown = set(data) - allowed
        if unknown:
            raise CompatibilityError("unknown environment fields: " + ", ".join(sorted(unknown)))
        try:
            return cls(**dict(data))
        except TypeError as exc:
            raise CompatibilityError(f"environment metadata is incomplete: {exc}") from exc


@dataclass(frozen=True)
class DesktopContext:
    """Exact profile identity; HWND values are intentionally absent."""

    environment: EnvironmentMetadata
    calibration_digest: str
    capture_size: tuple[int, int]
    input_size: tuple[int, int]
    mapping: str = "1:1"

    def __post_init__(self) -> None:
        if not isinstance(self.environment, EnvironmentMetadata):
            raise CompatibilityError("environment must be EnvironmentMetadata")
        digest = _text(self.calibration_digest, "calibration_digest")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise CompatibilityError("calibration_digest must be a SHA-256 hex digest")
        object.__setattr__(self, "calibration_digest", digest)
        object.__setattr__(self, "capture_size", _positive_pair(self.capture_size, "capture_size"))
        object.__setattr__(self, "input_size", _positive_pair(self.input_size, "input_size"))
        if self.mapping != "1:1":
            raise CompatibilityError("M0 supports only a 1:1 capture/input mapping")

    @property
    def fingerprint(self) -> str:
        identity = self.to_dict()
        # The date describes when evidence was collected; editing it alone is
        # not a client/OBS/calibration change and must not stale that evidence.
        identity["environment"].pop("test_date", None)
        return _digest(identity)

    @property
    def aligned(self) -> bool:
        return self.mapping == "1:1" and self.capture_size == self.input_size

    def to_dict(self) -> dict[str, Any]:
        return {
            "environment": asdict(self.environment),
            "calibration_digest": self.calibration_digest,
            "capture_size": list(self.capture_size),
            "input_size": list(self.input_size),
            "mapping": self.mapping,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DesktopContext":
        if not isinstance(data, Mapping):
            raise CompatibilityError("context must be an object")
        expected = {"environment", "calibration_digest", "capture_size", "input_size", "mapping"}
        if set(data) != expected:
            raise CompatibilityError("context fields must be exactly: " + ", ".join(sorted(expected)))
        return cls(
            EnvironmentMetadata.from_dict(data["environment"]),
            data["calibration_digest"],
            data["capture_size"],
            data["input_size"],
            data["mapping"],
        )


def calibration_digest(calibration: Mapping[str, Any]) -> str:
    """Fingerprint calibration geometry without retaining runtime handles."""

    if not isinstance(calibration, Mapping):
        raise CompatibilityError("calibration must be an object")
    # These fields define all persisted client-local geometry and input choices.
    relevant = {
        key: calibration.get(key)
        for key in ("Stats", "calibration_size", "Positions", "Boxes", "SQM", "MouseOption", "ItemsMode")
    }
    return _digest(relevant)


def context_from_calibration(
    calibration: Mapping[str, Any],
    environment: EnvironmentMetadata,
    *,
    input_size: Sequence[int] | None = None,
) -> DesktopContext:
    validate_calibration(calibration)
    capture_size = _positive_pair(calibration.get("calibration_size"), "calibration_size")
    expected_mode = {0: "messages", 1: "physical"}.get(calibration.get("MouseOption"))
    if expected_mode is None:
        raise CompatibilityError("calibration MouseOption must be 0 (messages) or 1 (physical)")
    if environment.input_mode != expected_mode:
        raise CompatibilityError(
            f"environment input_mode is {environment.input_mode!r} but calibration uses {expected_mode!r}"
        )
    selected_input = capture_size if input_size is None else _positive_pair(input_size, "input_size")
    return DesktopContext(
        environment,
        calibration_digest(calibration),
        capture_size,
        selected_input,
    )


@dataclass(frozen=True)
class ProbeEvidence:
    operation: str
    context_fingerprint: str
    recorded_at: str
    dispatch: Outcome = Outcome.UNVERIFIED
    automatic_confirmation: Outcome = Outcome.UNVERIFIED
    operator_confirmation: Outcome = Outcome.UNVERIFIED
    detail: str = ""
    point: tuple[int, int] | None = None
    source: EvidenceSource = EvidenceSource.SIMULATION
    cursor_original: tuple[int, int] | None = None
    cursor_final: tuple[int, int] | None = None
    key: str = ""
    destination: tuple[int, int] | None = None
    intended_target: str = ""
    before: tuple[str, ...] = ()
    after: tuple[str, ...] = ()
    cursor_restore_confirmation: Outcome = Outcome.UNVERIFIED

    def __post_init__(self) -> None:
        if self.operation not in OPERATIONS:
            raise CompatibilityError(f"unknown operation {self.operation!r}")
        fingerprint = _text(self.context_fingerprint, "evidence context_fingerprint")
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise CompatibilityError("evidence context_fingerprint must be SHA-256")
        object.__setattr__(self, "context_fingerprint", fingerprint)
        recorded_at = _text(self.recorded_at, "evidence recorded_at")
        try:
            parsed = datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise CompatibilityError("recorded_at must be an ISO-8601 timestamp") from exc
        if parsed.tzinfo is None:
            raise CompatibilityError("recorded_at must include a timezone")
        object.__setattr__(self, "recorded_at", recorded_at)
        for name in ("dispatch", "automatic_confirmation", "operator_confirmation"):
            object.__setattr__(self, name, Outcome(getattr(self, name)))
        object.__setattr__(
            self, "cursor_restore_confirmation", Outcome(self.cursor_restore_confirmation)
        )
        object.__setattr__(self, "source", EvidenceSource(self.source))
        object.__setattr__(self, "detail", _text(self.detail, "evidence detail", optional=True))
        object.__setattr__(self, "key", _text(self.key, "evidence key", optional=True))
        object.__setattr__(
            self, "intended_target", _text(self.intended_target, "intended target", optional=True)
        )
        if self.point is not None:
            point = tuple(self.point)
            if len(point) != 2 or any(type(value) is not int or value < 0 for value in point):
                raise CompatibilityError("evidence point must contain non-negative integers")
            object.__setattr__(self, "point", point)
        for name in ("cursor_original", "cursor_final"):
            value = getattr(self, name)
            if value is not None:
                point = tuple(value)
                if len(point) != 2 or any(type(item) is not int for item in point):
                    raise CompatibilityError(f"{name} must contain integers")
                object.__setattr__(self, name, point)
        if self.destination is not None:
            destination = tuple(self.destination)
            if len(destination) != 2 or any(type(item) is not int or item < 0 for item in destination):
                raise CompatibilityError("destination must contain non-negative integers")
            object.__setattr__(self, "destination", destination)
        for name in ("before", "after"):
            values = tuple(_text(item, f"{name} observation") for item in getattr(self, name))
            if len(values) > 64:
                raise CompatibilityError(f"{name} may contain at most 64 observation summaries")
            object.__setattr__(self, name, values)

    @property
    def outcome(self) -> Outcome:
        if self.source is not EvidenceSource.LIVE:
            return Outcome.UNVERIFIED
        confirmations = (self.automatic_confirmation, self.operator_confirmation)
        if self.dispatch is Outcome.FAIL or Outcome.FAIL in confirmations:
            return Outcome.FAIL
        if self.dispatch is Outcome.PASS and Outcome.PASS in confirmations:
            return Outcome.PASS
        return Outcome.UNVERIFIED

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "context_fingerprint": self.context_fingerprint,
            "recorded_at": self.recorded_at,
            "dispatch": self.dispatch.value,
            "automatic_confirmation": self.automatic_confirmation.value,
            "operator_confirmation": self.operator_confirmation.value,
            "outcome": self.outcome.value,
            "detail": self.detail,
            "point": None if self.point is None else list(self.point),
            "source": self.source.value,
            "cursor_original": (
                None if self.cursor_original is None else list(self.cursor_original)
            ),
            "cursor_final": None if self.cursor_final is None else list(self.cursor_final),
            "key": self.key,
            "destination": None if self.destination is None else list(self.destination),
            "intended_target": self.intended_target,
            "before": list(self.before),
            "after": list(self.after),
            "cursor_restore_confirmation": self.cursor_restore_confirmation.value,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ProbeEvidence":
        if not isinstance(data, Mapping):
            raise CompatibilityError("evidence must be an object")
        allowed = {
            "operation", "context_fingerprint", "recorded_at", "dispatch",
            "automatic_confirmation", "operator_confirmation", "outcome", "detail", "point",
            "source", "cursor_original", "cursor_final",
            "key", "destination", "intended_target", "before", "after",
            "cursor_restore_confirmation",
        }
        if set(data) - allowed:
            raise CompatibilityError("unknown evidence fields")
        evidence = cls(**{key: value for key, value in data.items() if key != "outcome"})
        stored = data.get("outcome")
        if stored is not None and stored != evidence.outcome.value:
            raise CompatibilityError("stored evidence outcome is inconsistent with confirmations")
        return evidence


@dataclass(frozen=True)
class AlignmentEvidence:
    """Operator-observed equality of one capture and client-local point."""

    context_fingerprint: str
    recorded_at: str
    capture_point: tuple[int, int]
    input_point: tuple[int, int]
    operator_confirmation: Outcome
    source: EvidenceSource = EvidenceSource.SIMULATION
    detail: str = ""

    def __post_init__(self) -> None:
        fingerprint = _text(self.context_fingerprint, "alignment context_fingerprint")
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise CompatibilityError("alignment context_fingerprint must be SHA-256")
        object.__setattr__(self, "context_fingerprint", fingerprint)
        recorded_at = _text(self.recorded_at, "alignment recorded_at")
        try:
            parsed = datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise CompatibilityError("alignment recorded_at must be ISO-8601") from exc
        if parsed.tzinfo is None:
            raise CompatibilityError("alignment recorded_at must include a timezone")
        object.__setattr__(self, "recorded_at", recorded_at)
        for name in ("capture_point", "input_point"):
            point = tuple(getattr(self, name))
            if len(point) != 2 or any(type(item) is not int or item < 0 for item in point):
                raise CompatibilityError(f"{name} must contain non-negative integers")
            object.__setattr__(self, name, point)
        object.__setattr__(self, "operator_confirmation", Outcome(self.operator_confirmation))
        object.__setattr__(self, "source", EvidenceSource(self.source))
        object.__setattr__(self, "detail", _text(self.detail, "alignment detail", optional=True))

    @property
    def outcome(self) -> Outcome:
        if self.source is not EvidenceSource.LIVE:
            return Outcome.UNVERIFIED
        if self.operator_confirmation is Outcome.FAIL:
            return Outcome.FAIL
        if (
            self.operator_confirmation is Outcome.PASS
            and self.capture_point == self.input_point
        ):
            return Outcome.PASS
        return Outcome.UNVERIFIED

    def to_dict(self) -> dict[str, Any]:
        return {
            "context_fingerprint": self.context_fingerprint,
            "recorded_at": self.recorded_at,
            "capture_point": list(self.capture_point),
            "input_point": list(self.input_point),
            "operator_confirmation": self.operator_confirmation.value,
            "source": self.source.value,
            "detail": self.detail,
            "outcome": self.outcome.value,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AlignmentEvidence":
        allowed = {
            "context_fingerprint", "recorded_at", "capture_point", "input_point",
            "operator_confirmation", "source", "detail", "outcome",
        }
        if not isinstance(data, Mapping) or set(data) - allowed:
            raise CompatibilityError("invalid alignment evidence fields")
        item = cls(**{key: value for key, value in data.items() if key != "outcome"})
        if data.get("outcome", item.outcome.value) != item.outcome.value:
            raise CompatibilityError("stored alignment outcome is inconsistent")
        return item


@dataclass(frozen=True)
class CaptureCheck:
    """Capture facts kept distinct from unknowable upstream frame freshness."""

    context_fingerprint: str
    recorded_at: str
    transport: Outcome
    visual_validity: Outcome
    operator_visible_change: Outcome
    upstream_freshness: Outcome = Outcome.UNVERIFIED
    source: EvidenceSource = EvidenceSource.SIMULATION
    detail: str = ""

    def __post_init__(self) -> None:
        fingerprint = _text(self.context_fingerprint, "capture context_fingerprint")
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise CompatibilityError("capture check context_fingerprint must be SHA-256")
        object.__setattr__(self, "context_fingerprint", fingerprint)
        recorded_at = _text(self.recorded_at, "capture recorded_at")
        try:
            parsed = datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise CompatibilityError("capture check recorded_at must be ISO-8601") from exc
        if parsed.tzinfo is None:
            raise CompatibilityError("capture check recorded_at must include a timezone")
        object.__setattr__(self, "recorded_at", recorded_at)
        for name in (
            "transport", "visual_validity", "operator_visible_change", "upstream_freshness"
        ):
            object.__setattr__(self, name, Outcome(getattr(self, name)))
        object.__setattr__(self, "source", EvidenceSource(self.source))
        object.__setattr__(self, "detail", _text(self.detail, "capture detail", optional=True))

    @property
    def outcome(self) -> Outcome:
        required = (self.transport, self.visual_validity, self.operator_visible_change)
        if Outcome.FAIL in required or self.upstream_freshness is Outcome.FAIL:
            return Outcome.FAIL
        if self.source is EvidenceSource.LIVE and all(item is Outcome.PASS for item in required):
            return Outcome.PASS
        return Outcome.UNVERIFIED

    def to_dict(self) -> dict[str, Any]:
        return {
            "context_fingerprint": self.context_fingerprint,
            "recorded_at": self.recorded_at,
            "transport": self.transport.value,
            "visual_validity": self.visual_validity.value,
            "operator_visible_change": self.operator_visible_change.value,
            "upstream_freshness": self.upstream_freshness.value,
            "source": self.source.value,
            "detail": self.detail,
            "outcome": self.outcome.value,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CaptureCheck":
        allowed = {
            "context_fingerprint", "recorded_at", "transport", "visual_validity",
            "operator_visible_change", "upstream_freshness", "source", "detail", "outcome",
        }
        if not isinstance(data, Mapping) or set(data) - allowed:
            raise CompatibilityError("invalid capture check fields")
        item = cls(**{key: value for key, value in data.items() if key != "outcome"})
        if data.get("outcome", item.outcome.value) != item.outcome.value:
            raise CompatibilityError("stored capture outcome is inconsistent")
        return item


@dataclass(frozen=True)
class CompatibilityProfile:
    calibration_profile: str
    context: DesktopContext
    declared_operations: tuple[str, ...] = OPERATIONS
    evidence: tuple[ProbeEvidence, ...] = ()
    alignment_evidence: tuple[AlignmentEvidence, ...] = ()
    capture_checks: tuple[CaptureCheck, ...] = ()

    def __post_init__(self) -> None:
        filename = _text(self.calibration_profile, "calibration_profile")
        scripts_path(filename)
        object.__setattr__(self, "calibration_profile", filename)
        if not isinstance(self.context, DesktopContext):
            raise CompatibilityError("profile context must be DesktopContext")
        operations = tuple(self.declared_operations)
        if not operations or len(set(operations)) != len(operations):
            raise CompatibilityError("declared_operations must be unique and non-empty")
        unknown = set(operations) - set(OPERATIONS)
        if unknown:
            raise CompatibilityError("unknown declared operations: " + ", ".join(sorted(unknown)))
        object.__setattr__(self, "declared_operations", operations)
        evidence = tuple(self.evidence)
        if len(evidence) > MAX_EVIDENCE or any(not isinstance(item, ProbeEvidence) for item in evidence):
            raise CompatibilityError(f"profile may contain at most {MAX_EVIDENCE} evidence records")
        object.__setattr__(self, "evidence", evidence)
        alignment = tuple(self.alignment_evidence)
        checks = tuple(self.capture_checks)
        if len(alignment) > 64 or any(not isinstance(item, AlignmentEvidence) for item in alignment):
            raise CompatibilityError("profile may contain at most 64 alignment records")
        if len(checks) > 64 or any(not isinstance(item, CaptureCheck) for item in checks):
            raise CompatibilityError("profile may contain at most 64 capture checks")
        object.__setattr__(self, "alignment_evidence", alignment)
        object.__setattr__(self, "capture_checks", checks)
        capture_width, capture_height = self.context.capture_size
        input_width, input_height = self.context.input_size
        for item in alignment:
            if (item.capture_point[0] >= capture_width
                    or item.capture_point[1] >= capture_height):
                raise CompatibilityError("capture alignment evidence lies outside calibrated bounds")
            if item.input_point[0] >= input_width or item.input_point[1] >= input_height:
                raise CompatibilityError("input alignment evidence lies outside calibrated bounds")

    def add_evidence(self, item: ProbeEvidence) -> "CompatibilityProfile":
        if not isinstance(item, ProbeEvidence):
            raise TypeError("item must be ProbeEvidence")
        if item.operation not in self.declared_operations:
            raise CompatibilityError(f"operation {item.operation!r} is not declared")
        return CompatibilityProfile(
            self.calibration_profile,
            self.context,
            self.declared_operations,
            (*self.evidence, item)[-MAX_EVIDENCE:],
            self.alignment_evidence,
            self.capture_checks,
        )

    def add_alignment(self, item: AlignmentEvidence) -> "CompatibilityProfile":
        if not isinstance(item, AlignmentEvidence):
            raise TypeError("item must be AlignmentEvidence")
        for label, point, size in (
            ("capture", item.capture_point, self.context.capture_size),
            ("input", item.input_point, self.context.input_size),
        ):
            width, height = size
            if point[0] >= width or point[1] >= height:
                raise CompatibilityError(f"{label} alignment point lies outside calibrated bounds")
        return CompatibilityProfile(
            self.calibration_profile, self.context, self.declared_operations, self.evidence,
            (*self.alignment_evidence, item)[-64:], self.capture_checks,
        )

    def add_capture_check(self, item: CaptureCheck) -> "CompatibilityProfile":
        return CompatibilityProfile(
            self.calibration_profile, self.context, self.declared_operations, self.evidence,
            self.alignment_evidence, (*self.capture_checks, item)[-64:],
        )

    def capture_outcome(self) -> Outcome:
        for item in reversed(self.capture_checks):
            if item.context_fingerprint == self.context.fingerprint:
                return item.outcome
        return Outcome.UNVERIFIED

    def alignment_outcome(self) -> Outcome:
        width, height = self.context.capture_size
        current: dict[tuple[int, int], AlignmentEvidence] = {}
        for item in self.alignment_evidence:
            if item.context_fingerprint == self.context.fingerprint:
                current[item.capture_point] = item
        passing = [point for point, item in current.items() if item.outcome is Outcome.PASS]
        if any(item.outcome is Outcome.FAIL for item in current.values()):
            return Outcome.FAIL
        if len(passing) < 5:
            return Outcome.UNVERIFIED
        center = any(abs(x - (width - 1) / 2) <= width * .15 and
                     abs(y - (height - 1) / 2) <= height * .15 for x, y in passing)
        covered = all((
            any(x <= (width - 1) * .25 and y <= (height - 1) * .25 for x, y in passing),
            any(x >= (width - 1) * .75 and y <= (height - 1) * .25 for x, y in passing),
            any(x <= (width - 1) * .25 and y >= (height - 1) * .75 for x, y in passing),
            any(x >= (width - 1) * .75 and y >= (height - 1) * .75 for x, y in passing),
        ))
        return Outcome.PASS if center and covered else Outcome.UNVERIFIED

    def operation_outcome(self, operation: str) -> Outcome:
        if operation not in self.declared_operations:
            return Outcome.UNVERIFIED
        for item in reversed(self.evidence):
            if item.operation == operation and item.context_fingerprint == self.context.fingerprint:
                # Older schema-v1 records may not contain the optional key.
                # Keep those usable, but never let an explicitly recorded key
                # satisfy the other press category.
                if operation in ("press", "modified_press") and item.key:
                    try:
                        if key_operation(item.key) != operation:
                            continue
                    except CompatibilityError:
                        continue
                result = item.outcome
                if operation == "cursor_restore" and result is Outcome.PASS:
                    if (
                        self.context.environment.input_mode != "physical"
                        or not (
                            item.cursor_original is not None
                            and item.cursor_original == item.cursor_final
                        )
                        and item.cursor_restore_confirmation is not Outcome.PASS
                    ):
                        return Outcome.UNVERIFIED
                return result
        return Outcome.UNVERIFIED

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "version": SCHEMA_VERSION,
            "calibration_profile": self.calibration_profile,
            "context": self.context.to_dict(),
            "context_fingerprint": self.context.fingerprint,
            "declared_operations": list(self.declared_operations),
            "evidence": [item.to_dict() for item in self.evidence],
            "alignment_evidence": [item.to_dict() for item in self.alignment_evidence],
            "capture_checks": [item.to_dict() for item in self.capture_checks],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CompatibilityProfile":
        if not isinstance(data, Mapping):
            raise CompatibilityError("compatibility profile root must be an object")
        expected = {
            "schema", "version", "calibration_profile", "context",
            "context_fingerprint", "declared_operations", "evidence",
            "alignment_evidence", "capture_checks",
        }
        if set(data) != expected:
            raise CompatibilityError("compatibility profile has missing or unknown fields")
        if (
            data["schema"] != SCHEMA
            or type(data["version"]) is not int
            or data["version"] != SCHEMA_VERSION
        ):
            raise CompatibilityError("unsupported compatibility profile schema/version")
        context = DesktopContext.from_dict(data["context"])
        if data["context_fingerprint"] != context.fingerprint:
            raise CompatibilityError("context fingerprint does not match profile content")
        raw_evidence = data["evidence"]
        if not isinstance(raw_evidence, list) or len(raw_evidence) > MAX_EVIDENCE:
            raise CompatibilityError(f"evidence must be a list of at most {MAX_EVIDENCE} records")
        return cls(
            data["calibration_profile"],
            context,
            tuple(data["declared_operations"]),
            tuple(ProbeEvidence.from_dict(item) for item in raw_evidence),
            tuple(AlignmentEvidence.from_dict(item) for item in data["alignment_evidence"]),
            tuple(CaptureCheck.from_dict(item) for item in data["capture_checks"]),
        )


def compatibility_filename(calibration_profile: str) -> str:
    path = scripts_path(calibration_profile)
    return path.stem + ".compatibility.json"


def _read_bounded(path: Path) -> Mapping[str, Any]:
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise CompatibilityError(f"{path.name} is not a regular file")
            encoded = stream.read(MAX_PROFILE_BYTES + 1)
            if len(encoded) > MAX_PROFILE_BYTES or stream.read(1):
                raise CompatibilityError(
                    f"{path.name} exceeds the {MAX_PROFILE_BYTES}-byte limit"
                )
        value = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=_json_pairs,
            parse_constant=_invalid_constant,
        )
    except CompatibilityError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise CompatibilityError(f"cannot read compatibility profile {path.name}: {exc}") from exc
    if not isinstance(value, Mapping):
        raise CompatibilityError("compatibility profile root must be an object")
    return value


def load_profile(filename: str) -> CompatibilityProfile:
    return CompatibilityProfile.from_dict(_read_bounded(scripts_path(filename)))


def save_profile(
    profile: CompatibilityProfile, filename: str | None = None, *, overwrite: bool = False
) -> Path:
    if not isinstance(profile, CompatibilityProfile):
        raise TypeError("profile must be CompatibilityProfile")
    selected = filename or compatibility_filename(profile.calibration_profile)
    path = scripts_path(selected)
    if path.exists() and not overwrite:
        raise FileExistsError(f"refusing to overwrite existing profile: {path.name}")
    payload = profile.to_dict()
    encoded = json.dumps(payload, indent=2, ensure_ascii=True).encode("ascii") + b"\n"
    if len(encoded) > MAX_PROFILE_BYTES:
        raise CompatibilityError(f"profile exceeds the {MAX_PROFILE_BYTES}-byte limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=".compat-", delete=False) as out:
            temporary = Path(out.name)
            out.write(encoded)
            out.flush()
            os.fsync(out.fileno())
        if overwrite:
            os.replace(temporary, path)
            temporary = None
        else:
            # Hard-link publication is atomic and fails if a competing writer
            # created the destination after the earlier existence check.
            try:
                os.link(temporary, path)
            except FileExistsError:
                raise FileExistsError(f"refusing to overwrite existing profile: {path.name}")
            temporary.unlink()
            temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path


@dataclass(frozen=True)
class ActivationCheck:
    allowed: bool
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        reasons = tuple(_text(item, "activation reason") for item in self.reasons)
        object.__setattr__(self, "reasons", reasons)
        if self.allowed and reasons:
            raise CompatibilityError("an allowed activation check cannot contain blocking reasons")
        if not self.allowed and not reasons:
            raise CompatibilityError("a blocked activation check needs an actionable reason")

    def require(self) -> None:
        if not self.allowed:
            raise CompatibilityError("; ".join(self.reasons))


def validate_profile_context(
    profile: CompatibilityProfile,
    current_context: DesktopContext,
    required_operations: Iterable[str] = (),
) -> ActivationCheck:
    if not isinstance(profile, CompatibilityProfile):
        raise TypeError("profile must be CompatibilityProfile")
    if not isinstance(current_context, DesktopContext):
        raise TypeError("current_context must be DesktopContext")
    reasons: list[str] = []
    if profile.context.fingerprint != current_context.fingerprint:
        reasons.append("desktop environment or calibration changed; rerun compatibility probes")
    if not current_context.aligned:
        reasons.append("capture and input client areas must have identical dimensions for 1:1 mapping")
    capture = profile.capture_outcome()
    if capture is not Outcome.PASS:
        reasons.append(f"capture visible-change check is {capture.value} for this context")
    alignment = profile.alignment_outcome()
    if alignment is not Outcome.PASS:
        reasons.append(f"multi-point 1:1 alignment is {alignment.value} for this context")
    try:
        operations = tuple(required_operations)
    except TypeError:
        operations = ()
        reasons.append("feature required_operations() did not return an iterable")
    if (
        current_context.environment.input_mode == "physical"
        and any(
            isinstance(operation, str) and operation in _PHYSICAL_CURSOR_OPERATIONS
            for operation in operations
        )
    ):
        # Do not hash feature-provided values here. The normal validation loop
        # below reports malformed/unknown entries without turning the guard
        # itself into an exception.
        operations = (*operations, "cursor_restore")
    for operation in operations:
        if operation not in OPERATIONS:
            reasons.append(f"feature requires unknown operation {operation!r}")
        elif operation not in profile.declared_operations:
            reasons.append(f"operation {operation!r} is not declared by this profile")
        else:
            outcome = profile.operation_outcome(operation)
            if outcome is not Outcome.PASS:
                reasons.append(f"operation {operation!r} is {outcome.value} for this context")
    return ActivationCheck(not reasons, tuple(dict.fromkeys(reasons)))


def activation_guard(
    profile: CompatibilityProfile, current_context: DesktopContext
) -> Callable[[object], ActivationCheck]:
    """Return the guard accepted by ``ClientSession(activation_guard=...)``."""

    def guard(feature: object) -> ActivationCheck:
        provider = getattr(feature, "required_operations", None)
        if provider is None:
            required: tuple[str, ...] = ()
        elif callable(provider):
            required = provider()
            if not isinstance(required, tuple):
                return ActivationCheck(False, ("feature required_operations() must return a tuple",))
        else:
            return ActivationCheck(False, ("feature required_operations must be callable",))
        return validate_profile_context(profile, current_context, required)

    return guard


def _validate_probe_operation(operation: str, action: Action) -> None:
    if operation not in OPERATIONS:
        raise CompatibilityError(f"unknown operation {operation!r}")
    kinds = {
        "press": ActionKind.PRESS,
        "modified_press": ActionKind.PRESS,
        "left_click": ActionKind.LEFT_CLICK,
        "right_click": ActionKind.RIGHT_CLICK,
        "drag": ActionKind.DRAG,
        "cursor_restore": ActionKind.LEFT_CLICK,
        "battle_list_click": ActionKind.LEFT_CLICK,
    }
    if action.kind is not kinds[operation]:
        raise CompatibilityError(f"operation {operation!r} requires action kind {kinds[operation].value}")
    if action.kind is ActionKind.PRESS:
        actual = key_operation(action.key)
        if actual != operation:
            raise CompatibilityError(
                f"hotkey {action.key!r} is a {actual} probe, not {operation}"
            )


class ProbeFeature:
    """Operator-started policy that emits one and only one SDK Action."""

    name = "CompatibilityProbe"
    interval = 0.05

    def __init__(
        self,
        operation: str | None,
        action: Action | None,
        *,
        sensors: tuple[Sensor, ...] = (),
        confirmation: Confirmation | None = None,
        reason: str = "operator-started compatibility probe",
        evidence_source: EvidenceSource = EvidenceSource.SIMULATION,
        intended_target: str = "",
        context_guard: Callable[[], object] | None = None,
    ) -> None:
        if action is None:
            if operation not in (None, "capture_preview"):
                raise CompatibilityError("an input operation requires an SDK Action")
            operation = "capture_preview"
        else:
            if not isinstance(action, Action):
                raise TypeError("action must be an SDK Action")
            if operation is None:
                raise CompatibilityError("action probes require an operation")
            _validate_probe_operation(operation, action)
        sensors = tuple(sensors)
        if len(sensors) > 64 or any(not isinstance(sensor, Sensor) for sensor in sensors):
            raise CompatibilityError("probe sensors must contain at most 64 Sensor values")
        if confirmation is not None and not isinstance(confirmation, Confirmation):
            raise TypeError("confirmation must be an SDK Confirmation")
        if confirmation is not None and confirmation.sensor not in sensors:
            sensors = (*sensors, confirmation.sensor)
        self._frame_sensor = Sensor(SensorKind.FRAME_VALID)
        if self._frame_sensor not in sensors:
            sensors = (*sensors, self._frame_sensor)
        self.operation = operation
        self.action = action
        self._sensors = sensors
        self.confirmation = confirmation
        self.reason = _text(reason, "probe reason")
        self.evidence_source = EvidenceSource(evidence_source)
        self.intended_target = _text(intended_target, "intended target", optional=True)
        if context_guard is not None and not callable(context_guard):
            raise TypeError("context_guard must be callable")
        self._context_guard = context_guard
        self._issued = False
        self._terminal = False
        self.result: ActionResult | None = None
        self.dispatch_outcome = Outcome.UNVERIFIED
        self.automatic_outcome = Outcome.UNVERIFIED
        self.before: dict[Sensor, Any] = {}
        self.after: dict[Sensor, Any] = {}
        self.observations: Mapping[Sensor, Observation] = MappingProxyType({})
        self.cursor_original: tuple[int, int] | None = None
        self.cursor_final: tuple[int, int] | None = None

    def required_operations(self) -> tuple[str, ...]:
        return () if self.action is None else (self.operation,)

    def probe_context_check(self) -> object:
        """Revalidate the composition-owned live binding before dispatch."""

        return True if self._context_guard is None else self._context_guard()

    def sensors(self) -> tuple[Sensor, ...]:
        return self._sensors

    @staticmethod
    def _values(context: FeatureContext, sensors: tuple[Sensor, ...]) -> dict[Sensor, Any]:
        values: dict[Sensor, Any] = {}
        for sensor in sensors:
            observation = context.snapshot.observations.get(sensor)
            values[sensor] = None if observation is None else observation.value
        return values

    def tick(self, context: FeatureContext) -> Step:
        if not self._issued:
            # Preserve quality, age and frame identity for detector inspection,
            # even after the scheduler retires this one-shot capture policy.
            self.observations = MappingProxyType({
                sensor: observation for sensor in self._sensors
                if (observation := context.snapshot.observations.get(sensor)) is not None
            })
            self.before = self._values(context, self._sensors)
            self._issued = True
            if self.action is None:
                self.after = dict(self.before)
                self._terminal = True
                return Step(phase="Fresh capture preview ready", finished=True)
            return Step(
                requests=(Request(
                    "compatibility-probe",
                    self.action,
                    Priority.BACKGROUND,
                    ttl=1.0,
                    requirements=(
                        (Requirement(self._frame_sensor, 0.5),)
                        if self.action.kind is not ActionKind.PRESS else ()
                    ),
                    confirmation=self.confirmation,
                    reason=self.reason,
                ),),
                phase=f"Dispatching {self.operation}",
            )
        if self._terminal:
            self.after = self._values(context, self._sensors)
            return Step(phase="Probe complete — confirm the visible result", finished=True)
        return Step(phase="Waiting for bounded action result")

    def on_result(self, result: ActionResult) -> None:
        if not isinstance(result, ActionResult):
            raise TypeError("result must be ActionResult")
        self.result = result
        if result.state in (ResultState.EXECUTED, ResultState.CONFIRMED):
            self.dispatch_outcome = Outcome.PASS
        elif result.state is ResultState.FAILED and self.dispatch_outcome is not Outcome.PASS:
            self.dispatch_outcome = Outcome.FAIL
        if result.state is ResultState.CONFIRMED:
            self.automatic_outcome = Outcome.PASS
        elif self.confirmation is not None and result.final and result.state is ResultState.FAILED:
            self.automatic_outcome = Outcome.FAIL
        self._terminal = result.final

    def on_dispatch_metadata(
        self,
        *,
        cursor_original: tuple[int, int] | None = None,
        cursor_final: tuple[int, int] | None = None,
    ) -> None:
        """Receive immutable adapter telemetry through the session boundary."""

        for name, value in (("cursor_original", cursor_original), ("cursor_final", cursor_final)):
            if value is not None:
                point = tuple(value)
                if len(point) != 2 or any(type(item) is not int for item in point):
                    raise CompatibilityError(f"{name} metadata must be an integer coordinate pair")
                setattr(self, name, point)

    def evidence(
        self,
        context: DesktopContext,
        *,
        operator_confirmation: Outcome = Outcome.UNVERIFIED,
        detail: str = "",
        cursor_restore_confirmation: Outcome = Outcome.UNVERIFIED,
    ) -> ProbeEvidence:
        if self.action is None:
            raise CompatibilityError("capture-only preview does not create action evidence")
        dispatch = self.dispatch_outcome
        automatic = self.automatic_outcome
        if (
            self.operation == "cursor_restore"
            and self.cursor_original is not None
            and self.cursor_final is not None
        ):
            automatic = (
                Outcome.PASS
                if self.cursor_original == self.cursor_final
                else Outcome.FAIL
            )
        point = self.action.point
        def summarize(values: Mapping[Sensor, Any]) -> tuple[str, ...]:
            result = []
            for sensor, value in values.items():
                rendered = repr(value)
                if len(rendered) > 240:
                    rendered = rendered[:237] + "..."
                result.append(f"{sensor.kind.value}:{sensor.name or '-'}={rendered}")
            return tuple(result)
        return ProbeEvidence(
            operation=self.operation,
            context_fingerprint=context.fingerprint,
            recorded_at=datetime.now(timezone.utc).isoformat(),
            dispatch=dispatch,
            automatic_confirmation=automatic,
            operator_confirmation=Outcome(operator_confirmation),
            detail=detail or (self.result.reason if self.result is not None else "probe has no result"),
            point=point,
            source=self.evidence_source,
            cursor_original=self.cursor_original,
            cursor_final=self.cursor_final,
            key=self.action.key,
            destination=self.action.destination,
            intended_target=self.intended_target,
            before=summarize(self.before),
            after=summarize(self.after),
            cursor_restore_confirmation=Outcome(cursor_restore_confirmation),
        )


def export_evidence_bundle(
    profile: CompatibilityProfile,
    destination: str | Path,
    *,
    frame_files: Sequence[str | Path] = (),
    trace_file: str | Path | None = None,
) -> Path:
    """Create a non-overwriting bundle; frames remain separate from trace v1."""

    if not isinstance(profile, CompatibilityProfile):
        raise TypeError("profile must be CompatibilityProfile")
    target = Path(destination).expanduser()
    if target.exists():
        raise FileExistsError(f"refusing to overwrite existing export: {target}")
    if target.name in ("", ".", ".."):
        raise CompatibilityError("export destination must name a new directory")
    frames = tuple(Path(item).expanduser() for item in frame_files)
    if len(frames) > MAX_FRAMES:
        raise CompatibilityError(f"at most {MAX_FRAMES} frame files may be exported")
    sources: list[tuple[Any, str, int]] = []
    total = 0
    names: set[str] = set()
    opened: list[Any] = []
    trace: tuple[Any, int] | None = None

    def open_source(source: Path, limit: int, label: str) -> tuple[Any, int]:
        if source.is_symlink():
            raise CompatibilityError(f"{label} must not be a symlink: {source}")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            stream = os.fdopen(os.open(source, flags), "rb")
        except OSError as exc:
            raise CompatibilityError(f"cannot open {label} {source}: {exc}") from exc
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            stream.close()
            raise CompatibilityError(f"{label} must be a regular file of at most {limit} bytes")
        opened.append(stream)
        return stream, info.st_size

    try:
        for source in frames:
            if source.suffix.lower() not in (".png", ".jpg", ".jpeg", ".bmp"):
                raise CompatibilityError(f"unsupported frame extension: {source.suffix}")
            safe_name = source.name
            if safe_name in names or safe_name in ("", ".", ".."):
                raise CompatibilityError(f"duplicate or unsafe frame filename: {safe_name}")
            names.add(safe_name)
            stream, size = open_source(source, MAX_FRAME_BYTES, "frame")
            sources.append((stream, safe_name, size))
            total += size
        if trace_file is not None:
            stream, size = open_source(Path(trace_file).expanduser(), MAX_EXPORT_BYTES, "trace")
            trace = (stream, size)
            total += size
        if total > MAX_EXPORT_BYTES:
            raise CompatibilityError(f"export files exceed the {MAX_EXPORT_BYTES}-byte limit")

        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
        (temporary / "frames").mkdir()
        for stream, name, size in sources:
            copied = temporary / "frames" / name
            with copied.open("xb") as output:
                remaining = size
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise CompatibilityError(f"frame changed while exporting: {name}")
                    output.write(chunk)
                    remaining -= len(chunk)
                if stream.read(1):
                    raise CompatibilityError(f"frame grew while exporting: {name}")
            try:
                from PIL import Image

                with Image.open(copied) as image:
                    if image.width <= 0 or image.height <= 0 or image.width * image.height > 16_000_000:
                        raise CompatibilityError(f"frame dimensions are unsafe: {name}")
                    image.verify()
            except CompatibilityError:
                raise
            except Exception as exc:
                raise CompatibilityError(f"frame is not a valid image: {name}: {exc}") from exc
        if trace is not None:
            stream, size = trace
            with (temporary / "trace-v1.jsonl").open("xb") as output:
                remaining = size
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise CompatibilityError("trace changed while exporting")
                    output.write(chunk)
                    remaining -= len(chunk)
                if stream.read(1):
                    raise CompatibilityError("trace grew while exporting")
            from tibia_client.trace import load_trace

            load_trace(temporary / "trace-v1.jsonl", max_bytes=MAX_EXPORT_BYTES)
        manifest = {
            "schema": "tibiaauto.compatibility-evidence-bundle",
            "version": 1,
            "profile": profile.to_dict(),
            "frames": [f"frames/{name}" for _, name, _ in sources],
            "trace": None if trace is None else "trace-v1.jsonl",
            "note": "Frames are opt-in files separate from the observation-only trace v1.",
        }
        encoded = json.dumps(manifest, indent=2).encode("utf-8") + b"\n"
        if len(encoded) > MAX_PROFILE_BYTES:
            raise CompatibilityError("evidence manifest exceeds its bounded JSON limit")
        with (temporary / "evidence.json").open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        # Claim the destination without an overwrite race. The manifest moves
        # last, so a concurrent reader never mistakes a partial directory for
        # a complete bundle.
        target.mkdir()
        try:
            os.rename(temporary / "frames", target / "frames")
            trace_path = temporary / "trace-v1.jsonl"
            if trace_path.exists():
                os.rename(trace_path, target / "trace-v1.jsonl")
            os.rename(temporary / "evidence.json", target / "evidence.json")
            temporary.rmdir()
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)
            raise
    except BaseException:
        if "temporary" in locals():
            shutil.rmtree(temporary, ignore_errors=True)
        raise
    finally:
        for stream in opened:
            stream.close()
    return target


def load_current_context(
    calibration_profile: str, environment: EnvironmentMetadata, *, input_size=None
) -> DesktopContext:
    return context_from_calibration(
        ConfManager.get(calibration_profile), environment, input_size=input_size
    )


__all__ = [
    "ActivationCheck", "AlignmentEvidence", "CaptureCheck", "CompatibilityError",
    "CompatibilityProfile", "DesktopContext", "EnvironmentMetadata", "EvidenceSource",
    "OPERATIONS", "Outcome", "ProbeEvidence", "ProbeFeature",
    "activation_guard", "calibration_digest", "compatibility_filename",
    "context_from_calibration", "export_evidence_bundle", "load_current_context",
    "key_operation", "load_profile", "save_profile", "validate_profile_context",
]
