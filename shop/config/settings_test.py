"""Test settings: debug-mode defaults so no real secrets are needed; everything else matches production settings."""
import os

os.environ.setdefault("DJANGO_DEBUG", "1")
os.environ.setdefault("DATABASE_URL", "postgres://localhost:5432/ticket_checkout")

from .settings import *  # noqa: E402,F403

WEBHOOK_SECRET = "test-webhook-secret"
REST_FRAMEWORK = {**REST_FRAMEWORK, "DEFAULT_THROTTLE_CLASSES": []}  # noqa: F405
