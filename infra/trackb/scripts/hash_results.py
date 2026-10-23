"""Generate docs/track-b-technical-evidence/04-result-files-and-hashes.md from the files actually present.

    python infra/trackb/scripts/hash_results.py

Lists every current (non-superseded) result file with its SHA-256 and size, plus the superseded
files (kept for transparency), and the source-tree hash of the code that produced this listing.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # repository root
sys.path.insert(0, str(ROOT))

from backend.aorts.experiments.provenance import (  # noqa: E402
    file_sha256,
    git_commit,
    source_sha256,
)


def rows(base: Path, superseded: bool) -> list[str]:
    out = []
    for p in sorted(base.rglob("*")):
        if not p.is_file() or p.name == ".gitkeep" or (("superseded" in p.parts) != superseded):
            continue
        rel = p.relative_to(ROOT).as_posix()
        out.append(f"| `{rel}` | {p.stat().st_size:,} | `{file_sha256(p)}` |")
    return out


def main() -> None:
    res = ROOT / "results"
    lines = [
        "# 04: Result files and hashes",
        "",
        f"Generated {datetime.now(UTC).isoformat()} by `infra/trackb/scripts/hash_results.py`.",
        f"Code version: git commit `{git_commit()}`; source-tree SHA-256 `{source_sha256()}`.",
        "",
        "Verify a file with: `certutil -hashfile <file> SHA256` (Windows) or `sha256sum <file>`.",
        "",
        "## Current result files (used in 03-experimental-evidence.md and RESULTS.md)",
        "",
        "| File | Bytes | SHA-256 |",
        "|---|---|---|",
        *rows(res, superseded=False),
        "",
        "## Superseded result files (kept for transparency; see results/raw/superseded/NOTE.md; do not use)",
        "",
        "| File | Bytes | SHA-256 |",
        "|---|---|---|",
        *rows(res, superseded=True),
        "",
    ]
    out = ROOT / "docs" / "track-b-technical-evidence" / "04-result-files-and-hashes.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
