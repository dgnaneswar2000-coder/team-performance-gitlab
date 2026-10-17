"""Transform: raw JSON -> anonymised staging tables -> SQL reporting tables.

Staging is Python because it does the fiddly parts SQL is bad at: hashing
user ids into pseudonyms, regex-parsing revert titles, picking labels out of
arrays, and matching each revert to the task it undid. That keeps the SQL in
sql/ down to simple, readable SELECT ... GROUP BY statements.

SQLite has no MEDIAN, so we register one; the SQL can then just say
MEDIAN(cycle_hours). Reporting tables are materialised (CREATE TABLE AS), so
they open fine in any SQLite tool, which wouldn't know our MEDIAN function.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from datetime import datetime
from statistics import median

import pandas as pd

from . import config

log = logging.getLogger(__name__)

# 'Revert "Merge branch 'fix-x' into 'master'"'  or  'Revert "Some MR title"'
REVERT_TITLE_RE = re.compile(r'^Revert\s+"(?P<inner>.*)"\s*$', re.IGNORECASE)
MERGE_BRANCH_RE = re.compile(r"^Merge branch '(?P<branch>.+?)' into '.+'$")
# 'This reverts merge request !123456' (GitLab's own revert button writes this)
REVERTS_IID_RE = re.compile(r"reverts merge request !(?P<iid>\d+)", re.IGNORECASE)


def pseudonym(user_id) -> str | None:
    """Stable, non-reversible display name, e.g. 'Associate 3FA2C1'."""
    if user_id is None:
        return None
    digest = hashlib.sha256(f"{config.ANON_SALT}:{user_id}".encode()).hexdigest()
    return f"Associate {digest[:6].upper()}"


def is_bot(username: str | None) -> bool:
    u = (username or "").lower()
    return any(p in u for p in config.BOT_USERNAME_PATTERNS)


def parse_revert(title: str | None, description: str | None) -> dict:
    out = {"is_revert": 0, "reverted_iid": None, "reverted_branch": None, "reverted_title": None}
    m = REVERT_TITLE_RE.match(title or "")
    if not m:
        return out
    out["is_revert"] = 1
    inner = m.group("inner")
    if b := MERGE_BRANCH_RE.match(inner):
        out["reverted_branch"] = b.group("branch")
    else:
        out["reverted_title"] = inner
    if i := REVERTS_IID_RE.search(description or ""):
        out["reverted_iid"] = int(i.group("iid"))
    return out


def pick_label(labels, prefix: str) -> tuple[str | None, int]:
    """First label with the prefix (sorted, so the choice is deterministic) and how many there were."""
    matches = sorted(l[len(prefix):] for l in labels or [] if isinstance(l, str) and l.startswith(prefix))
    return (matches[0] if matches else None), len(matches)


def _hours_between(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    a = datetime.fromisoformat(start.replace("Z", "+00:00"))
    b = datetime.fromisoformat(end.replace("Z", "+00:00"))
    return round((b - a).total_seconds() / 3600, 2)


def stage_mr(mr: dict) -> dict | None:
    """One raw MR -> one clean row. Returns None if the record is unusable."""
    if not isinstance(mr, dict) or not mr.get("id") or not mr.get("merged_at"):
        return None
    author = mr.get("author") or {}          # deleted users come back as null
    time_stats = mr.get("time_stats") or {}  # sometimes absent entirely
    team, n_team = pick_label(mr.get("labels"), config.TEAM_LABEL_PREFIX)
    work_type, _ = pick_label(mr.get("labels"), config.TYPE_LABEL_PREFIX)
    spent = time_stats.get("total_time_spent")
    return {
        "mr_id": mr["id"],
        "iid": mr.get("iid"),
        "title": mr.get("title"),
        "source_branch": mr.get("source_branch"),
        "created_at": mr.get("created_at"),
        "merged_at": mr["merged_at"],
        "merged_day": mr["merged_at"][:10],
        "associate": pseudonym(author.get("id")),
        "is_bot": int(is_bot(author.get("username"))),
        "team": team,
        "team_label_count": n_team,
        "work_type": work_type,
        "review_comments": mr.get("user_notes_count"),
        "time_spent_hours": round(spent / 3600, 2) if isinstance(spent, (int, float)) and spent > 0 else None,
        "cycle_hours": _hours_between(mr.get("created_at"), mr["merged_at"]),
        **parse_revert(mr.get("title"), mr.get("description")),
        "web_url": mr.get("web_url"),
    }


def link_reverts(df: pd.DataFrame) -> pd.DataFrame:
    """For each revert, find the task it undid.

    Tries, in order: the MR number GitLab writes into the description, the
    branch name in the title, then the original title. The original must have
    been merged before the revert; if several match, the most recent wins.
    """
    originals = df[df.is_revert == 0].sort_values("merged_at")
    by_iid = dict(zip(originals.iid, zip(originals.mr_id, originals.merged_at)))
    by_branch = dict(zip(originals.source_branch, zip(originals.mr_id, originals.merged_at)))
    by_title = dict(zip(originals.title, zip(originals.mr_id, originals.merged_at)))

    links = []
    for r in df[df.is_revert == 1].itertuples():
        match = (by_iid.get(int(r.reverted_iid)) if pd.notna(r.reverted_iid) else None) \
            or by_branch.get(r.reverted_branch) or by_title.get(r.reverted_title)
        if match and match[1] >= r.merged_at:
            match = None  # can't revert something merged later
        links.append({"revert_mr_id": r.mr_id, "revert_day": r.merged_day,
                      "original_mr_id": match[0] if match else None})
    return pd.DataFrame(links, columns=["revert_mr_id", "revert_day", "original_mr_id"])


def build_staging(conn: sqlite3.Connection) -> dict:
    pages = pd.read_sql("SELECT run_id, fetched_at, payload FROM raw_mr_pages", conn)
    rows, skipped = [], 0
    for p in pages.itertuples():
        try:
            body = json.loads(p.payload)
        except ValueError:
            skipped += 1
            continue
        for mr in body if isinstance(body, list) else []:
            row = stage_mr(mr)
            if row is None:
                skipped += 1
                continue
            row["fetched_at"] = p.fetched_at
            rows.append(row)
    if not rows:
        raise SystemExit("No raw data yet. Run `python run_pipeline.py` first.")

    df = pd.DataFrame(rows)
    # Recent days are re-fetched on every run; keep the newest copy of each MR.
    df = df.sort_values("fetched_at").drop_duplicates("mr_id", keep="last")
    df.to_sql("stg_merge_requests", conn, if_exists="replace", index=False)
    link_reverts(df).to_sql("stg_reverts", conn, if_exists="replace", index=False)
    conn.execute("DROP TABLE IF EXISTS stg_load_stats")
    conn.execute("CREATE TABLE stg_load_stats AS SELECT ? AS skipped_records", (skipped,))
    conn.commit()
    stats = {"stg_merge_requests": len(df), "skipped_records": skipped}
    log.info("staging built: %s", stats)
    return stats


class Median:
    """MEDIAN() aggregate for SQLite. NULLs are ignored, like AVG()."""

    def __init__(self):
        self.values = []

    def step(self, value):
        if value is not None:
            self.values.append(value)

    def finalize(self):
        return median(self.values) if self.values else None


def build_marts(conn: sqlite3.Connection):
    conn.create_aggregate("MEDIAN", 1, Median)
    for path in sorted(config.SQL_DIR.glob("*.sql")):
        conn.executescript(path.read_text())
        log.info("applied %s", path.name)
    conn.commit()


def run(conn: sqlite3.Connection) -> dict:
    stats = build_staging(conn)
    build_marts(conn)
    return stats
