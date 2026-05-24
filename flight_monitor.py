#!/usr/bin/env python3
from __future__ import annotations
"""
Flight Deal Monitor — IAD → Anywhere in the US
Uses fast-flights to scrape real-time Google Flights prices.
Parallel requests via ThreadPoolExecutor for fast runs (~2-3 min).
"""

import json
import os
import random
import smtplib
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from fast_flights import FlightData, Passengers, get_flights

# ── Configuration ──────────────────────────────────────────────────────────────

ORIGIN      = "IAD"
CURRENCY    = "USD"
MAX_WORKERS = 8  # parallel flight searches

SEEN_FILE = Path(__file__).parent / "seen_deals.json"

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

DESTINATIONS = [
    "ATL", "BOS", "ORD", "DFW", "DEN", "JFK", "SFO", "SEA",
    "LAS", "MCO", "CLT", "MIA", "IAH", "MSP", "FLL", "SLC",
    "TPA", "LAX", "AUS", "MSY", "PDX", "BNA", "RDU", "PHX", "HNL",
]

AIRPORT_NAMES = {
    "IAD": "Dulles, VA",
    "ATL": "Atlanta, GA",
    "BOS": "Boston, MA",
    "ORD": "Chicago, IL",
    "DFW": "Dallas, TX",
    "DEN": "Denver, CO",
    "JFK": "New York, NY",
    "SFO": "San Francisco, CA",
    "SEA": "Seattle, WA",
    "LAS": "Las Vegas, NV",
    "MCO": "Orlando, FL",
    "CLT": "Charlotte, NC",
    "MIA": "Miami, FL",
    "IAH": "Houston, TX",
    "MSP": "Minneapolis, MN",
    "FLL": "Fort Lauderdale, FL",
    "SLC": "Salt Lake City, UT",
    "TPA": "Tampa, FL",
    "LAX": "Los Angeles, CA",
    "AUS": "Austin, TX",
    "MSY": "New Orleans, LA",
    "PDX": "Portland, OR",
    "BNA": "Nashville, TN",
    "RDU": "Raleigh, NC",
    "PHX": "Phoenix, AZ",
    "HNL": "Honolulu, HI",
}


# ── Date helpers ───────────────────────────────────────────────────────────────

def candidate_trips() -> list[tuple[date, date]]:
    today = date.today()
    trips = []
    for offset in range(7, 29):
        d = today + timedelta(days=offset)
        if d.weekday() in (0, 4):
            for nights in (2, 3, 4, 5, 6):
                trips.append((d, d + timedelta(days=nights)))
    for offset in range(35, 85, 14):
        d = today + timedelta(days=offset)
        d += timedelta(days=(4 - d.weekday()) % 7)
        for nights in (2, 3, 4, 5, 6):
            trips.append((d, d + timedelta(days=nights)))
    return trips


def is_weekend_trip(depart: date, ret: date) -> bool:
    return depart.weekday() in {3, 4} and ret.weekday() in {6, 0}


# ── Flight search ──────────────────────────────────────────────────────────────

def get_cheapest_price(origin: str, destination: str, depart: date, ret: date) -> float | None:
    try:
        result = get_flights(
            flight_data=[
                FlightData(date=depart.isoformat(), from_airport=origin, to_airport=destination),
                FlightData(date=ret.isoformat(),    from_airport=destination, to_airport=origin),
            ],
            trip="round-trip",
            seat="economy",
            passengers=Passengers(adults=1),
        )
        prices = []
        for f in result.flights:
            if f.price:
                try:
                    p = float(f.price.replace("$", "").replace(",", ""))
                    if p > 0:
                        prices.append(p)
                except ValueError:
                    pass
        return min(prices) if prices else None
    except Exception as e:
        print(f"[WARN] {origin}→{destination} {depart}: {e}")
        return None


def check_route(args: tuple) -> dict | None:
    """Worker: check one (destination, depart, ret) combo. Returns deal dict or None."""
    destination, depart, ret, max_price_global = args
    time.sleep(random.uniform(0.1, 0.4))  # small polite stagger per worker
    price = get_cheapest_price(ORIGIN, destination, depart, ret)
    if price is None or price > max_price_global:
        return None
    return {
        "destination": destination,
        "depart":      depart,
        "return":      ret,
        "nights":      (ret - depart).days,
        "price":       price,
        "is_weekend":  is_weekend_trip(depart, ret),
        "book_url":    (
            f"https://www.kayak.com/flights/"
            f"{ORIGIN}-{destination}/"
            f"{depart.strftime('%Y-%m-%d')}/"
            f"{ret.strftime('%Y-%m-%d')}/1adults"
        ),
    }


# ── Subscribers ────────────────────────────────────────────────────────────────

def fetch_subscribers() -> list[dict]:
    import urllib.request
    url = f"{SUPABASE_URL}/rest/v1/subscribers?active=eq.true&select=email,max_price,origin"
    req = urllib.request.Request(url, headers={
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


# ── Deduplication ──────────────────────────────────────────────────────────────

def load_seen() -> dict:
    if not SEEN_FILE.exists():
        return {}
    try:
        return json.loads(SEEN_FILE.read_text())
    except (json.JSONDecodeError, ValueError):
        return {}


def save_seen(seen: dict) -> None:
    SEEN_FILE.write_text(json.dumps(seen, indent=2, default=str))


def deal_key(email: str, destination: str, depart: date, ret: date) -> str:
    return f"{email}_{destination}_{depart}_{ret}"


# ── Email ──────────────────────────────────────────────────────────────────────

def format_email(deals: list[dict], max_price: int) -> tuple[str, str]:
    weekend_only = all(d["is_weekend"] for d in deals)
    count = len(deals)
    if count == 1:
        d = deals[0]
        tag = "[Weekend] " if d["is_weekend"] else ""
        subject = (
            f"{tag}✈ ${d['price']:.0f}/pp Round Trip: "
            f"{ORIGIN} → {d['destination']} | "
            f"{d['depart'].strftime('%a %b %-d')} – {d['return'].strftime('%a %b %-d')}"
        )
    else:
        prefix = "[Weekend] " if weekend_only else ""
        subject = f"{prefix}✈ {count} Flight Deals from {ORIGIN} (≤ ${max_price}/pp)"

    lines = [
        f"Flight deals from Dulles (IAD) under ${max_price}/person round trip:",
        "Prices are live from Google Flights — act fast!\n",
    ]
    for d in deals:
        weekend_tag = " [WEEKEND]" if d["is_weekend"] else ""
        lines += [
            "─" * 50,
            f"  Route:   {ORIGIN} → {d['destination']}{weekend_tag}",
            f"  From:    {AIRPORT_NAMES.get(ORIGIN, ORIGIN)}",
            f"  To:      {AIRPORT_NAMES.get(d['destination'], d['destination'])}",
            f"  Depart:  {d['depart'].strftime('%A, %B %-d %Y')}",
            f"  Return:  {d['return'].strftime('%A, %B %-d %Y')}",
            f"  Stay:    {d['nights']} nights",
            f"  Price:   ${d['price']:.0f} / person round trip",
            f"  Book:    {d['book_url']}",
            "",
        ]
    lines.append("Happy travels! ✈")
    return subject, "\n".join(lines)


def add_unsubscribe_footer(body: str, email: str) -> str:
    base_url = os.getenv("APP_URL", "https://flight-monitor-ui.vercel.app")
    url = f"{base_url}/api/unsubscribe?email={urllib.parse.quote(email)}"
    return body + f"\n\n─────────────────────────────\nDon't want these alerts? Unsubscribe: {url}"


def send_email(to: str, subject: str, body: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = os.environ["GMAIL_ADDRESS"]
    msg["To"]      = to
    msg.attach(MIMEText(body, "plain"))
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.ehlo()
        server.starttls()
        server.login(os.environ["GMAIL_ADDRESS"], os.environ["GMAIL_APP_PASSWORD"])
        server.sendmail(os.environ["GMAIL_ADDRESS"], to, msg.as_string())
    print(f"[OK] Email → {to}: {subject}")


# ── Test mode ──────────────────────────────────────────────────────────────────

def send_test_email() -> None:
    fake = {
        "destination": "MIA",
        "depart":      date.today() + timedelta(days=9),
        "return":      date.today() + timedelta(days=11),
        "nights":      2,
        "price":       38.0,
        "is_weekend":  True,
        "book_url":    "https://www.kayak.com/flights/IAD-MIA",
    }
    notify = os.environ["NOTIFY_EMAIL"]
    subject, body = format_email([fake], 60)
    send_email(notify, "[TEST] " + subject, add_unsubscribe_footer(body, notify))


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    if "--test" in sys.argv:
        print("[INFO] Test mode — sending dummy email...")
        send_test_email()
        return

    print("[flight-monitor] Run started")

    if SUPABASE_URL and SUPABASE_KEY:
        try:
            subscribers = fetch_subscribers()
            print(f"[flight-monitor] Subscribers: {len(subscribers)}")
        except Exception as e:
            print(f"[WARN] Supabase error: {e} — falling back to NOTIFY_EMAIL")
            notify = os.getenv("NOTIFY_EMAIL")
            if notify:
                subscribers = [{"email": notify, "max_price": 60, "origin": ORIGIN}]
            else:
                print("[flight-monitor] No fallback email — skipping")
                return
    else:
        notify = os.getenv("NOTIFY_EMAIL")
        if not notify:
            print("[flight-monitor] No Supabase and no NOTIFY_EMAIL — skipping")
            return
        subscribers = [{"email": notify, "max_price": 60, "origin": ORIGIN}]

    if not subscribers:
        print("[flight-monitor] No subscribers — exiting")
        return

    max_price_global = max(int(s["max_price"]) for s in subscribers)
    trips            = candidate_trips()
    destinations     = DESTINATIONS[:]
    random.shuffle(destinations)
    seen             = load_seen()
    today_str        = date.today().isoformat()
    emails_sent      = 0

    # Build all (destination, depart, ret) tasks
    tasks = [
        (dest, depart, ret, max_price_global)
        for dest in destinations
        for depart, ret in trips
    ]
    print(f"[flight-monitor] {len(tasks)} route/date combos — {MAX_WORKERS} parallel workers")

    # Parallel search
    found_deals: list[dict] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(check_route, t): t for t in tasks}
        for future in as_completed(futures):
            result = future.result()
            if result:
                print(f"[DEAL] {ORIGIN}→{result['destination']} "
                      f"{result['depart']}–{result['return']}: ${result['price']:.0f}")
                found_deals.append(result)

    print(f"[flight-monitor] Total deals ≤ ${max_price_global}: {len(found_deals)}")

    for sub in subscribers:
        email     = sub["email"]
        max_price = int(sub["max_price"])
        new_deals = []

        for deal in found_deals:
            if deal["price"] > max_price:
                continue
            key   = deal_key(email, deal["destination"], deal["depart"], deal["return"])
            entry = seen.get(key)
            if isinstance(entry, dict):
                if entry.get("date") == today_str and deal["price"] > entry.get("price", 0) * 0.90:
                    continue
            elif entry == today_str:
                continue
            new_deals.append(deal)
            seen[key] = {"date": today_str, "price": deal["price"]}

        if not new_deals:
            print(f"[flight-monitor] No new deals for {email}")
            continue

        if len(new_deals) <= 3:
            for deal in new_deals:
                subject, body = format_email([deal], max_price)
                send_email(email, subject, add_unsubscribe_footer(body, email))
                emails_sent += 1
        else:
            subject, body = format_email(new_deals, max_price)
            send_email(email, subject, add_unsubscribe_footer(body, email))
            emails_sent += 1

    save_seen(seen)
    print(f"[flight-monitor] Done. Emails sent: {emails_sent}")


if __name__ == "__main__":
    main()
