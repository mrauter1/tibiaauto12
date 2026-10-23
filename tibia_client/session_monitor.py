"""Readings presentation over the application's shared observation session."""
from __future__ import annotations

from collections.abc import Mapping
import math

from tibia_client.adaptive import FIELDS
from tibia_client.models import Quality, Reading, Sensor, SensorKind
from tibia_client.resource_monitor import MonitorState, Sample


class SessionResourceMonitor:
    """A passive UI facade; the session owns capture, decoding and scheduling."""

    def __init__(self, client, *, interval=0.1):
        self.client = client
        self.interval = interval
        self._sensor = Sensor(SensorKind.READINGS)
        self._subscription = None
        self._closed = False
        self._observation = None
        self._state = self._empty("Stopped", "Start monitoring to use the shared session.")

    @staticmethod
    def _empty(phase, detail, *, running=False):
        return MonitorState(phase, detail=detail, running=running,
                            source="Shared automation session")

    def _clear(self, phase, detail, *, running=False):
        self._observation = None
        self._state = self._empty(phase, detail, running=running)
        return self._state

    def _session_detail(self, fallback):
        diagnostics = self.client.diagnostics()
        capture = getattr(diagnostics, "capture", None)
        reasons = getattr(capture, "reasons", ())
        if reasons:
            return "; ".join(reasons)
        failures = getattr(diagnostics, "failure_reasons", ())
        if failures:
            return str(failures[-1])
        return getattr(diagnostics, "readiness_reason", None) or fallback

    @staticmethod
    def _timing(diagnostics, key):
        value = diagnostics.get(key, 0.0)
        return float(value) if (type(value) in (int, float)
                                and math.isfinite(value) and value >= 0) else 0.0

    def state(self):
        if self._subscription is None:
            return self._state
        if self.client.closed:
            self._subscription = None
            return self._clear("Error", self._session_detail("The shared session is closed."))
        view = self.client.observation_view(self._subscription)
        if view is None:
            self._subscription = None
            return self._clear("Stopped", "Shared monitoring stopped. Press Start monitoring to resume.")
        observation = view.snapshot.observations.get(self._sensor)
        frame = view.frame
        if observation is None or frame is None or observation.frame_id != view.snapshot.frame_id:
            return self._clear("Waiting for capture", self._session_detail(
                "Waiting for the shared session's next capture."), running=True)
        if observation is self._observation and frame is self._state.sample.frame:
            return self._state

        rows = observation.value
        if (observation.quality is not Quality.KNOWN or not isinstance(rows, Mapping)
                or any(not isinstance(name, str) or not isinstance(row, Reading)
                       for name, row in rows.items())):
            quality = observation.quality if observation.quality is not Quality.KNOWN else Quality.ERROR
            rows = {name: Reading(None, quality, observation.detail or "Invalid shared readings")
                    for name in FIELDS}
        numeric = {name: rows[name] for name in FIELDS if name in rows}
        visual = {name: reading for name, reading in rows.items() if name not in FIELDS}
        timings = [self._timing(observation.diagnostics, key) for key in
                   ("capture_ms", "discovery_ms", "decode_ms", "total_ms")]
        sample = Sample(observation.frame_id, frame, numeric, observation.observed_at, *timings)
        visual_sample = Sample(
            observation.frame_id, frame, visual, observation.observed_at, timings[0], 0.0,
            self._timing(observation.diagnostics, "visual_decode_ms"), timings[3],
        )
        known = sum(reading.quality is Quality.KNOWN for reading in numeric.values())
        self._state = MonitorState(
            "Monitoring", sample, f"{known}/4 resource fields read · shared session",
            "Shared automation session", True, sample,
            str(observation.diagnostics.get("recovery", "")), visual_sample,
        )
        self._observation = observation
        return self._state

    @property
    def busy(self):
        return self.state().running

    def start(self):
        if self._closed:
            raise RuntimeError("Monitor is closed")
        if self.busy:
            return False
        if self.client.closed:
            detail = self._session_detail("The shared session is closed.")
            self._clear("Error", detail)
            raise RuntimeError(detail)
        try:
            self._subscription = self.client.subscribe_observations((self._sensor,), interval=self.interval)
        except Exception as exc:
            self._clear("Error", f"Could not start shared monitoring: {exc}")
            raise
        self._clear("Starting", "Waiting for the shared session's next capture.", running=True)
        return True

    def stop(self):
        subscription, self._subscription = self._subscription, None
        try:
            if subscription is not None:
                self.client.unsubscribe_observations(subscription)
        finally:
            self._clear("Stopped", "Monitoring stopped. Previous readings were cleared.")

    def close(self):
        self._closed = True
        self.stop()

    def wait(self, timeout=3.0):
        """There are no facade-owned workers to join."""
        return True

    def recalibrate(self):
        if self._closed:
            return
        restart = self.busy
        self.stop()
        try:
            self.client.reset_readings()
            if restart:
                self.start()
        except Exception as exc:
            self._clear("Error", f"Could not reload readings: {exc}")
