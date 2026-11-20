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
    "self identif", "protected veteran", "accommodation",
)

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
    ("full_name", ("full name", "your name", "name", "candidate name")),
    ("email", ("email", "e-mail")),
    ("phone", ("phone", "mobile", "contact number", "telephone", "cell")),
    ("linkedin", ("linkedin",)),
    ("github", ("github", "git hub")),
    ("portfolio", ("portfolio", "website", "personal site", "blog", "url", "web page")),
    ("company", ("current company", "current employer", "employer", "company")),
    ("title", ("current title", "current role", "job title", "your title")),
    ("location", ("location", "city", "where are you based", "current city")),
    ("country", ("country",)),
    ("school", ("school", "university", "college", "institution")),
    ("degree", ("degree",)),
    ("discipline", ("discipline", "major", "field of study")),
    ("grad_year", ("graduation", "grad year", "year of completion", "end date")),
    ("years", ("years of experience", "how many years", "total experience", "yoe")),
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
)


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
    return any(word in low for word in EEO_WORDS)


def classify(label: str, name: str = "", el_id: str = "") -> str:
    """Map a control to a semantic key using label text, then name/id."""
    if is_eeo(label):
        return "eeo"
    hay = _lower(label)
    for key, needles in SEMANTICS:
        if any(n in hay for n in needles):
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


def resolve_value(
    label: str, semantic: str, identity: Identity, answers: dict, job=None
) -> tuple[str | None, str]:
    """Return (value, reason). value None means skip -- never a guess."""
    if is_eeo(label):
        return None, "EEO/demographic field - intentionally not answered"

    if semantic in IDENTITY_KEYS:
        token = IDENTITY_TOKEN[semantic]
        value = identity.resolve_token(token)
        if value:
            return value, f"resume field ({semantic})"
        return None, f"not present in resume ({semantic}) - skipped, never faked"

    rule_answer = _answer_rules(label, answers)
    if rule_answer == "ANSWER_SKIP":
        return None, "configured as skip"
    if rule_answer:
        if rule_answer.startswith("ANSWER_"):
            resolved = identity.resolve_token(rule_answer)
            if resolved:
                return resolved, f"resume field via rule ({rule_answer})"
            return None, f"rule needs {rule_answer} but resume lacks it - skipped"
        if rule_answer == "ANSWER_BRAIN":
            # Delegate to brain for country-aware, tech-scoped answers
            if job:
                value, reason = brain.answer(label, identity, job, [])
                if value is not None:
                    return value, reason
            return None, f"brain cannot answer {semantic} without job context"
        return rule_answer, "confirmed answer from answers.yaml"

    # Final fallback: try brain if job is available
    if job:
        value, reason = brain.answer(label, identity, job, [])
        if value is not None:
            return value, reason

    return None, "no resume fact or confirmed answer matches this question"


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
                  .map((id) => document.getElementById(id))
                  .filter(Boolean)
                  .map((n) => clean(n.innerText));
                if (clean(parts.join(' '))) return clean(parts.join(' '));
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
              required: (n.required || n.getAttribute('aria-required') === 'true') ? '1' : '',
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



def plan_fields(
    page: Page, identity: Identity, title: str = "", company: str = "", job=None
) -> list[FieldPlan]:
    """Walk every control on the page and decide fill-or-skip for each."""
    answers = load_answers()
    plans: list[FieldPlan] = []
    seen_radio_groups: set[str] = set()

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

        if not a.get("visible") and kind != "file":
            continue

        # Skip react-select decoy inputs: hidden validation inputs with empty
        # id/name/type/role but visible=1. These are duplicates of the real combobox.
        if (tag == "input" and a.get("visible") and not a.get("id") and
            not a.get("name") and not ftype and not a.get("role")):
            continue
        label = label_for(page, el)
        name, el_id = a.get("name", ""), a.get("id", "")
        required = bool(a.get("required"))

        if kind == "radio":
            group = name or label
            if group in seen_radio_groups:
                continue
            seen_radio_groups.add(group)

        if kind == "file":
            # Greenhouse ships #resume AND #cover_letter as file inputs that are
            # BOTH labelled 'Attach', so the label cannot tell them apart. Route
            # on id/name instead: the resume goes to the resume input, and an
            # optional cover-letter upload is left alone rather than being fed a
            # duplicate resume PDF.
            slot = f"{el_id} {name}".lower()
            if "cover" in slot or "letter" in slot:
                plans.append(FieldPlan(
                    kind="file", label=label or "Cover letter", semantic="cover_letter",
                    required=required, value="", action="skip",
                    reason="optional cover-letter upload - the resume belongs in the resume slot",
                    locator=el, meta={"name": name, "id": el_id},
                ))
                continue
            plans.append(FieldPlan(
                kind="file", label=label or "Resume", semantic="resume",
                required=required, value=identity.resume, action="fill",
                reason="resume file from profile", locator=el,
                meta={"name": name, "id": el_id},
            ))
            continue

        semantic = classify(label, name, el_id)

        # Conditional follow-ups ("If other, please specify") belong to a branch
        # we did not take - answering them would be noise, so leave them blank.
        if is_conditional(label) and not required:
            plans.append(FieldPlan(
                kind=kind, label=label, semantic=semantic, required=required,
                value=None, action="skip",
                reason="conditional follow-up field - not applicable to our answers",
                locator=el, meta={"name": name, "id": el_id},
            ))
            continue

        # Long free-text that reads like an essay -> cover-letter style answer.
        if kind == "textarea" and semantic in ("question", "cover_letter"):
            text = cover_letter_text(identity, answers, title, company)
            rule = _answer_rules(label, answers)
            if rule and rule != "ANSWER_SKIP" and not rule.startswith("ANSWER_"):
                value, reason = rule, "confirmed answer from answers.yaml"
            elif is_eeo(label):
                value, reason = None, "EEO/demographic - intentionally not answered"
            elif text:
                value, reason = text, "cover-letter template built from resume facts"
            else:
                value, reason = None, "no confirmed answer for this free-text question"
            plans.append(FieldPlan(
                kind="textarea", label=label, semantic="cover_letter",
                required=required, value=value,
                action="fill" if value else "skip", reason=reason, locator=el,
                meta={"name": name, "id": el_id},
            ))
            continue

        value, reason = resolve_value(label, semantic, identity, answers, job)

        # Some boards mark EEO/demographic dropdowns as required. We never invent
        # personal characteristics, but "decline to self-identify" is a real,
        # honest option the form itself offers, so take it instead of stalling.
        if value is None and semantic == "eeo" and required and kind in ("select", "combobox", "radio"):
            plans.append(FieldPlan(
                kind=kind, label=label, semantic="eeo_decline", required=required,
                value=DECLINE_SENTINEL, action="fill",
                reason="required EEO field - choosing the form's own 'decline to self-identify' option",
                locator=el, meta={"name": name, "id": el_id},
            ))
            continue

        plans.append(FieldPlan(
            kind=kind, label=label, semantic=semantic, required=required,
            value=value, action="fill" if value else "skip", reason=reason,
            locator=el, meta={"name": name, "id": el_id},
        ))

    return plans


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
            if _norm(current):
                return True
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
    seen: list[str] = []
    for q in out:
        if q and q not in seen:
            seen.append(q)
    return seen


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
    probes = [_lower(value), _lower(query), _lower(value.split(",")[0])]
    for probe in probes:
        if not probe:
            continue
        for test in (lambda t, p: t == p, lambda t, p: t.startswith(p), lambda t, p: p in t):
            for text, opt in pairs:
                if test(text, probe):
                    return opt, text
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


def _fill_combobox(page: Page, el: Locator, value: str) -> bool:
    """React-select / autocomplete: click, type, then choose from the listbox."""
    queries = _combobox_queries(value)
    # Options must be hunted inside THIS field's own select container. A global
    # search also sees the phone widget's dial-code menu, which is how typing
    # 'India' into Country* ended up committing the dial code +91 instead.
    scope = _select_scope(el)
    if not queries:
        return False
    saw_options = False

    for query in queries:
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
        try:
            el.type(query[:60], delay=20, timeout=10000)
        except Exception:
            continue

        # Autocompletes hit a network endpoint; poll instead of a fixed sleep.
        options: list[Locator] = []
        for _ in range(10):
            page.wait_for_timeout(300)
            options = _visible_options(page, scope)
            if not options and scope is not None:
                options = _visible_options(page)
            if options:
                break
        if not options:
            _dismiss_menu(page, el)
            continue
        saw_options = True

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
            if _combobox_settled(el):
                return True
        except Exception:
            pass

    _dismiss_menu(page, el)
    return _combobox_settled(el)


def _set_checkbox(el: Locator, value: str) -> bool:
    want_yes = _lower(value).startswith(("y", "t", "a", "1"))
    try:
        if want_yes:
            el.check(timeout=6000)
        else:
            el.uncheck(timeout=6000)
        return True
    except Exception:
        try:
            el.click(timeout=4000)
            return True
        except Exception:
            return False


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
    for idx in range(min(total, 12)):
        radio = group.nth(idx)
        text = _lower(label_for(page, radio))
        if text and (want == text or want in text or text in want):
            try:
                radio.check(timeout=5000)
                return True
            except Exception:
                try:
                    radio.click(timeout=4000)
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

    for plan in plans:
        if plan.action == "skip" or not plan.value:
            skipped.append(f"{plan.label or plan.semantic} ({plan.reason})")
            audit.append({"label": plan.label, "status": "skipped", "reason": plan.reason})
            continue

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
            ok = _fill_combobox(page, plan.locator, plan.value)
        elif plan.kind == "checkbox":
            ok = _set_checkbox(plan.locator, plan.value)
        elif plan.kind == "radio":
            ok = _set_radio(page, plan.locator, plan.label, plan.value)
        else:
            ok = _fill_text(plan.locator, plan.value)
            if not ok:
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
        if plan.kind in ("checkbox", "radio") or plan.value == DECLINE_SENTINEL:
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


def _relocate(page: Page, plan: FieldPlan) -> Locator:
    """Fresh locator for a planned field, by id when the ATS gave us one.

    Greenhouse's React form remounts inputs while validating, which detaches
    the locator captured during planning: reads then return '' and re-fills
    land on a dead node. Re-querying by id is what makes the repair pass real.
    """
    el_id = (plan.meta or {}).get("id") or ""
    if el_id:
        try:
            fresh = page.locator(f"#{_css_escape(el_id)}")
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
            return bool(_combobox_value(target).strip())
        if plan.kind == "select":
            return bool((target.input_value() or "").strip())
        current = (target.input_value() or "").strip()
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
        found, detail = _captcha_in(ctx)
        if found:
            return True, detail
    return False, ""


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
                if not node.is_visible(timeout=1200):
                    continue
                box = node.bounding_box()
            except Exception:
                continue
            if box and box.get("width", 0) > 60 and box.get("height", 0) > 30:
                kind = "hcaptcha" if "hcaptcha" in selector or "h-captcha" in selector else "recaptcha"
                return True, f"{kind} widget visible ({int(box['width'])}x{int(box['height'])})"
    return False, ""


def missing_required(plans: list[FieldPlan], outcome: dict[str, Any] | None = None) -> list[str]:
    """Required fields that are genuinely still unanswered before submitting.

    Looking only at `action == "skip"` plans was unsound: a field we TRIED to
    fill and failed on (Turing's Country*/Location*) slipped straight past the
    applier's guard. So hard failures from apply_plan count as missing too.
    """
    missing = [
        p.label or p.semantic
        for p in plans
        if p.required and p.action == "skip" and not is_eeo(p.label)
    ]
    if outcome:
        labels = {p.label: p for p in plans}
        for entry in outcome.get("audit", []):
            if entry.get("status") not in ("failed", "empty"):
                continue
            plan = labels.get(entry.get("label", ""))
            if plan and plan.required and not is_eeo(plan.label):
                missing.append(plan.label or plan.semantic)
    # Preserve order, drop duplicates (Country* appears twice: real + decoy).
    return list(dict.fromkeys(missing))
