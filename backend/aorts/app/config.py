"""Central configuration. Every value can be overridden with an ``AORTS_`` env var.

Secrets (keyring, admin token, database password) only ever come from the
environment locally, or from Secrets Manager-injected env vars on ECS.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AORTS_", env_file=".env", extra="ignore")

    # --- identity of this process -------------------------------------------------
    service_name: str = "aorts"
    environment: str = "local"  # local | test | aws
    bank_id: str = "B"  # used by the beneficiary service only

    # --- persistence ----------------------------------------------------------------
    database_url: SecretStr = SecretStr(
        "postgresql+psycopg://aorts:aorts_local_dev@127.0.0.1:5432/aorts"
    )
    db_pool_size: int = 10

    # --- service-to-service authentication -----------------------------------------
    # Format: "key_id=identity:secret,key_id=identity:secret". Example in .env.example.
    service_keys: SecretStr = SecretStr("")
    # Which key id this process uses when it signs as a given identity: "identity=key_id,..."
    signing_keys: str = ""
    max_clock_skew_seconds: int = 300
    nonce_ttl_seconds: int = 600
    max_body_bytes: int = 64 * 1024

    # --- administrative / lab controls -----------------------------------------------
    admin_token: SecretStr = SecretStr("")
    enable_attack_controls: bool = False
    enable_fault_controls: bool = False
    attack_allowed_hosts: str = (
        "localhost,127.0.0.1,switch,remitter,beneficiary-b,beneficiary-c,testserver"
    )

    # --- service URLs -------------------------------------------------------------------
    remitter_url: str = "http://127.0.0.1:8100"
    switch_url: str = "http://127.0.0.1:8110"
    beneficiary_urls: str = "B=http://127.0.0.1:8121,C=http://127.0.0.1:8122"
    control_url: str = "http://127.0.0.1:8130"

    # --- timeouts -----------------------------------------------------------------------
    remitter_switch_timeout_s: float = 2.0
    switch_beneficiary_timeout_s: float = 1.5
    status_check_timeout_s: float = 2.0

    # --- switch capacity model ---------------------------------------------------------
    switch_workers_per_destination: int = 8
    switch_queue_wait_s: float = 0.5
    switch_core_workers: int = 16
    switch_check_workers: int = (
        0  # >0 enables the status-check bulkhead (separate pool from payments)
    )
    # Fraction of each destination pool (and of a shared core pool) that status checks may never occupy.
    switch_primary_reserve_fraction: float = 0.25
    switch_base_processing_ms: int = 5

    # --- status-check admission (Security Module 1) -------------------------------------
    bank_check_rate_per_sec: float = 10.0
    bank_check_burst: float = 20.0
    route_check_rate_per_sec: float = 15.0
    route_check_burst: float = 30.0
    global_check_rate_per_sec: float = 60.0
    global_check_burst: float = 100.0
    tier2_credit_cap: float = 60.0
    tier2_credit_refill_per_sec: float = 5.0
    reconciliation_rate_per_sec: float = 1.0
    reconciliation_burst: float = 5.0
    reconciliation_interval_seconds: float = 10.0  # dispatcher pass over HANDOFF_UDIR (0 = off)

    # --- recovery / dispatcher -----------------------------------------------------------
    controller_mode: str = "mock"  # mock | http
    controller_base_url: str = "http://controller:8001"
    controller_timeout_seconds: float = 2.0
    dispatch_tick_seconds: float = 1.0
    max_inflight_checks_per_route: int = 10  # pre-dispatch gate: recovery limit per route
    dispatch_duplicate_job_prob: float = (
        0.0  # experiments: simulate at-least-once (duplicate) job delivery
    )
    fallback_budget_per_route: int = 2
    max_budget_per_route: int = 20
    max_checks_per_transaction: int = 6
    check_backoff_base_seconds: float = 1.0
    check_backoff_max_seconds: float = 30.0
    deadline_seconds: int = 120  # simulated "T+1" for experiments (NOT a real calendar day)
    auto_reverse_on_confirmed_failure: bool = True

    # --- telemetry integrity (Security Module 2) -----------------------------------------
    telemetry_sources: str = "twin-probe-1,twin-probe-2"
    telemetry_max_age_seconds: int = 30
    telemetry_max_future_seconds: int = 5
    telemetry_min_confidence: float = 0.6
    telemetry_cross_check_tolerance: float = 0.5
    telemetry_bias_check: bool = True  # CUSUM check for persistent in-tolerance optimistic bias

    # --- event / cloud adapters --------------------------------------------------------
    queue_backend: str = "local"  # local | sqs
    sqs_queue_url: str = ""
    event_sink: str = "local"  # local | eventbridge
    eventbridge_bus_name: str = "aorts-bus"
    twin_store: str = "postgres"  # postgres (local, cross-process) | memory | dynamodb
    dynamodb_table: str = "aorts-twin-state"
    aws_region: str = "ap-south-1"
    emf_enabled: bool = False
    emf_namespace: str = "AORTS"
    s3_results_bucket: str = ""

    # --- misc -------------------------------------------------------------------------
    log_level: str = "INFO"
    experiment_id: str = "trackb-local"  # contracts/v1 experiment_id for emitted messages
    random_seed: int = 42
    accounts_seed_balance_paise: int = Field(default=10_000_000_00, ge=0)

    # On ECS the RDS secret's JSON keys are injected separately (never baked into the image).
    db_host: str = ""
    db_port: int = 5432
    db_user: str = ""
    db_password: SecretStr = SecretStr("")
    db_name: str = "aorts"

    def model_post_init(self, __context) -> None:
        if self.db_host and self.db_password.get_secret_value():
            from urllib.parse import quote

            url = (
                f"postgresql+psycopg://{quote(self.db_user, safe='')}:{quote(self.db_password.get_secret_value(), safe='')}"
                f"@{self.db_host}:{self.db_port}/{self.db_name}?sslmode=require"
            )
            object.__setattr__(self, "database_url", SecretStr(url))

    # Convenience parsers ------------------------------------------------------------
    def beneficiary_url_map(self) -> dict[str, str]:
        return _parse_pairs(self.beneficiary_urls)

    def signing_key_map(self) -> dict[str, str]:
        return _parse_pairs(self.signing_keys)

    def telemetry_source_set(self) -> set[str]:
        return {s.strip() for s in self.telemetry_sources.split(",") if s.strip()}

    def attack_host_set(self) -> set[str]:
        return {s.strip() for s in self.attack_allowed_hosts.split(",") if s.strip()}


def _parse_pairs(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        key, _, value = item.partition("=")
        out[key.strip()] = value.strip()
    return out


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
