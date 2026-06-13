"""Google Maps scraper using Playwright (async).

Optimizations (all live):
  #1 Phase-1 parallelism: place-detail visits run on 3 concurrent pages
     in a single shared browser context (~3× speedup on detail extraction).
  #2 fid pre-dedup: place URLs are deduped by Google's fid before any visit,
     killing redundant work across grid tiles.
  #6 Persistent cache: scraped place dicts are stored in cache.py keyed by fid;
     subsequent runs of the same/overlapping query short-circuit the visit.

The public API stays sync — `scrape_google_maps()` is still a regular
generator that yields place dicts as they're ready. Internally we run an
asyncio loop on a background thread and stream items through a queue.

Selectors target current Google Maps DOM with fallbacks for A/B variants.
"""
from __future__ import annotations

import asyncio
import queue
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional
from urllib.parse import quote_plus

from playwright.async_api import (
    async_playwright,
    BrowserContext,
    Page,
    Route,
    TimeoutError as PWTimeout,
)

import cache as place_cache
from grid import geocode, tile_urls


# --- control: stop + pause + run-id ----------------------------------------

@dataclass
class ScrapeControl:
    """Cross-thread coordination. Streamlit holds the events; scraper polls them."""
    stop_event: threading.Event = field(default_factory=threading.Event)
    pause_event: threading.Event = field(default_factory=threading.Event)
    signature: Optional[str] = None  # current run id for persistent state
    stats: dict = field(default_factory=lambda: {
        "closed_skipped": 0,
        "extract_failed": 0,
        "captchas_hit": 0,
        "tiles_timed_out": 0,
        "prededuped": 0,
        "cache_hits": 0,
        "places_yielded": 0,
    })


class StopRequested(Exception):
    pass


# --- network retry backoff ---------------------------------------------------

RETRY_BACKOFFS = (5, 10, 30, 60)  # seconds between attempts


# --- regex helpers -----------------------------------------------------------

FID_RE = re.compile(r"!1s(0x[0-9a-f]+:0x[0-9a-f]+)", re.IGNORECASE)

# Resources we drop to keep memory low on free-tier hosts.
BLOCKED_RESOURCE_TYPES = {"image", "media", "font", "stylesheet"}
BLOCKED_URL_PATTERNS = (
    "googlevideo.com",
    "doubleclick.net",
    "google-analytics.com",
    "googletagmanager.com",
    "gstatic.com/images",
)

# Closure detection.
PERMANENTLY_CLOSED_PHRASES = (
    "Permanently closed",
    "Permanent closed",
    "Closed permanently",
)


# --- small async helpers -----------------------------------------------------

async def _safe_text(page: Page, selector: str) -> Optional[str]:
    try:
        el = await page.query_selector(selector)
        if not el:
            return None
        t = (await el.inner_text() or "").strip()
        return t or None
    except Exception:
        return None


async def _safe_attr(page: Page, selector: str, name: str) -> Optional[str]:
    try:
        el = await page.query_selector(selector)
        return await el.get_attribute(name) if el else None
    except Exception:
        return None


async def _first_text(page: Page, selectors: list[str]) -> Optional[str]:
    for s in selectors:
        v = await _safe_text(page, s)
        if v:
            return v
    return None


def _parse_fid(url: str) -> Optional[str]:
    m = FID_RE.search(url)
    return m.group(1) if m else None


# --- routing -----------------------------------------------------------------

async def _route_filter(route: Route):
    req = route.request
    if req.resource_type in BLOCKED_RESOURCE_TYPES:
        return await route.abort()
    url = req.url
    for p in BLOCKED_URL_PATTERNS:
        if p in url:
            return await route.abort()
    return await route.continue_()


# --- consent banner ----------------------------------------------------------

async def _dismiss_consent(page: Page):
    for sel in (
        'button[aria-label*="Accept all" i]',
        'button[aria-label*="Reject all" i]',
        'button:has-text("Accept all")',
        'button:has-text("I agree")',
        'form[action*="consent"] button',
    ):
        try:
            btn = await page.query_selector(sel)
        except Exception:
            continue
        if btn:
            try:
                await btn.click(timeout=2000)
                await page.wait_for_timeout(800)
                return
            except Exception:
                pass


# --- control polling, retry, CAPTCHA -----------------------------------------

async def _wait_if_paused(control: ScrapeControl, say):
    """Honor pause + stop. Returns when allowed to continue, raises if stopped."""
    paused_announced = False
    while control.pause_event.is_set():
        if control.stop_event.is_set():
            raise StopRequested()
        if not paused_announced:
            say("Paused — click Resume to continue.")
            paused_announced = True
        await asyncio.sleep(0.5)
    if control.stop_event.is_set():
        raise StopRequested()


async def _goto_with_retry(page: Page, url: str, say, control: ScrapeControl,
                           timeout: int = 45000) -> bool:
    """page.goto with exponential backoff on transient network errors.

    Returns True on success, False on permanent failure (after all retries).
    Honors stop/pause between attempts.
    """
    last_err: Optional[str] = None
    for attempt, backoff in enumerate((0, *RETRY_BACKOFFS)):
        if backoff:
            say(f"Network hiccup ({last_err}) — retrying in {backoff}s "
                f"({attempt}/{len(RETRY_BACKOFFS)})...")
            for _ in range(backoff * 2):
                await _wait_if_paused(control, say)
                await asyncio.sleep(0.5)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            return True
        except PWTimeout:
            last_err = "timeout"
        except Exception as e:
            last_err = type(e).__name__
    say(f"Giving up on URL after {len(RETRY_BACKOFFS) + 1} attempts.")
    return False


async def _is_captcha(page: Page) -> bool:
    """Detect Google's anti-bot challenge page."""
    try:
        cur = page.url or ""
    except Exception:
        cur = ""
    if "/sorry/" in cur or "google.com/sorry" in cur:
        return True
    # Selector / text fallbacks.
    for sel in (
        'iframe[src*="recaptcha"]',
        'form[action*="/sorry/"]',
        'div#captcha-form',
    ):
        try:
            if await page.query_selector(sel):
                return True
        except Exception:
            continue
    try:
        text = await page.evaluate(
            "() => (document.body && document.body.innerText || '').slice(0,2000)"
        ) or ""
    except Exception:
        text = ""
    text_l = text.lower()
    return any(p in text_l for p in (
        "our systems have detected unusual traffic",
        "unusual traffic from your computer network",
        "captcha",
    ))


async def _handle_captcha(page: Page, say, control: ScrapeControl,
                          headless: bool) -> bool:
    """If a CAPTCHA is up, either wait it out (visible browser) or stop.

    Returns True if cleared and safe to continue, False if we must abort.
    """
    if not await _is_captcha(page):
        return True
    control.stats["captchas_hit"] = control.stats.get("captchas_hit", 0) + 1
    if headless:
        say("CAPTCHA detected (headless). Stopping — try again later or "
            "run locally with the browser visible.")
        control.stop_event.set()
        return False
    say("CAPTCHA detected — solve it in the open browser window. "
        "I'll resume automatically once it's cleared.")
    # Poll every 5s, up to 10 min.
    for _ in range(120):
        if control.stop_event.is_set():
            return False
        await asyncio.sleep(5)
        if not await _is_captcha(page):
            say("CAPTCHA cleared — resuming.")
            return True
    say("CAPTCHA wait timed out (10 min). Stopping.")
    control.stop_event.set()
    return False


# --- closed-status detection -------------------------------------------------

async def _is_permanently_closed(page: Page) -> bool:
    selectors = (
        "div.fCEvvc.fontBodyMedium",
        "span.aSftqf",
        '[aria-label*="Permanently closed" i]',
    )
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
        except Exception:
            continue
        if not el:
            continue
        try:
            txt = (await el.inner_text() or "").strip().lower()
        except Exception:
            continue
        if any(p.lower() in txt for p in PERMANENTLY_CLOSED_PHRASES):
            return True

    try:
        panel_text = await page.evaluate(
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

async def _extract_place(
    page: Page,
    url: str,
    say,
    control: ScrapeControl,
    headless: bool,
) -> Optional[dict]:
    if not await _goto_with_retry(page, url, say, control):
        return None
    await _dismiss_consent(page)
    if not await _handle_captcha(page, say, control, headless):
        return None
    try:
        await page.wait_for_selector("h1.DUwDvf, h1", timeout=25000)
    except PWTimeout:
        return None

    try:
        await page.wait_for_timeout(500)

        if await _is_permanently_closed(page):
            return {"_closed_skip": True}

        title = await _first_text(page, ["h1.DUwDvf", "h1"])
        category = await _first_text(page, [
            "button.DkEaL",
            "div.skqShb button.DkEaL",
            'button[jsaction*="category"]',
        ])
        address = await _first_text(page, [
            'button[data-item-id="address"] div.fontBodyMedium',
            'button[data-item-id="address"] div.Io6YTe',
            '[data-item-id="address"]',
        ])
        if not address:
            address = await _safe_attr(page, 'button[data-item-id="address"]', "aria-label")
            if address:
                address = address.replace("Address:", "").strip()

        website = await _safe_attr(page, 'a[data-item-id="authority"]', "href")
        if not website:
            website = await _safe_attr(page, 'a[data-tooltip="Open website"]', "href")

        phone = await _first_text(page, [
            'button[data-item-id^="phone:tel:"] div.fontBodyMedium',
            'button[data-item-id^="phone:tel:"] div.Io6YTe',
        ])
        if not phone:
            raw = await _safe_attr(page, 'button[data-item-id^="phone:tel:"]', "data-item-id")
            if raw:
                phone = raw.replace("phone:tel:", "")

        rating = None
        reviews_count = None
        score_block = await _safe_text(page, "div.F7nice")
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
            aria = await _safe_attr(page, 'div.F7nice span[aria-hidden="true"]', "aria-label")
            if aria:
                m = re.search(r"(\d+[.,]\d+)", aria)
                if m:
                    rating = float(m.group(1).replace(",", "."))

        final_url = page.url
        fid = _parse_fid(final_url)

        return {
            "title": title,
            "category": category,
            "address": address,
            "website": website,
            "phone": phone,
            "rating": rating,
            "reviews_count": reviews_count,
            "url": final_url,
            "_fid": fid,
        }
    except Exception:
        return None


# --- search feed scrolling ---------------------------------------------------

async def _collect_place_links(
    page: Page,
    max_results: int | None,
    say,
    control: ScrapeControl,
    headless: bool,
) -> list[str]:
    unlimited = max_results is None or max_results <= 0
    cap = float("inf") if unlimited else max_results

    feed_sel = 'div[role="feed"]'
    try:
        await page.wait_for_selector(feed_sel, timeout=20000)
    except PWTimeout:
        if "/place/" in page.url:
            return [page.url]
        return []

    link_sel = f'{feed_sel} a.hfpxzc, {feed_sel} a[href*="/place/"]'
    seen: list[str] = []
    seen_set: set[str] = set()
    stagnant = 0
    stagnant_limit = 10 if unlimited else 6

    while len(seen) < cap and stagnant < stagnant_limit:
        await _wait_if_paused(control, say)
        if await _is_captcha(page):
            if not await _handle_captcha(page, say, control, headless):
                break
        hrefs = await page.eval_on_selector_all(link_sel, "els => els.map(e => e.href)")
        before = len(seen)
        for h in hrefs:
            if h and h not in seen_set:
                seen_set.add(h)
                seen.append(h)
                if len(seen) >= cap:
                    break

        if len(seen) == before:
            stagnant += 1
        else:
            stagnant = 0

        if len(seen) >= cap:
            break

        end_marker = await page.query_selector(
            "p.fontBodyMedium span.HlvSq, "
            'p.fontBodyMedium:has-text("end of the list"), '
            "div.PbZDve"
        )
        if end_marker:
            break

        try:
            await page.eval_on_selector(feed_sel, "el => el.scrollBy(0, el.scrollHeight)")
        except Exception:
            break
        await page.wait_for_timeout(1800 if unlimited else 1500)

    return seen if unlimited else seen[: int(cap)]


# --- URL helpers -------------------------------------------------------------

def _build_search_urls(
    term: str, location: str, language: str, grid_size: int, say
) -> list[str]:
    if grid_size and grid_size > 1:
        if not location.strip():
            say("Grid search needs a location — falling back to single search.")
        else:
            say(f'Geocoding "{location}"...')
            geo = geocode(location)
            if geo:
                urls = tile_urls(term, geo["bbox"], grid_size, language=language)
                say(f'Grid search: {len(urls)} tiles for "{term}"')
                return urls
            say("Geocoding failed — falling back to single search.")

    query = f"{term} in {location}" if location.strip() else term
    return [f"https://www.google.com/maps/search/{quote_plus(query)}/?hl={language}"]


def _dedupe_urls_by_fid(urls: list[str]) -> list[str]:
    """Drop URLs that share an fid we've already queued. The fid lives inside
    the URL string, so no network calls are needed for this dedupe."""
    seen: set[str] = set()
    keep: list[str] = []
    for u in urls:
        fid = _parse_fid(u)
        if fid:
            if fid in seen:
                continue
            seen.add(fid)
        keep.append(u)
    return keep


# --- async main --------------------------------------------------------------

async def _scrape_async(
    out_q: queue.Queue,
    *,
    search_terms: list[str],
    locations: list[str],
    max_results: int | None,
    language: str,
    headless: bool,
    grid_size: int,
    use_cache: bool,
    parallelism: int,
    control: ScrapeControl,
    resume_done_fids: set[str],
    resume_completed_tiles: set[tuple],
):
    def say(msg: str):
        out_q.put(("status", msg))

    async with async_playwright() as p:
        browser = await p.chromium.launch(
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
        context = await browser.new_context(
            locale=language,
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        await context.add_cookies([
            {"name": "CONSENT", "value": "YES+1", "domain": ".google.com", "path": "/"},
            {"name": "SOCS", "value": "CAESHAgBEhJnd3NfMjAyMzA4MjItMF9SQzIaAmVuIAEaBgiA_LyaBg",
             "domain": ".google.com", "path": "/"},
        ])
        await context.route("**/*", _route_filter)

        seen_fids: set[str] = set(resume_done_fids)
        try:
            for term in search_terms:
                term = term.strip()
                if not term:
                    continue
                for loc in locations:
                    await _wait_if_paused(control, say)
                    if len(locations) > 1:
                        say(f'[{term}] location: {loc or "(none)"}')

                    search_urls = _build_search_urls(term, loc, language, grid_size, say)

                    # Phase A: collect place URLs across every tile.
                    tile_place_urls: list[str] = []
                    feed_page = await context.new_page()
                    try:
                        for tile_idx, url in enumerate(search_urls, 1):
                            await _wait_if_paused(control, say)
                            tile_key = (term, loc, tile_idx)
                            if tile_key in resume_completed_tiles:
                                say(f"[{term}] tile {tile_idx}/{len(search_urls)} — already done, skipping")
                                continue
                            say(f"[{term}] tile {tile_idx}/{len(search_urls)} — loading map")
                            if not await _goto_with_retry(feed_page, url, say, control, timeout=60000):
                                control.stats["tiles_timed_out"] = control.stats.get("tiles_timed_out", 0) + 1
                                say(f"Skipped tile {tile_idx} after retries.")
                                continue
                            await _dismiss_consent(feed_page)
                            if not await _handle_captcha(feed_page, say, control, headless):
                                break
                            links = await _collect_place_links(
                                feed_page, max_results, say, control, headless,
                            )
                            tile_place_urls.extend(links)
                            say(
                                f"[{term}] tile {tile_idx}/{len(search_urls)} — "
                                f"{len(links)} found"
                            )
                            if control.signature:
                                place_cache.add_completed_tile(control.signature, tile_key)
                    finally:
                        await feed_page.close()

                    # fid pre-dedup (optimization #2) — kills duplicates from
                    # overlapping grid tiles before any extra page load.
                    deduped = _dedupe_urls_by_fid(tile_place_urls)
                    pruned = len(tile_place_urls) - len(deduped)
                    if pruned:
                        control.stats["prededuped"] = control.stats.get("prededuped", 0) + pruned
                        say(f"[{term}] pre-dedup removed {pruned} duplicate URL(s)")

                    # Strip URLs whose fid we already yielded earlier in this run.
                    fresh: list[str] = []
                    for u in deduped:
                        f = _parse_fid(u)
                        if f and f in seen_fids:
                            continue
                        fresh.append(u)

                    # Cache hits (optimization #6) — skip the visit entirely.
                    to_visit: list[str] = []
                    cache_hits = 0
                    for u in fresh:
                        f = _parse_fid(u)
                        cached = place_cache.get(f) if (use_cache and f) else None
                        if cached:
                            cache_hits += 1
                            if f:
                                seen_fids.add(f)
                            cached["search_term"] = term
                            cached["search_location"] = loc
                            out_q.put(("place", cached))
                        else:
                            to_visit.append(u)
                    if cache_hits:
                        control.stats["cache_hits"] = control.stats.get("cache_hits", 0) + cache_hits
                        say(f"[{term}] cache hits: {cache_hits}")

                    if not to_visit:
                        continue

                    say(
                        f"[{term}] visiting {len(to_visit)} place(s) "
                        f"with {parallelism}× concurrency"
                    )

                    # Phase B: parallel detail visits (optimization #1).
                    sem = asyncio.Semaphore(parallelism)
                    counter = {"n": 0}
                    total_n = len(to_visit)

                    async def visit(u: str):
                        async with sem:
                            if control.stop_event.is_set():
                                return
                            await _wait_if_paused(control, say)
                            page = await context.new_page()
                            try:
                                place = await _extract_place(
                                    page, u, say, control, headless,
                                )
                            finally:
                                try:
                                    await page.close()
                                except Exception:
                                    pass

                            counter["n"] += 1
                            say(f"[{term}] {counter['n']}/{total_n}")

                            if not place:
                                control.stats["extract_failed"] = control.stats.get("extract_failed", 0) + 1
                                return
                            if place.get("_closed_skip"):
                                control.stats["closed_skipped"] = control.stats.get("closed_skipped", 0) + 1
                                return
                            f = place.pop("_fid", None)
                            if f:
                                if f in seen_fids:
                                    return
                                seen_fids.add(f)
                            place["search_term"] = term
                            place["search_location"] = loc
                            if use_cache and f:
                                place_cache.put(f, place)
                            if control.signature and f:
                                place_cache.add_done_fid(control.signature, f)
                            control.stats["places_yielded"] = control.stats.get("places_yielded", 0) + 1
                            out_q.put(("place", place))

                    try:
                        await asyncio.gather(*(visit(u) for u in to_visit))
                    except StopRequested:
                        say("Stop requested — shutting down.")
                        return
        finally:
            try:
                await context.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass


# --- sync generator façade ---------------------------------------------------

_SENTINEL: Any = object()


def scrape_google_maps(
    search_terms: list[str],
    location: str = "",
    locations: list[str] | None = None,
    max_results: int | None = 20,
    language: str = "en",
    headless: bool = True,
    grid_size: int = 1,
    use_cache: bool = True,
    parallelism: int = 3,
    control: ScrapeControl | None = None,
    resume: bool = False,
    status_cb=None,
) -> Iterator[dict]:
    """Streaming sync generator. Internally runs an asyncio loop on a thread.

    Pass a ScrapeControl to enable pause/stop and persistent resume.
    """
    loc_list = [l for l in (locations or []) if l and l.strip()] or [location]
    ctrl = control or ScrapeControl()

    # Persistent run-state init.
    params = {
        "search_terms": search_terms,
        "locations": loc_list,
        "max_results": max_results,
        "language": language,
        "grid_size": grid_size,
    }
    signature = place_cache.make_signature(params)
    ctrl.signature = signature

    resume_done_fids: set[str] = set()
    resume_completed_tiles: set[tuple] = set()
    if resume:
        existing = place_cache.get_run(signature)
        if existing:
            resume_done_fids = existing.get("done_fids") or set()
            resume_completed_tiles = existing.get("completed_tiles") or set()

    place_cache.start_run(signature, params)

    out_q: queue.Queue = queue.Queue(maxsize=128)
    err_holder: list[BaseException] = []

    def runner():
        final_status = "done"
        try:
            asyncio.run(
                _scrape_async(
                    out_q,
                    search_terms=search_terms,
                    locations=loc_list,
                    max_results=max_results,
                    language=language,
                    headless=headless,
                    grid_size=grid_size,
                    use_cache=use_cache,
                    parallelism=parallelism,
                    control=ctrl,
                    resume_done_fids=resume_done_fids,
                    resume_completed_tiles=resume_completed_tiles,
                )
            )
            if ctrl.stop_event.is_set():
                final_status = "stopped"
        except StopRequested:
            final_status = "stopped"
        except BaseException as e:
            err_holder.append(e)
            final_status = "crashed"
        finally:
            try:
                place_cache.set_run_status(signature, final_status)
            except Exception:
                pass
            out_q.put(("stats", dict(ctrl.stats)))
            out_q.put(("done", _SENTINEL))

    t = threading.Thread(target=runner, daemon=True)
    t.start()

    while True:
        kind, payload = out_q.get()
        if kind == "done":
            break
        if kind == "status":
            if status_cb:
                try:
                    status_cb(payload)
                except Exception:
                    pass
            continue
        if kind == "place":
            yield payload

    if err_holder:
        raise err_holder[0]
