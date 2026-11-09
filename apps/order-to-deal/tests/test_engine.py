"""The run: document -> lines -> catalog products -> quantities in the item's unit -> prices -> the deal.
The model is the real ClaudeReader over a scripted client."""

from decimal import Decimal
from typing import Any

import anthropic
import httpx
import pytest

from alnuzum_orders import intake
from alnuzum_orders.engine import Result, apply_choices, clean_customer, plan, priced, run
from alnuzum_orders.model import ModelError, Usage
from alnuzum_orders.pricing import Rules, Tier

from .conftest import catalog, extract_lines, item, questions, reader_with

D = Decimal

CAT = catalog(
    item(1, "Кабель NYM 3x2,5", sku="EL-CB-001", price="120", unit="м", category="Кабель", stock="500"),
    item(
        2, "Хомут нейлоновый 3,6x200 белый", sku="EL-KP-001", price="95", unit="уп", category="Крепёж",
        stock="40", packs={"шт": D("0.01"), "кор": D(10)},
    ),
    item(3, "Автоматический выключатель ABB S201 C16", sku="EL-AV-001", price="700", category="Автоматы", stock="2"),
    item(4, "Автоматический выключатель IEK S201 C16", sku="EL-AV-002", price="300", category="Автоматы", stock="100"),
    item(5, "Розетка двойная Schneider Glossa белая", sku="EL-RZ-001", price="400", category="Розетки", stock="30"),
    item(6, "Розетка двойная Schneider Glossa бежевая", sku="EL-RZ-002", price="410", category="Розетки", stock="30"),
    item(7, "Лампа светодиодная A60 12 Вт E27", sku="EL-SV-001", price="110", category="Лампы", stock="300"),
)  # fmt: skip


async def gate(kind: str, worst: Any = None):  # type: ignore[no-untyped-def]
    async def done(used: Usage, ok: bool) -> None:
        return None

    return done


def by_name(result: Result) -> dict[str, Any]:
    return {ln.name: ln for ln in result.lines}


async def test_a_table_with_a_header_needs_no_model() -> None:
    reader, messages = reader_with()  # any request fails the test
    src = intake.from_text(
        "o.csv",
        "Артикул;Наименование;Кол-во;Ед.\n"
        "EL-CB-001;кабель ниим;100;м\n"
        ";Лампа светодиодная A60 12 Вт E27;20 шт;\n"
        ";Х;1;шт\n",
    )
    result = await run(src, CAT, reader, gate)
    assert messages.requests == []
    assert [(ln.choice, ln.by, ln.status, ln.qty_item) for ln in result.lines] == [
        ("1", "article", "ok", "100"),
        ("7", "name", "ok", "20"),
    ]
    assert result.lines[0].src == [2]
    assert result.usage["calls"] == 0
    assert result.catalog == {"currency": "RUB", "count": 7, "has_stock": True, "warning": None}


async def test_units_packs_rounding_quantity_problems_and_short_stock() -> None:
    reader, _ = reader_with(extract_lines)
    src = intake.from_text(
        "mail.txt",
        "Добрый день!\n"
        "Хомут нейлоновый 3,6x200 белый - 250 шт\n"
        "Хомут нейлоновый 3,6x200 белый - 2 кор\n"
        "Хомут нейлоновый 3,6x200 белый - 1 бухта\n"
        "Кабель NYM 3x2,5 - 0 м\n"
        "Автоматический выключатель ABB S201 C16 - 5 шт\n",
    )
    result = await run(src, CAT, reader, gate)
    lines = result.lines
    assert [ln.src for ln in lines] == [[2], [3], [4], [5], [6]]
    # 250 pieces of a product sold by packs of 100: three packs, rounded up and said so
    assert (lines[0].qty_item, lines[0].rounded, lines[0].status) == ("3", True, "ok")
    assert (lines[1].qty_item, lines[1].rounded) == ("20", False)
    # a coil of a product that has none: the person decides
    assert (lines[2].status, lines[2].problem, lines[2].qty_item) == ("disputed", "unit", None)
    assert (lines[3].status, lines[3].problem) == ("disputed", "qty")
    short = lines[4]
    assert (short.status, short.short, short.problem, short.qty_item) == ("ok", True, "stock", "5")
    assert [a.id for a in short.analogs] == ["4"]
    assert result.usage["calls"] == 1 and result.usage["input"] == 1000
    # a person fixes the quantity and takes the analogue
    apply_choices(result, {"q3": "2", "c5": "4", "q4": "abc", "q5": "-1"})
    assert (lines[2].qty_item, lines[2].status, lines[2].problem) == ("2", "ok", None)
    assert (lines[4].choice, lines[4].by, lines[4].short, lines[4].problem) == ("4", "person", False, None)
    assert lines[3].status == "disputed"


async def test_unsure_lines_go_to_the_model_and_below_its_bar_to_a_person() -> None:
    def answer(request: dict[str, Any]) -> dict[str, Any]:
        (k1, c1), (k2, _) = questions(request)
        assert len(c1) >= 2 and all("sold by" in c for c in c1)
        beige = next(i for i, c in enumerate(c1, 1) if "бежевая" in c)
        return {
            "answers": [
                {"line": k1, "choice": beige, "confidence": 0.93},
                {"line": k2, "choice": 1, "confidence": 0.4},
            ]
        }

    reader, messages = reader_with(extract_lines, answer)
    src = intake.from_text("m.txt", "Розетка двойная Glossa беж - 6 шт\nрозетка Schneider Glossa - 2 шт")
    result = await run(src, CAT, reader, gate)
    first, second = result.lines
    assert (first.choice, first.by, first.status, first.confidence) == ("6", "model", "ok", 0.93)
    assert second.status == "disputed" and second.choice is not None
    assert len(messages.requests) == 2
    assert messages.requests[1]["output_config"]["effort"] == "low"
    # the person picks among the line's own options only
    apply_choices(result, {"c2": "5", "c1": "999"})
    assert (second.choice, second.status, second.by) == ("5", "ok", "person")
    assert first.choice == "6"
    apply_choices(result, {"c1": "skip"})
    assert (first.choice, first.status) == (None, "not_found")


async def test_not_in_the_catalog_gets_analogues_and_a_failed_choice_leaves_lines_to_a_person() -> None:
    broken = anthropic.APIConnectionError(
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )
    reader, _ = reader_with(extract_lines, broken, broken)
    src = intake.from_text(
        "m.txt",
        "Стабилизатор напряжения 5 кВт - 1 шт\nРозетка двойная Glossa - 2 шт\nЛампа светодиодная E27 - 3 шт",
    )
    result = await run(src, CAT, reader, gate)
    names = by_name(result)
    assert names["Стабилизатор напряжения 5 кВт"].status == "not_found"
    assert names["Стабилизатор напряжения 5 кВт"].candidates == []
    assert names["Розетка двойная Glossa"].status == "disputed"
    assert names["Розетка двойная Glossa"].choice is None


async def test_model_says_none_with_confidence_means_not_in_the_catalog() -> None:
    def answer(request: dict[str, Any]) -> dict[str, Any]:
        return {"answers": [{"line": k, "choice": None, "confidence": 0.9} for k, _ in questions(request)]}

    reader, _ = reader_with(extract_lines, answer)
    result = await run(intake.from_text("m", "Розетка двойная Glossa - 2 шт"), CAT, reader, gate)
    assert result.lines[0].status == "not_found"


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (anthropic.RateLimitError, "e_model_busy"),
        (anthropic.InternalServerError, "e_model_busy"),
        (anthropic.BadRequestError, "e_model_busy"),
    ],
)
async def test_model_failures_become_a_message_for_the_user(
    error: type[anthropic.APIStatusError], code: str
) -> None:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    status = {"RateLimitError": 429, "InternalServerError": 500, "BadRequestError": 400}[error.__name__]
    exc = error("x", response=httpx.Response(status, request=request), body=None)
    # no answer (429, 5xx) is tried once more; a refused request (400) is not
    reader, messages = reader_with(*([exc] * (1 if status == 400 else 2)))
    with pytest.raises(ModelError) as caught:
        await run(intake.from_text("m", "что-то"), CAT, reader, gate)
    assert caught.value.code == code
    assert messages.script == []


@pytest.mark.parametrize("answer", [("max_tokens", {"items": []}), ("refusal", {}), "not json", "[1, 2]"])
async def test_unreadable_answers(answer: Any) -> None:
    reader, _ = reader_with(answer)
    with pytest.raises(ModelError) as caught:
        await run(intake.from_text("m", "что-то"), CAT, reader, gate)
    assert caught.value.code == "e_model_unreadable"


async def test_a_photo_goes_as_an_image_and_its_transcript_is_the_source() -> None:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "white").save(buf, "PNG")
    src = intake.read("photo.png", buf.getvalue())

    def answer(request: dict[str, Any]) -> dict[str, Any]:
        content = request["messages"][0]["content"]
        assert content[0]["type"] == "image" and content[0]["source"]["media_type"] == "image/jpeg"
        return {
            "items": [
                {
                    "lines": [2],
                    "text": "лампа E27 - 3",
                    "name": "Лампа светодиодная A60 12 Вт E27",
                    "quantity": 3,
                }
            ],
            "customer": {"person": "Алишер"},
            "transcript": ["Заказ", "лампа E27 - 3"],
        }

    reader, _ = reader_with(answer)
    result = await run(src, CAT, reader, gate)
    assert result.source["lines"] == ["Заказ", "лампа E27 - 3"]
    assert result.lines[0].src == [2] and result.lines[0].choice == "7"
    assert result.customer["person"] == "Алишер"

    def pdf_answer(request: dict[str, Any]) -> dict[str, Any]:
        assert [b["type"] for b in request["messages"][0]["content"]] == ["image", "image", "text"]
        return {"items": [], "customer": {}, "transcript": []}

    scan = intake.Source("image", "scan.pdf", [], images=[b"\xff\xd8\xff1", b"\xff\xd8\xff2"], pages=2)
    reader, _ = reader_with(pdf_answer)
    assert (await run(scan, CAT, reader, gate)).lines == []


async def test_plan_merges_lines_prices_by_the_rules_and_leaves_a_task() -> None:
    reader, _ = reader_with(extract_lines)
    src = intake.from_text(
        "z-17.txt",
        "Кабель NYM 3x2,5 - 100 м\nКабель NYM 3x2,5 - 50 м\nЛампа светодиодная A60 12 Вт E27 - 10 шт\n"
        "Стабилизатор напряжения 5 кВт - 1 шт\n",
    )
    result = await run(src, CAT, reader, gate)
    result.customer = {
        "company": "ООО  Ромашка\n",
        "inn": "77-01-234567",
        "person": None,
        "phone": "12",
        "email": "bad",
    }
    rules = Rules(categories={"Кабель": D(5)}, volume=(Tier(D(10000), D(10)),))
    p = priced(result, rules)
    assert p.subtotal == D(126) * 150 + D(110) * 10
    with_prices = plan(result, rules, line_prices=True, lang="ru")
    assert [(x.product_id, x.quantity, x.unit_price) for x in with_prices.lines] == [
        ("1", D(150), D("113.40")),
        ("7", D(10), D("99.00")),
    ]
    assert with_prices.total is None
    assert with_prices.title.startswith("Заявка: ООО Ромашка ·")
    assert "Скидка от суммы 10 %" in with_prices.note and "нет в каталоге" in with_prices.note
    assert with_prices.task == "Проверить 1 поз. заявки «z-17.txt»" and with_prices.left == 1
    at_catalog = plan(result, rules, line_prices=False, lang="en")
    assert [x.unit_price for x in at_catalog.lines] == [D(120), D(110)]
    assert at_catalog.total == p.total
    assert "the deal budget is the total after the rules" in at_catalog.note
    c = clean_customer(result.customer)
    assert (c.company, c.inn, c.phone, c.email) == ("ООО Ромашка", "7701234567", None, None)


# --- I2: the booking is an upper bound of what the call can cost ---


def booking_gate(seen: list[Any]) -> Any:
    async def gate(kind: str, worst: Any = None):  # type: ignore[no-untyped-def]
        seen.append(worst)

        async def done(used: Any, ok: bool) -> None:
            return None

        return done

    return gate


async def test_a_pdf_without_a_text_layer_for_pypdf_goes_as_pictures_booked_by_the_pictures() -> None:
    """Text pypdf does not see (here a long annotation; equally a font without ToUnicode or a hidden
    layer) could make the PDF dear for the model: the model never gets the PDF, only its pages drawn."""
    import io

    from pypdf import PdfWriter
    from pypdf.annotations import FreeText

    from alnuzum_orders.model import (
        EXTRACT_MAX_TOKENS,
        EXTRACT_SCHEMA,
        EXTRACT_SYSTEM,
        IMAGE_TOKENS,
        cost,
        worst_input,
    )

    writer = PdfWriter()
    for _ in range(2):
        writer.add_blank_page(width=595, height=842)
    for n in range(2):
        writer.add_annotation(
            n, FreeText(text="Кабель силовой медный 100 м. " * 2000, rect=(50, 50, 545, 792))
        )
    out = io.BytesIO()
    writer.write(out)
    src = intake.read("scan.pdf", out.getvalue())
    assert (src.kind, src.pages, len(src.images), src.lines) == ("image", 2, 2, [])
    seen: list[Any] = []
    reader, messages = reader_with({"items": [], "customer": {}, "transcript": []})
    await reader.extract(src, booking_gate(seen))
    content = messages.requests[0]["messages"][0]["content"]
    assert [b["type"] for b in content] == ["image", "image", "text"]
    assert seen[0] == cost(worst_input(EXTRACT_SYSTEM, content, EXTRACT_SCHEMA), EXTRACT_MAX_TOKENS)
    text_only = [b for b in content if b["type"] == "text"]
    assert seen[0] == cost(
        worst_input(EXTRACT_SYSTEM, text_only, EXTRACT_SCHEMA) + 2 * IMAGE_TOKENS, EXTRACT_MAX_TOKENS
    )
    with pytest.raises(ValueError, match="no bound"):
        worst_input("", [{"type": "document"}], {})


async def test_a_text_is_booked_by_its_bytes_and_an_oversized_request_is_never_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alnuzum_orders import model
    from alnuzum_orders.model import EXTRACT_MAX_TOKENS, cost

    text = "Кабель силовой медный - 100 м\n" * 300
    seen: list[Any] = []
    reader, _ = reader_with({"items": [], "customer": {}, "transcript": []})
    await reader.extract(intake.from_text("m", text), booking_gate(seen))
    assert seen[0] >= cost(len(text.encode()), EXTRACT_MAX_TOKENS)
    monkeypatch.setattr(model, "MAX_INPUT_BYTES", 1000)
    reader, messages = reader_with({"items": []})
    with pytest.raises(ModelError) as caught:
        await reader.extract(intake.from_text("m", text), booking_gate(seen))
    assert caught.value.code == "e_too_long"
    assert messages.requests == [] and len(seen) == 1
