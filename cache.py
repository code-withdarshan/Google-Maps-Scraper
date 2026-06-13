"""Persistent on-disk cache for scraped places, keyed by Google's stable fid.

Cuts re-scrape cost to zero for repeated/overlapping searches. Lives at
~/.cache/gmaps_scraper/places.db. Records older than TTL_DAYS are ignored.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional


CACHE_DIR = Path.home() / ".cache" / "gmaps_scraper"
DB_PATH = CACHE_DIR / "places.db"
TTL_DAYS = 30

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is not None:
        return _conn
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    _conn.execute(
        "CREATE TABLE IF NOT EXISTS places ("
        "  fid TEXT PRIMARY KEY,"
        "  data TEXT NOT NULL,"
        "  ts  REAL NOT NULL"
        ")"
    )
    _conn.execute(
        "CREATE TABLE IF NOT EXISTS runs ("
        "  signature TEXT PRIMARY KEY,"
        "  params_json TEXT NOT NULL,"
        "  done_fids_json TEXT NOT NULL,"
        "  completed_tiles_json TEXT NOT NULL,"
        "  status TEXT NOT NULL,"
        "  started_at REAL NOT NULL,"
        "  updated_at REAL NOT NULL,"
        "  results_json TEXT,"
        "  stats_json TEXT"
        ")"
    )
    # Best-effort column adds for pre-existing DBs.
    for col in ("results_json TEXT", "stats_json TEXT"):
        try:
            _conn.execute(f"ALTER TABLE runs ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    _conn.execute(
        "CREATE TABLE IF NOT EXISTS presets ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  name TEXT NOT NULL UNIQUE,"
        "  params_json TEXT NOT NULL,"
        "  created_at REAL NOT NULL"
        ")"
    )
    _conn.commit()
    return _conn


# --- Run-state (mid-run resume) ---------------------------------------------

import hashlib  # noqa: E402


def make_signature(params: dict) -> str:
    """Stable hash of the user's scrape parameters."""
    canonical = json.dumps(params, sort_keys=True, default=str)
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]


def start_run(signature: str, params: dict) -> None:
    now = time.time()
    with _lock:
        c = _connect()
        existing = c.execute(
            "SELECT status FROM runs WHERE signature = ?", (signature,)
        ).fetchone()
        if existing is None:
            c.execute(
                "INSERT INTO runs (signature, params_json, done_fids_json,"
                " completed_tiles_json, status, started_at, updated_at)"
                " VALUES (?, ?, '[]', '[]', 'running', ?, ?)",
                (signature, json.dumps(params, default=str), now, now),
            )
        else:
            c.execute(
                "UPDATE runs SET status='running', updated_at=? WHERE signature=?",
                (now, signature),
            )
        c.commit()


def get_run(signature: str) -> Optional[dict]:
    with _lock:
        row = _connect().execute(
            "SELECT status, done_fids_json, completed_tiles_json, started_at,"
            " updated_at, params_json FROM runs WHERE signature = ?",
            (signature,),
        ).fetchone()
    if not row:
        return None
    status, done_fids, completed_tiles, started_at, updated_at, params_json = row
    try:
        return {
            "status": status,
            "done_fids": set(json.loads(done_fids)),
            "completed_tiles": set(tuple(t) for t in json.loads(completed_tiles)),
            "started_at": started_at,
            "updated_at": updated_at,
            "params": json.loads(params_json),
        }
    except Exception:
        return None


def list_unfinished_runs() -> list[dict]:
    with _lock:
        rows = _connect().execute(
            "SELECT signature, params_json, status, started_at, updated_at,"
            " done_fids_json FROM runs WHERE status IN ('running','paused','crashed')"
            " ORDER BY updated_at DESC"
        ).fetchall()
    out = []
    for sig, params_json, status, started_at, updated_at, done_fids in rows:
        try:
            out.append({
                "signature": sig,
                "params": json.loads(params_json),
                "status": status,
                "started_at": started_at,
                "updated_at": updated_at,
                "done_count": len(json.loads(done_fids)),
            })
        except Exception:
            continue
    return out


def add_done_fid(signature: str, fid: str) -> None:
    if not signature or not fid:
        return
    with _lock:
        c = _connect()
        row = c.execute(
            "SELECT done_fids_json FROM runs WHERE signature = ?", (signature,)
        ).fetchone()
        if not row:
            return
        try:
            ids = json.loads(row[0])
        except Exception:
            ids = []
        if fid in ids:
            return
        ids.append(fid)
        c.execute(
            "UPDATE runs SET done_fids_json=?, updated_at=? WHERE signature=?",
            (json.dumps(ids), time.time(), signature),
        )
        c.commit()


def add_completed_tile(signature: str, tile_key: tuple) -> None:
    if not signature:
        return
    with _lock:
        c = _connect()
        row = c.execute(
            "SELECT completed_tiles_json FROM runs WHERE signature = ?",
            (signature,),
        ).fetchone()
        if not row:
            return
        try:
            tiles = json.loads(row[0])
        except Exception:
            tiles = []
        as_list = list(tile_key)
        if as_list in tiles:
            return
        tiles.append(as_list)
        c.execute(
            "UPDATE runs SET completed_tiles_json=?, updated_at=? WHERE signature=?",
            (json.dumps(tiles), time.time(), signature),
        )
        c.commit()


def set_run_status(signature: str, status: str) -> None:
    if not signature:
        return
    with _lock:
        c = _connect()
        c.execute(
            "UPDATE runs SET status=?, updated_at=? WHERE signature=?",
            (status, time.time(), signature),
        )
        c.commit()


def delete_run(signature: str) -> None:
    if not signature:
        return
    with _lock:
        c = _connect()
        c.execute("DELETE FROM runs WHERE signature=?", (signature,))
        c.commit()


def save_run_results(signature: str, results: list[dict], stats: dict | None = None) -> None:
    """Persist the final, post-Phase-3 results so the history list can re-load them."""
    if not signature:
        return
    with _lock:
        c = _connect()
        c.execute(
            "UPDATE runs SET results_json=?, stats_json=?, updated_at=? WHERE signature=?",
            (
                json.dumps(results, default=str),
                json.dumps(stats or {}, default=str),
                time.time(),
                signature,
            ),
        )
        c.commit()


def list_done_runs(limit: int = 30) -> list[dict]:
    """Past completed runs, newest first. Used by the History panel."""
    with _lock:
        rows = _connect().execute(
            "SELECT signature, params_json, status, started_at, updated_at,"
            " done_fids_json, results_json, stats_json"
            " FROM runs WHERE status IN ('done','stopped')"
            " ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    out = []
    for sig, params_json, status, started_at, updated_at, done_fids, results_json, stats_json in rows:
        try:
            out.append({
                "signature": sig,
                "params": json.loads(params_json),
                "status": status,
                "started_at": started_at,
                "updated_at": updated_at,
                "done_count": len(json.loads(done_fids)) if done_fids else 0,
                "has_results": bool(results_json),
                "stats": json.loads(stats_json) if stats_json else {},
            })
        except Exception:
            continue
    return out


def get_run_results(signature: str) -> list[dict] | None:
    """Load persisted post-Phase-3 results for a finished run."""
    if not signature:
        return None
    with _lock:
        row = _connect().execute(
            "SELECT results_json FROM runs WHERE signature=?", (signature,)
        ).fetchone()
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return None


# --- Presets (saved searches) -----------------------------------------------

def save_preset(name: str, params: dict) -> int | None:
    """Save (or overwrite by name) a search preset. Returns its row id."""
    if not name or not name.strip():
        return None
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO presets (name, params_json, created_at) VALUES (?,?,?)"
            " ON CONFLICT(name) DO UPDATE SET params_json=excluded.params_json,"
            " created_at=excluded.created_at",
            (name.strip(), json.dumps(params, default=str), time.time()),
        )
        c.commit()
        row = c.execute("SELECT id FROM presets WHERE name=?", (name.strip(),)).fetchone()
    return row[0] if row else None


def list_presets() -> list[dict]:
    with _lock:
        rows = _connect().execute(
            "SELECT id, name, params_json, created_at FROM presets ORDER BY name"
        ).fetchall()
    out = []
    for pid, name, params_json, created_at in rows:
        try:
            out.append({
                "id": pid,
                "name": name,
                "params": json.loads(params_json),
                "created_at": created_at,
            })
        except Exception:
            continue
    return out


def get_preset(preset_id: int) -> dict | None:
    with _lock:
        row = _connect().execute(
            "SELECT name, params_json FROM presets WHERE id=?", (preset_id,)
        ).fetchone()
    if not row:
        return None
    try:
        return {"name": row[0], "params": json.loads(row[1])}
    except Exception:
        return None


def delete_preset(preset_id: int) -> None:
    with _lock:
        c = _connect()
        c.execute("DELETE FROM presets WHERE id=?", (preset_id,))
        c.commit()


def get(fid: str) -> Optional[dict]:
    """Return the cached place dict if present and within TTL, else None."""
    if not fid:
        return None
    cutoff = time.time() - TTL_DAYS * 86400
    with _lock:
        cur = _connect().execute(
            "SELECT data, ts FROM places WHERE fid = ?", (fid,)
        )
        row = cur.fetchone()
    if not row:
        return None
    if row[1] < cutoff:
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return None


def put(fid: str, place: dict) -> None:
    if not fid:
        return
    payload = json.dumps(place, ensure_ascii=False)
    with _lock:
        c = _connect()
        c.execute(
            "INSERT OR REPLACE INTO places (fid, data, ts) VALUES (?,?,?)",
            (fid, payload, time.time()),
        )
        c.commit()


def clear() -> int:
    """Wipe the cache. Returns the number of rows deleted."""
    with _lock:
        c = _connect()
        n = c.execute("SELECT COUNT(*) FROM places").fetchone()[0]
        c.execute("DELETE FROM places")
        c.commit()
    return n


def size() -> tuple[int, int]:
    """Return (rows, bytes)."""
    with _lock:
        c = _connect()
        rows = c.execute("SELECT COUNT(*) FROM places").fetchone()[0]
    try:
        nbytes = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    except Exception:
        nbytes = 0
    return rows, nbytes
