"""Core data structures. Kept dependency-free so every module can import this."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any


# Application outcome codes. These are reported VERBATIM in the sheet/report --
# we never upgrade a blocked application into a "submitted" one.
SUBMITTED = "SUBMITTED"
CAPTCHA_BLOCKED = "CAPTCHA_BLOCKED"
FAILED_VALIDATION = "FAILED_VALIDATION"
LOGIN_REQUIRED = "LOGIN_REQUIRED"
ALREADY_APPLIED = "ALREADY_APPLIED"
CLOSED = "CLOSED"
ERROR = "ERROR"
SKIPPED = "SKIPPED"


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class Job:
    """A single real job posting discovered from an ATS."""

    source: str  # greenhouse | lever | ashby | arbeitnow | remoteok
    company: str
    company_token: str
    job_id: str
    title: str
    location: str
    url: str  # public posting page
    apply_url: str  # page that actually contains the form
    description: str = ""
    is_remote: bool = False
    department: str = ""
    employment_type: str = ""
    posted_at: str = ""
    company_is_ai: bool = False

    @property
    def key(self) -> str:
        """Stable dedupe identity for a posting."""
        return f"{self.source}:{self.company_token}:{self.job_id}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["key"] = self.key
        d.pop("description", None)  # too large for logs/sheets
        return d


@dataclass
class Match:
    """A job scored against one candidate."""

    job: Job
    candidate_id: str
    score: float
    ai_score: float
    role_score: float
    location_score: float
    reasons: list[str] = field(default_factory=list)
    geo: str = "unknown"  # india | remote_open | remote_foreign | abroad | unknown

    @property
    def why(self) -> str:
        return "; ".join(self.reasons)


@dataclass
class ApplicationResult:
    """Outcome of one apply attempt. Truthful by construction."""

    candidate_id: str
    candidate_name: str
    job: Job
    status: str
    detail: str = ""
    fields_filled: int = 0
    fields_skipped: list[str] = field(default_factory=list)
    resume_attached: bool = False
    screenshot: str = ""
    confirmation_text: str = ""
    score: float = 0.0
    applied_at: str = field(default_factory=utcnow)
    duration_s: float = 0.0
    # --- contact snapshot, copied verbatim from the resume-derived profile ---
    email: str = ""
    phone: str = ""
    candidate_location: str = ""
    linkedin: str = ""
    github: str = ""
    portfolio: str = ""
    resume_file: str = ""
    match_reasons: str = ""

    @property
    def submitted(self) -> bool:
        return self.status == SUBMITTED

    def row(self) -> list[str]:
        """Flat row for CSV / Google Sheets. Order must match SHEET_HEADERS."""
        import os

        return [
            self.applied_at,
            self.candidate_name,
            self.email,
            self.phone,
            self.candidate_location,
            self.job.company,
            self.job.title,
            self.job.location,
            self.job.source,
            "AI company" if self.job.company_is_ai else "Tech",
            f"{self.score:.1f}",
            self.status,
            self.detail[:300],
            "yes" if self.resume_attached else "no",
            os.path.basename(self.resume_file),
            str(self.fields_filled),
            ", ".join(self.fields_skipped[:6]),
            self.linkedin,
            self.github,
            self.portfolio,
            self.match_reasons[:300],
            self.job.apply_url,
            self.screenshot,
        ]


SHEET_HEADERS = [
    "Applied At (UTC)",
    "Candidate",
    "Email Used",
    "Phone Used",
    "Candidate Location",
    "Company",
    "Job Title",
    "Job Location",
    "Source",
    "Company Type",
    "Match Score",
    "Status",
    "Detail",
    "Resume Attached",
    "Resume File",
    "Fields Filled",
    "Fields Skipped",
    "LinkedIn",
    "GitHub",
    "Portfolio",
    "Why Matched",
    "Apply URL",
    "Screenshot",
]
