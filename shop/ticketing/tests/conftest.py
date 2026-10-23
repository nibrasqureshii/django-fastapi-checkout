import itertools
from datetime import timedelta

import pytest
from django.utils import timezone

from ticketing import gateway_client
from ticketing.models import Event, Seat


@pytest.fixture
def event(db):
    ev = Event.objects.create(name="Test Night", venue="Hall", starts_at=timezone.now() + timedelta(days=7), price_cents=4500, currency="GBP")
    Seat.objects.bulk_create([Seat(event=ev, label=f"A{i}") for i in range(1, 9)])
    return ev


class FakeGateway:
    """Records calls and mimics the simulator's idempotency (same key -> same charge)."""

    def __init__(self):
        self.charges, self.refunds, self.by_key = [], [], {}
        self._ids = itertools.count(1)

    def create_charge(self, *, amount_cents, currency, idempotency_key, card, return_url, order_id):
        if idempotency_key in self.by_key:
            return self.by_key[idempotency_key]
        status = {"frictionless": "succeeded", "challenge": "requires_action", "decline": "failed"}[card]
        charge = {"id": f"ch_{next(self._ids)}", "status": status, "challenge_url": f"http://gw.test/challenge/{len(self.charges) + 1}" if status == "requires_action" else None}
        self.charges.append({"key": idempotency_key, "amount": amount_cents, "card": card, "order_id": order_id})
        self.by_key[idempotency_key] = charge
        return charge

    def refund_charge(self, charge_id):
        self.refunds.append(charge_id)
        return {"id": charge_id, "status": "voided"}


@pytest.fixture
def gateway(monkeypatch):
    fake = FakeGateway()
    monkeypatch.setattr(gateway_client, "create_charge", fake.create_charge)
    monkeypatch.setattr(gateway_client, "refund_charge", fake.refund_charge)
    return fake


def webhook_event(charge_id, status, order_id, event_id="evt_1"):
    return {"id": event_id, "type": f"charge.{status}", "data": {"id": charge_id, "status": status, "metadata": {"order_id": str(order_id)}}}
