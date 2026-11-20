"""YAML config loading + the real-job guard."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
SHOTS_DIR = DATA_DIR / "screenshots"
REPORTS_DIR = ROOT / "reports"

for _d in (DATA_DIR, SHOTS_DIR, REPORTS_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def _load(name: str) -> dict[str, Any]:
    path = CONFIG_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"missing config file: {path}")
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_companies() -> dict[str, Any]:
    return _load("companies.yaml")


def load_profiles() -> list[dict[str, Any]]:
    return _load("profiles.yaml")["candidates"]


def load_answers() -> dict[str, Any]:
    return _load("answers.yaml")


def get_candidate(cid: str) -> dict[str, Any]:
    for c in load_profiles():
        if c["id"] == cid:
            return c
    raise KeyError(f"unknown candidate id: {cid!r}")


# ---------------------------------------------------------------- real-job guard

class RealJobGuard:
    """Rejects fake / demo / test postings.

    The user's hard requirement: never apply to a fake job. We enforce this at
    three levels -- board token, posting title, and posting URL.
    """

    def __init__(self, companies: dict[str, Any] | None = None) -> None:
        cfg = companies or load_companies()
        self.bad_tokens = {t.lower() for t in cfg.get("blacklist_tokens", [])}
        self.bad_words = [w.lower() for w in cfg.get("blacklist_title_words", [])]

    def board_ok(self, token: str) -> bool:
        t = token.lower()
        if t in self.bad_tokens:
            return False
        return not any(x in t for x in ("demo", "sandbox", "test-", "dummy"))

    def job_ok(self, job) -> tuple[bool, str]:
        """Return (is_real, reason_if_not)."""
        if not self.board_ok(job.company_token):
            return False, "blacklisted demo/test board"

        title = (job.title or "").lower().strip()
        if not title:
            return False, "empty title"

        for w in self.bad_words:
            # word-ish boundary check so "Attestation" doesn't trip on "test"
            if w in title.split() or f" {w} " in f" {title} " or title.startswith(f"{w} "):
                return False, f"title contains blacklisted word {w!r}"

        if not job.apply_url or not job.apply_url.startswith("http"):
            return False, "no usable apply URL"

        low_url = job.apply_url.lower()
        if any(x in low_url for x in ("leverdemo", "/demo/", "example.com", "localhost")):
            return False, "demo/example apply URL"

        return True, ""


# ---------------------------------------------------------------- misc settings

def sheets_credentials_path() -> str | None:
    """Service-account JSON path, if the user has supplied one."""
    for cand in (
        os.environ.get("JOBBOT_GOOGLE_CREDS", ""),
        str(CONFIG_DIR / "service_account.json"),
    ):
        if cand and os.path.exists(cand):
            return cand
    return None


SHEET_NAME = os.environ.get("JOBBOT_SHEET_NAME", "AI Job Applications - Harshank & Divya")
CHROME_PROFILE_DIR = str(DATA_DIR / "chrome-profile")
