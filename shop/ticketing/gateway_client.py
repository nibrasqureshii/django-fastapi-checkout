"""Thin client for the payment gateway API. Every call is safe to retry (idempotency keys / idempotent refunds)."""
import logging

import httpx2
from django.conf import settings

log = logging.getLogger("ticketing.gateway")


class GatewayError(Exception):
    """The gateway was unreachable or answered with an error."""


def _request(method: str, path: str, **kwargs) -> dict:
    try:
        res = httpx2.request(
            method,
            f"{settings.GATEWAY_URL}{path}",
            headers={"Authorization": f"Bearer {settings.GATEWAY_API_KEY}"},
            timeout=10.0,
            **kwargs,
        )
    except httpx2.HTTPError as exc:
        log.warning("gateway %s %s failed: %s", method, path, type(exc).__name__)
        raise GatewayError("Payment provider is unreachable") from exc
    if res.status_code >= 400:
        log.warning("gateway %s %s returned %s", method, path, res.status_code)
        raise GatewayError(f"Payment provider returned {res.status_code}")
    return res.json()


def create_charge(*, amount_cents: int, currency: str, idempotency_key: str, card: str, return_url: str, order_id: str) -> dict:
    return _request(
        "POST",
        "/v1/charges",
        json={
            "amount_cents": amount_cents,
            "currency": currency,
            "idempotency_key": idempotency_key,
            "card": card,
            "return_url": return_url,
            "metadata": {"order_id": order_id},
        },
    )


def refund_charge(charge_id: str) -> dict:
    return _request("POST", f"/v1/charges/{charge_id}/refund")
