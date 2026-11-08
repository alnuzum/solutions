"""The interface every adapter implements, and the matching rules they share."""

import re
import unicodedata
from abc import ABC, abstractmethod
from decimal import ROUND_HALF_UP, Decimal
from types import TracebackType
from typing import ClassVar, Self

from alnuzum_crm.types import (
    CompanyIn,
    ContactIn,
    CreatedDeal,
    CrmKind,
    Deal,
    DealIn,
    FileIn,
    Leftovers,
    Page,
    Product,
    ProductIn,
    Ref,
    TaskIn,
)

E2E_TAG = "e2e"


class CRM(ABC):
    kind: ClassVar[CrmKind]
    #: what differs between CRMs and matters to a caller:
    #: "inn" — companies can be found by INN; "fractional_amount" — deal amount keeps kopecks;
    #: "delete" — test records are deleted (otherwise closed and tagged ``e2e``).
    capabilities: ClassVar[frozenset[str]] = frozenset()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, et: type[BaseException] | None, e: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.aclose()

    @abstractmethod
    async def aclose(self) -> None: ...

    @abstractmethod
    async def find_company(self, company: CompanyIn) -> str | None:
        """By INN first (where the CRM keeps one), then by exact name ignoring case."""

    @abstractmethod
    async def create_company(self, company: CompanyIn) -> str: ...

    async def find_or_create_company(self, company: CompanyIn) -> Ref:
        found = await self.find_company(company)
        if found is not None:
            return Ref(found, False)
        return await self._create_company_ref(company)

    async def _create_company_ref(self, company: CompanyIn) -> Ref:
        return Ref(await self.create_company(company), True)

    @abstractmethod
    async def find_contact(self, contact: ContactIn) -> str | None:
        """By e-mail ignoring case, then by phone digits."""

    @abstractmethod
    async def create_contact(self, contact: ContactIn, *, company_id: str | None = None) -> str: ...

    async def find_or_create_contact(self, contact: ContactIn, *, company_id: str | None = None) -> Ref:
        found = await self.find_contact(contact)
        if found is not None:
            return Ref(found, False)
        return await self._create_contact_ref(contact, company_id)

    async def _create_contact_ref(self, contact: ContactIn, company_id: str | None) -> Ref:
        return Ref(await self.create_contact(contact, company_id=company_id), True)

    @abstractmethod
    async def create_product(self, product: ProductIn) -> str: ...

    @abstractmethod
    async def create_deal(self, deal: DealIn) -> CreatedDeal: ...

    @abstractmethod
    async def get_deal(self, deal_id: str) -> Deal: ...

    @abstractmethod
    async def attach_file(self, deal_id: str, file: FileIn) -> str: ...

    @abstractmethod
    async def add_note(self, deal_id: str, text: str) -> str: ...

    @abstractmethod
    async def add_task(self, deal_id: str, task: TaskIn) -> str: ...

    @abstractmethod
    async def list_products(
        self, *, query: str | None = None, cursor: str | None = None, limit: int = 50
    ) -> Page[Product]: ...

    @abstractmethod
    async def deal_history(
        self, *, company_id: str | None = None, contact_id: str | None = None, limit: int = 200
    ) -> list[Deal]:
        """Deals of a company or a contact, newest first, without lines."""

    @abstractmethod
    async def discard(self, leftovers: Leftovers) -> None:
        """Remove test records: delete where the CRM allows, otherwise close and tag ``e2e``."""


def same_name(a: str, b: str) -> bool:
    return norm_name(a) == norm_name(b)


def norm_name(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    value = re.sub(r"[\"'«»“”„`]", "", value)
    return re.sub(r"\s+", " ", value).strip()


def digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def same_phone(a: str | None, b: str | None) -> bool:
    """Equal phone numbers whatever the formatting; national prefixes (8…, 0…) compare by the
    last ten digits when both numbers are that long."""
    da, db = digits(a), digits(b)
    if not da or not db:
        return False
    if da == db:
        return True
    return len(da) >= 10 and len(db) >= 10 and da[-10:] == db[-10:]


def same_email(a: str | None, b: str | None) -> bool:
    return bool(a and b and a.strip().casefold() == b.strip().casefold())


def require_one(company_id: str | None, contact_id: str | None, crm: str) -> None:
    if bool(company_id) == bool(contact_id):
        raise ValueError(f"{crm}: pass exactly one of company_id and contact_id")


def dec(value: object) -> Decimal | None:
    if value is None or value == "" or value is False:
        return None
    return Decimal(str(value))


def round_money(value: Decimal, places: str = "0.01") -> Decimal:
    return value.quantize(Decimal(places), rounding=ROUND_HALF_UP)
