"""Operator editor and controls for the closed-loop hunt workflow.

This module owns presentation only.  It never imports a desktop backend, asks
the session for a frame, or generates input.  Live work is delegated to the
``HuntWorkflowController`` supplied by the application.  Template authoring is
deliberately offline: it crops an image file previously saved by Compatibility.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import re
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any, Mapping

from PIL import Image, ImageTk, UnidentifiedImageError

from modules.HuntConsole import run_control
from tibia_client.hunt_plan import hunt_plan_template


WORKFLOW_SCHEMA = "tibiaauto.hunt-workflow"
MAX_DOCUMENT_BYTES = 512 * 1024
ACTION_GROUPS = (
    "prepare", "outbound.before", "outbound.after", "deposit", "sell",
    "refill", "return.before", "return.after",
)
RELATIVE_OPS = {"changed", "increased", "decreased"}
_WINDOWS: dict[int, "HuntWorkflow"] = {}


class WorkflowEditorError(ValueError):
    """A bounded UI value cannot be represented in the workflow document."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise WorkflowEditorError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_document_file(filename: str | Path) -> dict[str, Any]:
    """Load a bounded JSON object without invoking any controller side effect."""
    path = Path(filename)
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise WorkflowEditorError(f"cannot read workflow: {exc}") from exc
    if len(payload) > MAX_DOCUMENT_BYTES:
        raise WorkflowEditorError(
            f"workflow exceeds the {MAX_DOCUMENT_BYTES // 1024} KiB editor limit"
        )
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
    except UnicodeDecodeError as exc:
        raise WorkflowEditorError("workflow must be UTF-8 JSON") from exc
    except json.JSONDecodeError as exc:
        raise WorkflowEditorError(
            f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    if not isinstance(value, dict):
        raise WorkflowEditorError("workflow document must be a JSON object")
    return value


def default_workflow_document() -> dict[str, Any]:
    """Return an honest draft: it names placeholders and contains no fake evidence."""
    return {
        "schema": WORKFLOW_SCHEMA,
        "version": 1,
        "plan": hunt_plan_template(),
        "bindings": {},
        "visual": {},
        "targets": [],
        "navigation": {},
    }


def parse_integer(text: str, label: str, minimum: int = 0, maximum: int = 1_000_000) -> int:
    try:
        value = int(text)
    except (TypeError, ValueError) as exc:
        raise WorkflowEditorError(f"{label} must be a whole number") from exc
    if not minimum <= value <= maximum:
        raise WorkflowEditorError(f"{label} must be from {minimum} through {maximum}")
    return value


def parse_number(text: str, label: str, minimum: float = 0.0, maximum: float = 60.0) -> float:
    try:
        value = float(text)
    except (TypeError, ValueError) as exc:
        raise WorkflowEditorError(f"{label} must be a number") from exc
    if not minimum <= value <= maximum or value != value:
        raise WorkflowEditorError(f"{label} must be from {minimum:g} through {maximum:g}")
    return value


def parse_region(text: str, label: str = "Region") -> list[int]:
    """Parse a client-local left, top, right, bottom rectangle."""
    try:
        values = [int(item.strip()) for item in text.split(",")]
    except ValueError as exc:
        raise WorkflowEditorError(f"{label} must contain four comma-separated integers") from exc
    if len(values) != 4 or any(value < 0 for value in values):
        raise WorkflowEditorError(f"{label} must contain four non-negative integers")
    left, top, right, bottom = values
    if right <= left or bottom <= top:
        raise WorkflowEditorError(f"{label} must be a non-empty left, top, right, bottom box")
    if (right - left) * (bottom - top) > 2_000_000:
        raise WorkflowEditorError(f"{label} exceeds the 2,000,000 pixel limit")
    return values


def display_region(value: object) -> str:
    if isinstance(value, (list, tuple)) and len(value) == 4:
        return ", ".join(str(item) for item in value)
    return ""


def _plan(document: dict[str, Any]) -> dict[str, Any]:
    plan = document.setdefault("plan", {})
    if not isinstance(plan, dict):
        raise WorkflowEditorError("plan must be an object")
    return plan


def action_group(document: dict[str, Any], group: str) -> list[dict[str, Any]]:
    """Return one editable action group, creating only missing containers."""
    plan = _plan(document)
    if group in {"prepare", "deposit", "sell", "refill"}:
        actions = plan.setdefault("actions", {})
        if not isinstance(actions, dict):
            raise WorkflowEditorError("plan.actions must be an object")
        values = actions.setdefault(group, [])
    else:
        parts = group.split(".")
        if len(parts) == 2:
            segment, position = parts
            segment_index = 0
        elif len(parts) == 3:
            segment, raw_index, position = parts
            try:
                segment_index = int(raw_index)
            except ValueError as exc:
                raise WorkflowEditorError(f"invalid action group: {group}") from exc
        else:
            raise WorkflowEditorError(f"invalid action group: {group}")
        if segment not in {"outbound", "return"} or position not in {"before", "after"}:
            raise WorkflowEditorError(f"invalid action group: {group}")
        travel = plan.setdefault("travel", {})
        if not isinstance(travel, dict):
            raise WorkflowEditorError("plan.travel must be an object")
        raw_segments = travel.setdefault(segment, [])
        # Accept the original single-object draft without discarding it.
        if isinstance(raw_segments, dict):
            raw_segments = [raw_segments]
            travel[segment] = raw_segments
        if not isinstance(raw_segments, list):
            raise WorkflowEditorError(f"plan.travel.{segment} must be a list")
        if segment_index < 0 or segment_index >= len(raw_segments):
            raise WorkflowEditorError(f"travel segment {segment}.{segment_index} does not exist")
        values_by_segment = raw_segments[segment_index]
        if not isinstance(values_by_segment, dict):
            raise WorkflowEditorError(f"plan.travel.{segment}[{segment_index}] must be an object")
        values = values_by_segment.setdefault(position, [])
    if not isinstance(values, list):
        raise WorkflowEditorError(f"action group {group} must be a list")
    return values


def available_action_groups(document: dict[str, Any]) -> tuple[str, ...]:
    """List station groups and every ordered travel-segment boundary."""
    groups = ["prepare", "deposit", "sell", "refill"]
    plan = document.get("plan")
    travel = plan.get("travel") if isinstance(plan, Mapping) else None
    for phase in ("outbound", "return"):
        raw = travel.get(phase) if isinstance(travel, Mapping) else None
        size = len(raw) if isinstance(raw, list) else (1 if isinstance(raw, Mapping) else 0)
        for index in range(size):
            groups.extend((f"{phase}.{index}.before", f"{phase}.{index}.after"))
    return tuple(groups)


def relative_template_path(template: str | Path, workflow_file: str | Path | None) -> str:
    """Represent a template relative to the workflow directory when possible."""
    path = Path(template).expanduser().resolve()
    base = Path(workflow_file).expanduser().resolve().parent if workflow_file else Path.cwd().resolve()
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        # Existing documents may point at machine-local assets elsewhere.  The
        # validator decides whether that is permitted; the editor is lossless.
        return str(path)


def rebase_visual_paths(
    document: Mapping[str, Any], old_base: str | Path, new_base: str | Path,
) -> dict[str, Any]:
    """Preserve every visual asset target when moving a workflow JSON file."""
    result = deepcopy(dict(document))
    old_root, new_root = Path(old_base).resolve(), Path(new_base).resolve()

    def rebased(value: object) -> object:
        if not isinstance(value, str) or not value.strip():
            return value
        path = Path(value).expanduser()
        absolute = path.resolve() if path.is_absolute() else (old_root / path).resolve()
        return Path(os.path.relpath(absolute, new_root)).as_posix()

    visuals = result.get("visual")
    if not isinstance(visuals, dict):
        return result
    for spec in visuals.values():
        if not isinstance(spec, dict):
            continue
        if "template" in spec:
            spec["template"] = rebased(spec["template"])
        context = spec.get("context")
        if isinstance(context, dict) and "template" in context:
            context["template"] = rebased(context["template"])
        digits = spec.get("digits")
        if isinstance(digits, dict):
            for digit, value in tuple(digits.items()):
                digits[digit] = rebased(value)
    return result


def status_text(status: Mapping[str, Any] | None) -> str:
    """Produce a compact, stable operator status from a tolerant status mapping."""
    value = status or {}
    phase = str(value.get("phase") or ("running" if value.get("active") else "idle"))
    reason = str(value.get("reason") or value.get("error") or "no reason reported")
    supplies = value.get("supply_counts")
    if isinstance(supplies, Mapping):
        supply_text = ", ".join(f"{key}={item}" for key, item in sorted(supplies.items())) or "none"
    else:
        supply_text = "unknown"
    pending = value.get("pending_confirmation") or value.get("action_id") or "none"
    manual = value.get("manual_reason") if value.get("manual_required") else "no"
    return (
        f"Phase: {phase}  |  Reason: {reason}\n"
        f"Supplies: {supply_text}  |  Pending confirmation: {pending}\n"
        f"Manual reconciliation: {manual}"
    )


def inspection_text(status: Mapping[str, Any] | None) -> str:
    value = status or {}
    if value.get("active"):
        return "Observation-only inspection is running…"
    if value.get("error"):
        return f"Inspection failed: {value['error']}"
    readings = value.get("readings")
    if not isinstance(readings, Mapping) or not readings:
        return "No inspection has run. Saved crops are offline assets, not live verification."
    lines = ["Latest observation-only readings:"]
    for sensor_id, reading in sorted(readings.items()):
        if isinstance(reading, Mapping):
            quality = reading.get("quality", "unknown")
            result = reading.get("value")
            detail = reading.get("detail") or ""
            lines.append(f"• {sensor_id}: {quality}; value={result!r} {detail}".rstrip())
        else:
            lines.append(f"• {sensor_id}: unknown")
    return "\n".join(lines)


class _CropDialog:
    """Select one crop from a saved image; it never reads the live client."""

    def __init__(self, owner: "HuntWorkflow", source: Path) -> None:
        self.owner, self.source = owner, source
        try:
            with Image.open(source) as opened:
                self.image = opened.convert("RGB")
        except (OSError, UnidentifiedImageError) as exc:
            raise WorkflowEditorError(f"saved capture is not a readable image: {exc}") from exc
        self.window = tk.Toplevel(owner.window)
        self.window.title("Offline template crop")
        self.window.transient(owner.window)
        self.window.geometry("860x650")
        self.canvas = tk.Canvas(self.window, background="#202020", cursor="crosshair")
        self.canvas.pack(fill="both", expand=True, padx=8, pady=8)
        foot = ttk.Frame(self.window, padding=(8, 0, 8, 8))
        foot.pack(fill="x")
        ttk.Label(foot, text="Drag a box over the saved capture. This does not verify live pixels.").pack(side="left")
        ttk.Button(foot, text="Save crop…", command=self.save).pack(side="right")
        self.canvas.bind("<Configure>", self._render)
        self.canvas.bind("<Button-1>", self._begin)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._end)
        self._start = self._end_point = None
        self._bounds = (0, 0, 1, 1)
        self._photo = None

    def _render(self, _event: Any = None) -> None:
        width, height = max(2, self.canvas.winfo_width()), max(2, self.canvas.winfo_height())
        iw, ih = self.image.size
        scale = min((width - 20) / iw, (height - 20) / ih)
        dw, dh = max(1, round(iw * scale)), max(1, round(ih * scale))
        left, top = (width - dw) // 2, (height - dh) // 2
        resized = self.image.resize((dw, dh), Image.Resampling.LANCZOS)
        self._photo = ImageTk.PhotoImage(resized)
        self.canvas.delete("all")
        self.canvas.create_image(left, top, anchor="nw", image=self._photo)
        self._bounds = left, top, dw, dh

    def _clamp(self, x: int, y: int) -> tuple[int, int]:
        left, top, width, height = self._bounds
        return max(left, min(x, left + width)), max(top, min(y, top + height))

    def _begin(self, event: Any) -> None:
        self._start = self._end_point = self._clamp(event.x, event.y)

    def _drag(self, event: Any) -> None:
        if self._start is None:
            return
        self._end_point = self._clamp(event.x, event.y)
        self.canvas.delete("selection")
        self.canvas.create_rectangle(*self._start, *self._end_point, outline="#00e5ff", width=2, tags="selection")

    def _end(self, event: Any) -> None:
        self._drag(event)

    def crop_box(self) -> tuple[int, int, int, int]:
        if self._start is None or self._end_point is None:
            raise WorkflowEditorError("drag a non-empty crop box first")
        left, top, width, height = self._bounds
        x1, y1 = self._start
        x2, y2 = self._end_point
        px = sorted((round((x1 - left) * self.image.width / width), round((x2 - left) * self.image.width / width)))
        py = sorted((round((y1 - top) * self.image.height / height), round((y2 - top) * self.image.height / height)))
        box = (max(0, px[0]), max(0, py[0]), min(self.image.width, px[1]), min(self.image.height, py[1]))
        if box[2] <= box[0] or box[3] <= box[1]:
            raise WorkflowEditorError("crop box must be non-empty")
        if (box[2] - box[0]) > 512 or (box[3] - box[1]) > 512:
            raise WorkflowEditorError("template crops must fit within 512 by 512 pixels")
        return box

    def save(self) -> None:
        try:
            box = self.crop_box()
        except WorkflowEditorError as exc:
            messagebox.showerror("Crop template", str(exc), parent=self.window)
            return
        base = self.owner.base_dir
        detector_id = self.owner.visual_vars["id"].get().strip()
        safe_detector = re.sub(r"[^A-Za-z0-9_.-]+", "_", detector_id).strip("._-")
        if not safe_detector:
            messagebox.showerror(
                "Crop template", "Enter the detector ID before saving its template.", parent=self.window,
            )
            return
        initial = base / "templates" / safe_detector
        initial.mkdir(parents=True, exist_ok=True)
        target = self.owner.crop_target.get()
        initial_name = (
            "context.png" if target == "Context template" else
            f"{target[-1]}.png" if target.startswith("Digit ") else "template.png"
        )
        filename = filedialog.asksaveasfilename(
            parent=self.window, title="Save template crop", initialdir=str(initial),
            initialfile=initial_name, defaultextension=".png", filetypes=(("PNG image", "*.png"),),
        )
        if not filename:
            return
        destination = Path(filename).expanduser().resolve()
        try:
            destination.relative_to(base.resolve())
        except ValueError:
            messagebox.showerror(
                "Crop template", "Save the template inside the workflow directory so its path stays portable.",
                parent=self.window,
            )
            return
        if target.startswith("Digit ") and destination.name != initial_name:
            messagebox.showerror(
                "Crop template", f"Save {target.lower()} as {initial_name}; count detectors use 0.png through 9.png.",
                parent=self.window,
            )
            return
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            self.image.crop(box).save(destination, format="PNG")
        except OSError as exc:
            messagebox.showerror("Crop template", f"Could not save crop: {exc}", parent=self.window)
            return
        relative = relative_template_path(destination, self.owner.filename)
        if target == "Context template":
            self.owner.visual_vars["context_template"].set(relative)
        elif target.startswith("Digit "):
            self.owner.visual_vars["digits_dir"].set(str(Path(relative).parent).replace("\\", "/"))
        else:
            self.owner.visual_template.set(relative)
        if target in {"Context template", "Match template"}:
            selected = self.owner._selected_binding_value()
            if selected is not None:
                _binding_id, binding = selected
                region = binding.get("region")
                if isinstance(region, (list, tuple)) and len(region) == 4:
                    left, top, right, bottom = (int(value) for value in region)
                    if box[0] >= left and box[1] >= top and box[2] <= right and box[3] <= bottom:
                        relative_box = [box[0] - left, box[1] - top, box[2] - left, box[3] - top]
                        target_field = "context_region" if target == "Context template" else "region"
                        self.owner.visual_vars[target_field].set(display_region(relative_box))
                    else:
                        self.owner.message.set(
                            f"Crop saved at client box {box}, outside the selected binding; enter its relative region manually."
                        )
                        self.window.destroy()
                        return
        self.owner.message.set(
            f"Offline crop saved from client box {box}. Add/update the detector, then Inspect live evidence."
        )
        self.window.destroy()


class HuntWorkflow:
    """Workflow configuration, observation, and run controls."""

    def __new__(cls, parent: object, controller: object):
        key = id(controller)
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

    def __init__(self, parent: object, controller: object) -> None:
        if getattr(self, "_initialized", False):
            return
        self._initialized = True
        self.controller = controller
        self.data = default_workflow_document()
        self.filename: str | None = None
        self._closed = self._active = self._inspecting = False
        self._busy: str | None = None
        self._pending_epoch: Any | None = None
        self._control_serial = 0
        self._config_widgets: list[tk.Widget] = []
        self._last_inspection_frame: Any = None
        self._inspection_photo: Any = None

        self.window = tk.Toplevel(getattr(parent, "windowID", parent))
        self.window.title("Hunt Workflow")
        self.window.geometry("1120x820")
        self.window.minsize(900, 680)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.message = tk.StringVar(value="Draft loaded. Validate it before starting.")
        self.runtime = tk.StringVar(value=status_text({}))
        self.inspection = tk.StringVar(value=inspection_text({}))
        self.resume = tk.BooleanVar(value=True)
        self._make_vars()
        self._build()
        self._load_into_editor()
        self._mark_clean()
        self.window.after(200, self._poll)

    @property
    def base_dir(self) -> Path:
        return Path(self.filename).expanduser().resolve().parent if self.filename else Path.cwd().resolve()

    def _make_vars(self) -> None:
        self.plan_id = tk.StringVar()
        self.outbound_route = tk.StringVar()
        self.hunt_route = tk.StringVar()
        self.return_route = tk.StringVar()
        self.route_phase = tk.StringVar(value="outbound")
        self.route_name = tk.StringVar()
        self.max_refill = tk.StringVar()
        self.supply_vars = {key: tk.StringVar() for key in (
            "id", "sensor", "return_below", "emergency_reserve", "refill_to"
        )}
        self.supply_optional = tk.BooleanVar()
        self.action_vars = {key: tk.StringVar() for key in (
            "group", "id", "kind", "key", "target", "destination", "requires", "timeout",
            "retries", "sensor", "field", "op", "value",
        )}
        self.action_mutating = tk.BooleanVar(value=True)
        self.binding_vars = {key: tk.StringVar() for key in ("id", "name", "region")}
        self.visual_vars = {key: tk.StringVar() for key in (
            "id", "kind", "context_template", "context_region", "template", "region",
            "score", "context_score", "digits_dir",
        )}
        self.visual_template = self.visual_vars["template"]
        self.capture_file = tk.StringVar()
        self.crop_target = tk.StringVar(value="Match template")

    def _build(self) -> None:
        body = ttk.Frame(self.window, padding=10)
        body.pack(fill="both", expand=True)
        toolbar = ttk.Frame(body)
        toolbar.pack(fill="x")
        for label, callback in (
            ("New", self.new), ("Load…", self.load), ("Save", self.save),
            ("Save as…", self.save_as), ("Validate", self.validate),
            ("Inspect evidence", self.inspect),
        ):
            button = ttk.Button(toolbar, text=label, command=callback)
            button.pack(side="left", padx=(0, 5))
            self._config_widgets.append(button)
        ttk.Label(toolbar, textvariable=self.message, wraplength=610).pack(side="left", padx=8, fill="x", expand=True)

        run = ttk.LabelFrame(body, text="Run controls", padding=8)
        run.pack(fill="x", pady=(8, 5))
        self.start_button = ttk.Button(run, text="Start", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(run, text="Stop workflow", command=self.stop)
        self.stop_button.pack(side="left", padx=5)
        self.return_button = ttk.Button(run, text="Return after fight", command=lambda: self.request_return(False))
        self.return_button.pack(side="left")
        self.return_now_button = ttk.Button(run, text="Return now", command=lambda: self.request_return(True))
        self.return_now_button.pack(side="left", padx=5)
        self.recheck_button = ttk.Button(run, text="Re-check evidence", command=lambda: self.acknowledge(False))
        self.recheck_button.pack(side="left")
        self.manual_button = ttk.Button(run, text="I reconciled manually", command=lambda: self.acknowledge(True))
        self.manual_button.pack(side="left", padx=5)
        resume = ttk.Checkbutton(run, text="Resume saved cycle", variable=self.resume)
        resume.pack(side="right")
        self._config_widgets.append(resume)
        ttk.Label(
            body, textvariable=self.runtime, justify="left", wraplength=1060,
        ).pack(fill="x")
        ttk.Label(
            body,
            text=("Manual reconciliation means you personally checked and corrected the client state. "
                  "It is recorded as operator_reconciled; it is never reported as machine-confirmed."),
            wraplength=1060,
        ).pack(fill="x", pady=(2, 4))

        self.notebook = ttk.Notebook(body)
        self.notebook.pack(fill="both", expand=True)
        self._build_plan_tab()
        self._build_supplies_tab()
        self._build_actions_tab()
        self._build_visual_tab()
        self._build_advanced_tab()

    def _entry(self, parent: tk.Widget, row: int, column: int, label: str, variable: tk.Variable,
               width: int = 18) -> ttk.Entry:
        ttk.Label(parent, text=label).grid(row=row, column=column, sticky="w", padx=4, pady=3)
        entry = ttk.Entry(parent, textvariable=variable, width=width)
        entry.grid(row=row, column=column + 1, sticky="ew", padx=4, pady=3)
        self._config_widgets.append(entry)
        return entry

    def _build_plan_tab(self) -> None:
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text="Plan & routes")
        self._entry(frame, 0, 0, "Plan ID", self.plan_id, 28)
        self._entry(frame, 1, 0, "Hunt route", self.hunt_route, 28)
        self._entry(frame, 2, 0, "Maximum refill rounds", self.max_refill, 10)
        self.route_tree = ttk.Treeview(frame, columns=("phase", "number", "route", "before", "after"), show="headings", height=8)
        for column, heading, width in (
            ("phase", "Travel phase", 120), ("number", "Order", 70), ("route", "Saved route", 240),
            ("before", "Before actions", 110), ("after", "After actions", 110),
        ):
            self.route_tree.heading(column, text=heading)
            self.route_tree.column(column, width=width, stretch=True)
        self.route_tree.grid(row=3, column=0, columnspan=4, sticky="nsew", padx=4, pady=6)
        self.route_tree.bind("<<TreeviewSelect>>", self._select_route)
        ttk.Label(frame, text="Travel phase").grid(row=4, column=0, sticky="w", padx=4)
        phase = ttk.Combobox(frame, textvariable=self.route_phase, values=("outbound", "return"), state="readonly", width=14)
        phase.grid(row=4, column=1, sticky="w", padx=4)
        self._config_widgets.append(phase)
        self._entry(frame, 4, 2, "Saved route", self.route_name, 28)
        route_bar = ttk.Frame(frame)
        route_bar.grid(row=5, column=0, columnspan=4, sticky="w", padx=4)
        for label, command in (("Add / update", self._put_route), ("Remove selected", self._remove_route),
                               ("Move up", lambda: self._move_route(-1)), ("Move down", lambda: self._move_route(1))):
            button = ttk.Button(route_bar, text=label, command=command)
            button.pack(side="left", padx=(0, 5))
            self._config_widgets.append(button)
        ttk.Label(
            frame,
            text=("Travel phases are ordered. Add door, stair, depot, or other route segments in the "
                  "sequence they run; assign actions at each boundary on the Actions tab."),
            wraplength=760,
        ).grid(row=6, column=0, columnspan=4, sticky="w", padx=4, pady=12)
        frame.columnconfigure(3, weight=1)
        frame.rowconfigure(3, weight=1)

    def _tree(self, parent: tk.Widget, columns: tuple[str, ...], headings: tuple[str, ...], height: int = 8) -> ttk.Treeview:
        tree = ttk.Treeview(parent, columns=columns, show="headings", height=height)
        for column, heading in zip(columns, headings):
            tree.heading(column, text=heading)
            tree.column(column, width=120, stretch=True)
        tree.pack(fill="both", expand=True)
        return tree

    def _build_supplies_tab(self) -> None:
        frame = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(frame, text="Supplies")
        self.supply_tree = self._tree(
            frame, ("id", "sensor", "emergency", "return", "refill", "optional"),
            ("Supply", "Count sensor", "Emergency", "Return below", "Refill to", "Optional"), 7,
        )
        self.supply_tree.bind("<<TreeviewSelect>>", self._select_supply)
        form = ttk.Frame(frame)
        form.pack(fill="x", pady=6)
        labels = (("id", "Supply ID"), ("sensor", "Count sensor"),
                  ("emergency_reserve", "Emergency reserve"), ("return_below", "Return below"),
                  ("refill_to", "Refill target"))
        for index, (key, label) in enumerate(labels):
            self._entry(form, index // 3, (index % 3) * 2, label, self.supply_vars[key], 16)
        optional = ttk.Checkbutton(form, text="Optional", variable=self.supply_optional)
        optional.grid(row=1, column=4, sticky="w", padx=4)
        self._config_widgets.append(optional)
        bar = ttk.Frame(frame)
        bar.pack(fill="x")
        for label, command in (("Add / update", self._put_supply), ("Remove selected", self._remove_supply)):
            button = ttk.Button(bar, text=label, command=command)
            button.pack(side="left", padx=(0, 5))
            self._config_widgets.append(button)

    def _build_actions_tab(self) -> None:
        frame = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(frame, text="Actions & confirmation")
        self.action_tree = self._tree(
            frame, ("group", "id", "kind", "effect", "confirmation"),
            ("Phase", "Action", "Kind", "Key / target", "Required confirmation"), 7,
        )
        self.action_tree.bind("<<TreeviewSelect>>", self._select_action)
        form = ttk.Frame(frame)
        form.pack(fill="x", pady=5)
        ttk.Label(form, text="Phase").grid(row=0, column=0, sticky="w", padx=4)
        group = ttk.Combobox(form, textvariable=self.action_vars["group"], values=ACTION_GROUPS, state="readonly", width=18)
        group.grid(row=0, column=1, sticky="ew", padx=4)
        self.action_group_box = group
        ttk.Label(form, text="Kind").grid(row=0, column=2, sticky="w", padx=4)
        kind = ttk.Combobox(form, textvariable=self.action_vars["kind"], values=("press", "left_click", "right_click", "drag"), state="readonly", width=14)
        kind.grid(row=0, column=3, sticky="ew", padx=4)
        self._config_widgets.extend((group, kind))
        entries = (
            ("id", "Action ID"), ("key", "Key (press)"), ("target", "Target binding"),
            ("destination", "Drag destination"), ("requires", "Required bindings (comma list)"),
            ("timeout", "Timeout seconds"), ("retries", "Retries"),
            ("sensor", "Confirmation sensor"), ("value", "Confirmation value"),
        )
        for index, (key, label) in enumerate(entries):
            row, block = 1 + index // 3, index % 3
            self._entry(form, row, block * 2, label, self.action_vars[key], 18)
        ttk.Label(form, text="Confirmation field").grid(row=4, column=0, sticky="w", padx=4)
        field = ttk.Combobox(form, textvariable=self.action_vars["field"], values=("present", "count"), state="readonly", width=15)
        field.grid(row=4, column=1, sticky="ew", padx=4)
        ttk.Label(form, text="Comparison").grid(row=4, column=2, sticky="w", padx=4)
        op = ttk.Combobox(form, textvariable=self.action_vars["op"], values=("eq", "ne", "gte", "lte", "changed", "increased", "decreased"), state="readonly", width=15)
        op.grid(row=4, column=3, sticky="ew", padx=4)
        mutating = ttk.Checkbutton(form, text="Mutating action", variable=self.action_mutating)
        mutating.grid(row=4, column=4, sticky="w", padx=4)
        self._config_widgets.extend((field, op, mutating))
        bar = ttk.Frame(frame)
        bar.pack(fill="x")
        for label, command in (("Add / update", self._put_action), ("Remove selected", self._remove_action)):
            button = ttk.Button(bar, text=label, command=command)
            button.pack(side="left", padx=(0, 5))
            self._config_widgets.append(button)

    def _build_visual_tab(self) -> None:
        frame = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(frame, text="Visual bindings")
        panes = ttk.Panedwindow(frame, orient="vertical")
        panes.pack(fill="both", expand=True)
        binding_box = ttk.LabelFrame(panes, text="Client-local binding regions", padding=5)
        spec_box = ttk.LabelFrame(panes, text="Visual detectors", padding=5)
        panes.add(binding_box, weight=1)
        panes.add(spec_box, weight=2)
        self.binding_tree = self._tree(binding_box, ("id", "name", "region"), ("Binding ID", "Detector", "Region"), 4)
        self.binding_tree.bind("<<TreeviewSelect>>", self._select_binding)
        binding_form = ttk.Frame(binding_box)
        binding_form.pack(fill="x")
        for i, (key, label) in enumerate((("id", "Binding ID"), ("name", "Detector ID"), ("region", "Region L,T,R,B"))):
            self._entry(binding_form, 0, i * 2, label, self.binding_vars[key], 18)
        self._form_buttons(binding_box, self._put_binding, self._remove_binding)

        self.visual_tree = self._tree(spec_box, ("id", "kind", "template", "region"), ("Detector ID", "Kind", "Template / digits", "Search or count region"), 4)
        self.visual_tree.bind("<<TreeviewSelect>>", self._select_visual)
        visual_form = ttk.Frame(spec_box)
        visual_form.pack(fill="x")
        fields = (("id", "Detector ID"), ("context_template", "Context template"),
                  ("context_region", "Context region"), ("template", "Match template"),
                  ("region", "Search / count region"), ("digits_dir", "Digit templates folder"),
                  ("score", "Match score"), ("context_score", "Context score"))
        ttk.Label(visual_form, text="Kind").grid(row=0, column=4, sticky="w", padx=4)
        kind = ttk.Combobox(visual_form, textvariable=self.visual_vars["kind"], values=("template", "count"), state="readonly", width=12)
        kind.grid(row=0, column=5, sticky="ew", padx=4)
        self._config_widgets.append(kind)
        for index, (key, label) in enumerate(fields):
            row, block = index // 3, index % 3
            if row == 0 and block == 2:
                row = 1
            elif row >= 1:
                row += 1
            self._entry(visual_form, row, block * 2, label, self.visual_vars[key], 19)
        self._form_buttons(spec_box, self._put_visual, self._remove_visual)
        offline = ttk.Frame(spec_box)
        offline.pack(fill="x", pady=(5, 0))
        entry = ttk.Entry(offline, textvariable=self.capture_file)
        entry.pack(side="left", fill="x", expand=True)
        self._config_widgets.append(entry)
        target = ttk.Combobox(
            offline, textvariable=self.crop_target,
            values=("Match template", "Context template", *(f"Digit {number}" for number in range(10))),
            state="readonly", width=17,
        )
        target.pack(side="left", padx=(5, 0))
        self._config_widgets.append(target)
        choose = ttk.Button(offline, text="Load saved client capture…", command=self._choose_capture)
        choose.pack(side="left", padx=5)
        crop = ttk.Button(offline, text="Select crop and save template…", command=self._open_crop)
        crop.pack(side="left")
        self._config_widgets.extend((choose, crop))
        ttk.Label(frame, textvariable=self.inspection, justify="left", wraplength=1030).pack(fill="x", pady=(5, 0))
        self.inspection_canvas = tk.Canvas(frame, height=150, background="#202020")
        self.inspection_canvas.pack(fill="x", pady=(4, 0))
        self.inspection_canvas.bind("<Configure>", lambda _event: self._render_inspection_frame())

    def _form_buttons(self, parent: tk.Widget, put: Any, remove: Any) -> None:
        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=3)
        for label, command in (("Add / update", put), ("Remove selected", remove)):
            button = ttk.Button(bar, text=label, command=command)
            button.pack(side="left", padx=(0, 5))
            self._config_widgets.append(button)

    def _build_advanced_tab(self) -> None:
        frame = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(frame, text="Advanced JSON")
        ttk.Label(frame, text="Structured tabs are the primary editor. JSON preserves optional navigation, targets, loot, and future fields.").pack(anchor="w")
        self.json_text = tk.Text(frame, wrap="none", undo=True, height=20)
        self.json_text.pack(fill="both", expand=True, pady=5)
        self._config_widgets.append(self.json_text)
        bar = ttk.Frame(frame)
        bar.pack(fill="x")
        apply_button = ttk.Button(bar, text="Apply JSON to editor", command=self._apply_json)
        apply_button.pack(side="left")
        refresh_button = ttk.Button(bar, text="Refresh JSON from structured fields", command=self._refresh_json)
        refresh_button.pack(side="left", padx=5)
        self._config_widgets.extend((apply_button, refresh_button))

    def _load_into_editor(self) -> None:
        plan = _plan(self.data)
        travel = plan.get("travel") if isinstance(plan.get("travel"), Mapping) else {}
        outbound_raw = travel.get("outbound") if isinstance(travel, Mapping) else None
        returned_raw = travel.get("return") if isinstance(travel, Mapping) else None
        outbound = outbound_raw[0] if isinstance(outbound_raw, list) and outbound_raw and isinstance(outbound_raw[0], Mapping) else (outbound_raw if isinstance(outbound_raw, Mapping) else {})
        returned = returned_raw[0] if isinstance(returned_raw, list) and returned_raw and isinstance(returned_raw[0], Mapping) else (returned_raw if isinstance(returned_raw, Mapping) else {})
        self.plan_id.set(str(plan.get("id", "")))
        self.outbound_route.set(str(outbound.get("route", "")))
        self.hunt_route.set(str(plan.get("hunt_route", "")))
        self.return_route.set(str(returned.get("route", "")))
        self.max_refill.set(str(plan.get("max_refill_rounds", 10)))
        if hasattr(self, "route_tree"):
            self._refresh_route_tree()
        self._refresh_supply_tree()
        self._refresh_action_tree()
        self._refresh_visual_trees()
        self._refresh_json(collect=False)

    def _collect_structured(self) -> None:
        if hasattr(self, "json_text") and self.json_text.edit_modified():
            raise WorkflowEditorError("Apply or undo the edited Advanced JSON before continuing")
        plan = _plan(self.data)
        plan["id"] = self.plan_id.get().strip()
        plan["hunt_route"] = self.hunt_route.get().strip()
        plan["max_refill_rounds"] = parse_integer(self.max_refill.get(), "Maximum refill rounds", 1, 100)
        travel = plan.setdefault("travel", {})
        if not isinstance(travel, dict):
            raise WorkflowEditorError("plan.travel must be an object")
        # Headless callers and old drafts use the first-route variables.  The
        # GUI route list updates the arrays directly and preserves every segment.
        if not hasattr(self, "route_tree"):
            for name, variable in (("outbound", self.outbound_route), ("return", self.return_route)):
                raw = travel.setdefault(name, [])
                if isinstance(raw, dict):
                    raw = [raw]
                    travel[name] = raw
                if not isinstance(raw, list):
                    raise WorkflowEditorError(f"plan.travel.{name} must be a list")
                if not raw:
                    raw.append({"route": variable.get().strip(), "before": [], "after": []})
                elif isinstance(raw[0], dict):
                    raw[0]["route"] = variable.get().strip()
        if hasattr(self, "supply_vars"):
            if self._selected_route() is not None and self._put_route() is False:
                raise WorkflowEditorError(getattr(self, "_last_form_error", "correct the route form"))
            pending = (
                (self.supply_vars["id"].get().strip(), self._put_supply),
                (self.action_vars["id"].get().strip(), self._put_action),
                (self.binding_vars["id"].get().strip(), self._put_binding),
                (self.visual_vars["id"].get().strip(), self._put_visual),
            )
            for identifier, writer in pending:
                if identifier and writer(silent=True) is False:
                    raise WorkflowEditorError(getattr(self, "_last_form_error", "correct the open form"))

    def _clear_error(self) -> None:
        self.message.set("Checking configuration…")

    def _show_error(self, exc: Exception) -> None:
        self._last_form_error = str(exc)
        self.message.set(f"Configuration error: {exc}")

    def _mark_clean(self) -> None:
        self._clean_document = deepcopy(self.data)

    def _confirm_discard(self) -> bool:
        json_dirty = bool(hasattr(self, "json_text") and self.json_text.edit_modified())
        form_error = False
        try:
            self._collect_structured()
        except Exception:
            form_error = True
        if not json_dirty and not form_error and self.data == getattr(self, "_clean_document", None):
            return True
        return bool(messagebox.askyesno(
            "Discard workflow changes?",
            "This workflow has unsaved changes. Discard them?",
            parent=self.window,
        ))

    def new(self) -> None:
        if self._active or not self._confirm_discard():
            return
        self.data, self.filename = default_workflow_document(), None
        self._load_into_editor()
        self._mark_clean()
        self.message.set("New draft created. Placeholder routes and bindings must be configured.")

    def load(self) -> None:
        if self._active or not self._confirm_discard():
            return
        filename = filedialog.askopenfilename(parent=self.window, filetypes=(("Hunt workflow", "*.json"), ("All files", "*")))
        if not filename:
            return
        def loaded(value: Any) -> None:
            try:
                if not isinstance(value, dict):
                    raise WorkflowEditorError("controller returned a non-object workflow")
                old_data, old_filename = self.data, self.filename
                self.data, self.filename = deepcopy(value), filename
                try:
                    self._load_into_editor()
                except Exception:
                    self.data, self.filename = old_data, old_filename
                    self._load_into_editor()
                    raise
                self._mark_clean()
                self.message.set(f"Loaded {Path(filename).name}. Validate before starting.")
            except Exception as exc:
                self._show_error(exc)

        self._begin_control("Loading workflow", lambda: self.controller.load(filename), loaded)

    def _dispatch_control(self, operation: Any, done: Any) -> None:
        run_control(self.window, operation, done)

    def _begin_control(self, label: str, operation: Any, success: Any, failed: Any = None) -> bool:
        if self._busy is not None:
            self.message.set(f"{self._busy} is already in progress.")
            return False
        self._busy = label
        self._control_serial += 1
        serial = self._control_serial
        self._set_config_enabled(False)
        self.start_button.configure(state="disabled")
        if self._pending_epoch is not None:
            self.stop_button.configure(state="normal", text="Cancel pending")
        self.message.set(f"{label}…")

        def done(value: Any, error: str | None) -> None:
            if serial != self._control_serial or self._closed:
                return
            self._busy = None
            self._pending_epoch = None
            if error:
                if failed is not None:
                    failed()
                self._set_config_enabled(not self._active and not self._inspecting)
                self._show_error(WorkflowEditorError(error))
            else:
                self._set_config_enabled(not self._active and not self._inspecting)
                success(value)

        self._dispatch_control(operation, done)
        return True

    def save(self) -> None:
        if self.filename is None:
            self.save_as()
            return
        try:
            self._collect_structured()
            self.controller.save(self.filename, deepcopy(self.data))
            self._refresh_json(collect=False)
            self._mark_clean()
            self.message.set(f"Saved {Path(self.filename).name}.")
        except Exception as exc:
            self._show_error(exc)

    def save_as(self) -> None:
        if self._active:
            return
        filename = filedialog.asksaveasfilename(parent=self.window, defaultextension=".json", filetypes=(("Hunt workflow", "*.json"),))
        if not filename:
            return
        try:
            self._collect_structured()
            old_base = self.base_dir
            new_base = Path(filename).expanduser().resolve().parent
            candidate = rebase_visual_paths(self.data, old_base, new_base)
            self.controller.save(filename, deepcopy(candidate))
            self.data, self.filename = candidate, filename
            self._load_into_editor()
            self._mark_clean()
            self.message.set(f"Saved {Path(filename).name}.")
        except Exception as exc:
            self._show_error(exc)

    def validate(self) -> None:
        self._clear_error()
        try:
            self._collect_structured()
        except Exception as exc:
            self._show_error(exc)
            return
        snapshot, base_dir = deepcopy(self.data), self.base_dir
        self._begin_control(
            "Validating configuration",
            lambda: self.controller.validate(snapshot, base_dir),
            lambda _configuration: self.message.set("Configuration is valid for the current local profile."),
        )

    def start(self) -> None:
        if self._active or self._busy is not None:
            return
        try:
            self._collect_structured()
        except Exception as exc:
            self._show_error(exc)
            return
        snapshot, base_dir, resume = deepcopy(self.data), self.base_dir, bool(self.resume.get())
        try:
            epoch = self.controller.control_epoch
        except Exception as exc:
            self._show_error(exc)
            return
        self._pending_epoch = epoch
        self._begin_control(
            "Starting workflow",
            lambda: self.controller.start(
                snapshot, base_dir, resume=resume, expected_stop_epoch=epoch
            ),
            self._started,
        )

    def _started(self, _value: Any) -> None:
        self._active = True
        self._set_config_enabled(False)
        self.message.set("Workflow started. Runtime status shows the current verified phase.")

    def stop(self) -> None:
        pending = self._pending_epoch is not None
        # Supersede any start/inspect/command callback on Tk's thread before
        # queueing stop. Controller.stop invalidates its activation ticket as
        # soon as the stop worker enters, and also stops a generation that won
        # the narrow race and became active first.
        self._control_serial += 1
        serial = self._control_serial
        self._pending_epoch = None
        self._inspecting = False
        self._busy = "Cancelling pending activation" if pending else "Stopping workflow"
        self._set_config_enabled(False)
        self.stop_button.configure(state="disabled")
        self.message.set(f"{self._busy}…")

        def done(_value: Any, error: str | None) -> None:
            if serial != self._control_serial or self._closed:
                return
            self._busy = None
            if error:
                self.message.set(f"Stop failed: {error}")
            else:
                self.message.set(
                    "Pending workflow activation cancelled."
                    if pending else
                    "Workflow stop requested. Use Hunt Console Stop All for the global stop."
                )
            self._set_config_enabled(not self._active)

        self._dispatch_control(self.controller.stop, done)

    def request_return(self, now: bool) -> None:
        if self._busy is not None:
            return
        self._begin_control(
            "Requesting immediate return" if now else "Requesting return after fight",
            lambda: self.controller.request_return(now=now),
            lambda _value: self.message.set(
                "Immediate return requested." if now else "Return after the current fight requested."
            ),
        )

    def acknowledge(self, reconciled: bool) -> None:
        if self._busy is not None:
            return
        self._begin_control(
            "Recording manual reconciliation" if reconciled else "Requesting evidence re-check",
            lambda: self.controller.acknowledge_manual(operator_reconciled=reconciled),
            lambda _value: self.message.set(
                "Operator reconciliation recorded; fresh evidence is still required."
                if reconciled else "Fresh evidence re-check requested; no manual reconciliation was claimed."
            ),
        )

    def inspect(self) -> None:
        if self._active:
            self.message.set("Stop the workflow before running an observation-only inspection.")
            return
        self._clear_error()
        try:
            self._collect_structured()
        except Exception as exc:
            self._show_error(exc)
            return
        snapshot, base_dir = deepcopy(self.data), self.base_dir
        self._inspection_bindings = deepcopy(snapshot.get("bindings", {}))
        self._inspection_result_bindings = self._inspection_bindings
        self._inspecting = True
        try:
            epoch = self.controller.control_epoch
        except Exception as exc:
            self._inspecting = False
            self._show_error(exc)
            return
        self._pending_epoch = epoch
        started = self._begin_control(
            "Starting observation-only inspection",
            lambda: self.controller.inspect(
                snapshot, base_dir, expected_stop_epoch=epoch
            ),
            lambda _value: self.message.set(
                "Observation-only inspection started. It sends no client input."
            ),
            lambda: setattr(self, "_inspecting", False),
        )
        if not started:
            self._inspecting = False
            self._pending_epoch = None

    def _poll(self) -> None:
        if self._closed:
            return
        try:
            status = self.controller.status()
            if not isinstance(status, Mapping):
                status = {}
            self.runtime.set(status_text(status))
            active = bool(status.get("active"))
            if active != self._active:
                self._active = active
                self._set_config_enabled(not active and not self._inspecting and self._busy is None)
            manual = bool(status.get("manual_required"))
            command_ready = active and self._busy is None
            self.manual_button.configure(state="normal" if command_ready and manual else "disabled")
            self.recheck_button.configure(state="normal" if command_ready and manual else "disabled")
            self.return_button.configure(state="normal" if command_ready else "disabled")
            self.return_now_button.configure(state="normal" if command_ready else "disabled")
            activation_pending = self._pending_epoch is not None
            stopping = self._busy in {"Cancelling pending activation", "Stopping workflow"}
            self.stop_button.configure(
                state=("normal" if (active or self._inspecting or activation_pending) and not stopping
                       else "disabled")
            )
            self.stop_button.configure(
                text=("Cancel pending" if activation_pending else
                      "Stop inspection" if self._inspecting and not active else "Stop workflow")
            )
            self.start_button.configure(
                state="disabled" if active or self._inspecting or self._busy is not None else "normal"
            )
        except Exception as exc:
            self.runtime.set(f"Workflow status unavailable: {exc}")
        try:
            observed = self.controller.inspection_status()
            if isinstance(observed, Mapping):
                self.inspection.set(inspection_text(observed))
                inspecting = bool(observed.get("active"))
                if inspecting != self._inspecting:
                    self._inspecting = inspecting
                    self._set_config_enabled(not self._active and not inspecting and self._busy is None)
                frame = observed.get("frame")
                result_bindings = observed.get("bindings")
                if isinstance(result_bindings, Mapping):
                    self._inspection_result_bindings = dict(result_bindings)
                if frame is not self._last_inspection_frame:
                    self._last_inspection_frame = frame
                    self._render_inspection_frame()
        except Exception as exc:
            self.inspection.set(f"Inspection status unavailable: {exc}")
        try:
            self.window.after(250, self._poll)
        except tk.TclError:
            pass

    def _render_inspection_frame(self) -> None:
        if not hasattr(self, "inspection_canvas"):
            return
        canvas = self.inspection_canvas
        canvas.delete("all")
        frame = self._last_inspection_frame
        image = getattr(frame, "image", None)
        if not isinstance(image, Image.Image):
            canvas.create_text(
                max(1, canvas.winfo_width()) // 2, max(1, canvas.winfo_height()) // 2,
                fill="white", text="No observation-only inspection frame",
            )
            self._inspection_photo = None
            return
        width, height = max(2, canvas.winfo_width()), max(2, canvas.winfo_height())
        scale = min((width - 10) / image.width, (height - 10) / image.height)
        drawn_width = max(1, round(image.width * scale))
        drawn_height = max(1, round(image.height * scale))
        left, top = (width - drawn_width) // 2, (height - drawn_height) // 2
        self._inspection_photo = ImageTk.PhotoImage(
            image.resize((drawn_width, drawn_height), Image.Resampling.LANCZOS)
        )
        canvas.create_image(left, top, anchor="nw", image=self._inspection_photo)
        bindings = getattr(
            self, "_inspection_result_bindings", getattr(self, "_inspection_bindings", {})
        )
        if isinstance(bindings, Mapping):
            for binding_id, binding in bindings.items():
                if not isinstance(binding, Mapping):
                    continue
                region = binding.get("region")
                if not isinstance(region, (list, tuple)) or len(region) != 4:
                    continue
                try:
                    x1, y1, x2, y2 = (float(value) for value in region)
                except (TypeError, ValueError):
                    continue
                canvas.create_rectangle(
                    left + x1 * scale, top + y1 * scale,
                    left + x2 * scale, top + y2 * scale,
                    outline="#00e5ff", width=1,
                )
                canvas.create_text(
                    left + x1 * scale + 2, top + y1 * scale + 2,
                    anchor="nw", fill="#00e5ff", text=str(binding_id),
                )

    def _set_config_enabled(self, enabled: bool) -> None:
        for widget in self._config_widgets:
            try:
                if isinstance(widget, ttk.Combobox):
                    widget.configure(state="readonly" if enabled else "disabled")
                else:
                    widget.configure(state="normal" if enabled else "disabled")
            except tk.TclError:
                pass

    def _refresh_supply_tree(self) -> None:
        self.supply_tree.delete(*self.supply_tree.get_children())
        values = _plan(self.data).get("supplies", [])
        if isinstance(values, list):
            for index, item in enumerate(values):
                if isinstance(item, Mapping):
                    self.supply_tree.insert("", "end", iid=str(index), values=(
                        item.get("id", ""), item.get("sensor", ""), item.get("emergency_reserve", ""),
                        item.get("return_below", ""), item.get("refill_to", ""), item.get("optional", False),
                    ))

    def _travel_lists(self) -> dict[str, list[dict[str, Any]]]:
        plan = _plan(self.data)
        travel = plan.setdefault("travel", {})
        if not isinstance(travel, dict):
            raise WorkflowEditorError("plan.travel must be an object")
        result: dict[str, list[dict[str, Any]]] = {}
        for phase in ("outbound", "return"):
            raw = travel.setdefault(phase, [])
            if isinstance(raw, dict):
                raw = [raw]
                travel[phase] = raw
            if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
                raise WorkflowEditorError(f"plan.travel.{phase} must be a list of route segments")
            result[phase] = raw
        return result

    def _refresh_route_tree(self) -> None:
        self.route_tree.delete(*self.route_tree.get_children())
        try:
            phases = self._travel_lists()
        except WorkflowEditorError as exc:
            self._show_error(exc)
            return
        row = 0
        for phase in ("outbound", "return"):
            for index, segment in enumerate(phases[phase]):
                before = segment.get("before") if isinstance(segment.get("before"), list) else []
                after = segment.get("after") if isinstance(segment.get("after"), list) else []
                self.route_tree.insert("", "end", iid=str(row), values=(
                    phase, index + 1, segment.get("route", ""), len(before), len(after),
                ), tags=(phase, str(index)))
                row += 1
        if hasattr(self, "action_group_box"):
            self.action_group_box.configure(values=available_action_groups(self.data))

    def _selected_route(self) -> tuple[str, int, dict[str, Any]] | None:
        selected = self.route_tree.selection()
        if not selected:
            return None
        item = self.route_tree.item(selected[0])
        values = item.get("values", ())
        if len(values) < 2:
            return None
        phase, index = str(values[0]), int(values[1]) - 1
        try:
            segment = self._travel_lists()[phase][index]
        except (KeyError, IndexError, WorkflowEditorError):
            return None
        return phase, index, segment

    def _select_route(self, _event: Any = None) -> None:
        selected = self._selected_route()
        if selected:
            phase, _index, segment = selected
            self.route_phase.set(phase)
            self.route_name.set(str(segment.get("route", "")))

    def _put_route(self) -> bool:
        try:
            phase, route = self.route_phase.get(), self.route_name.get().strip()
            if phase not in {"outbound", "return"} or not route:
                raise WorkflowEditorError("Travel phase and saved route are required")
            selected = self._selected_route()
            if selected and selected[0] == phase:
                selected[2]["route"] = route
            else:
                self._travel_lists()[phase].append({"route": route, "before": [], "after": []})
            self._refresh_route_tree()
            self._refresh_action_tree()
            self.message.set(f"{phase.title()} route segment updated in the draft.")
            return True
        except Exception as exc:
            self._show_error(exc)
            return False

    def _remove_route(self) -> None:
        selected = self._selected_route()
        if selected:
            phase, index, _segment = selected
            self._travel_lists()[phase].pop(index)
            self._refresh_route_tree()
            self._refresh_action_tree()

    def _move_route(self, offset: int) -> None:
        selected = self._selected_route()
        if selected:
            phase, index, _segment = selected
            values = self._travel_lists()[phase]
            destination = index + offset
            if 0 <= destination < len(values):
                values[index], values[destination] = values[destination], values[index]
                self._refresh_route_tree()
                self._refresh_action_tree()

    def _select_supply(self, _event: Any = None) -> None:
        selection = self.supply_tree.selection()
        if not selection:
            return
        values = _plan(self.data).get("supplies", [])
        try:
            item = values[int(selection[0])]
        except (IndexError, TypeError, ValueError):
            return
        for key, variable in self.supply_vars.items():
            variable.set(str(item.get(key, "")))
        self.supply_optional.set(bool(item.get("optional", False)))

    def _put_supply(self, silent: bool = False) -> bool:
        try:
            item = {
                "id": self.supply_vars["id"].get().strip(),
                "sensor": self.supply_vars["sensor"].get().strip(),
                "return_below": parse_integer(self.supply_vars["return_below"].get(), "Return below"),
                "emergency_reserve": parse_integer(self.supply_vars["emergency_reserve"].get(), "Emergency reserve"),
                "refill_to": parse_integer(self.supply_vars["refill_to"].get(), "Refill target"),
                "optional": bool(self.supply_optional.get()),
            }
            if not item["id"] or not item["sensor"]:
                raise WorkflowEditorError("Supply ID and count sensor are required")
            if not item["emergency_reserve"] <= item["return_below"] <= item["refill_to"]:
                raise WorkflowEditorError("Emergency reserve must be <= return threshold <= refill target")
            supplies = _plan(self.data).setdefault("supplies", [])
            if not isinstance(supplies, list):
                raise WorkflowEditorError("plan.supplies must be a list")
            found = next((i for i, value in enumerate(supplies) if isinstance(value, Mapping) and value.get("id") == item["id"]), None)
            if found is None:
                supplies.append(item)
            else:
                supplies[found] = item
            self._refresh_supply_tree()
            if not silent:
                self.message.set(f"Supply {item['id']} updated in the draft.")
            return True
        except Exception as exc:
            self._show_error(exc)
            return False

    def _remove_supply(self) -> None:
        selection = self.supply_tree.selection()
        supplies = _plan(self.data).get("supplies")
        if selection and isinstance(supplies, list):
            try:
                supplies.pop(int(selection[0]))
            except (IndexError, ValueError):
                return
            self._refresh_supply_tree()

    def _all_actions(self) -> list[tuple[str, int, Mapping[str, Any]]]:
        result = []
        for group in available_action_groups(self.data):
            try:
                values = action_group(self.data, group)
            except WorkflowEditorError:
                continue
            result.extend((group, index, value) for index, value in enumerate(values) if isinstance(value, Mapping))
        return result

    def _refresh_action_tree(self) -> None:
        self.action_tree.delete(*self.action_tree.get_children())
        for row, (group, _index, item) in enumerate(self._all_actions()):
            confirmation = item.get("confirmation") if isinstance(item.get("confirmation"), Mapping) else {}
            effect = item.get("key") or item.get("target") or ""
            summary = f"{confirmation.get('sensor', '')} {confirmation.get('field', '')} {confirmation.get('op', '')}"
            if "value" in confirmation:
                summary += f" {confirmation['value']}"
            self.action_tree.insert("", "end", iid=str(row), values=(group, item.get("id", ""), item.get("kind", ""), effect, summary))

    def _selected_action(self) -> tuple[str, int, Mapping[str, Any]] | None:
        selected = self.action_tree.selection()
        rows = self._all_actions()
        if not selected:
            return None
        try:
            return rows[int(selected[0])]
        except (IndexError, ValueError):
            return None

    def _select_action(self, _event: Any = None) -> None:
        selected = self._selected_action()
        if selected is None:
            return
        group, _index, item = selected
        confirmation = item.get("confirmation") if isinstance(item.get("confirmation"), Mapping) else {}
        values = {
            "group": group, "id": item.get("id", ""), "kind": item.get("kind", ""),
            "key": item.get("key", ""), "target": item.get("target", ""),
            "destination": item.get("destination", ""),
            "requires": ", ".join(item.get("require_present", [])),
            "timeout": item.get("timeout", 2), "retries": item.get("retries", 1),
            "sensor": confirmation.get("sensor", ""), "field": confirmation.get("field", "present"),
            "op": confirmation.get("op", "eq"), "value": confirmation.get("value", ""),
        }
        for key, value in values.items():
            self.action_vars[key].set(str(value))
        self.action_mutating.set(bool(item.get("mutating", True)))

    def _condition_value(self, field: str, op: str, raw: str) -> Any:
        if op in RELATIVE_OPS:
            return None
        if field == "present":
            if raw.strip().lower() not in {"true", "false"}:
                raise WorkflowEditorError("Present confirmation value must be true or false")
            return raw.strip().lower() == "true"
        return parse_integer(raw, "Confirmation count")

    def _put_action(self, silent: bool = False) -> bool:
        try:
            group = self.action_vars["group"].get()
            if group not in available_action_groups(self.data):
                raise WorkflowEditorError("Choose an action phase")
            action_id = self.action_vars["id"].get().strip()
            kind = self.action_vars["kind"].get()
            field, op = self.action_vars["field"].get(), self.action_vars["op"].get()
            if not action_id or kind not in {"press", "left_click", "right_click", "drag"}:
                raise WorkflowEditorError("Action ID and kind are required")
            confirmation = {"sensor": self.action_vars["sensor"].get().strip(), "field": field, "op": op}
            value = self._condition_value(field, op, self.action_vars["value"].get())
            if value is not None:
                confirmation["value"] = value
            item: dict[str, Any] = {
                "id": action_id, "kind": kind, "confirmation": confirmation,
                "require_present": [value.strip() for value in self.action_vars["requires"].get().split(",") if value.strip()],
                "timeout": parse_number(self.action_vars["timeout"].get() or "2", "Timeout", 0.01, 60),
                "retries": parse_integer(self.action_vars["retries"].get() or "1", "Retries", 0, 20),
                "mutating": bool(self.action_mutating.get()),
            }
            if kind == "press":
                item["key"] = self.action_vars["key"].get().strip()
                if not item["key"]:
                    raise WorkflowEditorError("Press actions require a key")
            else:
                item["target"] = self.action_vars["target"].get().strip()
                if not item["target"]:
                    raise WorkflowEditorError("Mouse actions require a target binding")
                if kind == "drag":
                    item["destination"] = self.action_vars["destination"].get().strip()
                    if not item["destination"]:
                        raise WorkflowEditorError("Drag actions require a destination binding")
            found = None
            for existing_group in available_action_groups(self.data):
                for index, existing in enumerate(action_group(self.data, existing_group)):
                    if isinstance(existing, Mapping) and existing.get("id") == action_id:
                        found = existing_group, index
            if found:
                if found[0] == group:
                    action_group(self.data, group)[found[1]] = item
                else:
                    action_group(self.data, found[0]).pop(found[1])
                    action_group(self.data, group).append(item)
            else:
                action_group(self.data, group).append(item)
            self._refresh_action_tree()
            if not silent:
                self.message.set(f"Action {action_id} updated in {group}.")
            return True
        except Exception as exc:
            self._show_error(exc)
            return False

    def _remove_action(self) -> None:
        selected = self._selected_action()
        if selected:
            group, index, _item = selected
            action_group(self.data, group).pop(index)
            self._refresh_action_tree()

    def _refresh_visual_trees(self) -> None:
        self.binding_tree.delete(*self.binding_tree.get_children())
        bindings = self.data.get("bindings")
        if isinstance(bindings, Mapping):
            for binding_id, value in sorted(bindings.items()):
                if isinstance(value, Mapping):
                    self.binding_tree.insert("", "end", iid=str(binding_id), values=(binding_id, value.get("name", ""), display_region(value.get("region"))))
        self.visual_tree.delete(*self.visual_tree.get_children())
        visuals = self.data.get("visual")
        if isinstance(visuals, Mapping):
            for visual_id, value in sorted(visuals.items()):
                if isinstance(value, Mapping):
                    asset = value.get("template") or value.get("digits") or ""
                    region = value.get("search_region") or value.get("count_region")
                    self.visual_tree.insert("", "end", iid=str(visual_id), values=(visual_id, value.get("kind", ""), asset, display_region(region)))

    def _select_binding(self, _event: Any = None) -> None:
        selected = self.binding_tree.selection()
        bindings = self.data.get("bindings")
        if selected and isinstance(bindings, Mapping):
            item = bindings.get(selected[0])
            if isinstance(item, Mapping):
                self.binding_vars["id"].set(selected[0])
                self.binding_vars["name"].set(str(item.get("name", "")))
                self.binding_vars["region"].set(display_region(item.get("region")))

    def _put_binding(self, silent: bool = False) -> bool:
        try:
            binding_id, name = self.binding_vars["id"].get().strip(), self.binding_vars["name"].get().strip()
            if not binding_id or not name:
                raise WorkflowEditorError("Binding ID and detector ID are required")
            bindings = self.data.setdefault("bindings", {})
            if not isinstance(bindings, dict):
                raise WorkflowEditorError("bindings must be an object")
            bindings[binding_id] = {"region": parse_region(self.binding_vars["region"].get()), "name": name}
            self._refresh_visual_trees()
            if not silent:
                self.message.set(f"Binding {binding_id} updated in the draft.")
            return True
        except Exception as exc:
            self._show_error(exc)
            return False

    def _remove_binding(self) -> None:
        selected = self.binding_tree.selection()
        bindings = self.data.get("bindings")
        if selected and isinstance(bindings, dict):
            bindings.pop(selected[0], None)
            self._refresh_visual_trees()

    def _select_visual(self, _event: Any = None) -> None:
        selected = self.visual_tree.selection()
        visuals = self.data.get("visual")
        if not selected or not isinstance(visuals, Mapping):
            return
        item = visuals.get(selected[0])
        if not isinstance(item, Mapping):
            return
        context = item.get("context") if isinstance(item.get("context"), Mapping) else {}
        digits = item.get("digits")
        digits_dir = ""
        if isinstance(digits, Mapping) and digits:
            parents = {str(Path(str(path)).parent) for path in digits.values()}
            digits_dir = parents.pop() if len(parents) == 1 else ""
        values = {
            "id": selected[0], "kind": item.get("kind", "template"),
            "context_template": context.get("template", ""),
            "context_region": display_region(context.get("region")),
            "template": item.get("template", ""),
            "region": display_region(item.get("search_region") or item.get("count_region")),
            "score": item.get("score", 0.95), "context_score": context.get("score", 0.95),
            "digits_dir": digits_dir,
        }
        for key, value in values.items():
            self.visual_vars[key].set(str(value))

    def _put_visual(self, silent: bool = False) -> bool:
        try:
            visual_id, kind = self.visual_vars["id"].get().strip(), self.visual_vars["kind"].get()
            if not visual_id or kind not in {"template", "count"}:
                raise WorkflowEditorError("Detector ID and kind are required")
            context_template = self.visual_vars["context_template"].get().strip()
            if not context_template:
                raise WorkflowEditorError("Every detector requires a context template")
            visuals = self.data.setdefault("visual", {})
            if not isinstance(visuals, dict):
                raise WorkflowEditorError("visual must be an object")
            prior = visuals.get(visual_id)
            same_kind = isinstance(prior, Mapping) and prior.get("kind") == kind
            item: dict[str, Any] = deepcopy(dict(prior)) if same_kind else {}
            prior_context = item.get("context")
            context = deepcopy(dict(prior_context)) if isinstance(prior_context, Mapping) else {}
            context.update({
                "template": context_template,
                "region": parse_region(self.visual_vars["context_region"].get(), "Context region"),
                "score": parse_number(self.visual_vars["context_score"].get() or "0.95", "Context score", 0, 1),
            })
            item.update({
                "kind": kind, "context": context,
                "score": parse_number(self.visual_vars["score"].get() or "0.95", "Match score", 0, 1),
            })
            if kind == "template":
                for key in ("digits", "count_region", "foreground", "tolerance", "max_digits"):
                    item.pop(key, None)
                item["template"] = self.visual_vars["template"].get().strip()
                item["search_region"] = parse_region(self.visual_vars["region"].get(), "Search region")
                if not item["template"]:
                    raise WorkflowEditorError("Template detectors require a match template")
            else:
                for key in ("template", "search_region", "max_matches"):
                    item.pop(key, None)
                directory = self.visual_vars["digits_dir"].get().strip()
                if not directory:
                    raise WorkflowEditorError("Count detectors require a digit templates folder")
                item["digits"] = {str(number): (Path(directory) / f"{number}.png").as_posix() for number in range(10)}
                item["count_region"] = parse_region(self.visual_vars["region"].get(), "Count region")
                item.setdefault("foreground", [255, 255, 255])
                item.setdefault("tolerance", 8)
                item.setdefault("max_digits", 6)
                def exists(path_value: str) -> bool:
                    path = Path(path_value).expanduser()
                    resolved = path if path.is_absolute() else self.base_dir / path
                    return resolved.is_file()

                missing = [str(number) for number, path in item["digits"].items() if not exists(path)]
                if missing:
                    raise WorkflowEditorError(
                        f"Digit templates folder is missing: {', '.join(missing)}.png"
                    )
            visuals[visual_id] = item
            self._refresh_visual_trees()
            if not silent:
                self.message.set(f"Visual detector {visual_id} updated in the draft.")
            return True
        except Exception as exc:
            self._show_error(exc)
            return False

    def _remove_visual(self) -> None:
        selected = self.visual_tree.selection()
        visuals = self.data.get("visual")
        if selected and isinstance(visuals, dict):
            visuals.pop(selected[0], None)
            self._refresh_visual_trees()

    def _choose_capture(self) -> None:
        filename = filedialog.askopenfilename(parent=self.window, title="Open a frame saved by Compatibility", filetypes=(("Images", "*.png *.jpg *.jpeg *.bmp"), ("All files", "*")))
        if filename:
            self.capture_file.set(filename)
            self.message.set("Saved capture loaded for offline crop selection. No live verification has run.")

    def _open_crop(self) -> None:
        if self.filename is None:
            self.message.set("Save the workflow before authoring templates so their relative paths are stable.")
            return
        source = Path(self.capture_file.get()).expanduser()
        if not source.is_file():
            self.message.set("Choose a saved client capture before selecting a crop.")
            return
        try:
            _CropDialog(self, source)
        except Exception as exc:
            self.message.set(f"Cannot open saved capture: {exc}")

    def _refresh_json(self, collect: bool = True) -> None:
        try:
            if collect:
                self._collect_structured()
            text = json.dumps(self.data, indent=2, ensure_ascii=False) + "\n"
            self.json_text.delete("1.0", "end")
            self.json_text.insert("1.0", text)
            self.json_text.edit_modified(False)
        except Exception as exc:
            self._show_error(exc)

    def _apply_json(self) -> None:
        payload = self.json_text.get("1.0", "end-1c")
        if len(payload.encode("utf-8")) > MAX_DOCUMENT_BYTES:
            self._show_error(WorkflowEditorError("JSON exceeds the 512 KiB editor limit"))
            return
        try:
            value = json.loads(payload, object_pairs_hook=_unique_object)
            if not isinstance(value, dict):
                raise WorkflowEditorError("workflow document must be a JSON object")
            old = self.data
            self.data = value
            try:
                self._load_into_editor()
            except Exception:
                self.data = old
                self._load_into_editor()
                raise
            self.message.set("JSON applied to the structured editor. Validate before starting.")
        except Exception as exc:
            self._show_error(exc)

    def close(self) -> None:
        if self._busy is not None:
            self.message.set(
                "Cancel the pending activation before closing."
                if self._pending_epoch is not None else
                "Wait for the current operation to finish before closing."
            )
            return
        self.message.set(
            "Hunt Workflow is hidden; its draft and running generation are unchanged. "
            "Use Hunt Console STOP ALL for the global stop."
        )
        self.window.withdraw()


def open_window(parent: object, controller: object) -> HuntWorkflow:
    return HuntWorkflow(parent, controller)


__all__ = [
    "HuntWorkflow", "WorkflowEditorError", "action_group", "available_action_groups",
    "default_workflow_document", "display_region", "inspection_text", "load_document_file", "open_window",
    "parse_integer", "parse_number", "parse_region", "rebase_visual_paths",
    "relative_template_path", "status_text",
]
