"""Duplicate search in Odoo never treats % _ \\ of a name or e-mail as a pattern."""

import pytest

from alnuzum_crm.odoo import Odoo
from alnuzum_crm.types import CompanyIn, ContactIn

from .odoo_fake import FakeOdoo


def _odoo(fake: FakeOdoo) -> Odoo:
    return Odoo(
        "https://odoo.example.com", "db", "u", "k", trusted_url=True, rate=1000, transport=fake.transport()
    )


PARTNERS = {
    "res.partner": [
        {"id": 5, "name": "Acme Co", "is_company": True, "vat": False, "email": False, "phone": False},
        {"id": 6, "name": "Jane", "is_company": False, "email": "jane.doe@corp.com", "phone": False},
    ]
}


@pytest.mark.parametrize("name", ["Acme_Co", "Acme%", "%", "_cme Co", "Acme\\ Co"])
async def test_company_name_with_wildcards_matches_nothing_else(name: str) -> None:
    async with _odoo(FakeOdoo(PARTNERS)) as crm:
        assert await crm.find_company(CompanyIn(name)) is None


async def test_company_found_by_exact_name_ignoring_case() -> None:
    async with _odoo(FakeOdoo(PARTNERS)) as crm:
        assert await crm.find_company(CompanyIn("ACME CO")) == "5"


@pytest.mark.parametrize("email", ["jane_doe@corp.com", "%@corp.com", "jane.doe@corp.co_", "%"])
async def test_contact_email_with_wildcards_matches_nothing_else(email: str) -> None:
    async with _odoo(FakeOdoo(PARTNERS)) as crm:
        assert await crm.find_contact(ContactIn("x", email=email)) is None


async def test_contact_found_by_email_ignoring_case() -> None:
    async with _odoo(FakeOdoo(PARTNERS)) as crm:
        assert await crm.find_contact(ContactIn("x", email="Jane.Doe@CORP.com")) == "6"


async def test_wildcards_are_escaped_in_the_query() -> None:
    fake = FakeOdoo(PARTNERS)
    async with _odoo(fake) as crm:
        await crm.find_company(CompanyIn("Acme_Co 100%"))
        await crm.find_contact(ContactIn("x", email="a_b@c.com"))
    sent = [
        term[2]
        for _model, _method, args in fake.calls
        for term in args[0]
        if isinstance(term, list) and term[1] == "=ilike"
    ]
    assert sent == ["Acme\\_Co 100\\%", "a\\_b@c.com"]
