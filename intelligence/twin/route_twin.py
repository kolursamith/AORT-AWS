"""Route Digital Twin (Stage 1): a deterministic projection of observed route telemetry.

The twin records what trusted sources observed about each registered payment route. It does
not estimate capacity, predict, decide, dispatch checks or touch money: every estimate field
in its snapshots stays ``None`` until a later stage supplies an estimator.

Semantics
---------
* Isolation: state is keyed by ``(experiment_id, route_id)``. A route must be registered,
  with the sources allowed to report it, before telemetry is accepted. Nothing is created
  implicitly.
* Trust: only ``AUTHENTICATED`` envelopes are applied. Authenticated samples carrying any
  validation flag go to the flagged slot and never replace the trusted observation
  (contract decision 9). The twin never verifies signatures; Track B does.
* Sequences are scoped per ``(experiment_id, source_id)`` across all routes, as the v1.1
  contract defines. A higher sequence is applied (a gap is recorded, not rejected); an
  already-accepted sequence with identical content is a duplicate; with different content it
  is a conflict and quarantines the source; a lower, unrecognised sequence is out of order.
  Content is compared by SHA-256 of the telemetry's canonical JSON. Windows from one source
  on one route must not overlap; an overlap is treated as a conflict.
* Snapshot version: starts at 0 on registration and increases by exactly 1 for each applied
  observation. Duplicates, rejections and snapshot reads never change it. Freshness is
  evaluated against the injected clock when a snapshot is taken, so two snapshots with the
  same version may differ in ``freshness_status`` only.
* Freshness: ``NO_DATA`` with no observation; ``FRESH`` while the latest trusted observation
  is at most ``freshness_threshold_s`` old; otherwise ``STALE``. A route holding only flagged
  telemetry is ``STALE`` (the v1.1 contract forbids ``NO_DATA`` when an observation is
  present, and flagged data must not count as fresh evidence).
* Concurrency: one lock serialises every public method. No I/O happens while it is held.
"""

import hashlib
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError, model_validator

from contracts.v1_1 import (
    SCHEMA_VERSION,
    ErrorCode,
    FreshnessStatus,
    RouteDescriptor,
    RouteTelemetry,
    RouteTwinSnapshot,
    SourceSequenceMark,
    TelemetryTrustStatus,
    ValidatedRouteTelemetry,
)
from contracts.v1_1.models import (
    Identifier,
    NonNegativeFloat,
    PositiveFloat,
    PositiveInt,
    VersionLabel,
)
from intelligence.common.clock import Clock

_IDENTIFIER = TypeAdapter(Identifier)


class RouteTwinError(Exception):
    """Base class for Route Twin errors."""


class UnknownRouteError(RouteTwinError, LookupError):
    """The requested (experiment_id, route_id) has not been registered."""


class RouteRegistrationConflictError(RouteTwinError, ValueError):
    """A different descriptor or source set is already registered for this route."""


class RouteTwinInvariantError(RouteTwinError, RuntimeError):
    """Internal state or the injected clock violated an invariant; the registry is unusable."""


class RouteTwinConfig(BaseModel):
    """Explicit twin configuration. Assumed values are reported as assumptions, never as data."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    model_version: VersionLabel
    max_observation_age_s: PositiveFloat
    max_future_skew_s: NonNegativeFloat
    freshness_threshold_s: PositiveFloat
    recent_sequence_window: PositiveInt = 1024
    assumed_configured_capacity_rps: PositiveFloat | None = None
    assumed_client_timeout_s: PositiveFloat | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "RouteTwinConfig":
        if self.freshness_threshold_s > self.max_observation_age_s:
            raise ValueError(
                "freshness_threshold_s cannot exceed max_observation_age_s: a sample too old "
                "to accept would still count as fresh"
            )
        return self


class IngestStatus(StrEnum):
    APPLIED = "APPLIED"
    DUPLICATE = "DUPLICATE"
    OUT_OF_ORDER = "OUT_OF_ORDER"
    CONFLICT = "CONFLICT"
    STALE = "STALE"
    FUTURE = "FUTURE"
    REJECTED_TRUST = "REJECTED_TRUST"
    REJECTED_IDENTITY = "REJECTED_IDENTITY"
    REJECTED_SOURCE = "REJECTED_SOURCE"


@dataclass(frozen=True, slots=True)
class IngestResult:
    """Outcome of one ingestion. ``applied`` is true only when route state changed."""

    status: IngestStatus
    error_code: ErrorCode | None
    detail: str
    experiment_id: str
    route_id: str
    source_id: str
    sequence: int
    snapshot_version: int | None
    trusted: bool = False
    missing_sequences: int = 0

    @property
    def applied(self) -> bool:
        return self.status is IngestStatus.APPLIED

    @property
    def sequence_gap(self) -> bool:
        return self.missing_sequences > 0


@dataclass(frozen=True, slots=True)
class SourceStatus:
    """Read-only view of the per-experiment sequencing state of one telemetry source."""

    experiment_id: str
    source_id: str
    last_sequence: int | None
    gaps_detected: int
    missing_sequences_total: int
    quarantined: bool
    quarantine_reason: str | None


@dataclass
class _SourceState:
    recent_window: int
    last_sequence: int | None = None
    recent: OrderedDict[int, str] = field(default_factory=OrderedDict)
    gaps_detected: int = 0
    missing_sequences_total: int = 0
    quarantine_reason: str | None = None

    def remember(self, sequence: int, fingerprint: str) -> None:
        self.recent[sequence] = fingerprint
        while len(self.recent) > self.recent_window:
            self.recent.popitem(last=False)


@dataclass
class _RouteState:
    descriptor: RouteDescriptor
    allowed_sources: frozenset[str]
    last_updated_at: datetime
    snapshot_version: int = 0
    trusted: RouteTelemetry | None = None
    flagged: ValidatedRouteTelemetry | None = None
    watermarks: dict[str, int] = field(default_factory=dict)
    last_window_end: dict[str, datetime] = field(default_factory=dict)


def _fingerprint(telemetry: RouteTelemetry) -> str:
    return hashlib.sha256(telemetry.model_dump_json().encode("utf-8")).hexdigest()


def _is_newer(candidate: RouteTelemetry, current: RouteTelemetry | None) -> bool:
    return current is None or candidate.observation_timestamp >= current.observation_timestamp


class RouteTwinRegistry:
    """In-memory Route Twins for any number of experiments and routes."""

    def __init__(self, config: RouteTwinConfig, clock: Clock) -> None:
        if not isinstance(config, RouteTwinConfig):
            raise TypeError("config must be a RouteTwinConfig")
        self._config = config
        self._clock = clock
        self._lock = threading.Lock()
        self._routes: dict[tuple[str, str], _RouteState] = {}
        self._sources: dict[tuple[str, str], _SourceState] = {}
        self._experiments: set[str] = set()
        self._last_now: datetime | None = None

    @property
    def config(self) -> RouteTwinConfig:
        return self._config

    # ------------------------------------------------------------------ registration

    def register_route(self, descriptor: RouteDescriptor, sources: Iterable[str]) -> bool:
        """Register a route and the sources allowed to report it.

        Returns True when the route is newly registered and False when the identical
        registration already exists. A different descriptor or source set raises.
        """

        if not isinstance(descriptor, RouteDescriptor):
            raise TypeError("descriptor must be a RouteDescriptor")
        if isinstance(sources, str):
            raise TypeError("sources must be an iterable of source identifiers, not a string")
        allowed = frozenset(_IDENTIFIER.validate_python(source) for source in sources)
        if not allowed:
            raise ValueError("a route needs at least one allowed source")

        key = (descriptor.experiment_id, descriptor.route_id)
        with self._lock:
            now = self._now()
            existing = self._routes.get(key)
            if existing is not None:
                if existing.descriptor == descriptor and existing.allowed_sources == allowed:
                    return False
                raise RouteRegistrationConflictError(
                    f"route {key} is already registered with a different descriptor or sources"
                )
            self._routes[key] = _RouteState(
                descriptor=descriptor, allowed_sources=allowed, last_updated_at=now
            )
            self._experiments.add(descriptor.experiment_id)
            for source_id in allowed:
                self._sources.setdefault(
                    (descriptor.experiment_id, source_id),
                    _SourceState(recent_window=self._config.recent_sequence_window),
                )
            return True

    # ------------------------------------------------------------------ ingestion

    def ingest(self, item: ValidatedRouteTelemetry) -> IngestResult:
        """Apply one validated telemetry envelope, or report precisely why it was not applied."""

        if not isinstance(item, ValidatedRouteTelemetry):
            raise TypeError("ingest expects a ValidatedRouteTelemetry envelope")
        telemetry = item.telemetry
        with self._lock:
            now = self._now()

            def result(
                status: IngestStatus,
                code: ErrorCode | None,
                detail: str,
                route: _RouteState | None = None,
                missing: int = 0,
            ) -> IngestResult:
                return IngestResult(
                    status=status,
                    error_code=code,
                    detail=detail,
                    experiment_id=item.experiment_id,
                    route_id=item.route_id,
                    source_id=telemetry.source_id,
                    sequence=telemetry.sequence,
                    snapshot_version=route.snapshot_version if route else None,
                    trusted=status is IngestStatus.APPLIED and not item.validation_flags,
                    missing_sequences=missing,
                )

            # The contract already enforces these, but a model built without validation
            # (model_construct) must not be able to alter state.
            if item.experiment_id != telemetry.experiment_id:
                return result(
                    IngestStatus.REJECTED_IDENTITY,
                    ErrorCode.EXPERIMENT_MISMATCH,
                    "envelope experiment_id differs from telemetry",
                )
            if item.route_id != telemetry.route_id:
                return result(
                    IngestStatus.REJECTED_IDENTITY,
                    ErrorCode.ROUTE_IDENTITY_MISMATCH,
                    "envelope route_id differs from telemetry",
                )
            if item.experiment_id not in self._experiments:
                return result(
                    IngestStatus.REJECTED_IDENTITY,
                    ErrorCode.EXPERIMENT_MISMATCH,
                    "experiment has no registered routes",
                )
            route = self._routes.get((item.experiment_id, item.route_id))
            if route is None:
                return result(
                    IngestStatus.REJECTED_IDENTITY,
                    ErrorCode.ROUTE_IDENTITY_MISMATCH,
                    "route is not registered for this experiment",
                )

            source_id = telemetry.source_id
            if source_id not in route.allowed_sources:
                return result(
                    IngestStatus.REJECTED_SOURCE,
                    ErrorCode.UNKNOWN_SOURCE,
                    "source is not registered for this route",
                    route,
                )
            source = self._sources[(item.experiment_id, source_id)]
            if source.quarantine_reason is not None:
                return result(
                    IngestStatus.REJECTED_SOURCE,
                    ErrorCode.UNTRUSTED_SOURCE,
                    f"source is quarantined: {source.quarantine_reason}",
                    route,
                )
            if item.trust_status != TelemetryTrustStatus.AUTHENTICATED:
                return result(
                    IngestStatus.REJECTED_TRUST,
                    ErrorCode.UNTRUSTED_SOURCE,
                    f"trust_status is {item.trust_status}",
                    route,
                )

            age_s = (now - telemetry.observation_timestamp).total_seconds()
            if age_s > self._config.max_observation_age_s:
                return result(
                    IngestStatus.STALE,
                    ErrorCode.STALE_OBSERVATION,
                    f"observation is {age_s:g}s old",
                    route,
                )
            if -age_s > self._config.max_future_skew_s:
                return result(
                    IngestStatus.FUTURE,
                    ErrorCode.FUTURE_TIMESTAMP,
                    f"observation is {-age_s:g}s in the future",
                    route,
                )

            sequence = telemetry.sequence
            fingerprint = _fingerprint(telemetry)
            seen = source.recent.get(sequence)
            if seen == fingerprint:
                return result(
                    IngestStatus.DUPLICATE,
                    ErrorCode.DUPLICATE_SEQUENCE,
                    "identical sample already applied",
                    route,
                )
            if seen is not None:
                return self._conflict(
                    source, route, result, f"sequence {sequence} reused with different content"
                )
            if source.last_sequence is not None and sequence <= source.last_sequence:
                return result(
                    IngestStatus.OUT_OF_ORDER,
                    ErrorCode.OUT_OF_ORDER_SEQUENCE,
                    f"sequence {sequence} is not after {source.last_sequence}",
                    route,
                )
            previous_end = route.last_window_end.get(source_id)
            if previous_end is not None and telemetry.observation_window_start < previous_end:
                return self._conflict(
                    source, route, result, "observation window overlaps the previous window"
                )

            missing = 0 if source.last_sequence is None else sequence - source.last_sequence - 1
            self._apply(item, route, source, fingerprint, missing, now)
            detail = f"applied; {missing} sequence(s) missing" if missing else "applied"
            return result(IngestStatus.APPLIED, None, detail, route, missing)

    def _conflict(
        self,
        source: _SourceState,
        route: _RouteState,
        result: Callable[..., IngestResult],
        reason: str,
    ) -> IngestResult:
        source.quarantine_reason = reason
        return result(IngestStatus.CONFLICT, ErrorCode.CONFLICTING_SEQUENCE, reason, route)

    def _apply(
        self,
        item: ValidatedRouteTelemetry,
        route: _RouteState,
        source: _SourceState,
        fingerprint: str,
        missing: int,
        now: datetime,
    ) -> None:
        telemetry = item.telemetry
        source.last_sequence = telemetry.sequence
        source.remember(telemetry.sequence, fingerprint)
        if missing:
            source.gaps_detected += 1
            source.missing_sequences_total += missing

        route.watermarks[telemetry.source_id] = telemetry.sequence
        route.last_window_end[telemetry.source_id] = telemetry.observation_window_end
        if item.validation_flags:
            if route.flagged is None or _is_newer(telemetry, route.flagged.telemetry):
                route.flagged = item
        elif _is_newer(telemetry, route.trusted):
            route.trusted = telemetry
        route.snapshot_version += 1
        route.last_updated_at = now
        # Fail fast if the new state cannot be expressed as a valid contract snapshot.
        self._build_snapshot(route, now)

    # ------------------------------------------------------------------ reads

    def snapshot(self, experiment_id: str, route_id: str) -> RouteTwinSnapshot:
        """Return an immutable snapshot. Reading never changes the snapshot version."""

        with self._lock:
            now = self._now()
            route = self._routes.get((experiment_id, route_id))
            if route is None:
                raise UnknownRouteError(f"route ({experiment_id}, {route_id}) is not registered")
            return self._build_snapshot(route, now)

    def source_status(self, experiment_id: str, source_id: str) -> SourceStatus:
        with self._lock:
            source = self._sources.get((experiment_id, source_id))
            if source is None:
                raise UnknownRouteError(
                    f"source ({experiment_id}, {source_id}) is not registered on any route"
                )
            return SourceStatus(
                experiment_id=experiment_id,
                source_id=source_id,
                last_sequence=source.last_sequence,
                gaps_detected=source.gaps_detected,
                missing_sequences_total=source.missing_sequences_total,
                quarantined=source.quarantine_reason is not None,
                quarantine_reason=source.quarantine_reason,
            )

    def release_source_quarantine(self, experiment_id: str, source_id: str) -> bool:
        """Explicit operator action. Returns True if the source was quarantined."""

        with self._lock:
            source = self._sources.get((experiment_id, source_id))
            if source is None:
                raise UnknownRouteError(
                    f"source ({experiment_id}, {source_id}) is not registered on any route"
                )
            was_quarantined = source.quarantine_reason is not None
            source.quarantine_reason = None
            return was_quarantined

    def registered_routes(self, experiment_id: str) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(route for exp, route in self._routes if exp == experiment_id))

    # ------------------------------------------------------------------ internals

    def _now(self) -> datetime:
        now = self._clock.now()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise RouteTwinInvariantError("clock must return a timezone-aware datetime")
        if self._last_now is not None and now < self._last_now:
            raise RouteTwinInvariantError("clock moved backwards")
        self._last_now = now
        return now

    def _freshness(self, route: _RouteState, now: datetime) -> FreshnessStatus:
        if route.trusted is None and route.flagged is None:
            return FreshnessStatus.NO_DATA
        if route.trusted is None:
            return FreshnessStatus.STALE
        age_s = (now - route.trusted.observation_timestamp).total_seconds()
        if age_s <= self._config.freshness_threshold_s:
            return FreshnessStatus.FRESH
        return FreshnessStatus.STALE

    def _build_snapshot(self, route: _RouteState, now: datetime) -> RouteTwinSnapshot:
        observed = [obs.observation_timestamp for obs in (route.trusted,) if obs is not None]
        if route.flagged is not None:
            observed.append(route.flagged.telemetry.observation_timestamp)
        descriptor = route.descriptor
        try:
            return RouteTwinSnapshot(
                schema_version=SCHEMA_VERSION,
                experiment_id=descriptor.experiment_id,
                route_id=descriptor.route_id,
                snapshot_version=route.snapshot_version,
                model_version=self._config.model_version,
                last_updated_at=route.last_updated_at,
                last_observed_at=max(observed) if observed else None,
                latest_sequence_per_source=tuple(
                    SourceSequenceMark(source_id=source_id, sequence=sequence)
                    for source_id, sequence in sorted(route.watermarks.items())
                ),
                freshness_status=self._freshness(route, now),
                latest_trusted_observation=route.trusted,
                latest_flagged_observation=route.flagged,
                assumed_configured_capacity_rps=self._config.assumed_configured_capacity_rps,
                assumed_client_timeout_s=self._config.assumed_client_timeout_s,
            )
        except ValidationError as error:
            raise RouteTwinInvariantError(
                f"route state cannot form a valid snapshot: {error}"
            ) from error
