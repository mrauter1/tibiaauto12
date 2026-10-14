"""Bounded, template-first reading of Skills-panel integer values.

This module is deliberately independent of :mod:`tibia_client.visual`.  The
legacy visual detector continues to use its exact-scale profile behavior while
the adaptive resolver can use this reader on an already bounded numeric ROI.

Scores exposed here are normalized image-match scores, not probabilities.
Tesseract is optional and is never started unless a ``TesseractOCR`` (or a
compatible injected object) is explicitly supplied to :class:`NumberReader`.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from io import BytesIO
import csv
import math
from pathlib import Path
import re
import shutil
import subprocess
from typing import Protocol, Sequence

import cv2
import numpy as np
from PIL import Image, UnidentifiedImageError

from tibia_client.models import Quality, Reading


MAX_NUMBER_PIXELS = 250_000
MAX_OCR_PIXELS = 4_000_000
MAX_OCR_SIDE = 4096
MAX_OCR_TIMEOUT = 10.0
MAX_DIGITS = 9
_LEGACY_AMBIGUITY_MARGIN = 0.04
_VALID_PSM = frozenset({3, 4, 6, 7, 8, 11, 12, 13})
_GROUPED_INTEGER = re.compile(r"(?:0|[1-9][0-9]{0,2}(?:,[0-9]{3})+|[1-9][0-9]*)")


@dataclass(frozen=True, slots=True)
class OCRBox:
    """One Tesseract word box.

    ``box`` uses half-open ``(left, top, right, bottom)`` ROI coordinates.
    ``score`` is Tesseract's 0..100 confidence divided by 100.  It is an engine
    match score and must not be interpreted as a probability.
    """

    text: str
    box: tuple[int, int, int, int]
    score: float


class _OCRBackend(Protocol):
    def read_text_boxes(
        self,
        image: np.ndarray,
        *,
        psm: int = 7,
        timeout: float = 3.0,
    ) -> Sequence[OCRBox]: ...


class TesseractOCR:
    """Small, local-only Tesseract adapter for a bounded image.

    Discovery uses ``shutil.which`` and absence is represented by
    ``available == False``.  Calls use PNG bytes over stdin, ``shell=False``, a
    hard image-size budget, and a capped subprocess timeout.
    """

    def __init__(self, executable: str | Path | None = None) -> None:
        if executable is None:
            found = shutil.which("tesseract")
        else:
            value = str(executable)
            found = shutil.which(value)
            if found is None:
                path = Path(value).expanduser()
                found = str(path.resolve()) if path.is_file() else None
        self._executable = found

    @property
    def available(self) -> bool:
        return self._executable is not None

    @property
    def executable(self) -> str | None:
        return self._executable

    def read_text_boxes(
        self,
        image: np.ndarray,
        *,
        psm: int = 7,
        timeout: float = 3.0,
    ) -> tuple[OCRBox, ...]:
        return self._read_boxes(image, psm=psm, timeout=timeout, numeric_only=False)

    def read_number_boxes(
        self,
        image: np.ndarray,
        *,
        psm: int = 7,
        timeout: float = 3.0,
    ) -> tuple[OCRBox, ...]:
        """Read a single numeric line with Tesseract's character set bounded."""
        return self._read_boxes(image, psm=psm, timeout=timeout, numeric_only=True)

    def _read_boxes(
        self,
        image: np.ndarray,
        *,
        psm: int,
        timeout: float,
        numeric_only: bool,
    ) -> tuple[OCRBox, ...]:
        pixels = _rgb_image(image, pixel_limit=MAX_OCR_PIXELS, side_limit=MAX_OCR_SIDE)
        if type(psm) is not int or psm not in _VALID_PSM:
            raise ValueError(f"psm must be one of {sorted(_VALID_PSM)}")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("OCR timeout must be a positive finite number")
        if self._executable is None:
            return ()

        stream = BytesIO()
        Image.fromarray(pixels).save(stream, format="PNG", optimize=False)
        command = [
            self._executable,
            "stdin",
            "stdout",
            "--psm",
            str(psm),
        ]
        if numeric_only:
            command.extend(("-c", "tessedit_char_whitelist=0123456789,"))
        command.append("tsv")
        try:
            completed = subprocess.run(
                command,
                input=stream.getvalue(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=min(float(timeout), MAX_OCR_TIMEOUT),
                check=False,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ()
        if completed.returncode != 0 or len(completed.stdout) > 8 * 1024 * 1024:
            return ()
        return _parse_tesseract_tsv(completed.stdout, pixels.shape[1], pixels.shape[0])


@dataclass(frozen=True, slots=True)
class _Glyph:
    normalized: np.ndarray
    ratio: float
    height: int
    holes: int


@dataclass(frozen=True, slots=True)
class _Rank:
    token: str
    score: float


@dataclass(frozen=True, slots=True)
class _Decoded:
    text: str
    score: float
    average_score: float
    ranks: tuple[tuple[_Rank, ...], ...]
    alternatives: tuple[dict[str, object], ...]


class NumberReader:
    """Read a non-negative integer from a bounded RGB numeric ROI.

    The bundled Skills profile supplies the digit and comma shapes.  Passing an
    OCR backend makes fallback available.  Each ``read`` must additionally set
    ``use_ocr=True`` so latency-sensitive callers cannot start a subprocess by
    merely sharing a configured reader.  Template evidence remains mandatory
    before an OCR result can become known.
    """

    supports_font_family = True

    def __init__(
        self,
        *,
        profile_dir: str | Path | None = None,
        ocr: _OCRBackend | None = None,
        template_score: float = 0.70,
        ambiguity_margin: float = 0.05,
    ) -> None:
        if profile_dir is None:
            profile_dir = Path(__file__).resolve().parents[1] / "profiles" / "paulistinha-skills-v1"
        root = Path(profile_dir).expanduser().resolve()
        if not 0.5 <= template_score <= 1.0:
            raise ValueError("template_score must be from 0.5 through 1.0")
        if not 0 < ambiguity_margin <= 1.0:
            raise ValueError("ambiguity_margin must be greater than zero and at most 1.0")
        self._template_score = float(template_score)
        self._ambiguity_margin = float(ambiguity_margin)
        self._ocr = ocr
        digit_paths = tuple(
            (
                root / "digits" / f"{number}.png",
                *sorted((root / "digits").glob(f"{number}-*.png")),
            )
            for number in range(10)
        )
        self._digit_images = tuple(
            tuple(_load_rgb(path) for path in paths if "yonabra" not in path.stem)
            for paths in digit_paths
        )
        self._alternate_digits = tuple(
            tuple(
                _glyph(_ink(_load_rgb(path))) for path in paths if "yonabra" in path.stem
            )
            for paths in digit_paths
        )
        self._alternate_digit_images = tuple(
            tuple(_load_rgb(path) for path in paths if "yonabra" in path.stem)
            for paths in digit_paths
        )
        self._digits = tuple(
            tuple(_glyph(_ink(image)) for image in variants) for variants in self._digit_images
        )
        self._comma = _glyph(_ink(_load_rgb(root / "comma.png")))
        self._small_scale_commas = tuple(
            _glyph(_ink(_load_rgb(path))) for path in sorted(root.glob("comma-*.png"))
        )
        self._alternate_comma_images = tuple(
            _load_rgb(path) for path in sorted(root.glob("comma-yonabra*.png"))
        )

    def read(
        self,
        image: np.ndarray,
        *,
        scale: float = 1.0,
        max_digits: int = 9,
        allow_grouping: bool = True,
        use_ocr: bool = False,
        family: str | None = None,
    ) -> Reading:
        pixels = _rgb_image(image, pixel_limit=MAX_NUMBER_PIXELS)
        if (
            isinstance(scale, bool)
            or not isinstance(scale, (int, float))
            or not math.isfinite(scale)
            or not 0.5 <= scale <= 4.0
        ):
            raise ValueError("scale must be a finite number from 0.5 through 4.0")
        if type(max_digits) is not int or not 1 <= max_digits <= MAX_DIGITS:
            raise ValueError(f"max_digits must be an integer from 1 through {MAX_DIGITS}")
        if type(allow_grouping) is not bool:
            raise ValueError("allow_grouping must be boolean")
        if type(use_ocr) is not bool:
            raise ValueError("use_ocr must be boolean")
        if family not in (None, "alternate"):
            raise ValueError("family must be None or 'alternate'")

        decoded: _Decoded | None = None
        reason = "number contains no supported digit foreground"
        diagnostics: dict[str, object] = {
            "reader": "templates",
            "text": None,
            "score": 0.0,
            "alternatives": (),
            "scale": float(scale),
        }
        try:
            decoded = self._decode(pixels, float(scale), max_digits, allow_grouping, family)
            diagnostics.update(
                text=decoded.text,
                score=decoded.score,
                alternatives=decoded.alternatives,
            )
            reason = self._rejection_reason(decoded, max_digits, allow_grouping)
            if reason == "number contains a weakly matched thousands separator":
                # A resampled comma can straddle the fixed foreground cutoff
                # while every digit remains strong. Require the same grouped
                # text to pass every existing score and ambiguity check after a
                # small darker perturbation; the weak original alone is never
                # accepted.
                darker = np.clip(pixels.astype(np.int16) - 5, 0, 255).astype(np.uint8)
                try:
                    corroborating = self._decode(
                        darker, float(scale), max_digits, allow_grouping, family
                    )
                    corroborating_reason = self._rejection_reason(
                        corroborating, max_digits, allow_grouping
                    )
                except ValueError:
                    corroborating = None
                    corroborating_reason = "unstable"
                if (
                    corroborating is not None
                    and corroborating.text == decoded.text
                    and not corroborating_reason
                ):
                    reason = ""
                    diagnostics["threshold_corroboration"] = {
                        "delta": -5,
                        "text": corroborating.text,
                        "score": corroborating.score,
                    }
            if reason and not allow_grouping:
                touching = self._decode_touching_digits(
                    pixels, float(scale), max_digits, family
                )
                if touching is not None:
                    decoded = touching
                    diagnostics.update(
                        text=decoded.text,
                        score=decoded.score,
                        alternatives=decoded.alternatives,
                        segmentation="touching_digits",
                    )
                    reason = self._rejection_reason(decoded, max_digits, allow_grouping)
            if reason == "number contains an ambiguous digit":
                # Label raster dimensions can put the panel-scale estimate
                # exactly between two 0.05 template-bank steps. Accept neither
                # side independently: both neighboring scales must preserve the
                # same text and pass every ordinary score/ambiguity check.
                neighboring = []
                for candidate_scale in (float(scale) - 0.025, float(scale) + 0.025):
                    if candidate_scale < 0.5 or candidate_scale > 4.0:
                        break
                    try:
                        candidate = self._decode(
                            pixels,
                            candidate_scale,
                            max_digits,
                            allow_grouping,
                            family,
                        )
                        candidate_reason = self._rejection_reason(
                            candidate, max_digits, allow_grouping
                        )
                    except ValueError:
                        candidate = None
                        candidate_reason = "unstable"
                    neighboring.append((candidate_scale, candidate, candidate_reason))
                if (
                    len(neighboring) == 2
                    and all(
                        candidate is not None
                        and candidate.text == decoded.text
                        and not candidate_reason
                        for _, candidate, candidate_reason in neighboring
                    )
                ):
                    reason = ""
                    diagnostics["scale_corroboration"] = {
                        "scales": tuple(item[0] for item in neighboring),
                        "text": decoded.text,
                    }

            if reason == "number contains an ambiguous digit":
                digit_margins = [
                    items[0].score - items[1].score
                    for items in decoded.ranks
                    if items[0].token != "," and len(items) > 1
                ]
                # The stricter default closes resampling-induced 0/9 and 2/7
                # false accepts.  For only the newly guarded (.04, .05] band,
                # retain a value when a small foreground-threshold perturbation
                # preserves the text and turns every glyph into an unambiguous
                # match.  Unstable wrong hypotheses stay unknown.
                if digit_margins and min(digit_margins) > _LEGACY_AMBIGUITY_MARGIN:
                    brighter = np.clip(pixels.astype(np.int16) + 5, 0, 255).astype(
                        np.uint8
                    )
                    try:
                        corroborating = self._decode(
                            brighter, float(scale), max_digits, allow_grouping, family
                        )
                        corroborating_reason = self._rejection_reason(
                            corroborating, max_digits, allow_grouping
                        )
                    except ValueError:
                        corroborating = None
                        corroborating_reason = "unstable"
                    if (
                        corroborating is not None
                        and corroborating.text == decoded.text
                        and not corroborating_reason
                    ):
                        reason = ""
                        diagnostics["threshold_corroboration"] = {
                            "delta": 5,
                            "text": corroborating.text,
                            "score": corroborating.score,
                        }
            if not reason and float(scale) < 0.70:
                # At small resampled scales a threshold-edge pixel can erase a
                # hole or join two strokes, turning (for example) a 9 into a
                # high-scoring 0.  A five-level luminance perturbation retains
                # the supported font on the corpus but exposes that topology
                # loss.  A value is known only when the decoded syntax and all
                # per-glyph evidence remain acceptable on both masks.  Apply
                # this only below 0.70: at larger scales the ROI can contain
                # legitimate panel chrome near the foreground threshold.
                brighter = np.clip(pixels.astype(np.int16) + 5, 0, 255).astype(np.uint8)
                try:
                    corroborating = self._decode(
                        brighter, float(scale), max_digits, allow_grouping, family
                    )
                    corroborating_reason = self._rejection_reason(
                        corroborating, max_digits, allow_grouping
                    )
                except ValueError:
                    corroborating = None
                    corroborating_reason = "unstable"
                if (
                    corroborating is None
                    or corroborating.text != decoded.text
                    or corroborating_reason
                ):
                    reason = "number shape is unstable near the foreground threshold"
            if not reason:
                return Reading(
                    _integer_value(decoded.text),
                    Quality.KNOWN,
                    diagnostics=diagnostics,
                )
        except ValueError as exc:
            reason = str(exc)

        if use_ocr and self._ocr is not None:
            if reason == "number contains an ambiguous digit":
                fallback = self._ocr_fallback(
                    pixels,
                    decoded=decoded,
                    max_digits=max_digits,
                    allow_grouping=allow_grouping,
                    template_reason=reason,
                )
                if fallback is not None:
                    return fallback
                diagnostics["ocr"] = "no OCR candidate had sufficient matching template evidence"
            else:
                diagnostics["ocr"] = "OCR fallback requires a strong but ambiguous template decode"
        return Reading(None, Quality.UNKNOWN, reason, diagnostics=diagnostics)

    def _decode(
        self,
        image: np.ndarray,
        scale: float,
        max_digits: int,
        allow_grouping: bool,
        family: str | None = None,
    ) -> _Decoded:
        ink = _ink(image)
        if not bool(np.any(ink)):
            raise ValueError("number contains no supported digit foreground")
        if (
            bool(np.any(ink[0]))
            or bool(np.any(ink[-1]))
            or bool(np.any(ink[:, 0]))
            or bool(np.any(ink[:, -1]))
        ):
            raise ValueError("number foreground touches the ROI edge and may be clipped")

        occupied = np.any(ink, axis=0)
        starts = np.flatnonzero(occupied & np.r_[True, ~occupied[:-1]])
        ends = np.flatnonzero(occupied & np.r_[~occupied[1:], True]) + 1
        if len(starts) == 0 or len(starts) > max_digits * 2 + 1:
            raise ValueError("number has an unsupported number of glyph segments")
        gaps = starts[1:] - ends[:-1]
        maximum_gap = max(3, math.ceil(5.0 * scale))
        if len(gaps) and int(np.max(gaps)) > maximum_gap:
            raise ValueError("number contains abnormally separated glyph runs")
        glyphs = tuple(_glyph(ink[:, start:end]) for start, end in zip(starts, ends))
        tall_height = max(item.height for item in glyphs)
        expected_height = 9.5 * scale
        if tall_height < max(4.0, expected_height * 0.62) or tall_height > expected_height * 1.55:
            raise ValueError("number glyph height is inconsistent with the estimated scale")

        scaled_digits = self._scaled_digit_references(scale, family)
        ranks: list[tuple[_Rank, ...]] = []
        for candidate in glyphs:
            is_short = candidate.height <= tall_height * 0.68
            if is_short:
                commas = self._comma_references(scale, family)
                comma_score = max(_glyph_score(candidate, comma) for comma in commas)
                ranks.append((_Rank(",", comma_score),))
                continue
            ranked: list[_Rank] = []
            for number, variants in enumerate(scaled_digits):
                compatible = [item for item in variants if item.holes == candidate.holes]
                if compatible:
                    ranked.append(
                        _Rank(
                            str(number),
                            max(_glyph_score(candidate, item) for item in compatible),
                        )
                    )
            if not ranked:
                raise ValueError("number contains a glyph with unsupported topology")
            ranked.sort(key=lambda item: item.score, reverse=True)
            ranks.append(tuple(ranked[:3]))

        text = "".join(items[0].token for items in ranks)
        token_scores = [items[0].score for items in ranks]
        digit_scores = [items[0].score for items in ranks if items[0].token != ","]
        if not digit_scores:
            raise ValueError("number contains no digit glyphs")
        alternatives = _alternatives(tuple(ranks), max_digits, allow_grouping)
        return _Decoded(
            text,
            min(token_scores),
            sum(digit_scores) / len(digit_scores),
            tuple(ranks),
            alternatives,
        )

    def _decode_touching_digits(
        self, image: np.ndarray, scale: float, max_digits: int, family: str | None = None
    ) -> _Decoded | None:
        """Conservatively split a run whose neighboring digits touch.

        This fallback is limited to ungrouped integers after ordinary connected
        component decoding has already failed.  It considers near-uniform cell
        boundaries, then requires both strong per-cell evidence and a clear
        whole-string margin.  Those guards keep arbitrary text and punctuation
        from being reinterpreted as a number.
        """
        mask = _ink(image)
        rows, columns = np.where(mask)
        if not len(columns):
            return None
        tight = mask[:, columns.min() : columns.max() + 1]
        width = tight.shape[1]
        references = self._scaled_digit_references(scale, family)
        candidates: dict[str, tuple[float, float, tuple[tuple[_Rank, ...], ...]]] = {}
        for count in range(2, max_digits + 1):
            cell_width = width / count
            if not 2.5 * scale <= cell_width <= 12.0 * scale:
                continue
            choices = []
            for index in range(1, count):
                center = round(index * width / count)
                choices.append(tuple(x for x in range(center - 1, center + 2) if 1 <= x < width))
            for internal in product(*choices):
                boundaries = (0, *internal, width)
                if any(
                    right - left < math.ceil(5.0 * scale)
                    for left, right in zip(boundaries, boundaries[1:])
                ):
                    continue
                ranks = []
                for left, right in zip(boundaries, boundaries[1:]):
                    try:
                        glyph = _glyph(tight[:, left:right])
                    except ValueError:
                        break
                    ranked = []
                    for number, variants in enumerate(references):
                        compatible = [item for item in variants if item.holes == glyph.holes]
                        if compatible:
                            ranked.append(
                                _Rank(
                                    str(number),
                                    max(_glyph_score(glyph, item) for item in compatible),
                                )
                            )
                    if not ranked:
                        break
                    ranked.sort(key=lambda item: item.score, reverse=True)
                    ranks.append(tuple(ranked[:3]))
                if len(ranks) != count:
                    continue
                text = "".join(items[0].token for items in ranks)
                if not _valid_text(text, max_digits, False):
                    continue
                scores = [items[0].score for items in ranks]
                minimum, average = min(scores), sum(scores) / len(scores)
                old = candidates.get(text)
                if old is None or (minimum, average) > old[:2]:
                    candidates[text] = minimum, average, tuple(ranks)
        ordered = sorted(
            candidates.items(), key=lambda item: (item[1][0], item[1][1]), reverse=True
        )
        if not ordered:
            return None
        text, (minimum, average, ranks) = ordered[0]
        runner_up = ordered[1][1][0] if len(ordered) > 1 else 0.0
        if minimum < 0.75 or average < 0.86 or minimum - runner_up <= 0.03:
            return None
        return _Decoded(
            text,
            minimum,
            average,
            ranks,
            _alternatives(ranks, max_digits, False),
        )

    def _scaled_digit_references(
        self, scale: float, family: str | None = None
    ) -> tuple[tuple[_Glyph, ...], ...]:
        if family is None and abs(scale - 1.0) < 0.02:
            return self._digits
        resized: list[tuple[_Glyph, ...]] = []
        for native, base in zip(self._digit_images, self._digits):
            variants = list(base)
            for image in native:
                scaled = cv2.resize(
                    image,
                    None,
                    fx=scale,
                    fy=scale,
                    interpolation=cv2.INTER_LINEAR,
                )
                try:
                    variants.append(_glyph(_ink(scaled)))
                except ValueError:
                    continue
            resized.append(tuple(variants))
        if family == "alternate":
            family_scale = scale / 0.7
            rendered = []
            for images in self._alternate_digit_images:
                variants = []
                for image in images:
                    scaled = cv2.resize(
                        image,
                        None,
                        fx=family_scale,
                        fy=family_scale,
                        interpolation=cv2.INTER_LINEAR,
                    )
                    try:
                        variants.append(_glyph(_ink(scaled)))
                    except ValueError:
                        continue
                rendered.append(tuple(variants))
            resized = [
                (*variants, *alternate)
                for variants, alternate in zip(resized, rendered)
            ]
        elif 0.68 <= scale <= 0.72:
            resized = [
                (*variants, *alternate)
                for variants, alternate in zip(resized, self._alternate_digits)
            ]
        return tuple(resized)

    def _comma_references(self, scale: float, family: str | None) -> tuple[_Glyph, ...]:
        if family == "alternate":
            family_scale = scale / 0.7
            rendered = []
            for image in self._alternate_comma_images:
                scaled = cv2.resize(
                    image,
                    None,
                    fx=family_scale,
                    fy=family_scale,
                    interpolation=cv2.INTER_LINEAR,
                )
                try:
                    rendered.append(_glyph(_ink(scaled)))
                except ValueError:
                    continue
            return (self._comma, *rendered)
        if 0.68 <= scale <= 0.72:
            return (self._comma, *self._small_scale_commas)
        return (self._comma,)

    def _rejection_reason(
        self,
        decoded: _Decoded,
        max_digits: int,
        allow_grouping: bool,
    ) -> str:
        digits = decoded.text.replace(",", "")
        if len(digits) > max_digits:
            return "number has more digits than the configured maximum"
        if not _valid_text(decoded.text, max_digits, allow_grouping):
            if len(digits) > 1 and digits.startswith("0"):
                return "multi-digit number has a leading zero"
            if "," in decoded.text:
                return "number has malformed thousands grouping"
            return "number contains unsupported punctuation"
        separators = [items[0].score for items in decoded.ranks if items[0].token == ","]
        if separators and min(separators) < 0.82:
            return "number contains a weakly matched thousands separator"
        weak = [items[0].score for items in decoded.ranks if items[0].token != ","]
        if min(weak) < self._template_score or decoded.average_score < 0.78:
            return "number contains a weakly matched digit"
        for items in decoded.ranks:
            if items[0].token == "," or len(items) < 2:
                continue
            if items[0].score - items[1].score <= self._ambiguity_margin:
                return "number contains an ambiguous digit"
        return ""

    def _ocr_fallback(
        self,
        image: np.ndarray,
        *,
        decoded: _Decoded | None,
        max_digits: int,
        allow_grouping: bool,
        template_reason: str,
    ) -> Reading | None:
        assert self._ocr is not None
        # OCR is corroboration for an otherwise strong template decode, not an
        # independent font reader.  A weak-match rejection means the bounded
        # pixels do not sufficiently resemble the supported Skills font.  Letting
        # OCR promote that case accepts unrelated UI text whenever its string
        # happens to occur in the small template beam.  Only a close digit tie
        # has the shape evidence this fallback is intended to disambiguate.
        if template_reason != "number contains an ambiguous digit":
            return None
        try:
            numeric_reader = getattr(self._ocr, "read_number_boxes", None)
            if numeric_reader is None:
                numeric_reader = self._ocr.read_text_boxes
            boxes = tuple(numeric_reader(image, psm=7, timeout=3.0))
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
            return None
        candidates = _ocr_candidates(boxes, max_digits, allow_grouping)
        if len(candidates) != 1 or decoded is None:
            return None
        ocr_text, ocr_score, ocr_boxes = candidates[0]

        # OCR may resolve a close template tie, but it may not invent a glyph.
        # Require the whole OCR string to occur in the bounded template beam and
        # require every selected template glyph to retain visible shape evidence.
        evidence = {str(item["text"]): item for item in decoded.alternatives}
        matched = evidence.get(ocr_text)
        separator_scores = [items[0].score for items in decoded.ranks if items[0].token == ","]
        if (
            matched is None
            or float(matched["score"]) < 0.62
            or ocr_score < 0.50
            or (separator_scores and min(separator_scores) < 0.82)
        ):
            return None
        return Reading(
            _integer_value(ocr_text),
            Quality.KNOWN,
            diagnostics={
                "reader": "tesseract_with_template_evidence",
                "text": ocr_text,
                "score": min(float(matched["score"]), ocr_score),
                "alternatives": decoded.alternatives,
                "template_rejection": template_reason,
                "ocr_score": ocr_score,
                "ocr_boxes": tuple(box.box for box in ocr_boxes),
            },
        )


def _rgb_image(
    image: np.ndarray,
    *,
    pixel_limit: int,
    side_limit: int | None = None,
) -> np.ndarray:
    if not isinstance(image, np.ndarray) or image.ndim != 3 or image.shape[2] != 3:
        raise ValueError("image must be an HxWx3 RGB numpy array")
    height, width = image.shape[:2]
    if height < 1 or width < 1:
        raise ValueError("image must be non-empty")
    if height * width > pixel_limit or (side_limit is not None and max(height, width) > side_limit):
        raise ValueError("image exceeds the bounded OCR pixel budget")
    if image.dtype != np.uint8:
        raise ValueError("image must use uint8 RGB channels")
    return np.ascontiguousarray(image)


def _load_rgb(path: Path) -> np.ndarray:
    try:
        with Image.open(path) as source:
            return np.ascontiguousarray(np.asarray(source.convert("RGB"), dtype=np.uint8))
    except (OSError, ValueError, UnidentifiedImageError) as exc:
        raise ValueError(f"number template is unreadable: {path}") from exc


def _ink(image: np.ndarray) -> np.ndarray:
    values = image.astype(np.int16)
    gray = np.mean(values, axis=2)
    chroma = np.max(values, axis=2) - np.min(values, axis=2)
    return (gray >= 125.0) & (chroma <= 40)


def _glyph(mask: np.ndarray) -> _Glyph:
    rows, columns = np.where(mask)
    if rows.size == 0:
        raise ValueError("glyph contains no foreground")
    cropped = mask[rows.min() : rows.max() + 1, columns.min() : columns.max() + 1]
    height, width = cropped.shape
    normalized = (
        cv2.resize(cropped.astype(np.uint8), (20, 28), interpolation=cv2.INTER_AREA) >= 0.45
    )
    return _Glyph(normalized, width / height, height, _hole_count(cropped))


def _hole_count(mask: np.ndarray) -> int:
    padded = np.pad(mask.astype(np.uint8), 1)
    count, labels = cv2.connectedComponents(1 - padded, connectivity=4)
    border = set(labels[0]) | set(labels[-1]) | set(labels[:, 0]) | set(labels[:, -1])
    return sum(component not in border for component in range(1, count))


def _glyph_score(candidate: _Glyph, expected: _Glyph) -> float:
    pixel_score = 1.0 - float(np.mean(candidate.normalized != expected.normalized))
    ratio_score = min(candidate.ratio, expected.ratio) / max(candidate.ratio, expected.ratio)
    return 0.85 * pixel_score + 0.15 * ratio_score


def _valid_text(text: str, max_digits: int, allow_grouping: bool) -> bool:
    if _GROUPED_INTEGER.fullmatch(text) is None:
        return False
    if not allow_grouping and "," in text:
        return False
    return len(text.replace(",", "")) <= max_digits


def _integer_value(text: str) -> int:
    return int(text.replace(",", ""))


def _alternatives(
    ranks: tuple[tuple[_Rank, ...], ...],
    max_digits: int,
    allow_grouping: bool,
) -> tuple[dict[str, object], ...]:
    possibilities: list[tuple[str, float]] = []
    best = [items[0] for items in ranks]
    possibilities.append(("".join(item.token for item in best), min(item.score for item in best)))
    for index, items in enumerate(ranks):
        for replacement in items[1:3]:
            changed = list(best)
            changed[index] = replacement
            possibilities.append(
                ("".join(item.token for item in changed), min(item.score for item in changed))
            )
    unique: dict[str, float] = {}
    for text, score in possibilities:
        unique[text] = max(score, unique.get(text, -1.0))
    ordered = sorted(unique.items(), key=lambda item: item[1], reverse=True)[:6]
    return tuple(
        {
            "text": text,
            "value": _integer_value(text)
            if _valid_text(text, max_digits, allow_grouping)
            else None,
            "score": score,
            "reader": "templates",
        }
        for text, score in ordered
    )


def _parse_tesseract_tsv(data: bytes, width: int, height: int) -> tuple[OCRBox, ...]:
    try:
        text = data.decode("utf-8", errors="replace")
        rows = csv.DictReader(text.splitlines(), delimiter="\t")
        found: list[OCRBox] = []
        for row in rows:
            value = (row.get("text") or "").strip()
            if not value:
                continue
            left, top = int(row["left"]), int(row["top"])
            box_width, box_height = int(row["width"]), int(row["height"])
            confidence = float(row["conf"])
            right, bottom = left + box_width, top + box_height
            if (
                confidence < 0
                or left < 0
                or top < 0
                or right <= left
                or bottom <= top
                or right > width
                or bottom > height
            ):
                continue
            found.append(OCRBox(value, (left, top, right, bottom), min(confidence, 100.0) / 100.0))
        return tuple(found)
    except (KeyError, TypeError, ValueError):
        return ()


def _ocr_candidates(
    boxes: Sequence[OCRBox],
    max_digits: int,
    allow_grouping: bool,
) -> tuple[tuple[str, float, tuple[OCRBox, ...]], ...]:
    usable = tuple(
        box
        for box in boxes
        if isinstance(box, OCRBox)
        and box.score >= 0.0
        and re.fullmatch(r"[0-9,]+", box.text.strip()) is not None
    )
    possibilities: list[tuple[str, float, tuple[OCRBox, ...]]] = []
    if len(usable) == 1:
        box = usable[0]
        value = box.text.strip()
        if _valid_text(value, max_digits, allow_grouping):
            possibilities.append((value, box.score, (box,)))
    elif usable:
        ordered = tuple(sorted(usable, key=lambda item: item.box[0]))
        same_line = all(
            min(left.box[3], right.box[3]) > max(left.box[1], right.box[1])
            and right.box[0] - left.box[2]
            <= 1.5 * max(left.box[3] - left.box[1], right.box[3] - right.box[1])
            for left, right in zip(ordered, ordered[1:])
        )
        joined = "".join(item.text.strip() for item in ordered) if same_line else ""
        if same_line and _valid_text(joined, max_digits, allow_grouping):
            possibilities.append((joined, min(item.score for item in ordered), ordered))
    unique: dict[str, tuple[float, tuple[OCRBox, ...]]] = {}
    for text, score, source in possibilities:
        previous = unique.get(text)
        if previous is None or score > previous[0]:
            unique[text] = score, source
    return tuple((text, score, source) for text, (score, source) in sorted(unique.items()))


__all__ = [
    "MAX_DIGITS",
    "MAX_NUMBER_PIXELS",
    "MAX_OCR_PIXELS",
    "OCRBox",
    "NumberReader",
    "TesseractOCR",
]
