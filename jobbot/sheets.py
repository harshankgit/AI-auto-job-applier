"""Google Sheets writer - colour-coded, one row per application attempt.

Degrades gracefully: if no service-account key is present, we say so and the
CSV/Markdown reports remain the record. We never pretend a sheet was written.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from .config import SHEET_NAME, sheets_credentials_path
from .models import SHEET_HEADERS, ApplicationResult

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Status -> row background (soft, readable) and text colour.
STATUS_COLOR = {
    "SUBMITTED": (0.80, 0.94, 0.80),          # green
    "CAPTCHA_BLOCKED": (1.00, 0.90, 0.70),    # amber
    "FAILED_VALIDATION": (1.00, 0.95, 0.75),  # pale amber
    "LOGIN_REQUIRED": (0.88, 0.88, 0.98),     # lavender
    "ALREADY_APPLIED": (0.90, 0.93, 0.96),    # grey-blue
    "CLOSED": (0.92, 0.92, 0.92),             # grey
    "ERROR": (0.99, 0.82, 0.82),              # red
    "SKIPPED": (0.95, 0.95, 0.88),            # beige
}

HEADER_BG = (0.13, 0.24, 0.38)
HEADER_FG = (1.0, 1.0, 1.0)


class SheetsUnavailable(RuntimeError):
    pass


def _client():
    path = sheets_credentials_path()
    if not path:
        raise SheetsUnavailable(
            "No Google service-account key found. Put it at config/service_account.json "
            "or set JOBBOT_GOOGLE_CREDS, then share the sheet with that key's client_email."
        )
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except Exception as exc:  # pragma: no cover
        raise SheetsUnavailable(f"gspread/google-auth not importable: {exc}") from exc

    creds = Credentials.from_service_account_file(path, scopes=SCOPES)
    return gspread.authorize(creds), creds


def _open_or_create(gc, creds, title: str):
    import gspread

    try:
        return gc.open(title)
    except gspread.SpreadsheetNotFound:
        sh = gc.create(title)
        email = getattr(creds, "service_account_email", None)
        if email:
            try:
                sh.share(email, perm_type="user", role="writer")
            except Exception:
                pass
        return sh


def _fmt(bg=None, fg=None, bold=False, wrap=False, size=None) -> dict:
    cell: dict = {}
    if bg:
        cell["backgroundColor"] = {"red": bg[0], "green": bg[1], "blue": bg[2]}
    text: dict = {}
    if fg:
        text["foregroundColor"] = {"red": fg[0], "green": fg[1], "blue": fg[2]}
    if bold:
        text["bold"] = True
    if size:
        text["fontSize"] = size
    if text:
        cell["textFormat"] = text
    if wrap:
        cell["wrapStrategy"] = "CLIP"
    return cell


def _col_letter(n: int) -> str:
    s = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        s = chr(65 + rem) + s
    return s


def push(results: list[ApplicationResult], identities: dict, sheet_name: str = SHEET_NAME) -> str:
    """Write every attempt into a colour-coded sheet. Returns the sheet URL."""
    gc, creds = _client()
    sh = _open_or_create(gc, creds, sheet_name)
    last_col = _col_letter(len(SHEET_HEADERS))

    ws = _reset_worksheet(sh, "Applications", rows=max(len(results) + 20, 60), cols=len(SHEET_HEADERS))

    rows = [SHEET_HEADERS] + [r.row() for r in results]
    ws.update(values=rows, range_name=f"A1:{last_col}{len(rows)}")

    requests = [
        {"repeatCell": {
            "range": {"sheetId": ws.id, "startRowIndex": 0, "endRowIndex": 1},
            "cell": {"userEnteredFormat": _fmt(bg=HEADER_BG, fg=HEADER_FG, bold=True, size=10)},
            "fields": "userEnteredFormat(backgroundColor,textFormat)",
        }},
        {"updateSheetProperties": {
            "properties": {"sheetId": ws.id, "gridProperties": {"frozenRowCount": 1, "frozenColumnCount": 2}},
            "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount",
        }},
        {"autoResizeDimensions": {
            "dimensions": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": len(SHEET_HEADERS)}
        }},
    ]

    for i, r in enumerate(results, start=1):
        bg = STATUS_COLOR.get(r.status)
        if not bg:
            continue
        requests.append({"repeatCell": {
            "range": {"sheetId": ws.id, "startRowIndex": i, "endRowIndex": i + 1,
                      "startColumnIndex": 0, "endColumnIndex": len(SHEET_HEADERS)},
            "cell": {"userEnteredFormat": _fmt(bg=bg, bold=r.submitted)},
            "fields": "userEnteredFormat(backgroundColor,textFormat)",
        }})

    sh.batch_update({"requests": requests})

    _write_summary(sh, results, identities)
    try:
        _write_legend(sh)
    except Exception:
        pass
    return sh.url


def _reset_worksheet(sh, title: str, rows: int, cols: int):
    try:
        ws = sh.worksheet(title)
        ws.clear()
        try:
            ws.resize(rows=rows, cols=cols)
        except Exception:
            pass
        return ws
    except Exception:
        return sh.add_worksheet(title=title, rows=rows, cols=cols)


def _write_summary(sh, results: list[ApplicationResult], identities: dict) -> None:
    import os
    from .report import _backfill, done_companies

    _backfill(results, identities)
    by_cand: dict[str, list[ApplicationResult]] = defaultdict(list)
    for r in results:
        by_cand[r.candidate_id].append(r)

    title_done = "Companies DONE (submitted or already applied) - full details"
    rows: list[list[str]] = [["Candidate", "Email Used", "Phone Used", "Location",
                              "Resume Sent", "Companies Done", "Jobs Attempted", "Submitted", "Outcomes"]]
    for cid, items in by_cand.items():
        ident = identities.get(cid)
        outcomes = ", ".join(f"{k}:{v}" for k, v in Counter(r.status for r in items).most_common())
        rows.append([
            ident.full_name if ident else items[0].candidate_name,
            ident.email if ident else items[0].email,
            ident.phone_display if ident else items[0].phone,
            ident.location if ident else items[0].candidate_location,
            os.path.basename(ident.resume) if ident else os.path.basename(items[0].resume_file),
            str(len(done_companies(items))),
            str(len(items)),
            str(sum(1 for r in items if r.submitted)),
            outcomes,
        ])

    rows.append([])
    rows.append([title_done])
    detail_header = ["Candidate", "Company", "Job Title", "Job Location", "Applied At (UTC)", "Status",
                     "Email", "Phone", "Candidate Location", "LinkedIn", "GitHub", "Resume",
                     "AI company", "Source", "Apply URL"]
    rows.append(detail_header)
    for cid, items in by_cand.items():
        for r in done_companies(items):
            rows.append([r.candidate_name, r.job.company, r.job.title, r.job.location, r.applied_at,
                         r.status, r.email, r.phone, r.candidate_location, r.linkedin, r.github,
                         os.path.basename(r.resume_file), "yes" if r.job.company_is_ai else "no",
                         r.job.source, r.job.apply_url])

    ncols = len(detail_header)
    col = chr(ord("A") + ncols - 1)
    rows = [row + [""] * (ncols - len(row)) for row in rows]
    ws = _reset_worksheet(sh, "Summary", rows=max(len(rows) + 10, 40), cols=ncols)
    ws.update(values=rows, range_name=f"A1:{col}{len(rows)}")

    header_rows = [0]
    for idx, row in enumerate(rows):
        if row and row[0] == title_done or (idx > 0 and row == detail_header):
            header_rows.append(idx)

    requests = []
    for idx in header_rows:
        requests.append({"repeatCell": {
            "range": {"sheetId": ws.id, "startRowIndex": idx, "endRowIndex": idx + 1},
            "cell": {"userEnteredFormat": _fmt(bg=HEADER_BG, fg=HEADER_FG, bold=True)},
            "fields": "userEnteredFormat(backgroundColor,textFormat)",
        }})
    requests.append({"autoResizeDimensions": {
        "dimensions": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": ncols}
    }})
    sh.batch_update({"requests": requests})


def _write_legend(sh) -> None:
    rows = [["Colour", "Status", "What it means"]]
    meanings = {
        "SUBMITTED": "Application sent AND confirmed by the company's own page",
        "CAPTCHA_BLOCKED": "Form was filled but a captcha needs a human to clear it",
        "FAILED_VALIDATION": "A required question had no resume fact behind it - not submitted",
        "LOGIN_REQUIRED": "The site demanded an account login",
        "ALREADY_APPLIED": "This candidate had already applied to this posting",
        "CLOSED": "The posting was closed by the time we opened it",
        "ERROR": "Technical failure - nothing was submitted",
        "SKIPPED": "Dry run - form filled on purpose but not submitted",
    }
    order = list(STATUS_COLOR.keys())
    for status in order:
        rows.append(["", status, meanings.get(status, "")])

    ws = _reset_worksheet(sh, "Legend", rows=len(rows) + 5, cols=3)
    ws.update(values=rows, range_name=f"A1:C{len(rows)}")

    requests = [{"repeatCell": {
        "range": {"sheetId": ws.id, "startRowIndex": 0, "endRowIndex": 1},
        "cell": {"userEnteredFormat": _fmt(bg=HEADER_BG, fg=HEADER_FG, bold=True)},
        "fields": "userEnteredFormat(backgroundColor,textFormat)",
    }}]
    for i, status in enumerate(order, start=1):
        requests.append({"repeatCell": {
            "range": {"sheetId": ws.id, "startRowIndex": i, "endRowIndex": i + 1,
                      "startColumnIndex": 0, "endColumnIndex": 1},
            "cell": {"userEnteredFormat": _fmt(bg=STATUS_COLOR[status])},
            "fields": "userEnteredFormat(backgroundColor)",
        }})
    requests.append({"autoResizeDimensions": {
        "dimensions": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 3}
    }})
    sh.batch_update({"requests": requests})


def try_push(results: list[ApplicationResult], identities: dict) -> tuple[bool, str]:
    """Never raises. Returns (ok, message) so the CLI can report honestly."""
    if not results:
        return False, "nothing to write - no attempts were made"
    try:
        url = push(results, identities)
        return True, url
    except SheetsUnavailable as exc:
        return False, str(exc)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
