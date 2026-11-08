"""Data passed to and returned by every CRM adapter."""

from dataclasses import dataclass, field
from datetime import datetime
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

    @property
    def amount(self) -> Decimal:
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
    name: str
    price: Decimal
    sku: str | None = None


@dataclass(frozen=True, slots=True)
class Product:
    id: str
    name: str
    sku: str | None
    price: Decimal | None
    currency: str | None


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
