"""Evening summary pushed to the phone via ntfy.

Reads today's attempts from data/applications.db and posts a short per-
candidate digest (submitted companies, errors, questions still waiting for a
human answer) to the ntfy topic `<JOBBOT_NTFY_TOPIC>-summary`.  Subscribe to
that topic in the ntfy phone app once and the digest arrives every evening.

Run by the com.jobbot.summary LaunchAgent at 20:30 local time, or by hand:
    .venv/bin/python scripts/evening_summary.py [--dry-run] [--hours 24]
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "applications.db"
ENV_FILE = ROOT / "web" / ".env.local"


def _env(name: str) -> str:
    if os.environ.get(name):
        return os.environ[name]
    try:
        for line in ENV_FILE.read_text().splitlines():
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def topic() -> str:
    base = _env("JOBBOT_NTFY_TOPIC") or "jobbot-link-04021e99"
    return f"{base}-summary"


def build(hours: int = 24) -> tuple[str, str]:
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT candidate_id, candidate_name, company, title, status, detail, fields_skipped "
        "FROM attempts WHERE applied_at >= ? ORDER BY applied_at", (since,)
    ).fetchall()
    totals = {r[0]: r[1] for r in conn.execute(
        "SELECT candidate_id, COUNT(*) FROM attempts WHERE status='SUBMITTED' GROUP BY candidate_id")}
    conn.close()

    by_cand: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        by_cand.setdefault(r["candidate_id"], []).append(r)

    lines: list[str] = []
    grand = 0
    for cid in sorted(set(by_cand) | set(totals)):
        rs = by_cand.get(cid, [])
        name = (rs[0]["candidate_name"] if rs else cid).split()[0]
        sub = [r for r in rs if r["status"] == "SUBMITTED"]
        errs = [r for r in rs if r["status"] in ("ERROR", "FAILED_VALIDATION", "CAPTCHA_BLOCKED")]
        grand += len(sub)
        lines.append(f"{name}: {len(sub)} applied today ({totals.get(cid, 0)} total), {len(errs)} failed")
        for r in sub[:12]:
            lines.append(f"  + {r['company']} - {r['title']}"[:110])
        if len(sub) > 12:
            lines.append(f"  ... and {len(sub) - 12} more")
        pending = []
        for r in errs:
            skipped = r["fields_skipped"] or ""
            if "no grounded answer" in skipped or "needs answer" in skipped.lower():
                pending.append(r["company"])
        if pending:
            lines.append(f"  ? questions waiting for you at: {', '.join(sorted(set(pending))[:8])}")
    if not lines:
        lines.append("No activity in the last 24 hours - is the bot running?")
    title = f"JobBot: {grand} applications in the last {hours}h"
    return title, "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--hours", type=int, default=24)
    args = ap.parse_args()
    title, body = build(args.hours)
    if args.dry_run:
        print(f"[{topic()}] {title}\n{body}")
        return 0
    try:
        r = httpx.post(f"https://ntfy.sh/{topic()}", content=body.encode(),
                       headers={"Title": title, "Tags": "briefcase"}, timeout=20)
        print(f"posted to {topic()}: {r.status_code}")
        return 0 if r.is_success else 1
    except Exception as e:  # noqa: BLE001 - one-shot job, just report
        print(f"post failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
