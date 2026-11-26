"""Resume-only identity enforcement.

The user's rule is absolute: **every value typed into an application form must
come from that candidate's own resume** (via config/profiles.yaml) or from the
explicitly approved answer policy (config/answers.yaml). Nothing may be
invented, guessed, borrowed from the other candidate, or defaulted to a
placeholder.

This module is the ONLY place allowed to produce fill values. `forms.py` asks
it for data and can never fabricate. Anything unknown comes back as `None`,
which the filler reports as a skipped field instead of making something up.
"""

from __future__ import annotations

import os
import re

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)
PHONE_RE = re.compile(r"^\+?\d[\d\s\-()]{7,}$")


class ProfileError(RuntimeError):
    """Raised when a profile is unusable, so we fail loudly instead of guessing."""


class Identity:
    """Validated, read-only view of one candidate's resume facts."""

    # Fields that are genuinely allowed to be empty (absent from the resume).
    OPTIONAL = frozenset({"github", "portfolio", "linkedin", "state"})

    REQUIRED = ("id", "full_name", "first_name", "last_name", "email", "phone", "resume")

    def __init__(self, cand: dict):
        self._c = {**dict(cand), **_private_overrides(str(cand.get("id") or ""))}
        self._verify()

    # ------------------------------------------------------------------ checks
    def _verify(self) -> None:
        cid = self._c.get("id", "<unknown>")
        for key in self.REQUIRED:
            if not str(self._c.get(key) or "").strip():
                raise ProfileError(f"[{cid}] required resume field '{key}' is missing")

        if not EMAIL_RE.match(self.email):
            raise ProfileError(f"[{cid}] email from resume looks invalid: {self.email!r}")
        if not PHONE_RE.match(self.phone):
            raise ProfileError(f"[{cid}] phone from resume looks invalid: {self.phone!r}")

        resume = self.resume
        if not os.path.isfile(resume):
            raise ProfileError(f"[{cid}] resume file not found on disk: {resume}")
        if not resume.lower().endswith((".pdf", ".doc", ".docx")):
            raise ProfileError(f"[{cid}] resume is not a document file: {resume}")
        if os.path.getsize(resume) < 10_000:
            raise ProfileError(f"[{cid}] resume file suspiciously small: {resume}")

    # --------------------------------------------------------------- accessors
    @property
    def id(self) -> str:
        return str(self._c["id"])

    @property
    def full_name(self) -> str:
        return str(self._c["full_name"]).strip()

    @property
    def first_name(self) -> str:
        return str(self._c["first_name"]).strip()

    @property
    def last_name(self) -> str:
        return str(self._c["last_name"]).strip()

    @property
    def email(self) -> str:
        return str(self._c["email"]).strip()

    @property
    def phone(self) -> str:
        return str(self._c["phone"]).strip()

    @property
    def phone_display(self) -> str:
        return str(self._c.get("phone_display") or self.phone).strip()

    @property
    def location(self) -> str:
        return str(self._c.get("location") or "").strip()

    @property
    def city(self) -> str:
        return str(self._c.get("city") or "").strip()

    @property
    def country(self) -> str:
        return str(self._c.get("country") or "India").strip()

    @property
    def linkedin(self) -> str:
        return str(self._c.get("linkedin") or "").strip()

    @property
    def github(self) -> str:
        return str(self._c.get("github") or "").strip()

    @property
    def portfolio(self) -> str:
        return str(self._c.get("portfolio") or "").strip()

    @property
    def resume(self) -> str:
        raw = str(self._c["resume"]).strip()
        if raw and not os.path.isfile(raw):
            # Portable fallback (cloud runner): same file name under data/resumes/.
            here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            alt = os.path.join(here, "data", "resumes", os.path.basename(raw))
            if os.path.isfile(alt):
                return alt
        return raw

    @property
    def years_experience(self) -> int:
        return int(self._c.get("years_experience") or 0)

    @property
    def current_company(self) -> str:
        return str(self._c.get("current_company") or "").strip()

    @property
    def current_title(self) -> str:
        return str(self._c.get("current_title") or "").strip()

    @property
    def school(self) -> str:
        return str(self._c.get("school") or "").strip()

    @property
    def degree(self) -> str:
        return str(self._c.get("degree") or "").strip()

    @property
    def discipline(self) -> str:
        return str(self._c.get("discipline") or "").strip()

    @property
    def grad_year(self) -> str:
        return str(self._c.get("grad_year") or "").strip()

    @property
    def education_line(self) -> str:
        """'B.Tech ... , Medicaps University, Indore, 2024' built from resume facts only."""
        bits = [b for b in (self.degree, self.discipline, self.school, self.grad_year) if b]
        return ", ".join(bits)

    # --- salary expectations (user-confirmed, stored per candidate) ----------
    @property
    def expected_ctc(self) -> str:
        """Expected CTC as the user stated it: Harshank 14-15 LPA, Divya 4-5 LPA."""
        return str(self._c.get("expected_ctc") or "").strip()

    @property
    def expected_ctc_inr(self) -> str:
        """Numeric rupee figure, for forms that only accept digits."""
        return str(self._c.get("expected_ctc_inr") or "").strip()

    @property
    def expected_ctc_lpa(self) -> str:
        """Top of the stated range as a bare number (e.g. '15')."""
        return str(self._c.get("expected_ctc_max_lpa") or "").strip()

    @property
    def current_ctc(self) -> str:
        return str(self._c.get("current_ctc") or "").strip()

    @property
    def raw(self) -> dict:
        """Underlying profile dict (for the matcher, which needs keyword lists)."""
        return self._c

    # ------------------------------------------------------------- answer slots
    def resolve_token(self, token: str) -> str | None:
        """Resolve an ANSWER_* placeholder from answers.yaml using resume facts.

        Returns None when the resume genuinely has no such value, so the caller
        skips the field rather than inventing one (e.g. Divya has no GitHub).
        """
        table: dict[str, str] = {
            "ANSWER_NAME": self.full_name,
            "ANSWER_FIRST": self.first_name,
            "ANSWER_LAST": self.last_name,
            "ANSWER_EMAIL": self.email,
            "ANSWER_PHONE": self.phone_display,
            "ANSWER_LOCATION": self.location,
            "ANSWER_CITY": self.city,
            "ANSWER_COUNTRY": self.country,
            "ANSWER_LINKEDIN": self.linkedin,
            "ANSWER_GITHUB": self.github,
            "ANSWER_PORTFOLIO": self.portfolio,
            "ANSWER_COMPANY": self.current_company,
            "ANSWER_TITLE": self.current_title,
            "ANSWER_YEARS": str(self._c.get("years_experience") or "") ,
            "ANSWER_SCHOOL": self.school,
            "ANSWER_DEGREE": self.degree,
            "ANSWER_DISCIPLINE": self.discipline,
            "ANSWER_GRAD_YEAR": self.grad_year,
            # One-line education summary, assembled only from resume facts.
            "ANSWER_EDUCATION": self.education_line,
            "ANSWER_EXPECTED_CTC": self.expected_ctc,
            "ANSWER_EXPECTED_CTC_INR": self.expected_ctc_inr,
            "ANSWER_EXPECTED_CTC_LPA": self.expected_ctc_lpa,
            "ANSWER_CURRENT_CTC": self.current_ctc,
        }
        if token not in table:
            return None
        value = table[token].strip()
        return value or None

    def contact_snapshot(self) -> dict:
        """Exactly what gets logged to the sheet / markdown report."""
        return {
            "email": self.email,
            "phone": self.phone_display,
            "candidate_location": self.location,
            "linkedin": self.linkedin,
            "github": self.github,
            "portfolio": self.portfolio,
            "resume_file": self.resume,
        }


def _private_overrides(cid: str) -> dict:
    """Facts the user confirmed in chat but does not want on the site or in git.

    Stored in data/private_profile.json (data/ is git-ignored, never snapshotted).
    """
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "data" / "private_profile.json"
    try:
        data = json.loads(path.read_text())
    except Exception:
        return {}
    return dict(data.get(cid) or {})


def load_identity(cand: dict) -> Identity:
    return Identity(cand)
