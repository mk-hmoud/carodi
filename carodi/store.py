"""SQLite state.

Two things live here: what has already been seen (so the digest only ever shows
new matches), and what you did about it. The second one is the point -- a funnel
that cannot tell you "60 matches delivered, 3 applications sent" is an elaborate
way to procrastinate.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from pathlib import Path

from carodi.models import Kind, Opportunity, Remote


def to_opportunity(row: sqlite3.Row) -> Opportunity:
    """Rebuild an Opportunity from a stored row.

    The fingerprint is derived from org, title and location rather than stored,
    and all three round-trip, so the rebuilt record identifies the same row.
    """
    return Opportunity(
        source=row["source"],
        kind=Kind(row["kind"]),
        title=row["title"],
        org=row["org"],
        url=row["url"],
        location_raw=row["location_raw"] or "",
        countries=json.loads(row["countries"] or "[]"),
        remote=Remote(row["remote"]),
        deadline=date.fromisoformat(row["deadline"]) if row["deadline"] else None,
        score=row["score"] or 0.0,
        reasons=json.loads(row["reasons"] or "[]"),
        enrichment=json.loads(row["enrichment"] or "{}"),
    )

SCHEMA = """
CREATE TABLE IF NOT EXISTS opportunities (
    fingerprint   TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    kind          TEXT NOT NULL,
    title         TEXT NOT NULL,
    org           TEXT NOT NULL,
    url           TEXT NOT NULL,
    location_raw  TEXT,
    countries     TEXT,
    remote        TEXT,
    deadline      TEXT,
    score         REAL DEFAULT 0,
    reasons       TEXT,
    enrichment    TEXT,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    notified_at   TEXT,
    status        TEXT NOT NULL DEFAULT 'new',
    note          TEXT
);
CREATE INDEX IF NOT EXISTS idx_status     ON opportunities(status);
CREATE INDEX IF NOT EXISTS idx_notified   ON opportunities(notified_at);
CREATE INDEX IF NOT EXISTS idx_first_seen ON opportunities(first_seen);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    stats       TEXT
);

-- Every employer the funnel has ever seen, whether or not its roles passed.
-- This is the seed list for `carodi discover`: companies a job board already
-- listed are real tech employers by construction, which no heuristic over
-- register names manages to be.
CREATE TABLE IF NOT EXISTS seen_orgs (
    org_key           TEXT PRIMARY KEY,
    org               TEXT NOT NULL,
    sponsor_countries TEXT,
    first_seen        TEXT NOT NULL,
    last_seen         TEXT NOT NULL,
    times_seen        INTEGER NOT NULL DEFAULT 1
);

-- Probe results, cached forever so a token is never re-requested. Negative
-- results matter as much as positive ones: most companies have no public
-- board, and re-checking them nightly would be pure waste.
CREATE TABLE IF NOT EXISTS ats_probes (
    token         TEXT NOT NULL,
    provider      TEXT NOT NULL,
    org_key       TEXT NOT NULL,
    found         INTEGER NOT NULL,
    declared_name TEXT,
    confidence    INTEGER NOT NULL DEFAULT 0,
    job_count     INTEGER,
    probed_at     TEXT NOT NULL,
    PRIMARY KEY (token, provider)
);
CREATE INDEX IF NOT EXISTS idx_probe_found ON ats_probes(found, confidence);
"""

#: Terminal states -- these never reappear in a digest.
DECIDED = ("applied", "skipped")


class Store:
    def __init__(self, path: str | Path = "data/carodi.db"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # -- writing --------------------------------------------------------------

    def upsert(self, opp: Opportunity) -> bool:
        """Insert or refresh. Returns True if this fingerprint is new."""
        now = datetime.now().isoformat(timespec="seconds")
        cur = self.conn.execute(
            "SELECT fingerprint FROM opportunities WHERE fingerprint = ?", (opp.fingerprint,)
        )
        exists = cur.fetchone() is not None

        if exists:
            # Refresh the score but never touch status/notified_at -- a decision
            # you already made must survive the job being re-scraped.
            self.conn.execute(
                "UPDATE opportunities SET last_seen = ?, score = ?, reasons = ?, enrichment = ? "
                "WHERE fingerprint = ?",
                (now, opp.score, json.dumps(opp.reasons), json.dumps(opp.enrichment, default=str),
                 opp.fingerprint),
            )
            return False

        self.conn.execute(
            """INSERT INTO opportunities (
                   fingerprint, source, kind, title, org, url, location_raw, countries,
                   remote, deadline, score, reasons, enrichment, first_seen, last_seen)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                opp.fingerprint, opp.source, str(opp.kind), opp.title, opp.org, str(opp.url),
                opp.location_raw, json.dumps(opp.countries), str(opp.remote),
                opp.deadline.isoformat() if opp.deadline else None,
                opp.score, json.dumps(opp.reasons),
                json.dumps(opp.enrichment, default=str), now, now,
            ),
        )
        return True

    def commit(self) -> None:
        self.conn.commit()

    def mark_notified(self, fingerprints: Iterable[str]) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        self.conn.executemany(
            "UPDATE opportunities SET notified_at = ?, status = 'notified' "
            "WHERE fingerprint = ? AND notified_at IS NULL",
            [(now, fp) for fp in fingerprints],
        )
        self.conn.commit()

    def set_status(self, fingerprint: str, status: str, note: str | None = None) -> bool:
        cur = self.conn.execute(
            "UPDATE opportunities SET status = ?, note = COALESCE(?, note) WHERE fingerprint = ?",
            (status, note, fingerprint),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def start_run(self) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (started_at) VALUES (?)",
            (datetime.now().isoformat(timespec="seconds"),),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, stats: dict) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at = ?, stats = ? WHERE id = ?",
            (datetime.now().isoformat(timespec="seconds"), json.dumps(stats), run_id),
        )
        self.conn.commit()

    # -- reading --------------------------------------------------------------

    def undelivered(self, limit: int = 25) -> list[Opportunity]:
        """Everything stored but never sent, best first.

        The digest must be built from this rather than from "what the funnel
        found this run". If a run stores matches and then fails before
        delivering -- a missing token, a Telegram outage, a killed process --
        those rows keep notified_at NULL but stop being newly-seen, so keying
        the digest off novelty would strand them forever.
        """
        rows = self.conn.execute(
            "SELECT * FROM opportunities WHERE notified_at IS NULL "
            "ORDER BY score DESC, first_seen DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [to_opportunity(r) for r in rows]

    def accountability(self, days: int = 30) -> dict:
        """The uncomfortable numbers that go in the digest footer."""
        since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        row = self.conn.execute(
            """SELECT
                 COUNT(*) FILTER (WHERE notified_at >= ?)                        AS delivered,
                 COUNT(*) FILTER (WHERE status = 'applied' AND first_seen >= ?)  AS applied,
                 COUNT(*) FILTER (WHERE status = 'skipped' AND first_seen >= ?)  AS skipped,
                 COUNT(*) FILTER (WHERE status = 'notified' AND notified_at >= ?) AS undecided
               FROM opportunities""",
            (since, since, since, since),
        ).fetchone()
        return {"days": days, **{k: row[k] for k in row.keys()}}

    def undecided(self, limit: int = 50) -> list[Opportunity]:
        """Delivered but not yet acted on, best first.

        Ordering must be stable between calls: the triage flow navigates by
        position in this list, so a reshuffle mid-session would jump you
        around. Score then notified_at then fingerprint is fully determined.
        """
        rows = self.conn.execute(
            "SELECT * FROM opportunities WHERE status = 'notified' "
            "ORDER BY score DESC, notified_at DESC, fingerprint ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [to_opportunity(r) for r in rows]

    # -- employer harvest -----------------------------------------------------

    def record_orgs(self, items: Iterable[Opportunity]) -> int:
        """Remember every employer seen this run, passing or not.

        Called with the deduped set *before* filtering: a company whose current
        openings are all senior is still worth watching, and is exactly the kind
        of lead the filter would otherwise throw away.
        """
        now = datetime.now().isoformat(timespec="seconds")
        rows = 0
        for opp in items:
            key = opp.org_key
            if not key:
                continue
            # Only registers matching at >=90 reach sponsor_countries, so the
            # 60%-confidence noise never gets promoted into a discovery lead.
            sponsors = json.dumps(opp.enrichment.get("sponsor_countries") or [])
            self.conn.execute(
                """INSERT INTO seen_orgs (org_key, org, sponsor_countries, first_seen,
                                          last_seen, times_seen)
                   VALUES (?,?,?,?,?,1)
                   ON CONFLICT(org_key) DO UPDATE SET
                       last_seen = excluded.last_seen,
                       times_seen = times_seen + 1,
                       sponsor_countries = excluded.sponsor_countries""",
                (key, opp.org, sponsors, now, now),
            )
            rows += 1
        return rows

    def seed_orgs(self, sponsored_only: bool = True, limit: int | None = None) -> list[dict]:
        """Employers to probe for a public job board, most-seen first."""
        sql = "SELECT org_key, org, sponsor_countries, times_seen FROM seen_orgs"
        if sponsored_only:
            sql += " WHERE sponsor_countries IS NOT NULL AND sponsor_countries != '[]'"
        sql += " ORDER BY times_seen DESC, org_key ASC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [
            {
                "org_key": r["org_key"],
                "org": r["org"],
                "sponsors": json.loads(r["sponsor_countries"] or "[]"),
                "times_seen": r["times_seen"],
            }
            for r in self.conn.execute(sql).fetchall()
        ]

    def cached_probe(self, token: str, provider: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM ats_probes WHERE token = ? AND provider = ?", (token, provider)
        ).fetchone()

    def record_probe(
        self,
        token: str,
        provider: str,
        org_key: str,
        found: bool,
        declared_name: str | None = None,
        confidence: int = 0,
        job_count: int | None = None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO ats_probes (token, provider, org_key, found, declared_name,
                                       confidence, job_count, probed_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(token, provider) DO UPDATE SET
                   found = excluded.found,
                   declared_name = excluded.declared_name,
                   confidence = excluded.confidence,
                   job_count = excluded.job_count,
                   probed_at = excluded.probed_at""",
            (
                token, provider, org_key, int(found), declared_name, confidence,
                job_count, datetime.now().isoformat(timespec="seconds"),
            ),
        )

    def discovered(self, min_confidence: int = 0) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT p.*, o.org, o.sponsor_countries FROM ats_probes p "
            "LEFT JOIN seen_orgs o ON o.org_key = p.org_key "
            "WHERE p.found = 1 AND p.confidence >= ? "
            "ORDER BY p.confidence DESC, p.job_count DESC",
            (min_confidence,),
        ).fetchall()

    def count_undecided(self) -> int:
        return int(
            self.conn.execute(
                "SELECT COUNT(*) FROM opportunities WHERE status = 'notified'"
            ).fetchone()[0]
        )
