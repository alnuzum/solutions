"""A file from a stranger: what it really is, whether it is within bounds, and its lines.

The type is decided by the content, never by the name or the browser's Content-Type. The file lives
in memory for the length of the request and is never written anywhere.
"""

import csv
import io
import re
import subprocess  # nosec B404 - only to run our own page-drawing module
import sys
import threading
import zipfile
from dataclasses import dataclass, field
from typing import Any

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
        try:
            _pixels(reader)
        except IntakeError:
            raise
        except Exception:  # a dictionary pypdf cannot walk is a damaged file
            raise IntakeError("e_damaged") from None
        pictures = _rasterize(data)
        return Source("image", name, [], images=pictures, media_type="image/jpeg", pages=len(pictures))
    return Source("pdf", name, lines, pages=pages)


MAX_PAGE_PIXELS = MAX_PIXELS  # the pictures one page of a scan draws, together
MAX_SCAN_PIXELS = 4 * MAX_PIXELS  # the pictures of a whole scan
RENDER_SECONDS = 15.0  # below the parse's own 20 s: the child is killed before the slot gives up
# the memory ceiling of the drawing process (RLIMIT_AS): measured 66-98 MB for scans from one 300 dpi page
# to ten pages and a 40 Mp picture; 256 MB leaves a raw 40 Mp picture room, and with the platform's
# ~130 MB the container's 512 MB
RENDER_MEMORY = 256 * 1024 * 1024
_RENDER = threading.BoundedSemaphore(1)  # one drawing process at a time
RENDER_COMMAND = [sys.executable, "-I", "-m", "alnuzum_orders.render"]


def _pixels(reader: Any) -> None:
    """The declared size of every picture of the pages, read from the dictionaries without decoding
    anything: PDFium decodes a picture whole whatever the size it draws it at."""
    seen: set[int] = set()
    total = 0

    def images(resources: Any, depth: int) -> int:
        if resources is None or depth > 4:
            return 0
        found = 0
        xobjects = resources.get_object().get("/XObject")
        if xobjects is None:
            return 0
        xobjects = xobjects.get_object()
        for key in list(xobjects.keys())[:500]:
            obj = xobjects[key].get_object()
            if id(obj) in seen:
                continue
            seen.add(id(obj))
            if obj.get("/Subtype") == "/Image":
                for picture in (obj, obj.get("/SMask")):
                    if picture is None:
                        continue
                    picture = picture.get_object()
                    w, h = int(picture.get("/Width", 0)), int(picture.get("/Height", 0))
                    if w <= 0 or h <= 0 or w * h > MAX_PIXELS:
                        raise IntakeError("e_scan_size")
                    found += w * h
            elif obj.get("/Subtype") == "/Form":
                found += images(obj.get("/Resources"), depth + 1)
        return found

    for page in reader.pages:
        on_page = images(page.get("/Resources"), 0)
        total += on_page
        if on_page > MAX_PAGE_PIXELS or total > MAX_SCAN_PIXELS:
            raise IntakeError("e_scan_size")


def _rasterize(data: bytes) -> list[bytes]:
    """Each page of a scan as a JPEG of at most MAX_SIDE a side, drawn by ``alnuzum_orders.render`` in a
    process of its own: its own PDFium, a memory ceiling, killed after RENDER_SECONDS."""
    if not _RENDER.acquire(timeout=RENDER_SECONDS):
        raise IntakeError("e_busy")
    try:
        done = subprocess.run(  # noqa: S603  # nosec B603 - our own module, fixed arguments, no shell
            [*RENDER_COMMAND, str(RENDER_MEMORY)],
            input=data,
            capture_output=True,
            timeout=RENDER_SECONDS,
            env={"MALLOC_ARENA_MAX": "2"},
            check=False,
        )
    except subprocess.TimeoutExpired:  # run() has killed it
        raise IntakeError("e_slow") from None
    finally:
        _RENDER.release()
    if done.returncode == 3:
        raise IntakeError("e_pages")
    if done.returncode != 0:
        raise IntakeError("e_damaged")
    out, raw = [], done.stdout
    while raw:
        size = int.from_bytes(raw[:4], "big")
        out.append(raw[4 : 4 + size])
        raw = raw[4 + size :]
    if not 1 <= len(out) <= MAX_PAGES or not all(page.startswith(b"\xff\xd8\xff") for page in out):
        raise IntakeError("e_damaged")
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
