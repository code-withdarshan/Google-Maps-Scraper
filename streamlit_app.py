"""Streamlit UI for the Google Maps scraper.

Two-phase pipeline (tuned for Streamlit Community Cloud free tier):
  Phase 1: Playwright scrapes Google Maps → base data shown immediately
           (browser closes, freeing memory)
  Phase 2: Parallel HTTP enrichment of each business's website (emails + socials)
           runs with 5 worker threads; rows update progressively.
"""
from __future__ import annotations

import json
import math
import re
import subprocess
import sys
from pathlib import Path

import pandas as pd
import streamlit as st


SOCIAL_FIELDS = (
    "facebook", "instagram", "linkedin", "twitter",
    "youtube", "tiktok", "pinterest", "whatsapp", "telegram",
)


def _has(row: dict, key: str) -> bool:
    v = row.get(key)
    if v is None or v == "":
        return False
    if isinstance(v, (list, dict)):
        return bool(v)
    return True


def _has_email(row: dict) -> bool:
    return _has(row, "emails")


def _has_website(row: dict) -> bool:
    return _has(row, "website")


def _has_phone(row: dict) -> bool:
    return _has(row, "phone")


def _social_count(row: dict) -> int:
    return sum(1 for f in SOCIAL_FIELDS if _has(row, f))


def _lead_score(row: dict) -> float:
    """Composite cold-outreach lead score.

    base  = rating × log(1 + reviews_count)
    bonus = 5·email + 2·website + 1·phone + 1·social_count
    """
    rating = row.get("rating") or 0
    reviews = row.get("reviews_count") or 0
    try:
        base = float(rating) * math.log(1 + float(reviews))
    except (TypeError, ValueError):
        base = 0.0
    bonus = (
        5 * (1 if _has_email(row) else 0)
        + 2 * (1 if _has_website(row) else 0)
        + 1 * (1 if _has_phone(row) else 0)
        + 1 * _social_count(row)
    )
    return round(base + bonus, 2)


SORT_KEYS = {
    "Lead score":      lambda r: _lead_score(r),
    "Completeness":    lambda r: _row_completeness(r),
    "Rating":          lambda r: float(r.get("rating") or 0),
    "Reviews count":   lambda r: float(r.get("reviews_count") or 0),
    "Has email":       lambda r: 1 if _has_email(r) else 0,
    "Has website":     lambda r: 1 if _has_website(r) else 0,
    "Has phone":       lambda r: 1 if _has_phone(r) else 0,
    "Social presence": lambda r: _social_count(r),
}


def filter_by_channels(
    rows: list[dict],
    channels: dict[str, bool] | None,
) -> tuple[list[dict], int]:
    """Keep only rows that have every channel the user explicitly selected.

    channels=None (legacy "scrape everything") => no filtering.
    Otherwise: a row must satisfy ALL active channel checks to be kept.
    """
    if not channels:
        return rows, 0
    active = [k for k, v in channels.items() if v]
    if not active:
        return rows, 0

    def passes(r: dict) -> bool:
        for ch in active:
            if ch == "email":
                if not _has_email(r):
                    return False
            else:
                # Any social field name (instagram, facebook, ...).
                if not _has(r, ch):
                    return False
        return True

    kept = [r for r in rows if passes(r)]
    return kept, len(rows) - len(kept)


def apply_sort(
    rows: list[dict],
    primary_key: str,
    primary_desc: bool,
    secondary_key: str,
    secondary_desc: bool,
) -> list[dict]:
    """Two-level stable sort. 'None' as secondary_key disables the tiebreaker."""
    def keyfn(r: dict):
        a = SORT_KEYS[primary_key](r)
        a_sortable = -a if primary_desc else a
        if secondary_key == "None" or secondary_key not in SORT_KEYS:
            return (a_sortable,)
        b = SORT_KEYS[secondary_key](r)
        b_sortable = -b if secondary_desc else b
        return (a_sortable, b_sortable)
    return sorted(rows, key=keyfn)


def _parse_search_terms(text: str) -> list[str]:
    """Split user input into search terms.

    Accepts any combination of newlines, commas, semicolons, and tabs.
    Trims whitespace, drops empties, dedupes while preserving order.
    """
    if not text:
        return []
    raw = re.split(r"[,\n;\t]+", text)
    seen: set[str] = set()
    out: list[str] = []
    for t in raw:
        t = t.strip()
        if not t:
            continue
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


def _slugify(text: str, fallback: str = "results") -> str:
    """Filesystem-safe slug from a free-text string."""
    if not text:
        return fallback
    t = text.lower()
    t = re.sub(r"[^a-z0-9]+", "_", t).strip("_")
    return t or fallback


def _normalize_website(url: str) -> str:
    """Canonical key for comparing two website URLs."""
    if not url:
        return ""
    u = url.strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    u = u.split("?", 1)[0].split("#", 1)[0]
    return u.rstrip("/")


def _row_completeness(row: dict) -> int:
    """Higher = more fields filled. Used to pick the best of duplicates."""
    score = 0
    for k, v in row.items():
        if v is None or v == "":
            continue
        if isinstance(v, (list, dict)) and not v:
            continue
        score += 1
    return score


def sort_by_completeness(rows: list[dict]) -> list[dict]:
    """Sort rows so the most-filled-in records come first, emptiest last."""
    return sorted(rows, key=_row_completeness, reverse=True)


def dedupe_by_website(rows: list[dict]) -> tuple[list[dict], int]:
    """Drop duplicate rows that share a website; keep the most-complete one.

    Rows without a website are always kept (they refer to different businesses).
    Returns (deduped_rows, number_removed).
    """
    kept: list[dict] = []
    seen_idx: dict[str, int] = {}
    removed = 0
    for row in rows:
        key = _normalize_website(row.get("website") or "")
        if not key:
            kept.append(row)
            continue
        if key in seen_idx:
            existing = kept[seen_idx[key]]
            if _row_completeness(row) > _row_completeness(existing):
                kept[seen_idx[key]] = row
            removed += 1
            continue
        seen_idx[key] = len(kept)
        kept.append(row)
    return kept, removed


# --- Ensure Playwright Chromium is installed (Streamlit Cloud free tier) -----
@st.cache_resource(show_spinner="Setting up browser (first run only)...")
def _ensure_browser() -> bool:
    marker = Path.home() / ".cache" / "ms-playwright" / ".installed"
    if marker.exists():
        return True
    try:
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=True,
        )
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        return True
    except Exception as e:
        st.error(f"Browser install failed: {e}")
        return False


_ensure_browser()

from scraper import scrape_google_maps, ScrapeControl  # noqa: E402
from enrich import enrich_places_batch  # noqa: E402
import cache as place_cache  # noqa: E402
from format import normalize_phone, apply_domain_exclude  # noqa: E402

import queue as _queue  # noqa: E402
import threading as _threading  # noqa: E402
import time as _time  # noqa: E402
from datetime import datetime  # noqa: E402
from locations import (  # noqa: E402
    list_countries,
    list_states,
    list_cities,
    expand_locations,
    diagnose,
    FALLBACK_STATES,
)


# --- Cached location lookups -------------------------------------------------
@st.cache_data(ttl=86400, show_spinner=False)
def _countries():
    return list_countries()


@st.cache_data(ttl=86400, show_spinner=False)
def _states(country: str):
    return list_states(country)


@st.cache_data(ttl=86400, show_spinner=False)
def _cities(country: str, state: str):
    return list_cities(country, state)


st.set_page_config(
    page_title="Google Maps Scraper",
    page_icon="📍",
    layout="wide",
    initial_sidebar_state="expanded",
    menu_items={
        "About": "Lead-gen scraper for Google Maps — local-first, free-tier-friendly.",
    },
)

# --- Material Symbols icon font + custom dark theme CSS ---------------------
st.html(
    """
    <link rel="stylesheet"
          href="https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:opsz,wght,FILL,GRAD@20..48,400..700,0..1,-25..200&display=block" />
    <style>
    /* Inline icon helper — use as <span class="mi">icon_name</span> */
    .mi {
        font-family: 'Material Symbols Outlined';
        font-weight: 400;
        font-style: normal;
        font-size: 1em;
        line-height: 1;
        letter-spacing: normal;
        text-transform: none;
        display: inline-block;
        white-space: nowrap;
        word-wrap: normal;
        direction: ltr;
        vertical-align: -3px;
        font-variation-settings: 'FILL' 0, 'wght' 500, 'GRAD' 0, 'opsz' 24;
    }
    .mi.filled { font-variation-settings: 'FILL' 1; }
    .mi.lg     { font-size: 1.4em; vertical-align: -5px; }

    /* Page padding */
    .block-container { padding-top: 1.5rem; padding-bottom: 2rem; }

    /* Hero banner — electric-blue gradient with glow */
    .hero {
        background: linear-gradient(135deg, #0B1220 0%, #1E3A8A 55%, #38BDF8 110%);
        color: #F8FAFC;
        padding: 1.6rem 2rem;
        border-radius: 16px;
        margin-bottom: 1.5rem;
        border: 1px solid rgba(56, 189, 248, 0.35);
        box-shadow:
            0 0 0 1px rgba(56, 189, 248, .15) inset,
            0 10px 40px -12px rgba(56, 189, 248, .55);
        position: relative;
        overflow: hidden;
    }
    .hero::after {
        content: "";
        position: absolute;
        top: -40%; right: -10%;
        width: 280px; height: 280px;
        background: radial-gradient(circle, rgba(56,189,248,.35) 0%, transparent 70%);
        pointer-events: none;
    }
    .hero h1 { color: #F0F9FF; margin: 0; font-size: 1.85rem; font-weight: 700;
               letter-spacing: -.01em; }
    .hero p  { color: rgba(224, 242, 254, .9); margin: .4rem 0 0;
               font-size: .95rem; position: relative; z-index: 1; }

    /* Step pills (Phase indicator) */
    .step-row { display: flex; gap: .5rem; margin: .5rem 0 1.2rem; flex-wrap: wrap; }
    .step {
        padding: .4rem .95rem;
        border-radius: 999px;
        font-size: .82rem;
        font-weight: 600;
        background: #1E293B;
        color: #64748B;
        border: 1px solid #334155;
    }
    .step.active {
        background: rgba(56, 189, 248, .15);
        color: #7DD3FC;
        border-color: #38BDF8;
        box-shadow: 0 0 12px -2px rgba(56, 189, 248, .5);
    }
    .step.done {
        background: rgba(56, 189, 248, .08);
        color: #38BDF8;
        border-color: #0EA5E9;
    }

    /* Empty-state example cards */
    .example-card {
        background: #111827;
        border: 1px solid #1F2937;
        border-radius: 12px;
        padding: 1rem 1.1rem;
        height: 100%;
        transition: border-color .15s, transform .15s;
    }
    .example-card:hover {
        border-color: #38BDF8;
        transform: translateY(-1px);
        box-shadow: 0 8px 24px -16px rgba(56, 189, 248, .55);
    }
    .example-card h4 { margin: 0 0 .3rem; font-size: .98rem; color: #F1F5F9; }
    .example-card p  { margin: 0; font-size: .85rem; color: #94A3B8; }
    .example-card code {
        background: rgba(56, 189, 248, .12);
        color: #7DD3FC;
        padding: .08rem .35rem;
        border-radius: 4px;
        font-size: .82rem;
    }

    /* Sidebar headings — small uppercase labels */
    section[data-testid="stSidebar"] h3 {
        font-size: .85rem !important;
        text-transform: uppercase;
        letter-spacing: .06em;
        color: #7DD3FC !important;
        margin-top: 1.1rem !important;
        font-weight: 700;
    }

    /* Status chips */
    .chip {
        display: inline-block;
        padding: .25rem .7rem;
        border-radius: 999px;
        font-size: .78rem;
        font-weight: 700;
        margin-right: .35rem;
        border: 1px solid;
    }
    .chip.running {
        background: rgba(56, 189, 248, .15);
        color: #7DD3FC;
        border-color: #38BDF8;
        box-shadow: 0 0 10px -2px rgba(56, 189, 248, .4);
    }
    .chip.paused  {
        background: rgba(148, 163, 184, .15);
        color: #CBD5E1;
        border-color: #475569;
    }

    /* Primary buttons — thunder-blue glow */
    .stButton button[kind="primary"] {
        background: linear-gradient(135deg, #0EA5E9 0%, #38BDF8 100%) !important;
        color: #0B1220 !important;
        border: none !important;
        font-weight: 700 !important;
        box-shadow: 0 4px 18px -6px rgba(56, 189, 248, .6);
    }
    .stButton button[kind="primary"]:hover {
        filter: brightness(1.08);
        box-shadow: 0 6px 22px -4px rgba(56, 189, 248, .8);
    }

    /* No reds anywhere — recolor error alerts to thunder-blue */
    div[data-testid="stAlertContainer"][data-baseweb="notification"]
        [data-baseweb="notification"][kind="error"],
    div[data-testid="stAlert"][data-baseweb="notification"][kind="error"],
    .stAlert[data-baseweb="notification"][role="alert"] {
        background: rgba(56, 189, 248, .10) !important;
        border-left-color: #38BDF8 !important;
    }
    /* Catch-all: tone red-ish text down to thunder-blue */
    div[data-testid="stAlert"] [data-testid="stMarkdownContainer"] { color: #E2E8F0; }

    /* Secondary (Stop) button — keep neutral, NOT red */
    .stButton button[kind="secondary"] {
        background: #1E293B !important;
        color: #E2E8F0 !important;
        border: 1px solid #334155 !important;
    }
    .stButton button[kind="secondary"]:hover {
        border-color: #38BDF8 !important;
        color: #7DD3FC !important;
    }

    /* Metric tiles */
    div[data-testid="stMetric"] {
        background: #111827;
        border: 1px solid #1F2937;
        border-radius: 12px;
        padding: .8rem 1rem;
    }
    div[data-testid="stMetricValue"] { color: #7DD3FC !important; font-weight: 700; }

    /* Dataframes — softer borders */
    div[data-testid="stDataFrame"] { border-radius: 10px; overflow: hidden; }
    </style>
    """
)

# --- Hero header ------------------------------------------------------------
st.html(
    """
    <div class="hero">
        <h1><span class="mi lg">location_on</span>&nbsp;Google Maps Lead Scraper</h1>
        <p>Find businesses on Maps → enrich with emails + social media →
           sort, dedupe, and download as CSV. Pause anytime, resume later.</p>
    </div>
    """
)


def _phase_badges() -> str:
    """Return HTML for the Phase 1/2/3 step indicator."""
    ss = st.session_state
    active = ss.get("scrape_active", False)
    done = ss.get("scrape_done", False)
    all_done = ss.get("phase2_3_done", False)
    p1 = "done" if done else ("active" if active else "")
    p2 = "done" if all_done else ("active" if done and not all_done else "")
    p3 = "done" if all_done else ""
    return (
        '<div class="step-row">'
        f'<span class="step {p1}">1 · Scrape</span>'
        f'<span class="step {p2}">2 · Enrich</span>'
        f'<span class="step {p3}">3 · Sort & Dedupe</span>'
        "</div>"
    )


st.html(_phase_badges())

# --- Sidebar inputs ----------------------------------------------------------
with st.sidebar:
    st.markdown("### :material/my_location: Search settings")

    search_input = st.text_area(
        "Search terms",
        value="restaurant",
        height=110,
        key="search_input_text",
        help="One per line, or comma-separated. E.g.  `gym, yoga studio, pilates`",
        placeholder="restaurant, cafe, bakery\nor one per line",
    )
    _parsed_terms_preview = _parse_search_terms(search_input)
    if _parsed_terms_preview:
        st.caption(
            f"**{len(_parsed_terms_preview)} term(s):** "
            + " · ".join(f"`{t}`" for t in _parsed_terms_preview[:6])
            + (" …" if len(_parsed_terms_preview) > 6 else "")
        )

    st.markdown("**Location**")
    loc_mode = st.radio(
        "Location mode",
        ["Dropdowns (Country → State → City)", "Free text"],
        index=0,
        label_visibility="collapsed",
        horizontal=False,
    )

    if loc_mode.startswith("Dropdowns"):
        if st.button("🔄 Refresh location data", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

        countries = _countries()
        if not countries:
            st.error("Couldn't load countries — API unreachable. Switch to Free text mode.")
        country = st.selectbox(
            "Country (type to search)",
            options=[""] + countries,
            index=0,
            help="Single-select. Start typing to filter.",
        )

        all_states = _states(country) if country else []
        fallback_used = bool(country and all_states and all_states == FALLBACK_STATES.get(country, []))
        states_selected: list[str] = st.multiselect(
            "States / Regions (multi-select, searchable)",
            options=all_states,
            default=[],
            disabled=not country,
            placeholder="Pick one or more states..." if country else "Select a country first",
        )
        if fallback_used:
            st.info(f"ℹ️ Using bundled state list for **{country}** (API was unreachable or empty).")
        if country and not all_states:
            st.warning(
                f"No states returned for **{country}** and no bundled fallback. "
                "Switch to Free text mode or expand the diagnostics below."
            )
            with st.expander("🔧 Diagnostics"):
                st.json(diagnose())

        # Build city → state map so multi-state selection keeps the mapping.
        cities_by_state: dict[str, list[str]] = {}
        labeled_city_options: list[str] = []
        label_to_pair: dict[str, tuple[str, str]] = {}
        for st_name in states_selected:
            city_list = _cities(country, st_name)
            cities_by_state[st_name] = []
            for c in city_list:
                # Disambiguate same-named cities across states.
                label = f"{c} ({st_name})" if len(states_selected) > 1 else c
                labeled_city_options.append(label)
                label_to_pair[label] = (st_name, c)

        cities_selected_labels: list[str] = st.multiselect(
            "Cities (multi-select, searchable)",
            options=labeled_city_options,
            default=[],
            disabled=not states_selected,
            placeholder=(
                "Pick one or more cities..." if states_selected
                else "Select state(s) first"
            ),
        )
        if states_selected and not labeled_city_options:
            st.warning(
                "No cities returned for the selected state(s). "
                "You can still proceed — Google Maps will use the state-level search, "
                "or add neighborhoods in the **Places / Areas** field below."
            )

        # Pop the chosen cities back into the cities_by_state map.
        cities_by_state = {s: [] for s in states_selected}
        for lbl in cities_selected_labels:
            st_name, c = label_to_pair[lbl]
            cities_by_state[st_name].append(c)

        # Multi-select with free-text entries for places.
        places: list[str] = st.multiselect(
            "Places / Areas (optional, multi-select, free text)",
            options=[],
            default=[],
            accept_new_options=True,
            placeholder="Type a neighborhood/landmark and press Enter, or leave blank.",
            help="Type a place (e.g. 'Connaught Place') and press Enter. Add as many as you want.",
        )

        expanded = expand_locations(country, states_selected, cities_by_state, places)
        location = expanded[0] if expanded else ""
        locations_list = expanded

        if expanded:
            with st.expander(f"🗺️ {len(expanded)} location queries built", expanded=False):
                for q in expanded:
                    st.write(f"• {q}")
        else:
            st.caption("🗺️ Pick at least a country to build a search location.")
    else:
        location = st.text_input("Location", value="New York, USA", key="location_freetext")
        locations_list = [location] if location.strip() else []

    unlimited = st.checkbox(
        ":material/all_inclusive: Unlimited (scrape until end of list)",
        value=False,
    )
    max_results_input = st.number_input(
        "Max places per search term",
        min_value=1,
        max_value=1000,
        value=15,
        step=5,
        disabled=unlimited,
    )
    max_results = None if unlimited else int(max_results_input)

    language = st.text_input("Language code", value="en", max_chars=5)

    st.markdown("### :material/map: Deep search")
    use_grid = st.checkbox(
        ":material/grid_view: Split location into a grid",
        value=False,
        help=(
            "Google Maps shows max ~120 places per single viewport. "
            "Enable this to geocode your location, split its area into N×N tiles, "
            "and scrape each tile separately. Results are deduped by Google's place ID. "
            "Needs a Location set above."
        ),
    )
    grid_size = st.slider(
        "Grid size (N × N tiles)",
        min_value=2,
        max_value=6,
        value=3,
        disabled=not use_grid,
        help="3×3 = 9 tiles (good city default). 5×5 = 25 tiles (much slower, much more data).",
    )
    effective_grid = grid_size if use_grid else 1
    headless = st.checkbox(
        "Headless browser",
        value=True,
        help="Required on Streamlit Cloud.",
    )

    st.markdown("### :material/cleaning_services: Phase 3 — Filter & dedupe")
    dedupe_websites = st.checkbox(
        ":material/cleaning_services: Remove duplicates by website",
        value=True,
        help=(
            "After scraping, drop rows that share the same website. "
            "When duplicates exist, the most-complete row is kept."
        ),
    )

    min_rating = st.slider(
        "Minimum rating",
        min_value=0.0, max_value=5.0, value=0.0, step=0.1,
        help="Drop businesses below this rating after Phase 3. 0.0 = no filter.",
    )
    min_reviews = st.number_input(
        "Minimum review count",
        min_value=0, value=0, step=5,
        help="Drop businesses with fewer reviews than this. 0 = no filter.",
    )

    domain_blocklist_text = st.text_area(
        "Domain exclude list",
        value="",
        height=80,
        placeholder="wix.com\ngodaddy.com\nlinktr.ee",
        help="One domain per line. Rows whose website host contains any of these are dropped.",
    )

    st.markdown("### :material/language: Phase 2 — Enrichment")
    enrich_websites = st.checkbox(
        ":material/language: Enrich with emails + social media",
        value=True,
    )
    st.caption(
        "Pick channels to narrow scraping. **Leave both unchecked to get "
        "every channel** (email + all social media)."
    )
    scrape_email = st.checkbox(
        ":material/mail: Email only",
        value=False,
        disabled=not enrich_websites,
        help="If checked alone, only emails are scraped.",
    )
    scrape_instagram = st.checkbox(
        ":material/photo_camera: Instagram only",
        value=False,
        disabled=not enrich_websites,
        help="If checked alone, only Instagram links are scraped.",
    )
    if enrich_websites and not (scrape_email or scrape_instagram):
        st.caption(":material/language: Scraping all channels (email + all socials).")
    enrich_workers = st.slider(
        "Parallel website fetches",
        min_value=1,
        max_value=10,
        value=5,
        disabled=not enrich_websites,
        help="More workers = faster, but more memory + bandwidth.",
    )
    enrich_pages = st.slider(
        "Pages per site (home + contact/about)",
        min_value=1,
        max_value=5,
        value=3,
        disabled=not enrich_websites,
    )

    # Build the channels dict. If the user ticks at least one box we honor it
    # as a filter; if both are unticked we fall back to legacy behavior (all
    # channels) by passing channels=None to the enricher.
    if scrape_email or scrape_instagram:
        enrich_channels = {
            "email": bool(scrape_email),
            "instagram": bool(scrape_instagram),
        }
    else:
        enrich_channels = None  # None = scrape everything

    st.markdown("### :material/bolt: Performance")
    use_cache = st.checkbox(
        ":material/save: Use persistent place cache",
        value=True,
        help=(
            "Re-runs of the same/overlapping query skip places already scraped "
            "(stored locally for 30 days, keyed by Google's place id)."
        ),
    )
    rows, nbytes = place_cache.size()
    st.caption(f"Cache: {rows} place(s), {nbytes / 1024:.1f} KB")
    if st.button("Clear cache", use_container_width=True):
        n = place_cache.clear()
        st.toast(f"Cleared {n} cached place(s).")

    st.markdown("### :material/bookmark: Search presets")
    presets = place_cache.list_presets()
    preset_names = [p["name"] for p in presets]
    if preset_names:
        chosen_preset = st.selectbox(
            "Load a saved preset",
            options=["—"] + preset_names,
            index=0,
            key="preset_select",
        )
        if chosen_preset != "—":
            chosen = next(p for p in presets if p["name"] == chosen_preset)
            colp1, colp2 = st.columns(2)
            if colp1.button(":material/play_arrow: Apply", use_container_width=True,
                            key=f"apply_{chosen['id']}"):
                p = chosen["params"] or {}
                if p.get("search_input") is not None:
                    st.session_state["search_input_text"] = p["search_input"]
                if p.get("location_freetext"):
                    st.session_state["location_freetext"] = p["location_freetext"]
                st.toast(
                    f"Loaded “{chosen['name']}”. Search terms and free-text "
                    "location applied; re-check other sidebar toggles."
                )
                st.rerun()
            if colp2.button(":material/delete: Delete", use_container_width=True,
                            key=f"del_{chosen['id']}"):
                place_cache.delete_preset(chosen["id"])
                st.rerun()
    else:
        st.caption("No presets saved yet.")

    new_preset_name = st.text_input(
        "Save current settings as preset",
        value="",
        placeholder="e.g. NYC restaurants — email only",
        key="new_preset_name",
    )
    if st.button(":material/save: Save preset", use_container_width=True,
                 disabled=not new_preset_name.strip()):
        current_params = {
            "search_input": search_input,
            "loc_mode": loc_mode,
            "country": country if loc_mode.startswith("Dropdowns") else None,
            "states_selected": states_selected if loc_mode.startswith("Dropdowns") else None,
            "cities_by_state": cities_by_state if loc_mode.startswith("Dropdowns") else None,
            "places": places if loc_mode.startswith("Dropdowns") else None,
            "location_freetext": location if not loc_mode.startswith("Dropdowns") else None,
            "unlimited": unlimited,
            "max_results_input": max_results_input,
            "language": language,
            "headless": headless,
            "use_grid": use_grid,
            "grid_size": grid_size,
            "dedupe_websites": dedupe_websites,
            "min_rating": min_rating,
            "min_reviews": min_reviews,
            "domain_blocklist_text": domain_blocklist_text,
            "enrich_websites": enrich_websites,
            "scrape_email": scrape_email,
            "scrape_instagram": scrape_instagram,
            "enrich_workers": enrich_workers,
            "enrich_pages": enrich_pages,
            "use_cache": use_cache,
        }
        place_cache.save_preset(new_preset_name.strip(), current_params)
        st.toast(f"Saved preset “{new_preset_name.strip()}”.")
        st.rerun()

    st.markdown("---")
    run = st.button(
        ":material/play_arrow: Start scraping",
        type="primary",
        use_container_width=True,
        disabled=bool(st.session_state.get("scrape_active", False)),
    )

# --- State -------------------------------------------------------------------
if "results" not in st.session_state:
    st.session_state.results = []
if "phase1_done" not in st.session_state:
    st.session_state.phase1_done = False

# --- Helpers -----------------------------------------------------------------
def _render_df(area, rows: list[dict]):
    df = pd.DataFrame(rows)
    area.dataframe(df, use_container_width=True, hide_index=True)


# --- Run ---------------------------------------------------------------------
# --- Background-thread state machine ---------------------------------------
def _init_session():
    ss = st.session_state
    ss.setdefault("scrape_active", False)
    ss.setdefault("scrape_done", False)
    ss.setdefault("phase2_3_done", False)
    ss.setdefault("control", None)
    ss.setdefault("scraper_thread", None)
    ss.setdefault("result_queue", None)
    ss.setdefault("phase1_status", "")
    ss.setdefault("phase1_results", [])
    ss.setdefault("scrape_stats", {})
    ss.setdefault("phase2_errors", 0)
    ss.setdefault("loaded_signature", None)


_init_session()


def _drain_queue() -> bool:
    """Drain pending messages from the scraper thread. Returns True if thread done."""
    q = st.session_state.result_queue
    if q is None:
        return False
    done = False
    while True:
        try:
            kind, payload = q.get_nowait()
        except _queue.Empty:
            break
        if kind == "status":
            st.session_state.phase1_status = payload
        elif kind == "place":
            st.session_state.phase1_results.append(payload)
        elif kind == "stats":
            st.session_state.scrape_stats = payload or {}
        elif kind == "done":
            done = True
    return done


def _start_scrape(terms, resume: bool):
    q: _queue.Queue = _queue.Queue(maxsize=256)
    control = ScrapeControl()
    st.session_state.control = control
    st.session_state.result_queue = q
    st.session_state.phase1_results = []
    st.session_state.phase1_status = ""
    st.session_state.scrape_active = True
    st.session_state.scrape_done = False
    st.session_state.phase2_3_done = False
    st.session_state.results = []

    def _status_cb(msg: str):
        q.put(("status", msg))

    def worker():
        try:
            for place in scrape_google_maps(
                search_terms=terms,
                location=location,
                locations=locations_list,
                max_results=max_results,
                language=language,
                headless=headless,
                grid_size=effective_grid,
                use_cache=use_cache,
                parallelism=3,
                control=control,
                resume=resume,
                status_cb=_status_cb,
            ):
                q.put(("place", place))
        except Exception as e:
            q.put(("status", f"Phase 1 crashed: {e}"))
        finally:
            q.put(("done", None))

    t = _threading.Thread(target=worker, daemon=True)
    t.start()
    st.session_state.scraper_thread = t


# --- Run button trigger ------------------------------------------------------
if run and not st.session_state.scrape_active:
    terms = _parse_search_terms(search_input)
    if not terms:
        st.error("Please enter at least one search term.")
        st.stop()
    _start_scrape(terms, resume=False)
    st.rerun()


# --- Resume previous scrape (banner if an unfinished run is in the DB) ------
if not st.session_state.scrape_active and not st.session_state.scrape_done:
    unfinished = place_cache.list_unfinished_runs()
    if unfinished:
        with st.expander(f":material/restart_alt: Resume a previous scrape ({len(unfinished)} unfinished)", expanded=True):
            for u in unfinished[:5]:
                p = u["params"]
                terms_str = ", ".join((p.get("search_terms") or [])[:3])
                locs_str = ", ".join((p.get("locations") or [])[:2])
                col1, col2, col3 = st.columns([4, 2, 2])
                col1.write(
                    f"**{terms_str}** in *{locs_str}* — "
                    f"{u['done_count']} places scraped (status: {u['status']})"
                )
                if col2.button("Resume", key=f"resume_{u['signature']}"):
                    _start_scrape(p.get("search_terms") or [], resume=True)
                    st.rerun()
                if col3.button("Discard", key=f"discard_{u['signature']}"):
                    place_cache.delete_run(u["signature"])
                    st.rerun()

    # --- Run history (completed runs) -----------------------------------
    history = place_cache.list_done_runs(limit=10)
    if history:
        with st.expander(
            f":material/history: Past runs ({len(history)})", expanded=False
        ):
            for h in history:
                p = h["params"]
                terms_str = ", ".join((p.get("search_terms") or [])[:3])
                locs_str = ", ".join((p.get("locations") or [])[:2])
                col1, col2, col3 = st.columns([5, 2, 2])
                ts = datetime.fromtimestamp(h["updated_at"]).strftime("%Y-%m-%d %H:%M")
                col1.write(
                    f"**{terms_str}** in *{locs_str}*  \n"
                    f"<small>{h['done_count']} places · {ts} · {h['status']}</small>",
                    unsafe_allow_html=True,
                )
                if h["has_results"]:
                    if col2.button(":material/visibility: Load",
                                   key=f"load_{h['signature']}",
                                   use_container_width=True):
                        loaded = place_cache.get_run_results(h["signature"])
                        if loaded:
                            st.session_state.results = loaded
                            st.session_state.phase1_results = loaded
                            st.session_state.scrape_done = True
                            st.session_state.phase2_3_done = True
                            st.session_state.scrape_stats = h.get("stats") or {}
                            st.session_state.loaded_signature = h["signature"]
                            st.rerun()
                else:
                    col2.caption("no results saved")
                if col3.button(":material/delete: Delete",
                               key=f"hdel_{h['signature']}",
                               use_container_width=True):
                    place_cache.delete_run(h["signature"])
                    st.rerun()


# --- Active scraping view: pause / resume / stop + live updates -------------
if st.session_state.scrape_active:
    ctrl: ScrapeControl = st.session_state.control
    is_paused = ctrl.pause_event.is_set()
    chip_html = (
        '<span class="chip paused"><span class="mi">pause</span>&nbsp;Paused</span>'
        if is_paused
        else '<span class="chip running"><span class="mi filled">fiber_manual_record</span>&nbsp;Running</span>'
    )
    st.markdown(
        f'### <span class="mi">search</span>&nbsp;Phase 1 — Scraping Google Maps &nbsp; {chip_html}',
        unsafe_allow_html=True,
    )

    bc1, bc2, bc3 = st.columns([1, 1, 1.6])
    if is_paused:
        if bc1.button(":material/play_arrow: Resume", use_container_width=True, type="primary"):
            ctrl.pause_event.clear()
            st.rerun()
    else:
        if bc1.button(":material/pause: Pause", use_container_width=True):
            ctrl.pause_event.set()
            st.rerun()
    if bc2.button(":material/stop: Stop", use_container_width=True, type="secondary"):
        ctrl.stop_event.set()
        ctrl.pause_event.clear()
        st.rerun()
    bc3.metric("Places found so far", len(st.session_state.phase1_results))

    done = _drain_queue()
    if st.session_state.phase1_status:
        st.info(st.session_state.phase1_status, icon=":material/construction:")
    if st.session_state.phase1_results:
        st.dataframe(
            pd.DataFrame(st.session_state.phase1_results[-25:]),
            use_container_width=True,
            hide_index=True,
        )

    t: _threading.Thread = st.session_state.scraper_thread
    if done or (t is not None and not t.is_alive()):
        _drain_queue()  # final drain
        st.session_state.scrape_active = False
        st.session_state.scrape_done = True
        st.rerun()
    else:
        # Idle a beat, then rerun so buttons stay responsive.
        _time.sleep(1.0)
        st.rerun()


# --- Phase 2 + 3 run once after Phase 1 finishes ----------------------------
if st.session_state.scrape_done and not st.session_state.phase2_3_done:
    results = list(st.session_state.phase1_results)

    # Normalize phone numbers to E.164 where possible (per row).
    for r in results:
        r["phone"] = normalize_phone(r.get("phone"), r.get("search_location"))

    st.session_state.results = results
    st.success(f"Phase 1 done — {len(results)} places scraped.", icon=":material/check_circle:")

    # --- Phase 1 stats panel ----------------------------------------------
    s = st.session_state.scrape_stats or {}
    if s:
        sc1, sc2, sc3, sc4, sc5 = st.columns(5)
        sc1.metric("Closed (skipped)", s.get("closed_skipped", 0), border=True)
        sc2.metric("Extract fails", s.get("extract_failed", 0), border=True)
        sc3.metric("Tile timeouts", s.get("tiles_timed_out", 0), border=True)
        sc4.metric("Cache hits", s.get("cache_hits", 0), border=True)
        sc5.metric("CAPTCHAs hit", s.get("captchas_hit", 0), border=True)

    # ---- Phase 2 -----------------------------------------------------------
    if enrich_websites and results:
        with_site = sum(1 for r in results if r.get("website"))
        if enrich_channels is None:
            channels_label = "All (email + all socials)"
        else:
            channels_label = ", ".join(
                name.capitalize() for name, on in enrich_channels.items() if on
            )
        st.markdown("### :material/language: Phase 2 — Enriching websites")
        st.caption(
            f"{with_site} of {len(results)} places have a website to enrich. "
            f"Channels: **{channels_label}**."
        )

        phase2_status = st.empty()
        phase2_bar = st.progress(0.0)
        phase2_table = st.empty()

        def progress(done: int, total: int):
            pct = done / total if total else 1.0
            phase2_bar.progress(pct)
            phase2_status.info(f"Enriching {done}/{total}", icon=":material/language:")

        try:
            for idx, _ in enrich_places_batch(
                results,
                max_workers=enrich_workers,
                max_pages_per_site=enrich_pages,
                channels=enrich_channels,
                progress_cb=progress,
            ):
                # Update live table every few rows to avoid render churn.
                if idx % 5 == 0:
                    phase2_table.dataframe(
                        pd.DataFrame(results),
                        use_container_width=True,
                        hide_index=True,
                    )
            phase2_bar.progress(1.0)
            phase2_status.success("Phase 2 done.", icon=":material/check_circle:")
        except Exception as e:
            phase2_status.error(f"Phase 2 failed: {e}", icon=":material/error:")

        # Count enrichment failures for the final stats panel.
        st.session_state.phase2_errors = sum(
            1 for r in results if r.get("enrichment_error")
        )
        st.session_state.results = results

    # ---- Phase 3 -----------------------------------------------------------
    if results:
        st.markdown("### :material/cleaning_services: Phase 3 — Sort + dedupe")

        # Step 1: sort by how many fields are filled (most → least).
        before_sort = len(results)
        results = sort_by_completeness(results)
        st.info(
            f"Sorted {before_sort} rows by completeness "
            "(most-filled at the top, emptiest at the bottom).",
            icon=":material/insights:",
        )

        # Step 2: dedupe by website, if enabled.
        if dedupe_websites:
            before = len(results)
            results, removed = dedupe_by_website(results)
            if removed > 0:
                st.success(
                    f"Removed {removed} duplicate row(s) sharing a website "
                    f"— {before} → {len(results)}.",
                    icon=":material/cleaning_services:",
                )
            else:
                st.info("No website duplicates found.", icon=":material/done_all:")

        # Step 3: channel filter. If the user picked specific channels
        # (e.g. Instagram), drop rows that don't have that channel filled.
        before_filter = len(results)
        results, filtered_out = filter_by_channels(results, enrich_channels)
        if filtered_out > 0:
            active_names = ", ".join(
                k.capitalize() for k, v in (enrich_channels or {}).items() if v
            )
            st.success(
                f"Channel filter ({active_names}): kept {len(results)} of "
                f"{before_filter} rows (dropped {filtered_out} without the "
                f"selected channel).",
                icon=":material/filter_alt:",
            )

        # Step 4: rating / reviews quality gates.
        if min_rating > 0:
            before = len(results)
            results = [r for r in results if (r.get("rating") or 0) >= min_rating]
            if before - len(results):
                st.success(
                    f"Rating filter (≥ {min_rating}): kept {len(results)} of {before}.",
                    icon=":material/star:",
                )
        if min_reviews > 0:
            before = len(results)
            results = [r for r in results if (r.get("reviews_count") or 0) >= min_reviews]
            if before - len(results):
                st.success(
                    f"Reviews filter (≥ {min_reviews}): kept {len(results)} of {before}.",
                    icon=":material/reviews:",
                )

        # Step 5: domain exclude list.
        before = len(results)
        results, dropped = apply_domain_exclude(results, domain_blocklist_text)
        if dropped:
            st.success(
                f"Domain exclude: dropped {dropped} row(s) — {before} → {len(results)}.",
                icon=":material/block:",
            )

        st.session_state.results = results

    # Final stats including Phase-2 enrichment errors.
    total_stats = dict(st.session_state.scrape_stats or {})
    total_stats["enrichment_errors"] = st.session_state.phase2_errors
    total_stats["final_rows"] = len(results)

    if total_stats:
        with st.expander(":material/analytics: Run stats — what happened", expanded=False):
            cols = st.columns(4)
            keys = [
                ("places_yielded", "Places yielded"),
                ("closed_skipped", "Permanently closed"),
                ("extract_failed", "Extract failures"),
                ("tiles_timed_out", "Tile timeouts"),
                ("cache_hits", "Cache hits"),
                ("prededuped", "Pre-deduped URLs"),
                ("captchas_hit", "CAPTCHAs"),
                ("enrichment_errors", "Enrichment fails"),
                ("final_rows", "Final rows"),
            ]
            for i, (k, label) in enumerate(keys):
                cols[i % 4].metric(label, total_stats.get(k, 0), border=True)

    # Persist for run history.
    ctrl = st.session_state.control
    if ctrl and ctrl.signature:
        place_cache.save_run_results(ctrl.signature, results, total_stats)
        place_cache.set_run_status(ctrl.signature, "done")

    # Mark the run as fully complete so a sort-dropdown change doesn't re-run
    # Phase 2/3 from scratch.
    st.session_state.phase2_3_done = True


# --- Final results & downloads ----------------------------------------------
results = st.session_state.results

if results:
    # --- Attach lead_score to every row so it appears in CSV/JSON downloads ---
    for r in results:
        r["lead_score"] = _lead_score(r)

    # --- Top metrics row -----------------------------------------------------
    n_total    = len(results)
    n_with_web = sum(1 for r in results if _has_website(r))
    n_with_em  = sum(1 for r in results if _has_email(r))
    n_with_ig  = sum(1 for r in results if _has(r, "instagram"))
    avg_score  = round(sum(r.get("lead_score", 0) for r in results) / n_total, 1) if n_total else 0

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Places", n_total, border=True)
    m2.metric("With website", n_with_web, f"{n_with_web * 100 // max(1, n_total)}%", border=True)
    m3.metric("With email", n_with_em, f"{n_with_em * 100 // max(1, n_total)}%", border=True)
    m4.metric("With Instagram", n_with_ig, f"{n_with_ig * 100 // max(1, n_total)}%", border=True)
    m5.metric("Avg lead score", avg_score, border=True)

    # --- Sort controls -------------------------------------------------------
    st.markdown("### :material/swap_vert: Sort results")
    sort_options = list(SORT_KEYS.keys())
    secondary_options = ["None"] + sort_options

    sc1, sc2, sc3, sc4 = st.columns([2, 1, 2, 1])
    with sc1:
        primary_key = st.selectbox(
            "Primary sort",
            options=sort_options,
            index=sort_options.index("Lead score"),
        )
    with sc2:
        primary_dir = st.selectbox(
            "Direction", options=["Desc", "Asc"], index=0, key="primary_dir",
        )
    with sc3:
        secondary_key = st.selectbox(
            "Secondary sort (tiebreaker)",
            options=secondary_options,
            index=0,
        )
    with sc4:
        secondary_dir = st.selectbox(
            "Direction", options=["Desc", "Asc"], index=0,
            key="secondary_dir",
            disabled=(secondary_key == "None"),
        )

    results = apply_sort(
        results,
        primary_key, primary_dir == "Desc",
        secondary_key, secondary_dir == "Desc",
    )
    st.session_state.results = results

    st.markdown("### :material/table_view: Final results")
    df = pd.DataFrame(results)

    # Put lead_score next to reviews_count for visibility.
    preferred_order = [
        "title", "category", "address", "website", "phone",
        "rating", "reviews_count", "lead_score",
        "url", "search_term", "search_location",
        "emails", *SOCIAL_FIELDS,
    ]
    ordered_cols = [c for c in preferred_order if c in df.columns]
    leftover = [c for c in df.columns if c not in ordered_cols]
    df = df[ordered_cols + leftover]

    st.dataframe(df, use_container_width=True, hide_index=True)

    # Flatten list/dict columns for clean CSV export.
    csv_df = df.copy()
    for col in csv_df.columns:
        if csv_df[col].apply(lambda v: isinstance(v, list)).any():
            csv_df[col] = csv_df[col].apply(
                lambda v: " | ".join(map(str, v)) if isinstance(v, list) else v
            )
        elif csv_df[col].apply(lambda v: isinstance(v, dict)).any():
            csv_df[col] = csv_df[col].apply(
                lambda v: json.dumps(v, ensure_ascii=False) if isinstance(v, dict) else v
            )

    # Filename: <search_term>_<location>.csv (with overflow suffixes if multiple).
    terms_in_results = (
        df["search_term"].dropna().unique().tolist()
        if "search_term" in df.columns else []
    )
    locations_in_results = (
        df["search_location"].dropna().unique().tolist()
        if "search_location" in df.columns else []
    )

    term_slug = _slugify(str(terms_in_results[0])) if terms_in_results else "results"
    if len(terms_in_results) > 1:
        term_slug += f"_and_{len(terms_in_results) - 1}_more_terms"

    if locations_in_results:
        loc_slug = _slugify(str(locations_in_results[0]))
        if len(locations_in_results) > 1:
            loc_slug += f"_and_{len(locations_in_results) - 1}_more_locs"
        filename_base = f"{term_slug}_{loc_slug}"
    else:
        filename_base = term_slug

    c1, c2 = st.columns(2)
    with c1:
        st.download_button(
            ":material/download: Download CSV",
            data=csv_df.to_csv(index=False).encode("utf-8"),
            file_name=f"{filename_base}.csv",
            mime="text/csv",
            use_container_width=True,
        )
    with c2:
        st.download_button(
            ":material/download: Download JSON",
            data=json.dumps(results, indent=2, ensure_ascii=False).encode("utf-8"),
            file_name=f"{filename_base}.json",
            mime="application/json",
            use_container_width=True,
        )
elif not st.session_state.scrape_active:
    # --- Empty state: friendly welcome + example queries -------------------
    st.markdown("### :material/waving_hand: Welcome — let's find some leads")
    st.write(
        "Fill in **Search terms** + **Location** in the sidebar, then click "
        "**Start scraping**. The defaults are tuned for Streamlit free tier "
        "and your laptop."
    )

    ec1, ec2, ec3 = st.columns(3)
    with ec1:
        st.html(
            """
            <div class="example-card">
              <h4><span class="mi">restaurant</span>&nbsp;Local restaurants</h4>
              <p>Search: <code>restaurant, cafe, bakery</code><br/>
              Location: <code>Manhattan, New York</code></p>
            </div>
            """
        )
    with ec2:
        st.html(
            """
            <div class="example-card">
              <h4><span class="mi">fitness_center</span>&nbsp;Fitness leads</h4>
              <p>Search: <code>gym, yoga studio, personal trainer</code><br/>
              Location: <code>Austin, Texas</code></p>
            </div>
            """
        )
    with ec3:
        st.html(
            """
            <div class="example-card">
              <h4><span class="mi">dentistry</span>&nbsp;Dental clinics</h4>
              <p>Search: <code>dentist, dental clinic</code><br/>
              Location: <code>Bengaluru, Karnataka, India</code></p>
            </div>
            """
        )

    st.markdown("---")
    with st.expander(":material/menu_book: Quick guide", expanded=False):
        st.markdown(
            """
            **3 phases run automatically:**
            1. **Scrape Google Maps** for matching businesses (parallel, 3 tabs)
            2. **Enrich** each website for emails + social media links
            3. **Sort + dedupe** — most-complete rows first, no duplicates

            **Tips for more results:**
            - Use **Deep search** in the sidebar — splits your location
              into a grid and bypasses Google's 120-place cap.
            - Pick **multiple search terms** (one per line) — they're scraped together.
            - Need just emails? Tick **Email only** under Phase 2 — speeds it up.

            **Anything goes wrong?** Hit **Pause**, **Stop**, or close
            the tab. Your progress is saved — you'll see a **Resume**
            banner next time.
            """
        )
