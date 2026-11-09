"""A customer's letter: an ``.eml`` file (its body, its headers, up to three attachments read) or a letter
pasted as text (the header lines at its top, if any, are read as headers).

Nothing is cut silently: the history of earlier letters quoted below the new one is set aside and the page
says how much of it there was; a body still over ``MAX_BODY`` is refused with the number; an attachment over
``MAX_ATTACHMENT_TEXT`` is read from its start and the page says so."""

import re
from dataclasses import asdict, dataclass, field
from email.utils import parseaddr
from typing import Any

from alnuzum_files import FileError, docx, sniff
from alnuzum_files import image as image_mod
from alnuzum_files import pdf as pdf_mod

MAX_BYTES = 8 * 1024 * 1024
MAX_PASTE = 60_000
MAX_BODY = 20_000
MAX_ATTACHMENT_TEXT = 6_000
MAX_IMAGES = 4  # pictures of scans and photos the model reads, per letter
SCAN_PAGES = 2  # of a scanned attachment: a card of an enterprise is a page or two

_HEAD = re.compile(
    r"^(from|от|to|кому|subject|тема|date|дата|sent|отправлено|cc|копия)\s*:\s*(.*)$", re.IGNORECASE
)
# where the history of earlier letters begins: a quoted line, a separator of a client, «On … wrote:»
_HISTORY = re.compile(
    r"^(?:>|-{2,}\s*(?:original message|исходное сообщение|пересылаемое сообщение|forwarded message)"
    r"|_{10,}$"
    r"|on .{4,200}wrote:\s*$"
    r"|.{0,120}(?:пишет|написал|написала|wrote|yozdi)\s*:\s*$)",
    re.IGNORECASE,
)
_OUTLOOK = re.compile(r"^(?:from|от)\s*:", re.IGNORECASE)
_OUTLOOK_NEXT = re.compile(r"^(?:sent|отправлено|date|дата|to|кому|subject|тема)\s*:", re.IGNORECASE)
_FORWARD = re.compile(r"^\s*(?:fwd?|пересл|переслано)\s*:", re.IGNORECASE)
_REPLY = re.compile(r"^\s*(?:re|ответ|отв|aw|sv)\s*(?:\[\d+\])?\s*:", re.IGNORECASE)


@dataclass
class Attachment:
    id: str
    name: str
    kind: str  # pdf | docx | text | scan | image | big | other | empty | skipped
    size: int = 0
    text: str = ""
    cut: int = 0  # characters past the limit, not read
    error: str | None = None
    pictures: int = 0  # pictures of it the model reads


@dataclass
class Letter:
    source: str  # eml | text
    from_name: str
    from_email: str
    to: str
    subject: str
    date: str
    reply_hint: bool
    bulk: bool
    body: str
    history: int  # characters of earlier letters set aside
    attachments: list[Attachment] = field(default_factory=list)
    # (attachment id, JPEG) for the model; never stored
    images: list[tuple[str, bytes]] = field(default_factory=list, repr=False)

    def header(self) -> str:
        sender = f"{self.from_name} <{self.from_email}>" if self.from_email else self.from_name
        return f"{sender}\n{self.subject}".strip()

    def sources(self) -> dict[str, str]:
        """The texts fields may be quoted from: the header, the body, each attachment read."""
        out = {"h": self.header(), "b": self.body}
        out |= {a.id: a.text for a in self.attachments if a.text}
        return out

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("images")
        return data

    @classmethod
    def of(cls, data: dict[str, Any]) -> "Letter":
        items = [Attachment(**a) for a in data.get("attachments") or []]
        return cls(**{k: v for k, v in data.items() if k not in ("attachments", "images")}, attachments=items)


def split_history(text: str) -> tuple[str, int]:
    """The new letter and how many characters of quoted history follow it."""
    lines = text.split("\n")
    for n, line in enumerate(lines):
        stripped = line.strip()
        start = _HISTORY.match(stripped) or (
            _OUTLOOK.match(stripped) and any(_OUTLOOK_NEXT.match(x.strip()) for x in lines[n + 1 : n + 4])
        )
        if start:  # a letter that is all quotation, or almost: kept whole
            body = "\n".join(lines[:n]).strip()
            if len("".join(body.split())) >= 20:
                return body, len(text) - len("\n".join(lines[:n]))
            break
    return text.strip(), 0


def _bounded(body: str) -> str:
    if len(body) > MAX_BODY:
        raise FileError("e_too_long")
    if not body.strip():
        raise FileError("e_empty")
    return body


def from_text(text: str) -> Letter:
    """A pasted letter: header lines at the top (From/От, Subject/Тема, …) are read as headers."""
    if len(text) > MAX_PASTE:
        raise FileError("e_too_long")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    heads: dict[str, str] = {}
    n = 0
    while n < len(lines) and (found := _HEAD.match(lines[n].strip())):
        key = found.group(1).casefold()
        heads.setdefault(
            {"от": "from", "кому": "to", "тема": "subject", "дата": "date"}.get(key, key), found.group(2)
        )
        n += 1
    name, address = parseaddr(heads.get("from", ""))
    body, history = split_history("\n".join(lines[n:]))
    subject = " ".join(heads.get("subject", "").split())[:300]
    forwarded = _FORWARD.match(subject) and len(body) < 40
    if forwarded:  # a forwarded letter: what matters is the letter it carries
        body, history = "\n".join(lines[n:]).strip(), 0
    return Letter(
        "text",
        name[:120],
        address[:200] if "@" in address else "",
        heads.get("to", "")[:300],
        subject,
        heads.get("date", "")[:100],
        bool(_REPLY.match(subject)),
        False,
        _bounded(body),
        history,
    )


def _read_attachment(n: int, raw: dict[str, Any], images: list[tuple[str, bytes]]) -> Attachment:
    import base64

    item = Attachment(
        f"a{n}", str(raw.get("name") or "—"), str(raw.get("kind") or "other"), int(raw.get("size") or 0)
    )
    item.error = raw.get("error")
    text = str(raw.get("text") or "")
    data = base64.b64decode(raw["data"]) if raw.get("data") else b""
    try:
        if item.kind == "docx" and data:
            text = "\n".join(t for _label, _key, t in docx.read(data, max_chars=MAX_ATTACHMENT_TEXT * 4))
        elif item.kind == "scan" and data:
            room = MAX_IMAGES - len(images)
            if room > 0:
                # the renderer draws a scan of up to ten pages; the first ones are read
                pages = pdf_mod.rasterize(data, max_pages=10)[: min(SCAN_PAGES, room)]
                images += [(item.id, p) for p in pages]
                item.pictures = len(pages)
        elif item.kind == "image" and data and len(images) < MAX_IMAGES:
            images.append((item.id, image_mod.normalize(data)))
            item.pictures = 1
    except FileError as exc:
        item.error = exc.code
    if len(text) > MAX_ATTACHMENT_TEXT:
        item.cut = len(text) - MAX_ATTACHMENT_TEXT
        text = text[:MAX_ATTACHMENT_TEXT]
    item.text = text
    return item


def from_eml(data: bytes) -> Letter:
    """An ``.eml`` file: opened in a child process, its attachments read one at a time."""
    from alnuzum_mail import worker

    if not data:
        raise FileError("e_empty")
    if len(data) > MAX_BYTES:
        raise FileError("e_size")
    if sniff(data) != "text":
        raise FileError("e_not_mail")
    raw = worker.read_eml(data)
    body, history = split_history(str(raw.get("body") or ""))
    subject = str(raw.get("subject") or "")
    if _FORWARD.match(subject) and len(body) < 40:
        body, history = str(raw.get("body") or "").strip(), 0
    images: list[tuple[str, bytes]] = []
    attachments = [
        _read_attachment(n, a, images) for n, a in enumerate(raw.get("attachments") or [], start=1)
    ]
    letter = Letter(
        "eml",
        str(raw.get("from_name") or ""),
        str(raw.get("from_email") or "") if "@" in str(raw.get("from_email") or "") else "",
        str(raw.get("to") or ""),
        subject,
        str(raw.get("date") or ""),
        bool(raw.get("in_reply_to")) or bool(_REPLY.match(subject)),
        bool(raw.get("bulk")),
        body if body.strip() or not attachments else "",
        history,
        attachments,
        images,
    )
    if len(letter.body) > MAX_BODY:
        raise FileError("e_too_long")
    if not letter.body.strip() and not any(a.text or a.pictures for a in attachments):
        raise FileError("e_empty")
    return letter


def read(data: bytes | None, text: str | None) -> Letter:
    """A file wins over pasted text."""
    if data:
        return from_eml(data)
    if text and text.strip():
        return from_text(text)
    raise FileError("e_empty")


__all__ = ["MAX_BYTES", "Attachment", "FileError", "Letter", "from_eml", "from_text", "read"]
