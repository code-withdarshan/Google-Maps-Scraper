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

from scraper import scrape_google_maps  # noqa: E402
from enrich import enrich_places_batch  # noqa: E402
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


st.set_page_config(page_title="Google Maps Scraper", page_icon="📍", layout="wide")

st.title("📍 Google Maps Scraper")
st.caption(
    "Phase 1: scrape Google Maps (fast). "
    "Phase 2: enrich each website for emails + social media (parallel)."
)

# --- Sidebar inputs ----------------------------------------------------------
with st.sidebar:
    st.header("Search settings")

    search_input = st.text_area(
        "Search terms (one per line)",
        value="restaurant",
        height=110,
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
        location = st.text_input("Location", value="New York, USA")
        locations_list = [location] if location.strip() else []

    unlimited = st.checkbox(
        "♾️ Unlimited (scrape until end of list)",
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

    st.markdown("---")
    st.subheader("Deep search (bypass 120-place cap)")
    use_grid = st.checkbox(
        "🗺️ Split location into a grid",
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

    st.markdown("---")
    st.subheader("Phase 3 — Dedupe")
    dedupe_websites = st.checkbox(
        "🧹 Remove duplicates by website",
        value=True,
        help=(
            "After scraping, drop rows that share the same website. "
            "When duplicates exist, the most-complete row is kept."
        ),
    )

    st.markdown("---")
    st.subheader("Phase 2 — Website enrichment")
    enrich_websites = st.checkbox(
        "🌐 Enrich with emails + social media",
        value=True,
    )
    st.caption(
        "Pick channels to narrow scraping. **Leave both unchecked to get "
        "every channel** (email + all social media)."
    )
    scrape_email = st.checkbox(
        "📧 Email only",
        value=False,
        disabled=not enrich_websites,
        help="If checked alone, only emails are scraped.",
    )
    scrape_instagram = st.checkbox(
        "📷 Instagram only",
        value=False,
        disabled=not enrich_websites,
        help="If checked alone, only Instagram links are scraped.",
    )
    if enrich_websites and not (scrape_email or scrape_instagram):
        st.caption("🌐 Scraping all channels (email + all socials).")
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

    st.markdown("---")
    run = st.button("▶️ Start scraping", type="primary", use_container_width=True)

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
if run:
    terms = [t.strip() for t in search_input.splitlines() if t.strip()]
    if not terms:
        st.error("Please enter at least one search term.")
        st.stop()

    st.session_state.results = []
    st.session_state.phase1_done = False

    # ---- Phase 1 -----------------------------------------------------------
    st.markdown("### Phase 1 — Scraping Google Maps")
    phase1_status = st.empty()
    phase1_table = st.empty()
    results: list[dict] = []

    def status1(msg: str):
        phase1_status.info(f"⛏️ {msg}")

    try:
        for place in scrape_google_maps(
            search_terms=terms,
            location=location,
            locations=locations_list,
            max_results=max_results,
            language=language,
            headless=headless,
            grid_size=effective_grid,
            status_cb=status1,
        ):
            results.append(place)
            phase1_table.dataframe(
                pd.DataFrame(results[-25:]),
                use_container_width=True,
                hide_index=True,
            )
        phase1_status.success(f"✅ Phase 1 done — {len(results)} places scraped.")
    except Exception as e:
        phase1_status.error(f"Phase 1 failed: {e}")

    st.session_state.results = results
    st.session_state.phase1_done = True

    # ---- Phase 2 -----------------------------------------------------------
    if enrich_websites and results:
        with_site = sum(1 for r in results if r.get("website"))
        if enrich_channels is None:
            channels_label = "All (email + all socials)"
        else:
            channels_label = ", ".join(
                name.capitalize() for name, on in enrich_channels.items() if on
            )
        st.markdown("### Phase 2 — Enriching websites")
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
            phase2_status.info(f"🌐 Enriching {done}/{total}")

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
            phase2_status.success("✅ Phase 2 done.")
        except Exception as e:
            phase2_status.error(f"Phase 2 failed: {e}")

        st.session_state.results = results

    # ---- Phase 3 -----------------------------------------------------------
    if results:
        st.markdown("### Phase 3 — Sort + dedupe")

        # Step 1: sort by how many fields are filled (most → least).
        before_sort = len(results)
        results = sort_by_completeness(results)
        st.info(
            f"📊 Sorted {before_sort} rows by completeness "
            "(most-filled at the top, emptiest at the bottom)."
        )

        # Step 2: dedupe by website, if enabled.
        if dedupe_websites:
            before = len(results)
            results, removed = dedupe_by_website(results)
            if removed > 0:
                st.success(
                    f"🧹 Removed {removed} duplicate row(s) sharing a website "
                    f"— {before} → {len(results)}."
                )
            else:
                st.info("No website duplicates found.")

        # Step 3: channel filter. If the user picked specific channels
        # (e.g. Instagram), drop rows that don't have that channel filled.
        before_filter = len(results)
        results, filtered_out = filter_by_channels(results, enrich_channels)
        if filtered_out > 0:
            active_names = ", ".join(
                k.capitalize() for k, v in (enrich_channels or {}).items() if v
            )
            st.success(
                f"🎯 Channel filter ({active_names}): kept {len(results)} of "
                f"{before_filter} rows (dropped {filtered_out} without the "
                f"selected channel)."
            )

        st.session_state.results = results


# --- Final results & downloads ----------------------------------------------
results = st.session_state.results

if results:
    # --- Attach lead_score to every row so it appears in CSV/JSON downloads ---
    for r in results:
        r["lead_score"] = _lead_score(r)

    # --- Sort controls -------------------------------------------------------
    st.markdown("### Sort results")
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

    st.markdown("### Final results")
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
            "⬇️ Download CSV",
            data=csv_df.to_csv(index=False).encode("utf-8"),
            file_name=f"{filename_base}.csv",
            mime="text/csv",
            use_container_width=True,
        )
    with c2:
        st.download_button(
            "⬇️ Download JSON",
            data=json.dumps(results, indent=2, ensure_ascii=False).encode("utf-8"),
            file_name=f"{filename_base}.json",
            mime="application/json",
            use_container_width=True,
        )
else:
    st.info("Enter search terms in the sidebar and click **Start scraping**.")
