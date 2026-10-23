from django.core.management.base import BaseCommand

from ticketing.services import expire_stale_orders


class Command(BaseCommand):
    help = "Release seat holds that ran out of time and void pending challenges (run every minute via cron)."

    def handle(self, *args, **options):
        count = expire_stale_orders()
        self.stdout.write(self.style.SUCCESS(f"Expired {count} order(s)."))
