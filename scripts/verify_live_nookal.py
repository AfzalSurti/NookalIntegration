"""
Safe Read-Only Production Verification Script for Nookal API v2.

Usage:
    python scripts/verify_live_nookal.py

Prerequisites:
    Set NOOKAL_API_KEY in the environment or in a local .env file.
    Optionally set NOOKAL_BASE_URL (defaults to https://api.nookal.com/production/v2/).
"""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Load .env if present
env_path = Path(".env")
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))

from app.nookal_client.client import HttpNookalClient, NookalConfig


def main() -> int:
    api_key = os.getenv("NOOKAL_API_KEY")
    if not api_key:
        print("[SKIP] NOOKAL_API_KEY environment variable is not set.")
        print("To run live verification against production Nookal API:")
        print("  set NOOKAL_API_KEY=<your_api_key>")
        print("  python scripts/verify_live_nookal.py")
        return 0

    base_url = os.getenv("NOOKAL_BASE_URL", "https://api.nookal.com/production/v2/")
    print(f"Connecting to Nookal API: {base_url} (API key: {'*' * (len(api_key) - 4) + api_key[-4:] if len(api_key) > 4 else '***'})")

    client = HttpNookalClient(config=NookalConfig(api_key=api_key, base_url=base_url))

    checks = [
        ("Locations (/getLocations)", lambda: client.get_locations()),
        ("Practitioners (/getPractitioners)", lambda: client.get_practitioners()),
        ("Appointment Types (/getAppointmentTypes)", lambda: client.get_appointment_types()),
        ("Class Types (/getClassTypes)", lambda: client.get_class_types()),
        ("Waiting List (/getWaitingList)", lambda: client.get_waiting_list()),
        ("Class Availabilities (/getClassAvailabilities)", lambda: client.get_class_availabilities(date_from=date.today(), date_to=date.today())),
        ("Patients sample (/getPatients)", lambda: client.get_patients(page_length=5)),
        ("Appointments today (/getAppointments)", lambda: client.list_appointments(date_from=date.today(), page_length=5)),
        ("Appointment Availabilities (/getAppointmentAvailabilities)", lambda: client.get_appointment_availabilities(date_from=date.today(), date_to=date.today())),
        ("Cases sample (/getAllCases)", lambda: client.get_all_cases(page_length=5)),
        ("Treatment Notes sample (/getAllTreatmentNotes)", lambda: client.get_all_treatment_notes(page_length=5)),
        ("Patient Extras (/getExtras)", lambda: client.get_extras()),
        ("Invoices sample (/getInvoices)", lambda: client.get_invoices()),
        ("Invoice Entries (/getInvoiceEntries)", lambda: client.get_invoice_entries()),
        ("Invoice Credits (/getInvoiceCredits)", lambda: client.get_invoice_credits()),
        ("Invoice Discounts (/getInvoiceDiscounts)", lambda: client.get_invoice_discounts()),
        ("Invoice Payments (/getInvoicePayments)", lambda: client.get_invoice_payments()),
        ("Invoice Refunds (/getInvoiceRefunds)", lambda: client.get_invoice_refunds()),
        ("Invoice Adjustments (/getInvoiceAdjustments)", lambda: client.get_invoice_adjustments()),
    ]

    successes = 0
    failures = 0

    for name, fn in checks:
        print(f"\n[*] Checking: {name}...", end=" ")
        try:
            res = fn()
            count = len(res) if hasattr(res, "__len__") else 1
            print(f"OK (retrieved {count} records)")
            successes += 1
            if count > 0 and hasattr(res, "__getitem__"):
                sample = res[0]
                print(f"    Sample type: {type(sample).__name__}")
        except Exception as exc:
            print(f"FAILED: {type(exc).__name__}: {exc}")
            failures += 1

    print("\n" + "=" * 50)
    print(f"Verification completed: {successes} succeeded, {failures} failed.")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
