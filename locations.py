"""Country → State → City lookups.

Primary source: countriesnow.space (free, no key).
- GET /countries/positions  → countries list
- GET /countries/states     → ALL countries with their states (one request)
- POST /countries/state/cities → cities for (country, state)

Bundled fallback for the most-common countries when the API is down or
returns nothing (this happens a few times a week with countriesnow).
"""
from __future__ import annotations

import httpx


BASE = "https://countriesnow.space/api/v0.1"
TIMEOUT = 20.0
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (compatible; GoogleMapsScraperApp/1.0)",
}


# --- Bundled fallbacks ------------------------------------------------------

US_STATES = [
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
    "Connecticut", "Delaware", "Florida", "Georgia", "Hawaii", "Idaho",
    "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana",
    "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota",
    "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada",
    "New Hampshire", "New Jersey", "New Mexico", "New York",
    "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon",
    "Pennsylvania", "Rhode Island", "South Carolina", "South Dakota",
    "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington",
    "West Virginia", "Wisconsin", "Wyoming", "District of Columbia",
]

IN_STATES = [
    "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh",
    "Delhi", "Goa", "Gujarat", "Haryana", "Himachal Pradesh", "Jharkhand",
    "Karnataka", "Kerala", "Madhya Pradesh", "Maharashtra", "Manipur",
    "Meghalaya", "Mizoram", "Nagaland", "Odisha", "Punjab", "Rajasthan",
    "Sikkim", "Tamil Nadu", "Telangana", "Tripura", "Uttar Pradesh",
    "Uttarakhand", "West Bengal", "Andaman and Nicobar Islands",
    "Chandigarh", "Dadra and Nagar Haveli and Daman and Diu",
    "Jammu and Kashmir", "Ladakh", "Lakshadweep", "Puducherry",
]

UK_REGIONS = [
    "England", "Scotland", "Wales", "Northern Ireland",
    "London", "Greater Manchester", "West Midlands", "West Yorkshire",
    "Merseyside", "South Yorkshire", "Tyne and Wear",
]

CA_PROVINCES = [
    "Alberta", "British Columbia", "Manitoba", "New Brunswick",
    "Newfoundland and Labrador", "Northwest Territories", "Nova Scotia",
    "Nunavut", "Ontario", "Prince Edward Island", "Quebec",
    "Saskatchewan", "Yukon",
]

AU_STATES = [
    "Australian Capital Territory", "New South Wales", "Northern Territory",
    "Queensland", "South Australia", "Tasmania", "Victoria", "Western Australia",
]

DE_STATES = [
    "Baden-Württemberg", "Bavaria", "Berlin", "Brandenburg", "Bremen",
    "Hamburg", "Hesse", "Lower Saxony", "Mecklenburg-Vorpommern",
    "North Rhine-Westphalia", "Rhineland-Palatinate", "Saarland",
    "Saxony", "Saxony-Anhalt", "Schleswig-Holstein", "Thuringia",
]

FALLBACK_STATES: dict[str, list[str]] = {
    "United States": US_STATES,
    "United States of America": US_STATES,
    "USA": US_STATES,
    "India": IN_STATES,
    "United Kingdom": UK_REGIONS,
    "Canada": CA_PROVINCES,
    "Australia": AU_STATES,
    "Germany": DE_STATES,
}


# --- API name aliases -------------------------------------------------------

COUNTRY_ALIASES = {
    "United States": ["United States", "United States of America", "USA"],
    "United Kingdom": ["United Kingdom", "United Kingdom of Great Britain and Northern Ireland", "Britain", "Great Britain"],
    "Russia": ["Russia", "Russian Federation"],
    "South Korea": ["South Korea", "Korea, Republic of", "Republic of Korea"],
    "North Korea": ["North Korea", "Korea, Democratic People's Republic of"],
    "Iran": ["Iran", "Iran (Islamic Republic of)"],
    "Vietnam": ["Vietnam", "Viet Nam"],
    "Taiwan": ["Taiwan", "Taiwan, Province of China"],
    "Czech Republic": ["Czech Republic", "Czechia"],
    "Ivory Coast": ["Ivory Coast", "Côte d'Ivoire", "Cote d'Ivoire"],
}


# --- HTTP helpers -----------------------------------------------------------

def _client():
    return httpx.Client(timeout=TIMEOUT, headers=HEADERS, follow_redirects=True)


def _get(path: str) -> dict:
    with _client() as c:
        r = c.get(f"{BASE}{path}")
        r.raise_for_status()
        return r.json()


def _post(path: str, payload: dict | None = None) -> dict:
    with _client() as c:
        r = c.post(f"{BASE}{path}", json=payload or {})
        r.raise_for_status()
        return r.json()


# --- All-states cache (single GET, then filter locally) ---------------------

_ALL_STATES_CACHE: dict[str, list[str]] | None = None


def _fetch_all_states() -> dict[str, list[str]]:
    """Single GET → {country_name: [state_name, ...]}. Cached for the process."""
    global _ALL_STATES_CACHE
    if _ALL_STATES_CACHE is not None:
        return _ALL_STATES_CACHE
    try:
        data = _get("/countries/states")
        out: dict[str, list[str]] = {}
        for item in data.get("data") or []:
            name = item.get("name") or item.get("country")
            states = item.get("states") or []
            if not name:
                continue
            out[name] = sorted({s.get("name") for s in states if s.get("name")})
        _ALL_STATES_CACHE = out
        return out
    except Exception:
        _ALL_STATES_CACHE = {}
        return {}


# --- Public API -------------------------------------------------------------

def list_countries() -> list[str]:
    try:
        data = _get("/countries/positions")
        items = data.get("data") or []
        names = [item["name"] for item in items if item.get("name")]
        if names:
            return sorted(set(names))
    except Exception:
        pass
    # Fallback to whatever the states list contains.
    all_states = _fetch_all_states()
    if all_states:
        return sorted(all_states.keys())
    return sorted(FALLBACK_STATES.keys())


def list_states(country: str) -> list[str]:
    """States for a country, with API + alias + bundled fallback chain."""
    if not country:
        return []

    all_states = _fetch_all_states()
    if all_states:
        for variant in COUNTRY_ALIASES.get(country, [country]):
            if variant in all_states and all_states[variant]:
                return all_states[variant]

    # Try POST per-country.
    for variant in COUNTRY_ALIASES.get(country, [country]):
        try:
            data = _post("/countries/states", {"country": variant})
            if data.get("error"):
                continue
            states = (data.get("data") or {}).get("states") or []
            names = sorted({s["name"] for s in states if s.get("name")})
            if names:
                return names
        except Exception:
            continue

    # Bundled hardcoded fallback.
    return FALLBACK_STATES.get(country, [])


def list_cities(country: str, state: str) -> list[str]:
    if not country or not state:
        return []
    last_err = None
    for variant in COUNTRY_ALIASES.get(country, [country]):
        try:
            data = _post(
                "/countries/state/cities",
                {"country": variant, "state": state},
            )
            if data.get("error"):
                continue
            cities = data.get("data") or []
            if isinstance(cities, list) and cities:
                return sorted({c for c in cities if c})
        except Exception as e:
            last_err = e
            continue
    return []


def build_location_string(country: str, state: str, city: str, place: str) -> str:
    parts = [p.strip() for p in (place, city, state, country) if p and p.strip()]
    return ", ".join(parts)


def expand_locations(
    country: str,
    states: list[str],
    cities_by_state: dict[str, list[str]],
    places: list[str],
) -> list[str]:
    """Cross-product the selections into individual location query strings."""
    locations: list[str] = []
    place_list = places if places else [""]

    if not states:
        for place in place_list:
            s = build_location_string(country, "", "", place)
            if s:
                locations.append(s)
        return locations

    for state in states:
        cities = cities_by_state.get(state, [])
        if not cities:
            for place in place_list:
                s = build_location_string(country, state, "", place)
                if s:
                    locations.append(s)
        else:
            for city in cities:
                for place in place_list:
                    s = build_location_string(country, state, city, place)
                    if s:
                        locations.append(s)

    seen = set()
    out = []
    for loc in locations:
        if loc not in seen:
            seen.add(loc)
            out.append(loc)
    return out


def diagnose() -> dict:
    """Return a small dict the UI can show to debug API health."""
    info = {
        "positions_ok": False,
        "all_states_ok": False,
        "countries_found": 0,
        "states_countries": 0,
        "error": None,
    }
    try:
        d = _get("/countries/positions")
        info["countries_found"] = len(d.get("data") or [])
        info["positions_ok"] = info["countries_found"] > 0
    except Exception as e:
        info["error"] = f"positions: {e}"
    try:
        d = _get("/countries/states")
        info["states_countries"] = len(d.get("data") or [])
        info["all_states_ok"] = info["states_countries"] > 0
    except Exception as e:
        info["error"] = (info["error"] or "") + f" | states: {e}"
    return info
