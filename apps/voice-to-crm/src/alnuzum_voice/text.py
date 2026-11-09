"""Text around the speech: phrases of a transcript, Uzbek and Russian letters both ways, what may reach
a CRM."""

import re
import unicodedata

MAX_TEXT = 2000  # a typed command or a transcript of two minutes, at most
NOTE_TEXT = 1500  # what of the transcript goes into the note
MAX_PHRASES = 40

_SENTENCE = re.compile(r"(?<=[.!?;])\s+|\n+")

# Uzbek Cyrillic (and Russian) to Uzbek Latin, as the official alphabet writes it
_TO_LATIN = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "j", "з": "z", "и": "i",
    "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "x", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sh", "ъ": "", "ы": "i", "ь": "",
    "э": "e", "ю": "yu", "я": "ya", "ў": "o'", "қ": "q", "ғ": "g'", "ҳ": "h",
}  # fmt: skip
# Latin to Russian Cyrillic: longer letter groups first
_TO_CYRILLIC = [
    ("shch", "щ"), ("sh", "ш"), ("ch", "ч"), ("yo", "ё"), ("yu", "ю"), ("ya", "я"), ("ts", "ц"),
    ("o'", "у"), ("g'", "г"), ("zh", "ж"), ("kh", "х"),
    ("a", "а"), ("b", "б"), ("v", "в"), ("g", "г"), ("d", "д"), ("e", "е"), ("j", "ж"), ("z", "з"),
    ("i", "и"), ("y", "й"), ("k", "к"), ("l", "л"), ("m", "м"), ("n", "н"), ("o", "о"), ("p", "п"),
    ("r", "р"), ("s", "с"), ("t", "т"), ("u", "у"), ("f", "ф"), ("x", "х"), ("q", "к"), ("h", "х"),
    ("c", "к"), ("w", "в"),
]  # fmt: skip
_JOINED = re.compile(r"(?<=[a-zа-яёўқғҳ])(?=[A-ZА-ЯЁЎҚҒҲ])")
_APOSTROPHES = str.maketrans({"ʻ": "'", "‘": "'", "’": "'", "`": "'", "ʼ": "'"})


def clean(value: str, limit: int = MAX_TEXT) -> str:
    """Printable text only: control and format characters (bidi overrides, zero widths) become spaces,
    runs of spaces one, at most ``limit`` characters."""
    value = unicodedata.normalize("NFC", value)
    out = []
    for ch in value:
        cat = unicodedata.category(ch)
        out.append(" " if ch in "\r\n\t" or cat[0] == "C" or cat in ("Zl", "Zp") else ch)
    return re.sub(r"\s+", " ", "".join(out)).strip()[:limit]


def phrases(transcript: str) -> list[str]:
    """The transcript cut into numbered phrases at sentence ends; a long tail is kept in the last one."""
    text = unicodedata.normalize("NFC", transcript).strip()
    parts = [clean(p, 400) for p in _SENTENCE.split(text)]
    parts = [p for p in parts if p]
    if len(parts) > MAX_PHRASES:
        parts = [*parts[: MAX_PHRASES - 1], " ".join(parts[MAX_PHRASES - 1 :])[:400]]
    return parts


def to_latin(value: str) -> str:
    out = []
    for ch in value.translate(_APOSTROPHES):
        low = ch.lower()
        rep = _TO_LATIN.get(low)
        if rep is None:
            out.append(ch)
        else:
            out.append(rep.capitalize() if ch != low and rep else rep)
    return "".join(out)


def to_cyrillic(value: str) -> str:
    out, i, low = [], 0, value.translate(_APOSTROPHES).lower()
    src = value.translate(_APOSTROPHES)
    while i < len(low):
        for lat, cyr in _TO_CYRILLIC:
            if low.startswith(lat, i):
                out.append(cyr.upper() if src[i].isupper() else cyr)
                i += len(lat)
                break
        else:
            out.append(src[i])
            i += 1
    return "".join(out)


def variants(query: str) -> list[str]:
    """A name as said and in the other alphabet: «Ромашка» is searched as «Romashka» too."""
    query = clean(query, 80)
    if not query:
        return []
    has_cyr = any("Ѐ" <= ch <= "ӿ" for ch in query)
    has_lat = any("a" <= ch.lower() <= "z" for ch in query)
    out = [query]
    # speech services join a name: «НаврузСтрой» is searched as «Навруз Строй» too
    split = _JOINED.sub(" ", query)
    if split != query:
        out.append(split)
    for value in list(out):
        if has_cyr:
            out.append(to_latin(value))
        if has_lat:
            out.append(to_cyrillic(value))
    return list(dict.fromkeys(v for v in out if len(v) >= 2))


def fold(value: str) -> str:
    """For comparing names across alphabets and cases."""
    latin = to_latin(unicodedata.normalize("NFKC", value).lower()).replace("q", "k").replace("x", "h")
    return re.sub(r"[^a-z0-9]+", " ", latin.replace("'", "")).strip()
