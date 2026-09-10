"""Configuration for the workload generator, sourced from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# banking/workload/aort_workload/config.py -> banking/
BANKING_DIR = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = BANKING_DIR / ".env"


@dataclass(frozen=True)
class FineractConfig:
    """Connection settings for the target Fineract instance."""

    base_url: str
    tenant_id: str
    username: str
    password: str
    verify_tls: bool = False
    timeout_seconds: float = 60.0

    @property
    def actuator_url(self) -> str:
        """Health endpoint, derived from the API base URL.

        The API lives at <context-path>/api/v1 while actuator sits directly
        under the context path, so two path segments are trimmed.
        """
        root = self.base_url.rstrip("/")
        for suffix in ("/api/v1", "/api/v1/"):
            if root.endswith(suffix.rstrip("/")):
                root = root[: -len(suffix.rstrip("/"))]
                break
        return f"{root}/actuator/health"


def load_config(env_file: Path | None = None) -> FineractConfig:
    """Read Fineract settings from banking/.env, falling back to the process env."""
    env_path = env_file or DEFAULT_ENV_FILE
    if env_path.exists():
        load_dotenv(env_path, override=False)

    return FineractConfig(
        base_url=os.getenv(
            "FINERACT_BASE_URL", "http://localhost:8080/fineract-provider/api/v1"
        ),
        tenant_id=os.getenv("FINERACT_TENANT_ID", "default"),
        username=os.getenv("FINERACT_USERNAME", "mifos"),
        password=os.getenv("FINERACT_PASSWORD", "password"),
        timeout_seconds=float(os.getenv("FINERACT_TIMEOUT_SECONDS", "60")),
    )


# Fineract requires an explicit date format and locale on every date-bearing
# request body. Keeping them in one place avoids per-call drift.
DATE_FORMAT = "dd MMMM yyyy"
LOCALE = "en"
CURRENCY_CODE = os.getenv("AORT_CURRENCY_CODE", "INR")
