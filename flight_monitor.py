#!/usr/bin/env python3
"""
Flight Deal Monitor — IAD → Anywhere in the US
Uses Aviasales Data API to find round-trip deals per subscriber's price cap,
then sends personalized email alerts.
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
MIN_NIGHTS = 2
CURRENCY = "usd"

SHORT_WINDOW_START = 7
SHORT_WINDOW_END   = 28
LONG_WINDOW_START  = 30
LONG_WINDOW_END    = 90

SEEN_FILE = Path(__file__).parent / "seen_deals.json"

WEEKEND_DEPART_DAYS = {3, 4}
WEEKEND_RETURN_DAYS = {6, 0}

AVIASALES_API_URL = "https://api.travelpayouts.com/v2/prices/latest"
SUPABASE_URL      = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY      = os.getenv("SUPABASE_SERVICE_KEY", "")

US_AIRPORTS = {
    "ATL", "LAX", "ORD", "DFW", "DEN", "JFK", "SFO", "SEA", "LAS", "MCO",
    "EWR", "CLT", "PHX", "MIA", "IAH", "BOS", "MSP", "DTW", "FLL", "PHL",
    "LGA", "BWI", "SLC", "SAN", "TPA", "PDX", "HNL", "MDW", "STL", "OAK",
    "MCI", "RDU", "CLE", "AUS", "BNA", "PIT", "MSY", "SMF", "SJC", "DAL",
    "HOU", "OGG", "ABQ", "BUF", "CVG", "IND", "JAX", "MKE", "OMA", "SAT",
    "TUL", "BOI", "GEG", "GSP", "ICT", "LIT", "MEM", "OKC", "ONT", "PBI",
    "RSW", "SDF", "SNA", "SRQ", "TUS", "XNA",
}


# ── Subscribers ────────────────────────────────────────────────────────────────

def fetch_subscribers() -> list[dict]:
    """Return all active subscribers from Supabase."""
    url = f"{SUPABASE_URL}/rest/v1/subscribers?active=eq.true&select=email,max_price,origin"
    req = urllib.request.Request(url, headers={
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


# ── Date helpers ───────────────────────────────────────────────────────────────

def is_weekend_trip(depart: date, return_date: date) -> bool:
    return (
        depart.weekday() in WEEKEND_DEPART_DAYS
        and return_date.weekday() in WEEKEND_RETURN_DAYS
    )


# ── Aviasales Data API ─────────────────────────────────────────────────────────

def fetch_raw_prices(origin: str) -> list[dict]:
    token = os.environ["AVIASALES_TOKEN"]
    params = urllib.parse.urlencode({
        "origin":   origin,
        "currency": CURRENCY,
        "one_way":  "false",
        "limit":    1000,
        "token":    token,
    })
    url = f"{AVIASALES_API_URL}?{params}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        body = json.loads(resp.read())
    return body.get("data", [])


def parse_deals(raw: list[dict], max_price: int) -> list[dict]:
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
            if price > max_price:
                continue

            depart_str = item.get("depart_date", "")
            return_str = item.get("return_date", "")
            if not depart_str or not return_str:
                continue

            depart = date.fromisoformat(depart_str)
            ret    = date.fromisoformat(return_str)

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
                "book_url": (
                    f"https://www.google.com/flights#flt="
                    f"{ORIGIN}.{destination}.{depart.isoformat()}*"
                    f"{destination}.{ORIGIN}.{ret.isoformat()};c:USD;e:1;s:0*1;sd:1;t:f"
                ),
            })
        except (KeyError, ValueError, TypeError) as e:
            print(f"[WARN] Skipping item: {e}")

    return deals


# ── Deduplication ──────────────────────────────────────────────────────────────

def load_seen() -> dict:
    if not SEEN_FILE.exists():
        return {}
    return json.loads(SEEN_FILE.read_text())


def save_seen(seen: dict) -> None:
    SEEN_FILE.write_text(json.dumps(seen, indent=2, default=str))


def deal_key(email: str, deal: dict) -> str:
    return f"{email}_{deal['destination']}_{deal['depart']}_{deal['return']}"


# ── Email ──────────────────────────────────────────────────────────────────────

def format_email(deals: list[dict], max_price: int) -> tuple[str, str]:
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
        subject = f"{prefix}✈ {count} Flight Deals from {ORIGIN} (≤ ${max_price}/pp)"

    lines = [
        f"Flight deals from Dulles (IAD) under ${max_price}/person round trip:",
        "⚠ Prices are cached — act fast, they may have changed. Always verify before booking.\n",
    ]
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


def add_unsubscribe_footer(body: str, email: str) -> str:
    base_url = os.getenv("APP_URL", "https://flight-monitor-ui.vercel.app")
    unsubscribe_url = f"{base_url}/api/unsubscribe?email={urllib.parse.quote(email)}"
    return body + f"\n\n─────────────────────────────\nDon't want these alerts? Unsubscribe: {unsubscribe_url}"


def send_email(to: str, subject: str, body: str) -> None:
    gmail_address = os.environ["GMAIL_ADDRESS"]
    app_password  = os.environ["GMAIL_APP_PASSWORD"]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = gmail_address
    msg["To"]      = to
    msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.ehlo()
        server.starttls()
        server.login(gmail_address, app_password)
        server.sendmail(gmail_address, to, msg.as_string())

    print(f"[OK] Email → {to}: {subject}")


# ── Test mode ──────────────────────────────────────────────────────────────────

def send_test_email() -> None:
    fake = {
        "origin":      "IAD",
        "destination": "MIA",
        "depart":      date.today() + timedelta(days=9),
        "return":      date.today() + timedelta(days=11),
        "nights":      2,
        "price":       38.0,
        "is_weekend":  True,
        "stops":       0,
        "book_url":    "https://www.google.com/flights",
    }
    notify = os.environ["NOTIFY_EMAIL"]
    subject, body = format_email([fake], 60)
    send_email(notify, "[TEST] " + subject, body)


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    if "--test" in sys.argv:
        print("[INFO] Test mode — sending dummy email...")
        send_test_email()
        return

    # Fetch all active subscribers
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            subscribers = fetch_subscribers()
            print(f"[INFO] Subscribers: {len(subscribers)}")
        except Exception as e:
            print(f"[WARN] Could not fetch subscribers: {e} — falling back to NOTIFY_EMAIL")
            subscribers = [{
                "email":     os.environ["NOTIFY_EMAIL"],
                "max_price": int(os.getenv("MAX_PRICE", "60")),
                "origin":    ORIGIN,
            }]
    else:
        subscribers = [{
            "email":     os.environ["NOTIFY_EMAIL"],
            "max_price": int(os.getenv("MAX_PRICE", "60")),
            "origin":    ORIGIN,
        }]

    print("[INFO] Fetching latest prices from Aviasales...")
    try:
        raw = fetch_raw_prices(ORIGIN)
    except Exception as e:
        print(f"[ERROR] Failed to fetch prices: {e}")
        sys.exit(1)

    print(f"[INFO] Raw results: {len(raw)}")

    seen = load_seen()
    today_str = date.today().isoformat()
    emails_sent = 0

    for sub in subscribers:
        email     = sub["email"]
        max_price = sub["max_price"]

        deals = parse_deals(raw, max_price)
        new_deals = []
        for deal in deals:
            key = deal_key(email, deal)
            if seen.get(key) != today_str:
                new_deals.append(deal)
                seen[key] = today_str

        if not new_deals:
            print(f"[INFO] No new deals for {email}")
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
    print(f"[INFO] Done. Emails sent: {emails_sent}")


if __name__ == "__main__":
    main()
