"""Small utilities for cleaning scraped fields before output.

- `normalize_phone`: turn Google's freeform phone strings into E.164.
- `domain_of`: extract a comparable domain string from a website URL.
- `should_exclude_domain`: substring-match a URL against a user-supplied blocklist.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

import phonenumbers


# Country-name → ISO2 region map. Covers the cases the location dropdowns produce.
COUNTRY_TO_REGION = {
    "United States": "US", "United States of America": "US", "USA": "US", "US": "US",
    "India": "IN",
    "United Kingdom": "GB", "United Kingdom of Great Britain and Northern Ireland": "GB",
    "UK": "GB", "Britain": "GB", "Great Britain": "GB", "England": "GB",
    "Canada": "CA",
    "Australia": "AU",
    "Germany": "DE",
    "France": "FR",
    "Italy": "IT",
    "Spain": "ES",
    "Netherlands": "NL",
    "Belgium": "BE",
    "Switzerland": "CH",
    "Ireland": "IE",
    "Sweden": "SE",
    "Norway": "NO",
    "Denmark": "DK",
    "Finland": "FI",
    "Poland": "PL",
    "Portugal": "PT",
    "Mexico": "MX",
    "Brazil": "BR",
    "Argentina": "AR",
    "Japan": "JP",
    "South Korea": "KR", "Republic of Korea": "KR",
    "China": "CN",
    "Singapore": "SG",
    "United Arab Emirates": "AE", "UAE": "AE",
    "South Africa": "ZA",
    "New Zealand": "NZ",
    "Israel": "IL",
    "Turkey": "TR",
    "Saudi Arabia": "SA",
    "Pakistan": "PK",
    "Bangladesh": "BD",
    "Indonesia": "ID",
    "Philippines": "PH",
    "Thailand": "TH",
    "Vietnam": "VN", "Viet Nam": "VN",
}


def _hint_region(location: str | None) -> str | None:
    """Try to extract an ISO2 country code from a free-form location string."""
    if not location:
        return None
    # search_location is built finest-grain-first: "place, city, state, country"
    parts = [p.strip() for p in location.split(",") if p.strip()]
    if not parts:
        return None
    # Try each part from coarsest (last) backward.
    for part in reversed(parts):
        region = COUNTRY_TO_REGION.get(part)
        if region:
            return region
    return None


def normalize_phone(phone: str | None, location_hint: str | None = None) -> str | None:
    """Return phone in E.164 format (e.g. +12056852020) when parseable, else original.

    Steps: strip Google's chrome (spaces, dashes, parens), parse with phonenumbers
    using a region inferred from `location_hint` when the number isn't already
    international.
    """
    if not phone:
        return phone
    raw = phone.strip()
    if not raw:
        return phone

    region = None if raw.startswith("+") else _hint_region(location_hint)
    try:
        parsed = phonenumbers.parse(raw, region)
    except phonenumbers.NumberParseException:
        return raw
    if not phonenumbers.is_valid_number(parsed):
        return raw
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


# --- Domain exclude ---------------------------------------------------------

def domain_of(url: str | None) -> str:
    """Return the lowercased hostname of a URL, sans 'www.'."""
    if not url:
        return ""
    try:
        host = urlparse(url if "://" in url else f"http://{url}").netloc.lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def parse_exclude_list(text: str) -> list[str]:
    """Turn a multiline user input into a normalised list of substrings to block."""
    items: list[str] = []
    for raw in (text or "").splitlines():
        s = raw.strip().lower()
        if not s or s.startswith("#"):
            continue
        # Allow either bare domains, full URLs, or `*.example.com`.
        s = s.lstrip("*.").lstrip("/").rstrip("/")
        if "://" in s:
            s = domain_of(s)
        if s:
            items.append(s)
    return items


def should_exclude(url: str, blocklist: list[str]) -> bool:
    if not url or not blocklist:
        return False
    host = domain_of(url)
    return any(b in host for b in blocklist)


def apply_domain_exclude(rows: list[dict], blocklist_text: str) -> tuple[list[dict], int]:
    """Drop rows whose website is on the blocklist. Returns (kept, dropped)."""
    blocklist = parse_exclude_list(blocklist_text)
    if not blocklist:
        return rows, 0
    kept = [r for r in rows if not should_exclude(r.get("website") or "", blocklist)]
    return kept, len(rows) - len(kept)
