from django.contrib import admin

from .models import Event, Order, Payment, Seat, WebhookEvent


class SeatInline(admin.TabularInline):
    model = Seat
    extra = 0
    fields = ["label", "status", "order"]
    readonly_fields = ["order"]


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ["name", "venue", "starts_at", "price_cents", "currency"]
    inlines = [SeatInline]


class PaymentInline(admin.StackedInline):
    model = Payment
    extra = 0
    readonly_fields = ["gateway_charge_id", "status", "card", "challenge_url", "created_at"]
    can_delete = False


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ["id", "event", "email", "status", "amount_cents", "expires_at", "created_at"]
    list_filter = ["status", "event"]
    search_fields = ["id", "email"]
    readonly_fields = ["idempotency_key", "amount_cents", "currency", "created_at", "updated_at"]
    inlines = [PaymentInline]


@admin.register(WebhookEvent)
class WebhookEventAdmin(admin.ModelAdmin):
    list_display = ["event_id", "type", "received_at"]
    list_filter = ["type"]
    readonly_fields = ["event_id", "type", "payload", "received_at"]
