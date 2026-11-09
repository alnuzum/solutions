"""Data passed to and returned by every CRM adapter."""

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum


class CrmKind(StrEnum):
    AMOCRM = "amocrm"
    KOMMO = "kommo"
    BITRIX24 = "bitrix24"
    ZOHO = "zoho"
    ODOO = "odoo"


class DealStatus(StrEnum):
    OPEN = "open"
    WON = "won"
    LOST = "lost"


@dataclass(frozen=True, slots=True)
class CompanyIn:
    name: str
    inn: str | None = None
    phone: str | None = None
    email: str | None = None


@dataclass(frozen=True, slots=True)
class ContactIn:
    name: str
    email: str | None = None
    phone: str | None = None


@dataclass(frozen=True, slots=True)
class Ref:
    """A record found or created; ``created`` is False when an existing record was reused."""

    id: str
    created: bool


@dataclass(frozen=True, slots=True)
class AccountInfo:
    """The CRM account behind a set of credentials: ``id`` is stable for the account, ``web`` is the
    address its pages live under (deal links are built from it)."""

    id: str
    name: str
    web: str


@dataclass(frozen=True, slots=True)
class DealLine:
    name: str
    quantity: Decimal
    unit_price: Decimal
    product_id: str | None = None
    sku: str | None = None

    @property
    def total(self) -> Decimal:
        return self.quantity * self.unit_price


@dataclass(frozen=True, slots=True)
class DealIn:
    title: str
    lines: tuple[DealLine, ...] = ()
    company_id: str | None = None
    contact_id: str | None = None
    currency: str | None = None
    tags: tuple[str, ...] = ()
    #: the deal sum when it differs from the sum of the lines: a CRM whose lines cannot keep their own
    #: price (amoCRM, Kommo) gets the lines at the catalog price and the discounted sum here
    total: Decimal | None = None

    @property
    def amount(self) -> Decimal:
        if self.total is not None:
            return self.total
        return sum((line.total for line in self.lines), Decimal(0))


@dataclass(frozen=True, slots=True)
class CreatedDeal:
    """A new deal and the catalog products the adapter had to create for its lines."""

    id: str
    product_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Deal:
    id: str
    title: str
    amount: Decimal | None
    currency: str | None
    status: DealStatus
    created_at: datetime | None = None
    closed_at: datetime | None = None
    lines: tuple[DealLine, ...] = ()
    #: the id of its stage, as ``CRM.stages`` names it
    stage_id: str | None = None


@dataclass(frozen=True, slots=True)
class FileIn:
    name: str
    content: bytes
    mime_type: str = "application/octet-stream"


@dataclass(frozen=True, slots=True)
class TaskIn:
    text: str
    due: datetime
    responsible_id: str | None = None


@dataclass(frozen=True, slots=True)
class ProductIn:
    """``unit`` is one of шт, м, уп, кор, кг, л (mapped to the CRM's own units where it has them);
    ``category`` goes to the CRM's product sections where the adapter can create them; ``stock`` only
    where the CRM keeps stock without warehouse documents."""

    name: str
    price: Decimal
    sku: str | None = None
    unit: str | None = None
    category: str | None = None
    description: str | None = None
    stock: Decimal | None = None


@dataclass(frozen=True, slots=True)
class Product:
    """``unit`` and ``category`` as the CRM names them; ``stock`` is None where the CRM keeps none."""

    id: str
    name: str
    sku: str | None
    price: Decimal | None
    currency: str | None
    unit: str | None = None
    category: str | None = None
    description: str | None = None
    stock: Decimal | None = None


@dataclass(frozen=True, slots=True)
class Page[T]:
    items: tuple[T, ...]
    next_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class Leftovers:
    """Records to remove after a test run; adapters delete or close them as the CRM allows."""

    deal_ids: tuple[str, ...] = ()
    contact_ids: tuple[str, ...] = ()
    company_ids: tuple[str, ...] = ()
    product_ids: tuple[str, ...] = ()
    task_ids: tuple[str, ...] = field(default=())


@dataclass(frozen=True, slots=True)
class Stage:
    """A stage of a deal pipeline; ``id`` is what ``DealUpdate.stage_id`` takes, ``pipeline`` names the
    pipeline it belongs to where the CRM has several (a deal moves only within its own)."""

    id: str
    name: str
    kind: DealStatus
    pipeline: str | None = None


@dataclass(frozen=True, slots=True)
class DealHit:
    """A deal found by ``CRM.find_deals``: enough for a person to tell it from the others."""

    id: str
    title: str
    company: str | None
    contact: str | None
    stage_id: str | None
    amount: Decimal | None
    status: DealStatus
    #: how it was found: "title", "company" or "contact"
    by: str = "title"


@dataclass(frozen=True, slots=True)
class DealUpdate:
    """Changes of a deal's own fields; None leaves a field as it is. There is no way to clear one."""

    stage_id: str | None = None
    amount: Decimal | None = None
    close_date: date | None = None

    @property
    def empty(self) -> bool:
        return self.stage_id is None and self.amount is None and self.close_date is None
