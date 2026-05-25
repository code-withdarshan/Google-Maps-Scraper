# 📍 Google Maps Scraper — Streamlit App

A Streamlit web app that scrapes business listings from Google Maps. Enter search terms + location → get back names, addresses, phone, websites, ratings, review counts, price, status, hours, and coordinates. Export as CSV or JSON.

Built with **Streamlit + Playwright (Chromium)**, tuned for **Streamlit Community Cloud free tier**.

## What it extracts

**From Google Maps:**
`title`, `category`, `address`, `website`, `phone`, `plus_code`, `rating`, `reviews_count`, `price`, `status`, `latitude`, `longitude`, `fid`, `opening_hours`, `url`, `search_term`, `scraped_at`.

**From the business's website** (when "Visit websites" is enabled):
`emails`, `facebook`, `instagram`, `linkedin`, `twitter`, `youtube`, `tiktok`, `pinterest`, `whatsapp`.

The enricher fetches the homepage + up to two contact/about pages, then runs regex over the combined HTML. List fields are exported as `|`-separated strings in CSV and as JSON arrays in the JSON download.

## Run locally

```powershell
pip install -r requirements.txt
python -m playwright install chromium
streamlit run streamlit_app.py
```

Visit `http://localhost:8501`.

## Deploy to Streamlit Community Cloud (free tier)

1. Push this repo to GitHub.
2. Go to https://share.streamlit.io → **New app** → point at `streamlit_app.py`.
3. Streamlit Cloud auto-installs:
   - apt packages from `packages.txt` (Chromium system libs)
   - Python packages from `requirements.txt`
4. On first launch the app runs `playwright install chromium` itself (cached after that).

### Free-tier guardrails baked in
- Resource blocking: images, fonts, stylesheets, media, ads/analytics are aborted.
- Small viewport (1280×800), single reused browser page.
- Lean Chromium flags (`--disable-gpu`, `--disable-dev-shm-usage`, no extensions).
- Results stream into the UI as they're scraped — partial output is preserved on errors.
- Slider capped at 60 places per term; the UI warns past a total of 80.

## Files
| File | Purpose |
|---|---|
| `streamlit_app.py` | Two-phase UI + browser-install bootstrap |
| `scraper.py` | Phase 1: Playwright Google Maps scraper (with grid/deep-search) |
| `enrich.py` | Phase 2: parallel HTTP enrichment for emails + socials |
| `grid.py` | Geocoder (Nominatim) + N×N tile URL builder |
| `locations.py` | Country → State → City lookups (countriesnow.space) |
| `requirements.txt` | Python deps |
| `packages.txt` | Chromium system libs for Streamlit Cloud |
| `setup.sh` | Optional bash setup (not auto-run on newer Streamlit Cloud) |

## Location selection
Two modes in the sidebar:
- **Dropdowns** — pick Country → State → City, optionally add a Place/Area free-text. Cascading dropdowns load on demand and cache for 24h.
- **Free text** — type whatever you want (e.g. `Connaught Place, Delhi, India`).

The chosen location feeds both the Google Maps search query and (when **Deep search** is on) the Nominatim geocoder used to build the tile grid.

## Caveats
- **Datacenter IPs get throttled.** Streamlit Cloud's egress is well-known to Google. If you see CAPTCHAs or empty results, retry, or run locally.
- **Free tier RAM is ~1 GB.** Don't push max-results past ~30 for big queries.
- **Selectors track Google Maps as of late 2025 / early 2026.** Google reships frequently; expect to update selectors in `scraper.py` if extraction breaks.
- **Use specific terms.** `pizza` + `sushi` + `burger` returns better data than `restaurant`.
- Respect Google's Terms of Use and applicable privacy/IP laws when scraping.
