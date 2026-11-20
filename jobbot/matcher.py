"""Score every real job against a candidate profile.

Scoring is deliberately explainable: every point added is recorded as a
human-readable reason so the Google Sheet / Markdown report can justify
*why* a given person was submitted to a given role.

Total score is 0-100:
    role fit      0-45   (title must look like work they can actually do)
    skills        0-25   (resume keywords present in the description)
    ai signal     0-20   (AI company and/or AI-flavoured role)
    location      0-10   (India / remote preferred, both candidates are in Gurugram)
"""

from __future__ import annotations

import re

from .models import Job, Match

# Titles that are never engineering-IC work, regardless of keyword overlap.
HARD_BLOCK_TITLE = (
    "recruiter", "recruiting", "talent acquisition", "sales", "account executive",
    "account director", "account manager", "sdr", "bdr", "counsel", "attorney",
    "paralegal", "accountant", "accounting", "controller", "payroll", "tax ",
    "nurse", "physician", "therapist", "driver", "technician ii", "warehouse",
    "custodian", "barista", "chef", "security guard", "teacher", "professor",
    "marketing manager", "brand manager", "content writer", "copywriter",
    "graphic designer", "social media", "public relations", "office manager",
    "executive assistant", "chief of staff", "investor relations",
    "real estate", "facilities", "procurement", "supply chain",
)

# Role families our two candidates can credibly do.
ROLE_CORE = (
    "software engineer", "software developer", "full stack", "fullstack",
    "full-stack", "frontend", "front end", "front-end", "backend", "back end",
    "back-end", "web developer", "web engineer", "mern", "react", "node",
    "javascript", "typescript", "ui engineer", "ui developer", "application engineer",
    "product engineer", "platform engineer", "software engineering",
)

# Adjacent roles: plausible but a weaker fit than ROLE_CORE.
ROLE_ADJACENT = (
    "forward deployed", "solutions engineer", "implementation engineer",
    "technical support engineer", "developer experience", "developer advocate",
    "integration engineer", "automation engineer", "qa engineer",
    "test engineer", "sdet", "ai engineer", "applications engineer",
)

# Specialist roles neither candidate is qualified for (no ML/research background).
ROLE_OVERREACH = (
    "machine learning", "ml engineer", "research scientist", "research engineer",
    "deep learning", "nlp engineer", "computer vision", "data scientist",
    "quantitative", "cryptography", "compiler", "kernel", "firmware",
    "embedded", "fpga", "asic", "rf engineer", "mechanical engineer",
    "electrical engineer", "robotics", "hardware", "silicon", "chip design",
    "site reliability", "security engineer", "penetration",
    "data engineer", "analytics engineer", "devops", "infrastructure engineer",
)

AI_ROLE_WORDS = (
    "ai", "a.i.", "artificial intelligence", "llm", "genai", "generative",
    "machine learning", "ml", "agent", "agentic", "copilot", "chatbot",
    "prompt", "rag", "model", "inference", "neural",
)

INDIA_WORDS = (
    "india", "bangalore", "bengaluru", "hyderabad", "pune", "gurugram",
    "gurgaon", "noida", "delhi", "mumbai", "chennai", "ahmedabad", "indore",
    "kolkata", "jaipur", "kochi", "coimbatore", "chandigarh", "mohali",
    "trivandrum", "thiruvananthapuram", "nagpur", "bhopal", "vadodara",
    "surat", "lucknow", "bhubaneswar", "mysore", "mysuru", "visakhapatnam",
    "faridabad", "ghaziabad", "thane", "navi mumbai", "gandhinagar",
    "remote - india", "apac",
)

REMOTE_WORDS = ("remote", "anywhere", "distributed", "work from home", "wfh")

# Remote postings that are legally restricted to a foreign country are useless
# to an India-based candidate with no work authorisation there.
FOREIGN_RESTRICTION = (
    "united states", "usa", "u.s.", "us only", "us-only", " us", "us ",
    "canada", "united kingdom", " uk", "uk ", "ireland", "germany", "france",
    "netherlands", "spain", "portugal", "poland", "emea", "europe", "latam",
    "mexico", "brazil", "argentina", "colombia", "australia", "new zealand",
    "japan", "korea", "singapore", "israel", "switzerland", "sweden",
    "amer", "north america",
)

GLOBAL_WORDS = ("global", "worldwide", "anywhere", "any location", "international")

US_ONLY_WORDS = (
    "united states", "usa", "u.s.", "san francisco", "new york", "seattle",
    "austin", "boston", "chicago", "denver", "atlanta", "los angeles",
    "palo alto", "mountain view", "sunnyvale", "bellevue", "washington",
    "toronto", "vancouver", "london", "dublin", "berlin", "paris",
    "amsterdam", "singapore", "sydney", "tokyo", "tel aviv", "zurich",
)


# Geo buckets returned by classify_geo()
GEO_INDIA = "india"
GEO_REMOTE_OPEN = "remote_open"       # remote with no foreign-only restriction
GEO_REMOTE_FOREIGN = "remote_foreign"  # remote but restricted to another country
GEO_ABROAD = "abroad"
GEO_UNKNOWN = "unknown"

# "Boston, MA; Remote-Friendly" names no country but is plainly US-tied.
_US_STATE_RE = re.compile(
    r",\s*(?:al|ak|az|ar|ca|co|ct|de|fl|ga|hi|id|il|in|ia|ks|ky|la|me|md|ma|"
    r"mi|mn|ms|mo|mt|ne|nv|nh|nj|nm|ny|nc|nd|oh|ok|or|pa|ri|sc|sd|tn|tx|ut|vt|va|"
    r"wa|wv|wi|wy|dc)(?![a-z])"
)


def classify_geo(job) -> tuple[str, str]:
    """Classify a posting's location for an India-based applicant.

    Uses the posting's own location TEXT, never the ATS `isRemote` boolean -
    that flag is unreliable (Baseten marks San-Francisco-onsite roles remote).
    Returns (bucket, human reason).
    """
    loc = (job.location or "").lower().strip()
    if not loc:
        return GEO_UNKNOWN, "location not stated on the posting"

    india_word = _has_word(loc, INDIA_WORDS)
    remote_word = _has(loc, REMOTE_WORDS)

    if india_word:
        if remote_word:
            return GEO_INDIA, f"remote within India ({job.location})"
        return GEO_INDIA, f"India location ({job.location})"

    if remote_word:
        if _has(loc, GLOBAL_WORDS):
            return GEO_REMOTE_OPEN, f"remote, globally open ({job.location})"
        restriction = _has(loc, FOREIGN_RESTRICTION) or _has(loc, US_ONLY_WORDS)
        if not restriction and _US_STATE_RE.search(loc):
            restriction = "a US state"
        if restriction:
            return GEO_REMOTE_FOREIGN, (
                f"remote but tied to '{restriction.strip()}' ({job.location})"
            )
        return GEO_REMOTE_OPEN, f"remote, no country restriction stated ({job.location})"

    return GEO_ABROAD, f"onsite abroad - would need relocation/visa ({job.location})"


_WORD_RE_CACHE: dict[str, re.Pattern] = {}


def _has_word(text: str, needles) -> str | None:
    """Like _has but whole-word, so 'india' does not match 'Indiana'."""
    for n in needles:
        if not n:
            continue
        rx = _WORD_RE_CACHE.get(n)
        if rx is None:
            rx = _WORD_RE_CACHE[n] = re.compile(r"(?<![a-z])" + re.escape(n) + r"(?![a-z])")
        if rx.search(text):
            return n
    return None


def _has(text: str, needles) -> str | None:
    """Return the first needle present in text, else None."""
    for n in needles:
        if n and n in text:
            return n
    return None


def _title_seniority_blocked(title: str, blocked) -> str | None:
    """Word-ish match so 'Lead' blocks but 'Leadership Program' is handled sanely."""
    for word in blocked:
        word = str(word).strip().lower()
        if not word:
            continue
        if re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", title):
            return word
    return None


def score_job(job: Job, cand: dict) -> Match:
    title = (job.title or "").lower()
    desc = (job.description or "").lower()
    loc = (job.location or "").lower()
    blob = f"{title} {desc}"

    reasons: list[str] = []
    role_score = 0.0
    ai_score = 0.0
    loc_score = 0.0
    skill_score = 0.0

    # ---------- hard disqualifiers ----------
    bad = _has(title, HARD_BLOCK_TITLE)
    if bad:
        return Match(job, cand["id"], 0.0, 0, 0, 0, [f"not an engineering role ('{bad}')"])

    senior_bad = _title_seniority_blocked(title, cand.get("seniority_block") or [])
    if senior_bad:
        return Match(job, cand["id"], 0.0, 0, 0, 0, [f"seniority mismatch ('{senior_bad}')"])

    over = _has(title, ROLE_OVERREACH)
    if over:
        return Match(job, cand["id"], 0.0, 0, 0, 0, [f"requires specialist background ('{over}')"])

    # ---------- role fit (0-45) ----------
    core = _has(title, ROLE_CORE)
    adjacent = _has(title, ROLE_ADJACENT)
    if core:
        role_score = 45.0
        reasons.append(f"core role match '{core}'")
    elif adjacent:
        role_score = 26.0
        reasons.append(f"adjacent role '{adjacent}'")
    else:
        custom = _has(title, [r.lower() for r in (cand.get("role_include") or [])])
        if custom:
            role_score = 20.0
            reasons.append(f"profile role keyword '{custom}'")
        else:
            return Match(job, cand["id"], 0.0, 0, 0, 0, ["title outside candidate's role families"])

    # Junior candidates get a boost for explicitly junior titles.
    ok_words = [str(s).lower() for s in (cand.get("seniority_ok") or []) if str(s).strip()]
    hit_ok = _title_seniority_blocked(title, ok_words)
    if hit_ok:
        role_score = min(45.0, role_score + 6.0)
        reasons.append(f"seniority suits candidate ('{hit_ok}')")

    # ---------- skills (0-25) ----------
    skills = [str(s).lower() for s in (cand.get("skill_keywords") or [])]
    hits = sorted({s for s in skills if s and s in blob})
    if hits:
        skill_score = min(25.0, 5.0 * len(hits))
        reasons.append(f"{len(hits)} resume skills in posting ({', '.join(hits[:5])})")

    # ---------- AI signal (0-20) ----------
    if job.company_is_ai:
        ai_score += 12.0
        reasons.append("AI company")
    ai_word = _has(title, AI_ROLE_WORDS)
    if ai_word:
        ai_score += 8.0
        reasons.append(f"AI role keyword '{ai_word}'")
    elif any(w in desc for w in ("llm", "generative ai", "machine learning", "ai-powered", "artificial intelligence")):
        ai_score += 4.0
        reasons.append("AI mentioned in description")
    ai_score = min(20.0, ai_score)

    # ---------- location (0-10) ----------
    # Scored from the posting's own location text. An India-based applicant with
    # no foreign work authorisation is served best by India and globally-open
    # remote roles, so those score highest. Nothing is hard-filtered here -
    # rank(india_or_remote_only=True) does the filtering.
    geo, geo_reason = classify_geo(job)
    loc_score = {
        GEO_INDIA: 10.0,
        GEO_REMOTE_OPEN: 7.0,
        GEO_REMOTE_FOREIGN: 2.0,
        GEO_ABROAD: 1.0,
        GEO_UNKNOWN: 3.0,
    }[geo]
    reasons.append(geo_reason)

    total = round(role_score + skill_score + ai_score + loc_score, 1)
    m = Match(job, cand["id"], total, round(ai_score, 1), round(role_score, 1), round(loc_score, 1), reasons)
    m.geo = geo
    return m


def rank(
    jobs: list[Job],
    cand: dict,
    min_score: float = 55.0,
    limit: int | None = None,
    require_ai: bool = False,
    india_or_remote_only: bool = False,
) -> list[Match]:
    """Score, filter and sort jobs for one candidate (best first)."""
    out: list[Match] = []
    for job in jobs:
        m = score_job(job, cand)
        if m.score < min_score:
            continue
        if require_ai and m.ai_score <= 0:
            continue
        if india_or_remote_only and m.geo not in (GEO_INDIA, GEO_REMOTE_OPEN):
            continue
        out.append(m)

    out.sort(key=lambda m: (-m.score, -m.location_score, m.job.company))
    return out[:limit] if limit else out


def summarize(matches: list[Match]) -> dict:
    """Aggregate counts used by the report and the sheet."""
    companies: dict[str, int] = {}
    for m in matches:
        companies[m.job.company] = companies.get(m.job.company, 0) + 1
    return {
        "total": len(matches),
        "companies": len(companies),
        "per_company": dict(sorted(companies.items(), key=lambda kv: -kv[1])),
        "ai_jobs": sum(1 for m in matches if m.ai_score > 0),
        "india_or_remote": sum(1 for m in matches if m.geo in (GEO_INDIA, GEO_REMOTE_OPEN)),
        "india_only": sum(1 for m in matches if m.geo == GEO_INDIA),
        "top_score": max((m.score for m in matches), default=0.0),
    }
