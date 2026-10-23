"""Telemetry ingestion for the route twin, guarded by :class:`TelemetryValidator`.

Twin store keys:
* ``route:<route_id>``     - what the twin *believes* about the route (fed to the controller);
* ``integrity:<route_id>`` - verdict/confidence of the latest observation (fed to the budget guard).

With validation enabled, only TRUSTED observations change ``route:<id>``. With
validation disabled (experiment control arm) every observation is believed.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime

from backend.aorts.adapters.twin_state import VersionedStore, update_with_retry
from backend.aorts.app.dependencies import ServiceContext
from backend.aorts.banking.ledger.models import TelemetryObservation
from backend.aorts.contracts.enums import Decision, TelemetryVerdict
from backend.aorts.contracts.events import new_id
from backend.aorts.contracts.requests import RouteTelemetry
from backend.aorts.security.audit import make_record
from backend.aorts.security.telemetry_validator import TelemetryAssessment, TelemetryValidator

logger = logging.getLogger("aorts.telemetry")


class TelemetryService:
    def __init__(
        self,
        ctx: ServiceContext,
        twin: VersionedStore,
        validator: TelemetryValidator | None = None,
        measurement_source=None,
    ):
        s = ctx.settings
        self.ctx = ctx
        self.twin = twin
        self.validator = validator or TelemetryValidator(
            sources=s.telemetry_source_set(),
            max_age_s=s.telemetry_max_age_seconds,
            max_future_s=s.telemetry_max_future_seconds,
            min_confidence=s.telemetry_min_confidence,
            tolerance=s.telemetry_cross_check_tolerance,
            bias_check=s.telemetry_bias_check,
        )
        self._measure = measurement_source or self._switch_measurements
        self._cache: tuple[float, dict] = (0.0, {})
        self.recent: list[dict] = []

    def _switch_measurements(self) -> dict:
        now = time.monotonic()
        if now - self._cache[0] < 1.0:
            return self._cache[1]
        try:
            r = self.ctx.client.get(
                "switch", "/api/v1/metrics/routes", as_identity="CONTROLLER", timeout=1.0
            )
            data = r.json().get("routes", {}) if r.status_code == 200 else {}
        except Exception:  # noqa: BLE001 - measurements unavailable -> validator lowers confidence
            data = {}
        self._cache = (now, data)
        return data

    def ingest(
        self,
        obs: RouteTelemetry,
        *,
        authenticated_source: str | None,
        experiment_id: str | None = None,
    ) -> TelemetryAssessment:
        dest = obs.route_id.split("-", 1)[1]
        measured = self._measure().get(dest, {})
        self.validator.enabled = self.ctx.runtime.defenses().telemetry_validation
        a = self.validator.validate(
            obs, authenticated_source=authenticated_source, measured=measured
        )
        self._persist(obs, a, experiment_id)
        apply_to_twin(self.twin, obs, a)
        self.ctx.metrics.inc("telemetry", verdict=a.verdict.value)
        self.recent.append({"at": datetime.now(UTC).isoformat(), **a.as_dict()})
        del self.recent[:-200]
        return a

    def _persist(
        self, obs: RouteTelemetry, a: TelemetryAssessment, experiment_id: str | None
    ) -> None:
        with self.ctx.db.session() as s:
            s.add(
                TelemetryObservation(
                    event_id=obs.event_id,
                    source_id=obs.source_id,
                    route_id=obs.route_id,
                    sequence=obs.sequence,
                    reported=a.reported,
                    measured=a.measured,
                    verdict=a.verdict.value,
                    confidence=a.confidence,
                    reasons=a.reasons,
                    experiment_id=experiment_id,
                )
            )
        if a.verdict != TelemetryVerdict.TRUSTED:
            action = (
                "TELEMETRY_REJECTED"
                if a.verdict == TelemetryVerdict.REJECTED
                else "TELEMETRY_ANOMALY"
            )
            self.ctx.audit_buffer.record(
                make_record(
                    action,
                    Decision.REJECTED,
                    obs.source_id,
                    route_id=obs.route_id,
                    experiment_id=experiment_id,
                    reason="; ".join(a.reasons)[:300],
                    metadata={
                        "event_id": obs.event_id,
                        "confidence": a.confidence,
                        "reported": a.reported,
                        "measured": a.measured,
                    },
                ),
                sample_key=(obs.source_id, action),
            )


def apply_to_twin(twin: VersionedStore, obs: RouteTelemetry, a: TelemetryAssessment) -> None:
    """Only TRUSTED observations change the twin's belief; every verdict updates integrity state."""
    if a.verdict == TelemetryVerdict.TRUSTED:
        update_with_retry(
            twin,
            f"route:{obs.route_id}",
            lambda d: {
                **{k: v for k, v in a.reported.items() if v is not None},
                "source_id": obs.source_id,
                "sequence": obs.sequence,
                "observed_at": obs.timestamp.isoformat(),
            },
        )
    update_with_retry(
        twin,
        f"integrity:{obs.route_id}",
        lambda d: {
            "verdict": a.verdict.value,
            "confidence": a.confidence,
            "reasons": a.reasons[:5],
            "event_id": obs.event_id,
            "observed_at": obs.timestamp.isoformat(),
            "at": datetime.now(UTC).isoformat(),
        },
    )


class HonestProbe:
    """Publishes truthful route telemetry derived from switch measurements (the twin's normal sensor)."""

    def __init__(
        self,
        service: TelemetryService,
        routes: list[str],
        source_id: str = "twin-probe-1",
        interval_s: float = 2.0,
    ):
        self.service = service
        self.routes = routes
        self.source_id = source_id
        self.interval = interval_s
        self.seq = int(time.time())
        self._stop = threading.Event()

    def publish_once(self) -> None:
        measured = self.service._switch_measurements()
        for route in self.routes:
            m = measured.get(route.split("-", 1)[1])
            if not m or m.get("latency_p50_ms") is None:
                continue
            self.seq += 1
            obs = RouteTelemetry(
                event_id=new_id("tel"),
                source_id=self.source_id,
                route_id=route,
                sequence=self.seq,
                timestamp=datetime.now(UTC),
                latency_p50_ms=m["latency_p50_ms"],
                latency_p95_ms=m["latency_p95_ms"],
                throughput_tps=m["throughput_tps"],
                timeout_rate=m["timeout_rate"],
                backlog=m["backlog"],
                health_score=max(0.0, 1.0 - (m["timeout_rate"] or 0.0)),
            )
            self.service.ingest(obs, authenticated_source=self.source_id)

    def start(self) -> None:
        def _loop():
            while not self._stop.wait(self.interval):
                try:
                    self.publish_once()
                except Exception:  # noqa: BLE001
                    logger.exception("probe publish failed")

        threading.Thread(target=_loop, name="honest-probe", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
