# Setting up Forsyth Zoning Watch

Four free accounts, ~30 minutes, no server of your own. I can't create any of
these for you — account signup is a line I don't cross even for free
services — but everything below is copy/paste once you're in.

## What you're building

```
Resident's browser                 GitHub Actions (once/day)
   |                                      |
   v                                      v
index.html (GitHub Pages)  <---->   check_new_filings.py
   |  reads/writes                       |  reads/writes
   v                                      v
        Supabase (subscribers, processed_filings, notifications_sent)
                                          |
                                          v
                                   Resend (sends the emails)
```

The site and the script never talk to each other directly — they both talk
to Supabase, which is the shared source of truth.

---

## 1. Create the GitHub repo

1. On github.com, create a new **public** repository, e.g. `forsyth-zoning-watch`.
2. Upload everything in this project into it, keeping the folder structure:
   `site/`, `monitor/`, `.github/workflows/`, `schema.sql`.

## 2. Create the Supabase project

1. Go to supabase.com → sign up (free) → **New project**. Pick any name/region, set a database password (save it somewhere — you likely won't need it again, but don't lose it).
2. Once it's ready: left sidebar → **SQL Editor** → **New query**. Paste in the entire contents of `schema.sql` → **Run**. You should see three new tables in **Table Editor**: `subscribers`, `processed_filings`, `notifications_sent`.
3. Left sidebar → **Project Settings** → **API**. You need two values from this page:
   - **Project URL** (looks like `https://xxxxx.supabase.co`)
   - **anon / public key** — this one is *meant* to be public, it's what the website uses
   - **service_role key** — this one is a real secret, only the script uses it, never put it in the website

## 3. Create the Resend account

1. Go to resend.com → sign up (free tier: 3,000 emails/month, 100/day — plenty for this).
2. **Domains** → add a domain you control (or use their onboarding sandbox address to start, and add your own domain later — sending "from" a domain you own looks far less spammy and is worth doing before a real launch).
3. **API Keys** → create one, full access. Save it — you won't see it again.
4. Decide your "from" address, e.g. `alerts@your-domain.org`.

## 4. Fill in the placeholders

Three files have placeholder text to replace:

**`site/index.html`** and **`site/manage.html`** — near the top of the `<script>` block:
```js
var SUPABASE_URL = "YOUR_SUPABASE_URL";
var SUPABASE_ANON_KEY = "YOUR_SUPABASE_ANON_KEY";
```
Replace with your actual Project URL and anon key from step 2.3. (Yes, the anon key really is safe to put in public client-side code — that's what the Row Level Security policies in `schema.sql` are for.)

**`monitor/check_new_filings.py`** and **`site/index.html`** — search for `YOUR_GITHUB_USERNAME` (it appears in the unsubscribe/confirm link URLs) and replace with your actual GitHub username and repo name, matching wherever you end up hosting the site (step 6).

## 5. Add the script's secrets to GitHub

In your repo: **Settings → Secrets and variables → Actions → New repository secret**. Add four:

| Name | Value |
|---|---|
| `SUPABASE_URL` | same Project URL as above |
| `SUPABASE_SERVICE_KEY` | the **service_role** key (not anon) |
| `RESEND_API_KEY` | from step 3.3 |
| `RESEND_FROM` | e.g. `alerts@your-domain.org` |

## 6. Turn on GitHub Pages

**Settings → Pages** → Source: deploy from branch → pick your main branch, folder `/site`. Save. GitHub gives you a URL like `https://yourusername.github.io/forsyth-zoning-watch/` — that's the public link you'll share.

## 7. Test the monitor script before trusting it unattended

**Actions tab → Daily zoning filing check → Run workflow** (this is the `workflow_dispatch` trigger — it lets you fire it manually instead of waiting for tomorrow's 7am run). Watch the log.

**This first run is the important one.** The scraping logic was written from a real, hand-verified walkthrough of the county's search form, but the Playwright automation code itself has not been run end-to-end before now — that's flagged in the script's own comments. If a selector doesn't match (the county changes their portal's field labels, for instance), this is where you'll see it fail, with a specific error rather than a silent miss. Read the log for `[search error]` or `[email error]` lines.

Once it runs clean once, the daily 7am schedule takes over on its own.

## On going forward

- **Costs**: $0 at realistic early scale. Supabase, Resend, and GitHub Actions all have free tiers well above what a few hundred subscribers and a handful of daily filings would use. If this genuinely takes off countywide, you'd hit Resend's free email cap before anything else — worth knowing, not worth solving today.
- **Confirmation emails go out on the daily batch**, not instantly — a resident who signs up at 8am gets confirmed at tomorrow's 7am run, not in the next minute. That's a deliberate simplicity trade for v1 (see the code comments) — real-time confirmation is a small addition later (a Supabase Edge Function triggered on insert) if it's worth the extra moving part.
- **Branding decision still open**: this can go out under your name or under a neutral project name. Nothing in the code assumes either — it's a naming and `RESEND_FROM` choice, not an architecture one.
