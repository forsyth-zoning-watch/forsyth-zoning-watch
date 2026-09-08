"""
Forsyth Zoning Watch — daily filing monitor.

What this does, once per run:
  1. Opens the county's public permit portal (css.forsythco.com) in a real
     headless browser and searches for each zoning-relevant Plan Type.
  2. Parses the result rows (Plan Number, Applied Date, Main Parcel, etc.).
  3. Skips any case number already in `processed_filings` (seen on a prior run).
  4. For each genuinely new filing, looks up its parcel's coordinates against
     the county's own parcel GIS layer (a plain JSON API — no browser needed
     for this part).
  5. Finds every confirmed subscriber within their chosen radius of that point.
  6. Emails each match once, and records it so a re-run never double-sends.

Honesty check on what's been verified versus what hasn't, since this matters
for a script that runs unattended:
  - The search flow (select Plan, set Plan Type, click Search, read results)
    was walked through and confirmed live, by hand, in a real browser.
  - The plan-type option values below were copied verbatim from that live
    session — not guessed.
  - The Playwright automation of that same flow, and the text-block parser
    below, are new code written to match what was observed. They have NOT
    been run end-to-end. The very first scheduled run should be watched
    (`workflow_dispatch` it manually and read the Action log) rather than
    trusted blind. If a selector has drifted, this is where it'll show up.

Environment variables required (set as GitHub repo secrets — see SETUP.md):
  SUPABASE_URL, SUPABASE_SERVICE_KEY, RESEND_API_KEY, RESEND_FROM
"""

import os
import re
import sys
import math
import datetime
import requests
from playwright.sync_api import sync_playwright

SEARCH_URL = "https://css.forsythco.com/EnerGov_Prod/SelfService/#/search"
PARCEL_QUERY_URL = (
    "https://geo.forsythco.com/gis/rest/services/Public/Tax_Parcel/FeatureServer/0/query"
)

# Plan Type dropdown option values, copied verbatim from the live search form.
# These are the categories that give the earliest possible warning: a brand
# new rezoning application warns you before any site plan exists at all;
# Site Development is the next stage after that.
MONITORED_PLAN_TYPES = {
    "Rezoning": "string:47d73166-4f56-43ff-afdc-efe5de883919_d2785d3f-2e41-4099-a7fc-f684e7271fdb",
    "Rezoning/CUP": "string:47d73166-4f56-43ff-afdc-efe5de883919_8036ea41-843b-4235-9b87-034ce11024bf",
    "County-Initiated Rezoning": "string:47d73166-4f56-43ff-afdc-efe5de883919_73757460-4a83-4fd1-a536-75645670c90f",
    "County-Initiated Rezoning with CUP": "string:47d73166-4f56-43ff-afdc-efe5de883919_ea782537-0a53-41f4-8fa5-79e718406b39",
    "Zoning Condition Amendment": "string:051b9082-0808-41b0-9590-af4d169aa223_0a17c3b6-ed46-4ad4-8d38-cdc3f4a4553e",
    "County-Initiated Zoning Condition Amendment": "string:051b9082-0808-41b0-9590-af4d169aa223_b0d09e4e-c1df-4675-ac92-a422c8632094",
    "Site Development": "string:da832596-0ac4-4768-9704-e56656e94de4_71efaa8b-f84b-4c88-bad1-5232a91d2db0",
}

# Safety cap on how many result pages we'll click through per plan type.
# At ~100 results/page this is generous; we stop early anyway once we hit
# rows already in processed_filings.
MAX_PAGES = 6

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
RESEND_API_KEY = os.environ["RESEND_API_KEY"]
RESEND_FROM = os.environ["RESEND_FROM"]

SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}


# ---------------------------------------------------------------------------
# Supabase helpers (plain REST calls — no SDK, so there's nothing here that
# isn't visible in this file)
# ---------------------------------------------------------------------------

def sb_get(table, params):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{table}", headers=SB_HEADERS, params=params)
    r.raise_for_status()
    return r.json()


def sb_insert(table, row):
    r = requests.post(f"{SUPABASE_URL}/rest/v1/{table}", headers=SB_HEADERS, json=row)
    if r.status_code >= 300:
        print(f"  [supabase insert warning] {table}: {r.status_code} {r.text[:300]}")
    return r


def already_processed(case_number):
    rows = sb_get("processed_filings", {"case_number": f"eq.{case_number}", "select": "case_number"})
    return len(rows) > 0


def mark_processed(filing):
    sb_insert("processed_filings", filing)


def confirmed_subscribers():
    return sb_get("subscribers", {"confirmed": "eq.true", "select": "*"})


def pending_confirmation_subscribers():
    """Anyone who signed up since the last run and hasn't been emailed a
    confirm link yet. Confirmation goes out on this same daily schedule
    rather than instantly — see SETUP.md for why that's the deliberate
    simple-path tradeoff for v1."""
    return sb_get(
        "subscribers",
        {"confirmed": "eq.false", "confirm_sent_at": "is.null", "select": "*"},
    )


def mark_confirm_sent(subscriber_id):
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/subscribers",
        headers=SB_HEADERS,
        params={"id": f"eq.{subscriber_id}"},
        json={"confirm_sent_at": datetime.datetime.utcnow().isoformat()},
    )
    if r.status_code >= 300:
        print(f"  [supabase update warning] mark_confirm_sent: {r.status_code} {r.text[:300]}")


def already_notified(subscriber_id, case_number):
    rows = sb_get(
        "notifications_sent",
        {
            "subscriber_id": f"eq.{subscriber_id}",
            "case_number": f"eq.{case_number}",
            "select": "id",
        },
    )
    return len(rows) > 0


def record_notification(subscriber_id, case_number):
    sb_insert("notifications_sent", {"subscriber_id": subscriber_id, "case_number": case_number})


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def haversine_miles(lat1, lon1, lat2, lon2):
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def geocode_parcel(main_parcel):
    """Look up a parcel's centroid on the county's own GIS layer. Plain JSON
    API — this part has been live-tested extensively."""
    parcel_id = main_parcel.strip()
    params = {
        "where": f"PARCELID='{parcel_id}'",
        "outFields": "PARCELID",
        "returnGeometry": "false",
        "returnCentroid": "true",
        "outSR": "4326",
        "f": "json",
    }
    try:
        r = requests.get(PARCEL_QUERY_URL, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        feats = data.get("features") or []
        if not feats:
            return None, None
        c = feats[0].get("centroid")
        if not c:
            return None, None
        return c["y"], c["x"]  # lat, lon
    except Exception as e:
        print(f"  [geocode error] parcel {parcel_id}: {e}")
        return None, None


# ---------------------------------------------------------------------------
# EnerGov scraping
# ---------------------------------------------------------------------------

RESULT_BLOCK_RE = re.compile(
    r"Plan Number\s+(?P<case>\S+(?:\s*-\s*\S+)?)\s*\n"
    r"Applied Date\s+(?P<applied>\d{2}/\d{2}/\d{4})\s*\n"
    r"Type\s+(?P<type>.+?)\s*\n"
    r"(?:Completion Date.*?\n)?"
    r"(?:Expiration Date.*?\n)?"
    r"Status\s+(?P<status>.+?)\s*\n"
    r"Main Parcel\s+(?P<parcel>[\w\- ]+?)\s*\n"
    r"Project Name\s*(?P<project>.*?)\s*\n"
    r"Address\s*(?P<address>.*?)\s*\n"
    r"Description\s*(?P<description>.*?)\s*\n",
    re.MULTILINE,
)


def parse_results(page_text):
    """Turn the plain-text results dump into structured rows. Field order and
    labels below match a real captured search result exactly."""
    filings = []
    for m in RESULT_BLOCK_RE.finditer(page_text):
        filings.append(
            {
                "case_number": m.group("case").strip(),
                "applied_date": m.group("applied").strip(),
                "plan_type": m.group("type").strip(),
                "status": m.group("status").strip(),
                "main_parcel": m.group("parcel").strip(),
                "project_name": m.group("project").strip(),
                "address": m.group("address").strip(),
                "description": m.group("description").strip(),
            }
        )
    return filings


def search_plan_type(page, type_label, type_value):
    print(f"Searching plan type: {type_label}")
    page.goto(SEARCH_URL, wait_until="networkidle")

    page.get_by_label("Search", exact=True).select_option("Plan")
    page.wait_for_timeout(1500)  # form re-renders into the Plan-specific fields

    page.get_by_label("Plan Type").select_option(type_value)
    page.get_by_role("button", name="Search", exact=True).click()
    page.wait_for_timeout(2500)

    all_filings = []
    for page_num in range(MAX_PAGES):
        text = page.locator("body").inner_text()
        batch = parse_results(text)
        if not batch:
            break
        all_filings.extend(batch)

        # Stop early once every row on this page is already known — no point
        # paging further back in time.
        if all(already_processed(f["case_number"]) for f in batch):
            break

        next_link = page.get_by_role("link", name="Next", exact=True)
        if next_link.count() == 0:
            break
        next_link.first.click()
        page.wait_for_timeout(2000)

    return all_filings


def fetch_new_filings():
    new_filings = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        for label, value in MONITORED_PLAN_TYPES.items():
            try:
                results = search_plan_type(page, label, value)
            except Exception as e:
                print(f"  [search error] {label}: {e}")
                continue
            for f in results:
                if not already_processed(f["case_number"]):
                    new_filings.append(f)
        browser.close()
    return new_filings


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def send_alert(to_email, filing, distance_miles, unsubscribe_token):
    unsubscribe_url = f"https://YOUR_GITHUB_USERNAME.github.io/forsyth-zoning-watch/manage.html?action=unsubscribe&token={unsubscribe_token}"
    subject = f"New {filing['plan_type']} filing near you — {filing['address'] or filing['main_parcel']}"
    body_html = f"""
    <p>A new <strong>{filing['plan_type']}</strong> filing was submitted to Forsyth County
    about {distance_miles:.2f} miles from the address you're watching.</p>
    <ul>
      <li><strong>Case number:</strong> {filing['case_number']}</li>
      <li><strong>Applied:</strong> {filing['applied_date']}</li>
      <li><strong>Parcel:</strong> {filing['main_parcel']}</li>
      <li><strong>Address:</strong> {filing['address'] or 'not listed'}</li>
      <li><strong>Description:</strong> {filing['description'] or 'none on file'}</li>
    </ul>
    <p><a href="https://css.forsythco.com/EnerGov_Prod/SelfService/#/search">Look it up on the county portal</a></p>
    <p style="color:#888;font-size:12px;margin-top:24px;">
      You're getting this because you signed up for zoning alerts near this address.
      <a href="{unsubscribe_url}">Unsubscribe</a>
    </p>
    """
    r = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
        json={"from": RESEND_FROM, "to": [to_email], "subject": subject, "html": body_html},
    )
    if r.status_code >= 300:
        print(f"  [email error] {to_email}: {r.status_code} {r.text[:300]}")
    return r.status_code < 300


def send_confirmation(to_email, confirm_token, address):
    confirm_url = f"https://YOUR_GITHUB_USERNAME.github.io/forsyth-zoning-watch/manage.html?action=confirm&token={confirm_token}"
    r = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
        json={
            "from": RESEND_FROM,
            "to": [to_email],
            "subject": "Confirm your Forsyth Zoning Watch alert",
            "html": f"""
                <p>You (or someone using this email address) signed up for zoning
                alerts near <strong>{address}</strong>.</p>
                <p><a href="{confirm_url}">Click here to confirm</a> and start receiving alerts.</p>
                <p style="color:#888;font-size:12px;">If you didn't request this, ignore this email — nothing happens without confirmation.</p>
            """,
        },
    )
    if r.status_code >= 300:
        print(f"  [email error] confirm to {to_email}: {r.status_code} {r.text[:300]}")
    return r.status_code < 300


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print(f"=== Forsyth Zoning Watch run — {datetime.datetime.utcnow().isoformat()}Z ===")

    pending = pending_confirmation_subscribers()
    print(f"Sending {len(pending)} pending confirmation email(s).")
    for sub in pending:
        if send_confirmation(sub["email"], sub["confirm_token"], sub["address"]):
            mark_confirm_sent(sub["id"])

    new_filings = fetch_new_filings()
    print(f"Found {len(new_filings)} new filing(s) not seen before.")

    if not new_filings:
        return

    subscribers = confirmed_subscribers()
    print(f"{len(subscribers)} confirmed subscriber(s) to check against.")

    for filing in new_filings:
        lat, lon = geocode_parcel(filing["main_parcel"])
        filing["lat"], filing["lon"] = lat, lon

        if lat is not None:
            for sub in subscribers:
                dist = haversine_miles(lat, lon, sub["lat"], sub["lon"])
                if dist <= sub["radius_miles"]:
                    if already_notified(sub["id"], filing["case_number"]):
                        continue
                    ok = send_alert(sub["email"], filing, dist, sub["unsubscribe_token"])
                    if ok:
                        record_notification(sub["id"], filing["case_number"])
                        print(f"  Notified {sub['email']} ({dist:.2f} mi) re: {filing['case_number']}")
        else:
            print(f"  [no coordinates found for parcel {filing['main_parcel']}, case {filing['case_number']}]")

        # Mark seen regardless, so we never reprocess this case again.
        mark_processed(
            {
                "case_number": filing["case_number"],
                "plan_type": filing["plan_type"],
                "applied_date": filing["applied_date"] and _to_iso_date(filing["applied_date"]),
                "main_parcel": filing["main_parcel"],
                "project_name": filing["project_name"],
                "address": filing["address"],
                "description": filing["description"],
                "lat": lat,
                "lon": lon,
            }
        )

    print("Done.")


def _to_iso_date(mmddyyyy):
    try:
        m, d, y = mmddyyyy.split("/")
        return f"{y}-{m}-{d}"
    except Exception:
        return None


if __name__ == "__main__":
    sys.exit(main())
