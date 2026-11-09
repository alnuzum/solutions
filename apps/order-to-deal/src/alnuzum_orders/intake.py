"""A file from a stranger: what it really is, whether it is within bounds, and its lines.

The type is decided by the content, never by the name or the browser's Content-Type. The file lives
in memory for the length of the request and is never written anywhere.
"""

import csv
import io
import re
import sys
from dataclasses import dataclass, field
from typing import Any

from alnuzum_files import FileError, child, decode_text, sniff, unzip_bounds
from alnuzum_files import image as image_mod
from alnuzum_files import pdf as pdf_mod

MAX_BYTES = 8 * 1024 * 1024
MAX_PAGES = 10
MAX_PIXELS = 40_000_000
MAX_SIDE = 1568  # what the model reads without scaling it down itself
MAX_ROWS = 500
MAX_TEXT = 40_000
MAX_LINES = 400

# the file checks are shared with the other solutions (alnuzum_files); the names stay here for this solution
IntakeError = FileError


@dataclass
class Source:
    """``kind``: text, table, pdf (with a text layer) or image (the model reads it: a photo, a scan)."""

    kind: str
    name: str
    lines: list[str] = field(default_factory=list)
    # for the model: re-encoded JPEGs (a photo, or each page of a scan)
    images: list[bytes] = field(default_factory=list)
    media_type: str | None = None
    pages: int = 0
    # a table whose header is recognised: column positions of name, quantity, unit, article
    columns: dict[str, int] | None = None
    rows: list[list[str]] = field(default_factory=list)


def _clean(lines: list[str]) -> list[str]:
    out = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in lines]
    out = [ln[:300] for ln in out if ln]
    if len(out) > MAX_LINES:
        raise IntakeError("e_too_long")
    return out


_HEADER = {
    "name": ("наименование", "наим", "товар", "название", "позиция", "описание", "name", "item", "product",
             "description"),
    "qty": ("кол-во", "количество", "кол.", "колво", "qty", "quantity", "кол"),
    "unit": ("ед.", "ед", "ед. изм.", "ед.изм.", "единица", "unit", "uom", "ед изм"),
    "sku": ("артикул", "арт.", "арт", "код", "sku", "article", "code", "part no", "part number"),
}  # fmt: skip


def header_columns(row: list[str]) -> dict[str, int] | None:
    """Positions of the name, quantity, unit and article columns when a row reads as a header."""
    found: dict[str, int] = {}
    for i, cell in enumerate(row):
        key = re.sub(r"\s+", " ", cell.strip().casefold())
        for role, words in _HEADER.items():
            if role not in found and (key in words or any(key.startswith(w + " ") for w in words)):
                found[role] = i
    return found if {"name", "qty"} <= set(found) else None


def _table(name: str, rows: list[list[str]]) -> Source:
    rows = [[c.strip() for c in r] for r in rows if any(c.strip() for c in r)][:MAX_ROWS]
    if not rows:
        raise IntakeError("e_empty")
    columns = None
    body = rows
    for i, row in enumerate(rows[:10]):
        if cols := header_columns(row):
            columns, body = cols, rows[i + 1 :]
            break
    lines = _clean([" | ".join(c for c in r if c) for r in rows])
    return Source("table", name, lines, columns=columns, rows=body if columns else rows)


def _xlsx(name: str, data: bytes) -> Source:
    from openpyxl import load_workbook

    unzip_bounds(data)
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:
        raise IntakeError("e_damaged") from None
    try:
        sheet = wb.worksheets[0]
        rows: list[list[str]] = []
        for row in sheet.iter_rows(max_row=MAX_ROWS + 20, max_col=30, values_only=True):
            rows.append(["" if v is None else _cell(v) for v in row])
    finally:
        wb.close()
    return _table(name, rows)


def _cell(value: object) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _pdf(name: str, data: bytes) -> Source:
    reader, texts = pdf_mod.open_pdf(data, MAX_PAGES)
    pages = len(texts)
    text = "\n".join(texts)
    lines = _clean(text.splitlines())
    if sum(len(ln) for ln in lines) < 40:
        # a scan: the model gets its pages as pictures, never the PDF. What one reader of PDF sees as no
        # text another may read as pages of it (a font without ToUnicode, a hidden layer, annotations);
        # a picture's price is known before the call
        _pixels(reader)
        pictures = _rasterize(data)
        return Source("image", name, [], images=pictures, media_type="image/jpeg", pages=len(pictures))
    return Source("pdf", name, lines, pages=pages)


RENDER_SECONDS = child.SECONDS
RENDER_MEMORY = child.MEMORY
_RENDER = child.SLOT  # one child process at a time for the whole platform (alnuzum_files.child)
RENDER_COMMAND = [sys.executable, "-I", "-m", "alnuzum_orders.render"]


def _pixels(reader: Any) -> None:
    pdf_mod.pixels(reader)


def _rasterize(data: bytes) -> list[bytes]:
    """Each page of a scan as a JPEG of at most MAX_SIDE a side, drawn in a process of its own."""
    return pdf_mod.rasterize(data, command=RENDER_COMMAND, seconds=RENDER_SECONDS, max_pages=MAX_PAGES)


def _image(name: str, data: bytes) -> Source:
    picture = image_mod.normalize(data)
    return Source("image", name, [], images=[picture], media_type="image/jpeg", pages=1)


def read(name: str, data: bytes) -> Source:
    """``name`` is only shown back to the user; the content decides everything."""
    if not data:
        raise IntakeError("e_empty")
    if len(data) > MAX_BYTES:
        raise IntakeError("e_size")
    kind = sniff(data)
    if kind == "pdf":
        return _pdf(name, data)
    if kind in ("png", "jpeg", "webp"):
        return _image(name, data)
    if kind == "xlsx":
        return _xlsx(name, data)
    if kind == "text":
        return from_text(name, decode_text(data))
    raise IntakeError("e_type")


def from_text(name: str, text: str) -> Source:
    if len(text) > MAX_TEXT:
        raise IntakeError("e_too_long")
    if not text.strip():
        raise IntakeError("e_empty")
    sample = text[:4000]
    # a CSV export reads as a table when most lines split the same way
    for delim in (";", "\t", ","):
        rows = list(csv.reader(io.StringIO(text), delimiter=delim))
        widths = [len(r) for r in rows if any(c.strip() for c in r)]
        if len(widths) >= 2 and widths.count(max(set(widths), key=widths.count)) >= 0.8 * len(widths):
            common = max(set(widths), key=widths.count)
            if common >= 2 and delim in sample and header_columns(rows[0] if rows else []):
                return _table(name, rows)
    return Source("text", name, _clean(text.splitlines()))
