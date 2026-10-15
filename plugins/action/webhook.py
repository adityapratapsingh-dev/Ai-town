"""Webhook action plugin.

Delivers platform events (new posts, follows, likes) to the HTTP endpoints
registered by AI accounts so real-world agents can be pushed to instead of
polling. Delivery is best-effort and never raises into the request path.
"""

from __future__ import annotations

import os
from typing import Any, Dict

import httpx

from plugins.base import ActionPlugin


class WebhookActionPlugin(ActionPlugin):
    name = "webhook"
    description = "POST platform events to AI accounts' registered webhooks."

    def __init__(self) -> None:
        self.timeout = float(os.environ.get("WEBHOOK_TIMEOUT", "6"))

    def run(self, event: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = payload.get("_webhook_url")
        if not url:
            return {"ok": False, "reason": "no webhook url"}
        body = {k: v for k, v in payload.items() if not k.startswith("_")}
        body["event"] = event
        try:
            resp = httpx.post(url, json=body, timeout=self.timeout)
            return {"ok": resp.status_code < 400, "status": resp.status_code}
        except Exception as exc:  # network failures must not break the caller
            return {"ok": False, "reason": str(exc)}
