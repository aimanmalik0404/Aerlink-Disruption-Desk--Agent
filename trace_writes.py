#!/usr/bin/env python3
"""Diagnostic: maps every real write in /_audit to the case it belongs to (via booking_ref),
so a check_records.py mismatch can be traced to a specific case instead of guessed at.

Usage: python trace_writes.py
"""
import json
from pathlib import Path
from tools import OpsClient

client = OpsClient()
audit = client.get_audit()
writes = audit["writes"]

booking_to_case = {}
for p in Path("outputs").iterdir():
    rec_path = p / "record.json"
    if rec_path.exists():
        rec = json.loads(rec_path.read_text(encoding="utf-8"))
        if rec.get("booking_ref"):
            booking_to_case[rec["booking_ref"]] = rec.get("case_id", p.name)

for kind, id_field, amount_field in [
    ("payments", "payment_id", "amount_gbp"),
    ("hotel_vouchers", "voucher_id", "rate_gbp"),
    ("refunds", "refund_id", "amount_gbp"),
    ("rebookings", "rebooking_id", "fare_gbp"),
]:
    print(f"\n=== {kind} ({len(writes[kind])}) ===")
    for w in writes[kind]:
        ref = w.get("booking_ref")
        case = booking_to_case.get(ref, "??? unknown booking_ref -- check manually")
        print(f"{w.get(id_field)} type={w.get('type', '')} booking={ref} case={case} "
              f"amount={w.get(amount_field)} passengers={w.get('passenger_ids')}")