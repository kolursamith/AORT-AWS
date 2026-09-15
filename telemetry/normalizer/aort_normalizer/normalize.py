"""Prometheus samples -> I1 observation records.

The central rule: a record only carries a value when Prometheus returned one
finite, unambiguous number. Otherwise the ``quality`` field explains why the
value is absent. Out-of-range values are kept, but flagged, so that suspect
data stays visible to the twin instead of being silently dropped.
"""

from __future__ import annotations

import math
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Protocol

from .catalog import CATALOG, SignalSpec
from .prometheus import Sample

SCHEMA_VERSION = "i1-provisional-0.1"
UNKNOWN_LABEL = "unknown"
# Same pattern the I1 contract enforces for snapshot_id.
_UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


class QueryClient(Protocol):
    def query(self, promql: str, at: float) -> list[Sample]: ...


@dataclass(frozen=True)
class Observation:
    snapshot_id: str
    observed_at: str
    component_id: str
    signal: str
    category: str
    value: float | None
    unit: str
    quality: str
    source: str
    dimensions: dict[str, str] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "observed_at": self.observed_at,
            "component_id": self.component_id,
            "signal": self.signal,
            "category": self.category,
            "value": self.value,
            "unit": self.unit,
            "quality": self.quality,
            "source": self.source,
            "dimensions": dict(self.dimensions),
        }


def format_timestamp(epoch_seconds: float) -> str:
    """UTC ISO-8601 with millisecond precision and a trailing Z."""
    moment = datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _classify(spec: SignalSpec, raw: str) -> tuple[float | None, str]:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None, "non_finite"
    if not math.isfinite(value):
        return None, "non_finite"
    value += 0.0  # normalize -0.0 so it serializes as 0.0
    low, high = spec.bounds
    if (low is not None and value < low) or (high is not None and value > high):
        return value, "out_of_bounds"
    return value, "ok"


def normalize_samples(
    spec: SignalSpec,
    samples: Iterable[Sample],
    snapshot_id: str,
    observed_at: str,
) -> list[Observation]:
    """Convert one signal's query result into one or more observations."""
    samples = list(samples)

    def make(value: float | None, quality: str, dimensions: dict[str, str]) -> Observation:
        return Observation(
            snapshot_id=snapshot_id,
            observed_at=observed_at,
            component_id=spec.component_id,
            signal=spec.signal,
            category=spec.category,
            value=value,
            unit=spec.unit,
            quality=quality,
            source=spec.source,
            dimensions=dimensions,
        )

    if not samples:
        return [make(None, "missing", {})]

    if not spec.group_by:
        if len(samples) > 1:
            return [make(None, "ambiguous", {})]
        value, quality = _classify(spec, samples[0].value)
        return [make(value, quality, {})]

    groups: dict[tuple[str, ...], list[Sample]] = {}
    for sample in samples:
        key = tuple(sample.labels.get(label, UNKNOWN_LABEL) for label in spec.group_by)
        groups.setdefault(key, []).append(sample)

    observations = []
    for key in sorted(groups):
        dimensions = dict(zip(spec.group_by, key))
        members = groups[key]
        if len(members) > 1:
            observations.append(make(None, "ambiguous", dimensions))
        else:
            value, quality = _classify(spec, members[0].value)
            observations.append(make(value, quality, dimensions))
    return observations


def collect_snapshot(
    client: QueryClient,
    catalog: Iterable[SignalSpec] = CATALOG,
    at: float | None = None,
    snapshot_id: str | None = None,
) -> list[Observation]:
    """Query every catalog signal at one evaluation instant.

    Raises TelemetrySourceError (from the client) if Prometheus is unreachable
    or rejects a query - a broken feed must be loud, not quietly empty.
    """
    evaluation_time = time.time() if at is None else at
    if snapshot_id is None:
        snapshot = str(uuid.uuid4())
    elif _UUID4.fullmatch(snapshot_id):
        snapshot = snapshot_id
    else:
        raise ValueError(f"snapshot_id must be a lowercase UUID4, got {snapshot_id!r}")
    observed_at = format_timestamp(evaluation_time)

    observations: list[Observation] = []
    for spec in catalog:
        samples = client.query(spec.promql, evaluation_time)
        observations.extend(normalize_samples(spec, samples, snapshot, observed_at))
    return observations
