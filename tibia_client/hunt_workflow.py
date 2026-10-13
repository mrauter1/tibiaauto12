"""Strict composition service for hunt workflow documents and live controls."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
from types import MappingProxyType
from typing import Any, Mapping

from PIL import Image, UnidentifiedImageError

from conf.conf_manager import ConfManager
from core.Getters import validate_calibration
from core.perception import Frame
from core.paths import project_path, scripts_path
from engine.CaveBot.routes import Route, load_route
from features.cavebot import SessionConfig, TargetSpec
from features.hunt_cycle import HuntCycleFeature
from tibia_client.compatibility import ProbeFeature, calibration_digest
from tibia_client.hunt_plan import HuntPlan, hunt_plan_from_data
from tibia_client.models import Observation, Quality, Reading, Sensor, SensorKind
from tibia_client.resource_profiles import (
    MAX_DISCOVERY_PIXELS,
    ResourcePresetResult,
    apply_resource_preset as apply_bundled_resource_preset,
)
from tibia_client.visual import VisualProfile


WORKFLOW_SCHEMA = "tibiaauto.hunt-workflow"
WORKFLOW_VERSION = 1
MAX_DOCUMENT_BYTES = 512 * 1024
MAX_BINDINGS = 64
MAX_SENSOR_PIXELS = 2_000_000
MAX_TARGETS = 64
MAX_RESOURCE_FRAME_BYTES = 32 * 1024 * 1024


class HuntWorkflowError(ValueError):
    """A workflow document cannot be compiled safely for the current profile."""


@dataclass(frozen=True)
class WorkflowControlToken:
    """Opaque UI token for one queued workflow activation."""

    session_epoch: int
    operation_revision: int

    def __post_init__(self) -> None:
        if type(self.session_epoch) is not int or self.session_epoch < 0:
            raise ValueError("session_epoch must be a non-negative integer")
        if type(self.operation_revision) is not int or self.operation_revision < 0:
            raise ValueError("operation_revision must be a non-negative integer")


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _strict(
    value: object,
    label: str,
    required: set[str],
    optional: set[str] = frozenset(),
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HuntWorkflowError(f"{label} must be an object")
    keys = set(value)
    missing = required - keys
    unknown = keys - required - optional
    if missing:
        raise HuntWorkflowError(f"{label} is missing field(s): {', '.join(sorted(missing))}")
    if unknown:
        raise HuntWorkflowError(
            f"{label} has unknown field(s): {', '.join(sorted(str(item) for item in unknown))}"
        )
    return value


def _region(value: object, label: str) -> tuple[int, int, int, int]:
    try:
        result = tuple(value)  # type: ignore[arg-type]
    except TypeError:
        raise HuntWorkflowError(f"{label} must contain four non-negative integer bounds") from None
    if (
        len(result) != 4
        or any(type(item) is not int or item < 0 for item in result)
        or result[2] <= result[0]
        or result[3] <= result[1]
    ):
        raise HuntWorkflowError(f"{label} must contain valid left, top, right, bottom bounds")
    if (result[2] - result[0]) * (result[3] - result[1]) > MAX_SENSOR_PIXELS:
        raise HuntWorkflowError(f"{label} exceeds the {MAX_SENSOR_PIXELS} pixel limit")
    return result  # type: ignore[return-value]


def _box(calibration: Mapping[str, Any], name: str) -> tuple[int, int, int, int]:
    try:
        entry = calibration["Boxes"][name][0]
    except (KeyError, IndexError, TypeError):
        raise HuntWorkflowError(f"current calibration has no Boxes.{name}") from None
    if not isinstance(entry, Mapping) or entry.get("Stats") is not True:
        raise HuntWorkflowError(f"current calibration Boxes.{name} is not valid")
    return _region(tuple(entry.get(key) for key in ("x", "y", "w", "h")), f"Boxes.{name}")


def _route_names(plan: Mapping[str, Any]) -> tuple[str, ...]:
    names: list[str] = []
    hunt = plan.get("hunt_route")
    if isinstance(hunt, str) and hunt:
        names.append(hunt)
    travel = plan.get("travel")
    if isinstance(travel, Mapping):
        for phase in ("outbound", "return"):
            raw = travel.get(phase)
            segments = raw if isinstance(raw, list) else [raw]
            for segment in segments:
                if isinstance(segment, Mapping) and isinstance(segment.get("route"), str):
                    names.append(segment["route"])
    return tuple(dict.fromkeys(names))


def _canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


@dataclass(frozen=True)
class CompiledHuntWorkflow:
    """Immutable, fully resolved workflow ready for one session activation."""

    document: Mapping[str, Any]
    plan: HuntPlan
    bindings: Mapping[str, Sensor]
    visual_profile: VisualProfile
    targets: tuple[TargetSpec, ...]
    navigation: SessionConfig
    map_region: tuple[int, int, int, int]
    battle_region: tuple[int, int, int, int]
    binding_revision: str
    context_fingerprint: str | None
    calibration_digest: str
    checkpoint_path: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "document", _freeze(self.document))
        object.__setattr__(self, "bindings", MappingProxyType(dict(self.bindings)))


@dataclass(frozen=True)
class CompiledInspection:
    """Immutable detector-only result used while a plan is still incomplete."""

    document: Mapping[str, Any]
    bindings: Mapping[str, Sensor]
    visual_profile: VisualProfile
    binding_revision: str
    context_fingerprint: str | None
    calibration_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "document", _freeze(self.document))
        object.__setattr__(self, "bindings", MappingProxyType(dict(self.bindings)))


class HuntWorkflowController:
    """Compile workflow drafts and serialize their commands through ClientSession."""

    def __init__(self, session: Any, calibration_profile: str | Path, profile_controller: Any):
        required = (
            "diagnostics",
            "configure_visual_profile",
            "start_feature",
            "start_probe",
            "stop_feature",
            "feature_status",
        )
        if any(not callable(getattr(session, method, None)) for method in required):
            raise TypeError("session does not provide the hunt workflow control surface")
        if not isinstance(calibration_profile, (str, Path)) or not str(calibration_profile):
            raise ValueError("calibration_profile must name a saved calibration JSON file")
        for method in ("set_adapter_capabilities", "begin_probe", "validate_probe_token"):
            if not callable(getattr(profile_controller, method, None)):
                raise TypeError(f"profile_controller must provide {method}()")
        self.session = session
        self.calibration_profile = calibration_profile
        self.profile_controller = profile_controller
        self._lock = threading.RLock()
        self._feature: HuntCycleFeature | None = None
        self._generation: int | None = None
        self._probe: ProbeFeature | None = None
        self._probe_generation: int | None = None
        self._inspection_bindings: Mapping[str, Sensor] = MappingProxyType({})
        self._inspection_error: str | None = None
        self._inspection_frame: Any = None
        self._operation_ticket = 0

    @property
    def control_epoch(self) -> WorkflowControlToken:
        """Return an opaque token binding session and controller cancellation state."""

        with self._lock:
            return WorkflowControlToken(self._session_control_epoch(), self._operation_ticket)

    def _session_control_epoch(self) -> int:
        value = self.session.control_epoch
        if type(value) is not int or value < 0:
            raise RuntimeError("session control_epoch must be a non-negative integer")
        return value

    def _begin_operation(self, expected_stop_epoch: WorkflowControlToken | None) -> tuple[int, int]:
        with self._lock:
            session_epoch = self._session_control_epoch()
            current_epoch = WorkflowControlToken(session_epoch, self._operation_ticket)
            if expected_stop_epoch is None:
                expected_stop_epoch = current_epoch
            elif not isinstance(expected_stop_epoch, WorkflowControlToken):
                raise ValueError("expected_stop_epoch must be a WorkflowControlToken")
            if expected_stop_epoch != current_epoch:
                raise RuntimeError("client controls changed before workflow preparation began")
            self._operation_ticket += 1
            return self._operation_ticket, session_epoch

    def _require_operation(self, ticket: int) -> None:
        if ticket != self._operation_ticket:
            raise RuntimeError("workflow activation was cancelled while preparing")

    @staticmethod
    def _wrapper(data: object) -> Mapping[str, Any]:
        value = _strict(
            data,
            "workflow",
            {"schema", "version", "plan", "bindings", "visual", "targets", "navigation"},
        )
        if value["schema"] != WORKFLOW_SCHEMA:
            raise HuntWorkflowError(f"workflow schema must be {WORKFLOW_SCHEMA!r}")
        if type(value["version"]) is not int or value["version"] != WORKFLOW_VERSION:
            raise HuntWorkflowError(f"unsupported workflow version: {value['version']!r}")
        return value

    def _calibration(self) -> Mapping[str, Any]:
        try:
            value = ConfManager.get(self.calibration_profile)
        except Exception as exc:
            raise HuntWorkflowError(f"cannot load current calibration: {exc}") from exc
        if not isinstance(value, Mapping):
            raise HuntWorkflowError("current calibration must be an object")
        try:
            validate_calibration(value)
        except Exception as exc:
            raise HuntWorkflowError(f"current calibration is invalid: {exc}") from exc
        return value

    def apply_resource_preset(
        self,
        data: object,
        frame_path: str | Path,
        base_dir: str | Path,
    ) -> ResourcePresetResult:
        """Apply bundled detectors to a draft using one offline client-local frame.

        This authoring operation neither installs the visual profile nor starts a
        probe. The normal Inspect and Start compatibility gates remain required.
        """
        if not isinstance(data, Mapping):
            raise HuntWorkflowError("workflow document must be an object")
        try:
            calibration = ConfManager.get(self.calibration_profile)
        except Exception as exc:
            raise HuntWorkflowError(f"cannot load current calibration: {exc}") from exc
        if not isinstance(calibration, Mapping):
            raise HuntWorkflowError("current calibration must be an object")
        size = calibration.get("calibration_size")
        if (
            not isinstance(size, (list, tuple))
            or len(size) != 2
            or any(type(item) is not int or item <= 0 for item in size)
        ):
            raise HuntWorkflowError(
                "current calibration has no valid calibration_size; recalibrate the client"
            )
        path = Path(frame_path).expanduser()
        try:
            stat = path.stat()
            if not path.is_file() or stat.st_size > MAX_RESOURCE_FRAME_BYTES:
                raise HuntWorkflowError(
                    f"saved resource frame must be no larger than "
                    f"{MAX_RESOURCE_FRAME_BYTES // (1024 * 1024)} MiB"
                )
            with Image.open(path) as image:
                if image.size != (size[0], size[1]):
                    raise HuntWorkflowError(
                        f"saved image size {image.size} differs from calibrated client size "
                        f"{(size[0], size[1])}; save a client frame in Compatibility Test "
                        "without the desktop title bar or taskbar"
                    )
                if image.width * image.height > MAX_DISCOVERY_PIXELS:
                    raise HuntWorkflowError(
                        "saved client image exceeds the resource discovery size limit"
                    )
                frame = Frame(image.convert("RGB"), source=f"offline:{path.name}")
        except HuntWorkflowError:
            raise
        except (OSError, ValueError, UnidentifiedImageError) as exc:
            raise HuntWorkflowError(f"cannot read saved resource frame: {exc}") from exc
        try:
            return apply_bundled_resource_preset(
                data,
                frame,
                calibration_size=(size[0], size[1]),
                base_dir=base_dir,
            )
        except HuntWorkflowError:
            raise
        except Exception as exc:
            raise HuntWorkflowError(f"cannot apply default resource profile: {exc}") from exc

    def _compile_bindings(
        self,
        wrapper: Mapping[str, Any],
        base_dir: str | Path,
    ) -> tuple[VisualProfile, Mapping[str, Sensor]]:
        visual_raw = wrapper["visual"]
        binding_raw = wrapper["bindings"]
        if not isinstance(visual_raw, Mapping):
            raise HuntWorkflowError("visual must be an object")
        if not isinstance(binding_raw, Mapping):
            raise HuntWorkflowError("bindings must be an object")
        if len(binding_raw) > MAX_BINDINGS:
            raise HuntWorkflowError(f"bindings may contain at most {MAX_BINDINGS} sensors")
        try:
            visual = VisualProfile.from_data(visual_raw, base_path=base_dir)
        except Exception as exc:
            raise HuntWorkflowError(f"visual: {exc}") from exc
        bindings: dict[str, Sensor] = {}
        for semantic_id, raw in binding_raw.items():
            if not isinstance(semantic_id, str) or not semantic_id.isidentifier():
                raise HuntWorkflowError("binding ids must be non-empty identifiers")
            item = _strict(raw, f"bindings.{semantic_id}", {"region", "name"})
            detector = item["name"]
            if not isinstance(detector, str) or not detector.strip():
                raise HuntWorkflowError(f"bindings.{semantic_id}.name must be non-empty text")
            if detector not in visual:
                raise HuntWorkflowError(
                    f"bindings.{semantic_id}.name refers to unknown visual spec {detector!r}"
                )
            region = _region(item["region"], f"bindings.{semantic_id}.region")
            spec = visual[detector]
            width, height = region[2] - region[0], region[3] - region[1]
            subregions = (spec.context_region, spec.search_region)
            if any(
                value is not None and (value[2] > width or value[3] > height)
                for value in subregions
            ):
                raise HuntWorkflowError(
                    f"bindings.{semantic_id}.region is smaller than visual spec {detector!r}"
                )
            bindings[semantic_id] = Sensor(SensorKind.VISUAL, region, detector)
        return visual, MappingProxyType(bindings)

    @staticmethod
    def _targets(value: object) -> tuple[TargetSpec, ...]:
        if not isinstance(value, list):
            raise HuntWorkflowError("targets must be a list")
        if len(value) > MAX_TARGETS:
            raise HuntWorkflowError(f"targets may contain at most {MAX_TARGETS} entries")
        targets = []
        for index, raw in enumerate(value):
            item = _strict(raw, f"targets[{index}]", {"name", "priority"})
            try:
                target = TargetSpec(item["priority"], item["name"])
            except (TypeError, ValueError) as exc:
                raise HuntWorkflowError(f"targets[{index}]: {exc}") from exc
            if Path(target.name).name != target.name or any(
                separator in target.name for separator in ("/", "\\")
            ):
                raise HuntWorkflowError(f"targets[{index}].name must be an asset name, not a path")
            asset = Path(project_path(Path("images") / "Targets" / "Names" / f"{target.name}.png"))
            if not asset.is_file():
                raise HuntWorkflowError(
                    f"targets[{index}] has no battle-list asset: {target.name}.png"
                )
            targets.append(target)
        folded = [item.name.casefold() for item in targets]
        if len(folded) != len(set(folded)):
            raise HuntWorkflowError("target names must be unique")
        return tuple(sorted(targets))

    @staticmethod
    def _validate_binding_bounds(
        bindings: Mapping[str, Sensor], calibration: Mapping[str, Any]
    ) -> None:
        size = calibration.get("calibration_size")
        if (
            not isinstance(size, (list, tuple))
            or len(size) != 2
            or any(type(item) is not int or item <= 0 for item in size)
        ):
            raise HuntWorkflowError(
                "current calibration has no valid calibration_size; recalibrate the client"
            )
        width, height = size
        outside = [
            sensor_id
            for sensor_id, sensor in bindings.items()
            if sensor.region[2] > width or sensor.region[3] > height
        ]
        if outside:
            raise HuntWorkflowError(
                "binding region(s) outside current calibration: " + ", ".join(sorted(outside))
            )

    @staticmethod
    def _navigation(value: object) -> SessionConfig:
        if not isinstance(value, Mapping):
            raise HuntWorkflowError("navigation must be an object")
        allowed = {item.name for item in fields(SessionConfig)}
        unknown = set(value) - allowed
        if unknown:
            raise HuntWorkflowError(
                "navigation has unknown field(s): "
                + ", ".join(sorted(str(item) for item in unknown))
            )
        try:
            return SessionConfig(**dict(value))
        except (TypeError, ValueError) as exc:
            raise HuntWorkflowError(f"navigation: {exc}") from exc

    @staticmethod
    def _target_manifest(targets: tuple[TargetSpec, ...]) -> list[dict[str, Any]]:
        result = []
        for target in targets:
            asset = Path(project_path(Path("images") / "Targets" / "Names" / f"{target.name}.png"))
            try:
                digest = hashlib.sha256(asset.read_bytes()).hexdigest()
            except OSError as exc:
                raise HuntWorkflowError(
                    f"cannot fingerprint target asset {asset.name}: {exc}"
                ) from exc
            result.append({**asdict(target), "asset_sha256": digest})
        return result

    @staticmethod
    def _require_count_detectors(
        plan: HuntPlan,
        bindings: Mapping[str, Sensor],
        visual: VisualProfile,
    ) -> None:
        ids = {supply.sensor_id for supply in plan.supplies}
        for group in (
            plan.prepare,
            *(segment.before for segment in plan.outbound),
            *(segment.after for segment in plan.outbound),
            *(segment.before for segment in plan.return_segments),
            *(segment.after for segment in plan.return_segments),
            plan.deposit,
            plan.sell,
            plan.refill,
        ):
            ids.update(
                action.confirmation.sensor_id
                for action in group
                if action.confirmation.field.value == "count"
            )
        if plan.loot is not None:
            ids.update((plan.loot.capacity_id, *plan.loot.item_ids))
            if plan.loot.destination_count_id is not None:
                ids.add(plan.loot.destination_count_id)
        for sensor_id in sorted(ids):
            sensor = bindings.get(sensor_id)
            if sensor is not None and visual[sensor.name].kind != "count":
                raise HuntWorkflowError(f"binding {sensor_id!r} must use a count visual spec")

    def _binding_revision(
        self,
        calibration: Mapping[str, Any],
        visual: VisualProfile,
        bindings: Mapping[str, Sensor],
        navigation: SessionConfig | None,
        targets: tuple[TargetSpec, ...],
        context_fingerprint: str | None,
    ) -> str:
        payload = {
            "calibration": calibration_digest(calibration),
            "context": context_fingerprint,
            "visual": visual.fingerprint,
            "bindings": {
                key: {"region": list(sensor.region), "name": sensor.name}
                for key, sensor in sorted(bindings.items())
            },
            "navigation": None if navigation is None else asdict(navigation),
            "targets": self._target_manifest(targets),
        }
        return hashlib.sha256(
            json.dumps(_canonical(payload), sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def _context_identity(self) -> tuple[str | None, str | None]:
        context = getattr(self.profile_controller, "context", None)
        if context is None:
            return None, None
        fingerprint = getattr(context, "fingerprint", None)
        digest = getattr(context, "calibration_digest", None)
        if not isinstance(fingerprint, str) or not fingerprint:
            raise HuntWorkflowError("loaded Compatibility context has no valid fingerprint")
        if not isinstance(digest, str) or not digest:
            raise HuntWorkflowError("loaded Compatibility context has no calibration digest")
        return fingerprint, digest

    def _compile_context(self, calibration: Mapping[str, Any]) -> tuple[str | None, str]:
        expected_digest = calibration_digest(calibration)
        fingerprint, loaded_digest = self._context_identity()
        if fingerprint is not None and loaded_digest != expected_digest:
            raise HuntWorkflowError(
                "loaded Compatibility context belongs to different calibration data; reload it"
            )
        return fingerprint, expected_digest

    def validate(self, data: object, base_dir: str | Path) -> CompiledHuntWorkflow:
        wrapper = self._wrapper(data)
        visual, bindings = self._compile_bindings(wrapper, base_dir)
        if not isinstance(wrapper["plan"], Mapping):
            raise HuntWorkflowError("plan must be an object")
        routes: dict[str, Route] = {}
        for name in _route_names(wrapper["plan"]):
            try:
                routes[name] = load_route(name)
            except Exception as exc:
                raise HuntWorkflowError(f"route {name!r}: {exc}") from exc
        try:
            plan = hunt_plan_from_data(wrapper["plan"], routes)
        except Exception as exc:
            raise HuntWorkflowError(f"plan: {exc}") from exc
        missing = set(plan.sensor_ids()) - set(bindings)
        if missing:
            raise HuntWorkflowError("plan has no binding(s): " + ", ".join(sorted(missing)))
        if plan.loot is not None and plan.loot.destination_count_id is None:
            raise HuntWorkflowError(
                "plan loot.destination_count is required for verified source and destination deltas"
            )
        self._require_count_detectors(plan, bindings, visual)
        targets = self._targets(wrapper["targets"])
        navigation = self._navigation(wrapper["navigation"])
        calibration = self._calibration()
        self._validate_binding_bounds(bindings, calibration)
        map_region = _box(calibration, "MapBox")
        battle_region = _box(calibration, "BattleBox")
        context_fingerprint, current_calibration_digest = self._compile_context(calibration)
        revision = self._binding_revision(
            calibration, visual, bindings, navigation, targets, context_fingerprint
        )
        checkpoint = scripts_path(f"hunt-{plan.id}.state.json")
        return CompiledHuntWorkflow(
            wrapper,
            plan,
            bindings,
            visual,
            targets,
            navigation,
            map_region,
            battle_region,
            revision,
            context_fingerprint,
            current_calibration_digest,
            checkpoint,
        )

    def _compile_inspection(self, data: object, base_dir: str | Path) -> CompiledInspection:
        wrapper = self._wrapper(data)
        visual, bindings = self._compile_bindings(wrapper, base_dir)
        calibration = self._calibration()
        self._validate_binding_bounds(bindings, calibration)
        context_fingerprint, current_calibration_digest = self._compile_context(calibration)
        revision = self._binding_revision(
            calibration, visual, bindings, None, (), context_fingerprint
        )
        return CompiledInspection(
            wrapper,
            bindings,
            visual,
            revision,
            context_fingerprint,
            current_calibration_digest,
        )

    @staticmethod
    def load(path: str | Path) -> dict[str, Any]:
        filename = Path(path)
        try:
            if filename.stat().st_size > MAX_DOCUMENT_BYTES:
                raise HuntWorkflowError(
                    f"workflow exceeds the {MAX_DOCUMENT_BYTES // 1024} KiB limit"
                )
            payload = filename.read_bytes()
        except HuntWorkflowError:
            raise
        except OSError as exc:
            raise HuntWorkflowError(f"cannot read workflow {filename}: {exc}") from exc
        if len(payload) > MAX_DOCUMENT_BYTES:
            raise HuntWorkflowError(f"workflow exceeds the {MAX_DOCUMENT_BYTES // 1024} KiB limit")

        def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise HuntWorkflowError(f"duplicate JSON key: {key}")
                result[key] = value
            return result

        try:
            value = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=unique,
                parse_constant=lambda item: (_ for _ in ()).throw(
                    HuntWorkflowError(f"non-finite JSON number is not allowed: {item}")
                ),
            )
        except HuntWorkflowError:
            raise
        except UnicodeDecodeError as exc:
            raise HuntWorkflowError("workflow must be UTF-8 JSON") from exc
        except json.JSONDecodeError as exc:
            raise HuntWorkflowError(
                f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
            ) from exc
        if not isinstance(value, dict):
            raise HuntWorkflowError("workflow document must be a JSON object")
        return value

    @staticmethod
    def save(path: str | Path, data: object) -> Path:
        filename = Path(path)
        HuntWorkflowController._wrapper(data)
        try:
            payload = (
                json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False).encode("utf-8")
                + b"\n"
            )
        except (TypeError, ValueError) as exc:
            raise HuntWorkflowError(f"workflow is not JSON serializable: {exc}") from exc
        if len(payload) > MAX_DOCUMENT_BYTES:
            raise HuntWorkflowError(f"workflow exceeds the {MAX_DOCUMENT_BYTES // 1024} KiB limit")
        filename.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            descriptor, name = tempfile.mkstemp(
                prefix=f".{filename.name}.", suffix=".tmp", dir=str(filename.parent)
            )
            temporary = Path(name)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, filename)
            temporary = None
            return filename
        except OSError as exc:
            raise HuntWorkflowError(f"cannot atomically save workflow {filename}: {exc}") from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass

    def _ensure_idle(self) -> None:
        diagnostics = self.session.diagnostics()
        active = tuple(getattr(diagnostics, "active_features", ()))
        paused = tuple(getattr(diagnostics, "paused_features", ()))
        if active or paused:
            names = ", ".join(sorted(set((*active, *paused))))
            raise RuntimeError(
                f"stop all active or paused features before installing workflow detectors: {names}"
            )

    def _install(self, profile: VisualProfile, expected_epoch: int) -> int:
        self._ensure_idle()
        installed_epoch = self.session.configure_visual_profile(
            profile, expected_stop_epoch=expected_epoch
        )
        self.profile_controller.set_adapter_capabilities(self.session.diagnostics().capabilities)
        if type(installed_epoch) is not int:
            raise RuntimeError("session did not return a detector installation control epoch")
        return installed_epoch

    def _require_live_context(self) -> None:
        fingerprint, _digest = self._context_identity()
        if fingerprint is None:
            raise RuntimeError(
                "load and verify the current Compatibility profile before Start or Inspect"
            )

    def _require_compiled_context(
        self, expected_fingerprint: str | None, expected_calibration_digest: str
    ) -> None:
        self._require_live_context()
        fingerprint, digest = self._context_identity()
        if fingerprint != expected_fingerprint or digest != expected_calibration_digest:
            raise RuntimeError(
                "Compatibility context changed while compiling the workflow; validate and start again"
            )

    def start(
        self,
        data: object,
        base_dir: str | Path,
        *,
        resume: bool = True,
        expected_stop_epoch: WorkflowControlToken | None = None,
    ) -> CompiledHuntWorkflow:
        if type(resume) is not bool:
            raise ValueError("resume must be boolean")
        ticket, expected_epoch = self._begin_operation(expected_stop_epoch)
        compiled = self.validate(data, base_dir)
        self._require_compiled_context(compiled.context_fingerprint, compiled.calibration_digest)
        feature = HuntCycleFeature(
            compiled.plan,
            compiled.bindings,
            binding_revision=compiled.binding_revision,
            targets=compiled.targets,
            cavebot_config=compiled.navigation,
            map_region=compiled.map_region,
            battle_region=compiled.battle_region,
            checkpoint_path=compiled.checkpoint_path,
            resume=resume,
        )
        feature.desktop_context_fingerprint = compiled.context_fingerprint
        with self._lock:
            self._require_operation(ticket)
            installed_epoch = self._install(compiled.visual_profile, expected_epoch)
            self._require_compiled_context(
                compiled.context_fingerprint, compiled.calibration_digest
            )
            generation = self.session.start_feature(feature, expected_stop_epoch=installed_epoch)
            self._feature = feature
            self._generation = generation
            self._probe = None
            self._probe_generation = None
            self._inspection_error = None
        return compiled

    def request_return(self, *, now: bool = False) -> None:
        if type(now) is not bool:
            raise ValueError("now must be boolean")
        with self._lock:
            generation = self._generation
        if generation is None or not self.session.feature_command(
            "CaveBot", generation, "request_return", now=now
        ):
            raise RuntimeError("the original hunt workflow generation is not active")

    def acknowledge_manual(self, *, operator_reconciled: bool = False) -> None:
        if type(operator_reconciled) is not bool:
            raise ValueError("operator_reconciled must be boolean")
        if not operator_reconciled:
            self.request_recheck()
            return
        with self._lock:
            generation = self._generation
        if generation is None or not self.session.feature_command(
            "CaveBot",
            generation,
            "acknowledge_manual",
            operator_reconciled=operator_reconciled,
        ):
            raise RuntimeError("the original hunt workflow generation is not active")

    def request_recheck(self) -> None:
        with self._lock:
            generation = self._generation
        if generation is None or not self.session.feature_command(
            "CaveBot", generation, "request_recheck"
        ):
            raise RuntimeError("the workflow has no active recovery or reconciliation to re-check")

    def select_recovery_anchor(self, waypoint_index: int) -> None:
        if type(waypoint_index) is not int or waypoint_index < 0:
            raise ValueError("waypoint_index must be a non-negative integer")
        with self._lock:
            generation = self._generation
        if generation is None:
            raise RuntimeError("the original hunt workflow generation is not active")
        if not self.session.feature_command(
            "CaveBot", generation, "select_recovery_anchor", waypoint_index=waypoint_index
        ):
            raise RuntimeError(
                "the workflow is no longer awaiting this anchor; refresh the recovery status"
            )

    def stop(self) -> bool:
        with self._lock:
            self._operation_ticket += 1
            feature_generation = self._generation
            probe_generation = self._probe_generation
        stopped = False
        if feature_generation is not None:
            stopped = (
                bool(self.session.stop_feature("CaveBot", expected_generation=feature_generation))
                or stopped
            )
        if probe_generation is not None:
            stopped = (
                bool(
                    self.session.stop_feature(
                        ProbeFeature.name, expected_generation=probe_generation
                    )
                )
                or stopped
            )
        return stopped

    def status(self) -> Mapping[str, Any]:
        with self._lock:
            feature, generation = self._feature, self._generation
        if feature is None or generation is None:
            return MappingProxyType(
                {"active": False, "generation": None, "paused": False, "error": None}
            )
        runtime = self.session.feature_status("CaveBot")
        diagnostics = self.session.diagnostics()
        same = runtime.generation == generation
        paused = same and "CaveBot" in tuple(getattr(diagnostics, "paused_features", ()))
        result = feature.inspection_status()
        result.update(
            {
                "active": bool(same and (runtime.active or paused)),
                "generation": generation,
                "paused": paused,
                "error": (runtime.error if same else None) or result.get("error"),
            }
        )
        return MappingProxyType(result)

    def inspect(
        self,
        data: object,
        base_dir: str | Path,
        *,
        expected_stop_epoch: WorkflowControlToken | None = None,
    ) -> CompiledInspection:
        ticket, expected_epoch = self._begin_operation(expected_stop_epoch)
        try:
            compiled = self._compile_inspection(data, base_dir)
        except Exception as exc:
            with self._lock:
                if ticket == self._operation_ticket:
                    self._probe = None
                    self._probe_generation = None
                    self._inspection_bindings = MappingProxyType({})
                    self._inspection_frame = None
                    self._inspection_error = str(exc)
            raise
        with self._lock:
            self._inspection_bindings = compiled.bindings
            self._inspection_frame = None
            try:
                self._require_operation(ticket)
                self._require_compiled_context(
                    compiled.context_fingerprint, compiled.calibration_digest
                )
                token = self.profile_controller.begin_probe()
                self._require_compiled_context(
                    compiled.context_fingerprint, compiled.calibration_digest
                )
                token_check = self.profile_controller.validate_probe_token(token)
                require = getattr(token_check, "require", None)
                if callable(require):
                    require()
                elif token_check is False:
                    raise RuntimeError("Compatibility probe token is no longer valid")
                installed_epoch = self._install(compiled.visual_profile, expected_epoch)
                probe = ProbeFeature(
                    None,
                    None,
                    sensors=tuple(compiled.bindings.values()),
                    reason="hunt workflow observation-only inspection",
                    context_guard=lambda: self.profile_controller.validate_probe_token(token),
                )
                probe.desktop_context_fingerprint = compiled.context_fingerprint
                generation = self.session.start_probe(probe, expected_stop_epoch=installed_epoch)
                self._probe = probe
                self._probe_generation = generation
                self._inspection_error = None
            except Exception as exc:
                self._probe = None
                self._probe_generation = None
                self._inspection_error = str(exc)
                raise
        return compiled

    @staticmethod
    def _reading(value: Any) -> dict[str, Any]:
        if isinstance(value, Observation):
            return {
                "quality": value.quality.value,
                "value": value.value,
                "detail": value.detail,
                "diagnostics": value.diagnostics,
            }
        if isinstance(value, Reading):
            return {
                "quality": value.quality.value,
                "value": value.value,
                "detail": value.detail,
                "diagnostics": value.diagnostics,
            }
        if value is None:
            return {"quality": Quality.UNKNOWN.value, "value": None, "detail": "no observation"}
        return {"quality": Quality.KNOWN.value, "value": value, "detail": ""}

    def inspection_status(self) -> Mapping[str, Any]:
        with self._lock:
            probe = self._probe
            generation = self._probe_generation
            bindings = self._inspection_bindings
            error = self._inspection_error
            inspection_frame = self._inspection_frame
        if probe is None or generation is None:
            frozen_bindings = MappingProxyType(
                {
                    semantic_id: MappingProxyType({"region": sensor.region, "name": sensor.name})
                    for semantic_id, sensor in bindings.items()
                }
            )
            return MappingProxyType(
                {
                    "active": False,
                    "readings": MappingProxyType({}),
                    "bindings": frozen_bindings,
                    "frame": None,
                    "error": error,
                }
            )
        runtime = self.session.feature_status(ProbeFeature.name)
        same_generation = runtime.generation == generation
        if not same_generation:
            return MappingProxyType(
                {
                    "active": False,
                    "readings": MappingProxyType({}),
                    "bindings": MappingProxyType(
                        {
                            semantic_id: MappingProxyType(
                                {"region": sensor.region, "name": sensor.name}
                            )
                            for semantic_id, sensor in bindings.items()
                        }
                    ),
                    "frame": None,
                    "error": "inspection was replaced by another probe generation",
                }
            )
        active = runtime.active
        observations = getattr(probe, "observations", None)
        if not isinstance(observations, Mapping):
            observations = getattr(probe, "before", {})
        readings = {
            semantic_id: self._reading(observations.get(sensor))
            for semantic_id, sensor in bindings.items()
        }
        frame = inspection_frame
        if frame is None and observations:
            frame_ids = {
                value.frame_id for value in observations.values() if isinstance(value, Observation)
            }
            if len(frame_ids) == 1:
                preview = getattr(self.session, "preview_frame", None)
                if callable(preview):
                    try:
                        frame = preview(expected_frame_id=next(iter(frame_ids)))
                    except TypeError:
                        # Older sessions cannot prove frame identity; do not
                        # pair their latest frame with saved older readings.
                        frame = None
                if frame is not None:
                    with self._lock:
                        if self._probe is probe and self._probe_generation == generation:
                            self._inspection_frame = frame
        frozen_bindings = MappingProxyType(
            {
                semantic_id: MappingProxyType({"region": sensor.region, "name": sensor.name})
                for semantic_id, sensor in bindings.items()
            }
        )
        return MappingProxyType(
            {
                "active": active,
                "readings": MappingProxyType(readings),
                "bindings": frozen_bindings,
                "frame": frame,
                "error": (runtime.error if runtime.generation == generation else None) or error,
            }
        )


__all__ = [
    "CompiledHuntWorkflow",
    "CompiledInspection",
    "HuntWorkflowController",
    "HuntWorkflowError",
    "MAX_DOCUMENT_BYTES",
    "WORKFLOW_SCHEMA",
    "WORKFLOW_VERSION",
    "WorkflowControlToken",
]
