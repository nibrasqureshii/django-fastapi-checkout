"""Webhook signatures: HMAC-SHA256 over "<timestamp>.<raw body>", sent as `t=<ts>,v1=<hex>`.

The timestamp is signed too, so a captured webhook cannot be replayed later (the receiver
rejects anything outside its tolerance window). The Django side has a matching verifier.
"""
import hashlib
import hmac
import time


def sign(secret: str, body: bytes, timestamp: int | None = None) -> str:
    ts = int(time.time()) if timestamp is None else timestamp
    digest = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={digest}"
