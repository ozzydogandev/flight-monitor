#!/usr/bin/env python3
"""
Flight Deal Monitor — IAD → Anywhere in the US
Uses Aviasales Data API to find round-trip deals <= $50/person
with at least 2 nights stay, then sends email alerts.
"""

import json
import os
import smtplib
import sys
import urllib.request
import urllib.parse
from datetime import date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# ── Configuration ──────────────────────────────────────────────────────────────

ORIGIN = "IAD"
MAX_PRICE = 80      # USD per person round trip
MIN_NIGHTS = 2
CURRENCY = "usd"

# Search windows (days from today)
SHORT_WINDOW_START = 7
SHORT_WINDOW_END   = 28
LONG_WINDOW_START  = 30
LONG_WINDOW_END    = 90

SEEN_FILE = Path(__file__).parent / "seen_deals.json"

# Weekend: depart Thu(3) or Fri(4), return Sun(6) or Mon(0)
WEEKEND_DEPART_DAYS = {3, 4}
WEEKEND_RETURN_DAYS = {6, 0}

AVIASALES_API_URL = "https://api.travelpayouts.com/v2/prices/latest"

# Known US airport codes (top destinations from IAD)
US_AIRPORTS = {
    "ATL", "LAX", "ORD", "DFW", "DEN", "JFK", "SFO", "SEA", "LAS", "MCO",
    "EWR", "CLT", "PHX", "MIA", "IAH", "BOS", "MSP", "DTW", "FLL", "PHL",
    "LGA", "BWI", "SLC", "SAN", "TPA", "PDX", "HNL", "MDW", "STL", "OAK",
    "MCI", "RDU", "CLE", "AUS", "BNA", "PIT", "MSY", "SMF", "SJC", "DAL",
    "HOU", "OGG", "ABQ", "BUF", "CVG", "IND", "JAX", "MKE", "OMA", "SAT",
    "TUL", "BOI", "GEG", "GSP", "ICT", "LIT", "MEM", "OKC", "ONT", "PBI",
    "RSW", "SDF", "SNA", "SRQ", "TUS", "XNA",
}


# ── Date helpers ───────────────────────────────────────────────────────────────

def is_weekend_trip(depart: date, return_date: date) -> bool:
    return (
        depart.weekday() in WEEKEND_DEPART_DAYS
        and return_date.weekday() in WEEKEND_RETURN_DAYS
    )


def in_window(d: date, start_offset: int, end_offset: int) -> bool:
    today = date.today()
    return today + timedelta(days=start_offset) <= d <= today + timedelta(days=end_offset)


# ── Aviasales Data API ─────────────────────────────────────────────────────────

def fetch_raw_prices() -> list[dict]:
    token = os.environ["AVIASALES_TOKEN"]
    params = urllib.parse.urlencode({
        "origin":    ORIGIN,
        "currency":  CURRENCY,
        "one_way":   "false",
        "limit":     1000,
        "token":     token,
    })
    url = f"{AVIASALES_API_URL}?{params}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        body = json.loads(resp.read())
    return body.get("data", [])


def parse_deals(raw: list[dict]) -> list[dict]:
    """
    Each raw item looks like:
    {
      "origin":          "IAD",
      "destination":     "MIA",
      "depart_date":     "2026-06-06",
      "return_date":     "2026-06-09",
      "value":           38,
      "number_of_changes": 0,
      "found_at":        "2026-05-21T10:00:00Z",
      ...
    }
    """
    today = date.today()
    short_start = today + timedelta(days=SHORT_WINDOW_START)
    long_end    = today + timedelta(days=LONG_WINDOW_END)

    deals = []
    for item in raw:
        try:
            destination = item.get("destination", "")
            if destination not in US_AIRPORTS:
                continue

            price = float(item.get("value", 9999))
            if price > MAX_PRICE:
                continue

            depart_str = item.get("depart_date", "")
            return_str = item.get("return_date", "")
            if not depart_str or not return_str:
                continue

            depart = date.fromisoformat(depart_str)
            ret    = date.fromisoformat(return_str)

            # Must fall within one of our two search windows
            if not (short_start <= depart <= long_end):
                continue

            nights = (ret - depart).days
            if nights < MIN_NIGHTS:
                continue

            deals.append({
                "origin":      item.get("origin", ORIGIN),
                "destination": destination,
                "depart":      depart,
                "return":      ret,
                "nights":      nights,
                "price":       price,
                "is_weekend":  is_weekend_trip(depart, ret),
                "stops":       item.get("number_of_changes", "?"),
                "book_url":    (
                    f"https://www.aviasales.com/search/"
                    f"{ORIGIN}{depart.strftime('%d%m')}"
                    f"{destination}{ret.strftime('%d%m')}1"
                ),
            })
        except (KeyError, ValueError, TypeError) as e:
            print(f"[WARN] Skipping item: {e}")

    return deals


# ── Deduplication ──────────────────────────────────────────────────────────────

def load_seen() -> set[str]:
    if not SEEN_FILE.exists():
        return set()
    data = json.loads(SEEN_FILE.read_text())
    today_str = date.today().isoformat()
    return {k for k, v in data.items() if v == today_str}


def save_seen(seen: set[str]) -> None:
    existing: dict = {}
    if SEEN_FILE.exists():
        existing = json.loads(SEEN_FILE.read_text())
    today_str = date.today().isoformat()
    for key in seen:
        existing[key] = today_str
    SEEN_FILE.write_text(json.dumps(existing, indent=2))


def deal_key(deal: dict) -> str:
    return f"{deal['destination']}_{deal['depart']}_{deal['return']}"


# ── Email ──────────────────────────────────────────────────────────────────────

def format_email(deals: list[dict]) -> tuple[str, str]:
    weekend_only = all(d["is_weekend"] for d in deals)
    count = len(deals)

    if count == 1:
        d = deals[0]
        tag = "[Weekend] " if d["is_weekend"] else ""
        subject = (
            f"{tag}✈ ${d['price']:.0f}/pp Round Trip: "
            f"{d['origin']} → {d['destination']} | "
            f"{d['depart'].strftime('%a %b %-d')} – {d['return'].strftime('%a %b %-d')}"
        )
    else:
        prefix = "[Weekend] " if weekend_only else ""
        subject = f"{prefix}✈ {count} Flight Deals from {ORIGIN} (≤ ${MAX_PRICE}/pp)"

    lines = [f"Flight deals from Dulles (IAD) under ${MAX_PRICE}/person round trip:\n"]
    for d in deals:
        weekend_tag = " [WEEKEND]" if d["is_weekend"] else ""
        stops_label = "Nonstop" if d["stops"] == 0 else f"{d['stops']} stop(s)"
        lines += [
            "─" * 50,
            f"  Route:   {d['origin']} → {d['destination']}{weekend_tag}",
            f"  Depart:  {d['depart'].strftime('%A, %B %-d %Y')}",
            f"  Return:  {d['return'].strftime('%A, %B %-d %Y')}",
            f"  Stay:    {d['nights']} nights",
            f"  Price:   ${d['price']:.0f} / person round trip",
            f"  Stops:   {stops_label}",
            f"  Search:  {d['book_url']}",
            "",
        ]

    lines.append("Happy travels! ✈")
    return subject, "\n".join(lines)


def send_email(subject: str, body: str) -> None:
    gmail_address = os.environ["GMAIL_ADDRESS"]
    app_password  = os.environ["GMAIL_APP_PASSWORD"]
    notify_email  = os.environ["NOTIFY_EMAIL"]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = gmail_address
    msg["To"]      = notify_email
    msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.ehlo()
        server.starttls()
        server.login(gmail_address, app_password)
        server.sendmail(gmail_address, notify_email, msg.as_string())

    print(f"[OK] Email sent: {subject}")


# ── Test mode ──────────────────────────────────────────────────────────────────

def send_test_email() -> None:
    fake = {
        "origin":      "IAD",
        "destination": "MIA",
        "depart":      date.today() + timedelta(days=9),   # Friday
        "return":      date.today() + timedelta(days=11),  # Sunday
        "nights":      2,
        "price":       38.0,
        "is_weekend":  True,
        "stops":       0,
        "book_url":    "https://www.aviasales.com",
    }
    subject, body = format_email([fake])
    send_email("[TEST] " + subject, body)


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    if "--test" in sys.argv:
        print("[INFO] Test mode — sending dummy email...")
        send_test_email()
        return

    print("[INFO] Fetching latest prices from Aviasales...")
    try:
        raw = fetch_raw_prices()
    except Exception as e:
        print(f"[ERROR] Failed to fetch prices: {e}")
        sys.exit(1)

    print(f"[INFO] Raw results: {len(raw)}")
    deals = parse_deals(raw)
    print(f"[INFO] Matching deals (US, ≤${MAX_PRICE}, ≥{MIN_NIGHTS} nights): {len(deals)}")

    seen = load_seen()
    new_deals = []
    for deal in deals:
        key = deal_key(deal)
        if key not in seen:
            new_deals.append(deal)
            seen.add(key)

    print(f"[INFO] New (not yet notified today): {len(new_deals)}")

    if new_deals:
        if len(new_deals) <= 3:
            for deal in new_deals:
                subject, body = format_email([deal])
                send_email(subject, body)
        else:
            subject, body = format_email(new_deals)
            send_email(subject, body)
        save_seen(seen)
    else:
        print("[INFO] No new deals found.")


if __name__ == "__main__":
    main()
