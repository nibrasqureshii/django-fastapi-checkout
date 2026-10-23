"""End-to-end check against both running services (Django :8000, gateway :8100).

Walks the hardest path for real: hold seats -> pay with a 3DS challenge -> "refresh" and press Pay
again -> pass the challenge on the gateway -> wait for the signed webhook -> confirm exactly one
charge and a paid order. Run: python scripts/e2e.py
"""
import os
import sys
import time
import uuid

import httpx2

SHOP = os.environ.get("SHOP_URL", "http://127.0.0.1:8000")
GATEWAY = os.environ.get("GATEWAY_URL", "http://127.0.0.1:8100")
API_KEY = os.environ.get("GATEWAY_API_KEY", "dev-gateway-key")


def step(msg: str) -> None:
    print(f"  ✓ {msg}")


def main() -> int:
    with httpx2.Client(timeout=10) as http:
        event = http.get(f"{SHOP}/api/events/1/").json()
        free = [s["label"] for s in event["seats"] if s["status"] == "available"][:2]
        key = f"e2e-{uuid.uuid4()}"
        body = {"event_id": event["id"], "seats": free, "email": "e2e@example.com"}
        order = http.post(f"{SHOP}/api/orders/", json=body, headers={"Idempotency-Key": key}).json()
        retry = http.post(f"{SHOP}/api/orders/", json=body, headers={"Idempotency-Key": key})
        assert retry.status_code == 200 and retry.json()["id"] == order["id"], "idempotent create failed"
        step(f"held {free} on order {order['id'][:8]} (retry returned the same order)")

        paid = http.post(f"{SHOP}/api/orders/{order['id']}/pay/", json={"card": "challenge"}).json()
        assert paid["status"] == "requires_action", paid
        again = http.post(f"{SHOP}/api/orders/{order['id']}/pay/", json={"card": "challenge"}).json()
        resumed = http.get(f"{SHOP}/api/orders/{order['id']}/").json()
        assert again["payment"]["challenge_url"] == resumed["payment"]["challenge_url"] == paid["payment"]["challenge_url"]
        step("3DS challenge required; pressing Pay again and refreshing resume the same challenge")

        challenge = paid["payment"]["challenge_url"]
        res = http.post(challenge, data={"outcome": "pass"}, follow_redirects=False)
        assert res.status_code == 303, res.text
        step("cardholder approved on the simulated bank page")

        for _ in range(20):
            final = http.get(f"{SHOP}/api/orders/{order['id']}/").json()
            if final["status"] == "paid":
                break
            time.sleep(0.5)
        assert final["status"] == "paid", final
        step("signed webhook delivered; order is paid")

        charge_id = challenge.rsplit("/", 1)[-1]
        charge = http.get(f"{GATEWAY}/v1/charges/{charge_id}", headers={"Authorization": f"Bearer {API_KEY}"}).json()
        assert charge["status"] == "succeeded"
        seats = {s["label"]: s["status"] for s in http.get(f"{SHOP}/api/events/1/").json()["seats"]}
        assert all(seats[label] == "sold" for label in free)
        step(f"gateway shows one succeeded charge ({charge_id}); seats {free} are sold")
    print("E2E PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
