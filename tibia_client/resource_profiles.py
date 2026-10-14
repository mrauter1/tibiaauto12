"""Bundled, image-only resource presets for explicit offline configuration.

Discovery locates label anchors in a saved client frame. It never captures a
window, changes calibration, installs a detector, or records live evidence.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from core.paths import project_path
from core.perception import DetectionError, Frame, validate_region
from tibia_client.models import Reading, Sensor, SensorKind
from tibia_client.visual import VisualProfile, _find_matches


DEFAULT_RESOURCE_PRESET_ID = "paulistinha-skills-v1"
_PRESET_DIRECTORY = project_path(f"profiles/{DEFAULT_RESOURCE_PRESET_ID}")
MAX_DISCOVERY_PIXELS = 16_777_216
MAX_RESOURCE_CANDIDATES = 8


class ResourceProfileError(ValueError):
    """The saved image cannot be used to add the selected resource preset."""


@dataclass(frozen=True)
class ResourcePresetResult:
    document: dict[str, Any]
    readings: Mapping[str, Reading]
    bindings: Mapping[str, Sensor]
    frame: Frame | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.frame is not None and not isinstance(self.frame, Frame):
            raise TypeError("resource preset frame must be an immutable Frame")
        object.__setattr__(self, "readings", MappingProxyType(dict(self.readings)))
        object.__setattr__(self, "bindings", MappingProxyType(dict(self.bindings)))


@dataclass(frozen=True)
class ResourcePanelCandidate:
    """One verified offline panel geometry offered for explicit selection."""

    bounds: tuple[int, int, int, int]
    regions: Mapping[str, tuple[int, int, int, int]]
    scale: float
    _layout: Any = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            len(self.bounds) != 4
            or any(type(value) is not int or value < 0 for value in self.bounds)
            or self.bounds[2] <= self.bounds[0]
            or self.bounds[3] <= self.bounds[1]
        ):
            raise ValueError("resource candidate bounds must be a non-empty integer region")
        if not isinstance(self.regions, Mapping):
            raise TypeError("resource candidate regions must be a mapping")
        if type(self.scale) not in (int, float) or not 0 < self.scale < 10:
            raise ValueError("resource candidate scale must be positive")
        object.__setattr__(self, "regions", MappingProxyType(dict(self.regions)))


@dataclass(frozen=True)
class ResourcePresetCandidates:
    """A bounded ambiguity result tied to the exact immutable offline frame."""

    detail: str
    candidates: tuple[ResourcePanelCandidate, ...]
    frame: Frame = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.frame, Frame):
            raise TypeError("resource candidate frame must be an immutable Frame")
        if not self.candidates or len(self.candidates) > MAX_RESOURCE_CANDIDATES:
            raise ValueError(
                f"resource candidate count must be from 1 through {MAX_RESOURCE_CANDIDATES}"
            )
        for candidate in self.candidates:
            if not isinstance(candidate, ResourcePanelCandidate):
                raise TypeError("resource candidates must contain ResourcePanelCandidate values")
            try:
                validate_region(candidate.bounds, self.frame.size)
            except (DetectionError, TypeError, ValueError) as exc:
                raise ValueError("resource candidate is outside its offline frame") from exc


def _asset_paths(spec: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    """Resolve bundled assets relative to the workflow, including other drives."""
    result = deepcopy(spec)

    def path(value: str) -> str:
        absolute = (_PRESET_DIRECTORY / value).resolve()
        try:
            return Path(os.path.relpath(absolute, base_dir)).as_posix()
        except ValueError:
            return str(absolute)

    result["context"]["template"] = path(result["context"]["template"])
    result["digits"] = {digit: path(value) for digit, value in result["digits"].items()}
    if "digit_variants" in result:
        result["digit_variants"] = {
            digit: [path(value) for value in values]
            for digit, values in result["digit_variants"].items()
        }
    if "thousands_separator" in result:
        result["thousands_separator"] = path(result["thousands_separator"])
    return result


def apply_resource_preset(
    document: Mapping[str, Any],
    frame: Frame,
    *,
    calibration_size: tuple[int, int],
    base_dir: str | Path,
) -> ResourcePresetResult:
    """Return a new draft with resource bindings located in a saved client frame.

    Existing IDs are never replaced. All anchors must be uniquely located at
    the supported rendering scale before any result is returned. Number reads
    may still be unknown; they stay explicitly unknown for inspection and may
    never authorize resource-dependent actions. Equal dimensions are a basic
    alignment check, not proof that a user-selected file is a live client frame.
    """
    if not isinstance(document, Mapping):
        raise ResourceProfileError("workflow must be an object")
    if not isinstance(frame, Frame):
        raise TypeError("resource discovery requires a Frame")
    if (
        not isinstance(calibration_size, (tuple, list))
        or len(calibration_size) != 2
        or any(type(value) is not int or value <= 0 for value in calibration_size)
    ):
        raise ResourceProfileError("calibration size must contain two positive integers")
    if frame.size != tuple(calibration_size):
        raise ResourceProfileError(
            f"saved image size {frame.size} differs from calibrated client size "
            f"{tuple(calibration_size)}; save a client frame in Compatibility Test "
            "without the desktop title bar or taskbar"
        )
    if frame.width * frame.height > MAX_DISCOVERY_PIXELS:
        raise ResourceProfileError("saved client image exceeds the resource discovery size limit")

    manifest = json.loads((_PRESET_DIRECTORY / "profile.json").read_text(encoding="utf-8"))
    resources = manifest["resources"]
    existing_bindings = document.get("bindings", {})
    existing_visuals = document.get("visual", {})
    if not isinstance(existing_bindings, Mapping) or not isinstance(existing_visuals, Mapping):
        raise ResourceProfileError("workflow bindings and visual detectors must be objects")
    collisions = set(resources).intersection(set(existing_bindings) | set(existing_visuals))
    if collisions:
        raise ResourceProfileError(
            "resource profile already has configured IDs: " + ", ".join(sorted(collisions))
            + "; keep them, or rename/remove those entries before adding the preset again"
        )

    root = Path(base_dir).expanduser().resolve()
    specs = {key: _asset_paths(item["detector"], root) for key, item in resources.items()}
    profile = VisualProfile(specs, base_path=root)
    sensors: dict[str, Sensor] = {}
    readings: dict[str, Reading] = {}
    for key, item in resources.items():
        spec = profile[key]
        anchor = spec.context_template
        if anchor.width > frame.width or anchor.height > frame.height:
            raise ResourceProfileError(f"{item['label']} label is larger than the saved client image")
        matches, _ = _find_matches(frame.pixels, anchor, spec.context_score, limit=1)
        if not matches:
            raise ResourceProfileError(
                f"{item['label']} label was not found at the supported scale; "
                "open the Skills panel and use a fresh client frame with matching UI scale"
            )
        if len(matches) != 1:
            raise ResourceProfileError(
                f"{item['label']} label appears more than once; use a client-only frame "
                "showing one Skills panel"
            )
        match = matches[0]
        dx, dy = item["anchor_offset"]
        width, height = item["binding_size"]
        left, top = match.x + dx, match.y + dy
        region = (left, top, left + width, top + height)
        try:
            validate_region(region, frame.size)
        except (DetectionError, TypeError, ValueError) as exc:
            raise ResourceProfileError(
                f"{item['label']} value would be clipped by the client image edge"
            ) from exc
        sensor = Sensor(SensorKind.VISUAL, region, key)
        sensors[key] = sensor
        readings[key] = profile.read(sensor, frame)

    result = deepcopy(dict(document))
    result["bindings"] = deepcopy(dict(existing_bindings))
    result["visual"] = deepcopy(dict(existing_visuals))
    result["bindings"].update({
        key: {"name": sensor.name, "region": list(sensor.region)}
        for key, sensor in sensors.items()
    })
    result["visual"].update(specs)
    return ResourcePresetResult(result, readings, sensors, frame)


__all__ = [
    "DEFAULT_RESOURCE_PRESET_ID", "MAX_RESOURCE_CANDIDATES", "ResourcePanelCandidate",
    "ResourcePresetCandidates", "ResourcePresetResult", "ResourceProfileError",
    "apply_resource_preset",
]


def apply_adaptive_resource_preset(
    document: Mapping[str, Any],
    frame: Frame,
    *,
    calibration_size: tuple[int, int],
    base_dir: str | Path,
    selection: tuple[int, int, int, int] | None = None,
    use_ocr: bool = False,
) -> ResourcePresetResult | ResourcePresetCandidates:
    """Add semantic, relocatable resource fields from an explicitly saved frame.

    The saved binding is a discovery hint. Runtime observations retain the same
    sensor identity while independently resolving and guarding current regions.
    This never changes capture/input calibration or proves live compatibility.
    """
    from tibia_client.adaptive import FIELDS, ResourceReader

    if not isinstance(document, Mapping):
        raise ResourceProfileError("workflow must be an object")
    if not isinstance(frame, Frame):
        raise TypeError("resource discovery requires a Frame")
    if (
        not isinstance(calibration_size, (tuple, list))
        or len(calibration_size) != 2
        or any(type(value) is not int or value <= 0 for value in calibration_size)
    ):
        raise ResourceProfileError("calibration size must contain two positive integers")
    if frame.size != tuple(calibration_size):
        raise ResourceProfileError(
            f"saved image size {frame.size} differs from calibrated client size "
            f"{tuple(calibration_size)}; use a client-only capture from Compatibility Test"
        )
    if frame.width * frame.height > MAX_DISCOVERY_PIXELS:
        raise ResourceProfileError("saved client image exceeds the resource discovery size limit")
    if selection is not None:
        try:
            selection = validate_region(selection, frame.size)
        except (DetectionError, TypeError, ValueError) as exc:
            raise ResourceProfileError("resource panel selection is outside the saved image") from exc
    if type(use_ocr) is not bool:
        raise ResourceProfileError("use_ocr must be true or false")
    existing_bindings = document.get("bindings", {})
    existing_visuals = document.get("visual", {})
    if not isinstance(existing_bindings, Mapping) or not isinstance(existing_visuals, Mapping):
        raise ResourceProfileError("workflow bindings and visual detectors must be objects")
    collisions = set(FIELDS).intersection(set(existing_bindings) | set(existing_visuals))
    if collisions:
        raise ResourceProfileError(
            "resource profile already has configured IDs: " + ", ".join(sorted(collisions))
            + "; keep them, or rename/remove those entries before adding the preset again"
        )
    reader = ResourceReader()
    try:
        discovered = reader.discover(frame, selection=selection, use_ocr=use_ocr)
    except (ValueError, RuntimeError) as exc:
        raise ResourceProfileError(str(exc)) from exc
    if discovered.layout is None and discovered.candidates:
        if len(discovered.candidates) > MAX_RESOURCE_CANDIDATES:
            raise ResourceProfileError(
                f"{discovered.detail}; found more than {MAX_RESOURCE_CANDIDATES} plausible "
                "panels. Use the advanced panel region to narrow the saved capture."
            )
        return ResourcePresetCandidates(
            discovered.detail,
            tuple(
                ResourcePanelCandidate(
                    tuple(layout.bounds), dict(layout.regions), float(layout.scale), layout
                )
                for layout in discovered.candidates
            ),
            frame,
        )
    if discovered.layout is None:
        raise ResourceProfileError(
            discovered.detail + "; open the Skills panel with its resource rows visible. "
            "Use the advanced panel region only when automatic detection cannot isolate it."
        )
    return _finish_adaptive_resource_preset(
        document,
        frame,
        discovered.layout,
        base_dir=base_dir,
        use_ocr=use_ocr,
        reader=reader,
    )


def apply_adaptive_resource_candidate(
    document: Mapping[str, Any],
    choices: ResourcePresetCandidates,
    candidate_index: int,
    *,
    base_dir: str | Path,
    use_ocr: bool = False,
) -> ResourcePresetResult:
    """Finish an ambiguous import using one choice from its immutable frame."""
    if not isinstance(document, Mapping):
        raise ResourceProfileError("workflow must be an object")
    if not isinstance(choices, ResourcePresetCandidates):
        raise ResourceProfileError("resource choices are invalid or stale")
    if type(candidate_index) is not int or not 0 <= candidate_index < len(choices.candidates):
        raise ResourceProfileError("resource panel candidate selection is out of range")
    if type(use_ocr) is not bool:
        raise ResourceProfileError("use_ocr must be true or false")
    from tibia_client.adaptive import ResourceReader

    candidate = choices.candidates[candidate_index]
    return _finish_adaptive_resource_preset(
        document,
        choices.frame,
        candidate._layout,
        base_dir=base_dir,
        use_ocr=use_ocr,
        reader=ResourceReader(),
    )


def _finish_adaptive_resource_preset(
    document: Mapping[str, Any],
    frame: Frame,
    layout: Any,
    *,
    base_dir: str | Path,
    use_ocr: bool,
    reader: Any,
) -> ResourcePresetResult:
    """Build one draft only after automatic or explicit layout resolution."""
    from tibia_client.adaptive import FIELDS
    from tibia_client.adaptive_runtime import bounded_adaptive_count

    existing_bindings = document.get("bindings", {})
    existing_visuals = document.get("visual", {})
    if not isinstance(existing_bindings, Mapping) or not isinstance(existing_visuals, Mapping):
        raise ResourceProfileError("workflow bindings and visual detectors must be objects")
    collisions = set(FIELDS).intersection(set(existing_bindings) | set(existing_visuals))
    if collisions:
        raise ResourceProfileError(
            "resource profile already has configured IDs: " + ", ".join(sorted(collisions))
            + "; keep them, or rename/remove those entries before adding the preset again"
        )
    manifest = json.loads((_PRESET_DIRECTORY / "profile.json").read_text(encoding="utf-8"))
    root = Path(base_dir).expanduser().resolve()
    specs = {key: _asset_paths(item["detector"], root)
             for key, item in manifest["resources"].items()}
    for key, spec in specs.items():
        spec["adaptive"] = {"reader": DEFAULT_RESOURCE_PRESET_ID, "field": key}
        if use_ocr:
            spec["adaptive"]["use_ocr"] = True
    sensors = {key: Sensor(SensorKind.VISUAL, layout.bounds, key) for key in FIELDS}
    readings = {
        key: bounded_adaptive_count(reading, specs[key]["max_digits"])
        for key, reading in reader.read(frame, layout, use_ocr=use_ocr).items()
    }
    result = deepcopy(dict(document))
    result["bindings"] = deepcopy(dict(existing_bindings))
    result["visual"] = deepcopy(dict(existing_visuals))
    result["bindings"].update({key: {"name": sensor.name, "region": list(sensor.region)}
                               for key, sensor in sensors.items()})
    result["visual"].update(specs)
    return ResourcePresetResult(result, readings, sensors, frame)


__all__.extend(("apply_adaptive_resource_candidate", "apply_adaptive_resource_preset"))
