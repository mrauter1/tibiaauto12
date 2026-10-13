"""Composition-owned compatibility gate for a calibrated desktop session.

Durable evidence lives in a sidecar; live handles stay in conf.json. Changes to
any bound file latch the gate shut until the operator explicitly reloads. The
hot path checks file identity only, never parses JSON or invokes native input.
External OBS/client settings are operator-reported metadata, not autodetected.
"""
from __future__ import annotations

from dataclasses import dataclass
import threading
from pathlib import Path
from typing import Callable

from conf.conf_manager import ConfManager
from tibia_client.compatibility import (
    ActivationCheck, CaptureCheck, CompatibilityProfile, DesktopContext, EvidenceSource, Outcome,
    compatibility_filename, load_current_context, load_profile, validate_profile_context,
)
from tibia_client.diagnostics import AdapterCapabilities


def _stamp(path: Path):
    try:
        stat = path.stat()
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    except FileNotFoundError:
        return None


@dataclass(frozen=True)
class _ProbeBinding:
    fingerprint: str
    stamps: tuple
    previous_checks: tuple


class DesktopProfileController:
    def __init__(self, calibration_profile: str, *, input_size: Callable[[], tuple[int, int]],
                 capabilities: AdapterCapabilities = AdapterCapabilities()):
        self.calibration_profile = calibration_profile
        self.filename = compatibility_filename(calibration_profile)
        self._paths = tuple(ConfManager.path(name) for name in
                            (calibration_profile, "conf.json", self.filename))
        self._input_size = input_size
        self._capabilities = capabilities
        self._lock = threading.RLock()
        self.profile: CompatibilityProfile | None = None
        self.context: DesktopContext | None = None
        self._stamps = ()
        self._binding_key = None
        self._probe_token = None
        self._reason = "Open Compatibility Test to create and verify a desktop profile"
        self.reload()

    def reload(self) -> ActivationCheck:
        """Explicitly bind a saved profile to the current calibrated windows."""
        with self._lock:
            try:
                before = tuple(_stamp(path) for path in self._paths)
                profile = load_profile(self.filename)
                if profile.calibration_profile != self.calibration_profile:
                    raise ValueError("compatibility sidecar belongs to a different calibration")
                calibration = ConfManager.get(self.calibration_profile)
                config = ConfManager.get("conf.json")
                if config.get("preferences_name") != self.calibration_profile:
                    raise ValueError("selected calibration changed; reopen the client launcher")
                if config.get("configured") is not True:
                    raise ValueError("select and calibrate the capture and input windows first")
                expected_mode = "physical" if calibration.get("MouseOption", 1) == 1 else "messages"
                if profile.context.environment.input_mode != expected_mode:
                    raise ValueError("profile input mode differs from the selected input backend")
                for field in ("capture_title", "input_title"):
                    selected_title = config.get(field)
                    if not isinstance(selected_title, str) or not selected_title.strip():
                        raise ValueError("reselect capture/input windows to record their titles")
                    if getattr(profile.context.environment, field, "") != selected_title:
                        raise ValueError(f"selected {field} differs from tested metadata; reset and retest")
                if (profile.context.environment.capture_mode == "obs_projector"
                        and config.get("capture_hwnd") == config.get("input_hwnd")):
                    raise ValueError("OBS projector and input must be separate selected windows")
                context = load_current_context(self.calibration_profile, profile.context.environment,
                                               input_size=self._input_size())
                if tuple(config.get("capture_size", ())) != context.capture_size:
                    raise ValueError("capture size differs from calibration; recalibrate the client")
                for name in ("capture_hwnd", "input_hwnd"):
                    handle = config.get(name, config.get("hwnd"))
                    if type(handle) is not int or handle <= 0:
                        raise ValueError(f"select a valid {name} before testing")
                after = tuple(_stamp(path) for path in self._paths)
                if before != after:
                    raise ValueError("configuration changed while loading; reload again")
                self.profile, self.context = profile, context
                if self._binding_key != (context.fingerprint, after[:2]):
                    self._binding_key = None
                self._stamps, self._reason = after, None
            except Exception as exc:
                self.profile, self.context = None, None
                self._stamps = ()
                self._binding_key = None
                self._reason = f"Compatibility profile unavailable: {exc}"
            return self.check()

    def check(self, required_operations=()) -> ActivationCheck:
        with self._lock:
            if self._reason:
                return ActivationCheck(False, (self._reason,))
            try:
                changed = tuple(_stamp(path) for path in self._paths) != self._stamps
            except OSError as exc:
                self._reason = f"Cannot check compatibility files: {exc}"
                return ActivationCheck(False, (self._reason,))
            if changed:
                if tuple(_stamp(path) for path in self._paths[:2]) != self._stamps[:2]:
                    self._binding_key = None
                self._reason = "Profile, calibration or window binding changed; reload Compatibility Test"
                return ActivationCheck(False, (self._reason,))
            if self.profile is None or self.context is None:
                return ActivationCheck(False, ("No compatibility profile loaded",))
            required = tuple(required_operations)
            if (self.context.environment.input_mode == "physical"
                    and set(required).intersection({"left_click", "right_click", "drag", "battle_list_click"})):
                required = tuple(dict.fromkeys((*required, "cursor_restore")))
            check = validate_profile_context(self.profile, self.context, required)
            reasons = check.reasons
            if self._binding_key != (self.context.fingerprint, self._stamps[:2]):
                reasons = (*reasons, "Verify a visible capture change for the selected windows this session")
            return ActivationCheck(not reasons, reasons)

    def begin_probe(self):
        """Bind one operator probe to current files without bypassing geometry checks."""
        with self._lock:
            if self.profile is None or self.context is None or self._reason:
                raise ValueError(self._reason or "Load a desktop profile before probing")
            if tuple(_stamp(path) for path in self._paths) != self._stamps:
                raise ValueError("profile or window selection changed; reload before probing")
            if self.profile.context.fingerprint != self.context.fingerprint:
                raise ValueError("calibration changed; reset the compatibility profile before probing")
            if not self.context.aligned:
                raise ValueError("capture/input geometry differs; reselect and recalibrate")
            token = _ProbeBinding(self.context.fingerprint, self._stamps, self.profile.capture_checks)
            self._probe_token = token
            return token

    def validate_probe_token(self, token) -> ActivationCheck:
        with self._lock:
            valid = (token is not None and token is self._probe_token
                     and self.context is not None and token.fingerprint == self.context.fingerprint
                     and token.stamps == tuple(_stamp(path) for path in self._paths))
            return ActivationCheck(True) if valid else ActivationCheck(False, (
                "probe context changed or was superseded; reload and arm a fresh probe",))

    def confirm_binding(self, token, check: CaptureCheck) -> ActivationCheck:
        """Accept a new live visible-change result, never a saved historical check."""
        with self._lock:
            if (token is None or token is not self._probe_token or self.context is None
                    or token.fingerprint != self.context.fingerprint
                    or token.stamps[:2] != tuple(_stamp(path) for path in self._paths[:2])
                    or self._stamps != tuple(_stamp(path) for path in self._paths)):
                raise ValueError("probe bindings or profile changed before confirmation")
            if (not isinstance(check, CaptureCheck) or self.profile is None
                    or check.source is not EvidenceSource.LIVE or check.outcome is not Outcome.PASS
                    or check.context_fingerprint != token.fingerprint
                    or check not in self.profile.capture_checks or check in token.previous_checks):
                raise ValueError("binding requires a newly collected live passing capture check")
            self._binding_key = (token.fingerprint, token.stamps[:2])
            self._probe_token = None
            return ActivationCheck(True)

    def __call__(self, feature) -> ActivationCheck:
        method = getattr(feature, "required_operations", None)
        if not callable(method):
            return ActivationCheck(False, ("feature must declare required_operations()",))
        operations = method()
        if not isinstance(operations, tuple):
            return ActivationCheck(False, ("feature required_operations() must return a tuple",))
        return self.check(operations)

    def status(self):
        with self._lock:
            check = self.check()
            profile, context = self.profile, self.context
            operations = () if not check.allowed or profile is None else tuple(
                op for op in self._capabilities.operations
                if self.check((op,)).allowed
            )
            return {
                "profile_id": None if profile is None else profile.context.fingerprint,
                "calibration_id": None if context is None else context.calibration_digest,
                "ready": check.allowed,
                "capabilities": (*operations, *(f"sensor.{s}" for s in self._capabilities.sensors))
                if check.allowed else (),
                "problems": check.reasons,
            }
