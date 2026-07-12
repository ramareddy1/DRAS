"""Derive two-sided reconciliation pairs from the Olist Kaggle dataset.

Reads  samples/Kaggle/Olist_datasets/  (never committed - CC BY-NC-SA 4.0)
Writes samples/olist/                  (gitignored, derived data)

  olist_order_totals.csv  - order_id, order_total, order_purchase_timestamp, order_status
  olist_payments_raw.csv  - order_id, payment_value, payment_type, payment_sequential
                            (deliberately NOT aggregated: exercises many-to-one)

The pair reconciles sum(order_items.price + freight_value) per order against
sum(order_payments.payment_value) per order. Orders are sampled so both files
stay under the 10MB upload limit. Natural discrepancies survive: voucher
stacking, rounding, and canceled orders.

Run: python samples/build_olist_pair.py [n_orders]
"""
import sys
from pathlib import Path

import pandas as pd

SRC = Path(__file__).parent / "Kaggle" / "Olist_datasets"
OUT = Path(__file__).parent / "olist"
N_ORDERS = int(sys.argv[1]) if len(sys.argv) > 1 else 20000

OUT.mkdir(exist_ok=True)

items = pd.read_csv(SRC / "olist_order_items_dataset.csv",
                    usecols=["order_id", "price", "freight_value"])
orders = pd.read_csv(SRC / "olist_orders_dataset.csv",
                     usecols=["order_id", "order_purchase_timestamp", "order_status"])
pay = pd.read_csv(SRC / "olist_order_payments_dataset.csv")

totals = (items.groupby("order_id", as_index=False)
               .agg(items_total=("price", "sum"), freight=("freight_value", "sum")))
totals["order_total"] = (totals["items_total"] + totals["freight"]).round(2)
totals = (totals[["order_id", "order_total"]]
          .merge(orders, on="order_id", how="inner"))

sampled = totals.sample(n=min(N_ORDERS, len(totals)), random_state=42)
sampled.to_csv(OUT / "olist_order_totals.csv", index=False)
pay_sampled = pay[pay["order_id"].isin(sampled["order_id"])]
pay_sampled.to_csv(OUT / "olist_payments_raw.csv", index=False)

multi = pay_sampled[pay_sampled["payment_sequential"] > 1]["order_id"].nunique()
print(f"orders:   {len(sampled):>6} rows -> {OUT / 'olist_order_totals.csv'}")
print(f"payments: {len(pay_sampled):>6} rows -> {OUT / 'olist_payments_raw.csv'}")
print(f"multi-payment orders in sample: {multi} (these break 1:1 matching)")
