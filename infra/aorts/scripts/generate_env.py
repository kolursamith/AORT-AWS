"""Create infra/aorts/.env with fresh random secrets for the Stage 11 stack (never commit it).

Usage:  python infra/aorts/scripts/generate_env.py          # refuses to overwrite
        python infra/aorts/scripts/generate_env.py --add-missing  # keep secrets, add new keys
        python infra/aorts/scripts/generate_env.py --force  # regenerate (old secrets lost)
Secrets are written to the file only, never printed.
"""

from __future__ import annotations

import secrets
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
IDENTITIES = ["A1", "A2", "A3", "SWITCH", "B", "C", "REMITTER", "CHANNEL", "DISPATCHER",
              "CONTROLLER", "twin-probe-1", "twin-probe-2", "LAB"]  # fmt: skip


def main() -> int:
    env = HERE / ".env"
    if env.exists() and "--add-missing" in sys.argv:  # keep existing secrets, add new keys only
        present = {line.split("=", 1)[0] for line in env.read_text().splitlines() if "=" in line}
        if "AORTS_DASHBOARD_DB_PASSWORD" not in present:
            with env.open("a", encoding="utf-8") as f:
                f.write(f"AORTS_DASHBOARD_DB_PASSWORD={secrets.token_urlsafe(24)}\n")
            print(f"added AORTS_DASHBOARD_DB_PASSWORD to {env} (not printed).")
        return 0
    if env.exists() and "--force" not in sys.argv:
        print(f"{env} already exists; use --force to regenerate it.")
        return 1
    keys = ",".join(f"{i}-v1={i}:{secrets.token_hex(24)}" for i in IDENTITIES)
    lines = [
        "# Stage 11 local secrets (synthetic mock environment). Generated; do not commit.",
        f"POSTGRES_PASSWORD={secrets.token_urlsafe(18)}",
        f"AORTS_ADMIN_TOKEN={secrets.token_urlsafe(24)}",
        f"AORTS_SERVICE_KEYS={keys}",
        f"AORTS_BACKEND_TOKEN={secrets.token_urlsafe(32)}",
        f"AORTS_DASHBOARD_DB_PASSWORD={secrets.token_urlsafe(24)}",
    ]
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    env.chmod(0o600)
    print(f"wrote {env} with new random secrets (not printed).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
