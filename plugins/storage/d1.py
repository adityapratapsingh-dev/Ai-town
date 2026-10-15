"""Cloudflare D1 storage plugin.

D1 is a serverless SQLite that speaks the same SQL as our local backend, so we
reuse ``SQLStoragePlugin`` and only implement the transport: Cloudflare's
database HTTP API.

Required environment variables:

* ``CLOUDFLARE_ACCOUNT_ID``
* ``CLOUDFLARE_D1_DATABASE_ID``
* ``CLOUDFLARE_API_TOKEN``   (needs the *D1 Edit* permission)

Optional:
* ``CLOUDFLARE_API_BASE`` (default ``https://api.cloudflare.com/client/v4``)

Because D1 is a network service, every query is an HTTP round-trip; the
statements issued by the engine are small and indexed.
"""

from __future__ import annotations

import os
import time
from typing import Any, List, Sequence

import httpx

from plugins.storage.sql_base import SQLStoragePlugin, _Exec

DEFAULT_API_BASE = "https://api.cloudflare.com/client/v4"


class _D1Exec(_Exec):
    def __init__(
        self,
        account_id: str,
        database_id: str,
        api_token: str,
        api_base: str = DEFAULT_API_BASE,
        timeout: float = 20.0,
        transport: "httpx.BaseTransport | None" = None,
        max_retries: int = 2,
        retry_backoff: float = 0.25,
    ) -> None:
        self.account_id = account_id
        self.database_id = database_id
        self.api_token = api_token
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self.retry_backoff = retry_backoff
        # A keep-alive pool reuses the TLS connection across queries — the
        # difference between ~5ms and a fresh handshake on every round-trip.
        limits = httpx.Limits(max_keepalive_connections=8, max_connections=16)
        # Injectable transport keeps the plugin testable without a network.
        self._client = httpx.Client(
            transport=transport, timeout=timeout, limits=limits
        )

    @property
    def _url(self) -> str:
        return (
            f"{self.api_base}/accounts/{self.account_id}"
            f"/d1/database/{self.database_id}/query"
        )

    def _run(self, sql: str, params: Sequence[Any]) -> List[dict]:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._client.post(
                    self._url,
                    headers={"Authorization": f"Bearer {self.api_token}"},
                    json={"sql": sql, "params": [p for p in params]},
                )
                # Retry transient server-side failures; never retry 4xx (bad
                # credentials / bad SQL won't get better on a second try).
                if resp.status_code >= 500 or resp.status_code == 429:
                    raise httpx.HTTPStatusError(
                        f"transient {resp.status_code}", request=resp.request, response=resp
                    )
                resp.raise_for_status()
                body = resp.json()
                if not body.get("success", False):
                    raise RuntimeError(f"D1 query failed: {body.get('errors')}")
                results = body.get("result") or []
                if not results:
                    return []
                first = results[0]
                if first.get("success") is False:
                    raise RuntimeError(f"D1 statement failed: {first.get('results')}")
                return first.get("results") or []
            except (httpx.TransportError, httpx.HTTPStatusError, ValueError) as exc:
                # ValueError covers a malformed/empty JSON body from a proxy.
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status is not None and 400 <= status < 500 and status != 429:
                    raise
                last_exc = exc
                if attempt < self.max_retries:
                    time.sleep(self.retry_backoff * (2 ** attempt))
                    continue
                raise
        if last_exc:  # pragma: no cover - defensive
            raise last_exc
        return []

    def query(self, sql: str, params: Sequence[Any] = ()) -> List[dict]:
        return [dict(r) for r in self._run(sql, params)]

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        self._run(sql, params)

    def executescript(self, statements: List[str]) -> None:
        """Create the schema.

        D1 *usually* accepts several statements in one request, but it is known
        to reject multi-statement payloads in some cases (error 7500: "A
        prepared SQL statement must contain only one statement"). A schema
        failure here is fatal — the caller would fall back to ephemeral SQLite
        and silently lose every write — so try the fast single-request path and,
        on any failure, fall back to executing the statements one by one.
        """
        script = ";\n".join(s.strip() for s in statements if s.strip())
        if not script:
            return
        try:
            self._run(script, [])
            return
        except Exception:
            # Multi-statement rejected: run each statement on its own.
            for stmt in statements:
                if stmt.strip():
                    self._run(stmt, [])


class D1StoragePlugin(SQLStoragePlugin):
    name = "d1"
    description = "Cloudflare D1 (serverless SQLite) over the database HTTP API."

    def __init__(
        self,
        account_id: str | None = None,
        database_id: str | None = None,
        api_token: str | None = None,
        transport: "httpx.BaseTransport | None" = None,
    ) -> None:
        super().__init__()
        self.account_id = account_id or os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
        self.database_id = database_id or os.environ.get(
            "CLOUDFLARE_D1_DATABASE_ID", ""
        )
        self.api_token = api_token or os.environ.get("CLOUDFLARE_API_TOKEN", "")
        self.api_base = os.environ.get("CLOUDFLARE_API_BASE", DEFAULT_API_BASE)
        self.transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.account_id and self.database_id and self.api_token)

    def _connect(self) -> _Exec:
        if not self.configured:
            raise RuntimeError(
                "D1 storage selected but CLOUDFLARE_ACCOUNT_ID / "
                "CLOUDFLARE_D1_DATABASE_ID / CLOUDFLARE_API_TOKEN are not all set"
            )
        return _D1Exec(
            self.account_id,
            self.database_id,
            self.api_token,
            self.api_base,
            transport=self.transport,
        )

    def health(self) -> dict:
        return {
            **super().health(),
            "configured": self.configured,
            "database_id": self.database_id or None,
            "healthy": self.configured,
        }
