# Google Maps Lead Scraper

A self-hosted Streamlit app that turns Google Maps searches into clean, deduped, enriched lead lists you can drop into a CRM. Built for sales / marketing / agency workflows where you want hundreds of qualified businesses **with emails and social profiles**, not just a 20-row Google Maps export.

Built with **Streamlit + Playwright (async) + httpx + SQLite**. Tuned to run on a laptop or Streamlit Community Cloud's free tier.

---

## Table of contents

1. [What it does](#what-it-does)
2. [Features](#features)
3. [Quick start (local)](#quick-start-local)
4. [Quick start (Streamlit Cloud)](#quick-start-streamlit-cloud)
5. [Step-by-step walkthrough](#step-by-step-walkthrough)
6. [Sidebar reference](#sidebar-reference)
7. [Output fields](#output-fields)
8. [How it works (3-phase pipeline)](#how-it-works-3-phase-pipeline)
9. [Pause / resume / history / presets](#pause--resume--history--presets)
10. [Files](#files)
11. [Troubleshooting](#troubleshooting)
12. [Caveats & legal](#caveats--legal)

---

## What it does

1. You enter **search terms** (e.g. `gym, yoga studio, pilates`) and a **location** (e.g. `Austin, Texas`).
2. The app drives a headless Chromium browser through Google Maps, scrolls the result feed, and visits each place's detail page **3 places in parallel**.
3. For each place it pulls **name, category, address, website, phone, rating, review count** and the canonical Google Maps URL.
4. For every business with a website, an HTTP enricher fetches the homepage (+ a couple of contact/about pages) and extracts **emails, Facebook, Instagram, LinkedIn, Twitter, YouTube, TikTok, Pinterest, WhatsApp** links.
5. The final list is **deduped by website**, **sorted by a lead score** (rating × log(reviews) + email/website/phone/social bonuses), **filtered** by your quality gates, and downloaded as CSV or JSON with a filename like `gym_austin_texas.csv`.

A single run can stream in real time, get paused/stopped/resumed, recover from network blips, detect CAPTCHAs, and survive a browser crash — your progress lives in a local SQLite DB.

---

## Features

### Scraping
- **Multi-term + multi-location** search; comma- or newline-separated terms
- **Country → State → City** cascading dropdowns (offline fallback for US / India / UK / Canada / Australia / Germany) **or** free-text location
- **Multi-select** state, city, and place — full cartesian expansion across the cross-product
- **🗺️ Deep search**: splits the location's bounding box into an N×N grid and scrapes each tile separately to **bypass Google's ~120-result cap per viewport**
- **Unlimited or capped** results per term/tile
- **Permanently-closed places are skipped automatically**
- **Pre-dedupe by Google's stable `fid`** before any extra page load

### Enrichment
- **Optional per-channel filter** — tick "Email only" / "Instagram only" / both / neither (= all socials)
- **Parallel HTTP** with 5 worker threads sharing one keep-alive `httpx.Client`
- **Smart page count**: only fetches contact/about pages when emails are wanted
- **Auto-fills**: emails (with junk filter), Facebook, Instagram, LinkedIn, Twitter, YouTube, TikTok, Pinterest, WhatsApp

### Post-processing
- **Sort by lead score** (composite) or rating / reviews / completeness / has-email / has-website / has-phone / social presence — primary + tiebreaker
- **Dedupe by website** (smart URL normalization, keeps the most-complete row)
- **Channel filter** — drop rows missing your required channel(s)
- **Minimum rating + minimum review count** quality gates
- **Domain exclude list** — drop wix.com / godaddy / linktr.ee / facebook.com noise with one textarea
- **Phone numbers normalized to E.164** via the `phonenumbers` library, country inferred from the search location
- **Lead score column** added to every row so what's on screen is what gets downloaded

### Performance
- **3 concurrent Chromium pages** in Phase 1 (~3× speedup)
- **Persistent SQLite cache** by `fid` — re-running the same/overlapping query short-circuits known places (~30× faster on re-runs)
- **Image / font / CSS / media / ads** all blocked at the browser level
- **Lean Chromium flags** so a single browser fits comfortably in 1 GB RAM

### Resilience
- **▶️ Start / ⏸️ Pause / ▶️ Resume / ⏹️ Stop** buttons that take effect within ~5 s
- **Auto-retry on network blips** with `5s → 10s → 30s → 60s` exponential backoff
- **CAPTCHA detection** — headless mode stops cleanly; visible-browser mode waits up to 10 min for you to solve it, then auto-resumes
- **Mid-run resume** — close the laptop, come back later, the sidebar offers a Resume button with everything you'd already scraped pre-loaded

### Workflow
- **Run history** — past completed runs are saved with their final results; reload any with one click
- **Search presets** — save `restaurant + Manhattan + grid 3×3 + Email-only` as a named preset, restore later
- **Stats panel** — see how many places were skipped, timed out, hit CAPTCHAs, or enrichment-failed
- **CSV + JSON download** with smart filename built from search term + location
- **Live metrics** — places found, % with website, % with email, % with Instagram, average lead score

### UI
- **Dark mode** with electric thunder-blue accents (`#38BDF8`)
- **Material Symbols** throughout (no emoji)
- Phase progress pills (Scrape → Enrich → Sort & Dedupe) lighting up as you go

---

## Quick start (local)

### 1. Install Python 3.11 or 3.12
Download from <https://python.org>. On Windows make sure **Add python.exe to PATH** is checked. If `python` doesn't work but `py` does, that's fine — substitute `py` for `python` everywhere below.

### 2. Install dependencies + browser
```powershell
py -m pip install -r requirements.txt
py -m playwright install chromium
```

### 3. Run the app
```powershell
py -m streamlit run streamlit_app.py
```

Visit <http://localhost:8501>. First launch installs Playwright's Chromium (~120 MB, cached after that).

---

## Quick start (Streamlit Cloud)

1. Push this repo to a GitHub repository.
2. Go to <https://share.streamlit.io> → **New app** → select your repo → main file = `streamlit_app.py`.
3. The app auto-installs:
   - **apt packages** from [packages.txt](packages.txt) (Chromium system libs)
   - **Python packages** from [requirements.txt](requirements.txt)
   - **Playwright Chromium** is installed on first launch via a cached resource hook
4. Keep **Headless** checked (default) — Streamlit Cloud has no display.

> ⚠️ Streamlit Cloud uses datacenter IPs that Google rate-limits aggressively. For real workloads, run locally with residential internet.

---

## Step-by-step walkthrough

A typical lead-gen run:

1. **Sidebar → Search settings**
   - Enter terms: `gym, yoga studio, pilates`
   - Pick **Dropdowns** mode → Country: `United States` → State: `Texas` → Cities: `Austin`
2. **Sidebar → Deep search**
   - Tick **Split location into a grid** → grid size `3×3` (gets 5–10× more results than a single search).
3. **Sidebar → Phase 2 — Enrichment**
   - Leave both Email and Instagram unchecked → scrapes **all channels**.
4. **Sidebar → Phase 3 — Filter & dedupe**
   - Minimum rating: `4.0` · Minimum reviews: `10` · Domain exclude: `wix.com`, `godaddy.com`.
5. **Sidebar → Performance**
   - Cache stays on; optionally save these settings as a preset like `Austin gyms — quality leads`.
6. Click **▶ Start scraping**.
7. Watch Phase 1 stream rows live. Pause if you need to leave; resume later.
8. After Phase 1 finishes, Phase 2 runs (5 parallel HTTP workers) and shows a progress bar.
9. After Phase 3, you see KPI tiles (Places · With email · With Instagram · Avg lead score), the sorted dataframe, and **Download CSV / JSON** buttons.
10. The run is saved automatically — find it later in **🕘 Past runs**.

---

## Sidebar reference

### 🎯 Search settings
| Field | Notes |
|---|---|
| **Search terms** | Comma, newline, semicolon, or tab separated. Live preview shows the parsed terms. |
| **Location mode** | `Dropdowns` (Country → State → City) or `Free text`. |
| **Country / States / Cities / Places** | Multi-select with type-to-search. Cities are tagged with state when multiple states picked. Places accepts free-text entries (Enter to add). |
| **♾️ Unlimited** | Scrape until Google shows end-of-list. Disables the slider. |
| **Max places per search term** | Cap per (term × tile). |
| **Language code** | Google Maps UI locale. |
| **Headless browser** | Uncheck **only locally** to watch / solve CAPTCHAs by hand. |

### 🗺️ Deep search
| Field | Notes |
|---|---|
| **Split location into a grid** | Geocodes the location and scrapes an N×N tile grid. |
| **Grid size** | 2×2 = 4 tiles · 3×3 = 9 tiles · 6×6 = 36 tiles. Bigger = more results but exponentially longer. |

### 🌐 Phase 2 — Enrichment
| Field | Notes |
|---|---|
| **Enrich with emails + social media** | Master toggle. |
| **📧 Email only / 📷 Instagram only** | Both unchecked = scrape **all** channels. Tick one or both to narrow. |
| **Parallel website fetches** | 1–10 workers (default 5). More = faster but more bandwidth. |
| **Pages per site** | 1 = homepage only (fastest, IG-friendly). 3 = homepage + contact + about. |

### 🧹 Phase 3 — Filter & dedupe
| Field | Notes |
|---|---|
| **Remove duplicates by website** | Drops rows sharing a normalized domain; keeps the most-complete one. |
| **Minimum rating** | 0.0 = off. 4.0 ≈ quality businesses. |
| **Minimum review count** | 0 = off. 10+ filters out brand-new listings. |
| **Domain exclude list** | One domain per line. Accepts bare domains, `*.foo.com`, or full URLs. |

### ⚡ Performance
| Field | Notes |
|---|---|
| **Use persistent place cache** | Re-runs of the same query skip already-scraped places (30-day TTL). |
| **Clear cache** | Wipes the local SQLite cache. |

### 🔖 Search presets
| Field | Notes |
|---|---|
| **Load a saved preset** | Dropdown of saved searches. |
| **Apply** | Restores search terms + free-text location. (Re-check other toggles by hand.) |
| **Save current settings as preset** | Writes the full sidebar snapshot. |

---

## Output fields

After Phase 3, each row has:

| Field | Source |
|---|---|
| `title` | Google Maps place name |
| `category` | Primary category |
| `address` | Full address |
| `website` | Business homepage |
| `phone` | Normalized to E.164 when parseable (else original) |
| `rating` | Out of 5 |
| `reviews_count` | Total reviews |
| `lead_score` | `rating × log(1+reviews) + 5·email + 2·website + 1·phone + 1·social_count` |
| `url` | Canonical Google Maps URL for the place |
| `search_term` | Which input term found it |
| `search_location` | Which location query produced it |
| `emails` | Pipe-separated in CSV / array in JSON |
| `facebook` `instagram` `linkedin` `twitter` `youtube` `tiktok` `pinterest` `whatsapp` `telegram` | Same — pipe-separated lists |

Columns for channels you didn't enrich are not present at all.

---

## How it works (3-phase pipeline)

```
┌──────────────────────────────────────────────────────────────┐
│ Phase 1 — Playwright (async)                                 │
│   for each search term × location × tile:                    │
│     - load the Maps results page (with goto-retry backoff)   │
│     - scroll the feed until end-marker or cap reached        │
│     - collect place URLs, pre-dedupe by fid                  │
│     - cache-hit short-circuit by fid                         │
│     - 3 concurrent pages visit detail pages                  │
│     - detect permanently-closed + CAPTCHA                    │
│     - extract title/category/address/website/phone/...       │
│   yields place dicts to the UI via a thread-safe Queue       │
└──────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌──────────────────────────────────────────────────────────────┐
│ Phase 2 — httpx + ThreadPoolExecutor                         │
│   - 5 worker threads, ONE shared keep-alive Client           │
│   - per row: fetch homepage (+ contact/about if email wanted)│
│   - regex emails (with junk blocklist) + social URLs         │
│   - smart per-channel: skip contact pages when only socials  │
└──────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌──────────────────────────────────────────────────────────────┐
│ Phase 3 — pure Python                                        │
│   - sort by completeness                                     │
│   - dedupe by normalized website (keep most-complete row)    │
│   - channel filter (only rows with required channels)        │
│   - min rating / min reviews gates                           │
│   - domain blocklist                                         │
│   - compute lead_score column                                │
│   - persist final results to SQLite for run history          │
└──────────────────────────────────────────────────────────────┘
```

Phase 1 runs on a **background thread** via `asyncio.run` inside a daemon `threading.Thread`. The Streamlit UI polls a `queue.Queue` every second so buttons stay responsive — Pause/Stop signal through `threading.Event`s that the async scraper checks at safe points.

---

## Pause / resume / history / presets

### Pause / Resume / Stop
Buttons appear above the live table once a scrape kicks off. They take effect within 5–15 s (bounded by whatever Playwright call is in flight).

### Auto-resume after a crash
Every yielded `fid` and every completed tile is logged to SQLite. If Phase 1 crashes or you close the tab, the next page load shows a **🔄 Resume a previous scrape** banner. Click **Resume** → the scraper starts again with `seen_fids` and `completed_tiles` pre-loaded, so it picks up exactly where it left off.

### Run history
Done scrapes are kept indefinitely (until you Clear cache or hit Delete on a row). The **🕘 Past runs** panel lets you reload the final filtered+sorted results from any past run with one click.

### Search presets
The 🔖 section in the sidebar lets you save the entire sidebar state under a name and restore later. Currently the Apply button restores **search terms + free-text location** automatically; other toggles need re-checking by hand.

### CAPTCHA handling
- **Headless mode** (default, required on Streamlit Cloud): detection → clean stop with a clear status message.
- **Visible browser** (uncheck Headless locally): the scraper pauses, waits up to 10 min for you to solve the CAPTCHA in the open Chrome window, then auto-resumes.

---

## Files

| File | Purpose |
|---|---|
| [streamlit_app.py](streamlit_app.py) | UI, state machine, three-phase orchestration, downloads |
| [scraper.py](scraper.py) | Async Playwright scraper, control events, retry, CAPTCHA, run-state hooks |
| [enrich.py](enrich.py) | HTTP enrichment, email/social extraction, parallel batch runner |
| [grid.py](grid.py) | Nominatim geocoder + N×N tile URL builder |
| [locations.py](locations.py) | Country → state → city lookups + bundled offline fallbacks |
| [format.py](format.py) | Phone E.164 normalizer + domain exclude helpers |
| [cache.py](cache.py) | SQLite store for places, runs, results, presets |
| [requirements.txt](requirements.txt) | Python deps |
| [packages.txt](packages.txt) | apt deps for Streamlit Cloud (Chromium libs) |
| [.streamlit/config.toml](.streamlit/config.toml) | Dark theme + thunder-blue primary |
| `setup.sh` | Legacy bash setup (not auto-run on newer Streamlit Cloud) |

SQLite database lives at `~/.cache/gmaps_scraper/places.db`.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `streamlit` is not recognized | Use `py -m streamlit run streamlit_app.py` (Windows). |
| `Python was not found` | Install Python 3.11/3.12 from <https://python.org>. Tick "Add to PATH". |
| `Page.wait_for_selector: Timeout` errors mid-run | Network or rate limit. The retry+CAPTCHA detector handles most of these; just resume. |
| Got only 22 results for `restaurant in NYC` | Google caps a single viewport. Turn on **🗺️ Deep search** with grid 3×3 or higher. |
| US states won't load in dropdowns | Bundled fallback kicks in automatically. If both the API and bundle return nothing, switch to **Free text** mode. |
| Phase 2 keeps failing | Some sites block Python user-agents. Lower **Parallel website fetches** to 2–3 and **Pages per site** to 1. |
| Run takes forever | Lower Max places, drop grid size, turn off Phase 2, or pick a more specific search term. |
| CSS shows as raw text | Caused by old browser cache. Hard-refresh (Ctrl+Shift+R). |
| Past runs panel is empty | Only runs that reached Phase 3 are saved. Crashed or stopped runs go in the **Resume** banner instead. |

---

## Caveats & legal

- **Selectors track Google Maps as of late 2025 / early 2026.** Google reships often; if extraction breaks, update the selectors in `_extract_place()` and `_collect_place_links()`.
- **Datacenter IPs are throttled.** Use residential internet for serious volume. Adding proxy rotation is a 30-min feature add when you need it.
- **Google's Terms of Service** prohibit some automated scraping. Respect robots.txt and applicable IP / privacy law in your jurisdiction. This tool is intended for personal lead-gen on publicly listed business data.
- **GDPR / CAN-SPAM:** before emailing scraped contacts, make sure you have a lawful basis and a working unsubscribe path.
- **Streamlit Cloud free tier** has ~1 GB RAM and a 10-minute single-request limit. Long runs work because everything is on a background thread, but the container may hibernate. The cache + resume features cover most of that case.

---

## License

Personal / educational use. Adjust as needed for your situation.
