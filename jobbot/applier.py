"""Fill and submit one real job application, then verify the outcome honestly.

Rules this module enforces:
  * Every value typed into the form comes from forms.py, which sources it from
    the candidate's resume (identity.py) or the user-confirmed answers file.
  * Nothing is ever reported as SUBMITTED unless the page itself confirmed it.
  * Every outcome - success, captcha wall, validation failure, crash - is
    screenshotted and recorded.
"""

from __future__ import annotations

import time

from . import browser, config, forms, store
from .identity import Identity
from .models import (
    ALREADY_APPLIED,
    CAPTCHA_BLOCKED,
    CLOSED,
    ERROR,
    FAILED_VALIDATION,
    LOGIN_REQUIRED,
    SKIPPED,
    SUBMITTED,
    ApplicationResult,
    Match,
)

CONFIRM_PHRASES = (
    "thank you for applying",
    "thanks for applying",
    "thank you for your application",
    "application submitted",
    "application received",
    "we received your application",
    "we've received your application",
    "your application has been received",
    "your application was submitted",
    "successfully submitted",
    "submission received",
    "we will be in touch",
    "we'll be in touch",
    "thank you for your interest",
    "thanks, got it",
    "you'll hear from us",
    "you will hear from us",
    "we'll review your application",
)

ALREADY_PHRASES = (
    "already applied",
    "you have applied",
    "duplicate application",
    "already submitted an application",
)

CLOSED_PHRASES = (
    "no longer accepting applications",
    "this position has been closed",
    "this job is closed",
    "posting is no longer available",
    "job posting is not available",
    "position is no longer available",
    "page not found",
)

VALIDATION_HINTS = (
    "please complete",
    "is required",
    "required field",
    "please fill",
    "please enter",
    "invalid",
    "must be",
)

# Only these ATS platforms expose a real, fillable application form that this
# bot understands. Aggregator listings (RemoteOK, Arbeitnow) just link out to
# arbitrary third-party pages or email addresses, so they are never auto-applied.
APPLIABLE_SOURCES = ("greenhouse", "lever", "ashby")


def is_appliable(job) -> bool:
    return job.source in APPLIABLE_SOURCES


SUBMIT_TEXTS = (
    "submit application",
    "submit my application",
    "submit your application",
    "send application",
    "submit",
)


def _has_phrase(text: str, phrases) -> str:
    low = text.lower()
    for p in phrases:
        if p in low:
            return p
    return ""


def normalize_apply_url(job) -> str:
    """Point the browser at the actual form, not the marketing page."""
    url = (job.apply_url or job.url or "").strip()
    if not url:
        return ""
    src = job.source
    if src == "lever" and not url.rstrip("/").endswith("/apply"):
        url = url.rstrip("/") + "/apply"
    elif src == "ashby" and "/application" not in url:
        url = url.rstrip("/") + "/application"
    elif src == "greenhouse" and "greenhouse.io/" in url and "/embed/" not in url:
        # Many employers 301 their hosted Greenhouse board to their own careers
        # site, which is Cloudflare/Turnstile protected and renders no form at
        # all (Coinbase, Starburst). Greenhouse's embed endpoint always serves
        # the real application form, so address it directly.
        rest = url.split("greenhouse.io/", 1)[1]
        parts = [p for p in rest.split("/") if p]
        if len(parts) >= 3 and parts[1] == "jobs":
            board = parts[0]
            token = parts[2].split("?")[0].split("#")[0]
            if board and token.isdigit():
                url = (
                    "https://boards.greenhouse.io/embed/job_app"
                    f"?for={board}&token={token}"
                )
    return url


def _page_text(page) -> str:
    try:
        return page.inner_text("body", timeout=5000) or ""
    except Exception:
        try:
            return page.content() or ""
        except Exception:
            return ""


def _find_submit(page):
    """Locate the real submit control without hitting 'Apply with LinkedIn'."""
    for text in SUBMIT_TEXTS:
        try:
            loc = page.get_by_role("button", name=text, exact=False)
            count = loc.count()
        except Exception:
            continue
        for i in range(min(count, 4)):
            btn = loc.nth(i)
            try:
                if not btn.is_visible():
                    continue
                label = (btn.inner_text(timeout=2000) or "").lower()
                if "linkedin" in label or "indeed" in label or "autofill" in label:
                    continue
                return btn
            except Exception:
                continue
    for sel in ("input[type=submit]", "button[type=submit]"):
        try:
            loc = page.locator(sel)
            for i in range(min(loc.count(), 4)):
                btn = loc.nth(i)
                if btn.is_visible():
                    return btn
        except Exception:
            continue
    return None


def _wait_for_outcome(page, timeout_s: float = 20.0) -> str:
    """Poll the page after submit until it confirms, errors, or we give up."""
    start_url = page.url
    deadline = time.time() + timeout_s
    last = ""
    while time.time() < deadline:
        time.sleep(0.7)
        text = _page_text(page)
        last = text
        if _has_phrase(text, CONFIRM_PHRASES):
            return text
        if page.url != start_url and "confirmation" in page.url.lower():
            return text
    return last


def apply_to_job(
    session: browser.BrowserSession,
    match: Match,
    identity: Identity,
    answers: dict,
    dry_run: bool = False,
) -> ApplicationResult:
    """Attempt one application. Always returns a truthful ApplicationResult."""
    job = match.job
    started = time.time()
    snap = identity.contact_snapshot()

    def build(status: str, detail: str, **kw) -> ApplicationResult:
        return ApplicationResult(
            candidate_id=identity.id,
            candidate_name=identity.full_name,
            job=job,
            status=status,
            detail=detail,
            score=match.score,
            match_reasons=match.why,
            duration_s=round(time.time() - started, 1),
            email=snap["email"],
            phone=identity.phone_display,
            candidate_location=snap["candidate_location"],
            linkedin=snap["linkedin"],
            github=snap["github"],
            portfolio=snap["portfolio"],
            resume_file=snap["resume_file"],
            **kw,
        )

    url = normalize_apply_url(job)
    if not url:
        return build(ERROR, "no apply url on this posting")

    page = session.new_page()
    try:
        if not browser.goto(page, url):
            return build(ERROR, f"could not load {url}", screenshot=browser.shoot(page, f"loadfail-{job.company_token}"))

        browser.human_pause(0.6, 1.4)
        text = _page_text(page)

        hit = _has_phrase(text, CLOSED_PHRASES)
        if hit:
            return build(CLOSED, f"posting closed ('{hit}')", screenshot=browser.shoot(page, f"closed-{job.company_token}"))

        hit = _has_phrase(text, ALREADY_PHRASES)
        if hit:
            return build(ALREADY_APPLIED, f"page says '{hit}'", screenshot=browser.shoot(page, f"already-{job.company_token}"))

        low_url = page.url.lower()
        if "/login" in low_url or "authwall" in low_url or "signin" in low_url:
            return build(LOGIN_REQUIRED, f"redirected to a login wall: {page.url}", screenshot=browser.shoot(page, f"login-{job.company_token}"))

        # Lazy ATS forms only mount their fields once scrolled into view.
        browser.scroll_through(page, steps=3)
        browser.human_pause(0.5, 1.0)

        # Many boards (MongoDB, Figma, ...) redirect to their own careers site and
        # embed the real ATS form in an iframe. Work inside that frame when present.
        container, frame_reason = forms.form_container(page)

        plans = forms.plan_fields(container, identity, job.title, job.company, job)
        if not plans:
            return build(ERROR, f"no form fields found on the apply page ({frame_reason})", screenshot=browser.shoot(page, f"noform-{job.company_token}"))

        outcome = forms.apply_plan(container, plans)
        filled = outcome["filled"]
        skipped = outcome["skipped"]
        resume_ok = outcome["resume_attached"]

        if not resume_ok:
            resume_ok = _retry_resume(container, identity)

        browser.human_pause(0.8, 1.6)
        # Pass the fill outcome so fields we FAILED to set also block submission.
        missing = forms.missing_required(plans, outcome)

        if dry_run:
            shot = browser.shoot(page, f"dryrun-{job.company_token}-{job.job_id}")
            detail = f"DRY RUN - filled {filled} fields, resume={'yes' if resume_ok else 'no'}, not submitted"
            if missing:
                detail += f" | unanswered required: {', '.join(missing[:4])}"
            return build(
                SKIPPED, detail, fields_filled=filled, fields_skipped=skipped,
                resume_attached=resume_ok, screenshot=shot,
            )

        if not resume_ok:
            return build(
                FAILED_VALIDATION, "could not attach the resume file - refusing to submit without it",
                fields_filled=filled, fields_skipped=skipped,
                screenshot=browser.shoot(page, f"noresume-{job.company_token}"),
            )

        if missing:
            return build(
                FAILED_VALIDATION,
                f"required questions we have no resume fact or confirmed answer for: {', '.join(missing[:5])}",
                fields_filled=filled, fields_skipped=skipped, resume_attached=resume_ok,
                screenshot=browser.shoot(page, f"missing-{job.company_token}"),
            )

        blocked, why = forms.captcha_present(page)
        pre_shot = browser.shoot(page, f"filled-{job.company_token}-{job.job_id}")

        btn = _find_submit(container)
        if btn is None:
            return build(
                ERROR, "form filled but no submit button found",
                fields_filled=filled, fields_skipped=skipped,
                resume_attached=resume_ok, screenshot=pre_shot,
            )

        try:
            btn.scroll_into_view_if_needed(timeout=5000)
        except Exception:
            pass
        # Ashby runs reCAPTCHA Enterprise + a device fingerprint and is still
        # processing the resume upload for a few seconds - a click made too soon
        # is silently ignored. Give it time before submitting.
        browser.human_pause(3.0, 4.0)

        try:
            btn.click(timeout=15000)
        except Exception as exc:
            return build(
                ERROR, f"submit click failed: {type(exc).__name__}: {exc}",
                fields_filled=filled, fields_skipped=skipped,
                resume_attached=resume_ok, screenshot=pre_shot,
            )

        after = _wait_for_outcome(page)
        if not _has_phrase(after, CONFIRM_PHRASES) and not _has_phrase(after, VALIDATION_HINTS):
            # Form still sitting there with no error: the first click was swallowed.
            # Safe to click again - a successful submit replaces the form, so a
            # visible submit button means nothing was sent.
            again = _find_submit(container)
            if again is not None:
                try:
                    browser.human_pause(2.0, 3.0)
                    again.click(timeout=15000)
                    after = _wait_for_outcome(page, timeout_s=25.0)
                except Exception:
                    pass
        post_shot = browser.shoot(page, f"after-{job.company_token}-{job.job_id}")
        confirm = _has_phrase(after, CONFIRM_PHRASES)

        if confirm:
            return build(
                SUBMITTED, f"confirmed by the page ('{confirm}')",
                fields_filled=filled, fields_skipped=skipped, resume_attached=resume_ok,
                screenshot=post_shot, confirmation_text=_excerpt(after, confirm),
            )

        hit = _has_phrase(after, ALREADY_PHRASES)
        if hit:
            return build(
                ALREADY_APPLIED, f"page says '{hit}' after submit",
                fields_filled=filled, fields_skipped=skipped,
                resume_attached=resume_ok, screenshot=post_shot,
            )

        still_blocked, why2 = forms.captcha_present(page)
        if still_blocked or blocked:
            return build(
                CAPTCHA_BLOCKED,
                f"no confirmation and a captcha is in the way ({why2 or why}) - a human must clear it",
                fields_filled=filled, fields_skipped=skipped,
                resume_attached=resume_ok, screenshot=post_shot,
            )

        hit = _has_phrase(after, VALIDATION_HINTS)
        if hit:
            return build(
                FAILED_VALIDATION, f"form rejected the submission ('{hit}')",
                fields_filled=filled, fields_skipped=skipped,
                resume_attached=resume_ok, screenshot=post_shot,
            )

        return build(
            ERROR, "clicked submit but the page never confirmed - treating as NOT submitted",
            fields_filled=filled, fields_skipped=skipped,
            resume_attached=resume_ok, screenshot=post_shot,
        )

    except Exception as exc:
        shot = ""
        try:
            shot = browser.shoot(page, f"crash-{job.company_token}")
        except Exception:
            pass
        return build(ERROR, f"{type(exc).__name__}: {exc}", screenshot=shot)
    finally:
        try:
            page.close()
        except Exception:
            pass


def _retry_resume(page, identity: Identity) -> bool:
    """Second attempt at the file input - some ATS mount it late."""
    try:
        loc = page.locator("input[type=file]")
        for i in range(min(loc.count(), 3)):
            try:
                loc.nth(i).set_input_files(identity.resume, timeout=20000)
                return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _excerpt(text: str, needle: str, width: int = 200) -> str:
    low = text.lower()
    i = low.find(needle)
    if i < 0:
        return text.strip()[:width]
    start = max(0, i - 40)
    return " ".join(text[start : start + width].split())


def run_batch(
    session: browser.BrowserSession,
    matches: list[Match],
    identity: Identity,
    answers: dict,
    limit: int = 10,
    per_company: int = 1,
    dry_run: bool = False,
    progress=None,
    on_result=None,
    stop=None,
    max_tries_per_company: int = 0,
) -> list[ApplicationResult]:
    """Apply down a ranked list. Never the same role twice; different roles at
    one company are fine (up to per_company each run+history)."""
    store.init()
    results: list[ApplicationResult] = []
    company_hits: dict[str, int] = {}
    tried_this_run: set[tuple[str, str]] = set()
    done = 0
    # Failures only count against a role if they happened AFTER answers.yaml was
    # last edited - adding a missing answer makes those roles eligible again.
    answers_file = config.CONFIG_DIR / "answers.yaml"
    answers_mtime = answers_file.stat().st_mtime if answers_file.exists() else 0
    answers_since = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(answers_mtime))

    company_tries: dict[str, int] = {}
    for match in matches:
        if done >= limit:
            break
        if stop is not None and stop():
            break
        job = match.job
        token = job.company_token
        if max_tries_per_company and company_tries.get(token, 0) >= max_tries_per_company:
            continue

        if not is_appliable(job):
            # Aggregator listing - no fillable form on the other end.
            continue

        role_id = (token, store._norm_title(job.title))
        if role_id in tried_this_run:
            continue
        used = store.role_already_used(identity.id, job.key, token, job.company, job.title)
        if used:
            continue
        if store.failed_attempts_since(identity.id, job.key, answers_since) >= 2:
            # Two honest tries already failed with the current answers: a third
            # identical try just burns time. Edit answers.yaml and it retries.
            continue
        prior = store.company_attempt_count(identity.id, token)
        if company_hits.get(token, 0) + prior >= per_company:
            if progress:
                progress(f"    (skip {job.company}: {per_company} roles there already, moving on)")
            continue

        if progress:
            progress(f"[{done + 1}/{limit}] {identity.full_name} -> {job.company}: {job.title}")

        result = apply_to_job(session, match, identity, answers, dry_run=dry_run)
        store.record(result)
        tried_this_run.add(role_id)
        results.append(result)
        done += 1
        company_tries[token] = company_tries.get(token, 0) + 1

        if result.submitted or "clicked submit" in (result.detail or ""):
            company_hits[token] = company_hits.get(token, 0) + 1

        if progress:
            progress(f"    -> {result.status}: {result.detail[:110]}")

        # Flush the record to disk immediately: the user wants every application
        # visible in the sheet/markdown the moment it lands, not only at the end.
        if on_result:
            try:
                on_result(result)
            except Exception as exc:  # reporting must never abort the run
                if progress:
                    progress(f"    (report flush failed: {exc})")

        browser.human_pause(0.5, 1.2)

    return results
