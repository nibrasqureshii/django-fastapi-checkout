from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from ticketing.models import Event, Seat


class Command(BaseCommand):
    help = "Create a fictional demo event with 4 rows x 8 seats (idempotent)."

    @transaction.atomic
    def handle(self, *args, **options):
        event, created = Event.objects.get_or_create(
            name="Northern Lights Live",
            defaults={"venue": "Riverside Hall", "starts_at": timezone.now() + timedelta(days=30), "price_cents": 4500, "currency": "GBP"},
        )
        labels = [f"{row}{n}" for row in "ABCD" for n in range(1, 9)]
        Seat.objects.bulk_create([Seat(event=event, label=label) for label in labels], ignore_conflicts=True)
        self.stdout.write(self.style.SUCCESS(f"{'Created' if created else 'Found'} '{event.name}' with {event.seats.count()} seats."))
