"""Command line entry point.

  uv run python -m jobbot.cli discover
  uv run python -m jobbot.cli match --candidate harshank --top 25
  uv run python -m jobbot.cli run --dry-run --limit 3
  uv run python -m jobbot.cli run --limit 10 --per-company 2
  uv run python -m jobbot.cli report
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
import zlib

from . import applier, browser, matcher, report, sheets, sources, store
from .config import DATA_DIR, REPORTS_DIR, SHOTS_DIR, get_candidate, load_answers, load_profiles
from .identity import load_identity

ALL_CANDIDATES = ("harshank", "divya")
NCR_WORDS = ("gurugram", "gurgaon", "delhi", "noida", "ncr", "faridabad", "ghaziabad")


def log(msg: str = "") -> None:
    print(msg, flush=True)


def _identities(which: str) -> dict:
    ids = {}
    for cid in (ALL_CANDIDATES if which == "both" else (which,)):
        ids[cid] = load_identity(get_candidate(cid))
    return ids


def _discover(args) -> tuple[list, dict]:
    log("Searching company job boards for real openings...")
    jobs, stats = sources.discover(include_aggregators=not args.no_aggregators, progress=None)
    skip_ats = {s.strip().lower() for s in (getattr(args, "skip_ats", "") or "").split(",") if s.strip()}
    if skip_ats:
        before = len(jobs)
        jobs = [j for j in jobs if (j.source or "").lower() not in skip_ats]
        log(f"  skipping ATS {sorted(skip_ats)}: dropped {before - len(jobs)} postings")
    log(f"  boards queried      : {stats['boards_queried']}")
    log(f"  raw postings        : {stats['raw_jobs']}")
    log(f"  real postings kept  : {stats['real_jobs']}")
    log(f"  fake/test rejected  : {stats['rejected_fake']}")
    if stats["failures"]:
        log(f"  boards that failed  : {len(stats['failures'])}")
    log(f"  postings at AI companies: {sum(1 for j in jobs if j.company_is_ai)}")
    log("")
    return jobs, stats


def cmd_discover(args) -> int:
    jobs, stats = _discover(args)
    top = sorted(stats["per_company"].items(), key=lambda kv: -kv[1])[:15]
    log("Biggest boards:")
    for name, count in top:
        log(f"  {count:>5}  {name}")
    return 0


def cmd_match(args) -> int:
    jobs, _ = _discover(args)
    for cid, ident in _identities(args.candidate).items():
        cand = get_candidate(cid)
        ranked = matcher.rank(
            jobs, cand,
            min_score=args.min_score,
            require_ai=args.ai_only,
            india_or_remote_only=args.india_remote_only,
        )
        summary = matcher.summarize(ranked)
        log(f"=== {ident.full_name} ===")
        log(f"  matches {summary['total']} across {summary['companies']} companies "
            f"| AI {summary['ai_jobs']} | India/remote {summary['india_or_remote']} | top {summary['top_score']}")
        for m in ranked[: args.top]:
            flag = "AI" if m.job.company_is_ai else "  "
            log(f"  {m.score:5.1f} {flag} {m.job.company:<22} {m.job.title[:52]:<52} {m.job.location[:26]}")
        log("")
    return 0


def cmd_apply(args) -> int:
    store.init()
    jobs, stats = _discover(args)
    identities = _identities(args.candidate)
    answers = load_answers()
    results = []

    mode = "DRY RUN (fills the form, does NOT submit)" if args.dry_run else "LIVE (fills AND submits)"
    log(f"Mode: {mode}")
    show = getattr(args, "show", False)
    headless = not show
    if headless:
        where = "real Chrome, HEADLESS (no window, never touches your screen)"
    else:
        where = "real Chrome, visible on screen (--show)"
    log(f"Browser: {where}")
    log("")

    report_lock = threading.Lock()
    _learn_state: dict = {}

    def _write_reports(push_sheet: bool):
        hist = store.history_results()
        report.write_csv(hist)
        report.write_markdown(hist, _identities("both"), stats)
        if push_sheet:
            sheets.try_push(hist, _identities("both"))
        now = time.time()
        if now - _learn_state.get("at", 0) > 120:
            _learn_state["at"] = now
            try:
                from . import learn as _learn
                _learn.write_reports()
            except Exception as exc:
                log(f"[learn] could not refresh failure report: {exc}")

    def _run_candidate(cid, ident):
        cand = get_candidate(cid)
        ranked = matcher.rank(
            jobs, cand,
            min_score=args.min_score,
            require_ai=args.ai_only,
            india_or_remote_only=args.india_remote_only,
        )
        log(f"=== {ident.full_name}: {len(ranked)} eligible jobs, applying to at most {args.limit} ===")
        keep_going = bool(getattr(args, "continuous", False)) or int(getattr(args, "target_submitted", 0) or 0) > 0
        if not ranked and not keep_going:
            log(f"  [{cid}] nothing cleared the score threshold")
            return
        if not ranked:
            log(f"  [{cid}] 0 eligible jobs right now (network down or boards empty) - "
                "continuous mode keeps retrying instead of exiting")

        def _flush(result):
            # Rewrite from the full DB history so reports always show every
            # company ever applied to, not just this run.
            with report_lock:
                results.append(result)
                try:
                    _write_reports(not args.no_sheet)
                except Exception as exc:  # reporting must never stop applying
                    log(f"    [{cid}] report write failed: {exc}")
            log(f"    [{cid}] logged -> markdown + sheet ({result.job.company} / {result.status})")

        target = int(getattr(args, "target_submitted", 0) or 0)
        continuous = bool(getattr(args, "continuous", False))
        if target > 0 or continuous:
            nworkers = max(1, int(getattr(args, "workers", 1) or 1))
            base = len(store.submitted_companies(cid))
            goal = base + (target or 200)
            log(f"=== {ident.full_name}: {'CONTINUOUS' if continuous else 'TARGET'} mode - {base} companies "
                f"already submitted, goal {goal}, {nworkers} parallel browser(s) ===")

            def _worker(k):
                prof = _profile_for(cid, k)
                while not _should_stop(cid, goal):
                    try:
                        with browser.BrowserSession(headless=headless, offscreen=not show, profile_dir=prof) as session:
                            _run_until_target(cid, ident, cand, session, goal, _flush, k, nworkers)
                    except Exception as exc:
                        log(f"[{cid}#{k}] browser crashed ({type(exc).__name__}: {exc}); restarting in 20s")
                        time.sleep(20)

            ws = [threading.Thread(target=_worker, args=(k,), name=f"{cid}-w{k}") for k in range(nworkers)]
            for t in ws:
                t.start()
                time.sleep(2)
            for t in ws:
                t.join()
            log(f"=== {ident.full_name}: stopped - {len(store.submitted_companies(cid))} companies submitted ===")
            return
        profile = None if cid == ALL_CANDIDATES[0] else DATA_DIR / f"chrome-profile-{cid}"
        with browser.BrowserSession(headless=headless, offscreen=not show, profile_dir=profile) as session:
            out = applier.run_batch(
                session, ranked, ident, answers,
                limit=args.limit, per_company=args.per_company,
                dry_run=args.dry_run, progress=lambda m, _c=cid: log(f"[{_c}] {m}"), on_result=_flush,
            )
        log(f"  {ident.full_name}: {len(out)} attempts recorded")
        log("")

    jobs_state = {"jobs": jobs, "at": time.time() if jobs else 0.0}
    jobs_lock = threading.Lock()

    def _fresh_jobs(max_age: float):
        if time.time() - jobs_state["at"] <= max_age and jobs_state["jobs"]:
            return jobs_state["jobs"]
        # only ONE thread re-discovers; the others keep applying from the current
        # list instead of queueing behind the (CPU-heavy) discovery
        if not jobs_lock.acquire(blocking=not jobs_state["jobs"]):
            return jobs_state["jobs"]
        try:
            if time.time() - jobs_state["at"] > max_age or not jobs_state["jobs"]:
                log("[target] re-discovering job boards for new postings...")
                _maybe_probe_boards()
                try:
                    new_jobs, new_stats = _discover(args)
                    if new_jobs or not jobs_state["jobs"]:
                        jobs_state["jobs"] = new_jobs
                    else:
                        log("[target] discovery returned 0 postings (network?) - keeping the previous job list")
                    # an empty result is usually a network blip: try again in ~2 min, not 30
                    jobs_state["at"] = time.time() if new_jobs else time.time() - max(0.0, max_age - 120)
                    stats.update(new_stats)
                except Exception as exc:
                    jobs_state["at"] = time.time() - max(0.0, max_age - 120)
                    log(f"[target] re-discovery failed: {exc}")
            return jobs_state["jobs"]
        finally:
            jobs_lock.release()

    def _ordered(ranked, skip_companies):
        """One job per company first (best company first, Gurugram/NCR then India
        then remote), then second-best roles, and so on."""
        per: dict[str, list] = {}
        for m in ranked:
            co = (m.job.company or "").strip().lower()
            if not co or co in skip_companies:
                continue
            per.setdefault(co, []).append(m)

        def pri(m):
            loc = (m.job.location or "").lower()
            ncr = any(w in loc for w in NCR_WORDS)
            return (0 if ncr else 1 if m.geo == matcher.GEO_INDIA else 2, -m.score)

        for lst in per.values():
            lst.sort(key=pri)
        rounds: list = []
        depth = max((len(v) for v in per.values()), default=0)
        for i in range(depth):
            layer = [v[i] for v in per.values() if len(v) > i]
            layer.sort(key=pri)
            rounds.extend(layer)
        return rounds

    def _profile_for(cid, k):
        if k == 0:
            return None if cid == ALL_CANDIDATES[0] else DATA_DIR / f"chrome-profile-{cid}"
        return DATA_DIR / f"chrome-profile-{cid}-w{k}"

    def _should_stop(cid, goal):
        if (DATA_DIR / "STOP").exists() or (DATA_DIR / f"STOP-{cid}").exists():
            return True
        if getattr(args, "continuous", False):
            return False
        return len(store.submitted_companies(cid)) >= goal

    def _run_until_target(cid, ident, cand, session, goal, _flush, k=0, n=1):
        tag = cid if n == 1 else f"{cid}#{k}"
        min_score = float(args.min_score)
        floor = float(getattr(args, "min_score_floor", 40.0))
        idle_rounds = 0
        waited = False
        rnd = 0

        def reached():
            return _should_stop(cid, goal)

        while not reached():
            rnd += 1
            # widening the score reuses the same postings; only look for NEW postings
            # every 30 min, or 10 min after the pool was fully exhausted
            pool = _fresh_jobs(max_age=600 if waited else 1800)
            waited = False
            if not pool:
                log(f"[{tag}] no job postings reachable (internet down?) - retrying in 60s")
                for _ in range(6):
                    if reached():
                        break
                    time.sleep(10)
                continue
            ranked = matcher.rank(
                pool, cand, min_score=min_score, require_ai=args.ai_only,
                india_or_remote_only=args.india_remote_only,
            )
            done_cos = store.submitted_companies(cid)
            # form failures expire after 3 days: the filler keeps learning answers,
            # so a company that failed last week deserves a fresh try (captcha
            # blocks never expire)
            blocked = store.blocked_companies(
                cid, 2, time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - 3 * 86400)))
            try:
                from . import learn as _learn
                # taught companies get retried - but only until they fail twice
                # more since the last teaching (or in 24h), so one stubborn form
                # cannot eat the whole run in a retry loop
                since = max(time.time() - 86400, _learn.mtime())
                stubborn = store.recently_failed_companies(
                    cid, time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(since)), 2)
                blocked -= (_learn.learned_companies(cid) - stubborn)
            except Exception:
                pass
            ordered = _ordered(ranked, done_cos | blocked)
            if n > 1:
                ordered = [m for m in ordered
                           if zlib.crc32((m.job.company or "").strip().lower().encode()) % n == k]
            log(f"[{tag}] round {rnd}: {len(ordered)} candidate jobs at {len({m.job.company for m in ordered})} "
                f"new companies (min score {min_score:g}) - submitted {len(done_cos)}/{goal}")

            def _flush_progress(result, _f=_flush):
                _f(result)
                log(f"[{cid}] PROGRESS {len(store.submitted_companies(cid))}/{goal} companies submitted")

            out = applier.run_batch(
                session, ordered, ident, answers,
                limit=10 ** 6, per_company=1, max_tries_per_company=2,
                dry_run=args.dry_run, progress=lambda m, _c=tag: log(f"[{_c}] {m}"),
                on_result=_flush_progress, stop=reached,
            )
            if reached():
                break
            if out:
                idle_rounds = 0
                continue
            idle_rounds += 1
            if min_score > floor:
                min_score = max(floor, min_score - 5)
                log(f"[{tag}] pool exhausted - widening match to min score {min_score:g}")
                continue
            log(f"[{tag}] every matching job tried; waiting 10 min for new postings...")
            waited = True
            for _ in range(60):
                if reached():
                    break
                time.sleep(10)

    items = list(identities.items())
    if len(items) > 1 and not args.sequential:
        log(f"Running {len(items)} candidates in parallel (one Chrome each)")
        errors = []

        def _safe(cid, ident):
            try:
                _run_candidate(cid, ident)
            except Exception as exc:
                errors.append((cid, exc))
                log(f"[{cid}] crashed: {type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=_safe, args=it, name=f"apply-{it[0]}") for it in items]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    else:
        for cid, ident in items:
            _run_candidate(cid, ident)

    _finish(results, identities, stats, args)
    return 0


def _finish(results, identities, stats, args) -> None:
    hist = store.history_results()
    if not results:
        log("No new attempts this run.")
    if not hist:
        log("No attempts recorded, so no report was written.")
        return

    all_ids = _identities("both")
    csv_path = report.write_csv(hist)
    md_path = report.write_markdown(hist, all_ids, stats)
    if not results:
        results = hist

    log("=" * 70)
    log(report.console_summary(results))
    log("=" * 70)
    log(f"Markdown report : {md_path}")
    log(f"CSV             : {csv_path}")
    log(f"Screenshots     : {SHOTS_DIR}")
    log(f"Audit database  : {DATA_DIR}/applications.db")

    if args.no_sheet:
        log("Google Sheet    : skipped (--no-sheet)")
        return
    ok, msg = sheets.try_push(hist, all_ids)
    if ok:
        log(f"Google Sheet    : {msg}")
    else:
        log(f"Google Sheet    : NOT written -> {msg}")


def cmd_report(args) -> int:
    store.init()
    counts = store.stats()
    if not counts:
        log("No attempts recorded yet.")
        return 0
    log(f"Recorded attempts ({store.total_attempts()} total):")
    for status, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        log(f"  {status:<18} {count}")
    log("")

    hist = store.history_results()
    ids = _identities("both")
    md_path = report.write_markdown(hist, ids)
    csv_path = report.write_csv(hist)
    done = {(r.candidate_id, r.job.company) for r in hist if r.status in report.DONE_STATUSES}
    for cid in ALL_CANDIDATES:
        log(f"  {ids[cid].full_name:<24} companies done: {sum(1 for c, _ in done if c == cid)}")
    log(f"Markdown report : {md_path}")
    log(f"CSV             : {csv_path}")
    if not getattr(args, "no_sheet", False):
        ok, msg = sheets.try_push(hist, ids)
        log(f"Google Sheet    : {msg if ok else 'NOT written -> ' + msg}")
    return 0


def cmd_sheet(args) -> int:
    log("Checking Google Sheets access...")
    ok, msg = sheets.try_push([], {})
    log(msg)
    from .config import sheets_credentials_path

    path = sheets_credentials_path()
    log(f"credentials: {path or 'NOT FOUND'}")
    return 0 if path else 1


def cmd_profiles(args) -> int:
    for cid in ALL_CANDIDATES:
        ident = load_identity(get_candidate(cid))
        log(f"=== {ident.full_name} ({cid}) ===")
        for label, value in (
            ("email", ident.email), ("phone", ident.phone_display),
            ("location", ident.location), ("linkedin", ident.linkedin),
            ("github", ident.github), ("portfolio", ident.portfolio),
            ("resume", ident.resume), ("experience", f"{ident.years_experience} yr"),
            ("current", f"{ident.current_title} at {ident.current_company}"),
        ):
            log(f"  {label:<11}: {value or '(absent in resume - will be left blank)'}")
        log("")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jobbot", description="Find real AI-company jobs and apply with resume data only.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--candidate", default="both", choices=[*ALL_CANDIDATES, "both"])
        sp.add_argument("--min-score", type=float, default=62.0)
        sp.add_argument("--ai-only", action="store_true", help="only AI companies/roles")
        sp.add_argument("--india-remote-only", action="store_true", help="only India or remote postings")
        sp.add_argument("--no-aggregators", action="store_true")
        sp.add_argument("--skip-ats", default="", help="comma list of ATS to skip, e.g. greenhouse")
        return sp

    d = sub.add_parser("discover", help="search boards, show what is live")
    d.add_argument("--no-aggregators", action="store_true")
    d.set_defaults(func=cmd_discover)

    m = common(sub.add_parser("match", help="rank jobs per candidate"))
    m.add_argument("--top", type=int, default=20)
    m.set_defaults(func=cmd_match)

    a = common(sub.add_parser("apply", help="fill and submit applications"))
    a.add_argument("--limit", type=int, default=5, help="max applications per candidate")
    a.add_argument("--per-company", type=int, default=1, help="max applications per company per candidate")
    a.add_argument("--dry-run", action="store_true", help="fill the form but never submit")
    a.add_argument("--headless", action="store_true", help="hide the browser (lowers captcha pass rate)")
    a.add_argument("--show", action="store_true",
                   help="bring the Chrome window on screen (default: parked off-screen)")
    a.add_argument("--no-sheet", action="store_true")
    a.add_argument("--sequential", action="store_true", help="run candidates one after another, not in parallel")
    a.add_argument("--target-submitted", type=int, default=0,
                   help="keep finding + applying until N more DISTINCT companies are SUBMITTED")
    a.add_argument("--min-score-floor", type=float, default=40.0)
    a.add_argument("--continuous", action="store_true", help="never stop on target; stop only via data/STOP")
    a.add_argument("--workers", type=int, default=1, help="parallel Chrome workers per candidate")
    a.set_defaults(func=cmd_apply)

    r = common(sub.add_parser("run", help="alias for apply"))
    r.add_argument("--limit", type=int, default=5)
    r.add_argument("--per-company", type=int, default=1)
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--headless", action="store_true")
    r.add_argument("--show", action="store_true")
    r.add_argument("--no-sheet", action="store_true")
    r.add_argument("--sequential", action="store_true")
    r.add_argument("--target-submitted", type=int, default=0)
    r.add_argument("--min-score-floor", type=float, default=40.0)
    r.add_argument("--continuous", action="store_true")
    r.add_argument("--workers", type=int, default=1)
    r.set_defaults(func=cmd_apply)

    rp = sub.add_parser("report", help="rebuild markdown/CSV/sheet from the full audit history")
    rp.add_argument("--no-sheet", action="store_true")
    rp.set_defaults(func=cmd_report)
    sub.add_parser("sheet", help="check Google Sheets credentials").set_defaults(func=cmd_sheet)
    sub.add_parser("profiles", help="show the resume data that will be used").set_defaults(func=cmd_profiles)
    sub.add_parser("mcp", help="run the MCP server (stdio) for Claude Desktop / Claude Code").set_defaults(func=cmd_mcp)
    sub.add_parser("boards", help="probe for new company job boards with India openings").set_defaults(func=cmd_boards)
    return p


_board_probe = {"running": False}


def _maybe_probe_boards() -> None:
    """Once a day, look for new company boards in the background."""
    from . import boards
    if _board_probe["running"] or not boards.is_stale(24):
        return
    _board_probe["running"] = True

    def run():
        try:
            found = boards.refresh()
            n = sum(len(v) for v in found.values())
            log(f"[boards] board discovery done - {n} extra board(s) with India openings saved")
        except Exception as exc:
            log(f"[boards] board discovery failed: {exc}")
        finally:
            _board_probe["running"] = False

    threading.Thread(target=run, daemon=True, name="board-probe").start()


def cmd_boards(args) -> int:
    from . import boards

    def prog(src, token, total, india):
        if india:
            log(f"  + {src}:{token}  {total} postings, {india} in India")

    found = boards.refresh(progress=prog)
    for src, entries in found.items():
        log(f"{src}: {len(entries)} board(s)")
    log(f"saved to {boards.OUT_FILE}")
    return 0


def cmd_mcp(args) -> int:
    from .mcp_server import main as mcp_main
    mcp_main()
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        log("\ninterrupted - nothing further was submitted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
