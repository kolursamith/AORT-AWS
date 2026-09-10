"""HTTP client for the Apache Fineract REST API.

Wraps requests with the tenant header and basic auth Fineract expects, measures
per-call latency, and turns every response into an OperationResult so the run
report reflects exactly what the server returned.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

import requests
from requests.auth import HTTPBasicAuth

from .config import FineractConfig
from .report import OperationResult, RunReport

# Phase 3 instrumentation. Optional by design: the package lives in
# telemetry/ and is inert unless OTEL_EXPORTER_OTLP_ENDPOINT is set, so the
# banking module runs unchanged without it.
try:
    import aort_telemetry as _telemetry
except ImportError:  # pragma: no cover - telemetry is optional
    _telemetry = None


class FineractError(RuntimeError):
    """Raised when a call the caller treated as mandatory did not succeed."""

    def __init__(self, message: str, result: OperationResult) -> None:
        super().__init__(message)
        self.result = result


class FineractClient:
    """Thin, instrumented Fineract API client."""

    def __init__(self, config: FineractConfig, report: RunReport) -> None:
        self.config = config
        self.report = report
        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth(config.username, config.password)
        self.session.headers.update(
            {
                "Fineract-Platform-TenantId": config.tenant_id,
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )

    # --- readiness ------------------------------------------------------

    def wait_until_ready(self, timeout_seconds: float = 900.0,
                         poll_seconds: float = 5.0) -> float:
        """Block until Fineract reports UP and its API answers an authenticated call.

        Returns how long the wait took. Raises TimeoutError if it never comes up.
        Fineract runs Liquibase migrations on first boot, so a cold start can
        legitimately take several minutes.
        """
        deadline = time.monotonic() + timeout_seconds
        started = time.monotonic()
        last_reason = "not contacted yet"

        while time.monotonic() < deadline:
            try:
                health = self.session.get(self.config.actuator_url, timeout=10)
                if health.status_code == 200 and health.json().get("status") == "UP":
                    # Health being UP does not guarantee the tenant is migrated,
                    # so confirm with a real authenticated API read.
                    probe = self.session.get(
                        f"{self.config.base_url}/offices", timeout=30
                    )
                    if probe.status_code == 200:
                        return time.monotonic() - started
                    last_reason = f"API probe returned HTTP {probe.status_code}"
                else:
                    last_reason = f"actuator health HTTP {health.status_code}"
            except (requests.RequestException, ValueError) as exc:
                last_reason = f"{type(exc).__name__}: {exc}"

            time.sleep(poll_seconds)

        raise TimeoutError(
            f"Fineract did not become ready within {timeout_seconds:.0f}s "
            f"(last check: {last_reason})"
        )

    # --- core request ---------------------------------------------------

    def call(
        self,
        method: str,
        path: str,
        *,
        category: str,
        operation: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        required: bool = False,
        expected_status: tuple[int, ...] = (200, 201),
    ) -> tuple[OperationResult, Any]:
        """Issue one Fineract call and record the observed outcome.

        Returns the recorded result plus the decoded response body (or None).
        Raises FineractError when `required` is set and the call did not succeed.
        """
        url = f"{self.config.base_url}{path}"
        started = time.perf_counter()
        started_ns = time.time_ns()
        status: int | None = None
        payload: Any = None
        error: str | None = None
        ok = False

        try:
            response = self.session.request(
                method,
                url,
                json=json_body,
                params=params,
                timeout=self.config.timeout_seconds,
            )
            status = response.status_code
            ok = status in expected_status
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if not ok:
                error = self._describe_error(response, payload)
        except requests.RequestException as exc:
            error = f"{type(exc).__name__}: {exc}"

        latency_ms = (time.perf_counter() - started) * 1000.0

        result = self.report.record(
            OperationResult(
                run_id=self.report.run_id,
                seq=self.report.next_seq(),
                timestamp=datetime.now(timezone.utc).isoformat(),
                category=category,
                operation=operation,
                method=method.upper(),
                path=path,
                http_status=status,
                ok=ok,
                latency_ms=round(latency_ms, 2),
                resource_id=self._extract_resource_id(payload),
                error=error,
                context=context or {},
            )
        )

        if _telemetry is not None:
            _telemetry.record_call(result, started_ns)

        if required and not ok:
            raise FineractError(f"{operation} failed: {error}", result)
        return result, payload

    # --- helpers --------------------------------------------------------

    @staticmethod
    def _describe_error(response: requests.Response, payload: Any) -> str:
        """Pull Fineract's structured validation message out of an error body."""
        if isinstance(payload, dict):
            errors = payload.get("errors")
            if isinstance(errors, list) and errors:
                parts = []
                for err in errors[:3]:
                    if isinstance(err, dict):
                        parts.append(
                            err.get("developerMessage")
                            or err.get("defaultUserMessage")
                            or str(err)
                        )
                if parts:
                    return " | ".join(parts)
            for key in ("developerMessage", "defaultUserMessage", "message"):
                if payload.get(key):
                    return str(payload[key])
        return (response.text or "")[:400] or f"HTTP {response.status_code}"

    @staticmethod
    def _extract_resource_id(payload: Any) -> int | None:
        """Fineract returns the affected entity id under a few different keys."""
        if not isinstance(payload, dict):
            return None
        for key in ("resourceId", "clientId", "savingsId", "loanId", "id"):
            value = payload.get(key)
            if isinstance(value, int):
                return value
        return None

    # --- convenience verbs ---------------------------------------------

    def get(self, path: str, **kwargs: Any) -> tuple[OperationResult, Any]:
        return self.call("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> tuple[OperationResult, Any]:
        return self.call("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> tuple[OperationResult, Any]:
        return self.call("PUT", path, **kwargs)
