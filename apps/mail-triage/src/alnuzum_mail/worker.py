"""A letter from a stranger, opened in a process of its own (``python -I -m alnuzum_mail.worker <memory>``):
the MIME tree (nested parts, encodings, RFC 2047 headers), its HTML turned into text, the text layer of a PDF
attachment. A memory ceiling, and the parent kills it on time, which it cannot do to a thread: a letter of
thousands of nested parts or a PDF built to make its parser loop is stopped here, not in the platform.

The ``.eml`` comes on stdin; out goes ``{"letter": {...}}`` or ``{"error": code}``; exit 4 is what the parser
or the ceiling stopped. Attachments it does not read itself (a scan, a picture, a Word document) go back as
bytes: the parent reads them in children of their own, one at a time."""

import base64
import json
import re
import sys
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parseaddr
from html.parser import HTMLParser
from typing import Any

from alnuzum_files import FileError, sniff
from alnuzum_files.child import limit_memory

MAX_PARTS = 200
MAX_DEPTH = 10
MAX_ATTACHMENTS = 3  # read; the rest are listed
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
MAX_PDF_PAGES = 20
MAX_HTML_CHARS = 400_000  # the HTML of a body read at most; a longer body is refused by its text size
MIN_PDF_TEXT = 40  # a PDF with less text than this is a scan
HEADER_CHARS = 300

_SPACE = re.compile(r"[ \t ]+")
_BLANKS = re.compile(r"\n{3,}")


class _Text(HTMLParser):
    """The visible text of an HTML body: blocks on lines of their own, no scripts, styles or comments."""

    BLOCKS = frozenset(
        {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "blockquote"}
    )
    SKIP = frozenset({"script", "style", "head", "title"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCKS:
            self.out.append("\n")
        elif tag == "td":
            self.out.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCKS:
            self.out.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.out.append(data)


def html_text(html: str) -> str:
    parser = _Text()
    parser.feed(html[:MAX_HTML_CHARS])
    parser.close()
    return tidy("".join(parser.out))


def tidy(text: str) -> str:
    lines = [
        _SPACE.sub(" ", line).strip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ]
    return _BLANKS.sub("\n\n", "\n".join(lines)).strip()


def _header(msg: EmailMessage, name: str) -> str:
    try:
        value = msg.get(name)
    except (ValueError, IndexError, TypeError, AttributeError):  # a header the parser cannot decode
        return ""
    return " ".join(str(value or "").split())[:HEADER_CHARS]


def _depth_and_count(msg: EmailMessage) -> None:
    count = 0

    def walk(part: EmailMessage, depth: int) -> None:
        nonlocal count
        count += 1
        if count > MAX_PARTS or depth > MAX_DEPTH:
            raise FileError("e_mime")
        if part.is_multipart():
            for sub in part.iter_parts():
                walk(sub, depth + 1)  # type: ignore[arg-type]

    walk(msg, 0)


def _body(msg: EmailMessage) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        content = part.get_content()
    except (LookupError, ValueError, UnicodeError):
        content = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")  # type: ignore[union-attr]
    text = str(content)
    return html_text(text) if part.get_content_type() == "text/html" else tidy(text)


def _attachment(part: EmailMessage) -> dict[str, Any]:
    name = " ".join(str(part.get_filename() or "").split())[:120] or "—"
    try:
        data = part.get_payload(decode=True)
    except (ValueError, TypeError):
        data = None
    if not isinstance(data, bytes) or not data:
        return {"name": name, "kind": "empty", "size": 0}
    item: dict[str, Any] = {"name": name, "size": len(data)}
    if len(data) > MAX_ATTACHMENT_BYTES:
        return item | {"kind": "big"}
    kind = sniff(data)
    if kind == "pdf":
        from alnuzum_files import pdf as pdf_mod

        try:
            reader, texts = pdf_mod.open_pdf(data, MAX_PDF_PAGES)
        except FileError as exc:
            return item | {"kind": "pdf", "error": exc.code}
        if sum(len("".join(t.split())) for t in texts) < MIN_PDF_TEXT:
            pdf_mod.pixels(reader)  # declared pictures bounded before the parent draws the pages
            return item | {"kind": "scan", "data": base64.b64encode(data).decode()}
        return item | {"kind": "pdf", "text": tidy("\n".join(texts))}
    if kind in ("png", "jpeg", "webp", "docx"):
        return item | {"kind": kind if kind == "docx" else "image", "data": base64.b64encode(data).decode()}
    if kind == "text" and part.get_content_maintype() == "text":
        try:
            return item | {
                "kind": "text",
                "text": tidy(data.decode(part.get_content_charset() or "utf-8", "replace")),
            }
        except LookupError:
            return item | {"kind": "text", "text": tidy(data.decode("utf-8", "replace"))}
    return item | {"kind": "other"}


def open_letter(data: bytes) -> dict[str, Any]:
    msg = BytesParser(policy=policy.default).parsebytes(data)
    if not isinstance(msg, EmailMessage) or not msg.keys():
        raise FileError("e_not_mail")
    _depth_and_count(msg)
    name, address = parseaddr(_header(msg, "From"))
    attachments: list[dict[str, Any]] = []
    for n, part in enumerate(msg.iter_attachments()):
        if n >= MAX_ATTACHMENTS:
            attachments.append(
                {"name": " ".join(str(part.get_filename() or "").split())[:120] or "—", "kind": "skipped"}
            )
            continue
        attachments.append(_attachment(part))
    return {
        "from_name": name[:120],
        "from_email": address[:200],
        "to": ", ".join(a for _, a in getaddresses([_header(msg, "To")]) if a)[:HEADER_CHARS],
        "subject": _header(msg, "Subject"),
        "date": _header(msg, "Date"),
        "in_reply_to": bool(_header(msg, "In-Reply-To") or _header(msg, "References")),
        "bulk": bool(
            _header(msg, "List-Unsubscribe") or _header(msg, "Precedence").casefold() in ("bulk", "list")
        ),
        "body": _body(msg),
        "attachments": attachments,
    }


# --- the parent's side ---

COMMAND = [sys.executable, "-I", "-m", "alnuzum_mail.worker"]


def read_eml(data: bytes) -> dict[str, Any]:
    from alnuzum_files import child

    found: dict[str, Any] = json.loads(child.run([*COMMAND, str(child.MEMORY)], data))
    if "error" in found:
        raise FileError(str(found["error"]))
    letter: dict[str, Any] = found["letter"]
    return letter


def main(argv: list[str]) -> int:
    limit_memory(int(argv[1]))
    data = sys.stdin.buffer.read()
    try:
        out: dict[str, Any] = {"letter": open_letter(data)}
    except FileError as exc:
        out = {"error": exc.code}
    except Exception:  # whatever the parser or the memory ceiling stops, the letter is refused
        return 4
    sys.stdout.buffer.write(json.dumps(out, ensure_ascii=False).encode())
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
