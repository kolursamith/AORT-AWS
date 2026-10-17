"""Experiment registry: every run gets an id, a DB row (experiment_runs) and a JSON file in results/raw/."""

from __future__ import annotations

import json
import logging
import os
import platform
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from backend.aorts.banking.ledger.models import ExperimentRun

logger = logging.getLogger("backend.aorts.experiments")
RESULTS_DIR = Path(os.environ.get("AORTS_RESULTS_DIR", "results"))


def environment_info(settings) -> dict:
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "environment": settings.environment,
        "controller_mode": settings.controller_mode,
        "queue_backend": settings.queue_backend,
        "note": "synthetic mock environment; not production banking infrastructure",
    }


def save_json(subdir: str, name: str, payload: dict) -> str:
    path = RESULTS_DIR / subdir
    path.mkdir(parents=True, exist_ok=True)
    file = path / name
    # Never overwrite an earlier result: append a suffix if the name exists.
    i = 1
    while file.exists():
        file = path / f"{Path(name).stem}-{i}{Path(name).suffix}"
        i += 1
    file.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return str(file)


class ExperimentRegistry:
    def __init__(self, db, settings):
        self.db = db
        self.settings = settings
        self._threads: dict[str, threading.Thread] = {}

    def start(
        self,
        scenario: str,
        fn: Callable[[str], dict],
        *,
        config: dict,
        policy: str = "mock",
        seed: int | None = None,
        experiment_id: str | None = None,
        background: bool = True,
    ) -> str:
        exp_id = (
            experiment_id
            or f"exp-{scenario}-{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:4]}"
        )
        with self.db.session() as s:
            s.add(
                ExperimentRun(
                    experiment_id=exp_id,
                    scenario=scenario,
                    policy=policy,
                    seed=seed,
                    config=config,
                    environment=environment_info(self.settings),
                    status="RUNNING",
                )
            )

        def _run():
            status, results = "COMPLETED", {}
            try:
                results = fn(exp_id)
            except Exception as exc:  # noqa: BLE001 - record the failure, never fabricate results
                logger.exception("experiment %s failed", exp_id)
                status, results = "FAILED", {"error": f"{type(exc).__name__}: {exc}"}
            path = save_json(
                "raw",
                f"{exp_id}.json",
                {
                    "experiment_id": exp_id,
                    "scenario": scenario,
                    "status": status,
                    "config": config,
                    "results": results,
                },
            )
            with self.db.session() as s:
                row = s.get(ExperimentRun, exp_id)
                row.status = status
                row.results = {**_compact(results), "result_file": path}
                row.ended_at = datetime.now(UTC)

        if background:
            t = threading.Thread(target=_run, name=f"exp-{exp_id}", daemon=True)
            self._threads[exp_id] = t
            t.start()
        else:
            _run()
        return exp_id

    def get(self, exp_id: str) -> dict | None:
        with self.db.session() as s:
            row = s.get(ExperimentRun, exp_id)
            return _row(row) if row else None

    def list(self, limit: int = 50) -> list[dict]:
        with self.db.session() as s:
            rows = s.execute(
                select(ExperimentRun).order_by(ExperimentRun.started_at.desc()).limit(limit)
            ).scalars()
            return [_row(r) for r in rows]


def _compact(results: dict) -> dict:
    """Keep the DB row small: drop long per-request/per-tick lists (they stay in the JSON file)."""
    out = {}
    for k, v in results.items():
        if isinstance(v, list) and len(v) > 50:
            out[k] = f"<{len(v)} items in result_file>"
        elif isinstance(v, dict):
            out[k] = _compact(v)
        else:
            out[k] = v
    return out


def _row(r: ExperimentRun) -> dict:
    return {
        "experiment_id": r.experiment_id,
        "scenario": r.scenario,
        "policy": r.policy,
        "seed": r.seed,
        "status": r.status,
        "config": r.config,
        "environment": r.environment,
        "results": r.results,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "ended_at": r.ended_at.isoformat() if r.ended_at else None,
    }
