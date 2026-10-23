import uuid

from django.db import models


class Event(models.Model):
    name = models.CharField(max_length=200)
    venue = models.CharField(max_length=200)
    starts_at = models.DateTimeField()
    price_cents = models.PositiveIntegerField()
    currency = models.CharField(max_length=3, default="GBP")

    def __str__(self) -> str:
        return self.name


class Order(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending payment"
        REQUIRES_ACTION = "requires_action", "Awaiting 3DS challenge"
        PAID = "paid", "Paid"
        FAILED = "failed", "Payment failed"
        EXPIRED = "expired", "Hold expired"

    OPEN = (Status.PENDING, Status.REQUIRES_ACTION)

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event = models.ForeignKey(Event, on_delete=models.PROTECT, related_name="orders")
    email = models.EmailField()
    # Client-supplied key: retrying the same checkout request returns the same order
    idempotency_key = models.CharField(max_length=100, unique=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True)
    amount_cents = models.PositiveIntegerField()
    currency = models.CharField(max_length=3)
    expires_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Order {self.id} ({self.status})"


class Seat(models.Model):
    class Status(models.TextChoices):
        AVAILABLE = "available"
        HELD = "held"
        SOLD = "sold"

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="seats")
    label = models.CharField(max_length=10)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.AVAILABLE)
    order = models.ForeignKey(Order, null=True, blank=True, on_delete=models.SET_NULL, related_name="seats")

    class Meta:
        ordering = ["label"]
        constraints = [models.UniqueConstraint(fields=["event", "label"], name="unique_seat_per_event")]

    def __str__(self) -> str:
        return f"{self.event_id}:{self.label}"


class Payment(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending"
        REQUIRES_ACTION = "requires_action"
        SUCCEEDED = "succeeded"
        FAILED = "failed"
        REFUNDED = "refunded"
        VOIDED = "voided"

    # One payment per order, enforced by the database: the double-charge guard
    order = models.OneToOneField(Order, on_delete=models.CASCADE, related_name="payment")
    gateway_charge_id = models.CharField(max_length=64, unique=True, null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    card = models.CharField(max_length=20)
    challenge_url = models.URLField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"Payment {self.gateway_charge_id or 'pending'} ({self.status})"


class WebhookEvent(models.Model):
    """Every processed gateway event, keyed by its id, so duplicate deliveries are ignored."""

    event_id = models.CharField(max_length=64, unique=True)
    type = models.CharField(max_length=40)
    payload = models.JSONField()
    received_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.type} {self.event_id}"
