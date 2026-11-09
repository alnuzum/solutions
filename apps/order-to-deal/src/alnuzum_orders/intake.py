"""A file from a stranger: what it really is, whether it is within bounds, and its lines.

The type is decided by the content, never by the name or the browser's Content-Type. The file lives
in memory for the length of the request and is never written anywhere.
"""

import csv
import io
import re
import threading
import zipfile
from dataclasses import dataclass, field

MAX_BYTES = 8 * 1024 * 1024
MAX_PAGES = 10
MAX_PIXELS = 40_000_000
MAX_SIDE = 1568  # what the model reads without scaling it down itself
MAX_ROWS = 500
MAX_TEXT = 40_000
MAX_LINES = 400
# a spreadsheet is a zip: what it unpacks to is bounded before anything parses it (a few MB of deflate
# can hold gigabytes of XML, and the reader keeps the shared strings whole)
MAX_UNZIPPED = 50 * 1024 * 1024
MAX_PART = 20 * 1024 * 1024
MAX_RATIO = 200  # unpacked to packed, for a part over 1 MB
MAX_ZIP_PARTS = 1000


class IntakeError(ValueError):
    """A file that is refused; ``code`` names the message for the user."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


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


def sniff(data: bytes) -> str:
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = set(z.namelist())
        except zipfile.BadZipFile:
            return "unknown"
        return "xlsx" if "xl/workbook.xml" in names else "unknown"
    if b"\x00" in data[:4096]:
        return "unknown"
    return "text"


def decode_text(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise IntakeError("e_type")


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


def unzip_bounds(data: bytes) -> None:
    """The declared sizes of the parts: the zip reader never unpacks more than a part declares (a part
    that holds more fails its CRC), so bounding them bounds the memory of the parse."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            parts = z.infolist()
    except (zipfile.BadZipFile, ValueError):
        raise IntakeError("e_damaged") from None
    if len(parts) > MAX_ZIP_PARTS:
        raise IntakeError("e_unpacked")
    total = 0
    for part in parts:
        total += part.file_size
        if part.file_size > MAX_PART or total > MAX_UNZIPPED:
            raise IntakeError("e_unpacked")
        if part.file_size > 1024 * 1024 and part.file_size > MAX_RATIO * max(part.compress_size, 1):
            raise IntakeError("e_unpacked")


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
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise IntakeError("e_damaged")
        pages = len(reader.pages)
        if pages > MAX_PAGES:
            raise IntakeError("e_pages")
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
    except (PdfReadError, ValueError, KeyError, TypeError, RecursionError) as exc:
        if isinstance(exc, IntakeError):
            raise
        raise IntakeError("e_damaged") from None
    lines = _clean(text.splitlines())
    if sum(len(ln) for ln in lines) < 40:
        # a scan: the model gets its pages as pictures, never the PDF. What one reader of PDF sees as no
        # text another may read as pages of it (a font without ToUnicode, a hidden layer, annotations);
        # a picture's price is known before the call
        pictures = _rasterize(data)
        return Source("image", name, [], images=pictures, media_type="image/jpeg", pages=len(pictures))
    return Source("pdf", name, lines, pages=pages)


_PDFIUM = threading.Lock()  # PDFium is not thread-safe: one page is drawn at a time in the process


def _rasterize(data: bytes) -> list[bytes]:
    """Each page of a scan as a JPEG of at most MAX_SIDE a side, as a photo is."""
    import pypdfium2 as pdfium
    from PIL import Image

    out: list[bytes] = []
    with _PDFIUM:
        try:
            doc = pdfium.PdfDocument(data)
        except (pdfium.PdfiumError, ValueError, OSError):
            raise IntakeError("e_damaged") from None
        try:
            if not 1 <= len(doc) <= MAX_PAGES:
                raise IntakeError("e_pages" if len(doc) > MAX_PAGES else "e_damaged")
            for n in range(len(doc)):
                page = doc[n]
                try:
                    w, h = page.get_size()
                    if min(w, h) <= 0:
                        raise IntakeError("e_damaged")
                    picture = page.render(scale=MAX_SIDE / max(w, h)).to_pil().convert("RGB")
                    picture.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
                    buf = io.BytesIO()
                    picture.save(buf, "JPEG", quality=85)
                    out.append(buf.getvalue())
                finally:
                    page.close()
        except (pdfium.PdfiumError, ValueError, OSError):
            raise IntakeError("e_damaged") from None
        finally:
            doc.close()
    return out


def _image(name: str, data: bytes) -> Source:
    from PIL import Image, ImageOps, UnidentifiedImageError

    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        with Image.open(io.BytesIO(data)) as img:
            if img.width * img.height > MAX_PIXELS:
                raise IntakeError("e_image_size")
            # shrink first, in place: a 40 Mp picture is never copied whole (the container has 512 MB);
            # a JPEG is even decoded at a reduced scale
            img.draft("RGB", (MAX_SIDE, MAX_SIDE))
            img.thumbnail((MAX_SIDE, MAX_SIDE))
            turned = ImageOps.exif_transpose(img).convert("RGB")
            out = io.BytesIO()
            turned.save(out, "JPEG", quality=88)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, ValueError) as exc:
        if isinstance(exc, IntakeError):
            raise
        raise IntakeError("e_damaged") from None
    return Source("image", name, [], images=[out.getvalue()], media_type="image/jpeg", pages=1)


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
