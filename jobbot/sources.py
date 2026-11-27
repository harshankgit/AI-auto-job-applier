"""Job discovery from ATS public APIs.

Every endpoint here was probed empirically and returns live data. We use httpx
(which bundles certifi) because the system Python has a broken SSL trust store.
"""

from __future__ import annotations

import concurrent.futures as cf
import re
from typing import Any, Iterable

import httpx

from .config import RealJobGuard, load_companies
from .models import Job

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept": "application/json,text/plain,*/*"}
TIMEOUT = httpx.Timeout(25.0, connect=12.0)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _text(html: str | None, limit: int = 6000) -> str:
    if not html:
        return ""
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", html)).strip()[:limit]


def _remote(*parts: str) -> bool:
    blob = " ".join(p.lower() for p in parts if p)
    return "remote" in blob or "anywhere" in blob


# ------------------------------------------------------------------- greenhouse

def fetch_greenhouse(token: str, name: str, is_ai: bool) -> list[Job]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    with httpx.Client(timeout=TIMEOUT, headers=HEADERS, follow_redirects=True) as c:
        r = c.get(url)
        r.raise_for_status()
        payload = r.json()

    jobs: list[Job] = []
    for j in payload.get("jobs", []):
        jid = str(j.get("id", ""))
        loc = (j.get("location") or {}).get("name", "") or ""
        title = j.get("title", "") or ""
        # job-boards.greenhouse.io is the host that actually serves the form
        apply_url = f"https://job-boards.greenhouse.io/{token}/jobs/{jid}"
        jobs.append(
            Job(
                source="greenhouse",
                company=name,
                company_token=token,
                job_id=jid,
                title=title,
                location=loc,
                url=j.get("absolute_url", "") or apply_url,
                apply_url=apply_url,
                description=_text(j.get("content")),
                is_remote=_remote(loc, title),
                department=", ".join(
                    d.get("name", "") for d in (j.get("departments") or [])
                ),
                posted_at=j.get("updated_at", "") or "",
                company_is_ai=is_ai,
            )
        )
    return jobs


# ------------------------------------------------------------------------ lever

def fetch_lever(token: str, name: str, is_ai: bool) -> list[Job]:
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    with httpx.Client(timeout=TIMEOUT, headers=HEADERS, follow_redirects=True) as c:
        r = c.get(url)
        r.raise_for_status()
        payload = r.json()

    jobs: list[Job] = []
    for j in payload if isinstance(payload, list) else []:
        cats = j.get("categories") or {}
        loc = cats.get("location", "") or ""
        title = j.get("text", "") or ""
        hosted = j.get("hostedUrl", "") or ""
        jobs.append(
            Job(
                source="lever",
                company=name,
                company_token=token,
                job_id=str(j.get("id", "")),
                title=title,
                location=loc,
                url=hosted,
                apply_url=f"{hosted.rstrip('/')}/apply" if hosted else "",
                description=_text(j.get("descriptionPlain") or j.get("description")),
                is_remote=_remote(loc, title, cats.get("commitment", "") or ""),
                department=cats.get("team", "") or "",
                employment_type=cats.get("commitment", "") or "",
                posted_at=str(j.get("createdAt", "")),
                company_is_ai=is_ai,
            )
        )
    return jobs


# ------------------------------------------------------------------------ ashby

def fetch_ashby(token: str, name: str, is_ai: bool) -> list[Job]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{token}"
    with httpx.Client(timeout=TIMEOUT, headers=HEADERS, follow_redirects=True) as c:
        r = c.get(url)
        r.raise_for_status()
        payload = r.json()

    jobs: list[Job] = []
    for j in payload.get("jobs", []):
        if j.get("isListed") is False:
            continue
        loc = j.get("location", "") or ""
        title = j.get("title", "") or ""
        job_url = j.get("jobUrl", "") or ""
        apply_url = j.get("applyUrl", "") or job_url
        if apply_url and not apply_url.rstrip("/").endswith("application"):
            apply_url = f"{apply_url.rstrip('/')}/application"
        jobs.append(
            Job(
                source="ashby",
                company=name,
                company_token=token,
                job_id=str(j.get("id", "")),
                title=title,
                location=loc,
                url=job_url,
                apply_url=apply_url,
                description=_text(j.get("descriptionPlain") or j.get("descriptionHtml")),
                is_remote=bool(j.get("isRemote")) or _remote(loc, title),
                department=j.get("department", "") or j.get("team", "") or "",
                employment_type=j.get("employmentType", "") or "",
                posted_at=j.get("publishedAt", "") or "",
                company_is_ai=is_ai,
            )
        )
    return jobs


# --------------------------------------------------------------------- workable

def fetch_workable(token: str, name: str, is_ai: bool) -> list[Job]:
    url = f"https://apply.workable.com/api/v1/widget/accounts/{token}?details=true"
    with httpx.Client(timeout=TIMEOUT, headers=HEADERS, follow_redirects=True) as c:
        r = c.get(url)
        r.raise_for_status()
        payload = r.json()

    company = payload.get("name") or name
    jobs: list[Job] = []
    for j in payload.get("jobs", []) or []:
        code = str(j.get("shortcode", "") or "")
        if not code:
            continue
        locs = j.get("locations") or []
        parts = [j.get("city", ""), j.get("state", ""), j.get("country", "")]
        if locs:
            parts = [locs[0].get("city", ""), locs[0].get("region", ""), locs[0].get("country", "")]
        loc = ", ".join(p for p in parts if p)
        if j.get("telecommuting"):
            loc = f"{loc} (Remote)" if loc else "Remote"
        title = j.get("title", "") or ""
        job_url = f"https://apply.workable.com/{token}/j/{code}/"
        jobs.append(
            Job(
                source="workable",
                company=company,
                company_token=token,
                job_id=code,
                title=title,
                location=loc,
                url=job_url,
                apply_url=f"https://apply.workable.com/{token}/j/{code}/apply/",
                description=_text(j.get("description")),
                is_remote=bool(j.get("telecommuting")) or _remote(loc, title),
                department=j.get("department", "") or "",
                employment_type=j.get("employment_type", "") or "",
                posted_at=j.get("published_on", "") or "",
                company_is_ai=is_ai,
            )
        )
    return jobs


# -------------------------------------------------------------- smartrecruiters

def fetch_smartrecruiters(token: str, name: str, is_ai: bool) -> list[Job]:
    """India postings only (country=in) - the global lists are huge."""
    base = f"https://api.smartrecruiters.com/v1/companies/{token}/postings"
    rows: list[dict[str, Any]] = []
    with httpx.Client(timeout=TIMEOUT, headers=HEADERS, follow_redirects=True) as c:
        offset = 0
        while offset < 400:
            r = c.get(base, params={"limit": 100, "offset": offset, "country": "in"})
            r.raise_for_status()
            payload = r.json()
            content = payload.get("content") or []
            rows.extend(content)
            offset += 100
            if len(content) < 100 or offset >= int(payload.get("totalFound") or 0):
                break

    jobs: list[Job] = []
    for j in rows:
        jid = str(j.get("id", "") or "")
        if not jid:
            continue
        l = j.get("location") or {}
        loc = l.get("fullLocation") or ", ".join(
            p for p in (l.get("city"), l.get("region"), (l.get("country") or "").upper()) if p
        )
        remote = bool(l.get("remote"))
        title = j.get("name", "") or ""
        comp = (j.get("company") or {}).get("name") or name
        job_url = f"https://jobs.smartrecruiters.com/{token}/{jid}"
        labels = [
            (j.get(k) or {}).get("label", "")
            for k in ("function", "experienceLevel", "industry", "typeOfEmployment")
        ]
        jobs.append(
            Job(
                source="smartrecruiters",
                company=comp,
                company_token=token,
                job_id=jid,
                title=title,
                location=loc + (" (Remote)" if remote and "remote" not in loc.lower() else ""),
                url=job_url,
                apply_url=job_url,
                description=" | ".join(x for x in labels if x),
                is_remote=remote or _remote(loc, title),
                department=(j.get("department") or {}).get("label", "") or "",
                employment_type=(j.get("typeOfEmployment") or {}).get("label", "") or "",
                posted_at=j.get("releasedDate", "") or "",
                company_is_ai=is_ai,
            )
        )
    return jobs


# -------------------------------------------------------------------- recruitee

def fetch_recruitee(token: str, name: str, is_ai: bool) -> list[Job]:
    url = f"https://{token}.recruitee.com/api/offers/"
    with httpx.Client(timeout=TIMEOUT, headers=HEADERS, follow_redirects=True) as c:
        r = c.get(url)
        r.raise_for_status()
        payload = r.json()

    jobs: list[Job] = []
    for j in payload.get("offers", []) or []:
        if j.get("status") not in (None, "published"):
            continue
        slug = j.get("slug", "") or ""
        loc = j.get("location", "") or ", ".join(
            x.get("name", "") for x in (j.get("locations") or []) if x.get("name")
        )
        remote = bool(j.get("remote"))
        title = j.get("title", "") or ""
        job_url = j.get("careers_url") or f"https://{token}.recruitee.com/o/{slug}"
        apply_url = j.get("careers_apply_url") or f"{job_url.rstrip('/')}/c/new"
        jobs.append(
            Job(
                source="recruitee",
                company=j.get("company_name") or name,
                company_token=token,
                job_id=str(j.get("id", "") or slug),
                title=title,
                location=loc + (" (Remote)" if remote and "remote" not in loc.lower() else ""),
                url=job_url,
                apply_url=apply_url,
                description=_text((j.get("description") or "") + " " + (j.get("requirements") or "")),
                is_remote=remote or _remote(loc, title),
                department=j.get("department", "") or "",
                employment_type=j.get("employment_type_code", "") or "",
                posted_at=j.get("published_at", "") or "",
                company_is_ai=is_ai,
            )
        )
    return jobs


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "workable": fetch_workable,
    "smartrecruiters": fetch_smartrecruiters,
    "recruitee": fetch_recruitee,
}

# Workable is fetchable but its Cloudflare Turnstile rejects automated submits,
# so it is not part of the default discovery set (pass it explicitly to list jobs).
ALL_SOURCES = tuple(k for k in FETCHERS if k != "workable")


# ------------------------------------------------------------------ aggregators

def fetch_arbeitnow() -> list[Job]:
    """Open job board API - good source of genuinely remote/EU-friendly roles."""
    jobs: list[Job] = []
    try:
        with httpx.Client(timeout=TIMEOUT, headers=HEADERS, follow_redirects=True) as c:
            payload = c.get("https://www.arbeitnow.com/api/job-board-api").json()
    except Exception:
        return jobs

    for j in payload.get("data", []):
        url = j.get("url", "") or ""
        jobs.append(
            Job(
                source="arbeitnow",
                company=j.get("company_name", "") or "Unknown",
                company_token=(j.get("company_name", "") or "unknown").lower().replace(" ", "-"),
                job_id=j.get("slug", "") or url,
                title=j.get("title", "") or "",
                location=j.get("location", "") or "",
                url=url,
                apply_url=url,
                description=_text(j.get("description")),
                is_remote=bool(j.get("remote")),
                employment_type=", ".join(j.get("job_types") or []),
                posted_at=str(j.get("created_at", "")),
            )
        )
    return jobs


def fetch_remoteok() -> list[Job]:
    jobs: list[Job] = []
    try:
        with httpx.Client(timeout=TIMEOUT, headers=HEADERS, follow_redirects=True) as c:
            payload = c.get("https://remoteok.com/api").json()
    except Exception:
        return jobs

    rows = [r for r in payload if isinstance(r, dict) and r.get("id")]
    for j in rows:
        apply_url = j.get("apply_url", "") or j.get("url", "") or ""
        jobs.append(
            Job(
                source="remoteok",
                company=j.get("company", "") or "Unknown",
                company_token=(j.get("company", "") or "unknown").lower().replace(" ", "-"),
                job_id=str(j.get("id", "")),
                title=j.get("position", "") or "",
                location=j.get("location", "") or "Remote",
                url=j.get("url", "") or apply_url,
                apply_url=apply_url,
                description=_text(j.get("description")),
                is_remote=True,
                posted_at=str(j.get("date", "")),
            )
        )
    return jobs


# ------------------------------------------------------------------- orchestrator

def discover(
    sources: Iterable[str] = ALL_SOURCES,
    include_aggregators: bool = True,
    workers: int = 64,
    progress=None,
) -> tuple[list[Job], dict[str, Any]]:
    """Fetch every configured board in parallel.

    Returns (real_jobs, stats). Fake/demo postings are dropped here and counted.
    """
    cfg = load_companies()
    guard = RealJobGuard(cfg)
    sources = list(sources)

    tasks: list[tuple[str, str, str, bool]] = []
    for src in sources:
        if src not in FETCHERS:
            continue
        for entry in cfg.get(src, []):
            token = entry["token"]
            if not guard.board_ok(token):
                continue  # fake/demo board -- never fetched, never applied to
            tasks.append((src, token, entry.get("name", token), bool(entry.get("ai"))))

    raw: list[Job] = []
    per_company: dict[str, int] = {}
    failures: list[str] = []

    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {
            pool.submit(FETCHERS[src], token, name, is_ai): (src, token, name)
            for src, token, name, is_ai in tasks
        }
        for fut in cf.as_completed(futs):
            src, token, name = futs[fut]
            try:
                got = fut.result()
            except Exception as exc:
                failures.append(f"{src}:{token} ({type(exc).__name__})")
                continue
            raw.extend(got)
            per_company[name] = len(got)
            if progress:
                progress(name, len(got))

    if include_aggregators:
        for fn in (fetch_arbeitnow, fetch_remoteok):
            got = fn()
            raw.extend(got)
            if got:
                per_company[got[0].source] = len(got)

    # real-job guard + dedupe
    real: list[Job] = []
    seen: set[str] = set()
    rejected: dict[str, int] = {}
    for j in raw:
        ok, why = guard.job_ok(j)
        if not ok:
            rejected[why] = rejected.get(why, 0) + 1
            continue
        if j.key in seen:
            continue
        seen.add(j.key)
        real.append(j)

    stats = {
        "boards_queried": len(tasks),
        "raw_jobs": len(raw),
        "real_jobs": len(real),
        "rejected_fake": sum(rejected.values()),
        "rejected_reasons": rejected,
        "per_company": per_company,
        "failures": failures,
    }
    return real, stats
