import hashlib
import hmac

import pytest
from fastapi.testclient import TestClient

from app import main
from app.signing import sign

AUTH = {"Authorization": "Bearer dev-gateway-key"}


@pytest.fixture
def sent(monkeypatch):
    """Capture webhooks instead of sending HTTP."""
    events = []
    monkeypatch.setattr(main, "webhook_sender", lambda event_type, charge: events.append((event_type, charge.id, charge.status)))
    main.store.reset()
    return events


@pytest.fixture
def client():
    return TestClient(main.app)


def charge(client, card="challenge", key="order-0001-key", **extra):
    body = {"amount_cents": 4500, "currency": "GBP", "idempotency_key": key, "card": card, "return_url": "http://shop.test/orders/1", **extra}
    return client.post("/v1/charges", json=body, headers=AUTH)


def test_requires_api_key(client, sent):
    res = client.post("/v1/charges", json={}, headers={"Authorization": "Bearer wrong"})
    assert res.status_code == 401


def test_frictionless_succeeds_and_sends_webhook(client, sent):
    res = charge(client, card="frictionless")
    assert res.json()["status"] == "succeeded"
    assert sent == [("charge.succeeded", res.json()["id"], "succeeded")]


def test_decline_fails_and_sends_webhook(client, sent):
    res = charge(client, card="decline")
    assert res.json()["status"] == "failed"
    assert sent[0][0] == "charge.failed"


def test_same_idempotency_key_returns_same_charge(client, sent):
    first = charge(client).json()
    second = charge(client).json()
    assert first["id"] == second["id"]
    assert len(main.store.charges) == 1
    assert "idempotency_key" not in first


def test_challenge_flow_pass_redirects_and_notifies(client, sent):
    created = charge(client).json()
    assert created["status"] == "requires_action" and created["challenge_url"].endswith(created["id"])
    page = client.get(f"/challenge/{created['id']}")
    assert page.status_code == 200 and "Approve" in page.text
    res = client.post(f"/challenge/{created['id']}", data={"outcome": "pass"}, follow_redirects=False)
    assert res.status_code == 303 and res.headers["location"] == "http://shop.test/orders/1"
    assert sent == [("charge.succeeded", created["id"], "succeeded")]


def test_challenge_cannot_be_completed_twice(client, sent):
    created = charge(client).json()
    client.post(f"/challenge/{created['id']}", data={"outcome": "fail"}, follow_redirects=False)
    again = client.post(f"/challenge/{created['id']}", data={"outcome": "pass"}, follow_redirects=False)
    assert again.status_code == 409
    assert sent == [("charge.failed", created["id"], "failed")]


def test_refund_captured_and_void_pending(client, sent):
    captured = charge(client, card="frictionless", key="order-0002-key").json()
    pending = charge(client, key="order-0003-key").json()
    assert client.post(f"/v1/charges/{captured['id']}/refund", headers=AUTH).json()["status"] == "refunded"
    assert client.post(f"/v1/charges/{pending['id']}/refund", headers=AUTH).json()["status"] == "voided"
    # Refunding again is a no-op, not an error
    assert client.post(f"/v1/charges/{captured['id']}/refund", headers=AUTH).json()["status"] == "refunded"


def test_validation_rejects_bad_input(client, sent):
    assert charge(client, card="stolen").status_code == 422
    assert charge(client, currency="gbp").status_code == 422


def test_signature_matches_documented_scheme():
    body = b'{"id":"evt_1"}'
    header = sign("secret", body, timestamp=1700000000)
    expected = hmac.new(b"secret", b"1700000000." + body, hashlib.sha256).hexdigest()
    assert header == f"t=1700000000,v1={expected}"
