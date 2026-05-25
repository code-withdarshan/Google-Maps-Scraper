"""Google Maps scraper using Playwright (sync API).

Tuned for Streamlit Community Cloud free tier (~1 GB RAM):
- blocks images, media, fonts, stylesheets
- small viewport, single reused page
- aggressive flags to lower memory footprint

Selectors target the current Google Maps DOM (late 2025 / early 2026)
with fallbacks for variants that Google A/B-tests.
"""
from __future__ import annotations

import re
import time
from typing import Iterator, Optional
from urllib.parse import quote_plus

from playwright.sync_api import sync_playwright, Page, Route, TimeoutError as PWTimeout

from grid import geocode, tile_urls


# --- regex helpers -----------------------------------------------------------

COORDS_BANG_RE = re.compile(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)")
COORDS_AT_RE = re.compile(r"@(-?\d+\.\d+),(-?\d+\.\d+)")
FID_RE = re.compile(r"!1s(0x[0-9a-f]+:0x[0-9a-f]+)", re.IGNORECASE)
PLACE_ID_RE = re.compile(r"!19s([^!?/]+)")

# Resources we drop to keep memory low on free-tier hosts.
BLOCKED_RESOURCE_TYPES = {"image", "media", "font", "stylesheet"}
BLOCKED_URL_PATTERNS = (
    "googlevideo.com",
    "doubleclick.net",
    "google-analytics.com",
    "googletagmanager.com",
    "gstatic.com/images",
)


# --- low-level safe getters --------------------------------------------------

def _safe_text(page: Page, selector: str) -> Optional[str]:
    try:
        el = page.query_selector(selector)
        if not el:
            return None
        t = (el.inner_text() or "").strip()
        return t or None
    except Exception:
        return None


def _safe_attr(page: Page, selector: str, name: str) -> Optional[str]:
    try:
        el = page.query_selector(selector)
        return el.get_attribute(name) if el else None
    except Exception:
        return None


def _first_text(page: Page, selectors: list[str]) -> Optional[str]:
    for s in selectors:
        v = _safe_text(page, s)
        if v:
            return v
    return None


def _parse_coords(url: str):
    m = COORDS_BANG_RE.search(url) or COORDS_AT_RE.search(url)
    return {"lat": float(m.group(1)), "lng": float(m.group(2))} if m else None


def _parse_fid(url: str):
    m = FID_RE.search(url)
    return m.group(1) if m else None


# --- routing: drop heavy resources -------------------------------------------

def _route_filter(route: Route):
    req = route.request
    if req.resource_type in BLOCKED_RESOURCE_TYPES:
        return route.abort()
    url = req.url
    for p in BLOCKED_URL_PATTERNS:
        if p in url:
            return route.abort()
    return route.continue_()


# --- consent banner ----------------------------------------------------------

def _dismiss_consent(page: Page):
    for sel in (
        'button[aria-label*="Accept all" i]',
        'button[aria-label*="Reject all" i]',
        'button:has-text("Accept all")',
        'button:has-text("I agree")',
        'form[action*="consent"] button',
    ):
        btn = page.query_selector(sel)
        if btn:
            try:
                btn.click(timeout=2000)
                page.wait_for_timeout(800)
                return
            except Exception:
                pass


# --- closed-status detection -------------------------------------------------

# Google labels closed places with a colored banner near the header. The text
# string varies slightly across locales but on en-US it's always one of these.
PERMANENTLY_CLOSED_PHRASES = (
    "Permanently closed",
    "Permanent closed",
    "Closed permanently",
)


def _is_permanently_closed(page: Page) -> bool:
    """Return True if the place page shows a permanently-closed banner."""
    # Most reliable: the closure banner has a dedicated class. Selectors first,
    # text match as a fallback for redesigns.
    selectors = (
        'div.fCEvvc.fontBodyMedium',  # "Permanently closed" header label
        'span.aSftqf',                 # status pill
        '[aria-label*="Permanently closed" i]',
    )
    for sel in selectors:
        try:
            el = page.query_selector(sel)
        except Exception:
            continue
        if not el:
            continue
        try:
            txt = (el.inner_text() or "").strip().lower()
        except Exception:
            continue
        if any(p.lower() in txt for p in PERMANENTLY_CLOSED_PHRASES):
            return True

    # Text-based fallback: scan the side panel for the phrase.
    try:
        panel_text = page.evaluate(
            """() => {
                const el = document.querySelector('div[role="main"]') || document.body;
                return el ? el.innerText.slice(0, 5000) : '';
            }"""
        ) or ""
    except Exception:
        panel_text = ""
    panel_lower = panel_text.lower()
    return any(p.lower() in panel_lower for p in PERMANENTLY_CLOSED_PHRASES)


# --- place page extraction ---------------------------------------------------

def _extract_place(page: Page, url: str) -> Optional[dict]:
    # Retry navigation once if the page panel doesn't render in time.
    loaded = False
    for attempt in range(2):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            _dismiss_consent(page)
            page.wait_for_selector("h1.DUwDvf, h1", timeout=25000)
            loaded = True
            break
        except PWTimeout:
            if attempt == 0:
                page.wait_for_timeout(1500)
                continue
    if not loaded:
        return None  # silently skip — don't pollute output with error rows

    try:
        page.wait_for_timeout(600)  # let side panel hydrate

        # Skip permanently-closed places entirely.
        if _is_permanently_closed(page):
            return None

        # Title — current class is DUwDvf; fall back to bare h1.
        title = _first_text(page, ["h1.DUwDvf", "h1"])

        # Category sits inside the header button stack.
        category = _first_text(page, [
            'button.DkEaL',
            'div.skqShb button.DkEaL',
            'button[jsaction*="category"]',
        ])

        # Address row — button with data-item-id="address".
        address = _first_text(page, [
            'button[data-item-id="address"] div.fontBodyMedium',
            'button[data-item-id="address"] div.Io6YTe',
            '[data-item-id="address"]',
        ])
        if not address:
            address = _safe_attr(page, 'button[data-item-id="address"]', 'aria-label')
            if address:
                address = address.replace("Address:", "").strip()

        # Website — anchor with data-item-id="authority".
        website = _safe_attr(page, 'a[data-item-id="authority"]', "href")
        if not website:
            website = _safe_attr(page, 'a[data-tooltip="Open website"]', "href")

        # Phone — button data-item-id="phone:tel:+...".
        phone = _first_text(page, [
            'button[data-item-id^="phone:tel:"] div.fontBodyMedium',
            'button[data-item-id^="phone:tel:"] div.Io6YTe',
        ])
        if not phone:
            raw = _safe_attr(page, 'button[data-item-id^="phone:tel:"]', "data-item-id")
            if raw:
                phone = raw.replace("phone:tel:", "")

        # Rating + review count — pulled from the F7nice block under the h1.
        rating = None
        reviews_count = None
        score_block = _safe_text(page, "div.F7nice")
        if score_block:
            sm = re.search(r"(\d+[.,]\d+)", score_block)
            rm = re.search(r"\(([\d,\.\s]+)\)", score_block)
            if sm:
                rating = float(sm.group(1).replace(",", "."))
            if rm:
                num = re.sub(r"[^\d]", "", rm.group(1))
                if num:
                    reviews_count = int(num)
        if rating is None:
            aria = _safe_attr(page, 'div.F7nice span[aria-hidden="true"]', "aria-label")
            if aria:
                m = re.search(r"(\d+[.,]\d+)", aria)
                if m:
                    rating = float(m.group(1).replace(",", "."))

        final_url = page.url
        fid = _parse_fid(final_url)  # kept internal-only for cross-tile dedupe

        return {
            "title": title,
            "category": category,
            "address": address,
            "website": website,
            "phone": phone,
            "rating": rating,
            "reviews_count": reviews_count,
            "url": final_url,
            "_fid": fid,  # leading underscore = internal, stripped before output
        }
    except Exception:
        return None  # silently skip on extraction errors


# --- search feed scrolling ---------------------------------------------------

def _collect_place_links(page: Page, max_results: int | None, status_cb=None) -> list[str]:
    """Collect place URLs from a search-results feed.

    Pass max_results=None (or 0) to scrape until the end-of-list marker.
    """
    unlimited = max_results is None or max_results <= 0
    cap = float("inf") if unlimited else max_results

    feed_sel = 'div[role="feed"]'
    try:
        page.wait_for_selector(feed_sel, timeout=20000)
    except PWTimeout:
        if "/place/" in page.url:
            return [page.url]
        return []

    link_sel = f'{feed_sel} a.hfpxzc, {feed_sel} a[href*="/place/"]'

    seen: list[str] = []
    seen_set: set[str] = set()
    stagnant = 0
    # In unlimited mode allow more scroll attempts before giving up — Google
    # sometimes pauses loading for a few seconds mid-feed.
    stagnant_limit = 10 if unlimited else 6

    while len(seen) < cap and stagnant < stagnant_limit:
        hrefs = page.eval_on_selector_all(link_sel, "els => els.map(e => e.href)")
        before = len(seen)
        for h in hrefs:
            if h and h not in seen_set:
                seen_set.add(h)
                seen.append(h)
                if len(seen) >= cap:
                    break

        if status_cb:
            tail = "" if unlimited else f"/{int(cap)}"
            status_cb(f"Found {len(seen)}{tail} listings...")

        if len(seen) == before:
            stagnant += 1
        else:
            stagnant = 0

        if len(seen) >= cap:
            break

        # End-of-list marker.
        end_marker = page.query_selector(
            'p.fontBodyMedium span.HlvSq, '
            'p.fontBodyMedium:has-text("end of the list"), '
            'div.PbZDve'
        )
        if end_marker:
            if status_cb:
                status_cb(f"Reached end of Google's list — {len(seen)} places.")
            break

        try:
            page.eval_on_selector(feed_sel, "el => el.scrollBy(0, el.scrollHeight)")
        except Exception:
            break
        # Slightly longer pause in unlimited mode for slow chunks to load.
        page.wait_for_timeout(1800 if unlimited else 1500)

    return seen if unlimited else seen[:max_results]


# --- top-level orchestration -------------------------------------------------

def _build_search_urls(
    term: str,
    location: str,
    language: str,
    grid_size: int,
    status_cb=None,
) -> list[str]:
    """Return one URL for a normal search, or N×N tile URLs in grid mode."""
    if grid_size and grid_size > 1:
        if not location.strip():
            if status_cb:
                status_cb("Grid search needs a location — falling back to single search.")
        else:
            if status_cb:
                status_cb(f'Geocoding "{location}"...')
            geo = geocode(location)
            if geo:
                urls = tile_urls(term, geo["bbox"], grid_size, language=language)
                if status_cb:
                    status_cb(f"Grid search: {len(urls)} tiles for \"{term}\"")
                return urls
            if status_cb:
                status_cb("Geocoding failed — falling back to single search.")

    # Single-search fallback.
    query = f"{term} in {location}" if location.strip() else term
    return [
        f"https://www.google.com/maps/search/{quote_plus(query)}/?hl={language}"
    ]


def scrape_google_maps(
    search_terms: list[str],
    location: str = "",
    locations: list[str] | None = None,
    max_results: int | None = 20,
    language: str = "en",
    headless: bool = True,
    grid_size: int = 1,
    status_cb=None,
) -> Iterator[dict]:
    """Yield place dicts as they are scraped (streaming for the Streamlit UI).

    grid_size > 1 enables deep search: the bbox of `location` is split into a
    grid_size × grid_size grid and each cell is scraped, deduped by fid.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-sync",
                "--disable-translate",
                "--mute-audio",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        context = browser.new_context(
            locale=language,
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            java_script_enabled=True,
        )
        context.add_cookies([
            {"name": "CONSENT", "value": "YES+1", "domain": ".google.com", "path": "/"},
            {"name": "SOCS", "value": "CAESHAgBEhJnd3NfMjAyMzA4MjItMF9SQzIaAmVuIAEaBgiA_LyaBg",
             "domain": ".google.com", "path": "/"},
        ])
        context.route("**/*", _route_filter)
        page = context.new_page()
        page.set_default_timeout(30000)

        # Normalize the location input: prefer the `locations` list when given.
        loc_list = [l for l in (locations or []) if l and l.strip()] or [location]

        try:
            seen_fids: set[str] = set()
            seen_urls: set[str] = set()

            for term in search_terms:
                term = term.strip()
                if not term:
                    continue

                for loc in loc_list:
                    if status_cb and len(loc_list) > 1:
                        status_cb(f'[{term}] location: {loc or "(none)"}')

                    search_urls = _build_search_urls(
                        term, loc, language, grid_size, status_cb,
                    )

                    # First pass over every tile: collect place URLs, dedupe.
                    tile_place_urls: list[str] = []
                    for tile_idx, url in enumerate(search_urls, 1):
                        if status_cb:
                            status_cb(
                                f'[{term}] tile {tile_idx}/{len(search_urls)} — loading map'
                            )
                        try:
                            page.goto(url, wait_until="domcontentloaded", timeout=60000)
                        except PWTimeout:
                            if status_cb:
                                status_cb(f'Timeout on tile {tile_idx}, skipping.')
                            continue
                        _dismiss_consent(page)
                        links = _collect_place_links(page, max_results, status_cb)
                        new = 0
                        for link in links:
                            if link in seen_urls:
                                continue
                            seen_urls.add(link)
                            tile_place_urls.append(link)
                            new += 1
                        if status_cb:
                            status_cb(
                                f'[{term}] tile {tile_idx}/{len(search_urls)} — '
                                f'{new} new (total {len(tile_place_urls)})'
                            )

                    if status_cb:
                        status_cb(
                            f'[{term}] {len(tile_place_urls)} unique places to scrape '
                            f'({loc or "global"})'
                        )

                    # Second pass: visit each place detail page once.
                    for i, link in enumerate(tile_place_urls, 1):
                        if status_cb:
                            status_cb(f'[{term}] {i}/{len(tile_place_urls)}')
                        place = _extract_place(page, link)
                        if not place:
                            continue
                        fid = place.pop("_fid", None)  # internal, never output
                        if fid:
                            if fid in seen_fids:
                                continue
                            seen_fids.add(fid)
                        place["search_term"] = term
                        place["search_location"] = loc
                        yield place
                        time.sleep(0.4)
        finally:
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass
