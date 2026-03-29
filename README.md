# Redfin Real Estate Agent — Setup Guide

A local Python script that scans your Gmail for Redfin listing emails,
extracts property data, enriches with school ratings, and syncs
everything to a Google Sheet.

---

## Prerequisites

- Python 3.9 or later
- A Google account (the one that receives Redfin emails)

---

## Step 1 — Install dependencies

```bash
pip install -r requirements.txt
```

---

## Step 2 — Create a Google Cloud project & enable APIs

1. Go to https://console.cloud.google.com
2. Click **New Project** → name it "Redfin Agent" → **Create**
3. In the left sidebar go to **APIs & Services → Library**
4. Search for and enable **Gmail API**
5. Search for and enable **Google Sheets API**

---

## Step 3 — Create OAuth credentials

1. Go to **APIs & Services → OAuth consent screen**
   - Choose **External** → **Create**
   - Fill in App name ("Redfin Agent"), your email for support and developer contact
   - Click **Save and Continue** through all steps (no need to add scopes here)
   - On the last step click **Back to Dashboard**
2. Go to **APIs & Services → Credentials**
   - Click **+ Create Credentials → OAuth client ID**
   - Application type: **Desktop app**
   - Name: "Redfin Agent"
   - Click **Create**
3. Click **Download JSON** on the credential that was just created
4. Save the downloaded file as **`credentials.json`** in the same folder as `redfin_agent.py`

---

## Step 4 — Create your Google Sheet

1. Go to https://sheets.google.com → create a **blank spreadsheet**
2. Name it **"Redfin Listings Tracker"**
3. Copy the Sheet ID from the URL:
   `https://docs.google.com/spreadsheets/d/`**`THIS_PART`**`/edit`
4. Open `redfin_agent.py` and replace `YOUR_SHEET_ID_HERE` with your Sheet ID:
   ```python
   SHEET_ID = "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"  # example
   ```

---

## Step 5 — First run

```bash
python redfin_agent.py
```

- Your browser will open asking you to sign in with Google
- Grant the requested permissions (Gmail read/modify + Sheets edit)
- The token is saved to `token.json` — subsequent runs skip the browser step

**First run** processes the 10 most recent Redfin emails and populates the sheet.

---

## Step 6 — Subsequent / periodic runs

Just run the same command again:

```bash
python redfin_agent.py
```

The script detects the sheet already has data, switches to **periodic mode**,
and processes only **unread** Redfin emails received after the most recent date
already in the sheet. Processed emails are marked as read automatically.

---

## Automate with cron (optional)

Run the agent daily at 8am:

```bash
crontab -e
```

Add this line (adjust the path to where you saved the script):

```
0 8 * * * cd /path/to/redfin_agent && python redfin_agent.py >> agent.log 2>&1
```

---

## Sheet columns

| Column | Source |
|---|---|
| Date | Email received date |
| Address | Parsed from email |
| City | Parsed from email |
| Zip | Parsed from email |
| Status | Tag from email (New, Pending, Open House, etc.) |
| Price | Parsed from email |
| Beds | Parsed from email |
| Baths | Parsed from email |
| SqFt | Parsed from email |
| Price/SqFt | Calculated (Price ÷ SqFt) |
| School1–3 | Fetched from Redfin listing page |
| Type1–3 | Elementary / Middle / High |
| Rating1–3 | GreatSchools rating out of 10 |
| URL | Redfin listing URL |

---

## Troubleshooting

**"File not found: credentials.json"**
→ Make sure you downloaded the OAuth JSON and renamed it to `credentials.json`
  in the same folder as the script.

**"Access blocked: Redfin Agent has not completed the Google verification process"**
→ On the OAuth consent screen in Cloud Console, go to **Test users** and add
  your own Gmail address. This lets you use the app before it's verified.

**No listings extracted from an email**
→ Redfin occasionally changes their email HTML structure. Open the email in
  your browser, view source, and check that listing `<a>` tags still contain
  the property URL pattern `/CA/.../home/...`. The parser may need a tweak —
  open an issue or adjust `REDFIN_URL_RE` in the script.

**School data missing**
→ Redfin's listing pages are JavaScript-rendered. The script uses a simple
  HTTP fetch which may miss dynamically loaded content. If school data is
  consistently absent, the next enhancement would be to use Playwright or
  Selenium for JS rendering.
