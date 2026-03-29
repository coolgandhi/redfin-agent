#!/usr/bin/env python3
"""
Redfin Real Estate Agent
------------------------
Scans Gmail for Redfin emails, extracts listings from the HTML body,
enriches with school data from Redfin listing pages, and syncs to
Google Sheets with address-based deduplication.

First run : processes the 10 most recent Redfin emails.
Subsequent: processes only unread Redfin emails newer than the most
            recent date already stored in the sheet.
"""

import base64
import os
import re
import time
import json
from datetime import datetime, timezone
from email import message_from_bytes
from html.parser import HTMLParser

from bs4 import BeautifulSoup
import requests

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ─── CONFIG ──────────────────────────────────────────────────────────────────

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/spreadsheets",
]

# Path to your OAuth credentials file downloaded from Google Cloud Console
CREDENTIALS_FILE = "credentials.json"
# Where the OAuth token is cached after first login
TOKEN_FILE = "token.json"

# Google Sheet ID — get this from the URL of your sheet:
# https://docs.google.com/spreadsheets/d/SHEET_ID_HERE/edit
SHEET_ID = "1rLAIiye9GeJ7EYQs9Xe16k_yOsiiKCZ8PQZjuJW0hH8"
SHEET_TAB = "Listings"          # Tab name inside the spreadsheet

# How many emails to process on first run
FIRST_RUN_LIMIT = 10

# Polite delay between Redfin page fetches (seconds)
FETCH_DELAY = 1.5

# Column order written to the sheet
COLUMNS = [
    "Date", "Address", "City", "Zip", "Status",
    "Price", "Beds", "Baths", "SqFt", "Price/SqFt",
    "School1", "Type1", "Rating1",
    "School2", "Type2", "Rating2",
    "School3", "Type3", "Rating3",
    "URL",
]

# ─── AUTH ────────────────────────────────────────────────────────────────────

def get_google_services():
    """Authenticate and return (gmail_service, sheets_service)."""
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    gmail   = build("gmail",  "v1", credentials=creds)
    sheets  = build("sheets", "v4", credentials=creds)
    return gmail, sheets

# ─── GMAIL ───────────────────────────────────────────────────────────────────

def get_html_body(msg_payload):
    """Recursively find and decode the text/html MIME part."""
    mime_type = msg_payload.get("mimeType", "")
    if mime_type == "text/html":
        data = msg_payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    if mime_type.startswith("multipart/"):
        for part in msg_payload.get("parts", []):
            result = get_html_body(part)
            if result:
                return result
    return None


def fetch_emails(gmail, query, max_results=None):
    """Return list of full message dicts matching query."""
    params = {"userId": "me", "q": query}
    if max_results:
        params["maxResults"] = max_results

    result = gmail.users().messages().list(**params).execute()
    messages = result.get("messages", [])

    full_messages = []
    for m in messages:
        msg = gmail.users().messages().get(
            userId="me", id=m["id"], format="full"
        ).execute()
        full_messages.append(msg)
    return full_messages


def mark_as_read(gmail, message_id):
    """Remove the UNREAD label from a message."""
    gmail.users().messages().modify(
        userId="me",
        id=message_id,
        body={"removeLabelIds": ["UNREAD"]},
    ).execute()


def parse_email_date(msg):
    """Return a datetime (UTC) from a Gmail message's internalDate."""
    ts_ms = int(msg.get("internalDate", 0))
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)

# ─── LISTING EXTRACTION ──────────────────────────────────────────────────────

# Redfin email listing cards look like:
#   <a href="https://www.redfin.com/CA/...">
#     <span>$1,050,000</span>  3 beds  2 baths  1,400 sq ft
#     123 Main St, San Carlos, CA 94070
#   </a>
# The structure varies slightly; we use multiple heuristic passes.

PRICE_RE    = re.compile(r"\$[\d,]+")
BEDS_RE     = re.compile(r"(\d+(?:\.\d+)?)\s*(?:bed|bd)", re.I)
BATHS_RE    = re.compile(r"(\d+(?:\.\d+)?)\s*(?:bath|ba)", re.I)
SQFT_RE     = re.compile(r"([\d,]+)\s*sq\.?\s*ft", re.I)
ADDRESS_RE  = re.compile(
    r"\d+[^,\n]+,\s*[A-Za-z\s]+,\s*[A-Z]{2}\s*\d{5}"
)
ZIP_RE      = re.compile(r"\b(\d{5})\b")
STATUS_RE   = re.compile(
    r"\b(New|Pending|Open House|Price Drop|Sold|Back on Market|Active|Coming Soon)\b",
    re.I,
)
REDFIN_URL_RE = re.compile(
    r"https://www\.redfin\.com/[A-Z]{2}/[^\"'\s>]+"
)
REDFIN_TRACKING_RE = re.compile(
    r"https://redmail\d*\.redfin\.com/[^\"'\s>]+"
)

_redirect_cache = {}

def resolve_tracking_url(url):
    """Follow a Redfin email tracking redirect once and return the destination URL."""
    if url in _redirect_cache:
        return _redirect_cache[url]
    try:
        resp = requests.get(url, headers=HEADERS, allow_redirects=False, timeout=10)
        dest = resp.headers.get("Location", "")
        _redirect_cache[url] = dest
        return dest
    except Exception:
        return ""


def _clean(text):
    return re.sub(r"\s+", " ", text).strip()


def parse_listings_from_html(html, email_date):
    """
    Parse listing cards from a Redfin email HTML body.
    Returns a list of listing dicts.
    """
    soup = BeautifulSoup(html, "lxml")
    listings = []

    # Each listing is typically inside an <a> tag pointing to redfin.com/STATE/...
    # Redfin emails may use tracking redirects (redmail3.redfin.com) instead of direct URLs.
    def _all_listing_anchors():
        seen_urls = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if REDFIN_URL_RE.match(href):
                clean_url = href.split("?")[0]
            elif REDFIN_TRACKING_RE.match(href):
                dest = resolve_tracking_url(href)
                if dest and REDFIN_URL_RE.match(dest):
                    clean_url = dest.split("?")[0]
                else:
                    continue
            else:
                continue
            # Deduplicate by resolved URL, preferring anchors that have text
            # (address-text anchors) over empty/image anchors.
            anchor_text = _clean(a.get_text(" ", strip=True))
            if clean_url in seen_urls:
                continue
            if anchor_text:
                seen_urls.add(clean_url)
                yield a, clean_url

    for a_tag, url in _all_listing_anchors():
        # Skip non-listing URLs (feeds, settings, etc.)
        if not re.search(r"/home/\d+|/[A-Z]{2}/[^/]+/[^/]+/\d+", url):
            continue

        # Price/beds/baths are in sibling rows of the card <table>, not inside
        # the <a> tag itself. Walk up to find the enclosing card.
        card = a_tag
        for _ in range(6):
            card = card.parent
            card_text = _clean(card.get_text(" ", strip=True))
            if "$" in card_text and len(card_text) > 20:
                break
        text = card_text

        if len(text) < 15:
            continue

        # Use card text for numeric fields; use the <a> tag text for the address
        # to avoid sqft fragments corrupting the address regex match.
        addr_text = _clean(a_tag.get_text(" ", strip=True))

        price_m   = PRICE_RE.search(text)
        beds_m    = BEDS_RE.search(text)
        baths_m   = BATHS_RE.search(text)
        sqft_m    = SQFT_RE.search(text)
        addr_m    = ADDRESS_RE.search(addr_text)
        status_m  = STATUS_RE.search(text)

        price_str = price_m.group(0).replace(",", "").replace("$", "") if price_m else ""
        beds      = beds_m.group(1)  if beds_m  else ""
        baths     = baths_m.group(1) if baths_m else ""
        sqft_str  = sqft_m.group(1).replace(",", "") if sqft_m else ""

        # Try to compute price/sqft
        try:
            price_psf = round(int(price_str) / int(sqft_str)) if price_str and sqft_str else ""
        except (ValueError, ZeroDivisionError):
            price_psf = ""

        # Address parsing
        address_raw = addr_m.group(0) if addr_m else ""
        city, zip_code = "", ""
        if address_raw:
            parts = [p.strip() for p in address_raw.split(",")]
            # e.g. ['123 Main St', 'San Carlos', 'CA 94070']
            if len(parts) >= 3:
                state_zip = parts[-1]
                city      = parts[-2]
                zip_m     = ZIP_RE.search(state_zip)
                zip_code  = zip_m.group(1) if zip_m else ""
            elif len(parts) == 2:
                city = parts[-1]

        street = parts[0] if address_raw and len(parts) >= 1 else ""
        status = status_m.group(1).title() if status_m else "Active"

        if not street and not price_str:
            continue   # Not enough data

        listings.append({
            "date":      email_date.strftime("%Y-%m-%d"),
            "address":   street,
            "city":      city,
            "zip":       zip_code,
            "status":    status,
            "price":     price_str,
            "beds":      beds,
            "baths":     baths,
            "sqft":      sqft_str,
            "price_psf": str(price_psf) if price_psf else "",
            "url":       url,
            # Schools filled in later
            "school1": "", "type1": "", "rating1": "",
            "school2": "", "type2": "", "rating2": "",
            "school3": "", "type3": "", "rating3": "",
        })

    # Deduplicate within this email by URL
    seen = set()
    unique = []
    for l in listings:
        if l["url"] not in seen:
            seen.add(l["url"])
            unique.append(l)
    return unique

# ─── SCHOOL ENRICHMENT ───────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0 Safari/537.36"
    )
}


def fetch_schools(listing_url):
    """
    Fetch the Redfin listing page and extract up to 3 nearby schools
    with their name, type, and GreatSchools rating.
    Returns list of dicts: [{name, type, rating}, ...]
    """
    try:
        time.sleep(FETCH_DELAY)
        resp = requests.get(listing_url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return []
        soup = BeautifulSoup(resp.text, "lxml")

        schools = []
        # School rows are <div class="flex align-center"> containing:
        # "Taft Elementary School Public K-5 • Assigned • 0.4mi 4/10"
        # Environmental factors (Flood/Fire/Heat/Wind/Air) also have X/10
        # but live in <div class="ListItem__content ..."> — we exclude those.
        score_re = re.compile(r"(\d+)\s*/\s*10")
        school_type_re = re.compile(
            r"\b(Elementary|Middle|High|K-\d+|K-12|Charter|Private|PreK)\b", re.I
        )
        env_re = re.compile(
            r"\b(Flood|Fire|Heat|Wind|Air|Storm|Drought)\s+Factor\b", re.I
        )

        for div in soup.find_all("div"):
            classes = " ".join(div.get("class") or [])
            # Target the school row divs; skip environmental-factor divs
            if "flex" not in classes or "align-center" not in classes:
                continue
            text = _clean(div.get_text(" ", strip=True))
            if env_re.search(text):
                continue
            score_m = score_re.search(text)
            if not score_m:
                continue
            type_m = school_type_re.search(text)
            if not type_m:
                continue
            rating = score_m.group(1)
            kind   = type_m.group(1).title()
            # School name is the leading text before the school type keyword
            name = text[:type_m.start()].strip().rstrip("•·-– ")
            if name and len(name) > 3:
                schools.append({"name": name, "type": kind, "rating": rating})
            if len(schools) == 3:
                break

        return schools
    except Exception as e:
        print(f"    ⚠ Could not fetch schools for {listing_url}: {e}")
        return []

# ─── GOOGLE SHEETS ───────────────────────────────────────────────────────────

def ensure_header(sheets):
    """Write the header row if the sheet is empty."""
    result = sheets.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=f"{SHEET_TAB}!A1:T1",
    ).execute()
    if not result.get("values"):
        sheets.spreadsheets().values().update(
            spreadsheetId=SHEET_ID,
            range=f"{SHEET_TAB}!A1",
            valueInputOption="RAW",
            body={"values": [COLUMNS]},
        ).execute()


def read_sheet(sheets):
    """
    Return (rows, address_to_row_index) where rows is a list of lists,
    and address_to_row_index maps normalised address → 1-based sheet row number.
    Row 1 is the header; data starts at row 2.
    """
    result = sheets.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=f"{SHEET_TAB}!A:T",
    ).execute()
    rows = result.get("values", [])
    addr_idx = {}
    for i, row in enumerate(rows[1:], start=2):   # skip header
        if row:
            addr_key = normalise_address(row[1] if len(row) > 1 else "")
            if addr_key:
                addr_idx[addr_key] = i
    return rows, addr_idx


def normalise_address(addr):
    return addr.lower().strip()


def get_latest_date(rows):
    """
    Return the most recent date string found in column A (skip header).
    Returns None if the sheet is empty.
    """
    dates = []
    for row in rows[1:]:
        if row and row[0]:
            try:
                dates.append(datetime.strptime(row[0], "%Y-%m-%d"))
            except ValueError:
                pass
    return max(dates) if dates else None


def listing_to_row(l):
    return [
        l["date"], l["address"], l["city"], l["zip"], l["status"],
        l["price"], l["beds"], l["baths"], l["sqft"], l["price_psf"],
        l["school1"], l["type1"], l["rating1"],
        l["school2"], l["type2"], l["rating2"],
        l["school3"], l["type3"], l["rating3"],
        l["url"],
    ]


def write_listings(sheets, listings, addr_to_row):
    """
    For each listing:
      - If address not in sheet → batch append.
      - If address exists and new date > stored date → overwrite that row.
    Returns (appended_count, updated_count).
    """
    # Re-read sheet to get latest state before writing
    rows, addr_idx = read_sheet(sheets)

    rows_to_append = []
    update_requests = []

    for l in listings:
        key = normalise_address(l["address"])
        if not key:
            continue

        row_data = listing_to_row(l)

        if key not in addr_idx:
            rows_to_append.append(row_data)
        else:
            # Check if new email date is more recent than stored date
            existing_row = rows[addr_idx[key] - 1]
            stored_date_str = existing_row[0] if existing_row else ""
            try:
                stored_date = datetime.strptime(stored_date_str, "%Y-%m-%d")
                new_date    = datetime.strptime(l["date"], "%Y-%m-%d")
                if new_date > stored_date:
                    row_num = addr_idx[key]
                    update_requests.append({
                        "range": f"{SHEET_TAB}!A{row_num}:T{row_num}",
                        "values": [row_data],
                    })
            except ValueError:
                pass   # Can't parse stored date — skip

    # Batch append all new rows in one API call
    appended = 0
    if rows_to_append:
        sheets.spreadsheets().values().append(
            spreadsheetId=SHEET_ID,
            range=f"{SHEET_TAB}!A1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": rows_to_append},
        ).execute()
        appended = len(rows_to_append)

    # Batch update existing rows using batchUpdate
    updated = 0
    if update_requests:
        sheets.spreadsheets().values().batchUpdate(
            spreadsheetId=SHEET_ID,
            body={
                "valueInputOption": "USER_ENTERED",
                "data": update_requests,
            },
        ).execute()
        updated = len(update_requests)

    return appended, updated

# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    print("🏠 Redfin Real Estate Agent starting…\n")

    gmail, sheets = get_google_services()
    ensure_header(sheets)
    existing_rows, addr_to_row = read_sheet(sheets)
    latest_date = get_latest_date(existing_rows)

    # ── Determine scan mode ──────────────────────────────────────────────────
    if latest_date is None:
        mode = "first_run"
        print(f"📋 Mode: FIRST RUN — fetching {FIRST_RUN_LIMIT} most recent Redfin emails")
        query    = "from:redfin.com"
        messages = fetch_emails(gmail, query, max_results=FIRST_RUN_LIMIT)
    else:
        mode = "periodic"
        after_str = latest_date.strftime("%Y/%m/%d")
        print(f"📋 Mode: PERIODIC RUN — fetching unread Redfin emails after {after_str}")
        query    = f"from:redfin.com is:unread after:{after_str}"
        messages = fetch_emails(gmail, query)

    print(f"📧 Found {len(messages)} email(s) to process\n")

    all_listings = []

    # ── Extract listings from each email ────────────────────────────────────
    for idx, msg in enumerate(messages, 1):
        subject = next(
            (h["value"] for h in msg["payload"]["headers"] if h["name"] == "Subject"),
            "(no subject)"
        )
        email_date = parse_email_date(msg)
        print(f"  [{idx}/{len(messages)}] {email_date.date()} — {subject[:70]}")

        html = get_html_body(msg["payload"])
        if not html:
            print("    ⚠ No HTML body found, skipping")
            continue

        listings = parse_listings_from_html(html, email_date)
        print(f"    → {len(listings)} listing(s) extracted")
        all_listings.extend(listings)

    # ── Deduplicate across emails (keep most recent per address) ─────────────
    addr_best = {}
    for l in all_listings:
        key = normalise_address(l["address"])
        if not key:
            continue
        if key not in addr_best:
            addr_best[key] = l
        else:
            existing_d = datetime.strptime(addr_best[key]["date"], "%Y-%m-%d")
            new_d      = datetime.strptime(l["date"], "%Y-%m-%d")
            if new_d > existing_d:
                addr_best[key] = l

    deduped = list(addr_best.values())
    print(f"\n📊 {len(all_listings)} total listings → {len(deduped)} unique addresses\n")

    # ── Enrich with school data ───────────────────────────────────────────────
    for i, l in enumerate(deduped, 1):
        print(f"  🏫 Fetching schools for [{i}/{len(deduped)}] {l['address']}")
        schools = fetch_schools(l["url"])
        for j, s in enumerate(schools[:3], 1):
            l[f"school{j}"] = s["name"]
            l[f"type{j}"]   = s["type"]
            l[f"rating{j}"] = s["rating"]
        if schools:
            names = ", ".join(s["name"][:25] for s in schools)
            print(f"    → {len(schools)} school(s): {names}")
        else:
            print("    → no schools found")

    # ── Write to Sheets ───────────────────────────────────────────────────────
    print(f"\n📝 Writing to Google Sheets…")
    appended, updated = write_listings(sheets, deduped, addr_to_row)
    print(f"   ✅ {appended} new row(s) added, {updated} row(s) updated")

    # ── Mark emails as read ───────────────────────────────────────────────────
    if mode == "periodic":
        print(f"\n✉️  Marking {len(messages)} email(s) as read…")
        for msg in messages:
            mark_as_read(gmail, msg["id"])

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"""
╔══════════════════════════════════════╗
║           Run complete               ║
╠══════════════════════════════════════╣
║  Mode          : {mode:<20} ║
║  Emails scanned: {len(messages):<20} ║
║  Listings found: {len(all_listings):<20} ║
║  Unique addrs  : {len(deduped):<20} ║
║  Rows added    : {appended:<20} ║
║  Rows updated  : {updated:<20} ║
╚══════════════════════════════════════╝
""")


if __name__ == "__main__":
    main()
