from rest_framework import serializers

from .models import Event, Order, Seat


class SeatSerializer(serializers.ModelSerializer):
    class Meta:
        model = Seat
        fields = ["label", "status"]


class EventSerializer(serializers.ModelSerializer):
    seats = SeatSerializer(many=True, read_only=True)

    class Meta:
        model = Event
        fields = ["id", "name", "venue", "starts_at", "price_cents", "currency", "seats"]


class CreateOrderSerializer(serializers.Serializer):
    event_id = serializers.IntegerField(min_value=1)
    seats = serializers.ListField(child=serializers.CharField(max_length=10), min_length=1, max_length=8)
    email = serializers.EmailField(max_length=254)


class PaySerializer(serializers.Serializer):
    card = serializers.ChoiceField(choices=["frictionless", "challenge", "decline"])


class OrderSerializer(serializers.ModelSerializer):
    seats = serializers.SlugRelatedField(many=True, read_only=True, slug_field="label")
    payment = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = ["id", "event", "status", "amount_cents", "currency", "expires_at", "seats", "payment", "created_at"]

    def get_payment(self, order: Order):
        payment = getattr(order, "payment", None)
        if payment is None:
            return None
        # Only expose what the buyer's browser needs; the charge id stays server-side
        return {"status": payment.status, "challenge_url": payment.challenge_url or None}
