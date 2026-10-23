import json
import time

import pytest
from django.urls import reverse

from ticketing.gateway_client import GatewayError
from ticketing import gateway_client
from ticketing.signing import InvalidSignature, verify

from .conftest import webhook_event

pytestmark = pytest.mark.django_db
SECRET = "test-webhook-secret"


def sign(body: bytes, ts=None) -> str:
    import hashlib
    import hmac

    ts = int(time.time()) if ts is None else ts
    return f"t={ts},v1={hmac.new(SECRET.encode(), f'{ts}.'.encode() + body, hashlib.sha256).hexdigest()}"


def create(client, event, key="api-key-0001", seats=("A1",)):
    return client.post(
        reverse("order-create"),
        data=json.dumps({"event_id": event.id, "seats": list(seats), "email": "buyer@example.com"}),
        content_type="application/json",
        headers={"Idempotency-Key": key},
    )


def test_create_requires_idempotency_key(client, event):
    res = client.post(reverse("order-create"), data="{}", content_type="application/json")
    assert res.status_code == 400


def test_create_then_retry_returns_same_order(client, event):
    first, retry = create(client, event), create(client, event)
    assert first.status_code == 201 and retry.status_code == 200
    assert first.json()["id"] == retry.json()["id"]


def test_seat_conflict_is_409(client, event):
    create(client, event, key="first-buyer-key")
    assert create(client, event, key="second-buyer-key").status_code == 409


def test_pay_and_resume_do_not_leak_charge_id(client, event, gateway):
    order_id = create(client, event).json()["id"]
    paid = client.post(reverse("order-pay", args=[order_id]), data=json.dumps({"card": "challenge"}), content_type="application/json").json()
    resumed = client.get(reverse("order-detail", args=[order_id])).json()
    assert paid["status"] == resumed["status"] == "requires_action"
    assert resumed["payment"]["challenge_url"]
    assert "ch_" not in json.dumps(resumed)


def test_gateway_outage_returns_502_and_is_retryable(client, event, monkeypatch):
    def down(**_):
        raise GatewayError("down")

    monkeypatch.setattr(gateway_client, "create_charge", down)
    order_id = create(client, event).json()["id"]
    res = client.post(reverse("order-pay", args=[order_id]), data=json.dumps({"card": "frictionless"}), content_type="application/json")
    assert res.status_code == 502


def test_webhook_rejects_bad_and_stale_signatures(client):
    body = json.dumps({"id": "evt_x", "type": "charge.succeeded", "data": {}}).encode()
    url = reverse("gateway-webhook")
    assert client.post(url, data=body, content_type="application/json", headers={"X-Gateway-Signature": "t=1,v1=bad"}).status_code == 400
    stale = sign(body, ts=int(time.time()) - 3600)
    assert client.post(url, data=body, content_type="application/json", headers={"X-Gateway-Signature": stale}).status_code == 400


def test_signed_webhook_settles_order_and_dedupes(client, event, gateway):
    order_id = create(client, event).json()["id"]
    client.post(reverse("order-pay", args=[order_id]), data=json.dumps({"card": "challenge"}), content_type="application/json")
    body = json.dumps(webhook_event("ch_1", "succeeded", order_id, event_id="evt_api")).encode()
    url = reverse("gateway-webhook")
    first = client.post(url, data=body, content_type="application/json", headers={"X-Gateway-Signature": sign(body)})
    again = client.post(url, data=body, content_type="application/json", headers={"X-Gateway-Signature": sign(body)})
    assert first.json() == {"received": True, "duplicate": False}
    assert again.json() == {"received": True, "duplicate": True}
    assert client.get(reverse("order-detail", args=[order_id])).json()["status"] == "paid"


def test_verify_helper():
    body = b"{}"
    verify(sign(body), body, SECRET, 300)
    with pytest.raises(InvalidSignature):
        verify(sign(body), b"{tampered}", SECRET, 300)
    with pytest.raises(InvalidSignature):
        verify("garbage", body, SECRET, 300)


def test_checkout_page_renders(client, event):
    res = client.get(reverse("checkout"))
    assert res.status_code == 200 and b"Test Night" in res.content


def test_checkout_page_sends_json_content_type_with_custom_headers(client, event):
    """Regression: the page's fetch helper once let custom headers overwrite the JSON content type."""
    html = client.get(reverse("checkout")).content.decode()
    assert '{ ...opts, headers: { "content-type": "application/json"' in html
