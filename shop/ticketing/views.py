import json
import logging

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from . import services
from .gateway_client import GatewayError
from .models import Event, Order
from .serializers import CreateOrderSerializer, EventSerializer, OrderSerializer, PaySerializer
from .signing import InvalidSignature, verify

log = logging.getLogger("ticketing")


@api_view(["GET"])
def event_detail(request: Request, event_id: int) -> Response:
    event = get_object_or_404(Event.objects.prefetch_related("seats"), pk=event_id)
    return Response(EventSerializer(event).data)


@api_view(["POST"])
def create_order(request: Request) -> Response:
    key = request.headers.get("Idempotency-Key", "")
    if not 8 <= len(key) <= 100:
        return Response({"error": "An Idempotency-Key header (8-100 chars) is required."}, status=status.HTTP_400_BAD_REQUEST)
    body = CreateOrderSerializer(data=request.data)
    body.is_valid(raise_exception=True)
    try:
        order, created = services.create_order(
            event_id=body.validated_data["event_id"],
            seat_labels=body.validated_data["seats"],
            email=body.validated_data["email"],
            idempotency_key=key,
        )
    except Event.DoesNotExist:
        return Response({"error": "Event not found."}, status=status.HTTP_404_NOT_FOUND)
    except services.CheckoutError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_409_CONFLICT)
    return Response(OrderSerializer(order).data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


@api_view(["GET"])
def order_detail(request: Request, order_id) -> Response:
    """Resumable: a refreshed tab reads the current state (and challenge URL) from here."""
    order = get_object_or_404(Order.objects.select_related("payment").prefetch_related("seats"), pk=order_id)
    return Response(OrderSerializer(order).data)


@api_view(["POST"])
def pay(request: Request, order_id) -> Response:
    body = PaySerializer(data=request.data)
    body.is_valid(raise_exception=True)
    get_object_or_404(Order, pk=order_id)
    try:
        order = services.start_payment(order_id, body.validated_data["card"])
    except services.CheckoutError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_409_CONFLICT)
    except GatewayError:
        # Safe for the buyer to retry: the gateway call is idempotent
        return Response({"error": "Payment provider is unavailable. Please try again."}, status=status.HTTP_502_BAD_GATEWAY)
    order = Order.objects.select_related("payment").prefetch_related("seats").get(pk=order.pk)
    return Response(OrderSerializer(order).data)


@csrf_exempt  # authenticated by HMAC signature instead of a session
@require_POST
def gateway_webhook(request: HttpRequest) -> HttpResponse:
    try:
        verify(
            request.headers.get("X-Gateway-Signature", ""),
            request.body,
            settings.WEBHOOK_SECRET,
            settings.WEBHOOK_TOLERANCE_SECONDS,
        )
        event = json.loads(request.body)
    except (InvalidSignature, ValueError) as exc:
        log.warning("rejected webhook: %s", exc)
        return JsonResponse({"error": "invalid signature"}, status=400)
    applied = services.handle_webhook(event)
    return JsonResponse({"received": True, "duplicate": not applied})


def checkout_page(request: HttpRequest, order_id=None) -> HttpResponse:
    """Server-rendered demo UI; all state changes go through the JSON API above."""
    event = Event.objects.order_by("starts_at").first()
    return render(request, "ticketing/checkout.html", {"event": event, "order_id": order_id or ""})
