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
    ) -> None:
        self.account_id = account_id
        self.database_id = database_id
        self.api_token = api_token
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        # Injectable transport keeps the plugin testable without a network.
        self._client = httpx.Client(transport=transport, timeout=timeout)

    @property
    def _url(self) -> str:
        return (
            f"{self.api_base}/accounts/{self.account_id}"
            f"/d1/database/{self.database_id}/query"
        )

    def _run(self, sql: str, params: Sequence[Any]) -> List[dict]:
        resp = self._client.post(
            self._url,
            headers={"Authorization": f"Bearer {self.api_token}"},
            json={"sql": sql, "params": [p for p in params]},
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

    def query(self, sql: str, params: Sequence[Any] = ()) -> List[dict]:
        return [dict(r) for r in self._run(sql, params)]

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        self._run(sql, params)

    def executescript(self, statements: List[str]) -> None:
        """Run the whole schema in a single round-trip.

        D1 accepts multiple statements in one request (no bound params), which
        turns ~16 cold-start HTTP calls into one — important on serverless.
        """
        script = ";\n".join(s.strip() for s in statements if s.strip())
        if script:
            self._run(script, [])


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
