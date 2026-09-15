"""Minimal Prometheus HTTP API client for instant queries."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import requests


class TelemetrySourceError(RuntimeError):
    """Prometheus could not be reached or did not return a usable result."""


@dataclass(frozen=True)
class Sample:
    """One series from an instant-vector result: its labels and raw value string."""

    labels: dict[str, str] = field(default_factory=dict)
    value: str = ""


class PrometheusClient:
    """Evaluates instant queries at an explicit time.

    Querying every signal at the same explicit evaluation time is what makes a
    snapshot internally consistent.
    """

    def __init__(
        self,
        base_url: str,
        timeout: float = 10.0,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()

    def query(self, promql: str, at: float) -> list[Sample]:
        url = f"{self.base_url}/api/v1/query"
        try:
            response = self.session.get(
                url, params={"query": promql, "time": at}, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise TelemetrySourceError(f"Prometheus request failed: {exc}") from exc

        try:
            body: Any = response.json()
        except ValueError as exc:
            raise TelemetrySourceError(
                f"Prometheus returned non-JSON (HTTP {response.status_code}): "
                f"{response.text[:200]}"
            ) from exc

        if not isinstance(body, dict) or body.get("status") != "success":
            detail = body.get("error") if isinstance(body, dict) else None
            raise TelemetrySourceError(
                f"Prometheus query failed (HTTP {response.status_code}): "
                f"{detail or 'unexpected response'} [query: {promql}]"
            )

        data = body.get("data")
        if not isinstance(data, dict):
            raise TelemetrySourceError(
                f"Prometheus response has no data object [query: {promql}]"
            )
        result_type = data.get("resultType")
        if result_type != "vector":
            raise TelemetrySourceError(
                f"expected an instant vector, got {result_type!r} [query: {promql}]"
            )
        result = data.get("result")
        if not isinstance(result, list):
            raise TelemetrySourceError(
                f"Prometheus vector result is not a list [query: {promql}]"
            )

        samples: list[Sample] = []
        for series in result:
            try:
                labels = dict(series["metric"])
                _, raw_value = series["value"]
            except (KeyError, TypeError, ValueError) as exc:
                raise TelemetrySourceError(
                    f"malformed series in Prometheus result: {series!r}"
                ) from exc
            labels.pop("__name__", None)
            samples.append(Sample(labels=labels, value=str(raw_value)))
        return samples
