"""MCP (Model Context Protocol) server for jobbot.

Lets an MCP client (Claude Desktop, Claude Code, Cursor, ...) watch and steer
the bot in plain language: "how many jobs did Divya get today?", "which
questions are blocking Harshank?", "answer the notice period question with
Immediate", "start the bot", "stop the bot".

Run:  .venv/bin/python -m jobbot.mcp_server      (stdio transport)
  or: .venv/bin/jobbot mcp

All tools are local: they read data/applications.db and the logs, and write only
the learned-answers file and the STOP marker. Nothing here submits a form.
"""
from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import time
from contextlib import closing
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from . import config, learn
from .config import DATA_DIR

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = DATA_DIR / "applications.db"
LOG_PATH = ROOT / "logs" / "realrun.log"
STOP_FILES = ("STOP", "STOP-harshank", "STOP-divya")
CANDIDATES = ("harshank", "divya")
BOT_CMD = [
    "-u", "-m", "jobbot.cli", "apply", "--candidate", "both", "--continuous", "--workers", "2",
    "--india-remote-only", "--min-score", "50", "--min-score-floor", "40",
    "--skip-ats", "greenhouse,lever",
]
STATUSES = ("SUBMITTED", "ALREADY_APPLIED", "FAILED_VALIDATION", "CAPTCHA_BLOCKED",
            "ERROR", "CLOSED", "SKIPPED")

mcp = FastMCP(
    "jobbot",
    instructions=(
        "Tools for the jobbot auto-apply bot (Ashby boards, two candidates: "
        "harshank and divya). Use application_stats / list_applications to report "
        "progress, pending_questions + teach_answer to unblock forms, and "
        "bot_status / start_bot / stop_bot to control the run."
    ),
)


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _cid(candidate: str) -> str:
    c = (candidate or "").strip().lower()
    if c not in CANDIDATES:
        raise ValueError(f"candidate must be one of {CANDIDATES}, got {candidate!r}")
    return c


def _bot_pids() -> list[int]:
    out = subprocess.run(["pgrep", "-f", r"jobbot\.cli (apply|run)"],
                         capture_output=True, text=True).stdout
    return [int(p) for p in out.split() if p.strip().isdigit()]


def _tail(path: Path, lines: int) -> list[str]:
    if not path.exists():
        return []
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - 200_000))
        text = fh.read().decode("utf-8", "replace")
    return text.splitlines()[-lines:]


# ---------------------------------------------------------------- read tools

@mcp.tool()
def application_stats(since: str = "") -> dict:
    """Counts of application attempts per candidate and status.

    since: optional date/time 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM' (UTC) to count
    only newer attempts. Also returns how many distinct companies each
    candidate was SUBMITTED to.
    """
    where, args = ("WHERE applied_at >= ?", [since]) if since else ("", [])
    out: dict = {"since": since or "all time", "candidates": {}}
    with closing(_db()) as conn:
        for r in conn.execute(
            f"SELECT candidate_id, status, COUNT(*) n FROM attempts {where} "
            "GROUP BY candidate_id, status", args):
            out["candidates"].setdefault(r["candidate_id"], {})[r["status"]] = r["n"]
        for cid in CANDIDATES:
            q = "SELECT COUNT(DISTINCT lower(company)) FROM attempts WHERE candidate_id=? AND status='SUBMITTED'"
            a = [cid]
            if since:
                q += " AND applied_at >= ?"
                a.append(since)
            out["candidates"].setdefault(cid, {})["submitted_companies"] = conn.execute(q, a).fetchone()[0]
    return out


@mcp.tool()
def list_applications(candidate: str = "", status: str = "", company: str = "",
                      limit: int = 25) -> list[dict]:
    """Most recent application attempts, newest first.

    candidate: 'harshank' / 'divya' / '' for both. status: e.g. SUBMITTED,
    FAILED_VALIDATION, CAPTCHA_BLOCKED, ERROR ('' = any). company: substring
    filter. limit: max rows (1-200).
    """
    sql = ("SELECT candidate_id, company, title, job_location, status, detail, score, "
           "applied_at, job_url, apply_url FROM attempts WHERE 1=1")
    args: list = []
    if candidate:
        sql += " AND candidate_id=?"
        args.append(_cid(candidate))
    if status:
        sql += " AND status=?"
        args.append(status.strip().upper())
    if company:
        sql += " AND lower(company) LIKE ?"
        args.append(f"%{company.strip().lower()}%")
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 200)))
    with closing(_db()) as conn:
        return [{
            "candidate": r["candidate_id"], "company": r["company"], "title": r["title"],
            "location": r["job_location"], "status": r["status"],
            "detail": (r["detail"] or "")[:300], "score": r["score"],
            "applied_at": r["applied_at"], "url": r["job_url"] or r["apply_url"],
        } for r in conn.execute(sql, args)]


@mcp.tool()
def failure_reasons(candidate: str) -> dict:
    """Why applications failed for one candidate, grouped into plain-English
    categories, plus the form controls the bot could not operate."""
    cid = _cid(candidate)
    c = learn.analyze((cid,))["candidates"].get(cid, {})
    return {
        "submitted": c.get("submitted"), "submitted_companies": c.get("submitted_companies"),
        "failed": c.get("failed"), "reasons": c.get("reasons"),
        "form_control_issues": c.get("form_control_issues", [])[:20],
    }


@mcp.tool()
def pending_questions(candidate: str, include_answered: bool = False, limit: int = 30) -> list[dict]:
    """Required form questions that blocked applications for a candidate, most
    frequent first. Answer them with teach_answer so the bot never gets stuck on
    them again."""
    cid = _cid(candidate)
    qs = learn.analyze((cid,))["candidates"].get(cid, {}).get("questions", [])
    if not include_answered:
        qs = [q for q in qs if not q.get("learned")]
    return [{"question": q["question"], "count": q["count"], "companies": q["companies"][:5],
             "already_answered": q.get("learned", False),
             "maybe_not_eligible": q.get("maybe_not_eligible", False)}
            for q in qs[: max(1, min(int(limit), 150))]]


@mcp.tool()
def learned_answers(candidate: str = "") -> dict:
    """Answers the user already taught the bot (per candidate, plus 'both')."""
    data = learn.load()
    scopes = [_cid(candidate), "both"] if candidate else list(data)
    return {s: {v.get("question", k): v.get("answer") for k, v in (data.get(s) or {}).items()}
            for s in scopes}


@mcp.tool()
def candidate_profile(candidate: str) -> dict:
    """Resume facts the bot types into forms for a candidate (config/profiles.yaml)."""
    return config.get_candidate(_cid(candidate))


@mcp.tool()
def bot_status(log_lines: int = 15) -> dict:
    """Is the bot running, was Stop pressed, and the last lines of its log."""
    pids = _bot_pids()
    stops = [f for f in STOP_FILES if (DATA_DIR / f).exists()]
    age = int(time.time() - LOG_PATH.stat().st_mtime) if LOG_PATH.exists() else None
    return {
        "running": bool(pids), "pids": pids, "stop_markers": stops,
        "log_last_write_seconds_ago": age,
        "log_tail": _tail(LOG_PATH, max(1, min(int(log_lines), 200))),
    }


# ---------------------------------------------------------------- write tools

@mcp.tool()
def teach_answer(candidate: str, question: str, answer: str) -> dict:
    """Save the answer to a form question for a candidate ('harshank', 'divya'
    or 'both'). The running bot picks it up on the next form - no restart needed."""
    scope = (candidate or "").strip().lower()
    if scope != "both":
        scope = _cid(scope)
    if not question.strip() or not answer.strip():
        raise ValueError("question and answer must both be non-empty")
    learn.teach(scope, question.strip(), answer.strip())
    return {"saved": True, "candidate": scope, "question": question.strip(),
            "answer": answer.strip(), "key": learn.norm(question)}


@mcp.tool()
def start_bot() -> dict:
    """Start the continuous apply run (both candidates, Ashby only, 2 workers
    each). Clears Stop markers. Does nothing if it is already running."""
    pids = _bot_pids()
    if pids:
        return {"started": False, "already_running": pids}
    for f in STOP_FILES:
        (DATA_DIR / f).unlink(missing_ok=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    py = ROOT / ".venv" / "bin" / "python"
    with LOG_PATH.open("a") as log:
        proc = subprocess.Popen([str(py), *BOT_CMD], cwd=ROOT, stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True)
    return {"started": True, "pid": proc.pid, "log": str(LOG_PATH)}


@mcp.tool()
def stop_bot() -> dict:
    """Stop the apply run gracefully (writes data/STOP, then SIGTERM). The
    watchdog will not restart it until start_bot or the next morning start."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "STOP").write_text(time.strftime("%Y-%m-%dT%H:%M:%S"))
    pids = _bot_pids()
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    return {"stopped": pids}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
