"""Offline tests: a fake HTTP session replays the failure modes seen on the live API."""
import json
import sqlite3
from datetime import date

import pandas as pd
import pytest
import requests

from pipeline import config, ingest, transform
from pipeline.client import GitLabClient, RateLimitExhausted


class FakeResp:
    def __init__(self, status=200, body=None, text=None, headers=None):
        self.status_code, self._body, self.headers, self.url = status, body, headers or {}, "https://x"
        self.text = text if text is not None else json.dumps(body)

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body

    def raise_for_status(self):
        raise requests.HTTPError(self.status_code)


class FakeSession:
    def __init__(self, responses):
        self.responses, self.headers = list(responses), {}

    def get(self, url, params=None, timeout=None):
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def client(responses, slept=None):
    return GitLabClient(session=FakeSession(responses),
                        sleep=(slept.append if slept is not None else lambda s: None))


# ------------------------------------------------------------------ client
def test_retries_5xx_network_errors_and_bad_bodies_then_succeeds():
    c = client([FakeResp(502, text="bad gateway"), requests.Timeout("slow"),
                FakeResp(200, body=None, text="<html>"), FakeResp(200, {"not": "a list"}),
                FakeResp(200, [{"id": 1}])])
    assert c.get_list("u", {}).body == [{"id": 1}]


def test_429_waits_for_retry_after():
    slept = []
    c = client([FakeResp(429, text="", headers={"Retry-After": "30"}), FakeResp(200, [])], slept)
    c.get_list("u", {})
    assert 30.0 in slept


def test_429_with_long_wait_stops_the_run_instead_of_blocking():
    with pytest.raises(RateLimitExhausted):
        client([FakeResp(429, text="", headers={"Retry-After": "3600"})]).get_list("u", {})


def test_follows_x_next_page_until_it_is_empty():
    pages = [FakeResp(200, [{"id": 1}], headers={"X-Next-Page": "2", "X-Total": "2"}),
             FakeResp(200, [{"id": 2}], headers={"X-Next-Page": "", "X-Total": "2"})]
    got = list(client(pages).iter_merged_mrs("a", "b"))
    assert [p.body[0]["id"] for p in got] == [1, 2]


# ----------------------------------------------------------------- staging
def mr(**kw):
    base = {"id": 1, "iid": 10, "title": "Add thing", "source_branch": "add-thing",
            "created_at": "2026-10-01T10:00:00Z", "merged_at": "2026-10-01T16:00:00Z",
            "author": {"id": 42, "username": "alice"}, "labels": ["group::code review", "type::bug"],
            "user_notes_count": 3, "time_stats": {"total_time_spent": 0}}
    base.update(kw)
    return base


def test_stage_mr_cleans_and_anonymises():
    row = transform.stage_mr(mr())
    assert row["associate"].startswith("Associate ") and "alice" not in json.dumps(row)
    assert (row["team"], row["work_type"], row["cycle_hours"]) == ("code review", "bug", 6.0)
    assert row["time_spent_hours"] is None  # 0 means "not tracked", not "zero effort"


def test_stage_mr_survives_missing_fields():
    row = transform.stage_mr(mr(author=None, labels=None, time_stats=None, created_at=None))
    assert row["associate"] is None and row["team"] is None and row["cycle_hours"] is None
    assert transform.stage_mr({"id": 5}) is None  # no merge date: unusable


def test_bots_are_flagged():
    assert transform.stage_mr(mr(author={"id": 1, "username": "gitlab-bot"}))["is_bot"] == 1


@pytest.mark.parametrize("title, description, expected", [
    ('Revert "Merge branch \'fix-x\' into \'master\'"', "", ("fix-x", None, None)),
    ('Revert "Add thing"', "", (None, "Add thing", None)),
    ('Revert "Add thing"', "This reverts merge request !10", (None, "Add thing", 10)),
])
def test_parse_revert(title, description, expected):
    r = transform.parse_revert(title, description)
    assert r["is_revert"] == 1
    assert (r["reverted_branch"], r["reverted_title"], r["reverted_iid"]) == expected


def test_link_reverts_matches_by_iid_branch_or_title_and_never_forwards_in_time():
    rows = [transform.stage_mr(m) for m in [
        mr(id=1, iid=10, source_branch="a", title="A", merged_at="2026-10-01T10:00:00Z"),
        mr(id=2, iid=11, source_branch="b", title="B", merged_at="2026-10-02T10:00:00Z"),
        mr(id=3, iid=12, title='Revert "Merge branch \'b\' into \'master\'"', merged_at="2026-10-03T10:00:00Z"),
        mr(id=4, iid=13, title='Revert "A"', description="reverts merge request !10",
           merged_at="2026-10-04T10:00:00Z"),
        mr(id=5, iid=14, title='Revert "Z"', merged_at="2026-10-04T10:00:00Z"),  # original not loaded
    ]]
    links = transform.link_reverts(pd.DataFrame(rows)).set_index("revert_mr_id").original_mr_id
    assert links[3] == 2 and links[4] == 1 and pd.isna(links[5])


def test_median_aggregate():
    conn = sqlite3.connect(":memory:")
    conn.create_aggregate("MEDIAN", 1, transform.Median)
    conn.execute("CREATE TABLE t (x REAL)")
    conn.executemany("INSERT INTO t VALUES (?)", [(1,), (100,), (3,), (None,)])
    assert conn.execute("SELECT MEDIAN(x) FROM t").fetchone()[0] == 3


# ---------------------------------------------------------- incremental load
def test_only_missing_and_recent_days_are_reloaded(monkeypatch):
    monkeypatch.setattr(config, "LOOKBACK_DAYS", 4)
    conn = sqlite3.connect(":memory:")
    conn.executescript(ingest.RAW_DDL)
    today = date(2026, 10, 17)
    first = ingest.days_to_load(conn, today)
    assert len(first) == 5  # 13th..17th
    for d in first:
        conn.execute("INSERT INTO ingest_days VALUES (?, 1, 'x', 0, 0)", (d.isoformat(),))
    again = ingest.days_to_load(conn, today)
    assert [d.isoformat() for d in again] == ["2026-10-16", "2026-10-17"]  # still settling


def test_a_failed_day_is_not_marked_loaded(monkeypatch):
    monkeypatch.setattr(config, "LOOKBACK_DAYS", 0)
    conn = sqlite3.connect(":memory:")
    conn.executescript(ingest.RAW_DDL)
    c = client([FakeResp(500, text="x")] * config.MAX_RETRIES)
    ingest.run(client=c, conn=conn, today=date(2026, 10, 17))
    assert conn.execute("SELECT COUNT(*) FROM ingest_days").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM ingest_runs").fetchone()[0] == "partial"
