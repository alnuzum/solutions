"""The paragraphs of a Word document (DOCX), each with the number Word shows before it.

Read in a process of its own (``python -I -m alnuzum_files.docx <memory> <max_chars>``): the XML is parsed by
defusedxml (no DTD, no entities) as a stream, under a memory ceiling, and killed when it runs over its time.
The DOCX comes on stdin; stdout is JSON ``{"p": [[label, key, text], ...]}``: ``label`` as Word draws it
(«5.2.», «а)»), ``key`` the number with its parents («5.2», «2.1» for a nested list numbered «1.»). Exit codes:
0 read, 3 longer than ``max_chars``, 4 not readable.

Tracked changes are read as accepted: inserted runs are text, deleted and moved-away runs are not.
"""

import io
import json
import sys
import zipfile
from typing import Any

from alnuzum_files import FileError, child, unzip_bounds

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
MAX_BYTES = 8 * 1024 * 1024
MAX_DOCUMENT_XML = 10 * 1024 * 1024
MAX_SMALL_XML = 2 * 1024 * 1024  # numbering.xml, styles.xml
PART_LIMITS = {
    "word/document.xml": MAX_DOCUMENT_XML,
    "word/numbering.xml": MAX_SMALL_XML,
    "word/styles.xml": MAX_SMALL_XML,
}
DOCX_COMMAND = [sys.executable, "-I", "-m", "alnuzum_files.docx"]
SKIP = {
    W + "del",
    W + "moveFrom",
    W + "pPr",
    W + "rPr",
    W + "instrText",
    W + "delText",
    W + "footnoteReference",
}
RU = "абвгдежзиклмнопрстуфхцчшщэюя"


def _val(el: Any, name: str = "val") -> str | None:
    return None if el is None else el.get(W + name)


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


def _roman(n: int) -> str:
    out = ""
    for value, word in ((1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"), (50, "l"),
                        (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")):  # fmt: skip
        while n >= value:
            out, n = out + word, n - value
    return out


def render(fmt: str, n: int) -> str:
    n = max(n, 1)
    if fmt in ("bullet", "none"):
        return ""
    if fmt == "lowerLetter":
        return chr(ord("a") + (n - 1) % 26)
    if fmt == "upperLetter":
        return chr(ord("A") + (n - 1) % 26)
    if fmt == "lowerRoman":
        return _roman(min(n, 3999))
    if fmt == "upperRoman":
        return _roman(min(n, 3999)).upper()
    if fmt in ("russianLower", "russianUpper"):
        letter = RU[(n - 1) % len(RU)]
        return letter.upper() if fmt == "russianUpper" else letter
    return str(n)


class Numbering:
    """Word's list numbering: levels of each abstract list, the list instances that point at them (with their
    start overrides) and the styles that carry a list. Counters are kept per abstract list, so instances that
    share one continue it, as Word draws them."""

    def __init__(self, numbering: bytes | None, styles: bytes | None) -> None:
        from defusedxml import ElementTree

        self.levels: dict[str, dict[int, tuple[str, str, int]]] = {}
        self.nums: dict[str, tuple[str, dict[int, int]]] = {}
        self.styles: dict[str, tuple[str | None, int | None, str | None]] = {}
        self.counters: dict[str, dict[int, int]] = {}
        self.started: set[str] = set()
        self.stack: list[str] = []
        if numbering:
            root = ElementTree.fromstring(numbering, forbid_dtd=True)
            for a in root.findall(W + "abstractNum"):
                lv: dict[int, tuple[str, str, int]] = {}
                for el in a.findall(W + "lvl")[:9]:
                    lv[_int(_val(el, "ilvl"), 0)] = (
                        _val(el.find(W + "numFmt")) or "decimal",
                        _val(el.find(W + "lvlText")) or "",
                        _int(_val(el.find(W + "start")), 1),
                    )
                self.levels[_val(a, "abstractNumId") or ""] = lv
            for n in root.findall(W + "num"):
                overrides = {
                    _int(_val(o, "ilvl"), 0): _int(_val(o.find(W + "startOverride")), 1)
                    for o in n.findall(W + "lvlOverride")
                    if o.find(W + "startOverride") is not None
                }
                self.nums[_val(n, "numId") or ""] = (_val(n.find(W + "abstractNumId")) or "", overrides)
        if styles:
            root = ElementTree.fromstring(styles, forbid_dtd=True)
            for s in root.findall(W + "style"):
                np = s.find(f"{W}pPr/{W}numPr")
                num = _val(np.find(W + "numId")) if np is not None else None
                ilvl = (
                    _int(_val(np.find(W + "ilvl")), 0)
                    if np is not None and np.find(W + "ilvl") is not None
                    else None
                )
                self.styles[_val(s, "styleId") or ""] = (num, ilvl, _val(s.find(W + "basedOn")))

    def _style(self, style: str | None) -> tuple[str | None, int]:
        for _ in range(6):  # a chain of basedOn, bounded
            if style is None or style not in self.styles:
                return None, 0
            num, ilvl, based = self.styles[style]
            if num is not None:
                return num, ilvl or 0
            style = based
        return None, 0

    def label(self, p: Any) -> tuple[str, str]:
        ppr = p.find(W + "pPr")
        np = ppr.find(W + "numPr") if ppr is not None else None
        num: str | None
        if np is not None and np.find(W + "numId") is not None:
            num, ilvl = _val(np.find(W + "numId")), _int(_val(np.find(W + "ilvl")), 0)
        else:
            num, ilvl = self._style(_val(ppr.find(W + "pStyle")) if ppr is not None else None)
        if not num or num == "0" or num not in self.nums:
            return "", ""
        ilvl = min(max(ilvl, 0), 8)
        abstract, overrides = self.nums[num]
        levels = self.levels.get(abstract, {})
        counters = self.counters.setdefault(abstract, {})
        if num not in self.started:
            self.started.add(num)
            for lvl, start in overrides.items():
                counters[lvl] = start - 1
        start = levels.get(ilvl, ("decimal", "", 1))[2]
        counters[ilvl] = counters.get(ilvl, start - 1) + 1
        for deeper in [k for k in counters if k > ilvl]:
            del counters[deeper]
        fmt, text, _ = levels.get(ilvl, ("decimal", f"%{ilvl + 1}.", 1))
        if fmt in ("bullet", "none"):
            return "", ""
        label = text
        for k in range(ilvl + 1):
            kfmt, _, kstart = levels.get(k, ("decimal", "", 1))
            label = label.replace(f"%{k + 1}", render(kfmt, counters.get(k, kstart)))
        # the key: the list's own counters, and for levels it does not count (a nested list numbered on its
        # own) the numbers of the decimal list around it
        parts = []
        for k in range(ilvl + 1):
            if k in counters:
                parts.append(render(levels.get(k, ("decimal", "", 1))[0], counters[k]))
            else:
                parts.append(self.stack[k] if k < len(self.stack) else "1")
        if fmt.startswith("decimal"):
            self.stack = parts
        return label.strip(), ".".join(parts)


def _text(p: Any) -> str:
    parts: list[str] = []

    def walk(el: Any) -> None:
        for ch in el:
            tag = ch.tag
            if tag in SKIP:
                continue
            if tag == W + "t":
                parts.append(ch.text or "")
            elif tag in (W + "tab", W + "br", W + "cr"):
                parts.append(" ")
            elif tag == W + "noBreakHyphen":
                parts.append("-")
            else:
                walk(ch)

    walk(p)
    return " ".join("".join(parts).split())


def paragraphs(data: bytes, max_chars: int) -> list[list[str]]:
    from defusedxml import ElementTree

    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = set(z.namelist())
        numbering = Numbering(
            z.read("word/numbering.xml") if "word/numbering.xml" in names else None,
            z.read("word/styles.xml") if "word/styles.xml" in names else None,
        )
        out: list[list[str]] = []
        size = 0
        with z.open("word/document.xml") as source:
            for _event, el in ElementTree.iterparse(source, events=("end",), forbid_dtd=True):
                if el.tag != W + "p":
                    continue
                text = _text(el)
                label, key = numbering.label(el)
                el.clear()  # its text is taken; an enclosing paragraph (a text box) must not read it again
                if not text:
                    continue
                size += len(text) + len(label) + 1
                if size > max_chars:
                    raise OverflowError
                out.append([label, key, text])
    return out


def main(argv: list[str]) -> int:
    child.limit_memory(int(argv[1]))
    max_chars = int(argv[2])
    data = sys.stdin.buffer.read(MAX_BYTES + 1)
    try:
        found = paragraphs(data, max_chars)
    except OverflowError:
        return 3
    except Exception:  # whatever the parser or the memory ceiling stops, the file is refused
        return 4
    sys.stdout.buffer.write(json.dumps({"p": found}).encode("ascii"))
    sys.stdout.buffer.flush()
    return 0


def read(
    data: bytes, *, max_chars: int, command: list[str] | None = None, seconds: float = child.SECONDS
) -> list[tuple[str, str, str]]:
    """The paragraphs (label, key, text) of a DOCX; the zip is bounded before the child is started."""
    if len(data) > MAX_BYTES:
        raise FileError("e_size")
    unzip_bounds(data, part_limits=PART_LIMITS)
    raw = child.run(
        [*(command or DOCX_COMMAND), str(child.MEMORY), str(max_chars)],
        data,
        seconds=seconds,
        codes={3: "e_too_long"},
    )
    try:
        items = json.loads(raw)["p"]
        found = [(str(a), str(b), str(c)) for a, b, c in items]
    except (ValueError, KeyError, TypeError):
        raise FileError("e_damaged") from None
    if not found:
        raise FileError("e_empty")
    return found


if __name__ == "__main__":
    sys.exit(main(sys.argv))
