"""Normalisation of product names and order lines: the deterministic half of matching."""

import re
import unicodedata
from decimal import Decimal

# Cyrillic letters that look like Latin ones: inside a code or a size they mean the Latin letter
# ("С16" typed in Cyrillic is the curve C16, "ВА47" stays as is because В is not a lookalike).
_LOOKALIKE = str.maketrans(
    {"а": "a", "с": "c", "е": "e", "о": "o", "к": "k", "р": "p", "х": "x", "у": "y", "м": "m", "п": "p"}
)
# words compare by a Latin stem, so «Глосса» meets «Glossa» and «макита» meets «Makita»
_TRANSLIT = str.maketrans(
    {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh", "з": "z", "и": "i", "й": "i",
        "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
        "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e",
        "ю": "yu", "я": "ya", "w": "v",
    }
)  # fmt: skip
# a number and its unit written apart are one token: «10 Вт» = «10вт» = «10w»
_NUM_UNIT = re.compile(r"(?<=\d)\s+(?=(?:вт|w|а|a|ма|ma|мм|mm|кв|kv|ка|ka|к|k|м²|мм²)\b)")
_UNIT_SUFFIX = (
    ("мм2", "mm2"),
    ("мм²", "mm2"),
    ("вт", "w"),
    ("ма", "ma"),
    ("мм", "mm"),
    ("ка", "ka"),
    ("кв", "kv"),
)
_QUOTES = re.compile(r"[\"'«»“”„`]")
_TIMES = re.compile(r"(?<=\d)\s*[xх×*]\s*(?=\d)")
_COMMA = re.compile(r"(?<=\d),(?=\d)")
_TOKEN = re.compile(r"[0-9a-zа-я]+(?:[./x-][0-9a-zа-я]+)*")
_WORD_STEM = 5

# words that say nothing about which product it is
STOP = frozenset(
    {
        "и",
        "в",
        "на",
        "для",
        "с",
        "по",
        "из",
        "под",
        "до",
        "от",
        "the",
        "for",
        "of",
        "and",
        "with",
        "a",
        "an",
        "шт",
        "штук",
        "штуки",
        "уп",
        "упак",
        "упаковка",
        "упаковки",
        "кор",
        "коробка",
        "коробки",
        "м",
        "метр",
        "метров",
        "бухта",
        "бухты",
        "pcs",
        "pc",
        "pack",
        "box",
        "уп.",
        "кор.",
        "мм",
        "mm",
        "цвет",
        "нужно",
        "надо",
        "прошу",
        "пожалуйста",
        "please",
        "need",
        "мне",
        "нам",
    }
)


def norm(value: str) -> str:
    """Lower case, ё as е, no quotes, «3х2,5» as «3x2.5», single spaces."""
    value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    value = _QUOTES.sub("", value)
    value = _TIMES.sub("x", value)
    value = _COMMA.sub(".", value)
    value = _NUM_UNIT.sub("", value)
    return re.sub(r"\s+", " ", value).strip()


_ENDINGS = tuple(
    sorted(
        [
            "ая",
            "яя",
            "ое",
            "ее",
            "ые",
            "ие",
            "ый",
            "ий",
            "ой",
            "ей",
            "ую",
            "юю",
            "ого",
            "его",
            "ому",
            "ему",
            "ыми",
            "ими",
            "ых",
            "их",
            "ов",
            "ев",
            "ами",
            "ями",
            "ам",
            "ям",
            "ах",
            "ях",
            "ом",
            "ем",
            "а",
            "я",
            "ы",
            "и",
            "е",
            "у",
            "ю",
            "ь",
            "о",
        ],
        key=len,
        reverse=True,
    )
)


def _stem(word: str) -> str:
    """A light Russian stem: «белая», «белый» and «белые» are one word; Latin words stay whole."""
    if word.isascii():
        return word
    for end in _ENDINGS:
        if word.endswith(end) and len(word) - len(end) >= 3:
            return word[: -len(end)]
    return word


def _token_form(token: str) -> str:
    if any(ch.isdigit() for ch in token):
        for ru, en in _UNIT_SUFFIX:
            if token.endswith(ru) and token[: -len(ru)][-1:].isdigit():
                token = token[: -len(ru)] + en
        # "16а" and "16a" are amperes either way; "с16" typed in Cyrillic is the curve C16
        return token.translate(_LOOKALIKE)
    return _stem(token).translate(_TRANSLIT)[:_WORD_STEM]


def tokens(value: str) -> list[str]:
    """Comparable tokens: sizes and codes whole, Russian words cut to a stem."""
    out: list[str] = []
    for raw in _TOKEN.findall(norm(value)):
        if raw in STOP:
            continue
        out.append(_token_form(raw))
        # "c16" and "16a" also count as the bare number, so "автомат 16А" meets "C16"
        if any(ch.isdigit() for ch in raw):
            number = re.sub(r"[^0-9.x/]", "", raw.translate(_LOOKALIKE))
            if number and number != out[-1]:
                out.append(number)
    return out


def is_spec(token: str) -> bool:
    return any(ch.isdigit() for ch in token)


def sku_key(value: str | None) -> str:
    """An article as it can be compared: no spaces, dashes or dots, upper case, Latin lookalikes."""
    if not value:
        return ""
    value = unicodedata.normalize("NFKC", value).casefold().translate(_LOOKALIKE)
    return re.sub(r"[\s\-./_]", "", value).upper()


UNITS = {
    "шт": "шт",
    "шт.": "шт",
    "штук": "шт",
    "штуки": "шт",
    "штука": "шт",
    "pcs": "шт",
    "pc": "шт",
    "pieces": "шт",
    "each": "шт",
    "units": "шт",
    "unit": "шт",
    "ea": "шт",
    "единиц": "шт",
    "ед": "шт",
    "уп": "уп",
    "уп.": "уп",
    "упак": "уп",
    "упак.": "уп",
    "упаковка": "уп",
    "упаковки": "уп",
    "упаковок": "уп",
    "pack": "уп",
    "packs": "уп",
    "pkg": "уп",
    "пач": "уп",
    "пачка": "уп",
    "пачки": "уп",
    "кор": "кор",
    "кор.": "кор",
    "коробка": "кор",
    "коробки": "кор",
    "коробок": "кор",
    "box": "кор",
    "boxes": "кор",
    "carton": "кор",
    "м": "м",
    "м.": "м",
    "метр": "м",
    "метра": "м",
    "метров": "м",
    "m": "м",
    "meter": "м",
    "meters": "м",
    "metre": "м",
    "metres": "м",
    "пм": "м",
    "п.м": "м",
    "п.м.": "м",
    "мп": "м",
    "бухта": "бухта",
    "бухты": "бухта",
    "бухт": "бухта",
    "бух": "бухта",
    "coil": "бухта",
    "coils": "бухта",
    "reel": "бухта",
    "кг": "кг",
    "kg": "кг",
    "л": "л",
    "л.": "л",
    "l": "л",
    "компл": "компл",
    "комплект": "компл",
    "набор": "компл",
}


def unit_of(value: str | None) -> str | None:
    """Our unit for what a document or a CRM calls it; None when unknown."""
    if not value:
        return None
    key = norm(value).rstrip(".")
    return UNITS.get(key) or UNITS.get(key + ".")


def plain(value: Decimal) -> Decimal:
    """10 stays 10, not 1E+1; 2.500 becomes 2.5."""
    if value == value.to_integral_value():
        return value.quantize(Decimal(1))
    return value.normalize()
