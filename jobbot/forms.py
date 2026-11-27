"""Semantic form understanding and filling.

Design rule enforced here: every value written into a form comes from
`identity.Identity` (resume facts) or from `config/answers.yaml` (answers the
user explicitly confirmed). Nothing is ever invented. If a field cannot be
resolved from those two sources it is SKIPPED and reported as skipped.

The three ATS platforms expose fields completely differently, so matching is
label-text-driven with per-platform id/name fast-paths:
  * Greenhouse - no `name` attributes; ids `#first_name #last_name #email
    #phone` and `#question_<numeric>` for custom questions.
  * Ashby - `#_systemfield_name`, `#_systemfield_email`, other fields are
    per-posting UUID ids carrying readable <label> text.
  * Lever - clean names: `name email phone org location urls[LinkedIn]
    urls[GitHub]`, custom `cards[<uuid>]`.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field as dc_field
from typing import Any

from playwright.sync_api import Locator, Page

from .config import load_answers
from .identity import Identity
from . import brain

SKIP = "__SKIP__"

# Demographic / EEO questions: deliberately never answered.
EEO_WORDS = (
    "gender", "race", "ethnic", "veteran", "disability", "hispanic", "latino",
    "lgbt", "pronoun", "sexual orientation", "transgender", "self-identif",
    "self identif", "protected veteran",
)
# Leading word boundary so "race" does not fire inside "embrace"/"grace".
_EEO_RE = re.compile(r"\b(?:" + "|".join(re.escape(w) for w in EEO_WORDS) + r")")

# Honest escape hatch for EEO dropdowns the board marks as required.
DECLINE_SENTINEL = "__DECLINE__"
DECLINE_WORDS = (
    "decline to self", "decline to identify", "decline", "prefer not to",
    "i don't wish", "i do not wish", "don't wish to answer", "not disclose",
    "prefer not to say", "i don't want to answer", "choose not to",
)

# "If other, please specify" style fields only apply when an earlier answer
# selected 'other'. Dumping a cover letter into them is wrong, so they are
# left blank unless the form itself marks them required.
CONDITIONAL_WORDS = (
    "if other", "if yes", "if no,", "if so", "if applicable", "if any",
    "please specify", "if selected other", "if you selected", "other (please",
)


def is_conditional(label: str) -> bool:
    low = _lower(label)
    return any(w in low for w in CONDITIONAL_WORDS)

# label substrings -> semantic key. Order matters; first hit wins.
SEMANTICS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("first_name", ("first name", "given name", "firstname", "legal first")),
    ("last_name", ("last name", "family name", "surname", "lastname", "legal last")),
    ("full_name", ("full name", "your name", "candidate name", "legal name")),
    ("email", ("email", "e-mail")),
    ("phone", ("phone", "mobile", "contact number", "telephone", "cell")),
    ("linkedin", ("linkedin",)),
    ("github", ("github", "git hub")),
    ("portfolio", ("portfolio", "website", "personal site", "blog", "url", "web page")),
    ("company", ("current company", "current employer", "current organization",
                 "current organisation", "employer", "company")),
    ("title", ("current title", "current role", "job title", "your title",
               "current designation", "designation", "current position")),
    ("location", ("location", "city", "where are you based", "current city")),
    ("country", ("country",)),
    ("school", ("school", "university", "college", "institution")),
    ("degree", ("degree",)),
    ("discipline", ("discipline", "major", "field of study")),
    ("grad_year", ("graduation", "grad year", "year of completion", "end date")),
    # Bare "name" only after school/company/etc. so "School Name" is a school.
    ("full_name", ("name",)),
    ("years", ("years of experience", "how many years", "total experience", "yoe",
               "total professional experience", "relevant professional experience",
               "total work experience", "work experience (excluding",
               "experience in years", "experience (in years)")),
    ("resume", ("resume", "cv", "attach", "upload")),
    ("cover_letter", ("cover letter", "why do you want", "why are you interested",
                      "tell us about", "what interests you", "what about",
                      "motivat", "additional information", "anything else")),
)

IDENTITY_KEYS = {
    "first_name", "last_name", "full_name", "email", "phone", "linkedin",
    "github", "portfolio", "company", "title", "location", "country",
    "school", "degree", "discipline", "grad_year", "years",
}

IDENTITY_TOKEN = {
    "first_name": "ANSWER_FIRST", "last_name": "ANSWER_LAST",
    "full_name": "ANSWER_NAME", "email": "ANSWER_EMAIL",
    "phone": "ANSWER_PHONE", "linkedin": "ANSWER_LINKEDIN",
    "github": "ANSWER_GITHUB", "portfolio": "ANSWER_PORTFOLIO",
    "company": "ANSWER_COMPANY", "title": "ANSWER_TITLE",
    "location": "ANSWER_LOCATION", "country": "ANSWER_COUNTRY",
    "school": "ANSWER_SCHOOL", "degree": "ANSWER_DEGREE",
    "discipline": "ANSWER_DISCIPLINE", "grad_year": "ANSWER_GRAD_YEAR",
    "years": "ANSWER_YEARS",
}

YES_WORDS = ("yes", "yeah", "i do", "true", "agree", "i agree", "confirm")
NO_WORDS = ("no", "none", "i do not", "i don't", "false", "not required")

CAPTCHA_HINTS = (
    "iframe[src*='recaptcha']",
    "iframe[src*='hcaptcha']",
    "iframe[title*='challenge']",
    "[class*='h-captcha']",
    "[class*='g-recaptcha']",
    "iframe[src*='turnstile']",
    "iframe[src*='challenges.cloudflare.com']",
    "[class*='cf-turnstile']",
)

PAY_RE = re.compile(r"\b(ctc|salary|compensation|cost to company|remuneration|pay expectation|stipend)\b")


@dataclass
class FieldPlan:
    """One form control, what we think it is, and what we will do with it."""
    kind: str                 # text | textarea | select | file | checkbox | radio | combobox
    label: str
    semantic: str             # semantic key or "question"
    required: bool
    value: str | None
    action: str               # fill | skip
    reason: str = ""
    locator: Any = None
    meta: dict[str, Any] = dc_field(default_factory=dict)


def _norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _lower(text: str | None) -> str:
    return _norm(text).lower().replace("*", "").replace("\u2217", "").strip()


def is_eeo(label: str) -> bool:
    low = _lower(label)
    return bool(_EEO_RE.search(low))


def classify(label: str, name: str = "", el_id: str = "") -> str:
    """Map a control to a semantic key using label text, then name/id."""
    if is_eeo(label):
        return "eeo"
    hay = _lower(label)
    # Pay questions ("Total Cost to Company", "CTC") must reach the answer rules,
    # never the identity "company" field.
    if PAY_RE.search(hay):
        return "question"
    long_question = len(hay.split()) > 6
    for key, needles in SEMANTICS:
        hits = [n for n in needles if n in hay]
        # 'Let the company know about your interest...' is a message box, not
        # the current-employer field: bare 'company'/'employer' only counts in
        # a short label.
        if key == "company" and long_question and set(hits) <= {"company", "employer"}:
            continue
        if hits:
            return key
    attr = _lower(f"{name} {el_id}").replace("_", " ").replace("-", " ")
    for key, needles in SEMANTICS:
        if any(n in attr for n in needles):
            return key
    return "question"


def _answer_rules(label: str, answers: dict) -> str | None:
    """First matching rule from answers.yaml wins (ordered by the user)."""
    low = _lower(label)
    if not low:
        return None
    for rule in answers.get("rules") or []:
        for needle in rule.get("match") or []:
            if str(needle).lower() in low:
                return str(rule.get("answer", ""))
    return None


_EDU_DATE_RE = re.compile(r"^(start|end) date (month|year)$")
_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")


def _edu_date_answer(label: str, identity: Identity, options: list[str] | None) -> str | None:
    """Greenhouse education block: 'Start date month/year', 'End date month/year'.

    These used to collide with the 'start date' (notice) rule and the
    grad_year semantic. Derive them from the graduation year: a 4-year degree
    starting in August and ending in May of the graduation year.
    """
    m = _EDU_DATE_RE.match(_lower(label))
    if not m:
        return None
    grad = identity.resolve_token("ANSWER_GRAD_YEAR") or ""
    if not str(grad).strip().isdigit():
        return None
    grad_y = int(str(grad).strip())
    which, part = m.group(1), m.group(2)
    if part == "year":
        value = str(grad_y - 4 if which == "start" else grad_y)
    else:
        value = "August" if which == "start" else "May"
    if options:
        picked = brain.choose_option(value, options)
        if not picked and part == "month":
            num = str(_MONTHS.index(value) + 1)
            picked = next((o for o in options if o.strip() in (num, num.zfill(2))), None)
        return picked
    return value


_RESIDE_RE = re.compile(
    r"\b(?:residing|reside|resident|based|living|located|staying|currently)\s+(?:in|at|out of)\s+([a-z][a-z .()/-]{2,40})",
    re.I,
)
_HOME_WORDS = ("gurugram", "gurgaon", "delhi", "ncr", "new delhi", "noida", "india")


def _claims_other_city(option: str, identity: Identity) -> bool:
    """True when an option asserts the candidate lives somewhere they do not."""
    low = option.lower()
    if re.search(r"\b(outside|not (?:residing|based|living)|relocat)", low) and not _RESIDE_RE.search(
        low.replace("outside", "")
    ):
        return False
    m = _RESIDE_RE.search(low)
    if not m:
        return False
    place = m.group(1)
    if "outside" in low[: m.start() + 20]:
        return False
    home = " ".join(
        str(getattr(identity, a, "") or "") for a in ("location", "city")
    ).lower()
    homes = [w for w in _HOME_WORDS if w in home] or [home]
    return not any(h and h in place for h in homes) and not any(h in place for h in ("india", "remote", "anywhere"))


def _relocate_option(options: list[str]) -> str | None:
    for o in options:
        lo = o.lower()
        if ("outside" in lo or "not based" in lo or "not residing" in lo) and "relocat" in lo:
            return o
    for o in options:
        if "relocat" in o.lower() and "not" not in o.lower():
            return o
    return None


def resolve_value(
    label: str, semantic: str, identity: Identity, answers: dict, job=None,
    options: list[str] | None = None,
) -> tuple[str | None, str]:
    """resolve_value_raw plus a truthfulness guard on residence claims."""
    value, reason = _resolve_value_raw(label, semantic, identity, answers, job, options)
    if value and options and _claims_other_city(value, identity):
        alt = _relocate_option(options)
        if alt:
            return alt, "lives elsewhere - picked the willing-to-relocate option"
        return None, f"option '{value}' would claim a residence the resume does not have - skipped"
    return value, reason


def _resolve_value_raw(
    label: str, semantic: str, identity: Identity, answers: dict, job=None,
    options: list[str] | None = None,
) -> tuple[str | None, str]:
    """Return (value, reason). value None means skip -- never a guess.

    `options` is the control's own list of choices (select / radio / button
    group). When given, the returned value is always one of those strings.
    """
    if is_eeo(label):
        return None, "EEO/demographic field - intentionally not answered"

    edu = _edu_date_answer(label, identity, options)
    if edu is not None:
        return edu, "education dates derived from graduation year"

    if semantic in IDENTITY_KEYS and not options:
        token = IDENTITY_TOKEN[semantic]
        value = identity.resolve_token(token)
        if value:
            return value, f"resume field ({semantic})"
        return None, f"not present in resume ({semantic}) - skipped, never faked"

    # Answers the user confirmed on the dashboard for this exact question.
    try:
        from . import learn
        taught = learn.lookup(getattr(identity, "id", ""), label)
    except Exception:
        taught = None
    if taught:
        if options:
            picked = brain.choose_option(taught, options)
            if picked:
                return picked, "answer you confirmed on the dashboard"
        else:
            return taught, "answer you confirmed on the dashboard"

    rule_answer = _answer_rules(label, answers)
    if rule_answer == "ANSWER_SKIP":
        return None, "configured as skip"
    # ANSWER_BRAIN must be checked BEFORE the generic ANSWER_ token lookup:
    # resolve_token('ANSWER_BRAIN') is None, which silently skipped every
    # sponsorship / authorization / years / location question.
    if rule_answer == "ANSWER_BRAIN":
        value, reason = brain.answer(label, identity, job, options or [])
        if value is not None:
            return value, reason
        return None, reason or f"brain could not ground {semantic}"
    if rule_answer:
        if rule_answer.startswith("ANSWER_"):
            resolved = identity.resolve_token(rule_answer)
            if resolved:
                if options:
                    picked = brain.choose_option(resolved, options)
                    if picked:
                        return picked, f"resume field via rule ({rule_answer})"
                else:
                    return resolved, f"resume field via rule ({rule_answer})"
            else:
                return None, f"rule needs {rule_answer} but resume lacks it - skipped"
        elif options:
            picked = brain.choose_option(rule_answer, options)
            if picked:
                return picked, "confirmed answer from answers.yaml"
        else:
            return rule_answer, "confirmed answer from answers.yaml"

    if semantic in IDENTITY_KEYS and options:
        token = IDENTITY_TOKEN[semantic]
        value = identity.resolve_token(token)
        picked = brain.choose_option(value, options) if value else None
        if picked:
            return picked, f"resume field ({semantic})"

    value, reason = brain.answer(label, identity, job, options or [])
    if value is not None:
        return value, reason

    return None, reason or "no resume fact or confirmed answer matches this question"


def cover_letter_text(identity: Identity, answers: dict, title: str, company: str) -> str:
    template = answers.get("cover_letter_template") or ""
    if not template:
        return ""
    try:
        return template.format(
            title=title,
            company=company,
            current_title=identity.current_title or "Software Developer",
            years=identity.years_experience,
            full_name=identity.full_name,
            email=identity.email,
            phone=identity.phone_display or identity.phone,
        ).strip()
    except Exception:
        return ""


def label_for(page: Page, el: Locator) -> str:
    """Best-effort human label: <label for>, aria-label, placeholder, ancestors."""
    try:
        text = el.evaluate(
            """
            (node) => {
              const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
              // SmartRecruiters screening questions live inside shadow roots of
              // sr-question-field-* hosts; the question is that host's first line.
              if (node.getRootNode && node.getRootNode() !== document) {
                let a = node;
                for (let i = 0; i < 30 && a; i++) {
                  a = a.parentElement || (a.getRootNode && a.getRootNode().host) || null;
                  if (!a || !a.tagName) break;
                  if (/^SR-QUESTION-FIELD/.test(a.tagName)) {
                    const lines = (a.innerText || a.textContent || '').split(/\\n+/)
                      .map(clean).filter((s) => s && s !== '*');
                    const q = (lines[0] || '').replace(/^\\d+\\.\\s*/, '').replace(/\\s*\\*$/, '');
                    if (q) return q;
                    break;
                  }
                }
              }
              if (node.id) {
                const esc = (window.CSS && CSS.escape) ? CSS.escape(node.id) : node.id;
                const lab = document.querySelector(`label[for="${esc}"]`);
                if (lab && clean(lab.innerText)) return clean(lab.innerText);
              }
              const aria = node.getAttribute('aria-label');
              if (clean(aria)) return clean(aria);
              const labelledby = node.getAttribute('aria-labelledby');
              if (labelledby) {
                const parts = labelledby.split(/\\s+/)
                  .map((id) => document.getElementById(id) || (node.getRootNode && node.getRootNode().getElementById ? node.getRootNode().getElementById(id) : null))
                  .filter(Boolean)
                  .map((n) => clean(n.innerText));
                if (clean(parts.join(' '))) return clean(parts.join(' '));
              }
              // Workable: <span id="<name>_label"> holds the question for value inputs.
              if (node.name) {
                const wl = document.getElementById(node.name + '_label');
                if (wl && clean(wl.innerText)) return clean(wl.innerText);
              }
              let up = node.closest('label');
              if (up && clean(up.innerText)) return clean(up.innerText);
              let p = node.parentElement;
              for (let i = 0; i < 4 && p; i++, p = p.parentElement) {
                const lab = p.querySelector('label, legend, .label, [class*="label"]');
                if (lab && clean(lab.innerText)) return clean(lab.innerText);
              }
              const ph = node.getAttribute('placeholder');
              if (clean(ph)) return clean(ph);
              return clean(node.getAttribute('name') || node.id || '');
            }
            """
        )
        return _norm(text)[:300]
    except Exception:
        return ""


def _attrs(el: Locator) -> dict[str, str]:
    try:
        return el.evaluate(
            """
            (n) => ({
              tag: n.tagName.toLowerCase(),
              type: (n.getAttribute('type') || '').toLowerCase(),
              name: n.getAttribute('name') || '',
              id: n.id || '',
              sig: n.getAttribute('data-sr-id') || n.getAttribute('data-ui') || n.getAttribute('data-qa') || '',
              required: (n.required || n.getAttribute('aria-required') === 'true'
                || (() => { const m = /^input_(.+)_input$/.exec(n.id || '');
                     const h = m ? document.querySelector(`[name="${m[1]}"]`) : null;
                     return !!(h && (h.required || h.getAttribute('aria-required') === 'true')); })()) ? '1' : '',
              // Workable keeps a combobox's value in a second, id-less input
              // named like the question; the visible input_<name>_input is the
              // real control, so the holder must not be planned separately.
              holder: (!n.id && n.name && document.getElementById('input_' + n.name + '_input')) ? '1' : '',
              role: n.getAttribute('role') || '',
              value: n.value || '',
              visible: (n.offsetParent !== null || n.getClientRects().length > 0) ? '1' : '',
              readonly: n.readOnly ? '1' : '',
              haspopup: n.getAttribute('aria-haspopup') || '',
              autocomplete: n.getAttribute('autocomplete') || '',
            })
            """
        ) or {}
    except Exception:
        return {}


# Ashby (and some other React ATS) render every question as a field-entry div.
# The question text lives in the entry's heading <label>, and REQUIRED is shown
# only by a `_required_` class on that heading - the inputs themselves carry no
# required / aria-required attribute. Yes/No questions are two <button>s over a
# HIDDEN checkbox. This helper reads all of that in one round-trip.
_FIELD_CONTEXT_JS = """
(node) => {
  const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const out = {question: '', required: false, buttons: [], options: [], ashby: false};
  if (node.tagName === 'SELECT') {
    out.options = Array.from(node.options).map((o) => clean(o.text)).filter(Boolean);
  }
  // SmartRecruiters: shadow-DOM question hosts (sr-question-field-*).
  if (node.getRootNode && node.getRootNode() !== document) {
    let a = node;
    for (let i = 0; i < 30 && a; i++) {
      a = a.parentElement || (a.getRootNode && a.getRootNode().host) || null;
      if (!a || !a.tagName) break;
      if (/^SR-QUESTION-FIELD/.test(a.tagName)) {
        const raw = a.innerText || a.textContent || '';
        const lines = raw.split(/\\n+/).map(clean).filter((s) => s && s !== '*');
        out.question = (lines[0] || '').replace(/^\\d+\\.\\s*/, '').replace(/\\s*\\*$/, '');
        out.required = /\\*/.test(raw.slice(0, 400)) || node.getAttribute('aria-required') === 'true' || !!node.required;
        if (node.type === 'radio' && node.name) {
          const rs = node.getRootNode().querySelectorAll(`input[type=radio][name="${node.name}"]`);
          out.options = Array.from(rs).map((r) => {
            const l = r.closest('label') || (r.id && node.getRootNode().querySelector(`label[for="${r.id}"]`));
            return clean(l ? l.innerText : r.value);
          }).filter(Boolean);
        }
        return out;
      }
    }
  }
  const entry = node.closest('.ashby-application-form-field-entry, [class*="_fieldEntry"]');
  if (entry) {
    out.ashby = true;
    const head = entry.querySelector('[class*="_heading_"]') || entry.querySelector('label');
    if (head) {
      out.question = clean(head.innerText);
      out.required = /required/i.test(head.className || '') || /\\*\\s*$/.test(head.innerText || '');
    }
    out.buttons = Array.from(entry.querySelectorAll('button'))
      .filter((b) => b.offsetParent !== null || b.getClientRects().length > 0)
      .map((b) => clean(b.innerText)).filter(Boolean);
  } else {
    const fs = node.closest('fieldset');
    if (fs) {
      const lg = fs.querySelector('legend');
      if (lg) {
        out.question = clean(lg.innerText);
        out.required = /\\*\\s*$/.test(lg.innerText || '');
      } else {
        // Workable: fieldset without legend; options are [role=radio] wrappers
        // whose aria-labelledby starts with the question span id.
        const opt = fs.querySelector('[role=radio][aria-labelledby], [aria-labelledby]');
        const qid = opt ? (opt.getAttribute('aria-labelledby') || '').split(/\\s+/)[0] : '';
        const qn = qid ? document.getElementById(qid) : null;
        if (qn && clean(qn.innerText)) {
          out.question = clean(qn.innerText);
          out.required = /\\*\\s*$/.test(qn.innerText || '') || fs.getAttribute('aria-required') === 'true'
            || !!(qn.parentElement && /required|\\*/.test(qn.parentElement.innerText.slice(-3)));
        }
      }
    }
    // Lever: <li class="application-question"><div class="application-label"><div class="text">Q<span class="required">*</span>
    if (!out.question) {
      const lq = node.closest('.application-question, li.application-question, .custom-question');
      if (lq) {
        const lab = lq.querySelector('.application-label .text, .application-label, .text');
        if (lab && !lab.contains(node)) {
          out.question = clean(lab.innerText);
          out.required = !!lq.querySelector('.required') || /[\u2731*]\\s*$/.test(lab.innerText || '');
        }
      }
    }
  }
  if (node.type === 'radio' && node.name) {
    const scope = entry || node.closest('fieldset') || node.closest('.application-question, .custom-question') || document;
    const esc = (window.CSS && CSS.escape) ? CSS.escape(node.name) : node.name;
    out.options = Array.from(scope.querySelectorAll(`input[type=radio][name="${esc}"]`))
      .map((r) => {
        let t = '';
        if (r.id) {
          const l = document.querySelector(`label[for="${CSS.escape(r.id)}"]`);
          if (l) t = clean(l.innerText);
        }
        if (!t && r.closest('label')) t = clean(r.closest('label').innerText);
        return t || clean(r.value);
      }).filter(Boolean);
  }
  return out;
}
"""


def field_context(el: Locator) -> dict[str, Any]:
    try:
        return el.evaluate(_FIELD_CONTEXT_JS) or {}
    except Exception:
        return {}


ENTRY_XPATH = (
    "xpath=ancestor::div[contains(@class,'ashby-application-form-field-entry') "
    "or contains(@class,'_fieldEntry')][1]"
)


SKIP_INPUT_TYPES = {"hidden", "submit", "button", "image", "reset", "search"}


# Hosts that serve a real, fillable ATS application form. Some employers
# (MongoDB is the proven case) redirect the ATS job URL to their own careers
# site and embed the actual Greenhouse/Lever/Ashby form inside an <iframe>.
# Querying only the main frame finds site chrome (search box, scrollbar) and
# zero form fields, so the form layer has to pick the right frame first.
FORM_FRAME_HOSTS = (
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "job_app",
    "workable.com",
    "smartrecruiters.com",
    "recruitee.com",
)

FRAME_BLOCKLIST = ("recaptcha", "hcaptcha", "googleapis", "gstatic", "doubleclick")


def _fillable_count(ctx) -> int:
    try:
        return ctx.locator(
            "input:not([type=hidden]):not([type=submit]):not([type=button]), "
            "textarea, select"
        ).count()
    except Exception:
        return 0


def form_container(page: Page):
    """Return the Page or Frame that actually holds the application form.

    Prefers an ATS-hosted iframe when the main document has almost nothing to
    fill; otherwise stays on the main page. Never returns a captcha frame.
    """
    main_count = _fillable_count(page)
    best, best_count, best_url = None, 0, ""
    try:
        frames = list(page.frames)
    except Exception:
        frames = []

    for frame in frames:
        url = (frame.url or "").lower()
        if not url or url == "about:blank":
            continue
        if any(bad in url for bad in FRAME_BLOCKLIST):
            continue
        if frame is page.main_frame:
            continue
        count = _fillable_count(frame)
        ats = any(host in url for host in FORM_FRAME_HOSTS)
        # An ATS iframe wins on a small edge; a random iframe needs to be much richer.
        weight = count * (3 if ats else 1)
        if count >= 3 and weight > best_count:
            best, best_count, best_url = frame, weight, frame.url

    if best is not None and best_count > main_count:
        return best, f"form found in embedded frame: {best_url[:110]}"
    return page, "form on main page"



def _auto_answer(label, identity, job, options, reason):
    """A required question nothing grounds: decide a positive answer so the
    form still completes (user's standing instruction). EEO stays untouched."""
    try:
        value, why = brain.auto_decide(label, identity, job, options)
    except Exception as exc:  # never let the fallback break planning
        return None, f"{reason}; auto-decide failed: {exc}"
    if not value:
        return None, reason
    return value, f"{why} (was: {reason or 'no grounded answer'})"


def plan_fields(
    page: Page, identity: Identity, title: str = "", company: str = "", job=None
) -> list[FieldPlan]:
    """Walk every control on the page and decide fill-or-skip for each."""
    answers = load_answers()
    plans: list[FieldPlan] = []
    seen_radio_groups: set[str] = set()
    seen_button_groups: set[str] = set()
    auto_checked_groups: set[str] = set()

    controls = page.locator(
        "form input, form textarea, form select, form [role='combobox'], "
        "input, textarea, select"
    )
    try:
        count = min(controls.count(), 160)
    except Exception:
        return plans

    for i in range(count):
        el = controls.nth(i)
        a = _attrs(el)
        if not a:
            continue
        tag, ftype = a.get("tag", ""), a.get("type", "")
        if tag == "input" and ftype in SKIP_INPUT_TYPES:
            continue
        if a.get("holder"):
            continue
        if ftype == "file":
            kind = "file"
        elif tag == "textarea":
            kind = "textarea"
        elif tag == "select":
            kind = "select"
        elif ftype == "checkbox":
            kind = "checkbox"
        elif ftype == "radio":
            kind = "radio"
        elif a.get("role") == "combobox" or a.get("haspopup") in ("listbox", "true"):
            kind = "combobox"
        else:
            kind = "text"

        ctx = field_context(el) if kind != "file" else {}
        is_ashby = bool(ctx.get("ashby"))
        name, el_id = a.get("name", ""), a.get("id", "")

        # Ashby Yes/No question: two visible buttons over a HIDDEN checkbox.
        # The hidden checkbox is what the form validates, so it must not be
        # dropped by the visibility filter below - we answer by clicking.
        buttons = [b for b in (ctx.get("buttons") or []) if len(b) <= 40]
        if kind == "checkbox" and not a.get("visible") and is_ashby and len(buttons) >= 2:
            question = ctx.get("question") or label_for(page, el)
            key = f"{question}|{name}"
            if key in seen_button_groups:
                continue
            seen_button_groups.add(key)
            required = bool(a.get("required")) or bool(ctx.get("required"))
            semantic = classify(question, name, el_id)
            value, reason = resolve_value(question, semantic, identity, answers, job, buttons)
            if not value and required and semantic != "eeo" and not is_eeo(question):
                value, reason = _auto_answer(question, identity, job, buttons, reason)
            plans.append(FieldPlan(
                kind="buttons", label=question, semantic=semantic, required=required,
                value=value, action="fill" if value else "skip", reason=reason,
                locator=el.locator(ENTRY_XPATH),
                meta={"name": name, "id": el_id, "sig": a.get("sig", ""), "options": buttons},
            ))
            continue

        if not a.get("visible") and kind != "file":
            continue

        # Skip react-select decoy inputs: hidden validation inputs with empty
        # id/name/type/role but visible=1. These are duplicates of the real combobox.
        # (Ashby's location autocomplete also has no type - it is NOT a decoy.)
        if (tag == "input" and a.get("visible") and not a.get("id") and
            not a.get("name") and not ftype and not a.get("role") and not is_ashby):
            continue
        label = label_for(page, el)
        required = bool(a.get("required")) or bool(ctx.get("required"))
        question = ctx.get("question") or ""
        options: list[str] = []
        if kind == "radio":
            group = name or label
            if group in seen_radio_groups:
                continue
            seen_radio_groups.add(group)
            # label_for() on a radio returns the OPTION text ('7+ years'); the
            # question is the group heading / legend.
            options = [o for o in (ctx.get("options") or [])]
            if question:
                label = question
            # Ashby renders SMS / WhatsApp marketing-consent radios inside the
            # required Phone entry, so they inherit its _required heading. They are
            # opt-ins, not questions: never let them block a submit.
            blob = _lower(" ".join([name or "", *[str(o) for o in options]]))
            if not a.get("required") and ("consent" in blob or "text message" in blob
                                          or "whatsapp" in blob or "sms" in blob):
                required = False
        elif kind == "select":
            options = [
                o for o in (ctx.get("options") or [])
                if not _lower(o).startswith(("select", "choose", "please select", "--"))
            ]
            if question and is_ashby:
                label = question
        elif kind == "checkbox":
            if question and _lower(label) not in _lower(question):
                label = f"{question} - {label}" if label else question
            # Recruitee: the label IS the question; the option lives in value=.
            opt_val = str(a.get("value") or "").strip()
            if " - " not in label and opt_val and _lower(opt_val) not in ("on", "true", "1", "yes") \
                    and _lower(opt_val) not in _lower(label):
                label = f"{label} - {opt_val}"
        elif question and is_ashby:
            label = question


        if kind == "file":
            # Ashby's "Autofill from resume" pane re-parses the PDF and rewrites
            # fields asynchronously, possibly after we have filled them. The real
            # resume slot (_systemfield_resume) is a separate input - use only that.
            try:
                in_autofill = bool(el.evaluate(
                    "e => !!e.closest('[class*=\"autofill\"]')"))
            except Exception:
                in_autofill = False
            if in_autofill:
                continue
            # A photo/avatar slot (SmartRecruiters ships one that accepts only
            # images) must never receive the resume PDF - the form rejects it
            # with 'file format is not supported' and the submit never lands.
            try:
                accept = _lower(el.get_attribute("accept") or "")
            except Exception:
                accept = ""
            slot_text = _lower(f"{el_id} {name} {label}")
            image_only = bool(accept) and "pdf" not in accept and (
                "image" in accept or any(x in accept for x in (".png", ".jpg", ".jpeg", ".gif", ".webp")))
            if image_only or any(w in slot_text for w in ("photo", "avatar", "picture", "profile image", "headshot")):
                plans.append(FieldPlan(
                    kind="file", label=label or "Photo", semantic="photo",
                    required=required, value="", action="skip",
                    reason="photo/image upload - the resume PDF does not belong here",
                    locator=el, meta={"name": name, "id": el_id, "sig": a.get("sig", "")},
                ))
                continue
            # Greenhouse ships #resume AND #cover_letter as file inputs that are
            # BOTH labelled 'Attach', so the label cannot tell them apart. Route
            # on id/name instead: the resume goes to the resume input, and an
            # optional cover-letter upload is left alone rather than being fed a
            # duplicate resume PDF.
            slot = f"{el_id} {name}".lower()
            if "cover" in slot or "letter" in slot or "cover letter" in _lower(label):
                plans.append(FieldPlan(
                    kind="file", label=label or "Cover letter", semantic="cover_letter",
                    required=required, value="", action="skip",
                    reason="optional cover-letter upload - the resume belongs in the resume slot",
                    locator=el, meta={"name": name, "id": el_id, "sig": a.get("sig", "")},
                ))
                continue
            plans.append(FieldPlan(
                kind="file", label=label or "Resume", semantic="resume",
                required=required, value=identity.resume, action="fill",
                reason="resume file from profile", locator=el,
                meta={"name": name, "id": el_id, "sig": a.get("sig", "")},
            ))
            continue

        # A checkbox's own option text ("New York City") must not drive the
        # semantic - classify on the group question it belongs to.
        if kind == "checkbox":
            # (name/id too: Ashby bakes the option value into the checkbox id.)
            semantic = classify(question or label.split(" - ")[0])
        else:
            semantic = classify(label, name, el_id)
        if kind in ("text", "textarea") and re.search(r"\btwitter\b|\bx\.com\b|\bx \(twitter", _lower(label)):
            semantic = "question"   # not the portfolio site - never cross-filled
        # A long sentence that merely mentions "location"/"company" is a real
        # question ("This role requires someone in Pune... confirm your
        # location"), not a plain profile field - let the brain read it.
        if (kind in ("text", "textarea") and len(_lower(label)) > 80 and semantic in (
                "location", "country", "company", "title", "school", "degree",
                "discipline", "full_name")):
            semantic = "question"

        # Conditional follow-ups ("If other, please specify") belong to a branch
        # we did not take - answering them would be noise, so leave them blank.
        if is_conditional(label) and not required:
            plans.append(FieldPlan(
                kind=kind, label=label, semantic=semantic, required=required,
                value=None, action="skip",
                reason="conditional follow-up field - not applicable to our answers",
                locator=el, meta={"name": name, "id": el_id, "sig": a.get("sig", "")},
            ))
            continue

        # Long free-text: specific questions (start date, salary, stack...) get
        # the brain's grounded answer; real essays get the cover-letter text.
        if kind == "textarea" and semantic in ("question", "cover_letter"):
            text = cover_letter_text(identity, answers, title, company)
            if is_eeo(label):
                value, reason = None, "EEO/demographic - intentionally not answered"
                if required:
                    value, reason = "Prefer not to say", "required EEO free-text - honest decline, nothing invented"
            else:
                value, reason = resolve_value(label, "question", identity, answers, job)
                essay = value is None or reason.startswith("brain: short statement")
                # A bare Yes/No in a box that asks you to *describe* something is a
                # keyword-rule misfire ("remote" in "tell us about remote work").
                yes_no_q = _lower(label).startswith(
                    ("are ", "do ", "does ", "will ", "can ", "is ", "have ", "has ", "would "))
                misfire = bool(value) and value.strip().lower() in ("yes", "no") and not yes_no_q
                if (value is None or misfire) and not required:
                    value, reason = None, "optional free-text question with no grounded answer - left blank"
                elif (essay or misfire) and text:
                    value, reason = text, "cover-letter template built from resume facts"
                elif value is None:
                    value, reason = _auto_answer(label, identity, job, None, reason)
            plans.append(FieldPlan(
                kind="textarea", label=label, semantic="cover_letter",
                required=required, value=value,
                action="fill" if value else "skip", reason=reason, locator=el,
                meta={"name": name, "id": el_id, "sig": a.get("sig", "")},
            ))
            continue

        # Ashby's SMS-consent radio sits under the phone label. Consenting to
        # recruiter texts is harmless and some forms reject a blank group.
        if kind == "radio" and semantic == "phone" and options:
            yes = [o for o in options if re.search(r"\b(yes|i consent|i agree|agree)\b", _lower(o))
                   and not re.search(r"\b(not|don't|do not|no)\b", _lower(o))]
            if yes:
                plans.append(FieldPlan(
                    kind=kind, label=label, semantic="question", required=required,
                    value=yes[0], action="fill", reason="phone SMS consent - recruiter texts are fine",
                    locator=el, meta={"name": name, "id": el_id, "sig": a.get("sig", ""), "options": options},
                ))
                continue
        # Self-rating groups (Beginner / Intermediate / Advanced) - one honest
        # level: known resume skill -> by years; anything else -> Beginner.
        if kind == "checkbox" and " - " in label:
            option = label.split(" - ")[-1].strip()
            if _LEVEL_RE.search(_lower(option)):
                level = _honest_level(label.split(" - ")[0], identity)
                pick = _lower(option).startswith(level) or level in _lower(option)
                plans.append(FieldPlan(
                    kind=kind, label=label, semantic="question", required=required,
                    value="Yes" if pick else "No", action="fill",
                    reason=f"self-rating: honest level '{level}' from resume skills/years",
                    locator=el, meta={"name": name, "id": el_id, "sig": a.get("sig", ""), "options": options},
                ))
                continue
        value, reason = resolve_value(label, semantic, identity, answers, job, options or None)
        # <input type=number>: '14-15 LPA' is invalid there - send digits.
        if value and str(a.get("type") or "").lower() == "number" and not re.fullmatch(r"\d+(\.\d+)?", str(value).strip()):
            num = _numeric_answer(label, value, identity)
            if num:
                value, reason = num, f"number field - {reason}"

        # A checkbox IS one option of a group ("Gurugram", "Jaipur"). A text
        # answer like 'Gurugram, Haryana, India' must become tick / no-tick for
        # THIS option - otherwise every box is unticked and the form rejects.
        if kind == "checkbox" and value and not _YESNO_RE.match(_lower(value)):
            option = label.split(" - ")[-1].strip()
            if _option_matches(option, value, identity):
                value, reason = "Yes", f"ticked - matches '{value}' ({reason})"
            else:
                value, reason = "No", f"left unticked - '{option}' is not '{value}'"

        # Some boards mark EEO/demographic dropdowns as required. We never invent
        # personal characteristics, but "decline to self-identify" is a real,
        # honest option the form itself offers, so take it instead of stalling.
        if value is None and semantic == "eeo" and required and kind in ("select", "combobox", "radio"):
            plans.append(FieldPlan(
                kind=kind, label=label, semantic="eeo_decline", required=required,
                value=DECLINE_SENTINEL, action="fill",
                reason="required EEO field - choosing the form's own 'decline to self-identify' option",
                locator=el, meta={"name": name, "id": el_id, "sig": a.get("sig", "")},
            ))
            continue

        if not value and required and (semantic == "eeo" or is_eeo(label)) and kind in ("text", "textarea", "input"):
            value, reason = "Prefer not to say", "required EEO free-text - honest decline, nothing invented"
        elif not value and required and semantic != "eeo" and not is_eeo(label) and kind != "checkbox":
            value, reason = _auto_answer(label, identity, job, options or None, reason)
        elif not value and required and kind == "checkbox" and semantic != "eeo":
            group = (question or label.split(" - ")[0]).strip()
            if group not in auto_checked_groups:
                auto_checked_groups.add(group)
                value, reason = "Yes", f"auto-decided: required checkbox ticked ({reason or 'no grounded answer'})"

        plans.append(FieldPlan(
            kind=kind, label=label, semantic=semantic, required=required,
            value=value, action="fill" if value else "skip", reason=reason,
            locator=el, meta={"name": name, "id": el_id, "sig": a.get("sig", ""), "options": options},
        ))

    # Kept per plan (workers are threads) so a dropdown whose list only shows
    # up after typing can still be answered from the options it reveals.
    _plan_sr_radios(page, identity, answers, job, plans)
    _ensure_required_groups(plans, identity)
    for p in plans:
        if p.meta is not None:
            p.meta["ctx"] = (identity, job)
    return plans


_YESNO_RE = re.compile(r"^(yes|no|y|n|true|false|agree|accept|i agree|i accept|i confirm)\b")
_LEVEL_RE = re.compile(r"^(beginner|novice|basic|intermediate|advanced|expert|proficient|elementary)\b")


def _honest_level(question: str, identity) -> str:
    """'How do you rate your skills with Node.js?' -> beginner/intermediate/advanced."""
    from . import brain
    q = _lower(question)
    years = 0.0
    try:
        years = float(getattr(identity, "years_experience", 0) or 0)
    except (TypeError, ValueError):
        pass
    known = any(s and len(s) >= 2 and re.search(rf"(?<![a-z0-9]){re.escape(s)}(?![a-z0-9])", q)
                for s in brain._skills(identity))
    if not known:
        m = re.search(r"\bwith ([a-z0-9.+#/ -]{2,30}?)(?:\?|$| such| and)", q)
        known = bool(m and brain._knows(identity, m.group(1)))
    if not known:
        return "beginner"
    return "advanced" if years >= 3 else "intermediate"


def _numeric_answer(label: str, value: str, identity) -> str | None:
    """Digits for number inputs: salary -> INR (or USD when asked), else first number."""
    low = _lower(label)
    if re.search(r"salary|ctc|compensation|pay|remuneration|expect", low):
        try:
            inr = int(str(getattr(identity, "expected_ctc_inr", "") or "0").replace(",", ""))
        except ValueError:
            inr = 0
        if inr:
            if re.search(r"usd|\$|dollar", low):
                return str(int(round(inr / 83 / 1000.0)) * 1000)
            if re.search(r"eur|€", low):
                return str(int(round(inr / 90 / 1000.0)) * 1000)
            if re.search(r"lpa|lakh", low):
                return str(round(inr / 100000, 1)).rstrip("0").rstrip(".")
            return str(inr)
    m = re.search(r"\d+(\.\d+)?", str(value))
    return m.group(0) if m else None


def _option_matches(option: str, value: str, identity) -> bool:
    """Does checkbox option text describe the same thing as our answer?"""
    o, v = _lower(option), _lower(value)
    if not o:
        return False
    if o in v or v in o:
        return True
    parts = [p.strip() for p in re.split(r"[,/|]", v) if len(p.strip()) >= 3]
    if any(p == o or o.startswith(p) or p.startswith(o) for p in parts):
        return True
    home = _lower(getattr(identity, "location", "") or "")
    if any(w in home for w in ("gurugram", "gurgaon")) and re.search(r"\b(gurgaon|gurugram|delhi|ncr|noida)\b", o):
        return True
    return False


_GROUP_PREFER = re.compile(r"\b(remote|anywhere|india|delhi|ncr|gurgaon|gurugram|noida|open to|any location|flexible|willing to relocate)\b")


def _plan_sr_radios(page, identity, answers, job, plans) -> None:
    """SmartRecruiters Yes/No questions are <spl-radio> web components (no
    <input>), so the generic control walk never sees them. Plan them here."""
    try:
        hosts = page.locator("sr-question-field-radio")
        n = min(hosts.count(), 40)
    except Exception:
        return
    for i in range(n):
        host = hosts.nth(i)
        try:
            if not host.is_visible():
                continue
            text = host.inner_text(timeout=3000)
            radios = host.locator("spl-radio")
            opts = [t for t in (_sr_radio_text(radios.nth(k)) for k in range(radios.count())) if t]
        except Exception:
            continue
        lines = [ln.strip() for ln in text.splitlines() if ln.strip() and ln.strip() != "*"]
        if not lines or not opts:
            continue
        question = re.sub(r"^\d+\.\s*", "", lines[0]).rstrip("* ").strip()
        required = "*" in text[:400]
        semantic = classify(question, "", "")
        value, reason = resolve_value(question, semantic, identity, answers, job, opts)
        if not value and required and semantic != "eeo" and not is_eeo(question):
            value, reason = _auto_answer(question, identity, job, opts, reason)
        plans.append(FieldPlan(
            kind="srradio", label=question, semantic=semantic, required=required,
            value=value, action="fill" if value else "skip", reason=reason,
            locator=host, meta={"name": f"sr-radio-{i}", "id": "", "sig": "", "options": opts},
        ))


def _sr_radio_text(radio) -> str:
    """An <spl-radio>'s option text lives in its shadow root's <label>."""
    try:
        t = radio.locator("label").first.inner_text(timeout=2000).strip()
        if t:
            return t
    except Exception:
        pass
    try:
        return (radio.evaluate(
            "n => (n.shadowRoot ? n.shadowRoot.textContent : n.textContent) || ''") or "").strip()
    except Exception:
        return ""


def _sr_radio_checked(radio) -> bool:
    try:
        return bool(radio.evaluate(
            "n => n.checked === true || n.getAttribute('aria-checked') === 'true' || n.hasAttribute('checked')"))
    except Exception:
        return False


def _set_sr_radio(host, value: str) -> bool:
    want = _lower(value)
    try:
        radios = host.locator("spl-radio")
        for k in range(radios.count()):
            r = radios.nth(k)
            if _lower(_sr_radio_text(r)) != want:
                continue
            try:
                r.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass
            for how in ("label", "host", "force", "js"):
                try:
                    if how == "label":
                        r.locator("label").first.click(timeout=4000)
                    elif how == "host":
                        r.click(timeout=4000)
                    elif how == "force":
                        r.click(timeout=4000, force=True)
                    else:
                        r.evaluate("n => { const i = n.shadowRoot && n.shadowRoot.querySelector('input'); (i || n).click(); }")
                except Exception:
                    continue
                time.sleep(0.4)
                if _sr_radio_checked(r):
                    return True
            return _sr_radio_checked(r)
    except Exception:
        return False
    return False


def _ensure_required_groups(plans, identity) -> None:
    """A required checkbox group must end with at least one box ticked.

    If none of our answers matched an option (e.g. office cities that do not
    include ours), tick the closest honest one: remote/India/NCR/'open to
    relocate' first, else the first option (the candidate is willing to
    relocate, per profile).
    """
    groups: dict[str, list] = {}
    for p in plans:
        if p.kind != "checkbox" or not p.required or p.semantic == "eeo":
            continue
        key = p.label.split(" - ")[0] if " - " in p.label else f"__{(p.meta or {}).get('name') or p.semantic}"
        groups.setdefault(key, []).append(p)
    for members in groups.values():
        if len(members) < 2:
            continue
        if any(m.value and _lower(m.value).startswith(("y", "t", "a", "1")) for m in members):
            continue
        pick = next((m for m in members if _GROUP_PREFER.search(_lower(m.label.split(" - ")[-1]))), members[0])
        pick.value, pick.action = "Yes", "fill"
        pick.reason = "required group had no matching option - ticked the closest honest one (willing to relocate)"


def _fill_text(el: Locator, value: str) -> bool:
    try:
        el.scroll_into_view_if_needed(timeout=4000)
    except Exception:
        pass
    for attempt in (1, 2):
        try:
            el.fill("", timeout=6000)
            el.type(value, delay=6 if attempt == 1 else 0, timeout=12000)
            current = el.input_value(timeout=4000)
            if _norm(current) == _norm(value):
                return True
            # Typed on top of a value the form re-inserted (SR's email box
            # ended up 'x@gmail.como259@gmail.com') - set it wholesale.
            el.fill(value, timeout=6000)
            current = el.input_value(timeout=4000)
            if _norm(current) == _norm(value):
                return True
            if attempt == 2 and _norm(current):
                return True   # masked/formatted input (phone, dates) - keep it
        except Exception:
            try:
                el.fill(value, timeout=6000)
                return bool(_norm(el.input_value(timeout=4000)))
            except Exception:
                continue
    return False


# Greenhouse (and several other boards) wrap the phone field in the
# intl-tel-input widget. It renders a country <button> next to the <input>, and
# until a country is picked the flag slot shows a generic globe - which is how
# Divya's Turing application ended up carrying a +248 (Seychelles) dial code.
# Both candidates are in India, so we explicitly select India (+91) and then
# type the national number without the +91 prefix.
ITI_WRAPPER = ".iti"
ITI_BUTTON = "button.iti__selected-country"
ITI_SEARCH = "input.iti__search-input"


def national_number(value: str) -> str:
    """'+91 8109774968' -> '8109774968' (digits only, India prefix removed)."""
    digits = re.sub(r"\D", "", value or "")
    if digits.startswith("91") and len(digits) > 10:
        digits = digits[2:]
    return digits


def _iti_root(el: Locator) -> Locator | None:
    """The .iti wrapper around this tel input, or None if it is a plain field."""
    try:
        root = el.locator("xpath=ancestor::div[contains(@class,'iti')][1]")
        if root.count() and root.first.locator(ITI_BUTTON).count():
            return root.first
    except Exception:
        pass
    return None


def _close_iti(page: Page, root: Locator) -> None:
    """Shut the intl-tel-input country list.

    It renders 244 country rows as a tall overlay. Clicking a row usually
    collapses it, but when it lingers it covers every field below the phone
    input, so Country*, Location (City)* and the free-text questions all fail
    together as one contiguous block.
    """
    for attempt in (root, page):
        try:
            keyboard = getattr(attempt, "keyboard", None)
            if keyboard is not None:
                keyboard.press("Escape")
        except Exception:
            pass
    try:
        dropdown = root.locator(".iti__dropdown-content, .iti__country-list").first
        if dropdown.count() and dropdown.is_visible():
            page.keyboard.press("Escape")
            page.wait_for_timeout(150)
    except Exception:
        pass


def _select_india_dial_code(page: Page, root: Locator) -> bool:
    """Open the country dropdown and click India (+91). Verified against
    Greenhouse/Turing DOM: li[data-country-code='in'] with data-dial-code='91'."""
    try:
        button = root.locator(ITI_BUTTON).first
        if "iti__in" in (button.get_attribute("class") or ""):
            return True  # already India
        if button.locator(".iti__flag.iti__in").count():
            return True
        button.scroll_into_view_if_needed(timeout=3000)
        button.click(timeout=6000)
    except Exception:
        return False

    # The dropdown is appended either inside the wrapper or at the page root.
    for scope in (root, page):
        try:
            search = scope.locator(ITI_SEARCH)
            if search.count() and search.first.is_visible():
                search.first.fill("India", timeout=3000)
                page.wait_for_timeout(350)
        except Exception:
            pass
        for selector in (
            "li[data-country-code='in']",
            "li[data-dial-code='91']",
            "li.iti__country:has(span.iti__dial-code:text-is('+91'))",
        ):
            try:
                option = scope.locator(selector)
                if not option.count():
                    continue
                option.first.click(timeout=5000)
                page.wait_for_timeout(200)
                _close_iti(page, root)
                return True
            except Exception:
                continue
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    return False


def _fill_phone(page: Page, el: Locator, value: str) -> bool:
    """Fill a phone field, selecting the India (+91) dial code when the widget
    exposes a country picker. Falls back to the full +91... string otherwise."""
    root = _iti_root(el)
    if root is None:
        return _fill_text(el, value)

    picked = _select_india_dial_code(page, root)
    typed = _fill_text(el, national_number(value) if picked else value)
    if typed:
        return True
    # Last resort: hand the widget the fully qualified international number.
    return _fill_text(el, value)

def _pick_decline(page: Page, el: Locator, kind: str) -> bool:
    """Choose the form's own decline-to-answer option for a required EEO field."""
    if kind == "select":
        for word in DECLINE_WORDS:
            try:
                el.select_option(label=re.compile(word, re.I), timeout=4000)
                return True
            except Exception:
                continue
        return False

    if kind == "radio":
        name = ""
        try:
            name = el.get_attribute("name") or ""
        except Exception:
            pass
        if not name:
            return False
        group = page.locator(f"input[type=radio][name='{name}']")
        try:
            total = group.count()
        except Exception:
            return False
        for idx in range(min(total, 12)):
            radio = group.nth(idx)
            text = _lower(label_for(page, radio))
            if any(w in text for w in DECLINE_WORDS):
                try:
                    radio.check(timeout=4000)
                    return True
                except Exception:
                    continue
        return False

    # combobox / react-select
    for word in ("decline", "prefer not", "do not wish", "don't wish"):
        if _fill_combobox(page, el, word):
            return True
    return False



def _pick_option(el: Locator, value: str) -> bool:
    """Choose the closest <option>; fall back to the first real choice for yes/no."""
    want = _lower(value)
    try:
        options = el.evaluate(
            "(n) => Array.from(n.options).map(o => ({v: o.value, t: (o.textContent||'').trim()}))"
        ) or []
    except Exception:
        return False
    real = [o for o in options if _norm(o.get("v")) and _lower(o.get("t")) not in ("", "select...", "select")]
    for opt in real:
        if _lower(opt.get("t")) == want:
            try:
                el.select_option(value=opt["v"], timeout=6000)
                return True
            except Exception:
                break
    for opt in real:
        text = _lower(opt.get("t"))
        if want and (want in text or text in want):
            try:
                el.select_option(value=opt["v"], timeout=6000)
                return True
            except Exception:
                break
    words = YES_WORDS if want.startswith("y") else NO_WORDS if want.startswith("n") else ()
    for opt in real:
        if any(w == _lower(opt.get("t")) for w in words):
            try:
                el.select_option(value=opt["v"], timeout=6000)
                return True
            except Exception:
                break
    return False


# Option nodes, MOST SPECIFIC FIRST. Greenhouse and Lever both use react-select,
# whose options are `div[role='option'].select__option` inside `div.select__menu`.
# The generic `[role='option']` selector has to come later: Greenhouse also ships
# an intl-tel-input phone widget that keeps 247 HIDDEN `li[role='option']` country
# rows in the DOM at 0x0 pixels. Matching those first made the clicker aim at an
# invisible node, which is why Country* and Location (City)* were never filled.
OPTION_SELECTORS = (
    "spl-select-option",  # SmartRecruiters web components (lit, shadow DOM)
    "div[class*='select__option']",
    "div[class*='menu'] div[role='option']",
    "[role='listbox'] [role='option']",
    "[role='option']",
    "li[class*='option']",
    "div[class*='option']",
    "[class*='dropdown'] li",
    "[class*='autocomplete'] li",
    "ul[class*='select'] li",
)

# Hidden widgets that masquerade as option lists (the phone country picker).
OPTION_NOISE = ("iti__country", "iti__flag", "iti__dial")

# After a pick, react-select CLEARS the <input> and renders the chosen value in a
# sibling node, so input_value() reads empty even on success. Read these instead.
SINGLE_VALUE_SELECTORS = (
    "[class*='single-value']",
    "[class*='singleValue']",
    "[class*='multi-value']",
    "[class*='multiValue']",
)

SELECT_ROOT_XPATHS = (
    "xpath=ancestor::div[contains(@class,'select__control')][1]",
    "xpath=ancestor::div[contains(@class,'select__container')][1]",
    "xpath=ancestor::div[contains(@class,'select')][1]",
)


def _control_root(el: Locator) -> Locator | None:
    """The clickable `.select__control` box around a react-select input.

    The input itself autosizes to ~3px wide, which is a miserable click target;
    its control ancestor is the full-width (600x34) row the user would click.
    """
    try:
        root = el.locator("xpath=ancestor::*[contains(@class,'select__control')][1]")
        if root.count():
            return root.first
    except Exception:
        pass
    return None


def _select_scope(el: Locator) -> Locator | None:
    """This field's own select wrapper, used to hunt ONLY its own options.

    The menu is a sibling of the control, so we climb to the container.
    """
    for xp in (
        "xpath=ancestor::div[contains(@class,'select__container')][1]",
        "xpath=ancestor::div[contains(@class,'select')][1]",
    ):
        try:
            root = el.locator(xp)
            if root.count():
                return root.first
        except Exception:
            continue
    return None


def _combobox_queries(value: str) -> list[str]:
    """Autocompletes often reject a full 'City, State, Country' string.

    Try the whole value first, then progressively shorter forms (city + country,
    then just the city) so 'Gurugram, Haryana, India' still resolves.
    """
    value = (value or "").strip()
    if not value:
        return []
    parts = [p.strip() for p in value.split(",") if p.strip()]
    out = [value]
    if len(parts) >= 2:
        out.append(f"{parts[0]}, {parts[-1]}")
    if parts:
        out.append(parts[0])
    # Many ATS city lists still use the old name; try it after the real one.
    aliases = {"gurugram": "Gurgaon", "bengaluru": "Bangalore", "mumbai": "Bombay",
               "medicaps university": "Medi-Caps University"}
    if parts and parts[0].lower() in aliases:
        alt = aliases[parts[0].lower()]
        if len(parts) >= 2:
            out.append(f"{alt}, {parts[-1]}")
        out.append(alt)
    seen: list[str] = []
    for q in out:
        if q and q not in seen:
            seen.append(q)
    return seen


def _fallback_queries(value: str) -> list[str]:
    """Last-resort queries for a location value, matched STRICTLY.

    Some 'Location' boxes are really country lists (ElevenLabs) or only carry
    big metros; for 'Gurugram, Haryana, India' we then try the NCR capital and
    finally the country itself, accepting only an exact option so a city list
    can never turn 'India' into 'Indore, India'.
    """
    parts = [p.strip() for p in (value or "").split(",") if p.strip()]
    if len(parts) < 2:
        return []
    out: list[str] = []
    if parts[0].lower() in {"gurugram", "gurgaon", "noida", "faridabad", "ghaziabad"}:
        out += ["New Delhi", "Delhi"]
    out.append(parts[-1])
    return [q for q in out if q.lower() != parts[0].lower()]


def _strict_option(options: list[Locator], query: str):
    """Exact option, or one that is the query plus a qualifier ('Delhi, India')."""
    want = _lower(query)
    pairs = [(_lower(_option_text(o)), o) for o in options]
    for text, opt in pairs:
        if text == want:
            return opt, text
    for text, opt in pairs:
        if text.startswith((want + ",", want + " (", want + " -")):
            return opt, text
    return None, ""


def _option_text(opt: Locator) -> str:
    try:
        return _norm(opt.inner_text(timeout=1500))
    except Exception:
        return ""


def _visible_options(page: Page, scope: Locator | None = None) -> list[Locator]:
    """Option nodes that are genuinely rendered and clickable.

    Rejects zero-sized nodes and phone-country rows, which is the whole reason
    react-select fills used to fail on Greenhouse boards.
    """
    hunt = scope if scope is not None else page
    for selector in OPTION_SELECTORS:
        try:
            options = hunt.locator(selector)
            total = options.count()
        except Exception:
            continue
        if not total:
            continue
        good: list[Locator] = []
        for idx in range(min(total, 30)):
            opt = options.nth(idx)
            try:
                cls = _lower(opt.get_attribute("class") or "")
                if any(noise in cls for noise in OPTION_NOISE):
                    continue
                box = opt.bounding_box()
            except Exception:
                continue
            if not box:
                continue
            if box.get("width", 0) < 8 or box.get("height", 0) < 8:
                continue
            good.append(opt)
        if good:
            return good
    return []


# A lone '+91' / '+246' is meaningful ONLY for a dial-code picker, whose chosen
# option reads 'India +91' but whose rendered value is just '+91'. For a city or
# plain country field it is the neighbouring phone widget reporting itself, and
# accepting it made Country* look filled when nothing had been chosen.
_DIAL_ONLY = re.compile(r"^\+\d{1,5}$")
_HAS_DIAL = re.compile(r"\+\d{1,5}\b")


def _combobox_value(el: Locator, allow_dial: bool = False) -> str:
    """What the combobox actually holds, read the way react-select reports it."""
    for xp in SELECT_ROOT_XPATHS:
        try:
            root = el.locator(xp)
            if not root.count():
                continue
        except Exception:
            continue
        for sel in SINGLE_VALUE_SELECTORS:
            try:
                node = root.locator(sel).first
                if node.count() and node.is_visible():
                    text = _norm(node.inner_text(timeout=1500))
                    if text and (allow_dial or not _DIAL_ONLY.match(text)):
                        return text
            except Exception:
                continue
    try:
        return (el.input_value(timeout=2000) or "").strip()
    except Exception:
        return ""


def _combobox_settled(el: Locator, expect: str = "") -> bool:
    """True only when the control really holds a value.

    `expect` should be the text of the option we clicked, not what we typed: a
    dial-code picker answers 'India +91' with a rendered value of just '+91'.
    """
    allow_dial = bool(expect) and bool(_HAS_DIAL.search(expect))
    current = _lower(_combobox_value(el, allow_dial=allow_dial))
    if not current:
        return False
    if not expect:
        return True
    want = _lower(expect)
    head = want.split(",")[0].strip()
    return want in current or current in want or bool(head and head in current)


def _best_option(options: list[Locator], value: str, query: str):
    """exact -> startswith -> contains. Never blind-pick when several remain.

    Typing 'India' into Greenhouse's country picker lists 'British Indian Ocean
    Territory +246' FIRST (and focused); 'India +91' is second. Taking the first
    option would therefore have chosen the wrong country on every application.
    """
    pairs = [(_lower(_option_text(o)), o) for o in options]
    pairs = [(t, o) for t, o in pairs if t]
    if not pairs:
        return None, ""
    probes = [_lower(value), _lower(query), _lower(value.split(",")[0]),
              _lower(query.split(",")[0])]
    for probe in probes:
        if not probe:
            continue
        for test in (lambda t, p: t == p, lambda t, p: t.startswith(p), lambda t, p: p in t):
            hits = [(text, opt) for text, opt in pairs if test(text, probe)]
            # 'Fluent' must pick '...speak fluently', not '...but not fluently'.
            if " not " not in f" {probe} ":
                hits.sort(key=lambda h: bool(re.search(r"\b(not|no|never|without)\b", h[0])))
            if hits:
                return hits[0][1], hits[0][0]
    if len(pairs) == 1:
        return pairs[0][1], pairs[0][0]
    return None, ""


def _dismiss_menu(page: Page, el: Locator) -> None:
    """Close an open dropdown so its overlay cannot swallow later clicks.

    A left-open react-select menu is what made Phone*, Notice Period* and the
    qualification question all fail right after a combobox attempt.
    """
    try:
        el.press("Escape", timeout=1500)
        return
    except Exception:
        pass
    keyboard = getattr(page, "keyboard", None)
    if keyboard is not None:
        try:
            keyboard.press("Escape")
        except Exception:
            pass


def _owned_listbox(page: Page, el: Locator) -> Locator | None:
    """The listbox a combobox names via aria-controls/aria-owns (Workable)."""
    for attr in ("aria-controls", "aria-owns"):
        try:
            lid = (el.get_attribute(attr, timeout=1500) or "").split()
        except Exception:
            continue
        if lid and re.fullmatch(r"[A-Za-z][\w\-:.]*", lid[0]):
            return page.locator(f"[id='{lid[0]}']")
    return None


def _is_readonly(el: Locator) -> bool:
    try:
        return bool(el.evaluate("e => !!e.readOnly || e.getAttribute('aria-autocomplete') === 'none'"))
    except Exception:
        return False


def _pick_readonly(page: Page, el: Locator, value: str, scope: Locator | None) -> bool:
    """Select-only combobox: typing does nothing, the list opens on click."""
    for attempt in range(2):
        try:
            el.scroll_into_view_if_needed(timeout=4000)
            baseline = set() if scope is not None else {_option_text(o) for o in _visible_options(page)}
            (_control_root(el) or el).click(timeout=6000)
        except Exception:
            return False
        options: list[Locator] = []
        for _ in range(12):
            page.wait_for_timeout(250)
            options = _visible_options(page, scope) if scope is not None else [
                o for o in _visible_options(page) if _option_text(o) not in baseline]
            if options:
                break
        target, text = _best_option(options, value, value) if options else (None, "")
        if target is None:
            _dismiss_menu(page, el)
            return False
        try:
            target.click(timeout=4000, force=attempt > 0)
        except Exception:
            continue
        page.wait_for_timeout(400)
        if _combobox_settled(el, text):
            return True
    _dismiss_menu(page, el)
    return False


def _fill_combobox(page: Page, el: Locator, value: str) -> bool:
    """React-select / autocomplete: click, type, then choose from the listbox."""
    queries = _combobox_queries(value)
    strict = set(q for q in _fallback_queries(value) if q not in queries)
    queries = queries + [q for q in _fallback_queries(value) if q in strict]
    # Options must be hunted inside THIS field's own select container. A global
    # search also sees the phone widget's dial-code menu, which is how typing
    # 'India' into Country* ended up committing the dial code +91 instead.
    scope = _select_scope(el) or _owned_listbox(page, el)
    if not queries:
        return False
    if _is_readonly(el):
        return _pick_readonly(page, el, value, scope)
    saw_options = False
    # Hard budget: a combobox that never answers must not stall the whole form.
    deadline = time.time() + 30

    for query in queries:
        if time.time() > deadline:
            break
        try:
            el.scroll_into_view_if_needed(timeout=4000)
            control = _control_root(el)
            (control or el).click(timeout=6000)
        except Exception:
            _dismiss_menu(page, el)
            return False
        try:
            el.fill("", timeout=3000)
        except Exception:
            pass
        baseline: set[str] = set()
        if scope is not None:
            try:
                baseline = {_option_text(o) for o in _visible_options(page)}
            except Exception:
                baseline = set()
        try:
            el.type(query[:60], delay=20, timeout=10000)
        except Exception:
            continue

        # Autocompletes hit a network endpoint; poll instead of a fixed sleep.
        # The page-wide fallback must ignore option-like nodes that were already
        # on screen before we typed (Ashby renders other questions' Yes/No
        # buttons as options, which made a location box look 'answered').
        options: list[Locator] = []
        for _ in range(20):
            page.wait_for_timeout(300)
            options = _visible_options(page, scope)
            if not options and scope is not None:
                options = [o for o in _visible_options(page)
                           if _option_text(o) not in baseline]
            options = [o for o in options if _option_text(o)]
            if options or time.time() > deadline:
                break
        if not options:
            _dismiss_menu(page, el)
            continue
        saw_options = True

        if query in strict:
            target, text = _strict_option(options, query)
        else:
            target, text = _best_option(options, value, query)
        if target is None:
            # Options exist but none of them is our answer. Guessing here would
            # put a wrong country or city on a real application, so we do not.
            _dismiss_menu(page, el)
            continue
        for force in (False, True):
            try:
                target.click(timeout=4000, force=force)
            except Exception:
                continue
            page.wait_for_timeout(400)
            if _combobox_settled(el, text):
                return True
        # The right option was identified but would not take a click; commit it
        # with the keyboard instead.
        try:
            el.press("Enter", timeout=2000)
            page.wait_for_timeout(400)
            if _combobox_settled(el, text):
                return True
        except Exception:
            pass
        _dismiss_menu(page, el)

    if not saw_options:
        # No menu ever rendered, so this is a free-text autocomplete: commit the
        # typed value rather than leaving the field empty.
        try:
            el.press("Enter", timeout=3000)
            page.wait_for_timeout(300)
            # Ashby-style autocompletes keep the typed text in the input but
            # clear it on blur unless an option was chosen; only trust the
            # value if it survives losing focus.
            try:
                el.evaluate("e => e.blur()")
            except Exception:
                pass
            page.wait_for_timeout(600)
            if _combobox_settled(el):
                return True
        except Exception:
            pass
        try:
            el.fill("", timeout=2000)
        except Exception:
            pass
        _dismiss_menu(page, el)
        return False

    # Options were offered but none was ours: leave the box EMPTY so the
    # readback reports it honestly (typed text alone is not a selection and
    # Ashby rejects it as 'Missing entry for required field').
    try:
        el.fill("", timeout=2000)
    except Exception:
        pass
    _dismiss_menu(page, el)
    return False


def _set_checkbox(el: Locator, value: str) -> bool:
    want_yes = _lower(value).startswith(("y", "t", "a", "1"))
    try:
        if want_yes:
            el.check(timeout=4000)
        else:
            el.uncheck(timeout=4000)
        return True
    except Exception:
        pass
    # Hidden inputs (Radix/React custom checkboxes): click the visible label or
    # fire a DOM click, then check the state really changed.
    def _state():
        try:
            return el.is_checked(timeout=1500)
        except Exception:
            return None

    tries = []
    try:
        el_id = el.get_attribute("id", timeout=1500) or ""
    except Exception:
        el_id = ""
    if el_id:
        lab = el.page.locator(f"label[for={el_id!r}]")
        tries.append(lambda: lab.first.click(timeout=3000))
    tries.append(lambda: el.click(timeout=3000, force=True))
    tries.append(lambda: el.evaluate("n => n.click()"))
    for t in tries:
        if _state() == want_yes:
            return True
        try:
            t()
        except Exception:
            continue
    return _state() == want_yes


def _set_radio(page: Page, el: Locator, label: str, value: str) -> bool:
    """Pick the radio in this group whose own label matches the answer."""
    name = ""
    try:
        name = el.get_attribute("name") or ""
    except Exception:
        pass
    group = page.locator(f"input[type='radio'][name='{name}']") if name else el
    try:
        total = group.count()
    except Exception:
        total = 1
    want = _lower(value)
    radios = [group.nth(idx) for idx in range(min(total, 14))]
    texts = [_lower(label_for(page, r)) for r in radios]
    # Exact option text first: 'No' must never land on 'Not yet' or 'Yes, now'.
    order = [i for i, t in enumerate(texts) if t == want]
    order += [i for i, t in enumerate(texts) if t and i not in order and (want in t or t in want)]
    for idx in order:
        radio = radios[idx]
        try:
            radio.check(timeout=5000)
            return True
        except Exception:
            try:
                radio.click(timeout=4000, force=True)
                return True
            except Exception:
                continue
    return False


def _click_choice_button(entry: Locator, value: str) -> bool:
    """Ashby Yes/No: click the button in this field entry whose text matches."""
    want = _lower(value)
    try:
        btns = entry.locator("button")
        total = min(btns.count(), 10)
    except Exception:
        return False
    texts = []
    for idx in range(total):
        try:
            texts.append(_lower(btns.nth(idx).inner_text(timeout=2000)))
        except Exception:
            texts.append("")
    order = [i for i, t in enumerate(texts) if t == want]
    order += [i for i, t in enumerate(texts) if t and i not in order and t.startswith(want)]
    for idx in order:
        btn = btns.nth(idx)
        try:
            btn.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        try:
            btn.click(timeout=5000)
            return True
        except Exception:
            try:
                btn.click(timeout=3000, force=True)
                return True
            except Exception:
                continue
    return False


def apply_plan(page: Page, plans: list[FieldPlan]) -> dict[str, Any]:
    """Execute the plan. Returns counts plus a per-field audit trail."""
    filled = 0
    resume_attached = False
    skipped: list[str] = []
    audit: list[dict[str, str]] = []
    # A form must never take forever: past this budget we stop the slow
    # combobox rescue paths (fallbacks / option discovery) and only make
    # single attempts, so one stubborn dropdown can't stall the whole run.
    budget_end = time.time() + 150

    def _has_time() -> bool:
        return time.time() < budget_end

    for plan in plans:
        if plan.action == "skip" or not plan.value:
            skipped.append(f"{plan.label or plan.semantic} ({plan.reason})")
            audit.append({"label": plan.label, "status": "skipped", "reason": plan.reason})
            continue

        # The resume upload makes Greenhouse's React form remount, which
        # detaches every locator captured during planning. Re-query first.
        # Plan locators are positional (nth=N); filling earlier comboboxes
        # shifts the DOM, so N then points at a different control.
        planned_locator = plan.locator
        if plan.kind not in ("radio", "buttons", "srradio"):
            plan.locator = _relocate(page, plan)

        ok = False
        if plan.value == DECLINE_SENTINEL:
            ok = _pick_decline(page, plan.locator, plan.kind)
        elif plan.kind == "file":
            try:
                plan.locator.set_input_files(plan.value, timeout=20_000)
                ok = resume_attached = True
            except Exception:
                ok = False
        elif plan.semantic == "phone":
            ok = _fill_phone(page, plan.locator, plan.value)
        elif plan.kind == "select":
            ok = _pick_option(plan.locator, plan.value)
        elif plan.kind == "combobox":
            manual = _sr_manual_location(page, plan.locator, plan.value)
            ok = False
            # A long composed sentence never matches a dropdown entry - typing
            # it just burns ~25s per attempt. Read the box's own list first.
            if manual is None and len(plan.value or "") > 40 and _has_time():
                chosen = _discover_and_choose(page, plan)
                if chosen and _fill_combobox(page, plan.locator, chosen):
                    ok = True
                    plan.reason = f"{plan.reason}; picked '{chosen}' from the form's own list"
                    plan.value = chosen
            if not ok:
                ok = manual if manual is not None else _fill_combobox(page, plan.locator, plan.value)
            if not ok and planned_locator is not plan.locator and _has_time():
                try:
                    _dismiss_menu(page, plan.locator)
                except Exception:
                    pass
                if _fill_combobox(page, planned_locator, plan.value):
                    ok = True
                    plan.locator = planned_locator
            # Optional dropdowns get one honest try; the slow rescue paths are
            # only worth it for required questions.
            rescue = plan.required and _has_time()
            # Read the form's own list BEFORE the catch-all 'Other' fallbacks:
            # Bosch offers 'Gurugram'/'B.E/B.Tech', and 'Other' would be wrong.
            if not ok and rescue:
                chosen = _discover_and_choose(page, plan)
                if chosen and _fill_combobox(page, plan.locator, chosen):
                    ok = True
                    plan.reason = f"{plan.reason}; picked '{chosen}' from the form's own list"
                    plan.value = chosen
            if not ok and plan.required and _has_time():
                for alt in _combobox_fallbacks(plan):
                    if _fill_combobox(page, plan.locator, alt):
                        ok = True
                        plan.reason = f"{plan.reason}; list lacked '{plan.value}', chose '{alt}'"
                        plan.value = alt
                        break
        elif plan.kind == "checkbox":
            ok = _set_checkbox(plan.locator, plan.value)
        elif plan.kind == "buttons":
            ok = _click_choice_button(plan.locator, plan.value)
        elif plan.kind == "srradio":
            ok = _set_sr_radio(plan.locator, plan.value)
        elif plan.kind == "radio":
            ok = _set_radio(page, plan.locator, plan.label, plan.value)
        else:
            ok = _fill_text(plan.locator, plan.value)
            if not ok and _has_time():
                ok = _fill_combobox(page, plan.locator, plan.value)

        if ok:
            filled += 1
            shown = plan.value
            if plan.semantic == "cover_letter":
                shown = "<cover letter>"
            elif plan.value == DECLINE_SENTINEL:
                shown = "<declined to self-identify>"
            audit.append({
                "label": plan.label, "status": "filled",
                "value": shown[:120], "reason": plan.reason,
            })
        else:
            skipped.append(f"{plan.label or plan.semantic} (could not set)")
            audit.append({"label": plan.label, "status": "failed", "reason": "could not set control"})

    # --------------------------------------------------------- verify + repair
    # React forms (and the intl-tel-input phone widget) sometimes wipe a field
    # after we set it: the Turing run audited Phone* as "filled" while the DOM
    # actually held ''. So never trust the fill-time check - read every control
    # back at the END, and retry once for anything that came back empty.
    unverified: list[str] = []
    for plan in plans:
        if plan.action == "skip" or not plan.value or plan.kind == "file":
            continue
        if plan.kind in ("checkbox", "radio", "buttons", "srradio") or plan.value == DECLINE_SENTINEL:
            continue
        # React remounts these forms during validation, which detaches the
        # locator we captured while planning - so always re-query by id first.
        el = _relocate(page, plan)
        if _readback(plan, el):
            continue
        # Second attempt, now that any dropdown overlay has been dismissed.
        if plan.semantic == "phone":
            _fill_phone(page, el, plan.value)
        elif plan.kind == "combobox":
            if not _has_time() and not plan.required:
                unverified.append(plan.label or plan.semantic)
                continue
            if _sr_manual_location(page, el, plan.value) is None:
                _fill_combobox(page, el, plan.value)
        else:
            _fill_text(el, plan.value)
        el = _relocate(page, plan)
        if _readback(plan, el):
            audit.append({
                "label": plan.label, "status": "repaired",
                "value": str(plan.value)[:120], "reason": "was emptied by the form, re-entered",
            })
            continue
        unverified.append(plan.label or plan.semantic)
        audit.append({
            "label": plan.label, "status": "empty",
            "reason": "value did not stick in the DOM after two attempts",
        })
        # Don't count a field we could not verify as "filled".
        filled = max(0, filled - 1)

    # 'Confirm your email' validates against the main email only on its own
    # input/blur events. SmartRecruiters showed 'Provided emails do not match'
    # with two identical values because the confirm box was validated before
    # the main box settled - so re-type every confirm-email box last and blur.
    for plan in plans:
        if plan.action == "skip" or plan.semantic != "email" or not plan.value:
            continue
        if not re.search(r"confirm|re-?enter|re-?type|verify|repeat|again", _lower(plan.label)):
            continue
        try:
            el = _relocate(page, plan)
            _fill_text(el, plan.value)
            el.press("Tab", timeout=3000)
        except Exception:
            pass

    return {
        "filled": filled,
        "skipped": skipped,
        "resume_attached": resume_attached,
        "audit": audit,
        "unverified": unverified,
        "empty_required": [p.label or p.semantic for p in plans
                           if p.required and p.action == "fill"
                           and (p.label or p.semantic) in unverified],
    }


_HEAR_FALLBACKS = ("Company Website", "Website", "Careers Page", "Career Site",
                   "Job Posting", "Job Board", "LinkedIn", "Other")


def _combobox_fallbacks(plan: FieldPlan) -> list[str]:
    """Honest second choices when a dropdown does not list our exact answer.

    Never swaps in a different fact (another city, another school): only the
    form's own catch-all ('Other') or a synonym of the same answer.
    """
    low = _lower(plan.label)
    if any(k in low for k in ("hear", "come to know", "get to know", "find out", "referral source")):
        return list(_HEAR_FALLBACKS)
    if plan.semantic == "degree":
        return ["Bachelor of Technology", "B.Tech", "Bachelor of Engineering",
                "Bachelor's", "Bachelors", "Other"]
    if plan.semantic == "discipline":
        return ["Computer Science and Engineering", "Computer Engineering",
                "Information Technology", "Engineering", "Other"]
    if plan.semantic in ("school", "location") or "location" in low or "city" in low:
        return ["Other - Not Listed", "Other"]
    if plan.required:
        return ["Other"]
    return []


_DISCOVERY_PROBES = ("INR", "a", "e", "i", "o", "1", "2", "0", "Other", "Yes", "No")


def _discover_options(page: Page, el: Locator, plan: FieldPlan) -> list[str]:
    """Reveal a dropdown's list when it only appears after typing.

    Ashby's salary/'how did you hear' boxes show nothing on click and nothing
    for '14-15 LPA'; typing '1' or 'INR' reveals the real brackets. Collect the
    texts the form itself offers so the answer can be chosen from them.
    """
    scope = _select_scope(el) or _owned_listbox(page, el)
    words = [w for w in re.split(r"[^A-Za-z0-9]+", plan.value or "") if len(w) >= 2][:3]
    probes = [""] + words + list(_DISCOVERY_PROBES)
    found: list[str] = []
    for probe in probes:
        # Snapshot what is already on the page BEFORE touching this box, so
        # other questions' options never leak into this one's list.
        try:
            _dismiss_menu(page, el)
            baseline = {_option_text(o) for o in _visible_options(page)} if scope is None else set()
        except Exception:
            baseline = set()
        try:
            el.scroll_into_view_if_needed(timeout=3000)
            (_control_root(el) or el).click(timeout=4000)
        except Exception:
            break
        try:
            el.fill("", timeout=2000)
            if probe:
                el.type(probe, delay=20, timeout=5000)
        except Exception:
            pass
        options: list[Locator] = []
        for _ in range(8):
            page.wait_for_timeout(300)
            if scope is not None:
                options = _visible_options(page, scope)
            else:
                options = [o for o in _visible_options(page) if _option_text(o) not in baseline]
            if options:
                break
        for o in options:
            t = _option_text(o)
            if t and t not in found and len(t) < 160:
                found.append(t)
        _dismiss_menu(page, el)
        if len(found) >= 25:
            break
        # A plain dropdown shows its whole list on click - no need to type.
        if probe == "" and len(found) >= 2:
            break
    try:
        el.fill("", timeout=2000)
    except Exception:
        pass
    return found


def _discover_and_choose(page: Page, plan: FieldPlan) -> str | None:
    """Pick our answer from the options the box reveals (brain, then auto).

    Places are never auto-guessed: a wrong city/school is worse than a gap.
    """
    ctx = (plan.meta or {}).get("ctx")
    if not ctx:
        return None
    identity, job = ctx
    try:
        opts = _discover_options(page, plan.locator, plan)
    except Exception:
        return None
    opts = [o for o in opts if not _HAS_DIAL.search(o)]
    if not opts:
        return None
    try:
        value, _ = brain.answer(plan.label, identity, job, opts)
    except Exception:
        value = None
    if not value or value not in opts:
        picked = brain.choose_option(plan.value, opts) if plan.value else None
        value = picked if picked in opts else None
    low = _lower(plan.label)
    place = plan.semantic in ("school", "location") or any(
        k in low for k in ("location", "city", "country", "based", "school", "university", "college"))
    if not value and place:
        value = _home_option(opts, identity)
    if any(k in low for k in ("qualification", "degree", "education")):
        # Both candidates hold a B.Tech - prefer the form's matching entry
        # over whatever generic pick ('Others') the brain fell back to.
        deg = _degree_option(opts)
        if deg and (not value or not _degree_option([value])):
            value = deg
    if not value and plan.required and not place:
        try:
            value, _ = brain.auto_decide(plan.label, identity, job, opts)
        except Exception:
            value = None
        if value not in opts:
            value = None
    if value and _claims_other_city(value, identity):
        return None
    return value


_DEGREE_PREF = ("b.tech", "btech", "b. tech", "b.e", "bachelor of technology",
                "bachelor of engineering", "bachelor", "graduate", "graduation", "ug")


def _degree_option(opts: list[str]) -> str | None:
    """The option naming a B.Tech / bachelor's degree (the candidates' real one)."""
    for want in _DEGREE_PREF:
        for o in opts:
            ol = o.lower()
            if want in ol and "post" not in ol and "master" not in ol:
                return o
    return None


_HOME_PREF = ("gurugram", "gurgaon", "delhi", "ncr", "noida", "pan india", "any location",
              "anywhere", "remote", "india")


def _home_option(opts: list[str], identity) -> str | None:
    """For a place dropdown: the option nearest the candidate's real home."""
    for want in _HOME_PREF:
        for o in opts:
            if want in o.lower():
                return o
    return None


def _relocate(page: Page, plan: FieldPlan) -> Locator:
    """Fresh locator for a planned field, by id when the ATS gave us one.

    Greenhouse's React form remounts inputs while validating, which detaches
    the locator captured during planning: reads then return '' and re-fills
    land on a dead node. Re-querying by id is what makes the repair pass real.
    """
    sig = (plan.meta or {}).get("sig") or ""
    if sig:
        # SmartRecruiters re-renders the form after early fields are typed and
        # renumbers every id, but keeps its data-sr-id hooks stable.
        try:
            for attr in ("data-sr-id", "data-ui", "data-qa"):
                fresh = page.locator(f"[{attr}={sig!r}]")
                if fresh.count() == 1:
                    return fresh
            # After the resume is parsed SmartRecruiters swaps its location
            # search box for a country picker plus a plain city input.
            swap = _SR_SWAPS.get(sig)
            if swap:
                fresh = page.locator(f"[data-sr-id={swap!r}]")
                if fresh.count() == 1:
                    return fresh
        except Exception:
            pass
    el_id = (plan.meta or {}).get("id") or ""
    if el_id:
        try:
            fresh = page.locator(f"#{_css_escape(el_id)}")
            if fresh.count() == 1:
                return fresh
            # SmartRecruiters' web components repeat the id on their host
            # elements (spl-phone-field > spl-input > input all share it), so
            # narrow to the real form control instead of the positional
            # planning locator, which drifts when the DOM grows.
            esc = _css_escape(el_id)
            fresh = page.locator(f"input#{esc}, textarea#{esc}, select#{esc}")
            if fresh.count() == 1:
                return fresh
        except Exception:
            pass
    name = (plan.meta or {}).get("name") or ""
    if name:
        try:
            fresh = page.locator(f"[name={name!r}]")
            if fresh.count() == 1:
                return fresh
        except Exception:
            pass
    return plan.locator


_SR_SWAPS = {"location-autocomplete-search-search-input": "location-autocomplete-city-input"}


def _sr_manual_location(page: Page, el: Locator, value: str) -> bool | None:
    """Fill SmartRecruiters' manual location layout (country picker + city
    text box). Returns None when `el` is not that layout."""
    try:
        if el.get_attribute("data-sr-id") != "location-autocomplete-city-input":
            return None
    except Exception:
        return None
    parts = [x.strip() for x in value.split(",") if x.strip()]
    city = parts[0] if parts else value
    country = parts[-1] if len(parts) > 1 else ""
    if country:
        try:
            box = page.locator("[data-sr-id='location-autocomplete-country-search-input']")
            if box.count() == 1 and box.input_value().strip().lower() != country.lower():
                _fill_combobox(page, box, country)
        except Exception:
            pass
    return _fill_text(el, city)


def _css_escape(value: str) -> str:
    """Escape an id for use in a CSS selector (Greenhouse ids are plain, but
    embedded forms sometimes carry ':' and '.' which would break the query)."""
    return re.sub(r"([^a-zA-Z0-9_-])", r"\\\1", value)


def _readback(plan: FieldPlan, el: Locator | None = None) -> bool:
    """True when the control actually holds a value right now.

    Comboboxes must be read from the rendered `.select__single-value` text,
    because react-select CLEARS its <input> once an option is committed.
    """
    target = el if el is not None else plan.locator
    try:
        if plan.kind == "combobox":
            # Greenhouse's '#country' is the phone dial-code picker: a correct
            # 'India +91' pick renders as just '+91'.
            dial_ok = ((plan.meta or {}).get("id") or "") == "country"
            return bool(_combobox_value(target, allow_dial=dial_ok).strip())
        if plan.kind == "select":
            return bool((target.input_value() or "").strip())
        current = (target.input_value() or "").strip()
        if current and plan.semantic == "email" and plan.value \
                and current.lower() != str(plan.value).strip().lower():
            return False   # corrupted address - repair it
        if current:
            return True
        # A plain text input that react-select swapped for a chip.
        return bool(_combobox_value(target).strip())
    except Exception:
        # Control vanished (conditional field collapsed) - don't call that a failure.
        return True


def captcha_present(page: Page) -> tuple[bool, str]:
    """Visible challenge widget -> we must not pretend a submit succeeded.

    Scans every frame: on embedded ATS forms the captcha iframe element lives
    inside the embed frame, so a main-frame-only query misses it entirely.
    """
    try:
        contexts = [page] + [f for f in page.frames if f is not page.main_frame]
    except Exception:
        contexts = [page]

    for ctx in contexts:
        try:
            url = (getattr(ctx, "url", "") or "").lower()
        except Exception:
            url = ""
        # Content *inside* a captcha frame is always 'visible' to itself; what matters
        # is whether the host page actually shows that frame.
        if ctx is not page and any(h in url for h in ("hcaptcha", "recaptcha", "turnstile", "challenges.cloudflare")):
            continue
        found, detail = _captcha_in(ctx)
        if found:
            return True, detail
    return False, ""


# True only when an element is really painted: not display:none / visibility:hidden /
# opacity:0 on itself or any ancestor (invisible hCaptcha keeps a full-viewport
# iframe permanently mounted with visibility:hidden). For a container (.h-captcha)
# we require a really-visible iframe inside it.
_SHOWN_JS = """
(el) => {
  const shown = (n) => {
    const r = n.getBoundingClientRect();
    if (r.width <= 60 || r.height <= 30) return null;
    for (let x = n; x && x.nodeType === 1; x = x.parentElement) {
      const s = getComputedStyle(x);
      if (s.display === 'none' || s.visibility === 'hidden' || s.visibility === 'collapse') return null;
      if (parseFloat(s.opacity || '1') < 0.05) return null;
    }
    return [Math.round(r.width), Math.round(r.height)];
  };
  if (el.tagName === 'IFRAME') return shown(el);
  for (const f of el.querySelectorAll('iframe')) { const v = shown(f); if (v) return v; }
  return null;
}
"""


def _captcha_in(ctx) -> tuple[bool, str]:
    for selector in CAPTCHA_HINTS:
        try:
            loc = ctx.locator(selector)
            total = loc.count()
        except Exception:
            continue
        for idx in range(min(total, 6)):
            node = loc.nth(idx)
            try:
                dims = node.evaluate(_SHOWN_JS, timeout=1500)
            except Exception:
                continue
            if dims:
                kind = "hcaptcha" if "hcaptcha" in selector or "h-captcha" in selector else "recaptcha"
                return True, f"{kind} challenge visible ({dims[0]}x{dims[1]})"
    return False, ""


def missing_required(plans: list[FieldPlan], outcome: dict[str, Any] | None = None) -> list[str]:
    """Required fields that are genuinely still unanswered before submitting.

    Looking only at `action == "skip"` plans was unsound: a field we TRIED to
    fill and failed on (Turing's Country*/Location*) slipped straight past the
    applier's guard. So hard failures from apply_plan count as missing too.
    """
    # A checkbox group (same name) needs ONE tick, not every member ticked.
    ticked = {
        (p.meta or {}).get("name") for p in plans
        if p.kind == "checkbox" and p.action == "fill" and p.value
        and _lower(str(p.value)).startswith(("y", "t", "a", "1")) and (p.meta or {}).get("name")
    }

    def _group_ok(p) -> bool:
        return p.kind == "checkbox" and (p.meta or {}).get("name") in ticked

    missing = [
        p.label or p.semantic
        for p in plans
        if p.required and p.action == "skip" and not is_eeo(p.label) and not _group_ok(p)
    ]
    if outcome:
        labels = {p.label: p for p in plans}
        # A later 'repaired'/'filled' entry for the same label supersedes an
        # earlier failure (first fill missed, verify pass re-entered it).
        final: dict[str, str] = {}
        for entry in outcome.get("audit", []):
            final[entry.get("label", "")] = entry.get("status", "")
        for entry in outcome.get("audit", []):
            if entry.get("status") not in ("failed", "empty"):
                continue
            if final.get(entry.get("label", "")) in ("repaired", "filled"):
                continue
            plan = labels.get(entry.get("label", ""))
            if plan and plan.required and not is_eeo(plan.label) and not _group_ok(plan):
                missing.append(plan.label or plan.semantic)
    # Preserve order, drop duplicates (Country* appears twice: real + decoy).
    return list(dict.fromkeys(missing))
