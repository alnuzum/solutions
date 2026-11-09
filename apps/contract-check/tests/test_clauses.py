"""Pairing and comparing without a model: every difference points at real fragments, every quote is a slice
of the document, cosmetics are not changes."""

import random
import time

import pytest
from alnuzum_contracts.clauses import Clause, compare, hunks, normal, number_of, split, window
from alnuzum_contracts.intake import Block

STANDARD = [
    "ДОГОВОР ПОСТАВКИ № 15",
    "1. ПРЕДМЕТ ДОГОВОРА",
    "1.1. Поставщик обязуется передать в собственность Покупателя товар, а Покупатель обязуется принять и оплатить его.",
    "1.2. Наименование, количество и цена товара указываются в спецификациях.",
    "2. ЦЕНА И ПОРЯДОК РАСЧЁТОВ",
    "2.1. Покупатель оплачивает товар в течение 30 (тридцати) рабочих дней с даты поставки.",
    "2.2. Обязательство по оплате считается исполненным с даты зачисления денег на счёт Поставщика.",
    "3. ОТВЕТСТВЕННОСТЬ СТОРОН",
    "3.1. За просрочку оплаты Покупатель уплачивает неустойку 0,1 % от неоплаченной суммы за каждый день просрочки.",
    "3.2. Общий размер ответственности Поставщика не превышает 10 % от цены договора.",
    "4. РАЗРЕШЕНИЕ СПОРОВ",
    "4.1. Споры рассматриваются в Ташкентском межрайонном экономическом суде.",
]


def clauses(lines: list[str]) -> list[Clause]:
    return split([Block("", "", t, True) for t in lines])


def quotes_are_slices(cmp: object) -> None:
    for c in cmp.changes:  # type: ignore[attr-defined]
        if c.a is not None:
            text = cmp.ours[c.a].text  # type: ignore[attr-defined]
            for s, e in [*c.a_spans, *([c.qa] if c.qa else [])]:
                assert 0 <= s <= e <= len(text)
        if c.b is not None:
            text = cmp.theirs[c.b].text  # type: ignore[attr-defined]
            for s, e in [*c.b_spans, *([c.qb] if c.qb else [])]:
                assert 0 <= s <= e <= len(text)


def test_the_same_draft_has_no_changes() -> None:
    cmp = compare(clauses(STANDARD), clauses(STANDARD))
    assert cmp.changes == [] and cmp.same == len(STANDARD)


def test_thirty_working_days_become_sixty_calendar_days() -> None:
    theirs = list(STANDARD)
    theirs[5] = (
        "2.1. Покупатель оплачивает товар в течение 60 (шестидесяти) календарных дней с даты поставки."
    )
    cmp = compare(clauses(STANDARD), clauses(theirs))
    assert len(cmp.changes) == 1
    c = cmp.changes[0]
    a, b = cmp.ours[c.a].text, cmp.theirs[c.b].text  # type: ignore[index]
    assert [a[s:e] for s, e in c.a_spans] == ["30 (тридцати) рабочих"]
    assert [b[s:e] for s, e in c.b_spans] == ["60 (шестидесяти) календарных"]
    assert c.qa and c.qb and "30 (тридцати) рабочих дней" in a[c.qa[0] : c.qa[1]]
    assert "60 (шестидесяти) календарных дней" in b[c.qb[0] : c.qb[1]]
    assert cmp.theirs[c.b].key == "2.1"  # type: ignore[index]


def test_a_new_clause_renumbers_the_rest_and_only_it_is_a_change() -> None:
    theirs = list(STANDARD)
    theirs.insert(9, "3.2. Покупатель возмещает Поставщику все убытки сверх неустойки.")
    theirs[10] = theirs[10].replace("3.2.", "3.3.")
    cmp = compare(clauses(STANDARD), clauses(theirs))
    assert [(c.kind, cmp.theirs[c.b].key) for c in cmp.changes] == [("added", "3.2")]  # type: ignore[index]
    assert cmp.cosmetic == 1  # the clause that is now 3.3


def test_a_deleted_a_moved_and_a_cosmetic_clause() -> None:
    theirs = list(STANDARD)
    theirs.pop(9)  # the cap of liability goes
    theirs[3] = "1.2. НАИМЕНОВАНИЕ,  количество и цена товара указываются в «спецификациях»"  # cosmetics
    moved = theirs.pop(6)
    theirs.insert(2, moved)
    cmp = compare(clauses(STANDARD), clauses(theirs))
    kinds = sorted(c.kind for c in cmp.changes)
    assert kinds == ["deleted", "moved"]
    gone = next(c for c in cmp.changes if c.kind == "deleted")
    assert cmp.ours[gone.a].key == "3.2" and gone.b is None  # type: ignore[index]
    assert cmp.cosmetic == 1
    quotes_are_slices(cmp)


def test_normal_form_and_numbers() -> None:
    assert normal("5.2.  Срок — «30 дней» ;") == normal('срок - "30 дней"')
    assert normal("Ёлка") == "елка"
    assert (
        number_of("5.2. Срок") == "5.2" and number_of("5) Срок") == "5" and number_of("Статья 7. Срок") == "7"
    )
    assert number_of("30 (тридцати) дней") == "" and number_of("12.5") == "12.5"


def test_lines_of_a_pdf_are_joined_into_clauses() -> None:
    lines = [
        "ДОГОВОР ПОСТАВКИ № 15",
        "г. Ташкент 1 ноября 2026 г.",
        "",
        "1.1. Поставщик обязуется передать в собственность Покупателя товар, а Покупатель обязу-",
        "ется принять и оплатить его в порядке и сроки, установленные настоящим договором.",
        "1.2. Наименование, количество и цена товара указываются в спецификациях, которые",
        "являются неотъемлемой частью договора, а товар оплачивается в течение не более чем",
        "30 (тридцати) дней с даты поставки.",
        "Подписи сторон",
    ]
    got = split([Block("", "", t, False) for t in lines])
    assert [c.key for c in got] == ["", "", "1.1", "1.2", ""]
    assert "Покупатель обязу-ется принять" in got[2].text  # a hyphen at a line's end is kept: «CRM-системы»
    assert got[3].text.endswith("не более чем 30 (тридцати) дней с даты поставки.")


def test_punctuation_alone_is_no_fragment_and_a_quote_is_bounded() -> None:
    assert hunks("Срок поставки 10 дней.", "Срок поставки 10 дней;") == []
    long = "слово " * 200 + "ИЗМЕНЕНО " + "слово " * 200
    s, e = window(long, [(1200, 1208)])
    assert e - s <= 160 and long[1200:1208] in long[s:e]


def test_every_span_and_quote_lies_inside_its_clause_on_random_edits() -> None:
    rng = random.Random(7)
    words = [
        "поставщик",
        "покупатель",
        "товар",
        "оплата",
        "срок",
        "дней",
        "неустойка",
        "суд",
        "договор",
        "цена",
        "счёт",
        "акт",
    ]
    for _ in range(60):
        theirs = list(STANDARD)
        for _ in range(rng.randint(1, 4)):
            k = rng.randrange(1, len(theirs))
            parts = theirs[k].split()
            op = rng.choice(["swap", "drop", "add", "del", "ins"])
            if op == "swap" and len(parts) > 2:
                parts[rng.randrange(1, len(parts))] = rng.choice(words)
            elif op == "drop" and len(parts) > 3:
                parts.pop(rng.randrange(1, len(parts)))
            elif op == "add":
                parts.insert(rng.randrange(1, len(parts) + 1), rng.choice(words))
            theirs[k] = " ".join(parts)
            if op == "del" and len(theirs) > 3:
                theirs.pop(k)
            elif op == "ins":
                theirs.insert(k, "9.9. " + " ".join(rng.choice(words) for _ in range(8)))
        quotes_are_slices(compare(clauses(STANDARD), clauses(theirs)))


def test_forty_pages_compare_in_time() -> None:
    big = [
        f"{s}.{n}. " + " ".join(f"слово{(s * 31 + n * 7 + k) % 97}" for k in range(25))
        for s in range(1, 21)
        for n in range(1, 31)
    ]
    theirs = list(big)
    for k in range(0, 600, 37):
        theirs[k] = theirs[k].replace("слово1", "иное")
    theirs.insert(300, "15.99. совсем новый пункт о подсудности и неустойке")
    began = time.monotonic()
    cmp = compare(clauses(big), clauses(theirs))
    assert time.monotonic() - began < 3.0
    assert any(c.kind == "added" for c in cmp.changes)


def test_a_long_walk_word_by_word_is_cut_to_the_whole_clause() -> None:
    """Two clauses of 2 400 equal words but the last: the walk would be n × m; it is one fragment at once."""
    import time

    from alnuzum_contracts.clauses import MAX_WORD_PAIRS, hunks

    a, b = "слово " * 2400 + "конец", "слово " * 2400 + "иначе"
    began = time.monotonic()
    found = hunks(a, b)
    assert time.monotonic() - began < 1.0
    assert len(found) == 1 and found[0].a == (0, len(a)) and found[0].b == (0, len(b))
    assert MAX_WORD_PAIRS < 2401 * 2401


def test_over_800_clauses_or_a_clause_over_5000_characters_is_refused() -> None:
    from alnuzum_contracts.clauses import TooManyClauses
    from alnuzum_contracts.intake import Block

    from alnuzum_files import FileError

    with pytest.raises(TooManyClauses) as many:
        split([Block("", "", f"{n}. Пункт {n}.", True) for n in range(1, 802)])
    assert many.value.n == 801 and many.value.code == "e_clauses"
    with pytest.raises(FileError) as long:
        split([Block("", "", "1. " + "а" * 5001, True)])
    assert long.value.code == "e_clause_long"
