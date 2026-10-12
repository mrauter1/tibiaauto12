"""Compatibility facade for reliable capture and detection primitives."""

from __future__ import annotations

from typing import Optional

from PIL import Image

from core.perception import (
    CaptureError,
    DetectionError,
    Frame,
    ImageLike,
    Match,
    capture_frame,
    current_frame,
    frame_scope,
    load_template,
    locate_match,
    locate_matches,
    reset_capture_provider,
    set_capture_provider,
)


class Hooker:
    """Legacy capture object backed by the bounded provider interface."""

    def __init__(self, provider=None, max_attempts: int = 2):
        self.frame = capture_frame(provider, max_attempts=max_attempts)
        self.TakedImage = self.frame.image
        self.result = 1

    def HookWindow(self) -> Image.Image:
        return self.TakedImage.copy()


def TakeImage(Region=None, frame: Optional[ImageLike] = None) -> Image.Image:
    snapshot = current_frame(frame)
    return snapshot.image if Region is None else snapshot.crop(Region)


def LocateMatch(image, Region=None, Precision=0.8, frame: Optional[ImageLike] = None) -> Optional[Match]:
    """Return an explicit optional match; (0, 0) remains a valid position."""

    return locate_match(image, frame=frame, region=Region, precision=Precision)


def LocateMatches(
    image,
    Region=None,
    Precision=0.8,
    frame: Optional[ImageLike] = None,
    NmsIou=0.3,
) -> list[Match]:
    return locate_matches(image, frame=frame, region=Region, precision=Precision, nms_iou=NmsIou)


def LocateImage(image, Region=None, Precision=0.8, frame: Optional[ImageLike] = None):
    match = LocateMatch(image, Region=Region, Precision=Precision, frame=frame)
    return match.position if match is not None else (0, 0)


def LocateCenterImage(image, Region=None, Precision=0.8, frame: Optional[ImageLike] = None):
    match = LocateMatch(image, Region=Region, Precision=Precision, frame=frame)
    return match.center if match is not None else (0, 0)


def LocateAllImages(image, Region=None, Precision=0.8, frame: Optional[ImageLike] = None):
    return len(LocateMatches(image, Region=Region, Precision=Precision, frame=frame))


def LocateBoolRGBImage(image, Region=None, Precision=0.9, frame: Optional[ImageLike] = None):
    return LocateMatch(image, Region=Region, Precision=Precision, frame=frame) is not None


def PixelMatchesColor(
    X,
    Y,
    expectedRGBColor,
    frame: Optional[ImageLike] = None,
    tolerance: int = 0,
):
    snapshot = current_frame(frame)
    if not isinstance(tolerance, int) or tolerance < 0:
        raise DetectionError("color tolerance must be a non-negative integer")
    actual = snapshot.crop((int(X), int(Y), int(X) + 1, int(Y) + 1)).getpixel((0, 0))
    try:
        expected = tuple(int(channel) for channel in expectedRGBColor)
    except (TypeError, ValueError) as exc:
        raise DetectionError("expectedRGBColor must contain three integer channels") from exc
    if len(expected) != 3 or any(channel < 0 or channel > 255 for channel in expected):
        raise DetectionError("expectedRGBColor must contain three channels from 0 through 255")
    return all(abs(actual[index] - expected[index]) <= tolerance for index in range(3))


def SaveImage(Name, Region=None, frame: Optional[ImageLike] = None):
    image = TakeImage(Region, frame=frame)
    try:
        image.save(Name)
    except OSError as exc:
        raise CaptureError(f"could not save capture to {Name}: {exc}") from exc


def GetImageSize(needleImage):
    template = load_template(needleImage)
    return int(template.shape[1]), int(template.shape[0])


def IsFocused():
    """Return the foreground hwnd. Windows dependencies are imported lazily."""

    try:
        import win32gui
    except ImportError as exc:
        raise CaptureError("IsFocused is only available with pywin32") from exc
    return int(win32gui.GetForegroundWindow())


__all__ = [
    "CaptureError",
    "DetectionError",
    "Frame",
    "Match",
    "Hooker",
    "capture_frame",
    "frame_scope",
    "set_capture_provider",
    "reset_capture_provider",
    "TakeImage",
    "LocateMatch",
    "LocateMatches",
    "LocateImage",
    "LocateCenterImage",
    "LocateAllImages",
    "LocateBoolRGBImage",
    "PixelMatchesColor",
    "SaveImage",
    "GetImageSize",
    "IsFocused",
]
