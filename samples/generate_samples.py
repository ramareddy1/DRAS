"""Generate realistic sample data for ReconOps AI demos.

Run: python samples/generate_samples.py
"""
from __future__ import annotations

import csv
import os
import random
import string
from datetime import datetime, timedelta
from pathlib import Path

random.seed(42)

OUT = Path(__file__).parent


def _pi_id() -> str:
    return "pi_" + "".join(random.choices(string.ascii_letters + string.digits, k=24))


def gen_orders_vs_payments():
    """Sample Set 1: Shopify Orders <-> Stripe Payments."""
    n = 200
    orders = []
    payments = []

    start = datetime(2026, 4, 1)
    order_ids = [f"#{1000 + i}" for i in range(n)]

    for i, oid in enumerate(order_ids):
        order_date = start + timedelta(days=i // 8, hours=random.randint(0, 23))
        amount = round(random.uniform(25, 400), 2)
        customer = random.choice(["Anna Lee", "Ben Wright", "Carla Diaz", "Dan Patel",
                                  "Eva Klein", "Felix Tran", "Gia Romano", "Hugo Bauer",
                                  "Iris Chen", "Jay Okafor"])
        orders.append({
            "order_id": oid,
            "order_date": order_date.strftime("%Y-%m-%d %H:%M:%S"),
            "customer": customer,
            "order_total": amount,
            "currency": "USD",
            "status": "paid",
        })

    # ~85% perfect match | ~8% amount discrepancy (fee pattern) | ~5% in A not B | ~2% in B not A
    fates = (["match"] * 170) + (["fee_disc"] * 16) + (["a_only"] * 10) + (["b_only"] * 4)
    random.shuffle(fates)

    for order, fate in zip(orders, fates):
        if fate == "a_only":
            continue
        pay_date = datetime.strptime(order["order_date"], "%Y-%m-%d %H:%M:%S") + timedelta(
            days=random.randint(1, 3), hours=random.randint(0, 23)
        )
        if fate == "match":
            amt = order["order_total"]
        elif fate == "fee_disc":
            # Stripe net = gross - (2.9% + 0.30)
            amt = round(order["order_total"] - (order["order_total"] * 0.029 + 0.30), 2)
        else:
            amt = order["order_total"]
        # tie payment back to order id in description
        payments.append({
            "transaction_id": _pi_id(),
            "settlement_date": pay_date.strftime("%Y-%m-%d %H:%M:%S"),
            "order_reference": order["order_id"],
            "amount": amt,
            "currency": "USD",
            "status": "succeeded",
        })

    # add ~4 b_only manual charges / subscription payments
    for i in range(4):
        d = start + timedelta(days=random.randint(0, 25))
        payments.append({
            "transaction_id": _pi_id(),
            "settlement_date": d.strftime("%Y-%m-%d %H:%M:%S"),
            "order_reference": f"SUB-{random.randint(100,999)}",
            "amount": round(random.uniform(15, 80), 2),
            "currency": "USD",
            "status": "succeeded",
        })

    random.shuffle(payments)

    _write_csv(OUT / "shopify_orders.csv", orders)
    _write_csv(OUT / "stripe_payments.csv", payments)


def gen_inventory():
    """Sample Set 2: Platform stock <-> 3PL stock report."""
    n = 150
    products = [
        ("TEE", "T-Shirt"), ("HOD", "Hoodie"), ("CAP", "Cap"),
        ("MUG", "Mug"), ("BTL", "Water Bottle"), ("BAG", "Tote Bag"),
        ("SCK", "Socks"), ("STK", "Sticker Pack"),
    ]
    colors = ["BLK", "WHT", "GRY", "NVY", "RED", "GRN"]
    sizes = ["S", "M", "L", "XL"]

    seen = set()
    skus = []
    while len(skus) < n:
        p_code, p_name = random.choice(products)
        col = random.choice(colors)
        sz = random.choice(sizes)
        sku = f"{p_code}-{col}-{sz}"
        if sku in seen:
            continue
        seen.add(sku)
        skus.append((sku, f"{p_name} {col} {sz}"))

    platform = []
    threepl = []

    fates = (["match"] * 113) + (["disc"] * 22) + (["a_only"] * 8) + (["b_only"] * 7)
    random.shuffle(fates)

    for (sku, name), fate in zip(skus, fates):
        qty = random.randint(5, 250)
        if fate == "b_only":
            threepl.append({"sku": sku, "product_name": name, "qty_on_hand": qty})
            continue
        platform.append({"sku": sku, "product_name": name, "quantity": qty})
        if fate == "a_only":
            continue
        if fate == "match":
            threepl.append({"sku": sku, "product_name": name, "qty_on_hand": qty})
        else:
            delta = random.choice([-1, -2, -3, -5, -10, 1, 2, 5])
            threepl.append({"sku": sku, "product_name": name, "qty_on_hand": max(0, qty + delta)})

    random.shuffle(platform)
    random.shuffle(threepl)
    _write_csv(OUT / "shopify_inventory.csv", platform)
    _write_csv(OUT / "threepl_stock_report.csv", threepl)


def _write_csv(path: Path, rows: list[dict]):
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {path} ({len(rows)} rows)")


if __name__ == "__main__":
    gen_orders_vs_payments()
    gen_inventory()
