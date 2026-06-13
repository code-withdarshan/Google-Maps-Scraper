"""Website enrichment: pull emails + social profiles from a business's site.

Visits the homepage and a few likely-contact subpages, runs regex over the
combined HTML, and returns deduplicated lists of emails and per-platform
social URLs.

Kept dependency-light (httpx + bs4) so it stays fast on Streamlit free tier.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterator
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

CONTACT_PATHS = (
    "/contact",
    "/contact-us",
    "/contact.html",
    "/about",
    "/about-us",
    "/impressum",  # common on EU sites
)

# Email regex — broad, then we filter obvious junk.
EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}",
)

# Things that look like emails but aren't real contacts.
EMAIL_BLOCKLIST = (
    "sentry.io", "wixpress.com", "wix.com", "godaddy.com", "example.com",
    "yourdomain.com", "domain.com", "email.com", "test.com", "sample.com",
    "noreply", "no-reply", "donotreply", ".png", ".jpg", ".jpeg", ".gif",
    ".webp", ".svg", "u003e", "u003c",
)

# Social platform host → output field name.
SOCIAL_HOSTS = {
    "facebook.com":  "facebook",
    "fb.com":        "facebook",
    "m.facebook.com": "facebook",
    "instagram.com": "instagram",
    "linkedin.com":  "linkedin",
    "twitter.com":   "twitter",
    "x.com":         "twitter",
    "youtube.com":   "youtube",
    "youtu.be":      "youtube",
    "tiktok.com":    "tiktok",
    "pinterest.com": "pinterest",
    "wa.me":         "whatsapp",
    "api.whatsapp.com": "whatsapp",
    "t.me":          "telegram",
}

# Junk paths on social hosts that aren't profile links.
SOCIAL_JUNK = ("/sharer", "/share", "/intent", "/plugins", "/tr?", "/dialog")


def _normalize_url(u: str) -> str:
    if not u:
        return ""
    u = u.strip()
    if u.startswith("//"):
        return "https:" + u
    if not u.startswith(("http://", "https://")):
        return "https://" + u
    return u


def _clean_email(e: str) -> str | None:
    e = e.strip().strip(".,;:").lower()
    if any(b in e for b in EMAIL_BLOCKLIST):
        return None
    # Reject pseudo-emails like "2x@2x.png".
    if re.search(r"@\d+x\.", e):
        return None
    return e


def _extract_emails(html: str) -> list[str]:
    found = set()
    # mailto links — most reliable.
    for m in re.finditer(r'mailto:([^"\'>\s?]+)', html, flags=re.IGNORECASE):
        e = _clean_email(m.group(1))
        if e:
            found.add(e)
    # Plain-text emails.
    for m in EMAIL_RE.finditer(html):
        e = _clean_email(m.group(0))
        if e:
            found.add(e)
    return sorted(found)


def _classify_social(href: str) -> tuple[str, str] | None:
    try:
        p = urlparse(href)
    except Exception:
        return None
    host = (p.netloc or "").lower().lstrip("www.")
    if not host:
        return None
    for h, platform in SOCIAL_HOSTS.items():
        if host == h or host.endswith("." + h):
            if any(j in href for j in SOCIAL_JUNK):
                return None
            return platform, href
    return None


def _extract_socials(soup: BeautifulSoup) -> dict[str, list[str]]:
    out: dict[str, set[str]] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:")):
            continue
        classified = _classify_social(href)
        if not classified:
            continue
        platform, url = classified
        # Strip trailing junk params.
        url = url.split("?")[0].rstrip("/")
        out.setdefault(platform, set()).add(url)
    return {k: sorted(v) for k, v in out.items()}


def _fetch(client: httpx.Client, url: str) -> str | None:
    try:
        r = client.get(url, follow_redirects=True, timeout=10.0)
        if r.status_code >= 400:
            return None
        ctype = r.headers.get("content-type", "")
        if "html" not in ctype and "text" not in ctype:
            return None
        return r.text
    except Exception:
        return None


def enrich_website(
    url: str,
    max_pages: int = 3,
    client: httpx.Client | None = None,
    need_email: bool = True,
) -> dict:
    """Return dict with `emails` (list[str]) and `socials` (dict[platform, list[str]]).

    client: reuse a shared httpx.Client for connection pooling. If None, a
        per-call client is created (slower).
    need_email: when False (e.g. only socials are wanted), skip the contact /
        about pages — socials are almost always on the homepage. Drops Phase 2
        per-site latency by ~3×.
    """
    result = {"emails": [], "socials": {}}
    url = _normalize_url(url)
    if not url:
        return result

    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    pages: list[str] = [url]
    if need_email:
        for path in CONTACT_PATHS:
            if len(pages) >= max_pages:
                break
            pages.append(urljoin(base, path))
    # else: homepage-only (socials only)

    all_html_parts: list[str] = []

    own_client = client is None
    c = client or httpx.Client(headers=HEADERS, http2=False)
    try:
        fetched = 0
        for p in pages:
            if fetched >= max_pages:
                break
            html = _fetch(c, p)
            if html is None:
                continue
            fetched += 1
            all_html_parts.append(html)
    finally:
        if own_client:
            c.close()

    if not all_html_parts:
        return result

    combined_html = "\n".join(all_html_parts)
    combined_soup = BeautifulSoup(combined_html, "lxml")

    if need_email:
        result["emails"] = _extract_emails(combined_html)
    result["socials"] = _extract_socials(combined_soup)
    return result


# --- Batch / parallel enrichment for the UI ---------------------------------

SOCIAL_FIELDS = ("facebook", "instagram", "linkedin", "twitter",
                 "youtube", "tiktok", "pinterest", "whatsapp", "telegram")


def _apply_enrichment(
    place: dict,
    enrichment: dict,
    channels: dict[str, bool] | None = None,
) -> dict:
    """Merge an enrichment payload into a place dict.

    channels controls which fields are written. Keys: 'email' and any social
    name from SOCIAL_FIELDS. If channels is None, all fields are written
    (legacy behavior).
    """
    write_all = channels is None
    if write_all or channels.get("email"):
        place["emails"] = enrichment.get("emails", []) or []
    socials = enrichment.get("socials", {}) or {}
    for field in SOCIAL_FIELDS:
        if write_all or channels.get(field):
            place[field] = socials.get(field, [])
    return place


def enrich_places_batch(
    places: list[dict],
    max_workers: int = 5,
    max_pages_per_site: int = 3,
    channels: dict[str, bool] | None = None,
    progress_cb=None,
) -> Iterator[tuple[int, dict]]:
    """Enrich a list of places in parallel.

    channels selects which fields to populate. Example:
        channels = {"email": True, "instagram": True}
    Other social fields are NOT seeded or written. If channels is None, all
    enrichment fields are written (legacy behavior).

    Yields (index, enriched_place) tuples as each finishes.
    progress_cb(done, total) is called after each completion.
    """
    write_all = channels is None
    enabled_socials = [
        f for f in SOCIAL_FIELDS if write_all or channels.get(f)
    ]
    seed_email = write_all or channels.get("email")

    # Pre-seed only the enabled fields so the dataframe has stable columns
    # without polluting the output with unrequested channels.
    for p in places:
        if seed_email:
            p.setdefault("emails", [])
        for f in enabled_socials:
            p.setdefault(f, [])

    todo: list[tuple[int, str]] = []
    total = len(places)
    done = 0

    for i, p in enumerate(places):
        site = (p.get("website") or "").strip()
        if not site or p.get("error"):
            done += 1
            if progress_cb:
                progress_cb(done, total)
            yield i, p
        else:
            todo.append((i, site))

    if not todo:
        return

    # Smart page count: when email is NOT requested, skip contact/about pages.
    need_email = write_all or (channels or {}).get("email", False)

    # One shared httpx client across all worker threads → keep-alive + pooling.
    shared_client = httpx.Client(
        headers=HEADERS,
        http2=False,
        limits=httpx.Limits(
            max_keepalive_connections=max_workers * 2,
            max_connections=max_workers * 4,
        ),
    )

    try:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {
                ex.submit(
                    enrich_website,
                    site,
                    max_pages_per_site,
                    shared_client,
                    need_email,
                ): idx
                for idx, site in todo
            }
            for fut in as_completed(futures):
                idx = futures[fut]
                try:
                    enrichment = fut.result()
                except Exception as e:
                    places[idx]["enrichment_error"] = str(e)
                    enrichment = {"emails": [], "socials": {}}
                _apply_enrichment(places[idx], enrichment, channels)
                done += 1
                if progress_cb:
                    progress_cb(done, total)
                yield idx, places[idx]
    finally:
        shared_client.close()
