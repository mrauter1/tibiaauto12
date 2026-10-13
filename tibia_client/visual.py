"""Bounded, profile-owned visual evidence detectors.

Visual profiles contain only pixel templates and detector settings.  They do
not capture the screen, generate input, or import platform APIs.  Every read
is made from the :class:`core.perception.Frame` supplied by the adapter.

Scores returned by this module are normalized image-match scores.  They are
not probabilities or confidence estimates.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from hashlib import sha256
import json
import math
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any

import cv2
import numpy as np
from PIL import Image, UnidentifiedImageError

from core.perception import Frame, validate_region
from tibia_client.models import Quality, Reading, Sensor, SensorKind


MAX_SPECS = 128
MAX_TEMPLATE_FILE_BYTES = 2 * 1024 * 1024
MAX_TEMPLATE_SIDE = 512
MAX_TEMPLATE_PIXELS = 512 * 512
MAX_PROFILE_TEMPLATE_BYTES = 32 * 1024 * 1024
MAX_SENSOR_PIXELS = 2_000_000
MAX_VISUAL_WORK_PIXELS = 8_000_000
MAX_DIGITS = 9
MAX_MATCHES = 9
MIN_MATCH_SCORE = 0.5

_SPEC_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class VisualProfileError(ValueError):
    """A visual profile cannot be compiled safely."""


class _UnknownVisual(RuntimeError):
    """Pixels did not provide unambiguous evidence on this frame."""


@dataclass(frozen=True, slots=True)
class _Template:
    rgb: bytes = field(repr=False, compare=False)
    width: int
    height: int
    digest: str

    @property
    def size(self) -> tuple[int, int]:
        return self.width, self.height

    @property
    def pixels(self) -> np.ndarray:
        """Expose a read-only view backed by immutable bytes."""

        return np.frombuffer(self.rgb, dtype=np.uint8).reshape(self.height, self.width, 3)


@dataclass(frozen=True, slots=True)
class VisualSpec:
    """An immutable, validated detector compiled from JSON-friendly config."""

    kind: str
    template: _Template | None
    context_template: _Template
    context_region: tuple[int, int, int, int]
    context_score: float
    search_region: tuple[int, int, int, int] | None
    score: float
    ambiguity_margin: float
    max_matches: int
    digits: tuple[_Template, ...]
    foreground: tuple[int, int, int]
    tolerance: int
    max_digits: int
    fingerprint: str


def _number(value: object, label: str, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise VisualProfileError(f"{label} must be a number from {minimum} through {maximum}")
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise VisualProfileError(f"{label} must be a number from {minimum} through {maximum}")
    return number


def _integer(value: object, label: str, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise VisualProfileError(f"{label} must be an integer from {minimum} through {maximum}")
    return value


def _relative_region(value: object, label: str) -> tuple[int, int, int, int]:
    if isinstance(value, (str, bytes)):
        raise VisualProfileError(f"{label} must contain four non-negative integers")
    try:
        region = tuple(value)  # type: ignore[arg-type]
    except TypeError:
        raise VisualProfileError(f"{label} must contain four non-negative integers") from None
    if len(region) != 4 or any(type(item) is not int or item < 0 for item in region):
        raise VisualProfileError(f"{label} must contain four non-negative integers")
    left, top, right, bottom = region
    if right <= left or bottom <= top:
        raise VisualProfileError(f"{label} must be non-empty")
    if (right - left) * (bottom - top) > MAX_SENSOR_PIXELS:
        raise VisualProfileError(f"{label} exceeds the {MAX_SENSOR_PIXELS} pixel budget")
    return left, top, right, bottom


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise VisualProfileError(f"{label} must be an object")
    return value


def _only(config: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(str(key) for key in config if key not in allowed)
    if unknown:
        raise VisualProfileError(f"{label} has unknown field(s): {', '.join(unknown)}")


def _load_template(path_value: object, base_path: Path) -> _Template:
    if not isinstance(path_value, str) or not path_value.strip():
        raise VisualProfileError("template path must be non-empty text")
    path = Path(path_value).expanduser()
    resolved = (base_path / path).resolve() if not path.is_absolute() else path.resolve()
    try:
        stat = resolved.stat()
    except OSError as exc:
        raise VisualProfileError(f"template does not exist or is unreadable: {resolved}") from exc
    if not resolved.is_file() or stat.st_size > MAX_TEMPLATE_FILE_BYTES:
        raise VisualProfileError(
            f"template must be a file no larger than {MAX_TEMPLATE_FILE_BYTES} bytes: {resolved}"
        )
    try:
        with Image.open(resolved) as image:
            width, height = image.size
            if (
                width < 1
                or height < 1
                or width > MAX_TEMPLATE_SIDE
                or height > MAX_TEMPLATE_SIDE
                or width * height > MAX_TEMPLATE_PIXELS
            ):
                raise VisualProfileError(
                    f"template dimensions must be within {MAX_TEMPLATE_SIDE}x{MAX_TEMPLATE_SIDE}: {resolved}"
                )
            pixels = np.ascontiguousarray(np.asarray(image.convert("RGB"), dtype=np.uint8))
    except VisualProfileError:
        raise
    except (OSError, ValueError, UnidentifiedImageError) as exc:
        raise VisualProfileError(f"template is unreadable: {resolved}") from exc
    rgb = pixels.tobytes()
    digest = sha256(width.to_bytes(4, "big") + height.to_bytes(4, "big") + rgb).hexdigest()
    return _Template(rgb, width, height, digest)


def _canonical_fingerprint(settings: Mapping[str, Any], templates: Sequence[_Template]) -> str:
    payload = json.dumps(settings, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    digest = sha256(payload)
    for template in templates:
        digest.update(bytes.fromhex(template.digest))
    return digest.hexdigest()


def _compile_spec(config_value: object, load: Any) -> VisualSpec:
    config = _mapping(config_value, "visual spec")
    kind = config.get("kind")
    if kind not in ("template", "count"):
        raise VisualProfileError("visual spec kind must be 'template' or 'count'")

    common = {
        "kind", "context", "search_region", "score", "ambiguity_margin",
        "max_matches", "template", "digits", "foreground", "tolerance", "max_digits",
        "count_region",
    }
    _only(config, common, "visual spec")
    context = _mapping(config.get("context"), "visual context")
    _only(context, {"template", "region", "score"}, "visual context")
    context_template = load(context.get("template"))
    context_region = _relative_region(context.get("region"), "visual context region")
    if (
        context_template.width > context_region[2] - context_region[0]
        or context_template.height > context_region[3] - context_region[1]
    ):
        raise VisualProfileError("visual context template exceeds its configured region")
    context_pixels = context_template.pixels
    if bool(np.all(context_pixels == context_pixels[0, 0])):
        raise VisualProfileError("visual context template must contain identifying pixel variation")
    context_score = _number(
        context.get("score", 0.95),
        "visual context score",
        minimum=MIN_MATCH_SCORE,
        maximum=1.0,
    )
    score = _number(
        config.get("score", 0.95),
        "visual match score",
        minimum=MIN_MATCH_SCORE,
        maximum=1.0,
    )
    ambiguity_margin = _number(
        config.get("ambiguity_margin", 0.03),
        "visual ambiguity margin",
        minimum=0.0,
        maximum=1.0,
    )
    if ambiguity_margin <= 0:
        raise VisualProfileError("visual ambiguity margin must be greater than zero")
    max_matches = _integer(
        config.get("max_matches", 1), "visual max_matches", minimum=1, maximum=MAX_MATCHES
    )

    template: _Template | None = None
    digits: tuple[_Template, ...] = ()
    foreground = (255, 255, 255)
    tolerance = 0
    max_digits = 1
    if kind == "template":
        if "count_region" in config:
            raise VisualProfileError("template visual spec cannot use count_region")
        template = load(config.get("template"))
        search_region = (
            _relative_region(config["search_region"], "visual search region")
            if "search_region" in config
            else None
        )
        if search_region is not None and (
            template.width > search_region[2] - search_region[0]
            or template.height > search_region[3] - search_region[1]
        ):
            raise VisualProfileError("visual target template exceeds its configured search region")
        if any(key in config for key in ("digits", "foreground", "tolerance", "max_digits")):
            raise VisualProfileError("template visual spec has count-only fields")
        settings = {
            "kind": kind,
            "context_region": context_region,
            "context_score": context_score,
            "search_region": search_region,
            "score": score,
            "ambiguity_margin": ambiguity_margin,
            "max_matches": max_matches,
        }
        templates = (context_template, template)
    else:
        if "template" in config or "search_region" in config or "max_matches" in config:
            raise VisualProfileError("count visual spec has template-only fields")
        search_region = _relative_region(config.get("count_region"), "visual count region")
        digit_config = _mapping(config.get("digits"), "visual digits")
        if set(digit_config) != {str(number) for number in range(10)}:
            raise VisualProfileError("visual digits must define exactly the keys '0' through '9'")
        digits = tuple(load(digit_config[str(number)]) for number in range(10))
        color = config.get("foreground", [255, 255, 255])
        if isinstance(color, (str, bytes)):
            raise VisualProfileError("visual foreground must contain three byte values")
        try:
            foreground = tuple(color)  # type: ignore[arg-type,assignment]
        except TypeError:
            raise VisualProfileError("visual foreground must contain three byte values") from None
        if len(foreground) != 3 or any(type(channel) is not int or not 0 <= channel <= 255 for channel in foreground):
            raise VisualProfileError("visual foreground must contain three byte values")
        tolerance = _integer(
            config.get("tolerance", 8), "visual foreground tolerance", minimum=0, maximum=255
        )
        max_digits = _integer(
            config.get("max_digits", 6), "visual max_digits", minimum=1, maximum=MAX_DIGITS
        )
        for digit, digit_template in enumerate(digits):
            if (
                digit_template.width > search_region[2] - search_region[0]
                or digit_template.height > search_region[3] - search_region[1]
            ):
                raise VisualProfileError(
                    f"visual digit {digit} template exceeds the configured count region"
                )
            try:
                _glyph(_mask(digit_template.pixels, foreground, tolerance))
            except VisualProfileError as exc:
                raise VisualProfileError(f"visual digit {digit} template: {exc}") from exc
        settings = {
            "kind": kind,
            "context_region": context_region,
            "context_score": context_score,
            "count_region": search_region,
            "score": score,
            "ambiguity_margin": ambiguity_margin,
            "foreground": foreground,
            "tolerance": tolerance,
            "max_digits": max_digits,
        }
        templates = (context_template, *digits)

    return VisualSpec(
        kind=kind,
        template=template,
        context_template=context_template,
        context_region=context_region,
        context_score=context_score,
        search_region=search_region,
        score=score,
        ambiguity_margin=ambiguity_margin,
        max_matches=max_matches,
        digits=digits,
        foreground=foreground,
        tolerance=tolerance,
        max_digits=max_digits,
        fingerprint=_canonical_fingerprint(settings, templates),
    )


@dataclass(frozen=True, slots=True, init=False)
class VisualProfile(Mapping[str, VisualSpec]):
    """Immutable visual-spec registry compiled from an operator-owned mapping.

    The accepted input is a mapping from a short spec id to either::

        {"kind": "template", "template": "item.png",
         "context": {"template": "container.png", "region": [0, 0, 20, 20]},
         "search_region": [20, 0, 80, 40], "score": 0.95,
         "max_matches": 4}

    or::

        {"kind": "count", "digits": {"0": "0.png", ..., "9": "9.png"},
         "context": {"template": "container.png", "region": [0, 0, 20, 20]},
         "count_region": [20, 0, 80, 20], "foreground": [255, 255, 255],
         "tolerance": 8, "score": 0.95, "ambiguity_margin": 0.03,
         "max_digits": 6}

    All subregions are relative to the ``Sensor.region`` origin.  Template
    paths are relative to ``base_path``.  Context is mandatory for both kinds.
    """

    _specs: Mapping[str, VisualSpec]
    _fingerprint: str

    def __init__(
        self,
        specs: Mapping[str, object] | None = None,
        *,
        base_path: str | Path | None = None,
    ) -> None:
        source = {} if specs is None else specs
        if not isinstance(source, Mapping):
            raise VisualProfileError("visual profile must be an object")
        if len(source) > MAX_SPECS:
            raise VisualProfileError(f"visual profile may contain at most {MAX_SPECS} specs")
        root = Path.cwd() if base_path is None else Path(base_path).expanduser().resolve()
        cache: dict[Path, _Template] = {}
        template_bytes = 0

        def load(value: object) -> _Template:
            nonlocal template_bytes
            if not isinstance(value, str) or not value.strip():
                raise VisualProfileError("template path must be non-empty text")
            path = Path(value).expanduser()
            resolved = (root / path).resolve() if not path.is_absolute() else path.resolve()
            found = cache.get(resolved)
            if found is not None:
                return found
            found = _load_template(str(resolved), root)
            template_bytes += len(found.rgb)
            if template_bytes > MAX_PROFILE_TEMPLATE_BYTES:
                raise VisualProfileError(
                    f"visual profile templates exceed {MAX_PROFILE_TEMPLATE_BYTES} decoded bytes"
                )
            cache[resolved] = found
            return found

        compiled: dict[str, VisualSpec] = {}
        for spec_id, config in source.items():
            if not isinstance(spec_id, str) or _SPEC_ID.fullmatch(spec_id) is None:
                raise VisualProfileError(
                    "visual spec ids must use 1-64 ASCII letters, digits, '.', '_' or '-'"
                )
            compiled[spec_id] = _compile_spec(config, load)
        object.__setattr__(self, "_specs", MappingProxyType(compiled))
        manifest = [(spec_id, compiled[spec_id].fingerprint) for spec_id in sorted(compiled)]
        object.__setattr__(
            self,
            "_fingerprint",
            sha256(json.dumps(manifest, separators=(",", ":")).encode("ascii")).hexdigest(),
        )

    @classmethod
    def from_data(
        cls,
        specs: Mapping[str, object],
        *,
        base_path: str | Path | None = None,
    ) -> "VisualProfile":
        return cls(specs, base_path=base_path)

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    @property
    def spec_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._specs))

    def read(self, sensor: Sensor, frame: Frame) -> Reading:
        """Resolve and read a VISUAL sensor from ``frame``."""

        if not isinstance(sensor, Sensor) or sensor.kind is not SensorKind.VISUAL:
            raise ValueError("visual profile can read only SensorKind.VISUAL sensors")
        if not sensor.name.strip():
            raise ValueError("visual sensor needs a spec id")
        try:
            spec = self._specs[sensor.name]
        except KeyError:
            raise ValueError(f"visual profile has no spec {sensor.name!r}") from None
        return read_visual(spec, frame, sensor.region)

    def __getitem__(self, key: str) -> VisualSpec:
        return self._specs[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._specs)

    def __len__(self) -> int:
        return len(self._specs)


@dataclass(frozen=True, slots=True)
class _Match:
    x: int
    y: int
    width: int
    height: int
    score: float

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.width // 2, self.y + self.height // 2


def _score_map(haystack: np.ndarray, template: np.ndarray) -> np.ndarray:
    if template.shape[0] > haystack.shape[0] or template.shape[1] > haystack.shape[1]:
        raise _UnknownVisual("template is larger than its configured region")
    # Use absolute RGB error rather than normalized correlation.  Grayscale
    # matching treats chromatically different pixels with the same luminance
    # as identical, while normalized correlation can score an affine-brightness
    # impostor as a perfect match.  The fixed maximum error is well-defined for
    # constant and varying templates alike.
    hay_rgb = np.ascontiguousarray(haystack, dtype=np.uint8)
    template_rgb = np.ascontiguousarray(template, dtype=np.uint8)
    difference = cv2.matchTemplate(hay_rgb, template_rgb, cv2.TM_SQDIFF)
    maximum_error = float(template_rgb.size * (255 ** 2))
    return np.clip(
        1.0 - difference / maximum_error,
        0.0,
        1.0,
    )


def _iou(a: _Match, b: _Match) -> float:
    left, top = max(a.x, b.x), max(a.y, b.y)
    right = min(a.x + a.width, b.x + b.width)
    bottom = min(a.y + a.height, b.y + b.height)
    intersection = max(0, right - left) * max(0, bottom - top)
    if intersection == 0:
        return 0.0
    return intersection / (a.width * a.height + b.width * b.height - intersection)


def _find_matches(
    haystack: np.ndarray,
    template: _Template,
    threshold: float,
    *,
    limit: int,
) -> tuple[list[_Match], float]:
    scores = _score_map(haystack, template.pixels)
    best_score = float(np.max(scores))
    selected: list[_Match] = []
    work = scores.copy()
    height, width = template.pixels.shape[:2]
    # At most limit+1 score maxima are examined. Suppression makes the bound
    # independent of the number of above-threshold pixels in a noisy frame.
    for _ in range(limit + 1):
        _, maximum, _, position = cv2.minMaxLoc(work)
        if maximum < threshold:
            break
        candidate = _Match(position[0], position[1], width, height, float(maximum))
        if all(_iou(candidate, accepted) <= 0.3 for accepted in selected):
            selected.append(candidate)
            if len(selected) > limit:
                break
        x, y = position
        left, top = max(0, x - width + 1), max(0, y - height + 1)
        right = min(work.shape[1], x + width)
        bottom = min(work.shape[0], y + height)
        work[top:bottom, left:right] = -1.0
    return selected, best_score


def _absolute_region(
    sensor_region: tuple[int, int, int, int],
    relative: tuple[int, int, int, int] | None,
) -> tuple[int, int, int, int]:
    if relative is None:
        return sensor_region
    sensor_left, sensor_top, sensor_right, sensor_bottom = sensor_region
    left, top, right, bottom = relative
    width, height = sensor_right - sensor_left, sensor_bottom - sensor_top
    if right > width or bottom > height:
        raise ValueError(
            f"visual subregion {relative!r} exceeds sensor size {(width, height)!r}"
        )
    return sensor_left + left, sensor_top + top, sensor_left + right, sensor_top + bottom


def _crop(frame: Frame, region: tuple[int, int, int, int]) -> np.ndarray:
    left, top, right, bottom = region
    return frame.pixels[top:bottom, left:right]


def _context_match(
    spec: VisualSpec,
    frame: Frame,
    sensor_region: tuple[int, int, int, int],
) -> _Match:
    region = _absolute_region(sensor_region, spec.context_region)
    matches, _ = _find_matches(
        _crop(frame, region), spec.context_template, spec.context_score, limit=1
    )
    if not matches:
        raise _UnknownVisual("configured visual context is absent")
    if len(matches) != 1:
        raise _UnknownVisual("configured visual context is ambiguous")
    found = matches[0]
    return _Match(
        region[0] + found.x,
        region[1] + found.y,
        found.width,
        found.height,
        found.score,
    )


def _mask(image: np.ndarray, color: tuple[int, int, int], tolerance: int) -> np.ndarray:
    difference = np.abs(image.astype(np.int16) - np.asarray(color, dtype=np.int16))
    return np.max(difference, axis=2) <= tolerance


def _glyph(mask: np.ndarray) -> tuple[np.ndarray, float]:
    rows, columns = np.where(mask)
    if rows.size == 0:
        raise VisualProfileError("digit template contains no configured foreground pixels")
    cropped = mask[rows.min() : rows.max() + 1, columns.min() : columns.max() + 1]
    ratio = cropped.shape[1] / cropped.shape[0]
    normalized = cv2.resize(
        cropped.astype(np.uint8), (16, 24), interpolation=cv2.INTER_NEAREST
    ).astype(bool)
    return normalized, ratio


def _recognize_count(spec: VisualSpec, image: np.ndarray) -> tuple[int, float]:
    ink = _mask(image, spec.foreground, spec.tolerance)
    if not bool(np.any(ink)):
        raise _UnknownVisual("count region contains no configured digit foreground")
    if bool(np.any(ink[0])) or bool(np.any(ink[-1])) or bool(np.any(ink[:, 0])) or bool(np.any(ink[:, -1])):
        raise _UnknownVisual("count glyph touches the configured region edge and may be clipped")
    occupied = np.any(ink, axis=0)
    starts = np.flatnonzero(occupied & np.r_[True, ~occupied[:-1]])
    ends = np.flatnonzero(occupied & np.r_[~occupied[1:], True]) + 1
    if len(starts) == 0 or len(starts) > spec.max_digits:
        raise _UnknownVisual("count has an unsupported number of digit segments")

    reference = tuple(_glyph(_mask(item.pixels, spec.foreground, spec.tolerance)) for item in spec.digits)
    result: list[int] = []
    scores: list[float] = []
    for start, end in zip(starts, ends):
        segment = ink[:, start:end]
        candidate, ratio = _glyph(segment)
        ranked: list[tuple[float, int]] = []
        for number, (expected, expected_ratio) in enumerate(reference):
            pixel_score = 1.0 - float(np.mean(candidate != expected))
            ratio_score = min(ratio, expected_ratio) / max(ratio, expected_ratio)
            ranked.append((0.85 * pixel_score + 0.15 * ratio_score, number))
        ranked.sort(reverse=True)
        best, number = ranked[0]
        second = ranked[1][0]
        if best < spec.score or best - second <= spec.ambiguity_margin:
            raise _UnknownVisual("count contains an ambiguous digit")
        result.append(number)
        scores.append(best)
    if len(result) > 1 and result[0] == 0:
        raise _UnknownVisual("multi-digit count has a leading zero")
    return int("".join(str(number) for number in result)), min(scores)


def read_visual(
    spec: VisualSpec,
    frame: Frame,
    region: Sequence[int],
) -> Reading:
    """Read one visual spec from one immutable frame and client-local region."""

    if not isinstance(spec, VisualSpec):
        raise TypeError("visual detector needs a compiled VisualSpec")
    sensor_region = validate_region(region, frame.size)
    if (sensor_region[2] - sensor_region[0]) * (sensor_region[3] - sensor_region[1]) > MAX_SENSOR_PIXELS:
        raise ValueError(f"visual sensor exceeds the {MAX_SENSOR_PIXELS} pixel budget")
    try:
        context = _context_match(spec, frame, sensor_region)
        if spec.kind == "template":
            assert spec.template is not None
            search = _absolute_region(sensor_region, spec.search_region)
            matches, best_score = _find_matches(
                _crop(frame, search), spec.template, spec.score, limit=spec.max_matches
            )
            if len(matches) > spec.max_matches:
                raise _UnknownVisual("template match count exceeds the configured bound")
            best = max(matches, key=lambda item: item.score) if matches else None
            position = None if best is None else (
                search[0] + best.center[0], search[1] + best.center[1]
            )
            return Reading(
                {
                    "present": bool(matches),
                    "position": position,
                    "count": len(matches),
                    "score": best_score,
                }
            )

        count_region = _absolute_region(sensor_region, spec.search_region)
        count, score = _recognize_count(spec, _crop(frame, count_region))
        return Reading(
            {
                "present": True,
                "position": context.center,
                "count": count,
                "score": score,
            }
        )
    except _UnknownVisual as exc:
        return Reading(None, Quality.UNKNOWN, str(exc))


__all__ = [
    "MAX_DIGITS",
    "MAX_MATCHES",
    "MAX_SENSOR_PIXELS",
    "MAX_SPECS",
    "MAX_VISUAL_WORK_PIXELS",
    "MIN_MATCH_SCORE",
    "VisualProfile",
    "VisualProfileError",
    "VisualSpec",
    "read_visual",
]
