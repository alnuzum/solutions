"""What the voice methods refuse before any request: there is no way to delete, zero a sum or bring a
product the catalog does not have."""

from datetime import date
from decimal import Decimal

import httpx
import pytest

from alnuzum_crm.amocrm import AmoCRM
from alnuzum_crm.base import CRM, search_text
from alnuzum_crm.bitrix24 import Bitrix24
from alnuzum_crm.errors import CrmUnsupported, CrmValidationError
from alnuzum_crm.odoo import Odoo
from alnuzum_crm.tokens import StaticToken
from alnuzum_crm.types import DealLine, DealUpdate
from alnuzum_crm.zoho import ZohoCRM


def _never(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"no request expected, got {request.method} {request.url}")


def _all(transport: httpx.AsyncBaseTransport) -> list[CRM]:
    return [
        AmoCRM("example.amocrm.ru", StaticToken("T", crm="amocrm"), rate=1000, transport=transport),
        Bitrix24("https://example.bitrix24.ru/rest/1/WEBHOOKTOKEN/", rate=1000, transport=transport),
        ZohoCRM("https://www.zohoapis.com", StaticToken("T", crm="zoho"), rate=1000, transport=transport),
        Odoo("https://odoo.example.com", "db", "u", "k", trusted_url=True, rate=1000, transport=transport),
    ]


@pytest.mark.parametrize("amount", [Decimal(0), Decimal(-5)])
async def test_a_sum_of_zero_or_less_is_refused_before_any_request(amount: Decimal) -> None:
    for crm in _all(httpx.MockTransport(_never)):
        async with crm:
            with pytest.raises(CrmValidationError):
                await crm.update_deal("10", DealUpdate(amount=amount))


async def test_a_line_outside_the_catalog_is_refused_before_any_request() -> None:
    for crm in _all(httpx.MockTransport(_never)):
        async with crm:
            with pytest.raises(CrmValidationError):
                await crm.add_lines("10", (DealLine("new thing", Decimal(1), Decimal(5)),))
            with pytest.raises(CrmValidationError):
                await crm.add_lines("10", (DealLine("zero", Decimal(0), Decimal(5), product_id="7"),))


async def test_amocrm_has_no_closing_date_and_says_so_before_writing() -> None:
    async with AmoCRM(
        "example.amocrm.ru", StaticToken("T", crm="amocrm"), transport=httpx.MockTransport(_never)
    ) as crm:
        assert "close_date" not in crm.capabilities
        with pytest.raises(CrmUnsupported):
            await crm.update_deal("10", DealUpdate(stage_id="1:2", close_date=date(2026, 12, 1)))


async def test_an_amocrm_stage_id_cannot_leave_its_path() -> None:
    async with AmoCRM(
        "example.amocrm.ru", StaticToken("T", crm="amocrm"), transport=httpx.MockTransport(_never)
    ) as crm:
        with pytest.raises(ValueError, match="digits only"):
            await crm.update_deal("10", DealUpdate(stage_id="1:../../x"))


def test_a_search_carries_no_control_characters_and_is_short() -> None:
    assert search_text("Ромашка‮\x00\n  ООО" + "x" * 200) == ("Ромашка ООО" + "x" * 200)[:80]
