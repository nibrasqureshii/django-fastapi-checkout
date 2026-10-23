"""Payment gateway simulator (FastAPI).

Plays the role of a card processor with 3D Secure: charges can succeed frictionlessly, require a
cardholder challenge, or decline. Results are reported to the merchant with signed webhooks,
retried with backoff, exactly like a real processor would. State is in memory: this is a
simulator for demos and tests, not a payment system.

Test cards (the `card` field): "frictionless", "challenge", "decline".
"""
from __future__ import annotations

import html
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Literal

import httpx2
from fastapi import BackgroundTasks, Depends, FastAPI, Form, Header, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field, HttpUrl

from .signing import sign

log = logging.getLogger("gateway")

API_KEY = os.environ.get("GATEWAY_API_KEY", "dev-gateway-key")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "dev-webhook-secret")
WEBHOOK_URL = os.environ.get("MERCHANT_WEBHOOK_URL", "http://127.0.0.1:8000/api/webhooks/gateway/")
PUBLIC_URL = os.environ.get("GATEWAY_PUBLIC_URL", "http://127.0.0.1:8100")
# Demo switch: deliver every webhook twice to show the merchant deduplicates
DUPLICATE_WEBHOOKS = os.environ.get("GATEWAY_DUPLICATE_WEBHOOKS") == "1"

Status = Literal["succeeded", "requires_action", "failed", "refunded", "voided"]


@dataclass
class Charge:
    id: str
    amount_cents: int
    currency: str
    idempotency_key: str
    card: str
    status: Status
    return_url: str
    metadata: dict = field(default_factory=dict)
    challenge_url: str | None = None
    created_at: float = field(default_factory=time.time)


class Store:
    """Thread-safe in-memory charge store with an idempotency index."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.charges: dict[str, Charge] = {}
        self.by_key: dict[str, str] = {}

    def reset(self) -> None:
        with self.lock:
            self.charges.clear()
            self.by_key.clear()


store = Store()


def send_webhook(event_type: str, charge: Charge, attempts: int = 3) -> bool:
    """POST a signed event to the merchant, retrying on network errors and non-2xx with backoff."""
    payload = {"id": f"evt_{secrets.token_hex(8)}", "type": event_type, "created": int(time.time()), "data": asdict(charge)}
    body = json.dumps(payload, separators=(",", ":")).encode()
    deliveries = 2 if DUPLICATE_WEBHOOKS else 1
    ok = False
    for _ in range(deliveries):
        for attempt in range(attempts):
            try:
                res = httpx2.post(
                    WEBHOOK_URL,
                    content=body,
                    headers={"content-type": "application/json", "X-Gateway-Signature": sign(WEBHOOK_SECRET, body)},
                    timeout=5.0,
                )
                if 200 <= res.status_code < 300:
                    ok = True
                    break
                log.warning("webhook %s got %s (attempt %d)", payload["id"], res.status_code, attempt + 1)
            except httpx2.HTTPError as exc:
                log.warning("webhook %s failed: %s (attempt %d)", payload["id"], type(exc).__name__, attempt + 1)
            time.sleep(0.5 * 2**attempt)
    return ok


# Indirection so tests can capture webhooks instead of sending HTTP
webhook_sender: Callable[[str, Charge], object] = send_webhook

app = FastAPI(title="Payment gateway simulator", version="1.0.0")


def require_api_key(authorization: str = Header(default="")) -> None:
    expected = f"Bearer {API_KEY}"
    if not secrets.compare_digest(authorization.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Invalid API key")


class ChargeRequest(BaseModel):
    amount_cents: int = Field(gt=0, le=1_000_000)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    idempotency_key: str = Field(min_length=8, max_length=100)
    card: Literal["frictionless", "challenge", "decline"]
    return_url: HttpUrl
    metadata: dict[str, str] = Field(default_factory=dict)


def public(charge: Charge) -> dict:
    return {k: v for k, v in asdict(charge).items() if k != "idempotency_key"}


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/v1/charges", dependencies=[Depends(require_api_key)])
def create_charge(body: ChargeRequest, tasks: BackgroundTasks) -> dict:
    with store.lock:
        # Same idempotency key -> same charge, never a second one
        existing_id = store.by_key.get(body.idempotency_key)
        if existing_id:
            return public(store.charges[existing_id])
        status: Status = {"frictionless": "succeeded", "challenge": "requires_action", "decline": "failed"}[body.card]
        charge = Charge(
            id=f"ch_{secrets.token_hex(8)}",
            amount_cents=body.amount_cents,
            currency=body.currency,
            idempotency_key=body.idempotency_key,
            card=body.card,
            status=status,
            return_url=str(body.return_url),
            metadata=body.metadata,
        )
        if status == "requires_action":
            charge.challenge_url = f"{PUBLIC_URL}/challenge/{charge.id}"
        store.charges[charge.id] = charge
        store.by_key[body.idempotency_key] = charge.id
    if status in ("succeeded", "failed"):
        tasks.add_task(webhook_sender, f"charge.{status}", charge)
    return public(charge)


@app.get("/v1/charges/{charge_id}", dependencies=[Depends(require_api_key)])
def get_charge(charge_id: str) -> dict:
    charge = store.charges.get(charge_id)
    if not charge:
        raise HTTPException(status_code=404, detail="No such charge")
    return public(charge)


@app.post("/v1/charges/{charge_id}/refund", dependencies=[Depends(require_api_key)])
def refund_charge(charge_id: str, tasks: BackgroundTasks) -> dict:
    """Refund a captured charge, or void one still waiting on a challenge. Idempotent."""
    with store.lock:
        charge = store.charges.get(charge_id)
        if not charge:
            raise HTTPException(status_code=404, detail="No such charge")
        if charge.status == "succeeded":
            charge.status = "refunded"
            tasks.add_task(webhook_sender, "charge.refunded", charge)
        elif charge.status == "requires_action":
            charge.status = "voided"
    return public(charge)


CHALLENGE_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Verify your payment</title>
<style>
body{{margin:0;font-family:system-ui,sans-serif;background:#0b1020;color:#f1f5f9;display:grid;place-items:center;min-height:100vh}}
.card{{width:min(24rem,90vw);background:#121a2b;border:1px solid #273449;border-radius:20px;padding:28px}}
small{{color:#94a3b8;letter-spacing:.12em;text-transform:uppercase;font-size:11px}}
h1{{font-size:20px;margin:10px 0 6px}} p{{color:#a3adc2;font-size:14px}}
.row{{display:flex;gap:10px;margin-top:22px}} button{{flex:1;height:44px;border-radius:12px;border:0;font-weight:600;cursor:pointer}}
.pass{{background:#f1f5f9;color:#0b1020}} .fail{{background:transparent;color:#fca5a5;border:1px solid #7f1d1d}}
</style></head><body><main class="card">
<small>Simulated card issuer · 3D Secure</small>
<h1>Confirm {amount} {currency}</h1>
<p>This page stands in for the cardholder's bank. Choose an outcome.</p>
<form method="post" class="row">
<button class="pass" name="outcome" value="pass">Approve</button>
<button class="fail" name="outcome" value="fail">Decline</button>
</form></main></body></html>"""


@app.get("/challenge/{charge_id}", response_class=HTMLResponse)
def challenge_page(charge_id: str) -> str:
    charge = store.charges.get(charge_id)
    if not charge or charge.status != "requires_action":
        raise HTTPException(status_code=404, detail="Challenge not found or already completed")
    amount = f"{charge.amount_cents / 100:,.2f}"
    return CHALLENGE_PAGE.format(amount=html.escape(amount), currency=html.escape(charge.currency))


@app.post("/challenge/{charge_id}")
def complete_challenge(charge_id: str, tasks: BackgroundTasks, outcome: Literal["pass", "fail"] = Form(...)) -> RedirectResponse:
    with store.lock:
        charge = store.charges.get(charge_id)
        if not charge or charge.status != "requires_action":
            raise HTTPException(status_code=409, detail="Challenge already completed")
        charge.status = "succeeded" if outcome == "pass" else "failed"
    tasks.add_task(webhook_sender, f"charge.{charge.status}", charge)
    return RedirectResponse(charge.return_url, status_code=303)
