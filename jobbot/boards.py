"""Auto-discover new company job boards that have India-located openings.

The bot only applies where it already knows a board token. This module probes
a list of candidate company tokens on the ATS platforms the bot can submit to
(SmartRecruiters, Ashby, Recruitee, Workable, Lever), keeps the boards that
are real (respond with postings) AND have at least one India-located job, and
saves them to data/discovered_boards.yaml. config.load_companies() merges that
file in, so the next discovery pass picks the new boards up automatically.

Greenhouse is deliberately never probed - the user does not want applications
sent through Greenhouse.
"""
from __future__ import annotations

import concurrent.futures as cf
import time
from pathlib import Path
from typing import Any

import yaml

from .config import CONFIG_DIR, DATA_DIR

OUT_FILE = DATA_DIR / "discovered_boards.yaml"
EXTRA_CANDIDATES = CONFIG_DIR / "board_candidates.yaml"

# Companies with large Indian engineering teams. Tokens are tried as given and
# in lowercase; a nonexistent token simply returns no postings and is dropped.
_COMPANIES = [
    "Freshworks", "Swiggy", "Zomato", "Razorpay", "Meesho", "Cred", "Groww", "Zepto",
    "PhonePe", "Paytm", "Ola", "OlaElectric", "Flipkart", "Myntra", "Nykaa", "Lenskart",
    "Urbancompany", "Dream11", "Sharechat", "Unacademy", "Byjus", "upGrad", "Vedantu",
    "PhysicsWallah", "Cars24", "Spinny", "Delhivery", "Rivigo", "Udaan", "Dunzo", "BlinkIt",
    "BigBasket", "Licious", "Curefit", "Practo", "PharmEasy", "Innovaccer", "Postman",
    "BrowserStack", "Chargebee", "Zoho", "Druva", "Hasura", "Darwinbox", "Leadsquared",
    "Mindtickle", "Whatfix", "Sprinklr", "Capillary", "MoEngage", "CleverTap", "WebEngage",
    "Gupshup", "Yellow", "Haptik", "Uniphore", "Observe", "Exotel", "Ozonetel", "Knowlarity",
    "InMobi", "Glance", "Juspay", "Cashfree", "PayU", "Pine Labs", "PineLabs", "BharatPe",
    "Slice", "Jupiter", "Fi", "Navi", "Zerodha", "Smallcase", "Upstox", "Angelone",
    "PolicyBazaar", "Acko", "Digit", "Zeta", "M2P", "Perfios", "Signzy", "IDfy", "HyperVerge",
    "Rapido", "Porter", "Shiprocket", "Shadowfax", "Ecom Express", "Xpressbees", "Infra.Market",
    "OfBusiness", "Moglix", "Zetwerk", "Bizongo", "Jumbotail", "Ninjacart", "DeHaat",
    "Livspace", "NoBroker", "Housing", "MagicBricks", "Square Yards", "Stanza", "OYO",
    "MakeMyTrip", "Ixigo", "EaseMyTrip", "Cleartrip", "Yatra", "Tata1mg", "Tata", "Infosys",
    "Wipro", "HCLTech", "TechMahindra", "LTIMindtree", "Mphasis", "Persistent", "Coforge",
    "Zensar", "Cognizant", "Capgemini", "Accenture", "Deloitte", "EY", "KPMG", "PwC",
    "Genpact", "EXL", "WNS", "Publicis Sapient", "PublicisSapient", "Thoughtworks", "Nagarro",
    "GlobalLogic", "EPAM", "Grid Dynamics", "Endava", "Xebia", "Sigmoid", "Fractal",
    "MuSigma", "Tredence", "LatentView", "TheMathCompany", "Quantiphi", "Tiger Analytics",
    "Visa", "Mastercard", "AmericanExpress", "PayPal", "Intuit", "Adobe", "Atlassian",
    "Walmart", "Target", "Lowes", "Nike", "Siemens", "Philips", "ABB", "Schneider Electric",
    "Continental", "ZF", "Mercedes-Benz", "Volvo", "Daimler", "Honeywell", "Ericsson",
    "Nokia", "Cisco", "Juniper", "Arista", "Nutanix", "VMware", "Citrix", "Oracle", "SAP",
    "ServiceNow", "Salesforce", "Workday", "Zscaler", "Palo Alto Networks", "Fortinet",
    "Okta", "Twilio", "Freshdesk", "Hike", "Josh", "Moj", "Pocket FM", "PocketFM", "KukuFM",
    "Eloelo", "Classplus", "Testbook", "Scaler", "Masai", "Newton School", "Simplilearn",
    "Great Learning", "Eruditus", "Emeritus", "Leap", "Leverage Edu", "Turing", "Uplers",
    "Toptal", "Deel", "Remote", "Multiplier", "Rippling", "Gojek", "Grab", "Agoda",
    "Booking", "Expedia", "Airbnb", "Uber", "Lyft", "DoorDash", "Instacart", "Swiggy Instamart",
    "Sarvam", "Krutrim", "Yellow.ai", "Rephrase", "Gan", "Wysa", "Avataar", "Mad Street Den",
    "Locus", "LogiNext", "FarEye", "Vymo", "Kapture", "Kissflow", "Zluri", "SpotDraft",
    "Atlan", "Rocketlane", "Plum", "Loop", "Even", "Pristyn Care", "Medikabazaar",
    "Healthify", "HealthifyMe", "Ultrahuman", "Boat", "Noise", "Ather", "Ola Krutrim",
    "Simpl", "LazyPay", "KreditBee", "MoneyView", "Fibe", "Lendingkart", "Indifi",
    "Kotak", "HDFC", "ICICI", "Axis", "Yes Bank", "IDFC", "Airtel", "Jio", "Vodafone Idea",
    "Bosch", "NielsenIQ", "Sectigo", "Ubisoft", "Publicis", "Dentsu", "WPP", "GroupM",
]


def _tokens(name: str) -> list[str]:
    base = name.replace(" ", "").replace(".", "")
    out = []
    for t in (base, base.lower(), name.lower().replace(" ", "-"), name.lower().replace(" ", "")):
        if t and t not in out:
            out.append(t)
    return out


def _candidates() -> dict[str, list[tuple[str, str]]]:
    """(token, display name) pairs per ATS source."""
    names = list(_COMPANIES)
    if EXTRA_CANDIDATES.exists():
        try:
            extra = yaml.safe_load(EXTRA_CANDIDATES.read_text()) or []
            names += [str(x) for x in extra if x]
        except Exception:
            pass
    pairs = [(t, n) for n in dict.fromkeys(names) for t in _tokens(n)]
    # SmartRecruiters tokens are case-insensitive, so the lowercase variants
    # would just duplicate requests there.
    sr = [(t, n) for t, n in pairs if t == n.replace(" ", "").replace(".", "")]
    return {
        "smartrecruiters": sr,
        "ashby": pairs,
        "recruitee": [(t, n) for t, n in pairs if t.islower() and "-" not in t],
        "workable": [(t, n) for t, n in pairs if t.islower()],
        "lever": [(t, n) for t, n in pairs if t.islower()],
    }


def _known(cfg: dict[str, Any]) -> dict[str, set[str]]:
    known: dict[str, set[str]] = {}
    for src, entries in cfg.items():
        if isinstance(entries, list):
            known[src] = {str(e.get("token", "")).lower() for e in entries if isinstance(e, dict)}
    return known


def probe(workers: int = 32, progress=None) -> dict[str, list[dict[str, Any]]]:
    """Probe candidate tokens; return {source: [{token, name, ai, india_jobs}]}."""
    from . import sources
    from .config import load_companies
    from .matcher import GEO_INDIA, classify_geo

    known = _known(load_companies(include_discovered=False))
    tasks = []
    for src, pairs in _candidates().items():
        if src not in sources.FETCHERS:
            continue
        for token, name in pairs:
            if token.lower() in known.get(src, set()):
                continue
            tasks.append((src, token, name))

    found: dict[str, dict[str, dict[str, Any]]] = {}

    def run(task):
        src, token, name = task
        try:
            jobs = sources.FETCHERS[src](token, name, False)
        except Exception:
            return task, 0, 0
        india = sum(1 for j in jobs if classify_geo(j)[0] == GEO_INDIA)
        return task, len(jobs), india

    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        for (src, token, name), total, india in pool.map(run, tasks):
            if progress:
                progress(src, token, total, india)
            if india <= 0:
                continue
            bucket = found.setdefault(src, {})
            key = token.lower()
            if key not in bucket or india > bucket[key]["india_jobs"]:
                bucket[key] = {"token": token, "name": name, "ai": False, "india_jobs": india}
    return {src: sorted(b.values(), key=lambda e: -e["india_jobs"]) for src, b in found.items()}


def save(result: dict[str, list[dict[str, Any]]]) -> Path:
    old = load_discovered()
    # Keep boards found earlier too - a board with no India opening today
    # may have one tomorrow, and the fetch is cheap.
    for src, entries in old.items():
        have = {e["token"].lower() for e in result.get(src, [])}
        for e in entries:
            if e.get("token", "").lower() not in have:
                result.setdefault(src, []).append(e)
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    body = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), **result}
    OUT_FILE.write_text(yaml.safe_dump(body, sort_keys=False, allow_unicode=True))
    return OUT_FILE


def load_discovered() -> dict[str, list[dict[str, Any]]]:
    if not OUT_FILE.exists():
        return {}
    try:
        data = yaml.safe_load(OUT_FILE.read_text()) or {}
    except Exception:
        return {}
    return {k: v for k, v in data.items() if isinstance(v, list)}


def is_stale(max_age_h: float = 24) -> bool:
    if not OUT_FILE.exists():
        return True
    return (time.time() - OUT_FILE.stat().st_mtime) > max_age_h * 3600


def refresh(progress=None) -> dict[str, list[dict[str, Any]]]:
    result = probe(progress=progress)
    save(result)
    return load_discovered()
