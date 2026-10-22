"""Explicit, non-blocking desktop compatibility studio.

Opening the window performs no capture or input. Every capture and one-action
probe is routed through ``ClientSession.start_probe`` and a guarded controller
token. Trace payloads and captured frames are displayed/exported only; they are
never replayed by this module.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path
import queue
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any, Mapping

from PIL import Image, ImageTk

from conf.conf_manager import ConfManager
from tibia_client.compatibility import (
    AlignmentEvidence,
    CaptureCheck,
    CompatibilityProfile,
    EnvironmentMetadata,
    EvidenceSource,
    OPERATIONS,
    Outcome,
    ProbeEvidence,
    ProbeFeature,
    compatibility_filename,
    context_from_calibration,
    export_evidence_bundle,
    key_operation,
    load_profile,
    save_profile,
)
from tibia_client.models import Action, ActionKind, ResultState, SensorKind
from tibia_client.trace_view import export_session_trace


_POINT_OPERATIONS = {
    "left_click", "right_click", "drag", "cursor_restore", "battle_list_click"
}
_WINDOWS: dict[int, "Compatibility"] = {}


def _master(parent: object) -> object:
    return getattr(parent, "windowID", parent)


@dataclass(frozen=True)
class PreviewMapping:
    """Aspect-preserving mapping between an immutable frame and a canvas."""

    canvas_size: tuple[int, int]
    frame_size: tuple[int, int]
    padding: int = 10

    def __post_init__(self) -> None:
        if any(type(n) is not int or n <= 0 for n in (*self.canvas_size, *self.frame_size)):
            raise ValueError("canvas and frame sizes must contain positive integers")
        if type(self.padding) is not int or self.padding < 0:
            raise ValueError("padding must be a non-negative integer")
        if min(self.canvas_size) <= self.padding * 2:
            raise ValueError("canvas is too small for its padding")

    @property
    def bounds(self) -> tuple[int, int, int, int]:
        cw, ch = self.canvas_size
        fw, fh = self.frame_size
        scale = min((cw - 2 * self.padding) / fw, (ch - 2 * self.padding) / fh)
        width, height = max(1, round(fw * scale)), max(1, round(fh * scale))
        return (cw - width) // 2, (ch - height) // 2, width, height

    def contains(self, x: float, y: float) -> bool:
        left, top, width, height = self.bounds
        return left <= x <= left + width - 1 and top <= y <= top + height - 1

    def client_point(self, x: float, y: float) -> tuple[int, int]:
        if not self.contains(x, y):
            raise ValueError("select a point inside the captured client image")
        left, top, width, height = self.bounds
        fw, fh = self.frame_size
        nx = 0 if width == 1 else (x - left) / (width - 1)
        ny = 0 if height == 1 else (y - top) / (height - 1)
        return round(nx * (fw - 1)), round(ny * (fh - 1))

    def canvas_point(self, point: tuple[int, int]) -> tuple[float, float]:
        fw, fh = self.frame_size
        x, y = point
        if not (0 <= x < fw and 0 <= y < fh):
            raise ValueError("client point lies outside the frame")
        left, top, width, height = self.bounds
        return (
            left if fw == 1 else left + x / (fw - 1) * (width - 1),
            top if fh == 1 else top + y / (fh - 1) * (height - 1),
        )


def probe_completion(status: Any, probe: ProbeFeature, *, capture_only: bool) -> tuple[str, str]:
    """Classify a polled probe, including stop/failure paths with no result."""
    if bool(getattr(status, "active", False)):
        return "running", str(getattr(status, "phase", "Running") or "Running")
    if capture_only:
        if getattr(status, "error", None):
            return "failed", str(status.error)
        return (("complete", "fresh capture completed") if probe.after else
                ("failed", "capture stopped before post-capture observations"))
    if probe.result is None:
        return "failed", str(getattr(status, "error", None) or
                             "probe stopped before producing an action result")
    if not probe.result.final:
        return "failed", "probe stopped with a non-final action result"
    if probe.result.state in (ResultState.REJECTED, ResultState.CANCELLED, ResultState.FAILED):
        return "complete", "terminal action result recorded"
    if not probe.after:
        return "failed", "probe stopped before post-action observations completed"
    return "complete", "post-action observations completed"


def _frame_valid(probe: ProbeFeature) -> bool:
    return any(sensor.kind is SensorKind.FRAME_VALID and value is True
               for sensor, value in probe.before.items())


def _add_probe_evidence(
    profile: CompatibilityProfile, evidence: ProbeEvidence
) -> CompatibilityProfile:
    """Add a deliberately tested supported operation and its evidence together."""
    if evidence.operation not in profile.declared_operations:
        profile = replace(
            profile,
            declared_operations=(*profile.declared_operations, evidence.operation),
        )
    return profile.add_evidence(evidence)


def _current_alignment_points(
    profile: CompatibilityProfile, outcome: Outcome,
) -> tuple[tuple[int, int], ...]:
    """Return points whose latest alignment result has the requested outcome."""
    current: dict[tuple[int, int], AlignmentEvidence] = {}
    for item in profile.alignment_evidence:
        if item.context_fingerprint == profile.context.fingerprint:
            current[item.capture_point] = item
    return tuple(
        point for point, item in current.items() if item.outcome is outcome
    )


def _current_alignment_failures(
    profile: CompatibilityProfile,
) -> tuple[tuple[int, int], ...]:
    return _current_alignment_points(profile, Outcome.FAIL)


def _window_titles(configuration: Mapping[str, Any]) -> tuple[str, str]:
    values = configuration.get("capture_title", ""), configuration.get("input_title", "")
    return tuple(value.strip() if isinstance(value, str) else "" for value in values)  # type: ignore[return-value]


def _blocking_reasons(check: Any) -> tuple[str, ...]:
    if check is None or bool(getattr(check, "allowed", False)):
        return ()
    return tuple(str(item) for item in getattr(check, "reasons", ()) if str(item))


def _context_problems(profile: CompatibilityProfile, context: Any, check: Any) -> tuple[str, ...]:
    if context is None:
        return _blocking_reasons(check) or ("no current desktop context is loaded",)
    if (
        getattr(context, "fingerprint", None) != profile.context.fingerprint
        or not bool(getattr(context, "aligned", False))
    ):
        return _blocking_reasons(check) or ("current desktop context differs from the profile",)
    return ()


class Compatibility:
    """Guided capture, alignment, and input evidence UI."""

    def __new__(cls, parent, session, profile_filename: str, controller=None):
        key = id(session)
        current = _WINDOWS.get(key)
        if current is not None:
            try:
                if current.window.winfo_exists():
                    current.window.deiconify()
                    current.window.lift()
                    return current
            except (AttributeError, tk.TclError):
                pass
        instance = super().__new__(cls)
        instance._window_key = key
        _WINDOWS[key] = instance
        return instance

    def __init__(self, parent, session, profile_filename: str, controller=None) -> None:
        if getattr(self, "_initialized", False):
            return
        self._initialized = True
        self.session, self.controller = session, controller
        self.profile_filename = profile_filename
        self.sidecar_filename = compatibility_filename(profile_filename)
        self.profile: CompatibilityProfile | None = None
        self.pending_probe: ProbeFeature | None = None
        self.pending_kind: str | None = None
        self.pending_point: tuple[int, int] | None = None
        self.pending_token: Any = None
        self._probe_generation: int | None = None
        self._probe_started = self._awaiting_confirmation = self._cancel_requested = False
        self._cancel_stop_pending = False
        self._closed, self._countdown_id = False, None
        self._calibration: Mapping[str, Any] = {}
        self._preview_frame = self._preview_mapping = self._photo = None
        self._preview_context = ""
        self.selected_point: tuple[int, int] | None = None
        self.drag_destination: tuple[int, int] | None = None
        self._capture_before: tuple[ProbeFeature, Any] | None = None
        self._capture_confirmation: tuple[ProbeFeature, ProbeFeature, Any] | None = None

        self.window = tk.Toplevel(_master(parent))
        self.window.title("Desktop Compatibility Studio")
        self.window.geometry("920x850")
        self.window.minsize(780, 650)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.body = ttk.Frame(self.window, padding=10)
        self.body.pack(fill="both", expand=True)
        self.status = tk.StringVar(value="Loading profile…")
        self.identity = tk.StringVar(value="Window identities unavailable")
        self.operation, self.hotkey = tk.StringVar(value="press"), tk.StringVar(value="F12")
        self.target_label = tk.StringVar(value="visible UI control at the selected point")
        self.alignment_confirmation = tk.StringVar(value=Outcome.UNVERIFIED.value)
        self.cursor_confirmation = tk.StringVar(value=Outcome.UNVERIFIED.value)
        self.include_frame = tk.BooleanVar(value=False)
        self._metadata_vars: dict[str, tk.StringVar] = {}
        self._build()
        self.reload()
        self.window.after(100, self._poll)

    def _build(self) -> None:
        ttk.Label(self.body, wraplength=880, text=(
            "Opening this studio performs no capture or input. Use an explicit button, foreground "
            "the configured game, and keep it foregrounded until post-probe capture completes."
        )).pack(anchor="w")
        ttk.Label(self.body, textvariable=self.status, wraplength=880).pack(fill="x", pady=4)
        ttk.Label(self.body, textvariable=self.identity, wraplength=880).pack(anchor="w")

        bar = ttk.Frame(self.body)
        bar.pack(fill="x", pady=5)
        self.preview_button = ttk.Button(bar, text="Capture preview", command=self.capture_preview)
        self.preview_button.pack(side="left")
        self.capture_before_button = ttk.Button(
            bar, text="Start visible-change capture check", command=self.capture_change_before)
        self.capture_before_button.pack(side="left", padx=5)
        self.capture_after_button = ttk.Button(
            bar, text="Capture after manual change", command=self.capture_change_after,
            state="disabled")
        self.capture_after_button.pack(side="left")
        self.cancel_button = ttk.Button(
            bar, text="Cancel pending probe", command=self.cancel_probe, state="disabled")
        self.cancel_button.pack(side="right")

        self.canvas = tk.Canvas(self.body, width=720, height=390, bg="#151515", highlightthickness=1)
        self.canvas.pack(fill="both", expand=True, pady=5)
        self.canvas.bind("<Button-1>", self._select_canvas_point)
        self.canvas.bind("<Configure>", lambda _event: self._render_preview())

        probe = ttk.LabelFrame(self.body, text="One bounded input probe", padding=7)
        probe.pack(fill="x")
        ttk.Label(probe, text="Operation").grid(row=0, column=0, sticky="w")
        self.operation_box = ttk.Combobox(
            probe, textvariable=self.operation, values=OPERATIONS,
            state="readonly", width=20)
        self.operation_box.grid(row=0, column=1, sticky="w")
        ttk.Label(probe, text="Key").grid(row=0, column=2, sticky="e")
        self.key_entry = ttk.Entry(probe, textvariable=self.hotkey, width=14)
        self.key_entry.grid(row=0, column=3, sticky="w")
        ttk.Label(probe, text="Intended visible target").grid(row=1, column=0, sticky="w")
        self.target_entry = ttk.Entry(probe, textvariable=self.target_label, width=65)
        self.target_entry.grid(row=1, column=1, columnspan=3, sticky="ew")
        self.arm_button = ttk.Button(probe, text="Arm selected probe", command=self.arm_probe)
        self.arm_button.grid(row=2, column=0, pady=5)
        self.pass_button = ttk.Button(
            probe, text="Game response: PASS", command=lambda: self.confirm(Outcome.PASS))
        self.pass_button.grid(row=2, column=1)
        self.fail_button = ttk.Button(
            probe, text="Game response: FAIL", command=lambda: self.confirm(Outcome.FAIL))
        self.fail_button.grid(row=2, column=2)
        ttk.Label(probe, text=(
            "Alignment (separate): did input hit exactly the selected capture point?"
        )).grid(row=3, column=0, columnspan=2, sticky="w")
        self.alignment_box = ttk.Combobox(
            probe, textvariable=self.alignment_confirmation,
            values=tuple(item.value for item in Outcome), state="disabled", width=12)
        self.alignment_box.grid(row=3, column=2)
        ttk.Label(probe, text="Cursor returned to original client point?").grid(
            row=4, column=0, columnspan=2, sticky="w")
        self.cursor_box = ttk.Combobox(
            probe, textvariable=self.cursor_confirmation,
            values=tuple(item.value for item in Outcome), state="disabled", width=12)
        self.cursor_box.grid(row=4, column=2)
        ttk.Label(probe, text="Drag: click source, then Shift-click the distinct destination.").grid(
            row=5, column=0, columnspan=4, sticky="w")
        self.operation_note = tk.StringVar(value="")
        ttk.Label(probe, textvariable=self.operation_note, wraplength=850).grid(
            row=6, column=0, columnspan=4, sticky="w")
        probe.columnconfigure(1, weight=1)
        self._set_confirm_enabled(False)

        self.capture_confirm = ttk.Frame(self.body)
        ttk.Label(self.capture_confirm, text=(
            "Does AFTER show the manually triggered change in the current client?"
        )).pack(side="left")
        ttk.Button(self.capture_confirm, text="Current-client update: PASS",
                   command=lambda: self.confirm_capture(Outcome.PASS)).pack(side="left", padx=5)
        ttk.Button(self.capture_confirm, text="Current-client update: FAIL",
                   command=lambda: self.confirm_capture(Outcome.FAIL)).pack(side="left")

        actions = ttk.Frame(self.body)
        actions.pack(fill="x", pady=5)
        ttk.Button(actions, text="Reload", command=self.reload).pack(side="left")
        ttk.Button(actions, text="Edit / reset metadata…", command=self.edit_metadata).pack(
            side="left", padx=4)
        ttk.Button(actions, text="Save displayed frame…", command=self.save_displayed_frame).pack(
            side="left")
        ttk.Checkbutton(actions, text="Include displayed frame", variable=self.include_frame).pack(
            side="left", padx=(10, 2))
        ttk.Button(actions, text="Export evidence bundle…", command=self.export_bundle).pack(side="left")
        self._build_metadata()

    def _build_metadata(self) -> None:
        self.metadata = ttk.LabelFrame(self.body, text="Environment metadata", padding=8)
        fields = (
            ("client_build", ""), ("ot_server", ""), ("windows_version", ""),
            ("renderer", ""), ("obs_version", ""), ("obs_configuration", ""),
            ("dpi_scaling", "100%"), ("layout", "default"), ("input_mode", "physical"),
            ("capture_mode", "sandbox_screen" if
             ConfManager.get("conf.json").get("window_target", {}).get("kind") == "sandbox"
             else "obs_projector"), ("battleye_state", "unknown"),
            ("battleye_provenance", "operator-reported"),
            ("test_date", date.today().isoformat()),
        )
        for index, (name, default) in enumerate(fields):
            variable = tk.StringVar(value=default)
            self._metadata_vars[name] = variable
            row, column = divmod(index, 4)
            ttk.Label(self.metadata, text=name.replace("_", " ").title()).grid(
                row=row * 2, column=column, sticky="w", padx=3)
            choices = {"input_mode": ("physical", "messages"),
                       "capture_mode": ("obs_projector", "direct_client", "sandbox_screen"),
                       "battleye_state": ("unknown", "enabled", "disabled")}.get(name)
            widget = (ttk.Combobox(self.metadata, textvariable=variable, values=choices,
                                   state="readonly", width=18) if choices else
                      ttk.Entry(self.metadata, textvariable=variable, width=21))
            widget.grid(row=row * 2 + 1, column=column, padx=3, pady=(0, 4))
        ttk.Label(self.metadata, wraplength=850, text=(
            "Capture/input titles come from the current chooser configuration and cannot be typed "
            "here. Replacing metadata resets all evidence for the old context."
        )).grid(row=8, column=0, columnspan=4, sticky="w")
        ttk.Button(self.metadata, text="Create new profile", command=self.create).grid(
            row=9, column=0, columnspan=2)
        ttk.Button(self.metadata, text="Replace profile and reset evidence",
                   command=self.reset_profile).grid(row=9, column=2, columnspan=2)

    def _set_confirm_enabled(self, enabled: bool, operation: str | None = None) -> None:
        state = "normal" if enabled else "disabled"
        self.pass_button.configure(state=state)
        self.fail_button.configure(state=state)
        operation = operation or self.operation.get()
        self.alignment_box.configure(
            state="readonly" if enabled and operation in _POINT_OPERATIONS else "disabled")
        self.cursor_box.configure(
            state="readonly" if enabled and operation == "cursor_restore" else "disabled")

    def _controller_value(self, name: str) -> Any:
        value = getattr(self.controller, name, None) if self.controller is not None else None
        return value() if callable(value) else value

    def _source(self) -> EvidenceSource:
        callback = getattr(self.session, "probe_evidence_source", None)
        return (EvidenceSource.LIVE if callable(callback) and callback() == "live"
                else EvidenceSource.SIMULATION)

    def _begin_probe(self) -> tuple[Any, Any]:
        if self.controller is None:
            return None, None
        begin = getattr(self.controller, "begin_probe", None)
        validate = getattr(self.controller, "validate_probe_token", None)
        if not callable(begin) or not callable(validate):
            raise RuntimeError("compatibility controller does not provide guarded probe tokens")
        token = begin()
        return token, lambda token=token: validate(token)

    def _validate_token(self, token: Any) -> None:
        if self.controller is None:
            return
        validate = getattr(self.controller, "validate_probe_token", None)
        if not callable(validate):
            raise RuntimeError("compatibility controller cannot validate the probe context")
        result = validate(token)
        if hasattr(result, "allowed") and not result.allowed:
            reasons = "; ".join(str(item) for item in getattr(result, "reasons", ()))
            raise RuntimeError(reasons or "probe context changed before confirmation")

    def _refresh_identity(self) -> None:
        try:
            capture, input_title = _window_titles(ConfManager.get("conf.json"))
        except Exception:
            capture = input_title = ""
        self.identity.set(f"Configured capture title: {capture or '<missing>'}  |  "
                          f"input title: {input_title or '<missing>'}")

    def reload(self) -> bool:
        if self.pending_probe is not None or self._capture_before is not None:
            self.status.set("Cancel or finish the pending probe before reloading context.")
            return False
        loaded = False
        try:
            if self.controller is not None:
                check = self.controller.reload()
                selected = self._controller_value("profile")
            else:
                check = None
                selected = load_profile(self.sidecar_filename)
            if not isinstance(selected, CompatibilityProfile):
                reasons = _blocking_reasons(check)
                raise ValueError(
                    "; ".join(reasons) if reasons else "no compatibility profile is loaded"
                )
            self.profile = selected
            self._calibration = ConfManager.get(self.profile_filename)
            self._load_metadata(selected.context.environment)
            self.metadata.pack_forget()
            self._invalidate_preview()
            problems = (
                _context_problems(selected, self._controller_value("context"), check)
                if self.controller is not None else ()
            )
            self.status.set(
                self._profile_status()
                + (
                    " Current live context is blocked: " + "; ".join(problems) + "."
                    if problems else ""
                )
            )
            loaded = not problems
        except Exception as exc:
            self.profile, self._calibration = None, {}
            self.metadata.pack(fill="x", pady=5)
            self._invalidate_preview()
            self.status.set(f"Profile unavailable: {exc}. Create or explicitly reset it below.")
        self._refresh_identity()
        self._render_preview()
        return loaded

    def _load_metadata(self, environment: EnvironmentMetadata) -> None:
        for name, variable in self._metadata_vars.items():
            variable.set(str(getattr(environment, name)))
        self._update_operation_choices(environment.input_mode)

    def _update_operation_choices(self, input_mode: str) -> None:
        physical = input_mode == "physical"
        operations = OPERATIONS if physical else tuple(
            operation for operation in OPERATIONS if operation != "cursor_restore"
        )
        self.operation_box.configure(values=operations)
        if self.operation.get() not in operations:
            self.operation.set("press")
        self.operation_note.set(
            "Cursor restore is available only with physical input and is not required in "
            "message mode."
            if not physical else ""
        )

    def edit_metadata(self) -> None:
        if self.pending_probe is not None or self._capture_before is not None:
            self.status.set("Cancel or finish the pending probe/capture check before editing metadata.")
            return
        if self.profile is not None:
            self._load_metadata(self.profile.context.environment)
        self.metadata.pack(fill="x", pady=5)
        self.status.set("Saving changed metadata requires explicit replacement and resets old evidence.")

    def _new_profile(self) -> CompatibilityProfile:
        configuration = ConfManager.get("conf.json")
        capture_title, input_title = _window_titles(configuration)
        environment = EnvironmentMetadata(
            **{name: variable.get() for name, variable in self._metadata_vars.items()},
            capture_title=capture_title, input_title=input_title)
        calibration = ConfManager.get(self.profile_filename)
        current_context = getattr(self.controller, "current_context", None)
        context = (
            current_context(environment)
            if callable(current_context)
            else context_from_calibration(calibration, environment)
        )
        if not context.aligned:
            raise ValueError(
                f"capture size {context.capture_size} and current input size "
                f"{context.input_size} differ; resize or reselect the windows, then recalibrate"
            )
        return CompatibilityProfile(self.profile_filename, context)

    def create(self) -> None:
        if self.pending_probe is not None or self._capture_before is not None:
            self.status.set("Cancel or finish the pending probe/capture check before creating a profile.")
            return
        try:
            save_profile(self._new_profile(), self.sidecar_filename)
            if self.reload():
                self.status.set("Profile created. Capture, alignment, and operations are unverified.")
        except Exception as exc:
            self.status.set(f"Create failed: {exc}. Use Replace only if intended.")

    def reset_profile(self) -> None:
        if self.pending_probe is not None or self._capture_before is not None:
            self.status.set("Cancel or finish the pending probe/capture check before replacing metadata.")
            return
        if not messagebox.askyesno("Replace compatibility profile?", (
            "Replace the saved context and permanently reset all capture, alignment, and operation evidence?"
        ), parent=self.window):
            self.status.set("Profile replacement cancelled; saved evidence was unchanged.")
            return
        try:
            save_profile(self._new_profile(), self.sidecar_filename, overwrite=True)
            if self.reload():
                self.status.set("Profile replaced. All evidence is unverified for the new context.")
        except Exception as exc:
            self.status.set(f"Profile replacement failed: {exc}")

    def _profile_status(self) -> str:
        assert self.profile is not None
        operations = ", ".join(
            f"{name}={self.profile.operation_outcome(name).value}"
            for name in self.profile.declared_operations)
        preview = (
            " Preview is ready for another probe."
            if self._preview_ready()
            else " Capture a fresh preview before arming input."
        )
        return (f"Capture={self.profile.capture_outcome().value}; "
                f"alignment={self.profile.alignment_outcome().value}; {operations}." + preview)

    def _invalidate_preview(self) -> None:
        self._preview_frame = self._preview_mapping = self._photo = None
        self._preview_context, self.selected_point, self.drag_destination = "", None, None
        self._capture_before = self._capture_confirmation = None
        self.capture_after_button.configure(state="disabled")
        self.capture_confirm.pack_forget()
        self._update_controls()

    def _preview_ready(self) -> bool:
        return bool(self.profile and self._preview_frame is not None
                    and self._preview_context == self.profile.context.fingerprint
                    and tuple(self._preview_frame.size) == self.profile.context.capture_size)

    def _update_controls(self) -> None:
        busy = self.pending_probe is not None or self._capture_before is not None
        state = "disabled" if busy else "normal"
        self.preview_button.configure(state=state)
        self.capture_before_button.configure(state=state)
        self.cancel_button.configure(state="normal" if busy else "disabled")
        self.arm_button.configure(
            state="normal" if not busy and self._preview_ready() else "disabled")
        self.operation_box.configure(state="disabled" if busy else "readonly")
        self.key_entry.configure(state="disabled" if busy else "normal")
        self.target_entry.configure(state="disabled" if busy else "normal")

    def capture_preview(self) -> None:
        self._arm_capture("preview")

    def capture_change_before(self) -> None:
        self._capture_before = self._capture_confirmation = None
        self.capture_confirm.pack_forget()
        self._arm_capture("capture_before")

    def capture_change_after(self) -> None:
        if self._capture_before is None:
            self.status.set("Capture the BEFORE frame first.")
            return
        self._arm_capture("capture_after")

    def _arm_capture(self, kind: str) -> None:
        if (self.profile is None or self.pending_probe is not None
                or (kind != "capture_after" and self._capture_before is not None)):
            self.status.set("Load a profile and finish the current probe first.")
            return
        try:
            token, guard = self._begin_probe()
            probe = ProbeFeature("capture_preview", None, evidence_source=self._source(),
                                 reason="operator-started capture preview", context_guard=guard)
            self._queue_probe(probe, kind, token)
        except Exception as exc:
            self.status.set(f"Cannot start capture: {exc}")

    def _action(self) -> Action:
        operation = self.operation.get()
        point = self.selected_point if operation in _POINT_OPERATIONS else None
        if operation in _POINT_OPERATIONS and point is None:
            raise ValueError("select a point inside the captured client preview")
        if operation in ("press", "modified_press"):
            return Action(ActionKind.PRESS, key=self.hotkey.get().strip())
        if operation == "right_click":
            return Action(ActionKind.RIGHT_CLICK, point=point)
        if operation == "drag":
            if self.drag_destination is None or self.drag_destination == point:
                raise ValueError("Shift-click a distinct drag destination in the preview")
            return Action(ActionKind.DRAG, point=point, destination=self.drag_destination)
        return Action(ActionKind.LEFT_CLICK, point=point)

    def arm_probe(self) -> None:
        if not self._preview_ready() or self.profile is None:
            self.status.set("Capture a fresh matching client preview before arming input.")
            return
        try:
            operation = self.operation.get()
            if (
                operation == "cursor_restore"
                and self.profile.context.environment.input_mode != "physical"
            ):
                self.status.set(
                    "Cursor restore is physical-input only and is not required in message mode."
                )
                return
            if operation in ("press", "modified_press"):
                operation = key_operation(self.hotkey.get())
                self.operation.set(operation)
            token, guard = self._begin_probe()
            probe = ProbeFeature(operation, self._action(), evidence_source=self._source(),
                                 intended_target=self.target_label.get(), context_guard=guard)
            self.pending_point = self.selected_point if operation in _POINT_OPERATIONS else None
            self.alignment_confirmation.set(Outcome.UNVERIFIED.value)
            self.cursor_confirmation.set(Outcome.UNVERIFIED.value)
            self._queue_probe(probe, "input", token)
        except Exception as exc:
            self.pending_point = None
            self.status.set(f"Cannot arm probe: {exc}")

    def _queue_probe(self, probe: ProbeFeature, kind: str, token: Any) -> None:
        self.pending_probe, self.pending_kind, self.pending_token = probe, kind, token
        self._probe_generation = None
        self._probe_started = self._awaiting_confirmation = self._cancel_requested = False
        self._cancel_stop_pending = False
        self._set_confirm_enabled(False)
        self.capture_after_button.configure(state="disabled")
        self._update_controls()
        self._countdown(3)

    def _countdown(self, remaining: int) -> None:
        if self._closed or self.pending_probe is None:
            return
        if remaining:
            purpose = "capture" if self.pending_probe.action is None else self.pending_probe.operation
            declaration = ""
            if (
                self.pending_probe.action is not None
                and self.profile is not None
                and self.pending_probe.operation not in self.profile.declared_operations
            ):
                declaration = (
                    f" {self.pending_probe.operation} will be added to this profile only if "
                    "you confirm the completed probe."
                )
            self.status.set(f"ARMED — foreground the configured game. One {purpose} in {remaining}… "
                            "Keep it foregrounded until completion is reported here."
                            + declaration)
            self._countdown_id = self.window.after(1000, self._countdown, remaining - 1)
            return
        self._countdown_id = None
        try:
            self._probe_generation = self.session.start_probe(self.pending_probe)
            self._probe_started = True
            self.status.set("Probe started. Keep the game foregrounded through post-probe capture.")
        except Exception as exc:
            self.status.set(f"Probe did not start: {exc}")
            self._clear_pending()

    def _poll(self) -> None:
        if self._closed:
            return
        probe = self.pending_probe
        if probe is not None and self._probe_started:
            try:
                state, detail = probe_completion(
                    self.session.feature_status(ProbeFeature.name), probe,
                    capture_only=probe.action is None)
                if state == "running":
                    if self._cancel_requested:
                        message = (
                            "Cancellation requested; waiting for the stop operation to finish."
                            if self._cancel_stop_pending else
                            "The probe is still active; retry cancellation."
                        )
                        self.status.set(message)
                    else:
                        self.status.set(f"{detail}. Keep the configured game foregrounded.")
                elif self._cancel_requested:
                    if self._cancel_stop_pending:
                        self.status.set(
                            "Probe is inactive; finishing cancellation before retry is enabled."
                        )
                    else:
                        self.status.set(
                            "Probe discarded after cancellation; no evidence was saved. "
                            "You may retry."
                        )
                        self._clear_pending()
                elif state == "failed":
                    self.status.set(f"Probe incomplete: {detail}. No evidence was saved; retry.")
                    self._clear_pending()
                elif not self._awaiting_confirmation:
                    self._complete_probe(probe)
            except Exception as exc:
                if self._cancel_requested and self._cancel_stop_pending:
                    self.status.set(
                        f"Probe status failed during cancellation: {exc}. "
                        "Waiting for the stop operation to finish."
                    )
                else:
                    self.status.set(f"Probe status failed: {exc}. No evidence was saved; retry.")
                    self._clear_pending()
        try:
            self.window.after(100, self._poll)
        except tk.TclError:
            pass

    def _complete_probe(self, probe: ProbeFeature) -> None:
        if probe.action is None:
            callback = getattr(self.session, "preview_frame", None)
            frame = callback() if callable(callback) else None
            if frame is None or self.profile is None:
                self.status.set("Capture completed without an immutable preview frame; retry.")
                self._clear_pending()
                return
            if tuple(frame.size) != self.profile.context.capture_size:
                self.status.set(f"Captured size {frame.size} does not match calibrated "
                                f"{self.profile.context.capture_size}; reselect/recalibrate.")
                self._clear_pending()
                self._invalidate_preview()
                return
            kind, token = self.pending_kind, self.pending_token
            self._set_preview(frame)
            if kind == "capture_before":
                self._capture_before = (probe, frame)
                self.status.set("BEFORE captured. Keep the game foregrounded, manually cause a "
                                "visible change, then capture AFTER.")
                self._clear_pending()
                self.capture_after_button.configure(state="normal")
            elif kind == "capture_after" and self._capture_before is not None:
                self._capture_confirmation = (self._capture_before[0], probe, token)
                self.status.set("AFTER captured. Explicitly confirm whether it shows the current-client change.")
                self._clear_pending(preserve_capture=True)
                self.capture_confirm.pack(fill="x", pady=4)
            else:
                self.status.set("Fresh preview ready; select an intended client-local point.")
                self._clear_pending()
            return
        dispatch = probe.dispatch_outcome.value
        automatic = probe.automatic_outcome.value
        observed = ("post-action observations complete" if probe.after else
                    "terminal result recorded; no post-action observation was available")
        action = probe.action
        action_detail = (
            f"key={action.key!r}" if action and action.kind is ActionKind.PRESS else
            f"point={action.point!r}, destination={action.destination!r}"
        )
        refreshed = self._refresh_after_action_preview(probe)
        self.status.set(
            f"{probe.operation} ({action_detail}) at target {probe.intended_target!r}: "
            f"OS dispatch={dispatch}; automatic confirmation={automatic}; {observed}. "
            + (
                "The displayed preview was refreshed from the post-action capture. "
                if refreshed else ""
            )
            + "You may now assess this exact armed target.")
        self._awaiting_confirmation = True
        self._set_confirm_enabled(True, probe.operation)
        self.cancel_button.configure(state="normal")

    def _clear_pending(self, *, preserve_capture: bool = False) -> None:
        if self._countdown_id is not None:
            try:
                self.window.after_cancel(self._countdown_id)
            except tk.TclError:
                pass
        self._countdown_id = None
        self.pending_probe = self.pending_kind = self.pending_point = self.pending_token = None
        self._probe_generation = None
        self._probe_started = self._awaiting_confirmation = self._cancel_requested = False
        self._cancel_stop_pending = False
        self._set_confirm_enabled(False)
        if not preserve_capture:
            self.capture_confirm.pack_forget()
        self.capture_after_button.configure(
            state="normal" if self._capture_before and not self._capture_confirmation else "disabled")
        self._update_controls()

    def cancel_probe(self) -> None:
        if self.pending_probe is None:
            if self._capture_before is not None:
                self._capture_before = self._capture_confirmation = None
                self.capture_confirm.pack_forget()
                self.capture_after_button.configure(state="disabled")
                self.status.set("Visible-change capture check discarded; no evidence was saved.")
                self._update_controls()
            return
        if not self._probe_started:
            self.status.set("Countdown cancelled. No capture, input, or evidence occurred.")
            self._clear_pending()
            return
        if self._awaiting_confirmation:
            self.status.set("Completed probe discarded; no evidence was saved. You may retry.")
            self._clear_pending()
            return
        if self._cancel_requested and self._cancel_stop_pending:
            self.status.set("Cancellation already requested; waiting for the probe to stop.")
            return
        self._cancel_requested = True
        self._cancel_stop_pending = True
        self._set_confirm_enabled(False)
        self.cancel_button.configure(state="disabled")
        self.status.set("Cancelling; waiting for inactive status…")
        probe = self.pending_probe
        generation = self._probe_generation

        def stopped(_value, error):
            if self.pending_probe is probe:
                self._cancel_stop_pending = False
                self.status.set(
                    f"Cancel failed: {error}" if error else
                    "Cancellation requested; waiting for inactive status."
                )
                if error:
                    self.cancel_button.configure(state="normal")

        self._run_background(
            lambda: self.session.stop_feature(
                ProbeFeature.name, expected_generation=generation
            ),
            stopped,
        )

    def _refresh_after_action_preview(self, probe: ProbeFeature) -> bool:
        """Display the post-action frame when it still matches the armed context."""
        callback = getattr(self.session, "preview_frame", None)
        if (
            self.pending_probe is not probe
            or not any(
                sensor.kind is SensorKind.FRAME_VALID and value is True
                for sensor, value in probe.after.items()
            )
            or not callable(callback)
            or self.profile is None
        ):
            return False
        try:
            self._validate_token(self.pending_token)
            frame = callback()
            if frame is None or tuple(frame.size) != self.profile.context.capture_size:
                return False
            self._validate_token(self.pending_token)
            self._set_preview(frame)
        except Exception:
            return False
        return True

    def _set_preview(self, frame: Any) -> None:
        self._preview_frame = frame
        self._preview_context = self.profile.context.fingerprint if self.profile else ""
        if self.selected_point is None:
            self.selected_point = ((frame.width - 1) // 2, (frame.height - 1) // 2)
        if self.drag_destination is not None:
            if not (0 <= self.drag_destination[0] < frame.width
                    and 0 <= self.drag_destination[1] < frame.height):
                self.drag_destination = None
        self._render_preview()
        self._update_controls()

    def _render_preview(self) -> None:
        self.canvas.delete("all")
        if self._preview_frame is None:
            self.canvas.create_text(
                max(1, self.canvas.winfo_width()) / 2,
                max(1, self.canvas.winfo_height()) / 2,
                fill="white", text="No capture yet — Capture preview sends no input")
            return
        canvas_size = (max(2, self.canvas.winfo_width()), max(2, self.canvas.winfo_height()))
        try:
            mapping = PreviewMapping(canvas_size, tuple(self._preview_frame.size))
        except ValueError:
            return
        self._preview_mapping = mapping
        left, top, width, height = mapping.bounds
        image = self._preview_frame.image.resize((width, height), Image.Resampling.LANCZOS)
        self._photo = ImageTk.PhotoImage(image, master=self.window)
        self.canvas.create_image(left, top, image=self._photo, anchor="nw")
        self._draw_overlays(mapping)
        if self.selected_point is not None:
            try:
                x, y = mapping.canvas_point(self.selected_point)
                self.canvas.create_oval(
                    x - 8, y - 8, x + 8, y + 8, outline="#ff4f78", width=2)
                self.canvas.create_text(
                    x + 10, y - 10, fill="white", anchor="sw",
                    text=f"selected {self.selected_point[0]},{self.selected_point[1]}")
            except ValueError:
                pass
        if self.drag_destination is not None:
            try:
                dx, dy = mapping.canvas_point(self.drag_destination)
                if self.selected_point is not None:
                    sx, sy = mapping.canvas_point(self.selected_point)
                    self.canvas.create_line(sx, sy, dx, dy, fill="#53b7ff", width=2, arrow="last")
                self.canvas.create_rectangle(
                    dx - 7, dy - 7, dx + 7, dy + 7, outline="#53b7ff", width=2)
                self.canvas.create_text(dx + 9, dy, fill="white", anchor="w",
                                        text=f"destination {self.drag_destination}")
            except ValueError:
                pass

    def _draw_overlays(self, mapping: PreviewMapping) -> None:
        colors = ("#65d6ff", "#ffcb61", "#8ee68e", "#d9a7ff")
        boxes = self._calibration.get("Boxes", {})
        if isinstance(boxes, Mapping):
            for index, (name, raw) in enumerate(boxes.items()):
                try:
                    item = raw[0]
                    if item.get("Stats") is not True:
                        continue
                    first = mapping.canvas_point((item["x"], item["y"]))
                    second = mapping.canvas_point((item["w"] - 1, item["h"] - 1))
                except (KeyError, TypeError, ValueError, IndexError):
                    continue
                color = colors[index % len(colors)]
                self.canvas.create_rectangle(*first, *second, outline=color)
                self.canvas.create_text(
                    first[0] + 3, first[1] + 3, text=name, fill=color, anchor="nw")
        index = 0
        for group in ("Positions", "SQM"):
            points = self._calibration.get(group, {})
            if not isinstance(points, Mapping):
                continue
            for name, raw in points.items():
                try:
                    item = raw[0]
                    if item.get("Stats") is not True:
                        continue
                    x, y = mapping.canvas_point((item["x"], item["y"]))
                except (KeyError, TypeError, ValueError, IndexError):
                    continue
                color = colors[index % len(colors)]
                index += 1
                self.canvas.create_line(x - 4, y, x + 4, y, fill=color)
                self.canvas.create_line(x, y - 4, x, y + 4, fill=color)
                self.canvas.create_text(x + 5, y + 5, text=name, fill=color, anchor="nw")
        # These five spread locations are the explicit M0 alignment sample set.
        # They are guides on the captured image, not proof of input alignment.
        passed: set[tuple[int, int]] = set()
        if self.profile is not None:
            passed = set(_current_alignment_points(self.profile, Outcome.PASS))
        fw, fh = mapping.frame_size
        for number, (nx, ny) in enumerate(
            ((.1, .1), (.9, .1), (.5, .5), (.1, .9), (.9, .9)), 1
        ):
            point = round(nx * (fw - 1)), round(ny * (fh - 1))
            x, y = mapping.canvas_point(point)
            color = "#4fd16f" if point in passed else "#f0b64d"
            self.canvas.create_oval(x - 6, y - 6, x + 6, y + 6, outline=color, width=2)
            self.canvas.create_text(x, y + 9, text=str(number), fill=color, anchor="n")
        if self.profile is not None:
            for point in _current_alignment_failures(self.profile):
                try:
                    x, y = mapping.canvas_point(point)
                except ValueError:
                    continue
                self.canvas.create_line(x - 7, y - 7, x + 7, y + 7, fill="#ff5b5b", width=2)
                self.canvas.create_line(x - 7, y + 7, x + 7, y - 7, fill="#ff5b5b", width=2)
                self.canvas.create_text(
                    x + 9, y, text=f"failed {point[0]},{point[1]} — click to retry",
                    fill="#ff5b5b", anchor="w")

    def _select_canvas_point(self, event) -> None:
        if self.pending_probe is not None or self._capture_before is not None:
            self.status.set("Selection is locked until the armed probe/capture check is finished or cancelled.")
            return
        if self._preview_mapping is None:
            self.status.set("Capture a client preview before selecting a point.")
            return
        try:
            point = self._preview_mapping.client_point(event.x, event.y)
        except ValueError as exc:
            self.status.set(str(exc))
            return
        drag_destination = self.operation.get() == "drag" and bool(event.state & 0x0001)
        if not drag_destination and self.profile is not None:
            nearby: list[tuple[float, tuple[int, int]]] = []
            for failed in _current_alignment_failures(self.profile):
                try:
                    x, y = self._preview_mapping.canvas_point(failed)
                except ValueError:
                    continue
                distance = (event.x - x) ** 2 + (event.y - y) ** 2
                if distance <= 10 ** 2:
                    nearby.append((distance, failed))
            if nearby:
                point = min(nearby, key=lambda item: item[0])[1]
        if drag_destination:
            self.drag_destination = point
            self.status.set(f"Selected drag destination {point}; source is {self.selected_point}.")
        else:
            self.selected_point = point
            self.status.set(
                f"Selected capture point {point}; the 1:1 mapped input point is exactly {point}. "
                "Alignment still requires separate confirmation.")
        self._render_preview()

    def confirm(self, result: Outcome) -> None:
        if (
            self.profile is None
            or self.pending_probe is None
            or not self._awaiting_confirmation
            or self._cancel_requested
        ):
            return
        try:
            probe = self.pending_probe
            self._validate_token(self.pending_token)
            alignment = Outcome(self.alignment_confirmation.get())
            evidence = probe.evidence(
                self.profile.context,
                operator_confirmation=result,
                detail=f"operator assessed visible response at {probe.intended_target}",
                cursor_restore_confirmation=Outcome(self.cursor_confirmation.get()),
            )
            if alignment is Outcome.PASS and evidence.dispatch is not Outcome.PASS:
                raise ValueError("alignment cannot pass when OS dispatch did not pass")
            updated = _add_probe_evidence(self.profile, evidence)
            if self.pending_point is not None and alignment is not Outcome.UNVERIFIED:
                updated = updated.add_alignment(AlignmentEvidence(
                    updated.context.fingerprint,
                    datetime.now(timezone.utc).isoformat(),
                    self.pending_point, self.pending_point, alignment, evidence.source,
                    "operator separately assessed exact equality of capture and mapped input point",
                ))
        except Exception as exc:
            self.status.set(f"Could not save evidence: {exc}")
            return
        try:
            save_profile(updated, self.sidecar_filename, overwrite=True)
        except Exception as exc:
            self.status.set(f"Could not save evidence: {exc}")
            return
        problems = self._reload_saved_profile(updated)
        if problems:
            self.status.set(
                "Evidence was saved, but the current live context could not be reloaded: "
                + "; ".join(problems)
                + ". Reload after fixing the selected windows or calibration."
            )
        else:
            self.status.set(self._profile_status())
        self._clear_pending()
        self._render_preview()

    def confirm_capture(self, result: Outcome) -> None:
        if self.profile is None or self._capture_confirmation is None:
            return
        before, after, token = self._capture_confirmation
        try:
            self._validate_token(token)
            check = CaptureCheck(
                self.profile.context.fingerprint,
                datetime.now(timezone.utc).isoformat(),
                Outcome.PASS,
                Outcome.PASS if _frame_valid(before) and _frame_valid(after) else Outcome.FAIL,
                result,
                Outcome.UNVERIFIED,
                after.evidence_source,
                "two explicit capture-only probes; operator manually triggered and assessed a "
                "current-client visible change; continuous source freshness remains unknown",
            )
            updated = self.profile.add_capture_check(check)
        except Exception as exc:
            self.status.set(f"Could not save capture evidence / binding: {exc}")
            return
        try:
            save_profile(updated, self.sidecar_filename, overwrite=True)
        except Exception as exc:
            self.status.set(f"Could not save capture evidence: {exc}")
            return
        problems = list(self._reload_saved_profile(updated))
        note = ""
        if not problems and check.outcome is Outcome.PASS and check.source is EvidenceSource.LIVE:
            try:
                confirm = getattr(self.controller, "confirm_binding", None)
                if not callable(confirm):
                    raise RuntimeError("compatibility controller cannot confirm the live binding")
                confirmation = confirm(token, check)
                if hasattr(confirmation, "allowed") and not confirmation.allowed:
                    reasons = "; ".join(str(item) for item in confirmation.reasons)
                    raise RuntimeError(reasons or "live binding confirmation was rejected")
                note = " Live binding confirmed for this launch."
            except Exception as exc:
                problems.append(str(exc))
        if problems:
            self.status.set(
                "Capture evidence was saved, but the current live binding is blocked: "
                + "; ".join(problems)
                + ". Fix the context, reload, and run a fresh visible-change check."
            )
        else:
            self.status.set(self._profile_status() + note)
        self._capture_before = self._capture_confirmation = None
        self.capture_confirm.pack_forget()
        self.capture_after_button.configure(state="disabled")
        self._update_controls()

    def _reload_saved_profile(self, fallback: CompatibilityProfile) -> tuple[str, ...]:
        if self.controller is not None:
            try:
                check = self.controller.reload()
                selected = self._controller_value("profile")
            except Exception as exc:
                self.profile = fallback
                return (str(exc),)
            if isinstance(selected, CompatibilityProfile):
                self.profile = selected
                return _context_problems(
                    selected, self._controller_value("context"), check
                )
            self.profile = fallback
            return _blocking_reasons(check) or ("no compatibility profile is loaded",)
        self.profile = fallback
        return ()

    def save_displayed_frame(self) -> None:
        if self._preview_frame is None:
            self.status.set("Capture a preview before exporting a frame.")
            return
        path = filedialog.asksaveasfilename(
            parent=self.window, title="Save selected compatibility frame",
            defaultextension=".png", filetypes=(("PNG image", "*.png"),))
        if not path:
            return
        selected = Path(path)
        if selected.exists() and not messagebox.askyesno(
            "Replace frame?", "Replace the existing frame file?", parent=self.window):
            return
        frame = self._preview_frame
        self.status.set("Saving selected frame…")
        self._run_background(
            lambda: frame.image.save(selected, format="PNG"),
            lambda _value, error: self.status.set(
                f"Frame export failed: {error}" if error else f"Saved frame: {selected.name}"))

    def export_bundle(self) -> None:
        if self.profile is None:
            self.status.set("Create or load a profile before exporting.")
            return
        if self.include_frame.get() and self._preview_frame is None:
            self.status.set("Capture a preview or clear Include displayed frame.")
            return
        path = filedialog.asksaveasfilename(
            parent=self.window, title="Create evidence bundle directory",
            initialfile=Path(self.profile_filename).stem + "-evidence")
        if not path:
            return
        destination, profile = Path(path), self.profile
        frame = self._preview_frame if self.include_frame.get() else None

        def work():
            notes = []
            with tempfile.TemporaryDirectory(prefix="tibiaauto-evidence-") as temporary:
                root, frames = Path(temporary), []
                if frame is not None:
                    frame_path = root / "selected-frame.png"
                    frame.image.save(frame_path, format="PNG")
                    frames.append(frame_path)
                trace_path, trace = root / "session-trace.jsonl", None
                try:
                    export_session_trace(self.session, trace_path)
                    trace = trace_path
                except Exception as exc:
                    notes.append(f"trace omitted: {exc}")
                exported = export_evidence_bundle(
                    profile, destination, frame_files=frames, trace_file=trace)
            return exported, "; ".join(notes)

        self.status.set("Exporting bounded evidence bundle…")

        def done(value, error):
            if error:
                self.status.set(f"Bundle export failed: {error}")
            else:
                exported, note = value
                self.status.set(f"Exported bundle: {exported.name}" +
                                (f" ({note})" if note else ""))
        self._run_background(work, done)

    def _run_background(self, operation, on_complete) -> None:
        results: queue.Queue = queue.Queue(maxsize=1)

        def worker():
            try:
                results.put((operation(), None))
            except Exception as exc:
                results.put((None, str(exc)))

        def poll():
            if self._closed:
                return
            try:
                value, error = results.get_nowait()
            except queue.Empty:
                try:
                    self.window.after(50, poll)
                except tk.TclError:
                    pass
                return
            on_complete(value, error)

        threading.Thread(target=worker, name="compatibility-ui-operation", daemon=True).start()
        self.window.after(50, poll)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        _WINDOWS.pop(self._window_key, None)
        if self._countdown_id is not None:
            try:
                self.window.after_cancel(self._countdown_id)
            except tk.TclError:
                pass
        generation = self._probe_generation
        if self.pending_probe is not None and self._probe_started and generation is not None:
            threading.Thread(
                target=lambda generation=generation: self.session.stop_feature(
                    ProbeFeature.name, expected_generation=generation
                ),
                name="compatibility-close-cancel", daemon=True).start()
        self.pending_probe = self.pending_token = None
        self._probe_generation = None
        self.window.destroy()


def open_window(parent, session, profile_filename: str, controller=None) -> Compatibility:
    return Compatibility(parent, session, profile_filename, controller=controller)


__all__ = ["Compatibility", "PreviewMapping", "open_window", "probe_completion"]
