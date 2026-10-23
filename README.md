# Resumable 3D Secure Checkout: Django + FastAPI

A ticketing checkout that holds seats, takes card payments that may need a **3D Secure challenge**, and guarantees a buyer is **charged at most once**: however they refresh, double-click, abandon the tab, or however often the payment provider retries its webhooks.

- **Author:** Muhammad Nibras Qureshi
- **Type:** personal project with fictional data. It is an original implementation of industry-standard checkout patterns, not a copy of any production system.

## Architecture

```
 Browser ──► Django shop (:8000) ───────────────► FastAPI gateway simulator (:8100)
             • events, seats, orders, payments      • charges: frictionless / challenge / decline
             • Django REST Framework JSON API       • 3DS challenge page (simulated bank)
             • Django admin                         • idempotent charges, refunds, voids
             • PostgreSQL (row locks)               • signed webhooks with retry + backoff
                    ▲                                          │
                    └──── POST /api/webhooks/gateway/ ◄────────┘
                          HMAC-SHA256 signed, timestamped, deduplicated
```

## Guarantees and the tests that prove them

| Guarantee | How | Test |
|---|---|---|
| Retrying "create order" never creates a second order | `Idempotency-Key` header, unique in the DB | `test_same_idempotency_key_returns_same_order` |
| Two buyers can't hold the same seat | `SELECT … FOR UPDATE` on seats, locked in a stable order | `test_two_buyers_cannot_hold_the_same_seat` |
| Pressing Pay twice never charges twice | One `Payment` per order (DB-enforced) + gateway idempotency key | `test_pressing_pay_twice_never_charges_twice` |
| Refreshing mid-challenge resumes, it doesn't restart | Order state and challenge URL are read from the server | `test_pay_and_resume_do_not_leak_charge_id` |
| Duplicate or replayed webhooks change nothing | Event id is unique; signature has a timestamp tolerance | `test_duplicate_webhook_is_ignored`, `test_webhook_rejects_bad_and_stale_signatures` |
| A webhook racing our own write still lands | Fallback match on `order_id` in charge metadata | `test_webhook_arriving_before_charge_id_is_saved` |
| Abandoned holds free their seats and void the challenge | `expire_holds` command (run every minute) | `test_expired_hold_releases_seats_and_voids_challenge` |
| Money arriving after the hold expired is refunded, not double-sold | Refund on `charge.succeeded` for a closed order | `test_success_after_expiry_is_refunded_not_sold` |
| Gateway outage is safe to retry | Network call happens outside DB locks; idempotent | `test_gateway_outage_returns_502_and_is_retryable` |

**32 automated tests:** 23 Django tests against PostgreSQL and 9 FastAPI tests. There is also an end-to-end script (`scripts/e2e.py`) that drives both running services through the full challenge flow.

## Run it locally

Requirements: Python 3.12+, PostgreSQL.

```bash
python3 -m venv .venv
.venv/bin/pip install -r shop/requirements.txt -r gateway/requirements.txt -r requirements-dev.txt
createdb ticket_checkout

# Terminal 1: gateway simulator
cd gateway && ../.venv/bin/uvicorn app.main:app --port 8100

# Terminal 2: Django shop
cd shop
export DJANGO_DEBUG=1 DATABASE_URL=postgres://localhost:5432/ticket_checkout
../.venv/bin/python manage.py migrate && ../.venv/bin/python manage.py seed_demo
../.venv/bin/python manage.py runserver 8000
```

Open http://127.0.0.1:8000, pick seats, choose the "3D Secure challenge" test card and pay. Try refreshing on the bank page, or pressing Pay twice.

With Docker installed, `docker compose up` starts Postgres and both services. This file has not been run in the author's environment yet.

### Tests

```bash
cd gateway && ../.venv/bin/python -m pytest -q
cd shop && ../.venv/bin/python -m pytest -q        # needs a local Postgres
../.venv/bin/python scripts/e2e.py                  # with both services running
```

## Configuration

| Variable | Service | Purpose |
|---|---|---|
| `DJANGO_SECRET_KEY` | shop | Required when `DJANGO_DEBUG` is off |
| `DATABASE_URL` | shop | `postgres://user:pass@host:5432/db` |
| `GATEWAY_URL`, `GATEWAY_API_KEY` | shop | Where and how to call the gateway (Bearer key) |
| `WEBHOOK_SECRET` | both | Shared HMAC secret for webhook signatures |
| `SITE_URL` | shop | Public URL, used for the bank redirect back |
| `MERCHANT_WEBHOOK_URL`, `GATEWAY_PUBLIC_URL` | gateway | Where to send webhooks; public URL for challenge links |
| `GATEWAY_DUPLICATE_WEBHOOKS=1` | gateway | Demo: deliver every webhook twice to show deduplication |

## API

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/events/{id}/` | Event and seat map |
| `POST` | `/api/orders/` | Body `{event_id, seats[], email}`. Header `Idempotency-Key` required. 201 new, 200 replay, 409 conflict |
| `GET` | `/api/orders/{id}/` | Current state, used to resume after refresh or redirect |
| `POST` | `/api/orders/{id}/pay/` | Body `{card: frictionless \| challenge \| decline}`. 502 if the gateway is down (safe to retry) |
| `POST` | `/api/webhooks/gateway/` | Gateway only. Header `X-Gateway-Signature: t=…,v1=…` |

## Design decisions and trade-offs

- **The gateway is a separate service** so the boundary is real: network failures, retries and out-of-order webhooks all happen, as with a real payment processor.
- **No network calls inside DB transactions.** Row locks are never held while waiting on the gateway. Idempotency makes the call safe to repeat instead.
- **Webhooks are the source of truth** for challenge outcomes. The browser redirect only triggers a status read.
- **Hold expiry uses a management command** (cron or a platform scheduler) rather than a queue, to keep the stack small. A task queue would be the next step at scale.
- **The gateway keeps state in memory.** It's a simulator, so restarting it clears charges, which is fine for demos and tests but not for production.
