"""Create a local .env with freshly generated random secrets (never commit it).

Usage:  python infra/trackb/scripts/generate_env.py            # refuses to overwrite an existing .env
        python infra/trackb/scripts/generate_env.py --force    # regenerate (old secrets are lost)
"""

from __future__ import annotations

import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # repository root
IDENTITIES = [
    "A1",
    "A2",
    "A3",
    "SWITCH",
    "B",
    "C",
    "REMITTER",
    "CHANNEL",
    "DISPATCHER",
    "CONTROLLER",
    "twin-probe-1",
    "twin-probe-2",
    "LAB",
]


def main() -> int:
    env = ROOT / ".env"
    if env.exists() and "--force" not in sys.argv:
        print(".env already exists; use --force to regenerate it.")
        return 1
    db_password = secrets.token_urlsafe(18)
    keys = ",".join(f"{i}-v1={i}:{secrets.token_hex(24)}" for i in IDENTITIES)
    template = (ROOT / "infra" / "trackb" / "trackb.env.example").read_text(encoding="utf-8")
    out = []
    for line in template.splitlines():
        if line.startswith("POSTGRES_PASSWORD="):
            line = f"POSTGRES_PASSWORD={db_password}"
        elif line.startswith("AORTS_DATABASE_URL="):
            line = (
                f"AORTS_DATABASE_URL=postgresql+psycopg://aorts:{db_password}@127.0.0.1:55432/aorts"
            )
        elif line.startswith("AORTS_ADMIN_TOKEN="):
            line = f"AORTS_ADMIN_TOKEN={secrets.token_urlsafe(24)}"
        elif line.startswith("AORTS_SERVICE_KEYS="):
            line = f"AORTS_SERVICE_KEYS={keys}"
        out.append(line)
    env.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"wrote {env} with new random secrets (not printed).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
