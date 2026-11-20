"""SQLite memory of every application attempt.

Purpose: never apply twice to the same job with the same candidate, and keep a
permanent audit trail even if the Google Sheet or CSV is deleted.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import Iterable

from .config import DATA_DIR
import threading

from .models import ALREADY_APPLIED, ApplicationResult, Job, SUBMITTED

DB_PATH = DATA_DIR / "applications.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS attempts (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    job_key            TEXT NOT NULL,
    candidate_id       TEXT NOT NULL,
    candidate_name     TEXT,
    email_used         TEXT,
    phone_used         TEXT,
    company            TEXT,
    title              TEXT,
    job_location       TEXT,
    source             TEXT,
    status             TEXT NOT NULL,
    detail             TEXT,
    score              REAL,
    resume_file        TEXT,
    resume_attached    INTEGER,
    fields_filled      INTEGER,
    apply_url          TEXT,
    screenshot         TEXT,
    confirmation_text  TEXT,
    match_reasons      TEXT,
    applied_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_attempt_pair ON attempts (candidate_id, job_key);
CREATE INDEX IF NOT EXISTS idx_attempt_status ON attempts (status);
"""


# Columns added after the first release; init() migrates old databases.
_EXTRA_COLUMNS = {
    "fields_skipped": "TEXT",
    "candidate_location": "TEXT",
    "linkedin": "TEXT",
    "github": "TEXT",
    "portfolio": "TEXT",
    "company_is_ai": "INTEGER",
    "job_url": "TEXT",
    "duration_s": "REAL",
}

_LOCK = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with closing(_connect()) as conn:
        conn.executescript(_SCHEMA)
        have = {r["name"] for r in conn.execute("PRAGMA table_info(attempts)")}
        for col, kind in _EXTRA_COLUMNS.items():
            if col not in have:
                conn.execute(f"ALTER TABLE attempts ADD COLUMN {col} {kind}")
        conn.commit()


def record(result: ApplicationResult) -> None:
    """Persist one attempt (success or failure -- we log the truth either way)."""
    reasons = result.match_reasons
    if isinstance(reasons, (list, tuple)):
        reasons = "; ".join(reasons)
    values = {
        "job_key": result.job.key,
        "candidate_id": result.candidate_id,
        "candidate_name": result.candidate_name,
        "email_used": result.email,
        "phone_used": result.phone,
        "company": result.job.company,
        "title": result.job.title,
        "job_location": result.job.location,
        "source": result.job.source,
        "status": result.status,
        "detail": result.detail,
        "score": result.score,
        "resume_file": result.resume_file,
        "resume_attached": 1 if result.resume_attached else 0,
        "fields_filled": result.fields_filled,
        "apply_url": result.job.apply_url or result.job.url,
        "screenshot": result.screenshot,
        "confirmation_text": result.confirmation_text,
        "match_reasons": reasons or "",
        "applied_at": result.applied_at,
        "fields_skipped": " | ".join(result.fields_skipped or []),
        "candidate_location": result.candidate_location,
        "linkedin": result.linkedin,
        "github": result.github,
        "portfolio": result.portfolio,
        "company_is_ai": 1 if result.job.company_is_ai else 0,
        "job_url": result.job.url,
        "duration_s": result.duration_s,
    }
    cols = ", ".join(values)
    marks = ",".join("?" for _ in values)
    with _LOCK, closing(_connect()) as conn:
        conn.execute(f"INSERT INTO attempts ({cols}) VALUES ({marks})", tuple(values.values()))
        conn.commit()


def already_submitted(candidate_id: str, job_key: str) -> bool:
    """True only if a real submission already went through for this pair."""
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT 1 FROM attempts WHERE candidate_id=? AND job_key=? AND status=? LIMIT 1",
            (candidate_id, job_key, SUBMITTED),
        ).fetchone()
    return row is not None


def attempt_count(candidate_id: str, job_key: str) -> int:
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM attempts WHERE candidate_id=? AND job_key=?",
            (candidate_id, job_key),
        ).fetchone()
    return int(row["n"]) if row else 0


def submitted_keys(candidate_id: str) -> set[str]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT DISTINCT job_key FROM attempts WHERE candidate_id=? AND status=?",
            (candidate_id, SUBMITTED),
        ).fetchall()
    return {r["job_key"] for r in rows}


def seen_keys(candidate_id: str, statuses: Iterable[str] | None = None) -> set[str]:
    """Job keys already attempted; optionally restrict to given statuses."""
    query = "SELECT DISTINCT job_key FROM attempts WHERE candidate_id=?"
    params: list[object] = [candidate_id]
    status_list = list(statuses) if statuses else []
    if status_list:
        marks = ",".join("?" for _ in status_list)
        query += f" AND status IN ({marks})"
        params.extend(status_list)
    with closing(_connect()) as conn:
        rows = conn.execute(query, params).fetchall()
    return {r["job_key"] for r in rows}


def company_submitted_count(candidate_id: str, company_token: str) -> int:
    """How many submissions already landed at one company (for per-company caps)."""
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM attempts "
            "WHERE candidate_id=? AND status=? AND job_key LIKE ?",
            (candidate_id, SUBMITTED, f"%:{company_token}:%"),
        ).fetchone()
    return int(row["n"]) if row else 0


def company_attempt_count(candidate_id: str, company_token: str) -> int:
    """Applications that actually landed at one company (submitted or the site
    said we already applied). Failed forms do NOT burn the company's slot, so a
    blocked attempt lets the bot try that company's next matching role.
    """
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM attempts "
            "WHERE candidate_id=? AND status IN (?,?) AND job_key LIKE ?",
            (candidate_id, SUBMITTED, ALREADY_APPLIED, f"%:{company_token}:%"),
        ).fetchone()
    return int(row["n"]) if row else 0


def total_attempts() -> int:
    with closing(_connect()) as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM attempts").fetchone()
    return int(row["n"]) if row else 0


def stats() -> dict[str, int]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM attempts GROUP BY status"
        ).fetchall()
    return {r["status"]: int(r["n"]) for r in rows}


def all_results_rows() -> list[sqlite3.Row]:
    with closing(_connect()) as conn:
        return conn.execute(
            "SELECT * FROM attempts ORDER BY id DESC"
        ).fetchall()



def _get(row: sqlite3.Row, key: str, default=""):
    try:
        val = row[key]
    except (IndexError, KeyError):
        return default
    return default if val is None else val


def _repair_reasons(text: str) -> str:
    """Early rows were saved as '; '.join(<string>) -> 'S; c; o; r; e'. Undo that."""
    bits = text.split("; ")
    if len(bits) > 3 and all(len(b) <= 1 for b in bits):
        return "".join(b if b else " " for b in bits)
    return text


def history_results(candidate_ids: Iterable[str] | None = None) -> list[ApplicationResult]:
    """Every attempt ever recorded, oldest first, rebuilt as ApplicationResult.

    Reports and the sheet are generated from this so they always show the full
    history across all runs, not just the latest one.
    """
    init()
    with closing(_connect()) as conn:
        rows = conn.execute("SELECT * FROM attempts ORDER BY id ASC").fetchall()
    wanted = set(candidate_ids) if candidate_ids else None
    out: list[ApplicationResult] = []
    for r in rows:
        if wanted and r["candidate_id"] not in wanted:
            continue
        key = str(_get(r, "job_key"))
        parts = key.split(":", 2)
        source, token, job_id = (parts + ["", "", ""])[:3]
        apply_url = str(_get(r, "apply_url"))
        job = Job(
            source=str(_get(r, "source")) or source,
            company=str(_get(r, "company")),
            company_token=token,
            job_id=job_id,
            title=str(_get(r, "title")),
            location=str(_get(r, "job_location")),
            url=str(_get(r, "job_url")) or apply_url,
            apply_url=apply_url,
            company_is_ai=bool(_get(r, "company_is_ai", 0)),
        )
        skipped = str(_get(r, "fields_skipped"))
        out.append(ApplicationResult(
            candidate_id=r["candidate_id"],
            candidate_name=str(_get(r, "candidate_name")),
            job=job,
            status=r["status"],
            detail=str(_get(r, "detail")),
            fields_filled=int(_get(r, "fields_filled", 0) or 0),
            fields_skipped=[x for x in skipped.split(" | ") if x] if skipped else [],
            resume_attached=bool(_get(r, "resume_attached", 0)),
            screenshot=str(_get(r, "screenshot")),
            confirmation_text=str(_get(r, "confirmation_text")),
            score=float(_get(r, "score", 0.0) or 0.0),
            applied_at=str(_get(r, "applied_at")),
            duration_s=float(_get(r, "duration_s", 0.0) or 0.0),
            email=str(_get(r, "email_used")),
            phone=str(_get(r, "phone_used")),
            candidate_location=str(_get(r, "candidate_location")),
            linkedin=str(_get(r, "linkedin")),
            github=str(_get(r, "github")),
            portfolio=str(_get(r, "portfolio")),
            resume_file=str(_get(r, "resume_file")),
            match_reasons=_repair_reasons(str(_get(r, "match_reasons"))),
        ))
    return out
