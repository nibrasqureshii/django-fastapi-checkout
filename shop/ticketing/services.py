"""Checkout domain logic. Views stay thin; every money-moving rule lives (and is tested) here.

Guarantees:
- Retrying "create order" with the same idempotency key returns the same order.
- Seats are locked row by row, so two buyers can never hold the same seat.
- An order has at most one payment (DB-enforced), so pressing Pay twice never charges twice.
- Webhooks are applied once per event id, in any order, any number of times.
- A payment that succeeds after its hold expired is refunded, never double-sold.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from . import gateway_client
from .models import Event, Order, Payment, Seat, WebhookEvent

log = logging.getLogger("ticketing")


class CheckoutError(Exception):
    """A business rule rejected the request (shown to the buyer)."""


@transaction.atomic
def create_order(*, event_id: int, seat_labels: list[str], email: str, idempotency_key: str) -> tuple[Order, bool]:
    """Hold seats and open an order. Returns (order, created)."""
    existing = Order.objects.filter(idempotency_key=idempotency_key).first()
    if existing:
        return existing, False

    labels = sorted(set(seat_labels))
    if not labels or len(labels) > 8:
        raise CheckoutError("Choose between 1 and 8 seats.")
    event = Event.objects.get(pk=event_id)
    # Lock in a stable order (by label) so concurrent checkouts can't deadlock
    seats = list(Seat.objects.select_for_update().filter(event=event, label__in=labels).order_by("label"))
    if len(seats) != len(labels):
        raise CheckoutError("One or more seats do not exist.")
    taken = [s.label for s in seats if s.status != Seat.Status.AVAILABLE]
    if taken:
        raise CheckoutError(f"Already taken: {', '.join(taken)}.")

    order = Order.objects.create(
        event=event,
        email=email,
        idempotency_key=idempotency_key,
        amount_cents=event.price_cents * len(seats),
        currency=event.currency,
        expires_at=timezone.now() + timedelta(minutes=settings.HOLD_MINUTES),
    )
    Seat.objects.filter(pk__in=[s.pk for s in seats]).update(status=Seat.Status.HELD, order=order)
    log.info("order %s opened with %d seats", order.id, len(seats))
    return order, True


def start_payment(order_id, card: str) -> Order:
    """Create (or reuse) the order's single payment and ask the gateway to charge it."""
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order_id)
        if order.status not in Order.OPEN:
            raise CheckoutError(f"This order is {order.get_status_display().lower()}.")
        if order.expires_at <= timezone.now():
            raise CheckoutError("Your seat hold has expired. Please start again.")
        try:
            payment, created = Payment.objects.get_or_create(order=order, defaults={"card": card})
        except IntegrityError:  # a concurrent request created it first
            payment, created = Payment.objects.get(order=order), False
    if not created and payment.gateway_charge_id:
        # Second press of "Pay" (or a refresh): resume, never charge again
        return order

    # Outside the transaction: never hold row locks across a network call.
    # The idempotency key makes a retried call return the same charge.
    charge = gateway_client.create_charge(
        amount_cents=order.amount_cents,
        currency=order.currency,
        idempotency_key=f"order-{order.id}",
        card=payment.card,
        return_url=f"{settings.SITE_URL}/orders/{order.id}/",
        order_id=str(order.id),
    )
    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=payment.pk)
        payment.gateway_charge_id = charge["id"]
        if payment.status == Payment.Status.PENDING:  # a webhook may already have settled it
            payment.status = charge["status"]
            payment.challenge_url = charge.get("challenge_url") or ""
        payment.save()
        order = Order.objects.select_for_update().get(pk=order.pk)
        _apply_charge_status(order, payment, charge["status"])
    return order


def _apply_charge_status(order: Order, payment: Payment, status: str) -> None:
    """Move order and seats to match a charge status. Call inside a transaction with rows locked."""
    if status == "requires_action" and order.status == Order.Status.PENDING:
        order.status = Order.Status.REQUIRES_ACTION
        order.save(update_fields=["status", "updated_at"])
    elif status == "succeeded":
        if order.status in Order.OPEN:
            order.status = Order.Status.PAID
            order.save(update_fields=["status", "updated_at"])
            order.seats.update(status=Seat.Status.SOLD)
            log.info("order %s paid", order.id)
        elif order.status in (Order.Status.EXPIRED, Order.Status.FAILED):
            # Money arrived after we released the seats: give it back
            transaction.on_commit(lambda: _refund(payment.pk))
    elif status == "failed" and order.status in Order.OPEN:
        order.status = Order.Status.FAILED
        order.save(update_fields=["status", "updated_at"])
        _release_seats(order)


def _release_seats(order: Order) -> None:
    order.seats.update(status=Seat.Status.AVAILABLE, order=None)


def _refund(payment_pk: int) -> None:
    payment = Payment.objects.get(pk=payment_pk)
    if payment.gateway_charge_id and payment.status not in (Payment.Status.REFUNDED, Payment.Status.VOIDED):
        result = gateway_client.refund_charge(payment.gateway_charge_id)
        Payment.objects.filter(pk=payment_pk).update(status=result["status"])
        log.info("payment %s %s after order closed", payment.gateway_charge_id, result["status"])


def handle_webhook(event: dict) -> bool:
    """Apply a verified gateway event once. Returns False for duplicates."""
    charge = event.get("data", {})
    with transaction.atomic():
        try:
            with transaction.atomic():  # savepoint: a duplicate insert must not poison the outer transaction
                WebhookEvent.objects.create(event_id=event["id"], type=event["type"], payload=event)
        except IntegrityError:
            log.info("duplicate webhook %s ignored", event["id"])
            return False
        payment = Payment.objects.select_for_update().filter(gateway_charge_id=charge.get("id")).first()
        if payment is None:
            # The webhook can beat our own write of the charge id; fall back to the order in metadata
            order_id = (charge.get("metadata") or {}).get("order_id")
            payment = Payment.objects.select_for_update().filter(order_id=order_id).first()
            if payment is None:
                log.warning("webhook %s for unknown charge", event["id"])
                return True
            payment.gateway_charge_id = charge["id"]
        status = charge.get("status")
        if status in ("succeeded", "failed", "refunded"):
            payment.status = status
        payment.save()
        order = Order.objects.select_for_update().get(pk=payment.order_id)
        _apply_charge_status(order, payment, status)
    return True


def expire_stale_orders(now=None) -> int:
    """Release holds that ran out of time and void any challenge still pending at the gateway."""
    now = now or timezone.now()
    expired = 0
    with transaction.atomic():
        stale = Order.objects.select_for_update(skip_locked=True).filter(status__in=Order.OPEN, expires_at__lt=now)
        for order in stale:
            order.status = Order.Status.EXPIRED
            order.save(update_fields=["status", "updated_at"])
            _release_seats(order)
            payment = Payment.objects.filter(order=order).first()
            if payment and payment.status in (Payment.Status.REQUIRES_ACTION, Payment.Status.PENDING):
                transaction.on_commit(lambda pk=payment.pk: _refund(pk))
            expired += 1
    if expired:
        log.info("expired %d stale orders", expired)
    return expired
