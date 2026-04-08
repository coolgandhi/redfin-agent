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
import ssl
import subprocess
import sys
import time
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email import message_from_bytes
from html.parser import HTMLParser

from bs4 import BeautifulSoup
import requests

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ─── CONFIG ──────────────────────────────────────────────────────────────────

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/spreadsheets",
]

# Resolve paths relative to this script so cron can run it from any directory
_DIR = os.path.dirname(os.path.abspath(__file__))

# When launched by launchd (no TTY), redirect stdout/stderr to the log file
# so macOS sandbox restrictions on StandardOutPath don't prevent the job from running.
if not sys.stdout.isatty():
    _log_fh = open(os.path.join(_DIR, "agent.log"), "a", buffering=1)
    sys.stdout = _log_fh
    sys.stderr = _log_fh

# Path to your OAuth credentials file downloaded from Google Cloud Console
CREDENTIALS_FILE = os.path.join(_DIR, "credentials.json")
# Where the OAuth token is cached after first login
TOKEN_FILE = os.path.join(_DIR, "token.json")

# Google Sheet ID — get this from the URL of your sheet:
# https://docs.google.com/spreadsheets/d/SHEET_ID_HERE/edit
SHEET_ID = "1rLAIiye9GeJ7EYQs9Xe16k_yOsiiKCZ8PQZjuJW0hH8"
SHEET_TAB = "Listings"          # Tab name inside the spreadsheet
RUNS_TAB  = "Runs"              # Tab for run history / observability

# How many emails to process on first run
FIRST_RUN_LIMIT = 10

# Polite delay between Redfin page fetches (seconds)
FETCH_DELAY = 1.5

# Re-fetch school data if it was last updated more than this many days ago
SCHOOL_REFRESH_DAYS = 90

# Number of parallel workers for school page fetches
MAX_SCHOOL_WORKERS = 5

# Column order written to the sheet
COLUMNS = [
    "Date", "Address", "City", "Zip", "Status",
    "Price", "Beds", "Baths", "SqFt", "Price/SqFt",
    "School1", "Type1", "Rating1",
    "School2", "Type2", "Rating2",
    "School3", "Type3", "Rating3",
    "URL",
    "Price History",   # col U — pipe-separated "YYYY-MM-DD:price" entries
    "Schools Updated", # col V — date school data was last fetched
    "HOA",             # col W — monthly HOA fee if present (condos/HOA communities)
    "First Seen",      # col X — date listing was first added to sheet
    "Days on Market",  # col Y — days since First Seen (recomputed each run)
    "Price Drops",     # col Z — number of price reductions
    "Total Drop $",    # col AA — original price minus current price
    "HOA-Adj Price",   # col AB — price + HOA/mo * 12 * 25 (normalized cost)
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
    _with_retry(lambda: gmail.users().messages().modify(
        userId="me",
        id=message_id,
        body={"removeLabelIds": ["UNREAD"]},
    ).execute())


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
            "price_history":   "",
            "schools_updated": "",
            "hoa":             "",
            "first_seen":      "",
            "days_on_market":  "",
            "price_drops":     "",
            "total_drop":      "",
            "hoa_adj_price":   "",
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


def fetch_listing_data(listing_url):
    """
    Fetch the Redfin listing page and extract:
    - Up to 3 nearby schools with name, type, and GreatSchools rating
    - HOA monthly fee if present
    Returns {"schools": [{name, type, rating}, ...], "hoa": ""}
    """
    try:
        time.sleep(FETCH_DELAY)
        resp = requests.get(listing_url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return {"schools": [], "hoa": ""}
        soup = BeautifulSoup(resp.text, "lxml")

        # ── Schools ──────────────────────────────────────────────────────────
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

        # ── HOA ──────────────────────────────────────────────────────────────
        hoa = ""
        page_text = soup.get_text(" ")
        hoa_m = re.search(
            r'HOA\s+(?:Dues?|Fee)?\s*[:\-]?\s*\$\s*([\d,]+)', page_text, re.I
        )
        if hoa_m:
            hoa = "$" + hoa_m.group(1).replace(",", "")

        return {"schools": schools, "hoa": hoa}
    except Exception as e:
        print(f"    ⚠ Could not fetch listing data for {listing_url}: {e}")
        return {"schools": [], "hoa": ""}

# ─── GOOGLE SHEETS ───────────────────────────────────────────────────────────

def _with_retry(fn, retries=4):
    """Call fn() (which must call .execute() internally) with exponential backoff."""
    for attempt in range(retries):
        try:
            return fn()
        except (ssl.SSLEOFError, ssl.SSLError, OSError) as e:
            if attempt == retries - 1:
                raise
            wait = 2 ** attempt
            print(f"    ⚠ Network error ({e.__class__.__name__}), retrying in {wait}s…")
            time.sleep(wait)
        except HttpError as e:
            if e.resp.status in (429, 500, 502, 503, 504) and attempt < retries - 1:
                wait = 2 ** attempt
                print(f"    ⚠ API error {e.resp.status}, retrying in {wait}s…")
                time.sleep(wait)
            else:
                raise

def ensure_header(sheets):
    """Write the header row if the sheet is empty."""
    result = _with_retry(lambda: sheets.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=f"{SHEET_TAB}!A1:AB1",
    ).execute())
    if not result.get("values"):
        _with_retry(lambda: sheets.spreadsheets().values().update(
            spreadsheetId=SHEET_ID,
            range=f"{SHEET_TAB}!A1",
            valueInputOption="RAW",
            body={"values": [COLUMNS]},
        ).execute())


def read_sheet(sheets):
    """
    Return (rows, address_to_row_index) where rows is a list of lists,
    and address_to_row_index maps normalised address → 1-based sheet row number.
    Row 1 is the header; data starts at row 2.
    """
    result = _with_retry(lambda: sheets.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=f"{SHEET_TAB}!A:AB",
    ).execute())
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
        l.get("price_history", ""),
        l.get("schools_updated", ""),
        l.get("hoa", ""),
        l.get("first_seen", ""),
        l.get("days_on_market", ""),
        l.get("price_drops", ""),
        l.get("total_drop", ""),
        l.get("hoa_adj_price", ""),
    ]


def _schools_stale(date_str):
    """Return True if school data is missing or older than SCHOOL_REFRESH_DAYS."""
    if not date_str:
        return True
    try:
        from datetime import timedelta
        d = datetime.strptime(date_str, "%Y-%m-%d")
        return (datetime.now() - d).days > SCHOOL_REFRESH_DAYS
    except ValueError:
        return True


def _compute_dom(first_seen_str):
    """Days between first_seen and today."""
    if not first_seen_str:
        return ""
    try:
        return str((datetime.now() - datetime.strptime(first_seen_str, "%Y-%m-%d")).days)
    except ValueError:
        return ""


def _compute_price_stats(current_price_str, price_history_str):
    """Return (num_drops, total_drop) from price history.
    price_history entries are newest-first: "DATE:PRICE | DATE:PRICE"
    """
    if not current_price_str:
        return "0", "0"
    try:
        current = int(current_price_str)
        if not price_history_str:
            return "0", "0"
        entries = [e.strip() for e in price_history_str.split("|") if e.strip()]
        # Build chronological price list: oldest → newest → current
        past_prices = [int(e.split(":")[-1].strip()) for e in reversed(entries)]
        chrono = past_prices + [current]
        drops = sum(1 for i in range(1, len(chrono)) if chrono[i] < chrono[i - 1])
        total_drop = chrono[0] - current   # positive = price reduced overall
        return str(drops), str(total_drop) if total_drop > 0 else "0"
    except (ValueError, IndexError):
        return "0", "0"


def _hoa_adj(price_str, hoa_str):
    """Capitalise HOA cost into purchase price equivalent (HOA/mo × 12 × 25)."""
    try:
        price = int(price_str) if price_str else 0
        hoa_num = int(re.sub(r"[^\d]", "", hoa_str)) if hoa_str else 0
        return str(price + hoa_num * 12 * 25) if price else ""
    except (ValueError, AttributeError):
        return ""


def write_listings(sheets, listings, rows, addr_idx):
    """
    For each listing:
      - If address not in sheet → batch append.
      - If address exists and new date > stored date → overwrite that row.
    Returns (appended_count, updated_count).
    """

    rows_to_append = []
    update_requests = []

    for l in listings:
        key = normalise_address(l["address"])
        if not key:
            continue

        if key not in addr_idx:
            l["first_seen"]     = l["date"]
            l["days_on_market"] = _compute_dom(l["date"])
            l["price_drops"]    = "0"
            l["total_drop"]     = "0"
            l["hoa_adj_price"]  = _hoa_adj(l["price"], l["hoa"])
            rows_to_append.append(listing_to_row(l))
        else:
            # Check if new email date is more recent than stored date
            existing_row     = rows[addr_idx[key] - 1]
            stored_date_str  = existing_row[0]  if len(existing_row) > 0  else ""
            stored_price     = existing_row[5]  if len(existing_row) > 5  else ""
            stored_history   = existing_row[20] if len(existing_row) > 20 else ""
            stored_first_seen = existing_row[23] if len(existing_row) > 23 else ""
            try:
                stored_date = datetime.strptime(stored_date_str, "%Y-%m-%d")
                new_date    = datetime.strptime(l["date"], "%Y-%m-%d")
                if new_date > stored_date:
                    # Prepend old price to history if the price changed
                    if stored_price and stored_price != l["price"]:
                        entry = f"{stored_date_str}:{stored_price}"
                        l["price_history"] = (
                            f"{entry} | {stored_history}" if stored_history else entry
                        )
                    else:
                        l["price_history"] = stored_history
                    # Preserve first seen; recompute derived fields
                    l["first_seen"]     = stored_first_seen or stored_date_str
                    l["days_on_market"] = _compute_dom(l["first_seen"])
                    l["price_drops"], l["total_drop"] = _compute_price_stats(
                        l["price"], l["price_history"]
                    )
                    l["hoa_adj_price"]  = _hoa_adj(l["price"], l["hoa"])
                    row_num = addr_idx[key]
                    update_requests.append({
                        "range": f"{SHEET_TAB}!A{row_num}:AB{row_num}",
                        "values": [listing_to_row(l)],
                    })
            except ValueError:
                pass   # Can't parse stored date — skip

    # Batch append all new rows in one API call
    appended = 0
    if rows_to_append:
        _with_retry(lambda: sheets.spreadsheets().values().append(
            spreadsheetId=SHEET_ID,
            range=f"{SHEET_TAB}!A1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": rows_to_append},
        ).execute())
        appended = len(rows_to_append)

    # Batch update existing rows using batchUpdate
    updated = 0
    if update_requests:
        _with_retry(lambda: sheets.spreadsheets().values().batchUpdate(
            spreadsheetId=SHEET_ID,
            body={
                "valueInputOption": "USER_ENTERED",
                "data": update_requests,
            },
        ).execute())
        updated = len(update_requests)

    return appended, updated

# ─── RUN LOGGING ─────────────────────────────────────────────────────────────

RUNS_COLUMNS = [
    "Timestamp", "Mode", "Emails Scanned", "Listings Found",
    "Unique Addresses", "Rows Added", "Rows Updated", "Status", "Error",
]


def ensure_runs_header(sheets):
    try:
        result = _with_retry(lambda: sheets.spreadsheets().values().get(
            spreadsheetId=SHEET_ID,
            range=f"{RUNS_TAB}!A1:I1",
        ).execute())
    except HttpError as e:
        if e.resp.status == 400:
            # Tab doesn't exist yet — create it first
            _with_retry(lambda: sheets.spreadsheets().batchUpdate(
                spreadsheetId=SHEET_ID,
                body={"requests": [{"addSheet": {"properties": {"title": RUNS_TAB}}}]},
            ).execute())
            result = {}
        else:
            raise
    if not result.get("values"):
        _with_retry(lambda: sheets.spreadsheets().values().update(
            spreadsheetId=SHEET_ID,
            range=f"{RUNS_TAB}!A1",
            valueInputOption="RAW",
            body={"values": [RUNS_COLUMNS]},
        ).execute())


def log_run(sheets, run_start, mode, n_emails, n_listings, n_unique,
            n_added, n_updated, status, error):
    row = [
        run_start.strftime("%Y-%m-%d %H:%M:%S"),
        mode, n_emails, n_listings, n_unique, n_added, n_updated, status, error,
    ]
    _with_retry(lambda: sheets.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range=f"{RUNS_TAB}!A1",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute())


# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    print("🏠 Redfin Real Estate Agent starting…\n")
    run_start = datetime.now()

    gmail, sheets = get_google_services()
    ensure_header(sheets)
    ensure_runs_header(sheets)
    existing_rows, addr_to_row = read_sheet(sheets)
    latest_date = get_latest_date(existing_rows)

    # Tracking vars — set to defaults so finally block always has values
    mode        = "unknown"
    messages    = []
    all_listings = []
    deduped     = []
    appended = updated = 0
    error_msg   = ""

    try:
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

        # ── Build school cache from existing sheet rows ───────────────────────────
        addr_school_cache = {}
        for row in existing_rows[1:]:
            if len(row) > 1 and row[1]:
                key = normalise_address(row[1])
                cached_schools = []
                for j in range(3):
                    base = 10 + j * 3
                    name   = row[base]     if len(row) > base     else ""
                    kind   = row[base + 1] if len(row) > base + 1 else ""
                    rating = row[base + 2] if len(row) > base + 2 else ""
                    if name:
                        cached_schools.append((name, kind, rating))
                if cached_schools:
                    schools_updated = row[21] if len(row) > 21 else ""
                    hoa             = row[22] if len(row) > 22 else ""
                    addr_school_cache[key] = {"schools": cached_schools, "updated": schools_updated, "hoa": hoa}

        # ── Enrich with school data ───────────────────────────────────────────────
        today = datetime.now().strftime("%Y-%m-%d")

        # Apply cache for listings whose school data is still fresh
        to_fetch = []  # (index, listing, is_stale_refresh)
        for i, l in enumerate(deduped, 1):
            key = normalise_address(l["address"])
            cached = addr_school_cache.get(key)
            if cached and not _schools_stale(cached["updated"]):
                print(f"  🏫 [{i}/{len(deduped)}] {l['address']} → cached ({len(cached['schools'])} school(s))")
                for j, (name, kind, rating) in enumerate(cached["schools"][:3], 1):
                    l[f"school{j}"] = name
                    l[f"type{j}"]   = kind
                    l[f"rating{j}"] = rating
                l["schools_updated"] = cached["updated"]
                l["hoa"] = cached.get("hoa", "")
            else:
                to_fetch.append((i, l, cached is not None))

        # Fetch school data in parallel for listings that need it
        if to_fetch:
            label = "stale refresh" if all(stale for _, _, stale in to_fetch) else "fetch"
            print(f"\n  🏫 Fetching schools for {len(to_fetch)} listing(s) "
                  f"(up to {MAX_SCHOOL_WORKERS} parallel)…")
            with ThreadPoolExecutor(max_workers=MAX_SCHOOL_WORKERS) as executor:
                futures = {
                    executor.submit(fetch_listing_data, l["url"]): (i, l, stale)
                    for i, l, stale in to_fetch
                }
                for future in as_completed(futures):
                    i, l, stale = futures[future]
                    data = future.result()
                    schools = data["schools"]
                    for j, s in enumerate(schools[:3], 1):
                        l[f"school{j}"] = s["name"]
                        l[f"type{j}"]   = s["type"]
                        l[f"rating{j}"] = s["rating"]
                    l["hoa"] = data["hoa"]
                    l["schools_updated"] = today
                    action = "refreshed" if stale else "fetched"
                    hoa_str = f"  HOA: {data['hoa']}" if data["hoa"] else ""
                    if schools:
                        names = ", ".join(s["name"][:25] for s in schools)
                        print(f"  🏫 [{i}/{len(deduped)}] {l['address']} → {action}: {names}{hoa_str}")
                    else:
                        print(f"  🏫 [{i}/{len(deduped)}] {l['address']} → no schools found{hoa_str}")

        # ── Write to Sheets ───────────────────────────────────────────────────────
        print(f"\n📝 Writing to Google Sheets…")
        appended, updated = write_listings(sheets, deduped, existing_rows, addr_to_row)
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

    except Exception as e:
        error_msg = str(e)[:500]
        raise
    finally:
        status = "Failed" if error_msg else "Success"
        log_run(sheets, run_start, mode, len(messages), len(all_listings),
                len(deduped), appended, updated, status, error_msg)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        msg = str(e)[:120].replace('"', "'")
        subprocess.run([
            "osascript", "-e",
            f'display notification "{msg}" with title "Redfin Agent Failed" sound name "Basso"',
        ])
        raise
