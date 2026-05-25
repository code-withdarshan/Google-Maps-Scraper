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


def enrich_website(url: str, max_pages: int = 3) -> dict:
    """Return dict with `emails` (list[str]) and `socials` (dict[platform, list[str]])."""
    result = {"emails": [], "socials": {}}
    url = _normalize_url(url)
    if not url:
        return result

    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    pages: list[str] = [url]
    for path in CONTACT_PATHS:
        if len(pages) >= max_pages:
            break
        pages.append(urljoin(base, path))

    all_html_parts: list[str] = []
    combined_soup = BeautifulSoup("", "lxml")

    with httpx.Client(headers=HEADERS, http2=False) as client:
        fetched = 0
        for p in pages:
            if fetched >= max_pages:
                break
            html = _fetch(client, p)
            if html is None:
                continue
            fetched += 1
            all_html_parts.append(html)

    if not all_html_parts:
        return result

    combined_html = "\n".join(all_html_parts)
    combined_soup = BeautifulSoup(combined_html, "lxml")

    result["emails"] = _extract_emails(combined_html)
    result["socials"] = _extract_socials(combined_soup)
    return result


# --- Batch / parallel enrichment for the UI ---------------------------------

SOCIAL_FIELDS = ("facebook", "instagram", "linkedin", "twitter",
                 "youtube", "tiktok", "pinterest", "whatsapp", "telegram")


def _apply_enrichment(place: dict, enrichment: dict) -> dict:
    """Merge an enrichment payload into a place dict (in-place safe copy)."""
    place["emails"] = enrichment.get("emails", []) or []
    socials = enrichment.get("socials", {}) or {}
    for field in SOCIAL_FIELDS:
        place[field] = socials.get(field, [])
    return place


def enrich_places_batch(
    places: list[dict],
    max_workers: int = 5,
    max_pages_per_site: int = 3,
    progress_cb=None,
) -> Iterator[tuple[int, dict]]:
    """Enrich a list of places in parallel.

    Yields (index, enriched_place) tuples as each finishes, so the caller
    can update the UI progressively. Places without a website are yielded
    immediately (with empty enrichment fields).

    progress_cb(done, total) is called after each completion.
    """
    # Pre-seed enrichment fields so the dataframe has stable columns.
    for p in places:
        p.setdefault("emails", [])
        for f in SOCIAL_FIELDS:
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

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {
            ex.submit(enrich_website, site, max_pages_per_site): idx
            for idx, site in todo
        }
        for fut in as_completed(futures):
            idx = futures[fut]
            try:
                enrichment = fut.result()
            except Exception as e:
                places[idx]["enrichment_error"] = str(e)
                enrichment = {"emails": [], "socials": {}}
            _apply_enrichment(places[idx], enrichment)
            done += 1
            if progress_cb:
                progress_cb(done, total)
            yield idx, places[idx]
