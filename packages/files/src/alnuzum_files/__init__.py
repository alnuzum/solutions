"""Files from strangers, shared by the solutions: the type decided by the content, never by the name or the
browser's Content-Type; a zip bounded by what its parts declare before anything unpacks it; whatever may
run away with memory or time (drawing a scan, reading a Word document) done in a process of its own.

Nothing here writes a file anywhere: the bytes live in memory for the length of a request.
"""

import io
import zipfile


class FileError(ValueError):
    """A file that is refused; ``code`` names the message for the user."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# a zip (a spreadsheet, a Word document) is bounded by the declared sizes of its parts: a few MB of deflate
# can hold gigabytes of XML
MAX_UNZIPPED = 50 * 1024 * 1024
MAX_PART = 20 * 1024 * 1024
MAX_RATIO = 200  # unpacked to packed, for a part over 1 MB
MAX_ZIP_PARTS = 1000


def sniff(data: bytes) -> str:
    """pdf, png, jpeg, webp, xlsx, docx, text or unknown: from the first bytes and, for a zip, its parts."""
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
        except (zipfile.BadZipFile, ValueError):
            return "unknown"
        if "xl/workbook.xml" in names:
            return "xlsx"
        if "word/document.xml" in names:
            return "docx"
        return "unknown"
    if b"\x00" in data[:4096]:
        return "unknown"
    return "text"


def decode_text(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise FileError("e_type")


def unzip_bounds(data: bytes, *, part_limits: dict[str, int] | None = None) -> None:
    """The declared sizes of the parts: the zip reader never unpacks more than a part declares (a part that
    holds more fails its CRC), so bounding them bounds the memory of the parse. ``part_limits`` bounds named
    parts tighter (a Word document's ``word/document.xml``)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            parts = z.infolist()
    except (zipfile.BadZipFile, ValueError):
        raise FileError("e_damaged") from None
    if len(parts) > MAX_ZIP_PARTS:
        raise FileError("e_unpacked")
    total = 0
    for part in parts:
        total += part.file_size
        if part.file_size > MAX_PART or total > MAX_UNZIPPED:
            raise FileError("e_unpacked")
        if part.file_size > 1024 * 1024 and part.file_size > MAX_RATIO * max(part.compress_size, 1):
            raise FileError("e_unpacked")
        if part_limits and part.file_size > part_limits.get(part.filename, MAX_PART):
            raise FileError("e_unpacked")


__all__ = ["FileError", "decode_text", "sniff", "unzip_bounds"]
