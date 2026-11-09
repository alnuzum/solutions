"""amoCRM and Kommo finish some writes in the background; the adapter waits for them or cleans up after itself."""

import json
from decimal import Decimal
from typing import Any

import httpx
import pytest

from alnuzum_crm import amocrm
from alnuzum_crm.types import DealLine, Leftovers

from .test_narrow_fixes import amo, amo_catalog


def deal_with_widgets(prices: list[int]) -> dict[str, Any]:
    """A deal with 2.5 widgets (100 each); its plain reads give the budget from ``prices`` in turn."""

    def lead(request: httpx.Request) -> dict[str, Any]:
        if request.url.params.get("with"):
            links = [{"id": 42, "metadata": {"quantity": 2.5}}]
            return {"id": 1, "price": 250, "status_id": 1, "_embedded": {"catalog_elements": links}}
        return {"id": 1, "price": prices.pop(0) if len(prices) > 1 else prices[0]}

    return amo_catalog() | {"GET /api/v4/leads/1": lead, "POST /api/v4/leads/1/link": {}}


async def test_add_lines_returns_once_the_budget_is_recounted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kommo recounts the budget after a link in the background: once it landed after the sum the voice of a
    manager set right after the products (680 instead of 4321). add_lines returns when the recount is read."""
    monkeypatch.setattr(amocrm, "RECOUNT_PAUSE", 0)
    crm, router = amo(deal_with_widgets([250, 250, 650]))
    async with crm:
        await crm.add_lines("1", (DealLine("Widget", Decimal(4), Decimal(100), product_id="42"),))
    link = json.loads(router.sent("POST", "/api/v4/leads/1/link")[0].content)
    assert link[0]["metadata"]["quantity"] == 6.5
    reads = [r for r in router.sent("GET", "/api/v4/leads/1") if not r.url.params.get("with")]
    assert len(reads) == 3  # 250, 250, then the recount of 6.5 widgets


async def test_add_lines_does_not_hang_on_a_budget_that_never_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(amocrm, "RECOUNT_PAUSE", 0)
    monkeypatch.setattr(amocrm, "RECOUNT_WAIT", 0)
    crm, router = amo(deal_with_widgets([250]))
    async with crm:
        await crm.add_lines("1", (DealLine("Widget", Decimal(4), Decimal(100), product_id="42"),))
    assert len([r for r in router.sent("GET", "/api/v4/leads/1") if not r.url.params.get("with")]) == 1


async def test_discard_deletes_the_products_of_the_run_through_api_v2() -> None:
    """API v4 answers 405 to a delete of a catalog element; API v2 deletes it, so test runs leave no products."""
    answer: dict[str, Any] = {"_embedded": {"items": [], "errors": []}}
    crm, router = amo({"PATCH /api/v4/leads": {}, "POST /api/v2/catalog_elements": answer})
    async with crm:
        await crm.discard(Leftovers(("5",), (), (), ("42", "43"), ()))
    assert json.loads(router.sent("POST", "/api/v2/catalog_elements")[0].content) == {"delete": [42, 43]}
    assert json.loads(router.sent("PATCH", "/api/v4/leads")[0].content)[0]["status_id"] == amocrm.LOST
