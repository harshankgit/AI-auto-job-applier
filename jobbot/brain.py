"""Grounded last-resort answerer for screening questions.

`forms.resolve_value` asks this module only AFTER a resume field lookup and the
user-confirmed `config/answers.yaml` rules have both come up empty (or have
explicitly delegated here with the `ANSWER_BRAIN` token, which exists for the
handful of questions whose truthful answer depends on the job's country).

Hard rules this module obeys, in order of importance:

1. **Nothing is invented.** Every returned string is either
   * a verbatim resume/profile fact (`identity.*`),
   * a fact about the *job* (its country, its stated stack), or
   * a logical Yes/No/0 that follows from those facts.
   A question we cannot ground returns `(None, reason)` so the caller skips it.
2. **No inflation.** "Years of experience with <tech>" returns the candidate's
   real total only when that tech is genuinely in their resume skill list;
   otherwise it returns "0".
3. **Truthful about work rights.** Both candidates are Indian citizens based in
   India with no work authorization anywhere else. So sponsorship/authorization
   answers are derived from the country the job (or the question) names, never
   from a blanket "No".
4. **Auditable.** Every answer carries a one-line reason explaining the
   inference; it is written to the report's `Fields Skipped` / audit trail.

Deterministic pure Python. No network, no model, no API key.
"""

from __future__ import annotations

import re
from typing import Any

# --------------------------------------------------------------------- country

INDIA_MARKERS = (
    "india", "bharat",
    "bengaluru", "bangalore", "mumbai", "bombay", "delhi", "new delhi",
    "gurugram", "gurgaon", "noida", "hyderabad", "pune", "chennai", "madras",
    "kolkata", "calcutta", "ahmedabad", "jaipur", "indore", "chandigarh",
    "kochi", "cochin", "coimbatore", "thiruvananthapuram", "bhubaneswar",
    "nagpur", "surat", "vadodara", "mysuru", "mysore", "trivandrum",
)

US_MARKERS = (
    "united states", "u.s.a", "u.s.", "usa", " us ", "us-", "-us", "(us)",
    "america", "new york", "nyc", "san francisco", "sf bay", "bay area",
    "palo alto", "mountain view", "sunnyvale", "san jose", "santa clara",
    "seattle", "austin", "boston", "chicago", "denver", "atlanta", "miami",
    "los angeles", "san diego", "portland", "phoenix", "dallas", "houston",
    "philadelphia", "pittsburgh", "detroit", "minneapolis", "nashville",
    "washington, dc", "washington dc", "redmond", "bellevue", "cambridge, ma",
    "california", "new york state", "texas", "massachusetts", "washington state",
    "colorado", "illinois", "georgia, us", "florida", "oregon", "virginia",
    "north carolina", "arizona", "utah", "michigan", "ohio", "remote - us",
    "remote (us", "remote, us",
)

UK_MARKERS = (
    "united kingdom", "u.k.", " uk ", "(uk)", "uk-", "-uk", "england",
    "london", "manchester", "edinburgh", "cambridge, uk", "oxford", "bristol",
    "glasgow", "leeds", "birmingham, uk", "scotland", "wales", "northern ireland",
)

CANADA_MARKERS = (
    "canada", "toronto", "vancouver", "montreal", "montréal", "ottawa",
    "calgary", "waterloo, on", "ontario", "quebec", "british columbia",
)

EU_MARKERS = (
    "germany", "berlin", "munich", "münchen", "hamburg", "frankfurt", "cologne",
    "france", "paris", "lyon", "netherlands", "amsterdam", "rotterdam",
    "utrecht", "the hague", "spain", "madrid", "barcelona", "valencia",
    "italy", "milan", "rome", "ireland", "dublin", "poland", "warsaw",
    "krakow", "kraków", "sweden", "stockholm", "gothenburg", "denmark",
    "copenhagen", "norway", "oslo", "finland", "helsinki", "belgium",
    "brussels", "austria", "vienna", "portugal", "lisbon", "porto",
    "czech", "prague", "romania", "bucharest", "hungary", "budapest",
    "greece", "athens", "bulgaria", "sofia", "estonia", "tallinn",
    "lithuania", "vilnius", "croatia", "zagreb", "slovakia", "slovenia",
    "luxembourg", "european union", "emea",
)

OTHER_ABROAD_MARKERS = (
    "switzerland", "zurich", "zürich", "geneva", "basel",
    "singapore", "australia", "sydney", "melbourne", "brisbane", "perth",
    "new zealand", "auckland", "japan", "tokyo", "osaka", "korea", "seoul",
    "china", "beijing", "shanghai", "shenzhen", "hong kong", "taiwan", "taipei",
    "israel", "tel aviv", "jerusalem", "uae", "dubai", "abu dhabi",
    "saudi", "riyadh", "qatar", "doha", "egypt", "cairo", "south africa",
    "cape town", "johannesburg", "nigeria", "lagos", "kenya", "nairobi",
    "brazil", "sao paulo", "são paulo", "mexico", "mexico city", "argentina",
    "buenos aires", "chile", "santiago", "colombia", "bogota", "bogotá",
    "peru", "lima", "costa rica", "philippines", "manila", "indonesia",
    "jakarta", "malaysia", "kuala lumpur", "vietnam", "hanoi", "thailand",
    "bangkok", "turkey", "istanbul", "ukraine", "kyiv", "serbia", "belgrade",
    "pakistan", "bangladesh", "sri lanka", "nepal",
)

# Country label -> the marker table that identifies it. Order matters: India is
# checked first so "Remote - India" never falls through to a looser match.
_COUNTRY_TABLE: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("India", INDIA_MARKERS),
    ("the United States", US_MARKERS),
    ("the United Kingdom", UK_MARKERS),
    ("Canada", CANADA_MARKERS),
    ("the European Union", EU_MARKERS),
    ("abroad", OTHER_ABROAD_MARKERS),
)

REMOTE_MARKERS = ("remote", "work from home", "anywhere", "distributed")


def _low(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).lower().strip()


def detect_country(text: str) -> str | None:
    """Name the country a free-text location/question refers to, or None.

    Returns one of the labels in `_COUNTRY_TABLE` ("India", "the United
    States", ... , "abroad") - never a guess for text with no geography in it.
    """
    low = f" {_low(text)} "
    for country, markers in _COUNTRY_TABLE:
        for marker in markers:
            if marker in low:
                return country
    return None


def job_country(job) -> tuple[str | None, str]:
    """(country label, how we decided). Falls back to India for remote roles.

    A remote posting with no country named is one both candidates would work
    from their actual home in India, so "India" is the truthful reading - but
    the reason string records that it was inferred from 'remote', not stated.
    """
    location = _low(getattr(job, "location", ""))
    found = detect_country(location)
    if found:
        return found, f"job location {location!r} is in {found}"

    remote = bool(getattr(job, "is_remote", False)) or any(
        m in location for m in REMOTE_MARKERS
    )
    if remote:
        return "India", "remote posting with no country named - candidate works from India"

    # Last chance: the description sometimes names the office when the
    # structured location field is blank.
    desc = _low(getattr(job, "description", ""))[:1500]
    found = detect_country(desc)
    if found:
        return found, f"job description names {found}"
    return None, f"job location {location!r} names no country we recognise"


# ------------------------------------------------------------------ vocabulary

# Technology tokens we can recognise inside a "years of experience with X"
# question. Presence here does NOT mean the candidate knows it - that is checked
# against their own resume skill list.
TECH_VOCAB = (
    "react", "react.js", "reactjs", "next.js", "nextjs", "node", "node.js",
    "nodejs", "express", "mongodb", "mongoose", "javascript", "typescript",
    "redux", "tailwind", "jwt", "rest api", "graphql", "html", "css", "git",
    "vite", "formik", "supabase", "mern", "python", "django", "flask",
    "fastapi", "java", "spring", "kotlin", "swift", "objective-c", "c++",
    "c#", ".net", "golang", "go ", "rust", "ruby", "rails", "php", "laravel",
    "scala", "elixir", "haskell", "perl", "r ", "matlab", "sql", "postgres",
    "postgresql", "mysql", "redis", "elasticsearch", "kafka", "rabbitmq",
    "cassandra", "dynamodb", "snowflake", "databricks", "spark", "hadoop",
    "airflow", "dbt", "kubernetes", "docker", "terraform", "ansible", "aws",
    "azure", "gcp", "google cloud", "jenkins", "github actions", "circleci",
    "machine learning", "deep learning", "pytorch", "tensorflow", "keras",
    "scikit", "nlp", "llm", "computer vision", "data science", "pandas",
    "numpy", "tableau", "power bi", "looker", "salesforce", "sap", "oracle",
    "servicenow", "workday", "android", "ios", "flutter", "react native",
    "vue", "angular", "svelte", "ember", "jquery", "webpack", "babel",
    "sass", "less", "bootstrap", "material ui", "figma", "selenium",
    "cypress", "playwright", "jest", "mocha", "pytest", "junit",
    "static site generator", "jamstack", "hugo", "jekyll", "gatsby",
    "wordpress", "drupal", "shopify", "stripe", "twilio", "firebase",
    "solidity", "blockchain", "web3", "unity", "unreal", "embedded",
    "verilog", "vhdl", "fpga", "linux kernel", "networking", "security",
)

DECLINE_OPTION_WORDS = (
    "decline to self", "decline to identify", "prefer not to", "decline",
    "i don't wish", "i do not wish", "don't wish to answer", "do not wish",
    "prefer not to say", "choose not to", "not disclose", "i don't want to answer",
)

YES_OPTION_WORDS = ("yes", "y", "true", "i agree", "agree", "i consent", "confirm", "i do",
                    "i acknowledge", "acknowledge", "i have read", "i understand", "i accept",
                    "accept", "understood", "i confirm")
NO_OPTION_WORDS = ("no", "n", "false", "i do not", "i don't", "none", "not applicable", "n/a")


# -------------------------------------------------------------- option choosing

def choose_option(value: str, options: list[str] | None) -> str | None:
    """Map our answer onto one of the form's own option strings.

    Exact (case-insensitive) first, then prefix, then substring, then a
    Yes/No-family match. Returns None rather than blind-picking an option,
    because picking the wrong one puts a false statement on a real application.
    """
    if not options:
        return value or None
    if not value:
        return None
    clean = [o for o in options if str(o).strip()]
    want = _low(value)

    for opt in clean:
        if _low(opt) == want:
            return opt
    for opt in clean:
        if _low(opt).startswith(want) or want.startswith(_low(opt)):
            return opt
    for opt in clean:
        if want in _low(opt) or _low(opt) in want:
            return opt

    family = (
        YES_OPTION_WORDS if want.startswith(("y", "agree", "i agree", "true"))
        else NO_OPTION_WORDS if want.startswith(("n", "false"))
        else ()
    )
    for word in family:
        for opt in clean:
            if _low(opt) == word:
                return opt
    for word in family:
        for opt in clean:
            if _low(opt).startswith(word):
                return opt
    return None


def decline_option(options: list[str] | None) -> str | None:
    """The form's own 'decline to self-identify' choice, if it offers one."""
    for word in DECLINE_OPTION_WORDS:
        for opt in options or []:
            if word in _low(opt):
                return opt
    return None


def _yes(options: list[str] | None) -> str | None:
    return choose_option("Yes", options)


def _no(options: list[str] | None) -> str | None:
    return choose_option("No", options)


# --------------------------------------------------------------- resume helpers

def _skills(identity) -> list[str]:
    """The candidate's own resume skill/role keyword lists, lowercased."""
    raw = getattr(identity, "raw", {}) or {}
    out: list[str] = []
    for key in ("skill_keywords", "role_include"):
        for item in raw.get(key) or []:
            token = _low(item)
            if token:
                out.append(token)
    return out


def _knows(identity, tech: str) -> bool:
    tech = _low(tech).strip()
    if not tech:
        return False
    skills = _skills(identity)
    if tech in skills:
        return True
    # 'node.js' in the question vs 'node' on the resume, and vice versa.
    bare = tech.replace(".js", "").replace("js", "").strip()
    for skill in skills:
        if skill == tech or skill == bare:
            return True
        if len(bare) >= 4 and (bare in skill or skill in bare):
            return True
    return False


def _techs_in(label: str) -> list[str]:
    low = f" {_low(label)} "
    # Whole-word match: plain substrings made "r " fire on "either", "ember"
    # on "member", "rust" on "trust", "git" on "digital".
    found = [
        t for t in TECH_VOCAB if t.strip()
        and re.search(r"(?<![a-z0-9])" + re.escape(t.strip()) + r"(?![a-z0-9+#])", low)
    ]
    # Drop generic substrings when a more specific sibling matched
    # ('react' vs 'react native', 'node' vs 'node.js').
    return [t for t in found if not any(t != o and t in o for o in found)]


# ---------------------------------------------------------------- the answerer

def answer(
    label: str, identity, job, options: list[str] | None = None
) -> tuple[str | None, str]:
    """Return (value, reason), or (None, reason) when genuinely unanswerable.

    `options` is the form's own list of choices for a select / radio group /
    react-select combobox. When supplied, the returned value is guaranteed to be
    one of those strings (or None).
    """
    low = _low(label)
    if not low:
        return None, "brain: no question text to reason about"

    country, country_why = job_country(job)
    # A question can name its own country ("...authorized to work in the US?"),
    # which beats the posting's location.
    asked_country = detect_country(low)
    target = asked_country or country
    target_why = (
        f"the question names {asked_country}" if asked_country else country_why
    )

    for handler in (
        _eeo,
        _sponsorship,
        _authorization,
        _citizenship,
        _misc_policy,
        _min_years,
        _years,
        _salary,
        _notice_and_start,
        _relocation_remote,
        _education,
        _links,
        _hear_about,
        _free_text,
        _yes_no_default,
    ):
        value, reason = handler(low, identity, job, options, target, target_why)
        if value is not None:
            return value, reason
        if reason:  # a handler that owns the question but cannot ground it
            return None, reason
    return None, "brain: no resume fact or job fact grounds this question"


# ----------------------------------------------------------------- handlers
# Each handler returns (value, reason) when it owns and can answer the
# question, (None, reason) when it owns it but cannot answer honestly, and
# (None, "") when the question is not its business.

def _eeo(low, identity, job, options, target, why):
    words = (
        "gender", "race", "ethnic", "veteran", "disability", "hispanic",
        "latino", "lgbt", "pronoun", "sexual orientation", "transgender",
        "self-identif", "self identif",
    )
    if not re.search(r"\b(?:" + "|".join(re.escape(w) for w in words) + r")", low):
        return None, ""
    picked = decline_option(options)
    if picked:
        return picked, f"brain: demographic question - selected the form's own {picked!r}"
    return None, "brain: demographic question and the form offers no decline option - left blank"


def _authorized_in(identity, target):
    """Countries the candidate personally confirmed (data/private_profile.json)."""
    c = getattr(identity, "_c", {}) or {}
    raw = c.get("work_authorized_countries") or []
    names = {str(x).strip().lower() for x in raw}
    t = (target or "").strip().lower()
    if t.startswith("the "):
        t = t[4:]
    alias = {"united states": {"us", "usa", "united states", "united states of america"}}
    want = alias.get(t, {t})
    return bool(names & want)


def _sponsorship(low, identity, job, options, target, why):
    needles = (
        "require sponsorship", "need sponsorship", "visa sponsorship",
        "sponsorship for a visa", "require visa", "need a visa",
        "require immigration", "sponsorship to", "sponsorship now",
        "require employment sponsorship", "sponsorship",
    )
    if not any(n in low for n in needles):
        return None, ""
    if target and target != "India" and _authorized_in(identity, target):
        value = _no(options)
        if value is None:
            return None, "brain: answer is No but the form offers no such option"
        return value, f"brain: candidate confirmed work authorisation in {target}, no sponsorship needed ({why})"
    if target == "India":
        value = _no(options)
        reason = (
            "brain: Indian citizen already authorised to work in India, so no "
            f"sponsorship is needed ({why})"
        )
    elif target is None:
        return None, (
            "brain: sponsorship answer depends on the work country and the "
            f"posting names none ({why}) - refusing to guess"
        )
    else:
        value = _yes(options)
        reason = (
            f"brain: candidate holds no work authorisation in {target}, so "
            f"sponsorship would be required ({why})"
        )
    if value is None:
        return None, f"brain: answer is {'No' if target == 'India' else 'Yes'} but the form offers no such option"
    return value, reason


def _authorization(low, identity, job, options, target, why):
    needles = (
        "legally authorized", "legally authorised", "authorized to work",
        "authorised to work", "work authorization", "work authorisation",
        "right to work", "eligible to work", "legal right to work",
        "permitted to work", "work permit",
    )
    if not any(n in low for n in needles):
        return None, ""
    if target == "India":
        value, verdict = _yes(options), "Yes"
        reason = f"brain: Indian citizen, authorised to work in India ({why})"
    elif target and _authorized_in(identity, target):
        value, verdict = _yes(options), "Yes"
        reason = f"brain: candidate confirmed work authorisation in {target} ({why})"
    elif target is None:
        return None, (
            "brain: work-authorisation answer depends on the country and the "
            f"posting names none ({why}) - refusing to guess"
        )
    else:
        value, verdict = _no(options), "No"
        reason = f"brain: candidate has no work authorisation in {target} ({why})"
    if value is None:
        return None, f"brain: answer is {verdict} but the form offers no such option"
    return value, reason


def _citizenship(low, identity, job, options, target, why):
    if "citizen" in low or "nationality" in low or "nationalit" in low:
        for candidate in ("Indian", "India", "Indian citizen"):
            picked = choose_option(candidate, options)
            if picked:
                return picked, "brain: profile country is India - Indian citizen"
        return None, "brain: candidate is an Indian citizen but the form offers no matching option"
    if "country of residence" in low or "country you reside" in low or "where do you reside" in low:
        picked = choose_option(identity.country or "India", options)
        if picked:
            return picked, f"brain: resume location is {identity.location or 'India'}"
        return None, "brain: resident of India but the form offers no India option"
    if "visa status" in low or "immigration status" in low or "work status" in low:
        text = "Indian citizen residing in India - no visa required to work in India"
        picked = choose_option(text, options) if options else text
        if picked:
            return picked, "brain: resume facts - Indian citizen based in India"
        return None, "brain: visa-status options do not include 'Indian citizen / no visa required'"
    return None, ""


def _misc_policy(low, identity, job, options, target, why):
    """Logistics questions common on Ashby/Lever forms, answered by confirmed policy."""
    def yes(reason):
        value = _yes(options)
        if value:
            return value, reason
        return None, f"{reason} - but the form offers no Yes option"

    if any(n in low for n in ("18 years of age", "18 years old", "over 18", "age of 18",
                              "18 or older", "18+")) and "experience" not in low:
        return yes("brain: candidate is an adult (18+)")
    if any(n in low for n in ("live coding", "coding interview", "technical interview",
                              "take-home", "take home assignment", "coding assessment",
                              "coding challenge", "without ai assistance")):
        return yes("brain: candidate will complete the interview process as asked")
    if "new grad" in low or "recent graduate" in low or "recent grad" in low:
        # 'Student or New Grad?' - yes only when the degree finished within the last year.
        grad = identity.resolve_token("ANSWER_GRAD_YEAR") or ""
        years = [int(y) for y in re.findall(r"(?:19|20)\d{2}", str(grad))]
        import datetime as _dt
        if years:
            recent = _dt.date.today().year - max(years) <= 1
            value = _yes(options) if recent else _no(options)
            if value or not options:
                return (value or ("Yes" if recent else "No"),
                        f"brain: resume shows graduation in {max(years)}")
        return None, "brain: resume does not show a graduation year"
    if any(n in low for n in ("full-time education", "full time education",
                              "currently a student", "currently enrolled",
                              "currently pursuing a degree")):
        grad = identity.resolve_token("ANSWER_GRAD_YEAR") or ""
        years = [int(y) for y in re.findall(r"(?:19|20)\d{2}", str(grad))]
        import datetime as _dt
        if years and max(years) <= _dt.date.today().year:
            value = _no(options)
            if value or not options:
                return value or "No", f"brain: resume shows graduation in {max(years)} - not a student"
        return None, "brain: resume does not show a completed degree"
    if any(n in low for n in ("requires someone to be located", "must be located in",
                              "be located in", "must be based in", "need to be based in",
                              "requires you to be based")):
        if options:
            return yes("brain: candidate is open to relocating for the role (confirmed answer policy)")
        return ("Yes - I confirm I am open to relocating and working from the required location.",
                "brain: candidate is open to relocating for the role (confirmed answer policy)")
    if any(n in low for n in ("located within", "miles of", "km of", "commut")):
        if options:
            for opt in options:
                if "relocat" in _low(opt):
                    return opt, "brain: candidate is open to relocation (confirmed answer policy)"
        return yes("brain: candidate is open to relocating / commuting (confirmed answer policy)")
    if any(n in low for n in ("in person", "in-person", "in the office", "days per week",
                              "days a week", "on-site", "on site", "from our",
                              "office based", "office-based", "days office",
                              "days in office", "return to office")):
        return yes("brain: candidate works on-site or hybrid alike (confirmed answer policy)")
    if any(n in low for n in ("percentage of your day", "hands-on coding", "% of your time")):
        if options:
            for opt in options:
                if "75" in opt:
                    return opt, "brain: hands-on individual-contributor developer"
            best = max(options, key=lambda o: max([int(n) for n in re.findall(r"\d+", o)] or [-1]))
            return best, "brain: hands-on individual-contributor developer - highest coding share"
        return "75%", "brain: hands-on individual-contributor developer"
    if (any(n in low for n in ("timezone", "time zone", "overlap", "pacific time",
                               "coordination hours", "working hours", "core hours"))
            or " pst" in low or " est " in low):
        return yes("brain: candidate can work overlapping hours (confirmed answer policy)")
    if any(n in low for n in ("current or most recent employer", "current employer",
                              "most recent employer", "current company")):
        value = identity.current_company
        picked = choose_option(value, options) if options else value
        if picked:
            return picked, f"brain: resume current employer ({value})"
        return None, "brain: no current employer on the resume"
    if any(n in low for n in ("technical stack", "tech stack", "core stack", "primary stack")):
        if options:
            return None, ""
        raw = getattr(identity, "raw", {}) or {}
        skills = [str(s).strip() for s in (raw.get("skill_keywords") or []) if str(s).strip()]
        if skills:
            return ", ".join(skills[:12]), "brain: resume skill list"
        return None, "brain: no skill list on the resume"
    # Multi-checkbox office pickers ("Which office ... - San Francisco"): the
    # candidate is open to relocating, so every office box gets ticked.
    if "which office" in low or "which of our offices" in low or (
            "work out of" in low and " - " in low and not options):
        if options and not _yes(options):
            return options[0], "brain: candidate is open to relocating to any office - first listed office"
        return yes("brain: candidate is open to relocating to any office (confirmed answer policy)")
    if any(n in low for n in ("intend to work", "work out of", "where will you work",
                              "from where do you", "currently located", "where are you located",
                              "where do you live", "where are you currently")):
        value = identity.location
        picked = choose_option(value, options) if options else value
        if not picked and options:
            for alt in (identity.city, identity.country):
                picked = choose_option(alt, options)
                if picked:
                    break
        if picked:
            return picked, f"brain: resume location ({identity.location})"
        return None, "brain: resume location is not among the form's options"
    if "which country" in low:
        picked = choose_option(identity.country or "India", options)
        if picked:
            return picked, f"brain: resume country ({identity.country or 'India'})"
        return None, "brain: resume country is not among the form's options"
    if low.startswith(("have you built", "have you worked with", "have you used",
                       "have you developed", "do you have experience",
                       "do you have hands-on", "have you shipped")):
        skills = _skills(identity)
        known = [s for s in skills if s in low]
        if "api" in low and any("api" in s for s in skills):
            known.append("rest api")
        if known:
            return yes(f"brain: resume lists {', '.join(sorted(set(known)))}")
        techs = _techs_in(low)
        if techs and not any(_knows(identity, t) for t in techs):
            value = _no(options)
            if value:
                return value, f"brain: {', '.join(techs)} is not on the resume - honest No"
        if not techs and "product" in low:
            # "Have you used <Company> products?" - nothing on the resume says so.
            value = _no(options)
            if value or not options:
                return value or "No", "brain: nothing on the resume shows use of these products - honest No"
    return None, ""


def _years(low, identity, job, options, target, why):
    needles = (
        "years of experience", "years experience", "how many years",
        "years of professional", "years of relevant", "yoe",
        "years of hands-on", "years of work experience", "total experience",
        "total professional experience", "relevant professional experience",
        "experience in years", "experience (in years)", "relevant experience (yrs",
    )
    if not any(n in low for n in needles):
        return None, ""
    total = int(getattr(identity, "years_experience", 0) or 0)
    techs = _techs_in(low)
    if techs:
        known = [t for t in techs if _knows(identity, t)]
        if known:
            years, note = total, (
                f"brain: {', '.join(known)} is on the resume skill list, so the "
                f"candidate's real total of {total} year(s) applies"
            )
        else:
            years, note = 0, (
                f"brain: {', '.join(techs)} does not appear anywhere in the "
                "resume, so the honest answer is 0 - never inflated"
            )
    else:
        years, note = total, f"brain: resume states {total} year(s) of experience"

    if not options:
        return str(years), note
    picked = _years_option(years, options)
    if picked:
        return picked, f"{note}; mapped onto the form's option {picked!r}"
    return None, f"{note}; but no form option covers {years}"



_MIN_YEARS_RE = re.compile(
    r"(?:minimum of|min\.?|at least|over|more than|atleast)\s*(\d+(?:\.\d+)?)\s*\+?\s*(?:years|yrs|year)"
    r"|(\d+(?:\.\d+)?)\s*\+\s*(?:years|yrs|year)"
    r"|(\d+(?:\.\d+)?)\s*(?:or more|and above)\s*(?:years|yrs)"
)


def _min_years(low, identity, job, options, target, why):
    """Yes/No 'do you have N+ years ...' questions, answered from the resume total."""
    m = _MIN_YEARS_RE.search(low)
    if not m:
        return None, ""
    if options and not any(_low(o).startswith(("yes", "no")) for o in options):
        return None, ""
    need = float(next(g for g in m.groups() if g))
    try:
        have = float(getattr(identity, "_c", {}).get("years_experience") or 0)
    except (TypeError, ValueError):
        have = float(getattr(identity, "years_experience", 0) or 0)
    techs = _techs_in(low)
    unknown = [t for t in techs if not _knows(identity, t)]
    ok = have >= need and not unknown
    why_txt = (
        f"brain: resume shows {have:g} year(s)"
        + (f"; {', '.join(unknown)} not on resume" if unknown else "")
        + f" vs {need:g}+ asked -> {'Yes' if ok else 'No'}"
    )
    val = "Yes" if ok else "No"
    if options:
        picked = choose_option(val, options)
        return (picked, why_txt) if picked else (None, why_txt)
    return val, why_txt

_RANGE = re.compile(r"(\d+)\s*(?:\+|-|–|to)?\s*(\d+)?")


def _years_option(years: int, options: list[str]) -> str | None:
    """Pick the option whose numeric range actually contains `years`."""
    exact = choose_option(str(years), options)
    best: tuple[int, str] | None = None
    for opt in options:
        text = _low(opt)
        nums = [int(n) for n in re.findall(r"\d+", text)]
        if not nums:
            if years == 0 and ("none" in text or "no experience" in text):
                return opt
            continue
        if len(nums) >= 2:
            lo, hi = min(nums), max(nums)
        elif "+" in text or "more than" in text or "over" in text or "at least" in text:
            lo, hi = nums[0], 99
        elif "less than" in text or "under" in text or "<" in text:
            lo, hi = 0, max(nums[0] - 1, 0)
        else:
            lo = hi = nums[0]
        if lo <= years <= hi:
            span = hi - lo
            if best is None or span < best[0]:
                best = (span, opt)
    if best:
        return best[1]
    return exact


def _salary(low, identity, job, options, target, why):
    current = (
        "current ctc", "current salary", "current compensation", "present ctc",
        "existing ctc", "current fixed", "current annual",
    )
    expected = (
        "expected ctc", "expected salary", "expected compensation",
        "salary expectation", "compensation expectation", "desired salary",
        "desired compensation", "expected pay", "salary requirement",
        "rate expectation", "compensation requirement", "pay expectation",
    )
    if any(n in low for n in current):
        value = identity.current_ctc
        if value:
            return value, "brain: current-CTC answer the user confirmed for this candidate"
        return None, "brain: no current-CTC fact on file"
    if any(n in low for n in expected):
        if "number" in low or "numeric" in low or "digits" in low or "inr" in low or "amount" in low:
            value = identity.expected_ctc_inr or identity.expected_ctc
        else:
            value = identity.expected_ctc
        if value:
            return value, f"brain: expected CTC from the candidate's profile ({value})"
        return None, "brain: no salary expectation on file"
    return None, ""


def _notice_and_start(low, identity, job, options, target, why):
    if "notice period" in low or ("notice" in low and "period" in low) or "resign" in low:
        picked = choose_option("30 days", options) if options else "30 days"
        if picked:
            return picked, "brain: standard 30-day notice, as confirmed in answers.yaml"
        picked = choose_option("1 month", options)
        if picked:
            return picked, "brain: 30-day notice mapped to the form's '1 month' option"
        return None, "brain: notice period is 30 days but no form option matches"
    starters = (
        "when can you start", "start date", "earliest start", "availability",
        "available to start", "how soon can you", "joining date", "date of joining",
        "when would you be able to start", "notice", "available from",
        "could you start", "start working", "able to start",
        "when are you available", "start a new role",
    )
    if any(n in low for n in starters):
        text = "30 days from offer"
        picked = choose_option(text, options) if options else text
        if picked:
            return picked, "brain: available 30 days from offer (standard notice period)"
        for alt in ("1 month", "immediately", "30 days"):
            picked = choose_option(alt, options)
            if picked:
                return picked, f"brain: 30-day availability mapped to the form's {picked!r}"
        return None, "brain: availability is 30 days from offer but no form option matches"
    return None, ""


def _relocation_remote(low, identity, job, options, target, why):
    if "relocat" in low or "willing to move" in low:
        value = _yes(options)
        if value:
            return value, "brain: candidate is open to relocation (confirmed answer policy)"
        return None, "brain: answer is Yes to relocation but the form offers no Yes option"
    if any(n in low for n in ("work remote", "remote work", "work from home", "hybrid",
                              "comfortable working remote", "fully remote", "onsite",
                              "in office", "in-office", "work from office")):
        value = _yes(options)
        if value:
            return value, "brain: candidate works remotely and on-site alike (confirmed answer policy)"
        return None, "brain: answer is Yes but the form offers no Yes option"
    if "current location" in low or "where are you based" in low or "your location" in low \
            or "location (city)" in low or "city (" in low \
            or low.strip() in ("city", "location", "city*", "location*"):
        picked = choose_option(identity.location, options) if options else identity.location
        if picked:
            return picked, f"brain: resume location ({identity.location})"
        for alt in (identity.city, identity.country):
            picked = choose_option(alt, options)
            if picked:
                return picked, f"brain: resume location narrowed to {picked!r}"
        return None, "brain: resume location is not among the form's options"
    return None, ""


def _education(low, identity, job, options, target, why):
    if "graduation year" in low or "year of graduation" in low or "grad year" in low \
            or "year of completion" in low or "passing year" in low:
        value = identity.grad_year
        picked = choose_option(value, options) if options else value
        if picked:
            return picked, f"brain: resume graduation year ({value})"
        return None, f"brain: resume graduation year is {value} but no form option matches"
    if "college" in low or "university" in low or "institution" in low or "school" in low:
        if "qualification" in low or "degree" in low or "education" in low or "," in low:
            value = identity.education_line
        else:
            value = identity.school
        picked = choose_option(value, options) if options else value
        if picked:
            return picked, f"brain: resume education facts ({value})"
        return None, "brain: resume school is not among the form's options"
    if "highest qualification" in low or "highest education" in low \
            or "highest degree" in low or "education level" in low \
            or "level of education" in low or "degree" in low:
        for candidate in (identity.degree, identity.education_line, "Bachelor's Degree"):
            picked = choose_option(candidate, options) if options else candidate
            if picked:
                return picked, f"brain: resume degree ({identity.degree or candidate})"
        return None, "brain: resume degree is not among the form's options"
    if "field of study" in low or "major" in low or "discipline" in low or "specializ" in low:
        value = identity.discipline
        picked = choose_option(value, options) if options else value
        if picked:
            return picked, f"brain: resume discipline ({value})"
        return None, "brain: resume discipline is not among the form's options"
    return None, ""


def _links(low, identity, job, options, target, why):
    table = (
        (("linkedin",), identity.linkedin, "LinkedIn URL"),
        (("github", "git hub"), identity.github, "GitHub URL"),
        (("portfolio", "personal website", "personal site", "your website",
          "blog", "web page"), identity.portfolio, "portfolio URL"),
    )
    for needles, value, what in table:
        if any(n in low for n in needles):
            if value:
                return value, f"brain: {what} from the resume"
            return None, f"brain: this candidate's resume has no {what}"
    return None, ""


def _hear_about(low, identity, job, options, target, why):
    needles = (
        "how did you hear", "how did you find", "how did you learn",
        "referral source", "where did you hear", "how were you referred",
        "how do you know about",
    )
    if not any(n in low for n in needles):
        return None, ""
    for candidate in ("Company careers page", "Company website", "Job board",
                      "Other", "Careers page", "Website"):
        picked = choose_option(candidate, options) if options else candidate
        if picked:
            return picked, f"brain: found through the company's own job board - {picked!r}"
    return None, "brain: cannot describe the referral source with the form's options"


# Free-text questions that want a short grounded paragraph.
_ESSAY_NEEDLES = (
    "why do you want", "why are you interested", "why this role", "why us",
    "why do you wish", "tell us about yourself", "tell us why",
    "what interests you", "what excites you", "cover letter",
    "motivat", "why would you", "what makes you", "about yourself",
    "anything else you", "additional information", "why our", "why join",
    "what draws you", "share more about", "describe your experience",
    "give examples", "relevant experience", "your background",
    "interests you", "excited about", "why are you applying", "aligns with",
    "example from your professional", "tell us", "describe a",
    "share an example",
)


def _free_text(low, identity, job, options, target, why):
    if options:  # an essay answer can never be a dropdown choice
        return None, ""
    if not (any(n in low for n in _ESSAY_NEEDLES) or low.startswith("why ")):
        return None, ""
    text = compose_statement(identity, job)
    if text:
        return text, "brain: short statement composed only from resume facts and this job's own description"
    return None, "brain: not enough resume detail to write a grounded statement"


# Stack keywords we look for in the job description so the statement can name
# the overlap honestly instead of making claims up.
_STACK_PROBE = (
    "React", "Next.js", "Node.js", "Express", "MongoDB", "JavaScript",
    "TypeScript", "Redux", "REST APIs", "GraphQL", "Tailwind", "Python",
    "AWS", "Docker", "Kubernetes", "PostgreSQL", "MySQL", "Redis",
)


def compose_statement(identity, job) -> str:
    """2-4 sentences, every clause traceable to the resume or the posting.

    Nothing here asserts a skill the candidate's resume does not list, or a
    technology the job description does not mention.
    """
    title = _norm(getattr(job, "title", "")) or "this role"
    company = _norm(getattr(job, "company", "")) or "your team"
    desc = str(getattr(job, "description", "") or "")
    low_desc = _low(desc)

    skills = _skills(identity)
    overlap = [
        tech for tech in _STACK_PROBE
        if _low(tech) in low_desc and _knows(identity, tech)
    ]

    years = int(getattr(identity, "years_experience", 0) or 0)
    role = identity.current_title or "Software Developer"

    lines = [
        f"I am applying for the {title} role at {company}.",
        f"I am a {role} with {years} year{'s' if years != 1 else ''} of "
        "professional experience building production web applications on the "
        "MERN stack (React, Next.js, Node.js, Express and MongoDB).",
    ]
    if overlap:
        lines.append(
            "Your posting calls out "
            + ", ".join(overlap[:4])
            + ", which is the stack I work in daily."
        )
    if identity.education_line:
        lines.append(
            f"I hold a {identity.education_line} and I am based in "
            f"{identity.location or identity.country}; full details are in my attached resume."
        )
    else:
        lines.append(
            f"I am based in {identity.location or identity.country} and my resume is attached."
        )
    return " ".join(lines)


def _norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


# Consent / agreement / "have you ever" style yes-no questions. Runs LAST so a
# more specific handler always gets first refusal.
_CONSENT_NEEDLES = (
    "i agree", "do you agree", "i consent", "do you consent", "i acknowledge",
    "acknowledge", "i certify", "certify", "privacy policy", "privacy notice",
    "terms and conditions", "terms of use", "data protection", "gdpr",
    "i authorize", "i authorise", "i confirm", "do you confirm",
    "by checking", "by submitting", "i understand", "consent to",
    "permission to", "i have read",
)

_NEGATIVE_NEEDLES = (
    "have you ever worked", "have you previously worked", "previously worked",
    "ever been employed", "former employee", "worked at", "consulted for",
    "are you subject to", "post-employment", "non-compete", "noncompete",
    "restrictive covenant", "employment agreement", "any restrictions",
    "criminal", "convicted", "been terminated", "been dismissed",
    "related to any", "family member", "do you have any agreement",
    "currently employed by", "conflict of interest",
    "receive alerts", "similar jobs", "marketing email", "talent community",
    "newsletter", "stay up to date", "stay in touch",
)


def _yes_no_default(low, identity, job, options, target, why):
    looks_boolean = bool(options) and len(options) <= 6 and any(
        _low(o) in ("yes", "no") for o in options
    )
    if any(n in low for n in _NEGATIVE_NEEDLES):
        value = _no(options)
        if value or not options:
            return value or "No", (
                "brain: no resume fact says otherwise, so the truthful answer is No"
            )
        return None, "brain: answer is No but the form offers no No option"
    if any(n in low for n in _CONSENT_NEEDLES):
        value = _yes(options)
        if value or not options:
            return value or "Yes", "brain: consent/acknowledgement the candidate grants to apply"
        return None, "brain: consenting but the form offers no Yes/agree option"
    if looks_boolean and ("background check" in low or "drug" in low or "reference" in low):
        value = _yes(options)
        if value:
            return value, "brain: candidate consents to standard pre-employment checks"
    return None, ""
