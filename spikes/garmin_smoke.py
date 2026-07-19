#!/usr/bin/env python3
"""Garmin Connect read smoke test.

Throwaway spike: proves we can authenticate and pull real data from a Garmin
Connect account. Uses the community `garminconnect` client (private endpoints).

Setup:
    python3 -m venv .venv && source .venv/bin/activate
    pip install garminconnect curl_cffi
    # NOTE: 0.1.53 is old and usually fails login now; the current release is
    # what actually authenticates. To force the old one anyway:
    #   pip install "garminconnect==0.1.53"

Run:
    GARMIN_EMAIL="you@email.com" GARMIN_PASSWORD="secret" python spikes/garmin_smoke.py
    # or just run it and you'll be prompted. MFA code is asked interactively.

Tokens are cached under ~/.garminconnect so later runs skip the login/MFA step.
Nothing is written to your Garmin account: this is read-only.
"""

from __future__ import annotations

import datetime as dt
import getpass
import json
import os
import sys

try:
    from garminconnect import Garmin
except ImportError:
    sys.exit("Missing dependency. Run: pip install garminconnect curl_cffi")

TOKENSTORE = os.path.expanduser("~/.garminconnect")


def show(label: str, value: object) -> None:
    """Print a labelled, truncated JSON blob so output stays readable."""
    print(f"\n=== {label} ===")
    text = json.dumps(value, indent=2, default=str)
    print(text if len(text) <= 2000 else text[:2000] + "\n... (truncated)")


def connect() -> Garmin:
    """Log in, reusing cached tokens when possible, prompting for MFA if needed.

    In garminconnect 0.3.6 `login(tokenstore)` loads cached tokens when present
    and valid, otherwise performs a credential login and saves tokens there.
    """
    email = os.getenv("GARMIN_EMAIL") or input("Garmin email: ").strip()
    password = os.getenv("GARMIN_PASSWORD") or getpass.getpass("Garmin password: ")

    g = Garmin(email, password, prompt_mfa=lambda: input("MFA code: ").strip())
    g.login(TOKENSTORE)  # uses cache if valid, else credentials; persists tokens
    print(f"Authenticated. Tokens cached at {TOKENSTORE}")
    return g


def main() -> None:
    g = connect()
    today = dt.date.today().isoformat()

    # Each call is wrapped so one unsupported metric doesn't abort the whole run.
    checks = {
        "Full name": lambda: g.get_full_name(),
        "Device last used": lambda: g.get_device_last_used(),
        "Daily stats": lambda: g.get_stats(today),
        "Last 3 activities": lambda: g.get_activities(0, 3),
        "Sleep": lambda: g.get_sleep_data(today),
        "Resting heart rate": lambda: g.get_rhr_day(today),
        "HRV": lambda: g.get_hrv_data(today),
        "Training readiness": lambda: g.get_training_readiness(today),
    }

    for label, fn in checks.items():
        try:
            show(label, fn())
        except Exception as exc:  # noqa: BLE001 - spike: report and continue
            print(f"\n=== {label} ===\n(skipped: {exc})")


if __name__ == "__main__":
    main()
