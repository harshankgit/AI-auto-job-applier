"""Learn from failed applications.

* `analyze()` reads every attempt, explains each failure in plain words and
  collects the required questions the bot could not answer. It writes
  reports/failures.json (read by the dashboard) and reports/FAILURES.md.
* The dashboard lets the user answer those questions once; the answers are
  stored in data/learned_answers.json and `lookup()` hands them to the form
  filler on the next attempt, so the same question never blocks again.

Nothing here invents an answer: a question is only answered automatically
after the user typed the answer for that candidate.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path

from .config import DATA_DIR, REPORTS_DIR

DB_PATH = DATA_DIR / "applications.db"
LEARNED_PATH = DATA_DIR / "learned_answers.json"
FAILURES_JSON = REPORTS_DIR / "failures.json"
FAILURES_MD = REPORTS_DIR / "FAILURES.md"

_cache: dict = {"mtime": None, "data": {}}


def norm(label: str) -> str:
    low = (label or "").lower().replace("\u2731", " ").replace("*", " ")
    low = re.sub(r"[^a-z0-9]+", " ", low)
    return re.sub(r"\s+", " ", low).strip()


def load() -> dict:
    try:
        mtime = LEARNED_PATH.stat().st_mtime
    except OSError:
        _cache.update(mtime=None, data={})
        return {}
    if _cache["mtime"] != mtime:
        try:
            _cache.update(mtime=mtime, data=json.loads(LEARNED_PATH.read_text()))
        except Exception:
            _cache.update(mtime=mtime, data={})
    return _cache["data"]


def mtime() -> float:
    try:
        return LEARNED_PATH.stat().st_mtime
    except OSError:
        return 0.0


def lookup(candidate_id: str, label: str) -> str | None:
    key = norm(label)
    if not key:
        return None
    data = load()
    for scope in (candidate_id, "both"):
        hit = (data.get(scope) or {}).get(key)
        if hit and str(hit.get("answer", "")).strip():
            return str(hit["answer"]).strip()
    return None


def teach(candidate_id: str, question: str, answer: str) -> None:
    data = dict(load())
    data.setdefault(candidate_id, {})[norm(question)] = {
        "question": question, "answer": answer,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    LEARNED_PATH.parent.mkdir(parents=True, exist_ok=True)
    LEARNED_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------- analysis

_SKIP_RE = re.compile(r"^(?P<label>.*?) \((?P<reason>[^()]*(?:\([^()]*\)[^()]*)*)\)$")

_NOT_ELIGIBLE = ("years", "yrs", "authorized", "authorised", "eligible to work",
                 "legally", "sponsor", "visa", "citizen", "right to work")


def _why(status: str, detail: str) -> tuple[str, str]:
    d = (detail or "").lower()
    if status == "CAPTCHA_BLOCKED":
        return "captcha", "The site showed a captcha (robot check) that only a human can solve."
    if status == "CLOSED":
        return "closed", "The job was already closed when the bot opened it."
    if status == "ERROR":
        if "never confirmed" in d:
            return "unconfirmed", ("Submit was clicked but the site never showed a confirmation "
                                   "(often an email security code / OTP step).")
        return "error", "The page crashed or timed out: " + (detail or "")[:140]
    if "required field" in d or "form rejected" in d:
        return "form_rejected", "The form refused the submission because a required field was still empty."
    if "resume" in d and "upload" in d:
        return "resume", "The resume upload did not stick."
    if "no resume fact" in d or "confirmed answer" in d:
        return "needs_answer", "Required questions that are not in the resume - answer them below once."
    return "other", (detail or "")[:160]


def _rows(conn, cid: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT company, title, job_location, status, detail, fields_skipped, applied_at, "
        "apply_url, job_url, source, score FROM attempts WHERE candidate_id=? ORDER BY id DESC",
        (cid,),
    ).fetchall()


def analyze(candidates=("harshank", "divya")) -> dict:
    out: dict = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "candidates": {}}
    if not DB_PATH.exists():
        return out
    learned = load()
    with closing(sqlite3.connect(DB_PATH, timeout=30)) as conn:
        conn.row_factory = sqlite3.Row
        for cid in candidates:
            rows = _rows(conn, cid)
            ok_cos = {(r["company"] or "").strip().lower() for r in rows if r["status"] == "SUBMITTED"}
            successes, failures = [], []
            reasons = Counter()
            questions: dict[str, dict] = {}
            control: Counter = Counter()
            for r in rows:
                base = {
                    "company": r["company"], "title": r["title"], "location": r["job_location"],
                    "status": r["status"], "applied_at": r["applied_at"],
                    "url": r["job_url"] or r["apply_url"], "source": r["source"], "score": r["score"],
                }
                if r["status"] in ("SUBMITTED", "ALREADY_APPLIED"):
                    successes.append(base)
                    continue
                if r["status"] == "SKIPPED":
                    continue
                cat, why = _why(r["status"], r["detail"] or "")
                reasons[cat] += 1
                blocking = []
                detail = r["detail"] or ""
                for part in (r["fields_skipped"] or "").split(" | "):
                    m = _SKIP_RE.match(part.strip())
                    if not m:
                        continue
                    label, reason = m.group("label").strip(), m.group("reason")
                    if not label or label not in detail:
                        continue
                    if "could not set" in reason:
                        control[label] += 1
                        blocking.append(label + " (bot could not click/type it)")
                        continue
                    blocking.append(label)
                    key = norm(label)
                    if not key or len(key) < 3:
                        continue
                    q = questions.setdefault(key, {
                        "question": label, "count": 0, "companies": [],
                        "learned": bool((learned.get(cid) or {}).get(key, {}).get("answer")
                        or (learned.get("both") or {}).get(key, {}).get("answer")),
                        "maybe_not_eligible": any(w in key for w in _NOT_ELIGIBLE),
                    })
                    q["count"] += 1
                    if r["company"] and r["company"] not in q["companies"]:
                        q["companies"].append(r["company"])
                failures.append({**base, "category": cat, "why": why, "blocking": blocking[:8],
                                 "company_later_succeeded": (r["company"] or "").strip().lower() in ok_cos})
            pending = sorted(questions.values(), key=lambda q: (-q["count"], q["question"]))
            out["candidates"][cid] = {
                "submitted": len([s for s in successes if s["status"] == "SUBMITTED"]),
                "submitted_companies": len(ok_cos - {""}),
                "failed": len(failures),
                "reasons": dict(reasons.most_common()),
                "successes": successes[:400],
                "failures": failures[:400],
                "questions": pending[:150],
                "form_control_issues": [{"question": k, "count": v} for k, v in control.most_common(40)],
            }
    return out


_LABELS = {
    "needs_answer": "Needs an answer from you", "captcha": "Captcha", "form_rejected": "Form rejected",
    "unconfirmed": "No confirmation / OTP", "error": "Page error", "closed": "Job closed",
    "resume": "Resume upload", "other": "Other",
}


def write_reports() -> dict:
    data = analyze()
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    FAILURES_JSON.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    lines = ["# Why applications failed", "", f"_Generated {data['generated_at']}_", ""]
    for cid, c in data["candidates"].items():
        lines += [f"## {cid.title()}", "",
                  f"Submitted: **{c['submitted']}** ({c['submitted_companies']} companies) | "
                  f"Failed: **{c['failed']}**", "", "| Reason | Count |", "|---|---|"]
        lines += [f"| {_LABELS.get(k, k)} | {v} |" for k, v in c["reasons"].items()]
        lines += ["", "### Questions waiting for your answer", ""]
        for q in c["questions"][:40]:
            tag = " -> answered (saved privately)" if q["learned"] else ""
            lines.append(f"- ({q['count']}x) {q['question']} - {', '.join(q['companies'][:4])}{tag}")
        lines.append("")
    FAILURES_MD.write_text("\n".join(lines))
    return data


def learned_companies(candidate_id: str) -> set[str]:
    """Companies whose blocking questions now all have a learned answer."""
    data = load()
    keys = set((data.get(candidate_id) or {}).keys()) | set((data.get("both") or {}).keys())
    if not keys or not DB_PATH.exists():
        return set()
    out: set[str] = set()
    with closing(sqlite3.connect(DB_PATH, timeout=30)) as conn:
        rows = conn.execute(
            "SELECT company, detail, fields_skipped FROM attempts WHERE candidate_id=? "
            "AND status='FAILED_VALIDATION'", (candidate_id,)).fetchall()
    per: dict[str, list[bool]] = defaultdict(list)
    for company, detail, skipped in rows:
        for part in (skipped or "").split(" | "):
            m = _SKIP_RE.match(part.strip())
            if m and m.group("label").strip() in (detail or "") and "could not set" not in m.group("reason"):
                per[(company or "").strip().lower()].append(norm(m.group("label")) in keys)
    for co, flags in per.items():
        if flags and all(flags):
            out.add(co)
    return out


if __name__ == "__main__":
    d = write_reports()
    for cid, c in d["candidates"].items():
        print(cid, c["submitted"], c["failed"], c["reasons"], len(c["questions"]))
