"""Clauses of two drafts of a contract, paired and compared word by word, without a model.

A clause is a Word paragraph, or lines of a PDF or a text joined until the next number («5.2.», «5.2)»,
«Статья 5», «Article 5»), a heading or a paragraph's end. Clauses equal after normalising case, «ё», quotes,
dashes, spaces and the number are not changes (a renumbering is cosmetic). The rest are paired in document
order by their words; a clause left without a pair is deleted or added, a deleted and an added clause with the
same text is moved. Within a pair the difference is found word by word, and every fragment and quote is a
slice of the clause's own text, so nothing shown as a quote can be absent from the document.
"""

import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from typing import Any

from alnuzum_contracts.intake import Block
from alnuzum_files import FileError

MAX_CLAUSES = 800
MAX_CLAUSE_CHARS = 5000  # a clause longer than this is no clause a person compares word by word
MAX_WORD_PAIRS = 4_000_000  # words of ours × words of theirs a word-by-word comparison may walk
QUOTE = 160  # the longest quote, in characters
CONTEXT = 3  # words of context around a difference in a quote
SIMILAR = 0.4  # the share of common words two clauses need to be a pair
SIMILAR_SAME_NUMBER = 0.2


class TooManyClauses(FileError):
    """Refused, not cut: a cut would read as clauses deleted and hide the changes after it."""

    def __init__(self, n: int) -> None:
        super().__init__("e_clauses")
        self.n = n


_NUM = re.compile(r"^\s*(?:п\.\s*|пункт\s+)?(\d{1,2}(?:\.\d{1,3}){0,4})(\.|\))?(?=\s|$)", re.IGNORECASE)
_WORD_NUMBER = re.compile(r"^\s*(?:статья|раздел|article|section|clause)\s+(\d{1,2})\b", re.IGNORECASE)
_PARTY = re.compile(r"^[A-ZА-ЯЁ][\w«»\"' .-]{1,40}:\s")
_TOKEN = re.compile(r"\w+(?:[.,]\d+)*|[^\w\s]")
_QUOTES = str.maketrans({c: '"' for c in "«»“”„‟\"'’‘`"} | {c: "-" for c in "‐‑‒–—―−"} | {"ё": "е"})
_SPACES = re.compile(r"[\s   ]+")


def number_of(text: str) -> str:
    """«5.2.» → «5.2», «Статья 5» → «5»; «30 дней» is not a number of a clause."""
    if m := _WORD_NUMBER.match(text):
        return m.group(1)
    m = _NUM.match(text)
    if not m:
        return ""
    if "." not in m.group(1) and not m.group(2):
        return ""  # «30 (тридцати) дней»: a number that opens a line, not a clause
    return m.group(1)


def strip_number(text: str) -> str:
    if m := _WORD_NUMBER.match(text):
        return text[m.end() :].lstrip(" .:-")
    m = _NUM.match(text)
    if m and ("." in m.group(1) or m.group(2)):
        return text[m.end() :].lstrip()
    return text


def normal(text: str) -> str:
    t = _SPACES.sub(" ", strip_number(text).casefold().translate(_QUOTES))
    t = re.sub(r"\s*([,.;:!?)])", r"\1", t)
    t = re.sub(r"([(\"])\s+", r"\1", t)
    return t.strip(" .;:,")


def _heading(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return len(letters) >= 3 and len(text) <= 90 and sum(c.isupper() for c in letters) >= 0.8 * len(letters)


@dataclass
class Clause:
    label: str  # the number Word draws before the paragraph («5.2.»); a number in the text stays in the text
    key: str  # «5.2»: a hint for pairing, and how the page and the note name the clause
    text: str

    @property
    def norm(self) -> str:
        return normal(self.text)

    @property
    def name(self) -> str:
        return self.key or self.label.rstrip(".)")


def split(blocks: Iterable[Block]) -> list[Clause]:
    """Blocks into clauses. Lines that are not paragraphs (a PDF, a text) are joined to the clause before
    them unless they open a clause, are a heading, or the line before them ended a paragraph (a short line
    that ends a sentence)."""
    items = list(blocks)
    soft = [len(b.text) for b in items if not b.hard and b.text]
    width = sorted(soft)[len(soft) * 3 // 4] if soft else 0
    out: list[Clause] = []
    open_: Clause | None = None
    last_len = 0
    for b in items:
        text = b.text
        if b.hard:
            if not text:
                continue
            key = b.key or number_of(text)
            out.append(Clause(b.label, key, text))
            open_ = None
            continue
        if not text:
            open_ = None
            continue
        key = number_of(text)
        closed = open_ is not None and open_.text.rstrip()[-1:] in ".;:!?"
        # a short line that ends a sentence ends its paragraph; so does a sentence before «Заказчик: …»
        ends = closed and (last_len < 0.7 * width or bool(_PARTY.match(text)))
        if key or _heading(text) or open_ is None or ends:
            clause = Clause("", key, text)
            out.append(clause)
            open_ = None if _heading(text) else clause
        else:
            if re.search(r"\w-$", open_.text) and text[:1].isalnum():
                open_.text += text  # «CRM-» + «системы»: the hyphen is kept, the line break is not a space
            else:
                open_.text += " " + text
        last_len = len(text)
    if len(out) > MAX_CLAUSES:
        raise TooManyClauses(len(out))
    if any(len(c.text) > MAX_CLAUSE_CHARS for c in out):
        raise FileError("e_clause_long")
    return out


# --- word by word ---


@dataclass(frozen=True)
class Token:
    key: str
    start: int
    end: int

    @property
    def word(self) -> bool:
        return self.key[:1].isalnum()


def tokens(text: str) -> list[Token]:
    return [Token(m.group().casefold().translate(_QUOTES), m.start(), m.end()) for m in _TOKEN.finditer(text)]


def words(text: str) -> set[str]:
    return {t.key for t in tokens(strip_number(text)) if t.word}


def similarity(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return 2 * len(a & b) / (len(a) + len(b))


@dataclass
class Hunk:
    a: tuple[int, int]  # characters of our clause (empty for an insertion: where it would be)
    b: tuple[int, int]  # characters of theirs


def hunks(a_text: str, b_text: str) -> list[Hunk]:
    """The fragments that differ, word by word; differences of punctuation alone are not fragments."""
    ta, tb = tokens(a_text), tokens(b_text)
    # the number of a clause is not a difference (a renumbering)
    skip_a = len(tokens(a_text[: len(a_text) - len(strip_number(a_text))]))
    skip_b = len(tokens(b_text[: len(b_text) - len(strip_number(b_text))]))
    ta, tb = ta[skip_a:], tb[skip_b:]
    out: list[Hunk] = []
    if len(ta) * len(tb) > MAX_WORD_PAIRS:  # the walk is n × m: the whole clause is one fragment
        if not ta or not tb:
            return []
        return [Hunk((ta[0].start, ta[-1].end), (tb[0].start, tb[-1].end))]
    matcher = SequenceMatcher(None, [t.key for t in ta], [t.key for t in tb], autojunk=False)
    # differing runs joined across punctuation alone: «30 (тридцати) рабочих» is one fragment
    groups: list[list[int]] = []
    open_: list[int] | None = None
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            if any(t.word for t in ta[i1:i2]):
                open_ = None
            continue
        if not any(t.word for t in ta[i1:i2]) and not any(t.word for t in tb[j1:j2]):
            continue
        if open_ is None:
            open_ = [i1, i2, j1, j2]
            groups.append(open_)
        else:
            open_[1], open_[3] = i2, j2
    for i1, i2, j1, j2 in groups:
        a_span = (ta[i1].start, ta[i2 - 1].end) if i2 > i1 else _point(ta, i1, len(a_text))
        b_span = (tb[j1].start, tb[j2 - 1].end) if j2 > j1 else _point(tb, j1, len(b_text))
        out.append(Hunk(a_span, b_span))
    return out


def _point(ts: list[Token], i: int, size: int) -> tuple[int, int]:
    at = ts[i].start if i < len(ts) else size
    return (at, at)


def window(text: str, spans: list[tuple[int, int]]) -> tuple[int, int]:
    """A quote around the fragments: from a few words before the first to a few after the last, at most QUOTE
    characters (then around the first fragment). Always a slice of ``text``."""
    if not text:
        return (0, 0)
    if not spans:
        return (0, min(len(text), QUOTE))
    ts = tokens(text)
    first, last = spans[0][0], spans[-1][1]

    def around(lo: int, hi: int) -> tuple[int, int]:
        before = [t for t in ts if t.end <= lo]
        after = [t for t in ts if t.start >= hi]
        s = before[-CONTEXT].start if len(before) >= CONTEXT else 0
        e = after[CONTEXT - 1].end if len(after) >= CONTEXT else len(text)
        return s, e

    s, e = around(first, last)
    if e - s > QUOTE:
        s, e = around(spans[0][0], spans[0][1])
    if e - s > QUOTE:
        s, e = spans[0][0], min(spans[0][0] + QUOTE, len(text))
    while s > 0 and text[s - 1].isalnum():
        s -= 1
    return (s, e)


# --- the pairing ---


@dataclass
class Change:
    """``kind``: changed, added, deleted or moved. ``a``/``b``: the clause in ours/theirs. Spans and quotes
    are characters of those clauses' texts. Topic, risk, why come from the model (``rated``)."""

    id: int
    kind: str
    a: int | None
    b: int | None
    a_spans: list[tuple[int, int]] = field(default_factory=list)
    b_spans: list[tuple[int, int]] = field(default_factory=list)
    qa: tuple[int, int] | None = None
    qb: tuple[int, int] | None = None
    topic: str = "other"
    risk: str = "medium"
    why: str = ""
    substantive: bool = True
    rated: bool = False

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def of(cls, data: dict[str, Any]) -> "Change":
        d = dict(data)
        for k in ("a_spans", "b_spans"):
            d[k] = [tuple(x) for x in d.get(k) or []]
        for k in ("qa", "qb"):
            d[k] = tuple(d[k]) if d.get(k) else None
        return cls(**d)


@dataclass
class Comparison:
    ours: list[Clause]
    theirs: list[Clause]
    changes: list[Change]
    cosmetic: int  # pairs that differ only in case, quotes, spaces, punctuation or number
    same: int

    def to_json(self) -> dict[str, Any]:
        return {
            "ours": [[c.label, c.key, c.text] for c in self.ours],
            "theirs": [[c.label, c.key, c.text] for c in self.theirs],
            "changes": [c.to_json() for c in self.changes],
            "cosmetic": self.cosmetic,
            "same": self.same,
        }

    @classmethod
    def of(cls, data: dict[str, Any]) -> "Comparison":
        return cls(
            [Clause(*c) for c in data["ours"]],
            [Clause(*c) for c in data["theirs"]],
            [Change.of(c) for c in data["changes"]],
            int(data.get("cosmetic") or 0),
            int(data.get("same") or 0),
        )


def _pairs(ours: list[Clause], theirs: list[Clause]) -> list[tuple[int | None, int | None]]:
    """Every clause once, in order: (i, j) a pair, (i, None) only ours, (None, j) only theirs."""
    na, nb = [c.norm for c in ours], [c.norm for c in theirs]
    out: list[tuple[int | None, int | None]] = []
    matcher = SequenceMatcher(None, na, nb, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            out += [(i1 + k, j1 + k) for k in range(i2 - i1)]
        else:
            out += _align(ours, theirs, list(range(i1, i2)), list(range(j1, j2)))
    return out


def _align(
    ours: list[Clause], theirs: list[Clause], ia: list[int], jb: list[int]
) -> list[tuple[int | None, int | None]]:
    """The clauses between two runs of equal ones, paired in order by their words (dynamic programming over
    the gap: the most similar monotone pairing)."""
    if not ia or not jb:
        return [(i, None) for i in ia] + [(None, j) for j in jb]
    wa = [words(ours[i].text) for i in ia]
    wb = [words(theirs[j].text) for j in jb]
    n, m = len(ia), len(jb)

    def weight(x: int, y: int) -> float:
        same_key = bool(ours[ia[x]].key) and ours[ia[x]].key == theirs[jb[y]].key
        sim = similarity(wa[x], wb[y])
        if ours[ia[x]].norm == theirs[jb[y]].norm:
            return 3.0
        if sim >= SIMILAR or (same_key and sim >= SIMILAR_SAME_NUMBER):
            return sim + (0.25 if same_key else 0.0)
        if same_key:
            return 0.15  # a clause rewritten whole under its number: shown as was and now, not two changes
        return -1.0

    score = [[0.0] * (m + 1) for _ in range(n + 1)]
    for x in range(1, n + 1):
        row, prev = score[x], score[x - 1]
        for y in range(1, m + 1):
            best = max(prev[y], row[y - 1])
            w = weight(x - 1, y - 1)
            if w > 0 and prev[y - 1] + w > best:
                best = prev[y - 1] + w
            row[y] = best
    pairs: list[tuple[int | None, int | None]] = []
    x, y = n, m
    while x > 0 and y > 0:
        w = weight(x - 1, y - 1)
        if w > 0 and abs(score[x][y] - (score[x - 1][y - 1] + w)) < 1e-9:
            pairs.append((ia[x - 1], jb[y - 1]))
            x, y = x - 1, y - 1
        elif score[x][y] == score[x][y - 1]:
            pairs.append((None, jb[y - 1]))  # read backwards: a deletion comes before an addition
            y -= 1
        else:
            pairs.append((ia[x - 1], None))
            x -= 1
    pairs += [(ia[k], None) for k in range(x - 1, -1, -1)] + [(None, jb[k]) for k in range(y - 1, -1, -1)]
    pairs.reverse()
    return pairs


def compare(ours: list[Clause], theirs: list[Clause]) -> Comparison:
    changes: list[Change] = []
    cosmetic = same = 0
    pairs = _pairs(ours, theirs)
    for i, j in pairs:
        if i is not None and j is not None:
            a, b = ours[i], theirs[j]
            if a.text == b.text:
                same += 1
                continue
            found = hunks(a.text, b.text) if a.norm != b.norm else []
            if not found:
                cosmetic += 1
                continue
            a_spans, b_spans = [h.a for h in found], [h.b for h in found]
            changes.append(
                Change(0, "changed", i, j, a_spans, b_spans, window(a.text, a_spans), window(b.text, b_spans))
            )
        elif i is not None:
            changes.append(Change(0, "deleted", i, None, qa=window(ours[i].text, [])))
        elif j is not None:
            changes.append(Change(0, "added", None, j, qb=window(theirs[j].text, [])))
    # a clause deleted in one place and added in another with the same words is moved
    added = {theirs[c.b].norm: c for c in changes if c.kind == "added" and c.b is not None}
    for c in [c for c in changes if c.kind == "deleted"]:
        twin = added.pop(ours[c.a].norm, None) if c.a is not None else None
        if twin is not None:
            c.kind, c.b, c.qb = "moved", twin.b, twin.qb
            c.topic, c.risk, c.substantive, c.rated = "wording", "low", False, True
            changes.remove(twin)
    changes.sort(key=lambda c: (c.b if c.b is not None else _after(pairs, c.a), c.a or 0))
    for n, c in enumerate(changes, 1):
        c.id = n
    return Comparison(ours, theirs, changes, cosmetic, same)


def _after(pairs: list[tuple[int | None, int | None]], a: int | None) -> float:
    """Where a deleted clause stands in their draft: just after the pair before it."""
    last = -1
    for i, j in pairs:
        if i == a:
            return last + 0.5
        if j is not None:
            last = j
    return last + 0.5
