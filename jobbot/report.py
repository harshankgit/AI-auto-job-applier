"""CSV + Markdown reports. Every contact detail used is written down.

The user asked explicitly: record when each application went out, which phone
and email were used, and all the company details - per candidate.
"""

from __future__ import annotations

import csv
import os
from collections import Counter, defaultdict

from .config import REPORTS_DIR
from .models import SHEET_HEADERS, ApplicationResult, utcnow

CSV_PATH = os.path.join(REPORTS_DIR, "applications.csv")
MD_PATH = os.path.join(REPORTS_DIR, "APPLICATIONS.md")

STATUS_ICON = {
    "SUBMITTED": "[APPLIED]",
    "CAPTCHA_BLOCKED": "[CAPTCHA]",
    "FAILED_VALIDATION": "[INCOMPLETE]",
    "LOGIN_REQUIRED": "[LOGIN]",
    "ALREADY_APPLIED": "[DUPLICATE]",
    "CLOSED": "[CLOSED]",
    "ERROR": "[ERROR]",
    "SKIPPED": "[DRY RUN]",
}


def write_csv(results: list[ApplicationResult], path: str = CSV_PATH) -> str:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(SHEET_HEADERS)
        for r in results:
            w.writerow(r.row())
    return path


def _esc(text: str) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ").strip()


DONE_STATUSES = ("SUBMITTED", "ALREADY_APPLIED")

MEANINGS = {
    "SUBMITTED": "Application sent and confirmed by the company's site",
    "CAPTCHA_BLOCKED": "Form was filled but a captcha needs a human to clear it",
    "FAILED_VALIDATION": "Form asked something we had no resume fact for - not submitted",
    "LOGIN_REQUIRED": "Site demanded an account login",
    "ALREADY_APPLIED": "Company site says this candidate already applied",
    "CLOSED": "Posting was closed by the time we opened it",
    "ERROR": "Technical failure - not submitted",
    "SKIPPED": "Dry run - filled but deliberately not submitted",
}


def _backfill(results: list[ApplicationResult], identities: dict) -> None:
    """Old DB rows predate some contact columns; fill them from the profile."""
    for r in results:
        ident = identities.get(r.candidate_id)
        if not ident:
            continue
        r.candidate_name = r.candidate_name or ident.full_name
        r.email = r.email or ident.email
        r.phone = r.phone or ident.phone_display
        r.candidate_location = r.candidate_location or ident.location
        r.linkedin = r.linkedin or (ident.linkedin or "")
        r.github = r.github or (ident.github or "")
        r.portfolio = r.portfolio or (ident.portfolio or "")
        r.resume_file = r.resume_file or ident.resume


def done_companies(rows: list[ApplicationResult]) -> list[ApplicationResult]:
    """First confirmed application per company (one row per company)."""
    seen: dict[str, ApplicationResult] = {}
    for r in rows:
        if r.status in DONE_STATUSES and r.job.company not in seen:
            seen[r.job.company] = r
    return list(seen.values())


def _link(url: str) -> str:
    return f"[open]({url})" if url else "-"


def write_markdown(
    results: list[ApplicationResult],
    identities: dict,
    discovery: dict | None = None,
    path: str = MD_PATH,
) -> str:
    """Human-readable record of everything that ever happened, per candidate.

    `results` should be the FULL history (store.history_results()), so the
    report keeps growing across runs instead of being overwritten.
    """
    _backfill(results, identities)
    by_cand: dict[str, list[ApplicationResult]] = defaultdict(list)
    for r in results:
        by_cand[r.candidate_id].append(r)
    for cid in identities:
        by_cand.setdefault(cid, [])

    lines: list[str] = []
    add = lines.append

    add("# AI Job Application Report")
    add("")
    add(f"**Report updated (UTC):** {utcnow()}")
    if results:
        stamps = sorted(r.applied_at for r in results if r.applied_at)
        if stamps:
            add(f"**History covered (UTC):** {stamps[0]} -> {stamps[-1]}")
    all_done = [d for rows in by_cand.values() for d in done_companies(rows)]
    add(f"**Companies applied to (all candidates):** {len(all_done)}")
    add(f"**Applications submitted (page-confirmed):** {sum(1 for r in results if r.submitted)}")
    add(f"**Total attempts logged:** {len(results)}")
    add("")
    add("> Only applications the job site itself confirmed are counted as done.")
    add("> Every form value came from the candidate's own resume or their")
    add("> pre-approved answers. Nothing was invented.")
    add("")

    # ---------- scoreboard ----------
    add("## Scoreboard - how many companies are done")
    add("")
    add("| Candidate | Companies done | Applications submitted | Total attempts | Not gone through | Dry runs |")
    add("| --- | --- | --- | --- | --- | --- |")
    for cid, rows in by_cand.items():
        ident = identities.get(cid)
        name = ident.full_name if ident else (rows[0].candidate_name if rows else cid)
        sub = sum(1 for r in rows if r.submitted)
        dry = sum(1 for r in rows if r.status == "SKIPPED")
        bad = sum(1 for r in rows if r.status not in DONE_STATUSES and r.status != "SKIPPED")
        add(f"| **{_esc(name)}** | **{len(done_companies(rows))}** | {sub} | {len(rows)} | {bad} | {dry} |")
    add("")

    # ---------- master list ----------
    add("## Every company applied to - full details")
    add("")
    if all_done:
        add("| # | Candidate | Company | Job Title | Job Location | Company Type | Applied At (UTC) | Email Used | Phone Used | Candidate Location | LinkedIn | GitHub | Resume Sent | Fields Filled | Status | Job Link |")
        add("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for i, r in enumerate(sorted(all_done, key=lambda x: x.applied_at), 1):
            add(
                f"| {i} | {_esc(r.candidate_name)} | **{_esc(r.job.company)}** | {_esc(r.job.title)} | "
                f"{_esc(r.job.location) or '-'} | {'AI company' if r.job.company_is_ai else 'Tech'} | "
                f"{r.applied_at} | {_esc(r.email)} | {_esc(r.phone)} | {_esc(r.candidate_location) or '-'} | "
                f"{_esc(r.linkedin) or '-'} | {_esc(r.github) or '-'} | "
                f"`{os.path.basename(r.resume_file) or '-'}` ({'attached' if r.resume_attached else 'not confirmed'}) | "
                f"{r.fields_filled} | {STATUS_ICON.get(r.status, '')} {r.status} | {_link(r.job.apply_url)} |"
            )
    else:
        add("_No company has confirmed an application yet._")
    add("")

    if discovery:
        add("## Job discovery (latest run)")
        add("")
        add("| Metric | Value |")
        add("| --- | --- |")
        add(f"| Company boards queried | {discovery.get('boards_queried', 0)} |")
        add(f"| Raw postings fetched | {discovery.get('raw_jobs', 0)} |")
        add(f"| Real postings kept | {discovery.get('real_jobs', 0)} |")
        add(f"| Fake / demo / test postings rejected | {discovery.get('rejected_fake', 0)} |")
        reasons = discovery.get("rejected_reasons") or {}
        if reasons:
            detail = ", ".join(f"{k} ({v})" for k, v in sorted(reasons.items(), key=lambda kv: -kv[1])[:6])
            add(f"| Rejection reasons | {_esc(detail)} |")
        add("")

    overall = Counter(r.status for r in results)
    if overall:
        add("## Outcome summary (all runs)")
        add("")
        add("| Outcome | Count | Meaning |")
        add("| --- | --- | --- |")
        for status, count in overall.most_common():
            add(f"| {STATUS_ICON.get(status, '')} {status} | {count} | {MEANINGS.get(status, '')} |")
        add("")

    for cid, rows in by_cand.items():
        ident = identities.get(cid)
        add("---")
        add("")
        name = ident.full_name if ident else (rows[0].candidate_name if rows else cid)
        done = done_companies(rows)
        add(f"## {name} - {len(done)} compan{'y' if len(done) == 1 else 'ies'} done")
        add("")
        add("### Contact details used on every form")
        add("")
        add("| Field | Value used |")
        add("| --- | --- |")
        if ident:
            add(f"| Full name | {_esc(ident.full_name)} |")
            add(f"| Email | {_esc(ident.email)} |")
            add(f"| Phone | {_esc(ident.phone_display)} |")
            add(f"| Location | {_esc(ident.location)} |")
            add(f"| LinkedIn | {_esc(ident.linkedin) or '_not in resume - left blank_'} |")
            add(f"| GitHub | {_esc(ident.github) or '_not in resume - left blank_'} |")
            add(f"| Portfolio | {_esc(ident.portfolio) or '_not in resume - left blank_'} |")
            add(f"| Resume file sent | `{os.path.basename(ident.resume)}` |")
            add(f"| Current role | {_esc(ident.current_title)} at {_esc(ident.current_company)} |")
            add(f"| Experience | {ident.years_experience} year(s) |")
            add(f"| Education | {_esc(ident.degree)} {_esc(ident.discipline)}, {_esc(ident.school)} ({ident.grad_year}) |")
        elif rows:
            add(f"| Email | {_esc(rows[0].email)} |")
            add(f"| Phone | {_esc(rows[0].phone)} |")
        add("")

        sub = [r for r in rows if r.status in DONE_STATUSES]
        add(f"### Companies done: {len(done)} ({len(sub)} applications out of {len(rows)} attempts)")
        add("")
        if sub:
            add("| # | Applied At (UTC) | Company | Job Title | Job Location | Source | Company Type | Match | Resume | Fields Filled | Email Used | Phone Used | Status | Apply Link |")
            add("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
            for i, r in enumerate(sub, 1):
                add(
                    f"| {i} | {r.applied_at} | **{_esc(r.job.company)}** | {_esc(r.job.title)} | "
                    f"{_esc(r.job.location) or '-'} | {r.job.source} | "
                    f"{'AI company' if r.job.company_is_ai else 'Tech'} | {r.score:.0f} | "
                    f"{'attached' if r.resume_attached else 'not confirmed'} | {r.fields_filled} | "
                    f"{_esc(r.email)} | {_esc(r.phone)} | {r.status} | {_link(r.job.apply_url)} |"
                )
            add("")
            add("#### Why each of those jobs was chosen")
            add("")
            for i, r in enumerate(sub, 1):
                add(f"{i}. **{_esc(r.job.company)} - {_esc(r.job.title)}** (score {r.score:.0f}): {_esc(r.match_reasons)}")
            add("")
            confs = [r for r in sub if r.confirmation_text]
            if confs:
                add("#### Confirmation text shown by the company site")
                add("")
                for r in confs:
                    add(f"- **{_esc(r.job.company)}**: {_esc(r.confirmation_text)[:240]}")
                add("")
        else:
            add("_No application has been confirmed as submitted for this candidate yet._")
            add("")

        rest = [r for r in rows if r.status not in DONE_STATUSES]
        if rest:
            add(f"### Attempts that did not go through ({len(rest)})")
            add("")
            add("| Attempted At (UTC) | Company | Job Title | Job Location | Outcome | What happened | Fields Filled | Unanswered Fields | Email Used | Phone Used | Job Link | Screenshot |")
            add("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
            for r in rest:
                shot = os.path.basename(r.screenshot) if r.screenshot else "-"
                skipped = ", ".join(r.fields_skipped[:4]) or "-"
                add(
                    f"| {r.applied_at} | {_esc(r.job.company)} | {_esc(r.job.title)} | {_esc(r.job.location) or '-'} | "
                    f"{STATUS_ICON.get(r.status, '')} {r.status} | {_esc(r.detail)[:160]} | "
                    f"{r.fields_filled} | {_esc(skipped)} | {_esc(r.email)} | {_esc(r.phone)} | "
                    f"{_link(r.job.apply_url)} | {shot} |"
                )
            add("")

        if rows:
            companies = Counter(r.job.company for r in rows)
            add("### Companies touched for this candidate")
            add("")
            add("| Company | Source | AI company | Attempts | Submitted | Result |")
            add("| --- | --- | --- | --- | --- | --- |")
            ref: dict[str, ApplicationResult] = {}
            for r in rows:
                ref.setdefault(r.job.company, r)
            done_names = {d.job.company for d in done}
            for comp, count in companies.most_common():
                ok = sum(1 for r in rows if r.job.company == comp and r.submitted)
                add(
                    f"| {_esc(comp)} | {ref[comp].job.source} | {'yes' if ref[comp].job.company_is_ai else 'no'} | "
                    f"{count} | {ok} | {'DONE' if comp in done_names else 'not yet'} |"
                )
            add("")

    add("---")
    add("")
    add("Screenshots for every attempt are in `data/screenshots/`.")
    add("The full audit trail is in `data/applications.db` and `reports/applications.csv`.")
    add("")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return path


def console_summary(results: list[ApplicationResult]) -> str:
    """Short plain-text recap for the terminal."""
    if not results:
        return "No attempts were made."
    out = []
    by_cand: dict[str, list[ApplicationResult]] = defaultdict(list)
    for r in results:
        by_cand[r.candidate_name].append(r)
    for name, rows in by_cand.items():
        sub = [r for r in rows if r.submitted]
        out.append(f"{name}: {len(sub)} submitted / {len(rows)} attempted")
        for r in sub:
            out.append(f"    APPLIED  {r.job.company} - {r.job.title} ({r.applied_at} UTC)")
        for status, count in Counter(r.status for r in rows if not r.submitted).most_common():
            out.append(f"    {status}: {count}")
    return "\n".join(out)
