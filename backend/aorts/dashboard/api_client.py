"""Thin HTTP client for the control service, used by the Streamlit dashboard."""

from __future__ import annotations

import httpx


class ControlClient:
    def __init__(self, base_url: str, admin_token: str | None = None, timeout: float = 10.0):
        self.http = httpx.Client(base_url=base_url, timeout=timeout)
        self.admin = {"X-AORTS-Admin-Token": admin_token} if admin_token else {}

    def _get(self, path: str, admin: bool = False, **params):
        r = self.http.get(path, params=params, headers=self.admin if admin else None)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict | None = None):
        r = self.http.post(path, json=body or {}, headers=self.admin)
        try:
            data = r.json()
        except ValueError:
            data = {"text": r.text}
        return r.status_code, data

    # reads
    def health(self):
        return self._get("/api/v1/health")

    def summary(self):
        return self._get("/api/v1/metrics/summary")

    def timeseries(self, seconds: int = 180):
        return self._get("/api/v1/metrics/timeseries", seconds=seconds)

    def decisions(self, limit: int = 30):
        return self._get("/api/v1/decisions", limit=limit)["items"]

    def security_events(self, limit: int = 100):
        return self._get("/api/v1/security/events", limit=limit)["items"]

    def audit(self, limit: int = 100):
        return self._get("/api/v1/audit", admin=True, limit=limit)["items"]

    def verify_audit(self):
        return self._get("/api/v1/audit/verify", admin=True)

    def experiments(self):
        return self._get("/api/v1/admin/experiments", admin=True)["items"]

    def experiment(self, exp_id: str):
        return self._get(f"/api/v1/admin/experiments/{exp_id}", admin=True)

    def faults(self):
        return self._get("/api/v1/admin/faults", admin=True)

    def export_csv(self) -> str:
        r = self.http.get("/api/v1/admin/export", params={"fmt": "csv"}, headers=self.admin)
        r.raise_for_status()
        return r.text

    # controls (admin + lab flags)
    def set_fault(self, cfg: dict):
        return self._post("/api/v1/admin/faults", cfg)

    def reset_faults(self):
        return self._post("/api/v1/admin/faults/reset")

    def set_defenses(self, cfg: dict):
        return self._post("/api/v1/admin/defenses", cfg)

    def start_traffic(self, cfg: dict):
        return self._post("/api/v1/admin/traffic/start", cfg)

    def stop_traffic(self):
        return self._post("/api/v1/admin/traffic/stop")

    def run_attack(self, kind: str, body: dict):
        return self._post(f"/api/v1/security/attacks/{kind}/run", body)

    def export_json(self):
        return self._post_get("/api/v1/admin/export")

    def _post_get(self, path):
        r = self.http.get(path, headers=self.admin)
        return r.status_code, r.json()

    def twins(self, max_txns: int = 100):
        return self._get("/api/v1/twins", max_txns=max_txns)

    def strategy_summary(self):
        r = self.http.get("/api/v1/experiments/strategy-summary")
        return r.json() if r.status_code == 200 else None

    def reconcile_escalated(self):
        return self._post("/api/v1/admin/reconcile-escalated")

    def reset(self, phrase: str):
        return self._post("/api/v1/admin/reset", {"confirm": phrase})
