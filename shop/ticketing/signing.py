"""Verify gateway webhook signatures: `t=<unix ts>,v1=<hex HMAC-SHA256 of "<ts>.<raw body>">`."""
import hashlib
import hmac
import time


class InvalidSignature(Exception):
    pass


def verify(header: str, body: bytes, secret: str, tolerance: int, now: float | None = None) -> None:
    try:
        parts = dict(item.split("=", 1) for item in header.split(","))
        timestamp, received = int(parts["t"]), parts["v1"]
    except (KeyError, ValueError) as exc:
        raise InvalidSignature("Malformed signature header") from exc
    current = time.time() if now is None else now
    # Reject stale or future-dated events so captured webhooks can't be replayed
    if abs(current - timestamp) > tolerance:
        raise InvalidSignature("Timestamp outside tolerance")
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise InvalidSignature("Signature mismatch")
