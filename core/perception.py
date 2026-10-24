"""Reliable, testable client capture and template matching primitives.

Coordinates are client-local. A :class:`Frame` is an immutable RGB snapshot;
pass one through detectors or use :func:`frame_scope` so legacy calls share it.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
import math
import numbers
import os
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable, Iterator, Optional, Protocol, Sequence, Union

import cv2
import numpy as np
from PIL import Image, UnidentifiedImageError


class CaptureError(RuntimeError):
    """The client could not be captured or returned an invalid image."""


class DetectionError(RuntimeError):
    """A detection could not be performed (for example, a bad template)."""


ImageLike = Union["Frame", Image.Image, np.ndarray]
Region = tuple[int, int, int, int]


def _as_rgb_array(image: Union[Image.Image, np.ndarray], error_type: type[RuntimeError]) -> np.ndarray:
    if isinstance(image, Image.Image):
        array = np.asarray(image.convert("RGB"), dtype=np.uint8)
    elif isinstance(image, np.ndarray):
        array = image
        if array.dtype != np.uint8:
            raise error_type(f"capture array must use uint8 pixels, got {array.dtype}")
        if array.ndim == 2:
            array = np.repeat(array[:, :, None], 3, axis=2)
        elif array.ndim == 3 and array.shape[2] == 4:
            array = array[:, :, :3]
        elif array.ndim != 3 or array.shape[2] != 3:
            raise error_type(f"capture array must have shape (height, width, 3), got {array.shape}")
    else:
        raise error_type(f"capture provider returned unsupported type {type(image).__name__}")
    if array.shape[0] <= 0 or array.shape[1] <= 0:
        raise error_type("capture image must be non-empty")
    return np.ascontiguousarray(array, dtype=np.uint8)


@dataclass(frozen=True, slots=True, init=False)
class Frame:
    """An immutable, timestamped RGB client image backed by bytes."""

    _rgb: bytes
    width: int
    height: int
    captured_at: float
    source: str

    def __init__(
        self,
        image: Union[Image.Image, np.ndarray],
        captured_at: Optional[float] = None,
        source: str = "capture",
    ) -> None:
        if isinstance(image, Image.Image):
            if image.width <= 0 or image.height <= 0:
                raise CaptureError("capture image must be non-empty")
            rgb = image if image.mode == "RGB" else image.convert("RGB")
            pixels = rgb.tobytes()
            width, height = rgb.size
        else:
            rgb = _as_rgb_array(image, CaptureError)
            pixels = rgb.tobytes()
            height, width = rgb.shape[:2]
        object.__setattr__(self, "_rgb", pixels)
        object.__setattr__(self, "width", int(width))
        object.__setattr__(self, "height", int(height))
        object.__setattr__(self, "captured_at", time.time() if captured_at is None else float(captured_at))
        object.__setattr__(self, "source", str(source))

    @classmethod
    def _from_rgb_bytes(
        cls,
        rgb: bytes,
        width: int,
        height: int,
        *,
        captured_at: float,
        source: str,
    ) -> "Frame":
        """Adopt an immutable RGB snapshot already copied out of capture storage."""
        if type(rgb) is not bytes:
            raise CaptureError("RGB snapshot must be immutable bytes")
        if any(
            isinstance(value, bool) or not isinstance(value, numbers.Integral) or value <= 0
            for value in (width, height)
        ):
            raise CaptureError("RGB snapshot dimensions must be positive integers")
        width, height = int(width), int(height)
        if len(rgb) != width * height * 3:
            raise CaptureError("RGB snapshot byte count does not match its dimensions")
        frame = object.__new__(cls)
        object.__setattr__(frame, "_rgb", rgb)
        object.__setattr__(frame, "width", width)
        object.__setattr__(frame, "height", height)
        object.__setattr__(frame, "captured_at", float(captured_at))
        object.__setattr__(frame, "source", str(source))
        return frame

    @property
    def timestamp(self) -> float:
        return self.captured_at

    @property
    def pixels(self) -> np.ndarray:
        return np.frombuffer(self._rgb, dtype=np.uint8).reshape(self.height, self.width, 3)

    @property
    def image(self) -> Image.Image:
        return Image.frombytes("RGB", (self.width, self.height), self._rgb)

    @property
    def size(self) -> tuple[int, int]:
        return self.width, self.height

    def crop(self, region: Sequence[int]) -> Image.Image:
        left, top, right, bottom = validate_region(region, self.size)
        return Image.fromarray(self.pixels[top:bottom, left:right])


@dataclass(frozen=True, slots=True)
class Match:
    """A real template match; unlike legacy tuples, (0, 0) is not ambiguous."""

    x: int
    y: int
    width: int
    height: int
    score: float

    @property
    def position(self) -> tuple[int, int]:
        return self.x, self.y

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.width // 2, self.y + self.height // 2

    @property
    def box(self) -> tuple[int, int, int, int]:
        return self.x, self.y, self.x + self.width, self.y + self.height

    def translated(self, dx: int, dy: int) -> "Match":
        return Match(self.x + int(dx), self.y + int(dy), self.width, self.height, self.score)


class CaptureProvider(Protocol):
    def capture(self) -> ImageLike:
        ...


_Provider = Union[CaptureProvider, Callable[[], ImageLike]]
_provider: Optional[_Provider] = None
_provider_lock = threading.RLock()
_scope_frame: ContextVar[Optional[Frame]] = ContextVar("perception_frame", default=None)
_template_cache: dict[tuple[str, int, int], np.ndarray] = {}
_template_cache_lock = threading.RLock()


@dataclass(slots=True)
class _CaptureRequest:
    provider: _Provider
    done: threading.Event
    result: Optional[ImageLike] = None
    error: Optional[BaseException] = None


# Native capture calls can block inside an OS API. A single daemon actor gives
# callers a deadline without creating another worker when one is still stuck.
_actor_condition = threading.Condition()
_actor_thread: Optional[threading.Thread] = None
_actor_pending: Optional[_CaptureRequest] = None
_actor_busy = False


def _invoke_provider(provider: _Provider) -> ImageLike:
    method = getattr(provider, "capture", None)
    return method() if callable(method) else provider()  # type: ignore[operator]


def _capture_actor_loop() -> None:
    global _actor_pending, _actor_busy
    while True:
        with _actor_condition:
            while _actor_pending is None:
                _actor_condition.wait()
            request = _actor_pending
            _actor_pending = None
            _actor_busy = True
        try:
            request.result = _invoke_provider(request.provider)
        except BaseException as exc:
            request.error = exc
        finally:
            with _actor_condition:
                _actor_busy = False
                request.done.set()
                _actor_condition.notify_all()


def _ensure_capture_actor() -> None:
    global _actor_thread
    with _actor_condition:
        if _actor_thread is None or not _actor_thread.is_alive():
            _actor_thread = threading.Thread(target=_capture_actor_loop, name="capture-actor", daemon=True)
            _actor_thread.start()


def _cooperative_checkpoint() -> None:
    """Honor managed-worker cancellation without coupling imports at startup."""

    try:
        from core.runtime import checkpoint
    except ImportError:
        return
    checkpoint()


def _capture_with_deadline(provider: _Provider, timeout: float) -> ImageLike:
    global _actor_pending
    _ensure_capture_actor()
    request = _CaptureRequest(provider=provider, done=threading.Event())
    deadline = time.monotonic() + timeout
    with _actor_condition:
        while _actor_busy or _actor_pending is not None:
            _cooperative_checkpoint()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CaptureError(f"capture timed out after {timeout:g} seconds")
            _actor_condition.wait(min(remaining, 0.05))
        _actor_pending = request
        _actor_condition.notify()
    while not request.done.is_set():
        _cooperative_checkpoint()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CaptureError(f"capture timed out after {timeout:g} seconds")
        request.done.wait(min(remaining, 0.05))
    _cooperative_checkpoint()
    if request.error is not None:
        if isinstance(request.error, CaptureError):
            raise request.error
        raise CaptureError(f"capture provider failed: {request.error}") from request.error
    if request.result is None:
        raise CaptureError("capture provider returned no image")
    return request.result


def as_frame(value: ImageLike, *, source: str = "provided") -> Frame:
    return value if isinstance(value, Frame) else Frame(value, source=source)


def set_capture_provider(provider: Optional[_Provider]) -> None:
    global _provider
    if provider is not None and not callable(provider) and not callable(getattr(provider, "capture", None)):
        raise TypeError("capture provider must be callable or expose capture()")
    with _provider_lock:
        _provider = provider


def reset_capture_provider() -> None:
    set_capture_provider(None)


def get_capture_provider() -> _Provider:
    with _provider_lock:
        return _provider if _provider is not None else _default_capture


def capture_frame(
    provider: Optional[_Provider] = None,
    *,
    max_attempts: int = 2,
    timeout: float = 2.0,
) -> Frame:
    """Capture with bounded retries and a serialized native-call deadline."""

    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1 or max_attempts > 5:
        raise ValueError("max_attempts must be an integer from 1 through 5")
    if isinstance(timeout, bool) or not isinstance(timeout, numbers.Real) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a finite positive number")
    selected = provider if provider is not None else get_capture_provider()
    last_error: Optional[BaseException] = None
    deadline = time.monotonic() + float(timeout)
    for _ in range(max_attempts):
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            return as_frame(_capture_with_deadline(selected, remaining))
        except CaptureError as exc:
            last_error = exc
            if "timed out" in str(exc):
                break
    raise CaptureError(f"client capture failed after bounded attempt(s): {last_error}") from last_error


def current_frame(frame: Optional[ImageLike] = None) -> Frame:
    if frame is not None:
        return as_frame(frame)
    scoped = _scope_frame.get()
    return scoped if scoped is not None else capture_frame()


@contextmanager
def frame_scope(frame: Optional[ImageLike] = None, *, provider: Optional[_Provider] = None) -> Iterator[Frame]:
    """Reuse one snapshot for every detector called inside the context."""

    inherited = _scope_frame.get()
    selected = as_frame(frame) if frame is not None else (inherited or capture_frame(provider))
    token = _scope_frame.set(selected)
    try:
        yield selected
    finally:
        _scope_frame.reset(token)


def validate_region(region: Sequence[int], image_size: tuple[int, int]) -> Region:
    try:
        valid_length = not isinstance(region, (str, bytes)) and len(region) == 4
    except TypeError:
        valid_length = False
    if not valid_length:
        raise DetectionError("region must contain (left, top, right, bottom)")
    if any(isinstance(value, bool) or not isinstance(value, numbers.Integral) for value in region):
        raise DetectionError("region coordinates must be integers")
    left, top, right, bottom = (int(value) for value in region)
    width, height = image_size
    if left < 0 or top < 0 or right > width or bottom > height:
        raise DetectionError(f"region {(left, top, right, bottom)} is outside image bounds {(width, height)}")
    if right <= left or bottom <= top:
        raise DetectionError(f"region must be non-empty, got {(left, top, right, bottom)}")
    return left, top, right, bottom


def crop_array(frame: Frame, region: Optional[Sequence[int]] = None) -> np.ndarray:
    pixels = frame.pixels
    if region is None:
        return pixels
    left, top, right, bottom = validate_region(region, frame.size)
    return pixels[top:bottom, left:right]


def _project_path(path: Union[str, os.PathLike[str]]) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    try:
        from core.paths import project_path

        return Path(project_path(str(candidate))).resolve()
    except (ImportError, AttributeError, TypeError):
        return (Path(__file__).resolve().parents[1] / candidate).resolve()
    except ValueError as exc:
        raise DetectionError(f"invalid template path {path!r}: {exc}") from exc


def load_template(path: Union[str, os.PathLike[str]]) -> np.ndarray:
    """Load an RGB template, cached by resolved path, mtime, and file size."""

    resolved = _project_path(path)
    try:
        stat = resolved.stat()
    except OSError as exc:
        raise DetectionError(f"template does not exist or is unreadable: {resolved}") from exc
    if not resolved.is_file():
        raise DetectionError(f"template is not a file: {resolved}")
    key = (str(resolved), stat.st_mtime_ns, stat.st_size)
    with _template_cache_lock:
        cached = _template_cache.get(key)
        if cached is not None:
            return cached
    try:
        with Image.open(resolved) as image:
            template = np.ascontiguousarray(np.asarray(image.convert("RGB"), dtype=np.uint8))
    except (OSError, ValueError, UnidentifiedImageError) as exc:
        raise DetectionError(f"template is unreadable: {resolved}") from exc
    if template.shape[0] <= 0 or template.shape[1] <= 0:
        raise DetectionError(f"template is empty: {resolved}")
    template.setflags(write=False)
    with _template_cache_lock:
        for stale in [entry for entry in _template_cache if entry[0] == str(resolved) and entry != key]:
            _template_cache.pop(stale, None)
        _template_cache[key] = template
    return template


def clear_template_cache() -> None:
    with _template_cache_lock:
        _template_cache.clear()


def _match_map(haystack: np.ndarray, template: np.ndarray) -> np.ndarray:
    if template.shape[0] > haystack.shape[0] or template.shape[1] > haystack.shape[1]:
        raise DetectionError(
            f"template size {(template.shape[1], template.shape[0])} exceeds search image "
            f"size {(haystack.shape[1], haystack.shape[0])}"
        )
    hay_gray = cv2.cvtColor(np.ascontiguousarray(haystack), cv2.COLOR_RGB2GRAY)
    template_gray = cv2.cvtColor(np.ascontiguousarray(template), cv2.COLOR_RGB2GRAY)
    if float(template_gray.std()) < 1e-8:
        # OpenCV's normalized SQDIFF is undefined when both constant images are
        # zero. Normalize the unscaled squared error by its known maximum.
        difference = cv2.matchTemplate(hay_gray, template_gray, cv2.TM_SQDIFF)
        maximum_error = float(template_gray.size * (255 ** 2))
        return np.clip(1.0 - (difference / maximum_error), 0.0, 1.0)
    return cv2.matchTemplate(hay_gray, template_gray, cv2.TM_CCOEFF_NORMED)


def _validate_probability(value: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, numbers.Real) or not math.isfinite(value):
        raise DetectionError(f"{label} must be a finite number between 0 and 1")
    result = float(value)
    if not 0.0 <= result <= 1.0:
        raise DetectionError(f"{label} must be between 0 and 1")
    return result


def _scaled_template(template_path, scale: float) -> np.ndarray:
    if (isinstance(scale, bool) or not isinstance(scale, numbers.Real)
            or not math.isfinite(scale) or not .5 <= scale <= 3):
        raise DetectionError("template scale must be between 0.5 and 3")
    template = load_template(template_path)
    if scale != 1.0:
        template = cv2.resize(template, None, fx=scale, fy=scale, interpolation=cv2.INTER_LINEAR)
    return template


def locate_match(
    template_path: Union[str, os.PathLike[str]],
    *,
    frame: Optional[ImageLike] = None,
    region: Optional[Sequence[int]] = None,
    precision: float = 0.8,
    scale: float = 1.0,
) -> Optional[Match]:
    precision = _validate_probability(precision, "precision")
    haystack = crop_array(current_frame(frame), region)
    template = _scaled_template(template_path, scale)
    scores = _match_map(haystack, template)
    _, score, _, position = cv2.minMaxLoc(scores)
    if score < precision:
        return None
    return Match(position[0], position[1], template.shape[1], template.shape[0], float(score))


def _intersection_over_union(a: Match, b: Match) -> float:
    left, top = max(a.x, b.x), max(a.y, b.y)
    right = min(a.x + a.width, b.x + b.width)
    bottom = min(a.y + a.height, b.y + b.height)
    intersection = max(0, right - left) * max(0, bottom - top)
    if intersection == 0:
        return 0.0
    union = a.width * a.height + b.width * b.height - intersection
    return intersection / union


def locate_matches(
    template_path: Union[str, os.PathLike[str]],
    *,
    frame: Optional[ImageLike] = None,
    region: Optional[Sequence[int]] = None,
    precision: float = 0.8,
    nms_iou: float = 0.3,
    scale: float = 1.0,
) -> list[Match]:
    """Return distinct objects after score-ordered non-maximum suppression."""

    precision = _validate_probability(precision, "precision")
    nms_iou = _validate_probability(nms_iou, "nms_iou")
    haystack = crop_array(current_frame(frame), region)
    template = _scaled_template(template_path, scale)
    scores = _match_map(haystack, template)
    threshold = scores >= precision
    if not np.any(threshold):
        return []
    # Keep one candidate per local-maximum plateau. This preserves separate
    # nearby objects even if their lower-scoring response areas touch.
    local_maximum = cv2.dilate(scores, np.ones((3, 3), dtype=np.uint8))
    peaks = np.asarray(threshold & (scores >= local_maximum - 1e-7), dtype=np.uint8)
    component_count, labels, stats, _ = cv2.connectedComponentsWithStats(peaks, connectivity=8)
    if component_count > 1_001:
        raise DetectionError("template response is ambiguous: too many candidate regions")
    candidates: list[Match] = []
    max_plateau = max(256, int(template.shape[0] * template.shape[1] * 8))
    for label in range(1, component_count):
        if label % 64 == 0:
            _cooperative_checkpoint()
        left, top, width, height, area = (int(value) for value in stats[label])
        if area > max_plateau:
            raise DetectionError(
                f"template response is ambiguous: {area} adjacent positions exceed precision {precision}"
            )
        component_scores = scores[top : top + height, left : left + width]
        component_mask = labels[top : top + height, left : left + width] == label
        masked = np.where(component_mask, component_scores, -np.inf)
        local_y, local_x = np.unravel_index(int(np.argmax(masked)), masked.shape)
        x, y = left + int(local_x), top + int(local_y)
        candidates.append(Match(x, y, template.shape[1], template.shape[0], float(scores[y, x])))
    candidates.sort(key=lambda match: (-match.score, match.y, match.x))
    selected: list[Match] = []
    for index, candidate in enumerate(candidates):
        if index % 64 == 0:
            _cooperative_checkpoint()
        if all(_intersection_over_union(candidate, accepted) <= nms_iou for accepted in selected):
            selected.append(candidate)
    return sorted(selected, key=lambda match: (match.y, match.x))


def _load_capture_config() -> dict[str, Any]:
    try:
        from conf.conf_manager import ConfManager

        data = ConfManager.get("conf.json")
    except Exception as exc:
        raise CaptureError(f"could not load capture configuration: {exc}") from exc
    if not isinstance(data, dict):
        raise CaptureError("capture configuration must be an object")
    return data


def _default_capture() -> Image.Image:
    config = _load_capture_config()  # Fresh on every capture.
    platform = str(config.get("platform", "")).lower()
    if platform == "windows":
        from tibia_client.window_target import WindowTarget
        from tibia_client.direct_capture import capture_direct_host_client, capture_direct_target

        target_data = config.get("window_target")
        if isinstance(target_data, dict) and target_data.get("kind") == "sandbox":
            # The visible-region transport only needs geometry. Old profiles
            # may contain focus samples; do not decode unused templates per frame.
            target = WindowTarget("sandbox", target_data.get("host_size"),
                                  target_data.get("viewport"))
        else:
            target = WindowTarget.from_config(config)
        hwnd = config.get("capture_hwnd", config.get("hwnd"))
        if not hwnd:
            raise CaptureError("capture configuration has no capture_hwnd or hwnd")
        configured_size = config.get("capture_size")
        if (
            not isinstance(configured_size, (list, tuple))
            or len(configured_size) != 2
            or any(
                isinstance(value, bool)
                or not isinstance(value, numbers.Integral)
                or value <= 0
                for value in configured_size
            )
        ):
            raise CaptureError("capture_size is missing or invalid; recalibrate the client profile")
        expected_size = int(configured_size[0]), int(configured_size[1])
        if target.kind == "sandbox":
            if hwnd != config.get("input_hwnd"):
                raise CaptureError("Sandbox capture and input must use the same host window")
            try:
                image = capture_direct_target(int(hwnd), target)
            except RuntimeError as exc:
                raise CaptureError(str(exc)) from exc
        elif hwnd == config.get("input_hwnd", hwnd):
            image = capture_direct_host_client(int(hwnd))
        else:
            # A separate OBS/projector selection keeps its existing transport.
            image = _capture_windows(int(hwnd))
        if image.size != expected_size:
            failure = CaptureError(
                f"client size changed from calibrated {expected_size[0]}x{expected_size[1]} "
                f"to {image.width}x{image.height}; recalibrate the client profile"
            )
            from tibia_client.direct_capture import _diagnose_failure

            _diagnose_failure(failure, int(hwnd))
            raise failure
        return image
    if platform == "linux":
        return _capture_linux()
    raise CaptureError(f"unsupported capture platform: {platform!r}")


def _capture_windows(hwnd: int) -> Image.Image:
    """Capture a Windows client without foregrounding it; imports stay lazy."""

    try:
        from ctypes import windll, wintypes
        import win32gui
        import win32ui
    except ImportError as exc:
        raise CaptureError("Windows capture requires pywin32") from exc

    hwnd_dc = mfc_dc = save_dc = bitmap = None
    try:
        left, top, right, bottom = win32gui.GetClientRect(hwnd)
        width, height = right - left, bottom - top
        if width <= 0 or height <= 0:
            raise CaptureError(f"window {hwnd} has an empty client area")
        hwnd_dc = win32gui.GetWindowDC(hwnd)
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
        save_dc.SelectObject(bitmap)
        print_window = windll.user32.PrintWindow
        print_window.argtypes = (wintypes.HWND, wintypes.HDC, wintypes.UINT)
        print_window.restype = wintypes.BOOL
        result = print_window(hwnd, save_dc.GetSafeHdc(), 1)
        if result != 1:
            raise CaptureError(f"PrintWindow failed for hwnd {hwnd}")
        info = bitmap.GetInfo()
        bits = bitmap.GetBitmapBits(True)
        return Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]), bits, "raw", "BGRX", 0, 1).copy()
    except CaptureError as exc:
        exc.capture_retryable = True
        raise
    except Exception as exc:
        failure = CaptureError(f"Windows capture failed for hwnd {hwnd}: {exc}")
        failure.capture_retryable = True
        raise failure from exc
    finally:
        # Delete the compatible DC first so its bitmap is no longer selected.
        # DeleteObject fails for an object that remains selected into a DC.
        if save_dc is not None:
            try:
                save_dc.DeleteDC()
            except Exception:
                pass
        if bitmap is not None:
            try:
                win32gui.DeleteObject(bitmap.GetHandle())
            except Exception:
                pass
        # mfc_dc wraps the borrowed GetWindowDC handle. Detach the wrapper when
        # supported; the handle itself must be returned with ReleaseDC, never
        # destroyed with DeleteDC.
        if mfc_dc is not None:
            detach = getattr(mfc_dc, "Detach", None)
            if callable(detach):
                try:
                    detach()
                except Exception:
                    pass
        if hwnd_dc is not None:
            try:
                win32gui.ReleaseDC(hwnd, hwnd_dc)
            except Exception:
                pass


def _capture_linux() -> Image.Image:
    path: Optional[str] = None
    try:
        descriptor, path = tempfile.mkstemp(suffix=".png", prefix="tibiaauto-capture-")
        os.close(descriptor)
        completed = subprocess.run(["scrot", "-u", path], capture_output=True, text=True, timeout=5, check=False)
        if completed.returncode != 0:
            raise CaptureError(f"scrot failed: {completed.stderr.strip() or completed.returncode}")
        with Image.open(path) as image:
            return image.convert("RGB").copy()
    except FileNotFoundError as exc:
        raise CaptureError("Linux capture requires scrot") from exc
    except subprocess.TimeoutExpired as exc:
        raise CaptureError("Linux capture timed out") from exc
    except (OSError, UnidentifiedImageError) as exc:
        raise CaptureError(f"Linux capture produced no readable image: {exc}") from exc
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass
