"""Opt-in robust reading plus independently switchable bar and temporal guards.

This adapter retains the existing exact-value and region contract.  Bar fractions
and caption maxima are additional evidence, never replacement resource counts.
The caller must create a new capture-session identity after a capture restart.
"""
from __future__ import annotations

from dataclasses import replace

from tools.benchmark_row_refinement import make_reader
from tools.resource_bar_evidence import (
    ResourceBarEvidence, cross_check, frame_token, validated_maximum,
)
from tools.resource_bar_numbers import BarCaptionReader
from tools.resource_temporal_checks import (
    TemporalBarInterval, TemporalIdentity, TemporalPolicy, TemporalResourceGuard,
)

FIELDS = ("resource_health", "resource_mana")
VARIANTS = ("robust", "bar", "temporal", "combined")


class FusionResourceReader:
    """One worker owns one reader and one explicit capture-session history."""

    def __init__(self, variant="combined", *, capture_session, reader=None,
                 bar_reader=None, caption_reader=None):
        if variant not in VARIANTS:
            raise ValueError("unknown fusion variant")
        if not isinstance(capture_session, str) or not capture_session:
            raise ValueError("an explicit nonempty capture-session identity is required")
        self.variant = variant
        self.capture_session = capture_session
        self.reader = reader if reader is not None else make_reader("rows_cache_robust")
        self.bar_reader = bar_reader if bar_reader is not None else ResourceBarEvidence()
        self.caption_reader = caption_reader if caption_reader is not None else BarCaptionReader()
        self.temporal = TemporalResourceGuard(TemporalPolicy(confirm_changes=True))
        self.last_diagnostics = {}
        self._bar_evidence = {}

    def discover(self, frame, **kwargs):
        return self.reader.discover(frame, **kwargs)

    def validate(self, frame, layout):
        return self.reader.validate(frame, layout)

    def geometry_evidence_boxes(self, frame, anchors):
        return self.reader.geometry_evidence_boxes(frame, anchors)

    def additional_evidence_boxes(self):
        return [box for evidence in self._bar_evidence.values()
                for box in (evidence.bounds, evidence.caption_bounds) if box is not None]

    def reset(self, *, capture_session=None):
        if capture_session is not None:
            if not isinstance(capture_session, str) or not capture_session:
                raise ValueError("capture-session identity must be nonempty")
            self.capture_session = capture_session
        self.temporal.reset()
        self.last_diagnostics = {}
        self._bar_evidence = {}

    def read(self, frame, layout, *, use_ocr=False):
        readings = self.reader.read(frame, layout, use_ocr=use_ocr)
        self.last_diagnostics = {}
        self._bar_evidence = {}
        if self.variant == "robust":
            return readings
        # Numeric UNKNOWN is allowed; absent current layout evidence is not.
        current_layout = layout if any(
            reading.diagnostics.get("layout_fingerprint") == layout.fingerprint
            and reading.diagnostics.get("numeric_eligible") is True
            for reading in readings.values()) else None
        temporal_bars = {}
        token = None
        if self.variant in ("bar", "combined"):
            self._bar_evidence = self.bar_reader.measure(frame, current_layout)
            token = frame_token(frame)
            for field in FIELDS:
                evidence = self._bar_evidence.get(field)
                if evidence is None or field not in readings:
                    continue
                maximum = self.caption_reader.read(frame, evidence) if evidence.usable else None
                caption = dict(self.caption_reader.last_diagnostics) if evidence.usable else {}
                checked = cross_check(frame, evidence, readings[field], maximum)
                maximum_bound = bool(evidence.field == field and
                                     evidence.layout_fingerprint == layout.fingerprint and
                                     validated_maximum(frame, evidence, maximum))
                details = {
                    "bar_available": evidence.usable,
                    "fraction_interval": evidence.fraction_interval,
                    "bounds": evidence.bounds, "direction": evidence.direction,
                    "bar_detail": evidence.detail, "maximum_available": maximum_bound,
                    "maximum": maximum.value if maximum_bound else None,
                    "caption_current": maximum.current if maximum_bound else None,
                    "caption": caption,
                    "vetoed": checked.quality != readings[field].quality,
                }
                self.last_diagnostics[field] = details
                readings[field] = replace(checked, diagnostics={**checked.diagnostics,
                                                               "bar_evidence": details})
                if evidence.usable and current_layout is not None:
                    temporal_bars[field] = TemporalBarInterval(
                        *evidence.fraction_interval, captured_at=evidence.frame_timestamp,
                        layout_fingerprint=evidence.layout_fingerprint,
                        frame_token=evidence.frame_token,
                        maximum=maximum.value if maximum_bound else None,
                        maximum_verified=maximum_bound)
        if self.variant in ("temporal", "combined"):
            identity = None if current_layout is None else TemporalIdentity(
                frame.source, self.capture_session, frame.size,
                current_layout.fingerprint, current_layout.bounds)
            checked = self.temporal.check(
                {field: readings[field] for field in FIELDS if field in readings},
                captured_at=frame.captured_at, identity=identity,
                frame_token=token, bars=temporal_bars)
            readings.update(checked)
            for field, reading in checked.items():
                self.last_diagnostics.setdefault(field, {})["temporal"] = dict(
                    reading.diagnostics.get("temporal", {}))
        return readings
