"""Excel export with openpyxl: one .xlsx per category plus 00_Master_Summary.
Files are written atomically (tmp + replace) so a download during a checkpoint
never sees a half-written file, and only when their content changed: openpyxl
needs ~0.5 s of CPU for a 2,000-row sheet (~5 s of wall time on a 0.1-CPU
host), and every rewrite makes each open browser download the file again."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .. import fastjson
from .categories import SUMMARY_FILE
from .normalize import xlsx_safe

log = logging.getLogger("exporter")

COLUMNS = [
    "Company Name", "Category", "Subcategory", "Official Website",
    "Business Email", "Email Status", "Email Score", "Email Check", "Email Source",
    "Business Phone", "Country", "State", "City",
    "Full Business Address", "Industry", "Services", "Source URL",
    "Source Page", "Search Query", "Collected At", "Confidence Score", "Record Status",
]

HEADER_FILL = PatternFill("solid", fgColor="1F4E5F")
HEADER_FONT = Font(color="FFFFFF", bold=True)


def _style_sheet(ws, n_cols: int):
    for c in range(1, n_cols + 1):
        cell = ws.cell(row=1, column=c)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center")
        width = 12
        for row in ws.iter_rows(min_col=c, max_col=c, max_row=min(ws.max_row, 300)):
            v = row[0].value
            if v is not None:
                width = max(width, min(55, len(str(v)) + 2))
        ws.column_dimensions[get_column_letter(c)].width = width
    ws.freeze_panes = "A2"


_file_locks: dict[str, threading.Lock] = {}
_file_locks_guard = threading.Lock()


def _lock_for(path: str) -> threading.Lock:
    with _file_locks_guard:
        return _file_locks.setdefault(os.path.normcase(os.path.abspath(path)),
                                      threading.Lock())


def _save_locked(wb: Workbook, path: str) -> bool:
    """Atomic replace; True once `path` holds the new workbook. Several jobs
    may export at once (the master summary is written by every one of
    them): the caller holds the file's lock, so writers of the same file
    are serialized, and every temp file is unique, so they never interleave."""
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp.xlsx"
    wb.save(tmp)
    for attempt in range(3):
        try:
            os.replace(tmp, path)
            return True
        except PermissionError:
            # Windows: open in Excel (retrying won't help) or held for a
            # moment by an antivirus scan / a download (it will)
            if attempt == 2:
                # keep ONE up-to-date copy next to it (not one per export)
                fallback = path + ".tmp.xlsx"
                try:
                    os.replace(tmp, fallback)
                except OSError:
                    fallback = tmp
                log.warning("%s is open in another program; left %s", path, fallback)
                return False
            time.sleep(0.3 * (attempt + 1))
    return False


# path -> (content digest, mtime_ns, size) of the file this process last
# wrote there. A file changed or removed by anything else is rewritten.
_written: dict[str, tuple[bytes, int, int]] = {}


def _digest(*parts) -> bytes:
    return hashlib.blake2b(fastjson.dumps(parts), digest_size=16).digest()


def _write_if_changed(path: str, digest: bytes, build, force: bool = False) -> bool:
    """Save build()'s workbook to `path` unless the file there was written
    from identical content. Returns True if the file was (re)written."""
    with _lock_for(path):
        prev = _written.get(path)
        if not force and prev is not None and prev[0] == digest:
            try:
                st = os.stat(path)
                if (st.st_mtime_ns, st.st_size) == prev[1:]:
                    return False
            except OSError:
                pass
        _written.pop(path, None)
        if _save_locked(build(), path):
            try:
                st = os.stat(path)
                _written[path] = (digest, st.st_mtime_ns, st.st_size)
            except OSError:
                pass
        return True


_COL = {name: i for i, name in enumerate(COLUMNS)}
_LINK_FONT = Font(color="0563C1", underline="single")
_EMAIL_RE = re.compile(r"[^@\s\"<>]+@[^@\s\"<>]+\.[A-Za-z]{2,24}")


def _typed(row: list[str]) -> list:
    """Cells Excel can sort and filter: the score as a number, the time as a
    real date, missing values as truly empty cells (an "" cell would count
    in COUNTA). Every other cell stays a plain string."""
    out = [v if v != "" else None for v in row]
    for col in ("Confidence Score", "Email Score"):
        score = out[_COL[col]]
        if score and score.isdigit():
            out[_COL[col]] = int(score)
    when = out[_COL["Collected At"]]
    if when:
        try:
            out[_COL["Collected At"]] = datetime.fromisoformat(when)
        except ValueError:
            pass
    return out


def _link(cell, target: str):
    cell.hyperlink = target
    cell.font = _LINK_FONT


# -- location layout -----------------------------------------------------------
# One sheet per searched state (or country, when no state was searched); in
# it, one heading row per searched location, in the order the locations were
# first searched - so a city search lands below the state's earlier results.
# Records keep their stored order inside a section; nothing is re-sorted.
SECTION_PREFIX = "Search location: "
EARLIER_SECTION = "Earlier results (collected before location headings)"
SECTION_FILL = PatternFill("solid", fgColor="D9EAF0")
SECTION_FONT = Font(color="1F4E5F", bold=True, size=12)


def _legacy_state(query: str) -> str:
    """State named by an older record's search query ("... in Houston, Texas",
    "... in Texas, USA"), or "". A bare place ("New York") is ambiguous -
    city or state - so it is not guessed."""
    from .geo import find_state
    loc = query.rpartition(" in ")[2] if " in " in query else ""
    parts = [p.strip() for p in loc.split(",") if p.strip()]
    if len(parts) < 2:
        return ""
    first = find_state(parts[0], parts[-1])[1]
    if first and len(parts) == 2:          # "Texas, USA"
        return first
    for p in parts[1:]:                     # "Houston, Texas[, USA]"
        name = find_state(p, parts[-1])[1]
        if name:
            return name
    return ""


LEGACY_MAX_STATES = 3          # older records naming more states = a country search
LEGACY_MAX_UNNAMED = 0.10      # ... or this share naming none


def _legacy_states(records: list[dict]) -> dict[int, str]:
    """Sheet state of each older (untagged) record, by index - only when
    those records look like state searches. Measured on real data: a state
    search names its state in 95-100% of its queries; a country-wide search
    spreads over 10-50 states and many bare city names, and is then kept
    together in one sheet, like a new country-wide search."""
    found = {i: _legacy_state(r.get("Search Query") or "")
             for i, r in enumerate(records) if not (r.get("Search Location") or "").strip()}
    if not found:
        return {}
    named = {s for s in found.values() if s}
    unnamed = sum(1 for s in found.values() if not s) / len(found)
    if len(named) > LEGACY_MAX_STATES or unnamed > LEGACY_MAX_UNNAMED:
        return {}
    return {i: s for i, s in found.items() if s}


def _placement(r: dict, legacy_state: str, default_sheet: str) -> tuple[str, str]:
    """(sheet, section heading) of one record."""
    loc = (r.get("Search Location") or "").strip()
    if loc:
        sheet = (r.get("Search State") or r.get("Search Country") or default_sheet).strip()
        return sheet or default_sheet, loc
    return legacy_state or default_sheet, EARLIER_SECTION


def _sheet_title(name: str, used: set[str]) -> str:
    # truncate first, then strip: Excel rejects a title that starts or ends
    # with an apostrophe, and cutting at 31 can expose one at the end
    base = re.sub(r"[\[\]:*?/\\]", "-", name).strip("' ")[:31].strip("' ") or "Results"
    title, n = base, 2
    while title.lower() in used:
        suffix = f" ({n})"
        title, n = base[:31 - len(suffix)].rstrip("' ") + suffix, n + 1
    used.add(title.lower())
    return title


def _cell(r: dict, col: str):
    """The record's value as the workbook shows it. Email Status reads
    Exist only for a mailbox its mail server confirmed (Valid); Invalid,
    Risky and Unknown all read Not exist. No email = blank."""
    v = r.get(col, "")
    if col == "Email Status" and v:
        v = "Exist" if v == "Valid" else "Not exist"
    return xlsx_safe(v)


def _layout(records: list[dict], display: str) -> list[tuple[str, list[tuple[str, list[list[str]]]]]]:
    """[(sheet title, [(section heading, rows)])], all in first-seen order."""
    sheets: dict[str, dict[str, tuple[str, list]]] = {}
    names: dict[str, str] = {}
    legacy = _legacy_states(records)
    for i, r in enumerate(records):
        sheet, section = _placement(r, legacy.get(i, ""), display)
        skey = sheet.lower()
        names.setdefault(skey, sheet)
        secs = sheets.setdefault(skey, {})
        sec = secs.setdefault(section.lower(), (section, []))
        sec[1].append([_cell(r, col) for col in COLUMNS])
    used: set[str] = set()
    out = [(_sheet_title(names[k], used), list(secs.values())) for k, secs in sheets.items()]
    return out or [(_sheet_title(display, used), [])]


def _fill_sheet(ws, sections: list[tuple[str, list[list[str]]]]):
    ws.append(COLUMNS)
    url_cols = [_COL[c] + 1 for c in ("Official Website", "Email Source", "Source URL",
                                      "Source Page")]
    email_col = _COL["Business Email"] + 1
    date_col = _COL["Collected At"] + 1
    ncol = len(COLUMNS)
    for i, (heading, rows) in enumerate(sections):
        if i:
            ws.append([])                       # spacer between searches
        ws.append([f"{SECTION_PREFIX}{heading}   ({len(rows)} record"
                   f"{'' if len(rows) == 1 else 's'})"])
        hr = ws.max_row
        ws.merge_cells(start_row=hr, start_column=1, end_row=hr, end_column=ncol)
        cell = ws.cell(row=hr, column=1)
        cell.fill, cell.font = SECTION_FILL, SECTION_FONT
        cell.alignment = Alignment(vertical="center")
        ws.row_dimensions[hr].height = 20
        for row in rows:
            ws.append(_typed(row))
            r = ws.max_row
            for c in url_cols:
                cell = ws.cell(row=r, column=c)
                if isinstance(cell.value, str) and cell.value.startswith(("http://", "https://")):
                    _link(cell, cell.value)
            cell = ws.cell(row=r, column=email_col)
            if isinstance(cell.value, str) and _EMAIL_RE.fullmatch(cell.value):
                _link(cell, "mailto:" + cell.value)
            ws.cell(row=r, column=date_col).number_format = "yyyy-mm-dd hh:mm"
    _style_sheet(ws, ncol)
    ws.column_dimensions["A"].width = max(ws.column_dimensions["A"].width or 12, 30)
    ws.column_dimensions[get_column_letter(date_col)].width = 18
    ws.auto_filter.ref = ws.dimensions       # filter arrows on the header


def write_category_file(display: str, fname: str, records: list[dict],
                        out_dir: str, force: bool = False) -> str:
    """One category's workbook: a sheet per searched state, a heading per
    searched location. Skipped when its content is unchanged since this
    process last wrote the file (unless force)."""
    os.makedirs(out_dir, exist_ok=True)
    layout = _layout(records, display)

    def build() -> Workbook:
        wb = Workbook()
        for i, (title, sections) in enumerate(layout):
            ws = wb.active if i == 0 else wb.create_sheet()
            ws.title = title
            _fill_sheet(ws, sections)
        return wb
    _write_if_changed(os.path.join(out_dir, fname), _digest(layout), build, force)
    return fname


def is_data_row(row) -> bool:
    """A record row (not the column header, a location heading or a spacer)."""
    first = row[0] if row else None
    if isinstance(first, str) and first.startswith(SECTION_PREFIX):
        return False
    if first == COLUMNS[0] and len(row) > 1 and row[1] == COLUMNS[1]:
        return False
    return any(v not in (None, "") for v in row)


def read_data_rows(path: str) -> list[tuple]:
    """Every record row of a category workbook, all sheets, in order."""
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True)
    try:
        return [row for ws in wb.worksheets
                for row in ws.iter_rows(min_row=2, values_only=True) if is_data_row(row)]
    finally:
        wb.close()


SUMMARY_COLS = [
    "Category", "Records Found", "Unique Records", "Records With Website",
    "Records With Email", "Records With Phone", "Records With Address",
    "Duplicates Removed", "Failed URLs",
]


def write_master_summary(categories: dict[str, dict],
                         records_by_cat: dict[str, list[dict]],
                         stats_by_cat: dict[str, dict], out_dir: str) -> str:
    """categories: ordered slug -> {"display", "file"} (fixed + custom)."""
    os.makedirs(out_dir, exist_ok=True)
    table = []
    totals = {c: 0 for c in SUMMARY_COLS[1:]}
    for cat, cdef in categories.items():
        rows = records_by_cat.get(cat, [])
        st = stats_by_cat.get(cat, {})
        vals = {
            "Records Found": st.get("discovered", len(rows)),
            "Unique Records": len(rows),
            "Records With Website": sum(1 for r in rows if r.get("Official Website")),
            "Records With Email": sum(1 for r in rows if r.get("Business Email")),
            "Records With Phone": sum(1 for r in rows if r.get("Business Phone")),
            "Records With Address": sum(1 for r in rows if r.get("Full Business Address")),
            "Duplicates Removed": st.get("duplicates", 0),
            "Failed URLs": st.get("failed_urls", 0),
        }
        table.append([cdef["display"]] + [vals[c] for c in SUMMARY_COLS[1:]])
        for k, v in vals.items():
            totals[k] += v
    table.append(["TOTAL"] + [totals[c] for c in SUMMARY_COLS[1:]])

    def build() -> Workbook:
        wb = Workbook()
        ws = wb.active
        ws.title = "Summary"
        ws.append(SUMMARY_COLS)
        for row in table:
            ws.append(row)
        for cell in ws[ws.max_row]:
            cell.font = Font(bold=True)
        _style_sheet(ws, len(SUMMARY_COLS))
        return wb
    _write_if_changed(os.path.join(out_dir, SUMMARY_FILE), _digest(table), build)
    return SUMMARY_FILE
