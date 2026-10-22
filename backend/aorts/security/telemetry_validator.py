"""Route-telemetry integrity validation (Security Module 2: digital-twin telemetry poisoning).

Every observation receives a verdict (TRUSTED / SUSPICIOUS / REJECTED), a
confidence in [0, 1] and human-readable reasons. Only TRUSTED observations may
update the twin and the EWMA baselines; SUSPICIOUS and REJECTED ones can never
raise the recovery budget (see ``BudgetGuard``).
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from backend.aorts.contracts.enums import TelemetryVerdict
from backend.aorts.contracts.requests import RouteTelemetry
from backend.aorts.security.anomaly_detector import EwmaDetector

METRICS = (
    "latency_p50_ms",
    "latency_p95_ms",
    "throughput_tps",
    "timeout_rate",
    "backlog",
    "health_score",
)
RANGES = {
    "latency_p50_ms": (0.0, 60_000.0),
    "latency_p95_ms": (0.0, 60_000.0),
    "throughput_tps": (0.0, 10_000.0),
    "timeout_rate": (0.0, 1.0),
    "backlog": (0, 1_000_000),
    "health_score": (0.0, 1.0),
}
# Direction in which a lie makes the route look HEALTHIER than it is (the dangerous direction).
OPTIMISTIC = {
    "latency_p50_ms": -1,
    "latency_p95_ms": -1,
    "throughput_tps": +1,
    "timeout_rate": -1,
    "backlog": -1,
}
# Minimum absolute slack so tiny values (e.g. 0 vs 1 backlog) are not flagged.
ABS_FLOOR = {
    "latency_p50_ms": 20.0,
    "latency_p95_ms": 40.0,
    "throughput_tps": 2.0,
    "timeout_rate": 0.05,
    "backlog": 5,
}


@dataclass
class TelemetryAssessment:
    event_id: str
    route_id: str
    verdict: TelemetryVerdict
    confidence: float
    reasons: list[str] = field(default_factory=list)
    measured: dict = field(default_factory=dict)
    reported: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "route_id": self.route_id,
            "verdict": self.verdict.value,
            "confidence": round(self.confidence, 3),
            "reasons": self.reasons,
            "measured": self.measured,
            "reported": self.reported,
        }


class TelemetryValidator:
    def __init__(
        self,
        *,
        sources: set[str],
        max_age_s: int = 30,
        max_future_s: int = 5,
        min_confidence: float = 0.6,
        tolerance: float = 0.5,
        min_measured_samples: int = 5,
        detector: EwmaDetector | None = None,
        enabled: bool = True,
        bias_check: bool = True,
        bias_allowance: float = 0.10,
        bias_threshold: float = 0.6,
    ):
        self.sources = sources
        self.max_age = timedelta(seconds=max_age_s)
        self.max_future = timedelta(seconds=max_future_s)
        self.min_confidence = min_confidence
        self.tolerance = tolerance
        self.min_samples = min_measured_samples
        self.detector = detector or EwmaDetector()
        self.enabled = enabled
        # Persistent-bias (one-sided CUSUM) check: a lie that stays inside ``tolerance`` on every
        # sample still accumulates. Per sample, the relative optimistic deviation minus the
        # allowance is added; crossing the threshold marks the source/route as biased.
        self.bias_check = bias_check
        self.bias_allowance = bias_allowance
        self.bias_threshold = bias_threshold
        self._bias: dict[tuple[str, str, str], float] = {}
        self._last_seq: dict[tuple[str, str], int] = {}
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()

    def validate(
        self,
        obs: RouteTelemetry,
        *,
        authenticated_source: str | None,
        measured: dict | None,
        now: datetime | None = None,
    ) -> TelemetryAssessment:
        now = now or datetime.now(UTC)
        reported = {m: getattr(obs, m) for m in METRICS}
        a = TelemetryAssessment(
            obs.event_id, obs.route_id, TelemetryVerdict.TRUSTED, 1.0, [], measured or {}, reported
        )
        if not self.enabled:  # "defenses disabled" arm of the experiment: everything is believed
            a.reasons.append("validation disabled (experiment control arm)")
            return a

        hard: list[str] = []
        # 1. Source identity: the signer must be a registered probe AND match the claimed source_id.
        if authenticated_source is None:
            hard.append("unauthenticated source")
        elif authenticated_source != obs.source_id:
            hard.append(
                f"source_id '{obs.source_id}' does not match authenticated identity '{authenticated_source}'"
            )
        elif authenticated_source not in self.sources:
            hard.append(f"source '{authenticated_source}' is not a registered telemetry source")
        # 2. Schema version.
        if obs.schema_version != "1.0":
            hard.append(f"unsupported schema_version {obs.schema_version}")
        # 3. Timestamp window.
        if obs.timestamp < now - self.max_age:
            hard.append("stale observation (timestamp too old)")
        if obs.timestamp > now + self.max_future:
            hard.append("observation timestamp is in the future")
        # 4. Numeric ranges and internal consistency.
        for metric, value in reported.items():
            if value is None:
                continue
            lo, hi = RANGES[metric]
            if not (lo <= value <= hi) or value != value:  # NaN check
                hard.append(f"{metric}={value} outside [{lo}, {hi}]")
        p50, p95 = reported["latency_p50_ms"], reported["latency_p95_ms"]
        if p50 is not None and p95 is not None and p95 < p50:
            hard.append("latency_p95_ms < latency_p50_ms")
        present = [m for m, v in reported.items() if v is not None]
        if not present:
            hard.append("no metric values present")

        with self._lock:
            # 5. Duplicate event id and 6. sequence monotonicity per (source, route).
            if obs.event_id in self._seen:
                hard.append("duplicate event_id (replayed telemetry)")
            key = (obs.source_id, obs.route_id)
            last = self._last_seq.get(key)
            if last is not None and obs.sequence <= last:
                hard.append(
                    f"sequence {obs.sequence} not greater than last accepted {last} (replay/out-of-order)"
                )
            if hard:
                a.verdict, a.confidence, a.reasons = TelemetryVerdict.REJECTED, 0.0, hard
                self._remember(obs.event_id)
                return a

            penalty = 0.0
            # 7. Missing values lower confidence (they cannot be cross-checked).
            missing = [
                m
                for m in ("latency_p95_ms", "throughput_tps", "timeout_rate", "backlog")
                if reported[m] is None
            ]
            if missing:
                penalty += 0.15 * len(missing)
                a.reasons.append(f"missing values: {missing}")
            # 8. Independent comparison with switch-side measurements.
            corroborated: set[str] = set()
            cross_checked = bool(measured) and (measured.get("samples") or 0) >= self.min_samples
            if cross_checked:
                for metric, direction in OPTIMISTIC.items():
                    rv, mv = reported.get(metric), measured.get(metric)
                    if rv is None or mv is None:
                        continue
                    diff = rv - mv
                    slack = max(abs(mv) * self.tolerance, ABS_FLOOR[metric])
                    if abs(diff) > slack:
                        optimistic = (diff * direction) > 0
                        penalty += 0.45 if optimistic else 0.2
                        a.reasons.append(
                            f"{metric}: reported {rv:g} vs measured {mv:g}"
                            f"{' (optimistic: unsafe direction)' if optimistic else ''}"
                        )
                    else:
                        corroborated.add(metric)
                    if self.bias_check:
                        score = self._accumulate_bias(key, metric, rv, mv, direction)
                        if score >= self.bias_threshold:
                            corroborated.discard(metric)
                            penalty += 0.45
                            a.reasons.append(
                                f"{metric}: persistent optimistic bias vs switch measurement "
                                f"(CUSUM {score:.2f} >= {self.bias_threshold:g})"
                            )
            else:
                # Without an independent measurement nothing can be corroborated: stay below the
                # trust threshold so uncorroborated telemetry can never raise the budget.
                penalty += 0.45
                a.reasons.append(
                    "no independent switch-side measurement to corroborate (treated as untrusted)"
                )
            # 9. EWMA anomaly detection against previously accepted observations. A sudden change
            # that the switch independently corroborates is a real route change, not an attack: it
            # is learned (otherwise a genuine degradation would freeze the twin forever). An
            # uncorroborated change is penalised, more heavily when it makes the route look healthier.
            for metric in present:
                anomalous, z = self.detector.is_anomalous(
                    obs.route_id, metric, float(reported[metric])
                )
                if not anomalous:
                    continue
                if metric in corroborated:
                    a.reasons.append(
                        f"{metric} changed sharply (z={z:.1f}) but matches switch measurement"
                    )
                    continue
                baseline = self.detector.baseline(obs.route_id, metric) or {
                    "mean": reported[metric]
                }
                optimistic = (float(reported[metric]) - baseline["mean"]) * OPTIMISTIC.get(
                    metric, 0
                ) > 0
                penalty += 0.25 if optimistic else 0.1
                a.reasons.append(
                    f"{metric} anomalous vs EWMA baseline (z={z:.1f})"
                    f"{' (optimistic, uncorroborated)' if optimistic else ''}"
                )
            a.confidence = max(0.0, 1.0 - penalty)
            a.verdict = (
                TelemetryVerdict.TRUSTED
                if a.confidence >= self.min_confidence
                else TelemetryVerdict.SUSPICIOUS
            )
            self._remember(obs.event_id)
            self._last_seq[key] = obs.sequence
            if a.verdict == TelemetryVerdict.TRUSTED:
                for metric in present:
                    self.detector.update(obs.route_id, metric, float(reported[metric]))
        return a

    def _accumulate_bias(
        self, key: tuple[str, str], metric: str, rv: float, mv: float, direction: int
    ) -> float:
        """Update and return the one-sided CUSUM of the relative optimistic deviation.

        The score is capped at twice the threshold so an honest source recovers within a few
        samples once its reports agree with the switch again.
        """
        rel = direction * (rv - mv) / max(abs(mv), ABS_FLOOR[metric])
        rel = max(-1.0, min(1.0, rel))
        k = (*key, metric)
        score = max(0.0, self._bias.get(k, 0.0) + rel - self.bias_allowance)
        score = min(score, 2 * self.bias_threshold)
        self._bias[k] = score
        return score

    def _remember(self, event_id: str) -> None:
        self._seen[event_id] = None
        while len(self._seen) > 50_000:
            self._seen.popitem(last=False)
