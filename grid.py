"""Geocode a location and build a grid of Google Maps search URLs.

Used by the scraper's 'Deep search' mode to bypass Google's ~120-results-per-view
cap by hitting many smaller viewports and deduping.

Uses Nominatim (OpenStreetMap) for geocoding — free, no API key, but rate-limited
to 1 request/sec, which is fine for one geocode per run.
"""
from __future__ import annotations

import math
from urllib.parse import quote_plus

import httpx


NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "GoogleMapsScraper/0.1 (https://github.com/local; contact via app)"


def geocode(location: str) -> dict | None:
    """Return {'lat', 'lng', 'bbox': (south, north, west, east)} or None."""
    if not location.strip():
        return None
    try:
        r = httpx.get(
            NOMINATIM_URL,
            params={"q": location, "format": "json", "limit": 1},
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en"},
            timeout=15.0,
        )
        r.raise_for_status()
        data = r.json()
        if not data:
            return None
        item = data[0]
        lat = float(item["lat"])
        lng = float(item["lon"])
        # boundingbox = [south, north, west, east]
        bb = item.get("boundingbox")
        if bb and len(bb) == 4:
            south, north, west, east = (float(x) for x in bb)
        else:
            # Fall back to a ~10km square around the point.
            south, north = lat - 0.05, lat + 0.05
            west, east = lng - 0.05, lng + 0.05
        return {"lat": lat, "lng": lng, "bbox": (south, north, west, east)}
    except Exception:
        return None


def _zoom_for_span(lat_span_deg: float, lng_span_deg: float, lat: float) -> int:
    """Approximate Google Maps zoom level for a viewport of given degree span."""
    # Pick the larger axis (in km roughly) and map to Google's zoom scale.
    lat_km = lat_span_deg * 111.0
    lng_km = lng_span_deg * 111.0 * math.cos(math.radians(lat))
    span_km = max(lat_km, lng_km)
    if span_km <= 0:
        return 14
    # Empirical: zoom z roughly halves the span each step. World ≈ 40_000km at z=1.
    # 13z ≈ 5km, 14z ≈ 2.5km, 15z ≈ 1.2km, 12z ≈ 10km, 11z ≈ 20km.
    if span_km > 30:   return 10
    if span_km > 15:   return 11
    if span_km > 7:    return 12
    if span_km > 3:    return 13
    if span_km > 1.5:  return 14
    return 15


def tile_urls(
    search_term: str,
    bbox: tuple[float, float, float, float],
    grid_size: int,
    language: str = "en",
) -> list[str]:
    """Split bbox into grid_size x grid_size cells and return a maps URL per cell."""
    south, north, west, east = bbox
    if grid_size < 1:
        grid_size = 1

    lat_step = (north - south) / grid_size
    lng_step = (east - west) / grid_size
    zoom = _zoom_for_span(lat_step, lng_step, (south + north) / 2)

    urls: list[str] = []
    q = quote_plus(search_term)
    for i in range(grid_size):
        for j in range(grid_size):
            cell_lat = south + lat_step * (i + 0.5)
            cell_lng = west + lng_step * (j + 0.5)
            url = (
                f"https://www.google.com/maps/search/{q}/"
                f"@{cell_lat:.6f},{cell_lng:.6f},{zoom}z?hl={language}"
            )
            urls.append(url)
    return urls
