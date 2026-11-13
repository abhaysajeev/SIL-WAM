"""
Reading a contact list from an Excel (.xlsx) or CSV file, and writing the sample.

Shared by the agent app (POST /agent-api/v1/phonebooks/{id}/import) and the web panel
(POST /api/phonebooks/import/parse), so a file one accepts the other accepts too.

**The server reads the file, never the phone.** Parsing spreadsheets on the device would
mean the device team rebuilding this module — encodings, Excel's number handling, date
cells, formulas — in another language, and the two would drift.

Defensive reading, because the file comes from anywhere:
  * .xlsx is a zip of XML. The zip's declared uncompressed size and member count are
    checked BEFORE it is opened (a small file can expand to gigabytes), and defusedxml
    is installed, which openpyxl uses automatically against entity-expansion attacks.
  * Only the first sheet is read, values only (`data_only`: a formula gives its last
    calculated value, never the formula text).
  * CSV is decoded as UTF-8 (with or without BOM), falling back to Windows-1252 — what
    Excel on Windows writes by default — so names with accents do not become garbage.

Excel and phone numbers: a number typed into a General-format cell is stored exactly
(918075571893) and only DISPLAYED rounded, so numeric cells are read back as the exact
integer. A CSV saved from such a sheet, however, may contain the rounded text
"9.18076E+11" — the digits are gone; app/core/phone.py rejects that row with a message
telling the person to format the column as Text.
"""
import csv
import io
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, time

from app.core.column_mapper import CONTACT_FIELDS, suggest_mapping

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_UNZIPPED_BYTES = 50 * 1024 * 1024
MAX_ZIP_MEMBERS = 500
MAX_ROWS = 20_000            # hard ceiling on data rows read, whatever the settings say
FORMATS = ("xlsx", "csv")

# What a contact file may carry. agent_id is not offered: an agent's own contacts are
# always theirs, and the web panel's Agent ID column is a label only.
IMPORT_FIELDS = [f for f in CONTACT_FIELDS if f.name != "agent_id"]

SAMPLE_HEADERS = ["WhatsApp Number", "Customer Name", "Email"]


class ImportError_(Exception):
    """The file as a whole cannot be used. `code` is the agent API error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class ParsedFile:
    headers: list[str]
    # (spreadsheet row number, cell values aligned to headers)
    rows: list[tuple[int, list[str]]] = field(default_factory=list)
    truncated: bool = False


@dataclass
class ContactRow:
    row: int
    mobile_no: str
    customer_name: str
    email: str | None


# ── Reading ───────────────────────────────────────────────────────────────────

def detect_format(data: bytes, filename: str | None) -> str:
    if data[:4] == b"PK\x03\x04":
        return "xlsx"
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        raise ImportError_("unsupported_file_type",
                           "This is an old Excel file (.xls). Open it in Excel and "
                           "save it as .xlsx or .csv, then try again.")
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        raise ImportError_("file_unreadable", "This Excel file is damaged or incomplete.")
    if b"\x00" in data[:4096]:
        raise ImportError_("unsupported_file_type",
                           "Send an Excel (.xlsx) or CSV file.")
    return "csv"


def _cell_text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else repr(v)
    if isinstance(v, datetime):
        return v.strftime("%d-%m-%Y") if v.time() == time(0) else v.strftime("%d-%m-%Y %H:%M")
    if isinstance(v, date):
        return v.strftime("%d-%m-%Y")
    return str(v).strip()


def _check_zip(data: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            infos = zf.infolist()
    except zipfile.BadZipFile:
        raise ImportError_("file_unreadable", "This Excel file is damaged or incomplete.")
    if len(infos) > MAX_ZIP_MEMBERS or sum(i.file_size for i in infos) > MAX_UNZIPPED_BYTES:
        raise ImportError_("file_too_large", "This Excel file is too large to import.")


def _read_xlsx(data: bytes) -> list[list[str]]:
    import openpyxl

    _check_zip(data)
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:
        raise ImportError_("file_unreadable", "This Excel file could not be read.")
    try:
        ws = wb.worksheets[0]
        out = []
        for values in ws.iter_rows(values_only=True):
            out.append([_cell_text(v) for v in values])
            if len(out) > MAX_ROWS + 1:
                break
        return out
    finally:
        wb.close()


def _read_csv(data: bytes) -> list[list[str]]:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:                                   # pragma: no cover — cp1252 decodes nearly anything
        raise ImportError_("file_unreadable", "The file's text encoding is not supported.")
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    out = []
    for values in csv.reader(io.StringIO(text), dialect):
        out.append([v.strip() for v in values])
        if len(out) > MAX_ROWS + 1:
            break
    return out


def parse(data: bytes, filename: str | None = None) -> ParsedFile:
    """Headers and data rows of the first sheet. Raises ImportError_."""
    if not data:
        raise ImportError_("empty_file", "The file is empty.")
    if len(data) > MAX_FILE_BYTES:
        raise ImportError_("file_too_large",
                           f"The file is larger than {MAX_FILE_BYTES // 1048576} MB.")
    fmt = detect_format(data, filename)
    grid = _read_xlsx(data) if fmt == "xlsx" else _read_csv(data)

    # The header is the first row with anything in it; rows above are ignored.
    start = next((i for i, r in enumerate(grid) if any(c for c in r)), None)
    if start is None:
        raise ImportError_("empty_file", "The file has no rows.")
    headers = [h.strip() for h in grid[start]]
    parsed = ParsedFile(headers=headers)
    for offset, values in enumerate(grid[start + 1:], start=start + 2):
        if not any(v for v in values):
            continue                        # blank line
        if len(parsed.rows) >= MAX_ROWS:
            parsed.truncated = True
            break
        values = (values + [""] * len(headers))[:len(headers)]
        parsed.rows.append((offset, values))
    return parsed


def read_contacts(data: bytes, filename: str | None = None) -> tuple[list[ContactRow], dict]:
    """
    Contacts from a file, columns matched by header name (the sample's headers always
    match). Returns (rows, mapping). Raises ImportError_ if there is no number column.
    Values are raw — the caller normalises and validates each row.
    """
    parsed = parse(data, filename)
    mapping = suggest_mapping(parsed.headers, IMPORT_FIELDS)
    if not mapping.get("mobile_no"):
        raise ImportError_("missing_number_column",
                           "No WhatsApp number column was found. Use the sample file's "
                           "headings: WhatsApp Number, Customer Name, Email.")
    idx = {name: parsed.headers.index(h) for name, h in mapping.items() if h}
    rows = []
    for n, values in parsed.rows:
        get = lambda f: values[idx[f]] if f in idx else ""    # noqa: E731
        rows.append(ContactRow(row=n, mobile_no=get("mobile_no"),
                               customer_name=get("customer_name"),
                               email=get("email") or None))
    return rows, mapping


# ── The sample ────────────────────────────────────────────────────────────────

_INSTRUCTIONS = [
    ["How to fill in this file"],
    [""],
    ["WhatsApp Number", "Required. Include the country code if the number is not in your "
                        "company's country, e.g. 971501234567. Spaces, dashes and a "
                        "leading + are fine."],
    ["Customer Name", "Required."],
    ["Email", "Optional."],
    [""],
    ["One contact per row. Keep the headings in row 1 exactly as they are."],
    ["The WhatsApp Number column is formatted as Text — keep it that way, or Excel "
     "rounds long numbers and the digits are lost."],
    ["A number that appears twice is imported once."],
]


def sample(fmt: str) -> tuple[bytes, str, str]:
    """The agent app's phonebook sample: (content, mime, filename)."""
    return sample_for(SAMPLE_HEADERS, fmt, "phonebook_sample", _INSTRUCTIONS)


def _is_number_header(h: str) -> bool:
    from app.core.column_mapper import _MOBILE_ALIASES, _norm
    return _norm(h) in ({_norm(a) for a in _MOBILE_ALIASES} | {"mobileno"})


def _safe_header(h) -> str:
    """A heading written into a sample. A leading = + - @ would make a spreadsheet
    treat the cell as a formula (formula injection), so those are stripped."""
    h = str(h or "").strip()[:80]
    while h and h[0] in "=+-@\t\r":
        h = h[1:]
    return h


def sample_for(headers: list[str], fmt: str, basename: str,
               instructions: list[list[str]] | None = None) -> tuple[bytes, str, str]:
    """
    A headings-only file (an example row tends to get imported as a real contact).
    In the .xlsx, every WhatsApp-number column is formatted as Text.
    """
    headers = [h for h in (_safe_header(x) for x in headers) if h]
    if fmt == "csv":
        line = ",".join('"' + h.replace('"', '""') + '"' if ("," in h or '"' in h) else h
                        for h in headers)
        body = ("﻿" + line + "\r\n").encode("utf-8")
        return body, "text/csv; charset=utf-8", f"{basename}.csv"
    if fmt != "xlsx":
        raise ValueError(fmt)

    import openpyxl
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Contacts"
    ws.append(headers)
    for i, h in enumerate(headers, start=1):
        col = get_column_letter(i)
        ws[f"{col}1"].font = Font(bold=True)
        ws.column_dimensions[col].width = max(16, min(40, len(h) + 6))
        if _is_number_header(h):
            # Text as the column's default style, so a number typed anywhere in it
            # keeps every digit. Set on the column, not on thousands of cells, which
            # would make an empty sheet look thousands of rows long.
            ws.column_dimensions[col].number_format = "@"
            ws[f"{col}1"].number_format = "@"
    ws.freeze_panes = "A2"

    if instructions:
        info = wb.create_sheet("Instructions")
        for line in instructions:
            info.append(line)
        info["A1"].font = Font(bold=True)
        info.column_dimensions["A"].width = 20
        info.column_dimensions["B"].width = 90

    buf = io.BytesIO()
    wb.save(buf)
    return (buf.getvalue(),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            f"{basename}.xlsx")
