"""Backend boundary: controller code imports this protocol, not backend internals."""

from typing import Protocol


class BackendAdapter(Protocol):
    async def request_status(self, transaction_id: str, route_id: str) -> dict: ...


class DummyBackendAdapter:
    async def request_status(self, transaction_id: str, route_id: str) -> dict:
        return {
            "transaction_id": transaction_id,
            "route_id": route_id,
            "outcome": "unknown",
            "authoritative": False,
            "reason": "dummy adapter cannot certify beneficiary outcome",
        }
