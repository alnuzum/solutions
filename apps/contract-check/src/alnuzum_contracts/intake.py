"""A contract from a stranger: a Word document (its paragraphs and numbering), a PDF with a text layer, a scan
(a PDF without one, or a picture: the model reads it) or plain text. The type comes from the content.

Bounds: 8 MB, 40 pages (a Word document or a text: 120 000 characters), a scan 10 pages.
"""

import re
from dataclasses import dataclass, field

from alnuzum_files import FileError, decode_text, docx, sniff
from alnuzum_files import image as image_mod
from alnuzum_files import pdf as pdf_mod

MAX_BYTES = 8 * 1024 * 1024
MAX_PAGES = 40
MAX_SCAN_PAGES = 10
MAX_CHARS = 120_000
MIN_TEXT = 200  # a PDF with less text than this on all its pages is a scan
# lines a PDF repeats on every page: its number, «Страница 2 из 5», «Page 2 of 5»
_PAGE_LINE = re.compile(
    r"^\s*(?:(?:стр\.?|страница|page|p\.)\s*)?\d{1,3}\s*(?:(?:из|of|/)\s*\d{1,3})?\s*$", re.I
)


@dataclass(frozen=True)
class Block:
    """A paragraph (``hard``: a Word paragraph or a paragraph read off a scan) or a line of a PDF or a text,
    which may be the continuation of the one before."""

    label: str
    key: str
    text: str
    hard: bool


@dataclass
class Document:
    kind: str  # docx | pdf | text | scan
    blocks: list[Block] = field(default_factory=list)
    images: list[bytes] = field(default_factory=list)  # a scan's pages, for the model
    pages: int = 0


def _wrapped(lines: list[str]) -> bool:
    """A text whose lines are cut at a width (an export of a PDF, a letter) rather than one per paragraph."""
    sizes = sorted(len(ln) for ln in lines if ln.strip())
    if len(sizes) < 5 or sizes[-1] > 110:
        return False
    top = sizes[len(sizes) * 9 // 10]
    return sum(1 for n in sizes if n >= 0.8 * top) >= 0.5 * len(sizes)


def _lines(text: str, *, paragraphs: bool = False) -> list[Block]:
    out = []
    size = 0
    for raw in text.splitlines():
        line = " ".join(raw.replace("\x00", " ").split())
        if _PAGE_LINE.match(line) and line:
            continue
        size += len(line) + 1
        if size > MAX_CHARS:
            raise FileError("e_too_long")
        out.append(Block("", "", line, paragraphs and bool(line)))
    while out and not out[-1].text:
        out.pop()
    if not any(b.text for b in out):
        raise FileError("e_empty")
    return out


def read(data: bytes) -> Document:
    if not data:
        raise FileError("e_empty")
    if len(data) > MAX_BYTES:
        raise FileError("e_size")
    kind = sniff(data)
    if kind == "docx":
        found = docx.read(data, max_chars=MAX_CHARS)
        return Document("docx", [Block(label, key, text, True) for label, key, text in found])
    if kind == "pdf":
        reader, texts = pdf_mod.open_pdf(data, MAX_PAGES)
        if sum(len("".join(t.split())) for t in texts) < MIN_TEXT:
            if len(texts) > MAX_SCAN_PAGES:
                raise FileError("e_scan_pages")
            pdf_mod.pixels(reader)
            pictures = pdf_mod.rasterize(data, max_pages=MAX_SCAN_PAGES)
            return Document("scan", images=pictures, pages=len(pictures))
        return Document("pdf", _lines("\n".join(texts)), pages=len(texts))
    if kind in ("png", "jpeg", "webp"):
        return Document("scan", images=[image_mod.normalize(data)], pages=1)
    if kind == "text":
        text = decode_text(data)
        return Document("text", _lines(text, paragraphs=not _wrapped(text.splitlines())))
    raise FileError("e_type")


def from_paragraphs(paragraphs: list[str]) -> list[Block]:
    """A scan's paragraphs as the model read them: each is a paragraph of its own."""
    out, size = [], 0
    for p in paragraphs:
        text = " ".join(p.split())
        if not text:
            continue
        size += len(text) + 1
        if size > MAX_CHARS:
            raise FileError("e_too_long")
        out.append(Block("", "", text, True))
    if not out:
        raise FileError("e_empty")
    return out
