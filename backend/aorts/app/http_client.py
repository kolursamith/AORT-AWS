"""Signed service-to-service HTTP client.

Tests inject in-process ``TestClient`` instances via ``overrides`` so the full
remitter -> switch -> beneficiary chain runs without sockets.
"""

from __future__ import annotations

import json
import threading
from urllib.parse import urlsplit

import httpx

from backend.aorts.security.request_auth import KeyRegistry, sign_request


def dumps(payload) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str).encode()


class SignedClient:
    def __init__(
        self,
        registry: KeyRegistry,
        base_urls: dict[str, str],
        overrides: dict[str, httpx.Client] | None = None,
        default_timeout: float = 2.0,
    ):
        self.registry = registry
        self.base_urls = dict(base_urls)
        self.overrides = overrides or {}
        self.default_timeout = default_timeout
        self._clients: dict[str, httpx.Client] = {}
        self._lock = threading.Lock()

    def _client(self, target: str) -> httpx.Client:
        if target in self.overrides:
            return self.overrides[target]
        with self._lock:
            if target not in self._clients:
                if target not in self.base_urls:
                    raise KeyError(f"no base url configured for '{target}'")
                self._clients[target] = httpx.Client(
                    base_url=self.base_urls[target],
                    limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
                )
            return self._clients[target]

    def request(
        self,
        target: str,
        method: str,
        path: str,
        *,
        as_identity: str,
        payload=None,
        timeout: float | None = None,
        headers: dict | None = None,
    ) -> httpx.Response:
        body = dumps(payload) if payload is not None else b""
        split = urlsplit(path)
        key = self.registry.signing_key_for(as_identity)
        signed = sign_request(key, method, split.path, body, query=split.query)
        all_headers = {"Content-Type": "application/json", **signed, **(headers or {})}
        return self._client(target).request(
            method, path, content=body, headers=all_headers, timeout=timeout or self.default_timeout
        )

    def post(
        self,
        target: str,
        path: str,
        payload,
        *,
        as_identity: str,
        timeout: float | None = None,
        headers: dict | None = None,
    ) -> httpx.Response:
        return self.request(
            target,
            "POST",
            path,
            as_identity=as_identity,
            payload=payload,
            timeout=timeout,
            headers=headers,
        )

    def get(
        self, target: str, path: str, *, as_identity: str, timeout: float | None = None
    ) -> httpx.Response:
        return self.request(target, "GET", path, as_identity=as_identity, timeout=timeout)

    def close(self) -> None:
        for c in self._clients.values():
            c.close()
