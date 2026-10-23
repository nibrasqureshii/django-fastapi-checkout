from datetime import timedelta

import pytest
from django.utils import timezone

from ticketing import services
from ticketing.models import Order, Payment, Seat

from .conftest import webhook_event

pytestmark = pytest.mark.django_db


def open_order(event, seats=("A1", "A2"), key="key-0001-abcd"):
    order, _ = services.create_order(event_id=event.id, seat_labels=list(seats), email="buyer@example.com", idempotency_key=key)
    return order


def seat_status(event, label):
    return Seat.objects.get(event=event, label=label).status


def test_create_order_holds_seats_and_prices_them(event):
    order = open_order(event)
    assert order.amount_cents == 9000
    assert {s.label for s in order.seats.all()} == {"A1", "A2"}
    assert seat_status(event, "A1") == "held"


def test_same_idempotency_key_returns_same_order(event):
    first = open_order(event)
    second, created = services.create_order(event_id=event.id, seat_labels=["A5"], email="x@example.com", idempotency_key="key-0001-abcd")
    assert created is False and second.pk == first.pk
    assert Order.objects.count() == 1


def test_two_buyers_cannot_hold_the_same_seat(event):
    open_order(event, seats=("A3",), key="buyer-one-key")
    with pytest.raises(services.CheckoutError, match="Already taken: A3"):
        open_order(event, seats=("A3", "A4"), key="buyer-two-key")
    assert seat_status(event, "A4") == "available"  # nothing partially held


def test_frictionless_payment_marks_order_paid_and_seats_sold(event, gateway):
    order = services.start_payment(open_order(event).pk, "frictionless")
    assert order.status == "paid"
    assert seat_status(event, "A1") == "sold"


def test_challenge_waits_for_cardholder(event, gateway):
    order = services.start_payment(open_order(event).pk, "challenge")
    assert order.status == "requires_action"
    assert order.payment.challenge_url.startswith("http://gw.test/challenge/")


def test_pressing_pay_twice_never_charges_twice(event, gateway):
    order = open_order(event)
    services.start_payment(order.pk, "challenge")
    services.start_payment(order.pk, "challenge")
    services.start_payment(order.pk, "frictionless")  # even a different card reuses the same payment
    assert len(gateway.charges) == 1
    assert Payment.objects.count() == 1


def test_decline_releases_seats(event, gateway):
    order = services.start_payment(open_order(event).pk, "decline")
    assert order.status == "failed"
    assert seat_status(event, "A1") == "available"


def test_webhook_completes_challenge(event, gateway):
    order = services.start_payment(open_order(event).pk, "challenge")
    assert services.handle_webhook(webhook_event(order.payment.gateway_charge_id, "succeeded", order.pk))
    order.refresh_from_db()
    assert order.status == "paid" and seat_status(event, "A2") == "sold"


def test_duplicate_webhook_is_ignored(event, gateway):
    order = services.start_payment(open_order(event).pk, "challenge")
    evt = webhook_event(order.payment.gateway_charge_id, "succeeded", order.pk)
    assert services.handle_webhook(evt) is True
    assert services.handle_webhook(evt) is False


def test_webhook_arriving_before_charge_id_is_saved(event):
    """Gateway can call back before our own write lands; we match on the order id in metadata."""
    order = open_order(event)
    Payment.objects.create(order=order, card="frictionless")
    services.handle_webhook(webhook_event("ch_early", "succeeded", order.pk))
    order.refresh_from_db()
    assert order.status == "paid"
    assert order.payment.gateway_charge_id == "ch_early"


def test_expired_hold_releases_seats_and_voids_challenge(event, gateway, django_capture_on_commit_callbacks):
    order = services.start_payment(open_order(event).pk, "challenge")
    with django_capture_on_commit_callbacks(execute=True):
        assert services.expire_stale_orders(now=timezone.now() + timedelta(hours=1)) == 1
    order.refresh_from_db()
    assert order.status == "expired"
    assert seat_status(event, "A1") == "available"
    assert gateway.refunds == [order.payment.gateway_charge_id]


def test_success_after_expiry_is_refunded_not_sold(event, gateway, django_capture_on_commit_callbacks):
    order = services.start_payment(open_order(event).pk, "challenge")
    charge_id = order.payment.gateway_charge_id
    Order.objects.filter(pk=order.pk).update(status="expired")
    Seat.objects.filter(order=order).update(status="available", order=None)
    with django_capture_on_commit_callbacks(execute=True):
        services.handle_webhook(webhook_event(charge_id, "succeeded", order.pk))
    order.refresh_from_db()
    assert order.status == "expired"
    assert gateway.refunds == [charge_id]


def test_cannot_pay_after_hold_expires(event, gateway):
    order = open_order(event)
    Order.objects.filter(pk=order.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
    with pytest.raises(services.CheckoutError, match="expired"):
        services.start_payment(order.pk, "frictionless")
    assert gateway.charges == []
