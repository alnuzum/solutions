"""Counts with their words: the forms of a Russian and an English word after a number, as the pages write them."""

import pytest

from alnuzum_platform.web.i18n import Text, counted, fill, plural, texts

RU = ("письмо", "письма", "писем")


@pytest.mark.parametrize(
    ("n", "word", "of", "en"),
    [
        (1, "1 письмо", "из 1 письма", "1 letter"),
        (2, "2 письма", "из 2 писем", "2 letters"),
        (5, "5 писем", "из 5 писем", "5 letters"),
        (11, "11 писем", "из 11 писем", "11 letters"),
        (21, "21 письмо", "из 21 письма", "21 letters"),
        (22, "22 письма", "из 22 писем", "22 letters"),
        (25, "25 писем", "из 25 писем", "25 letters"),
        (31, "31 письмо", "из 31 письма", "31 letters"),
        (44, "44 письма", "из 44 писем", "44 letters"),
        (111, "111 писем", "из 111 писем", "111 letters"),
    ],
)
def test_a_count_takes_the_form_of_its_number(n: int, word: str, of: str, en: str) -> None:
    assert fill("{n:письмо|письма|писем}", n=n) == word
    assert fill("из {n:письма|писем|писем}", n=n) == of  # the case is the text's own
    assert fill("{n:letter|letters}", n=n) == en
    assert f"{n} {plural(n, RU)}" == word


def test_the_teens_and_the_rest_of_the_forms() -> None:
    assert [plural(n, RU) for n in (0, 3, 4, 12, 13, 14, 15, 20, 101, 112, 1000)] == [
        "писем", "письма", "письма", "писем", "писем", "писем", "писем", "писем", "письмо", "писем", "писем",
    ]  # fmt: skip
    assert fill("{n:правка|правки|правок} оформления", n=3) == "3 правки оформления"
    assert (
        fill("длиннее {n:знака|знаков|знаков}", n="5 000") == "длиннее 5 000 знаков"
    )  # written with a space
    assert fill("{a} из {b}, {c:.1f}", a=1, b=2, c=0.25) == "1 из 2, 0.2"  # the rest is str.format's


def test_a_count_that_is_no_number_is_refused() -> None:
    with pytest.raises(ValueError, match="a count is not a number"):
        fill("{n:письмо|письма|писем}", n="")
    with pytest.raises(ValueError, match="a count is not a number"):
        fill("{n:письмо|письма|писем}", n=True)


def test_the_texts_of_a_page_write_counts() -> None:
    t = counted({"q": "Не больше {quota:записи|записей|записей} в CRM"})
    assert isinstance(t["q"], Text) and t["q"].format(quota=21) == "Не больше 21 записи в CRM"
    assert texts("ru")["why_quota"].format(quota=20) == (
        "Суточная квота исчерпана: не больше 20 записей в CRM за 24 часа."
    )
    assert texts("en")["why_quota"].format(quota=1).endswith("at most 1 write into the CRM per 24 hours.")
