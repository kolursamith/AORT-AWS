"""Run provenance: run id, seed, code version, configuration - recorded with every experiment."""

from __future__ import annotations

import hashlib
import platform
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # repository root


def git_commit() -> str:
    """The Git commit if this is a Git checkout; otherwise say so explicitly (never invent one)."""
    if not (ROOT / ".git").exists():
        return "not-a-git-repository"
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607 - git from PATH, read-only
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=5,
        )
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],  # noqa: S607 - git from PATH, read-only
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return out.stdout.strip() + ("-dirty" if dirty.stdout.strip() else "")
    except (OSError, subprocess.SubprocessError):
        return "git-unavailable"


def source_sha256() -> str:
    """Hash of every Python source file under backend/aorts/ (stand-in for a commit id when there is no Git repo)."""
    h = hashlib.sha256()
    for p in sorted((ROOT / "backend" / "aorts").rglob("*.py")):
        h.update(str(p.relative_to(ROOT)).replace("\\", "/").encode())
        h.update(p.read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def provenance(experiment: str, extra: dict | None = None) -> dict:
    return {
        "run_id": f"{experiment}-{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}",
        "experiment": experiment,
        "started_at": datetime.now(UTC).isoformat(),
        "git_commit": git_commit(),
        "source_sha256": source_sha256(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        **(extra or {}),
    }
