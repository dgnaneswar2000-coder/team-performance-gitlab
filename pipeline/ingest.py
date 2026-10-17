"""Extract: pull merged MRs from GitLab and land the responses untouched.

The load unit is one calendar day (UTC) of merges. That keeps every listing
short (~3-4 pages), makes runs resumable, and makes "incremental" simple:
a day that was fully loaded and is old enough to have settled is skipped.

Raw tables are append-only and hold whole response bodies, so transforms can
be fixed and replayed (`--transform-only`) without calling the API again.
Note: raw keeps names exactly as GitLab returns them. It stays on the local
machine (data/ is git-ignored); anonymisation happens in staging.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime, timedelta, timezone

from . import config
from .client import GitLabClient, RateLimitExhausted

log = logging.getLogger(__name__)

RAW_DDL = """
CREATE TABLE IF NOT EXISTS ingest_runs (
    run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        TEXT,            -- running | success | partial | failed
    requests_made INTEGER,
    days_loaded   INTEGER,
    notes         TEXT
);
CREATE TABLE IF NOT EXISTS raw_mr_pages (
    run_id       INTEGER NOT NULL,
    merged_day   TEXT NOT NULL,     -- YYYY-MM-DD (UTC) the listing covers
    page         INTEGER NOT NULL,
    page_url     TEXT NOT NULL,
    api_total    INTEGER,           -- X-Total header: what GitLab says exists
    fetched_at   TEXT NOT NULL,
    payload      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ingest_days (
    merged_day   TEXT PRIMARY KEY,
    run_id       INTEGER NOT NULL,
    loaded_at    TEXT NOT NULL,
    rows_landed  INTEGER NOT NULL,
    api_total    INTEGER
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.executescript(RAW_DDL)
    return conn


def days_to_load(conn, today: date, full_refresh: bool = False) -> list[date]:
    """Every day in the lookback window that is missing or still settling."""
    loaded = {r[0] for r in conn.execute("SELECT merged_day FROM ingest_days")}
    settle_cutoff = today - timedelta(days=config.REFRESH_RECENT_DAYS - 1)
    days = []
    for offset in range(config.LOOKBACK_DAYS, -1, -1):
        d = today - timedelta(days=offset)
        if full_refresh or d.isoformat() not in loaded or d >= settle_cutoff:
            days.append(d)
    return days


def run(full_refresh: bool = False, client: GitLabClient | None = None,
        conn: sqlite3.Connection | None = None, today: date | None = None) -> int:
    client = client or GitLabClient()
    conn = conn or connect()
    today = today or datetime.now(timezone.utc).date()
    run_id = conn.execute(
        "INSERT INTO ingest_runs (started_at, status) VALUES (?, 'running')", (_now(),)
    ).lastrowid
    conn.commit()

    notes, status, loaded = [], "success", 0
    try:
        for day in days_to_load(conn, today, full_refresh):
            try:
                _load_day(conn, client, run_id, day)
                loaded += 1
            except RuntimeError as exc:
                # One bad day shouldn't sink the run; it stays unloaded and is retried next time.
                status = "partial"
                notes.append(f"{day}: {exc}")
                log.warning(notes[-1])
    except RateLimitExhausted as exc:
        status = "partial"
        notes.append(f"stopped early ({exc}); the next run resumes from the first unloaded day")
        log.warning(notes[-1])
    except Exception as exc:
        status = "failed"
        notes.append(f"unexpected error: {exc!r}")
        raise
    finally:
        conn.execute(
            "UPDATE ingest_runs SET finished_at=?, status=?, requests_made=?, days_loaded=?, notes=? "
            "WHERE run_id=?",
            (_now(), status, client.requests_made, loaded, "\n".join(notes) or None, run_id),
        )
        conn.commit()
        log.info("run %s: %s, %d day(s), %d requests", run_id, status, loaded, client.requests_made)
    return run_id


def _load_day(conn, client, run_id, day: date):
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    pages, rows, api_total = [], 0, None
    # Collect the whole day before writing, so a day is either fully landed
    # or not marked as loaded at all.
    for page in client.iter_merged_mrs(start.isoformat(), end.isoformat()):
        pages.append(page)
        rows += len(page.body)
        api_total = page.total if page.total is not None else api_total

    fetched_at = _now()
    conn.executemany(
        "INSERT INTO raw_mr_pages VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(run_id, day.isoformat(), p.page, p.url, p.total, fetched_at, json.dumps(p.body)) for p in pages],
    )
    conn.execute(
        "INSERT INTO ingest_days VALUES (?, ?, ?, ?, ?) ON CONFLICT(merged_day) DO UPDATE SET "
        "run_id=excluded.run_id, loaded_at=excluded.loaded_at, rows_landed=excluded.rows_landed, "
        "api_total=excluded.api_total",
        (day.isoformat(), run_id, fetched_at, rows, api_total),
    )
    conn.commit()
    log.info("%s: %d MRs in %d page(s)", day, rows, len(pages))
