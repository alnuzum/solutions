"""A PDF: its text layer (pypdf) or, for a scan, its pages as pictures drawn in a child process after the
declared size of every picture is measured."""

import io
import sys
from typing import Any

from alnuzum_files import FileError, child

MAX_PIXELS = 40_000_000  # one picture, and the pictures one page draws together
MAX_SCAN_PIXELS = 4 * MAX_PIXELS  # the pictures of a whole scan
RENDER_COMMAND = [sys.executable, "-I", "-m", "alnuzum_files.render"]


def open_pdf(data: bytes, max_pages: int) -> tuple[Any, list[str]]:
    """The reader and the text of each page; ``e_pages`` over ``max_pages``, ``e_damaged`` for an encrypted or
    broken file."""
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise FileError("e_damaged")
        if len(reader.pages) > max_pages:
            raise FileError("e_pages")
        texts = [(page.extract_text() or "") for page in reader.pages]
    except (PdfReadError, ValueError, KeyError, TypeError, RecursionError) as exc:
        if isinstance(exc, FileError):
            raise
        raise FileError("e_damaged") from None
    return reader, texts


def pixels(reader: Any) -> None:
    """The declared size of every picture of the pages, read from the dictionaries without decoding anything:
    PDFium decodes a picture whole whatever the size it draws it at."""
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
                        raise FileError("e_scan_size")
                    found += w * h
            elif obj.get("/Subtype") == "/Form":
                found += images(obj.get("/Resources"), depth + 1)
        return found

    try:
        for page in reader.pages:
            on_page = images(page.get("/Resources"), 0)
            total += on_page
            if on_page > MAX_PIXELS or total > MAX_SCAN_PIXELS:
                raise FileError("e_scan_size")
    except FileError:
        raise
    except Exception:  # a dictionary pypdf cannot walk is a damaged file
        raise FileError("e_damaged") from None


def rasterize(
    data: bytes, *, command: list[str] | None = None, seconds: float = child.SECONDS, max_pages: int = 10
) -> list[bytes]:
    """Each page of a scan as a JPEG of at most 1568 px a side (``alnuzum_files.render``)."""
    raw = child.run(
        [*(command or RENDER_COMMAND), str(child.MEMORY)], data, seconds=seconds, codes={3: "e_pages"}
    )
    out = child.frames(raw)
    if not 1 <= len(out) <= max_pages or not all(page.startswith(b"\xff\xd8\xff") for page in out):
        raise FileError("e_damaged")
    return out
