"""The interface every adapter implements, and the matching rules they share."""

import re
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import replace
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from types import TracebackType
from typing import ClassVar, Self

from alnuzum_crm.errors import CrmUnsupported, CrmValidationError
from alnuzum_crm.types import (
    AccountInfo,
    ClientTask,
    CompanyHit,
    CompanyIn,
    ContactIn,
    CreatedDeal,
    CrmKind,
    Deal,
    DealHit,
    DealIn,
    DealLine,
    DealStatus,
    DealUpdate,
    FileIn,
    Leftovers,
    Page,
    Product,
    ProductIn,
    Purchase,
    Ref,
    Stage,
    TaskIn,
    User,
)

E2E_TAG = "e2e"


class CRM(ABC):
    kind: ClassVar[CrmKind]
    #: what differs between CRMs and matters to a caller:
    #: "inn" — companies can be found by INN; "fractional_amount" — deal amount keeps kopecks;
    #: "delete" — test records are deleted (otherwise closed and tagged ``e2e``);
    #: "line_prices" — a deal line keeps its own price (otherwise the catalog price, see ``DealIn.total``);
    #: "stock" — products carry the quantity in stock;
    #: "close_date" — a deal keeps a planned closing date (``DealUpdate.close_date``).
    capabilities: ClassVar[frozenset[str]] = frozenset()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, et: type[BaseException] | None, e: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.aclose()

    @abstractmethod
    async def aclose(self) -> None: ...

    async def account(self) -> AccountInfo:
        """The account the credentials reach. One cheap call: a refused token or key raises
        ``CrmAuthError``, which is how a connection removed on the CRM side is noticed."""
        raise CrmUnsupported("account lookup", crm=self.kind.value)

    def deal_link(self, account: AccountInfo, deal_id: str) -> str:
        """Address of the deal's page in the CRM."""
        raise CrmUnsupported("deal links", crm=self.kind.value)

    @abstractmethod
    async def find_company(self, company: CompanyIn) -> str | None:
        """By INN first (where the CRM keeps one), then by exact name ignoring case."""

    @abstractmethod
    async def create_company(self, company: CompanyIn) -> str: ...

    async def find_companies(
        self,
        *,
        inn: str | None = None,
        domain: str | None = None,
        email: str | None = None,
        phone: str | None = None,
        name: str | None = None,
        limit: int = 20,
    ) -> list[CompanyHit]:
        """Companies that may be the one behind a letter: every key the CRM can search is asked (an INN, a
        domain of the site or of the mail, an e-mail, a phone, a word of the name), and each hit comes with
        what it keeps, so the caller compares the values instead of trusting how the CRM searched."""
        raise CrmUnsupported("company lookup by requisites", crm=self.kind.value)

    async def users(self) -> list[User]:
        """Active users who can lead a deal or a task."""
        raise CrmUnsupported("users", crm=self.kind.value)

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
    async def link_contact(self, contact_id: str, company_id: str) -> bool:
        """Link the contact to the company if it has none; True when a link was made."""

    @abstractmethod
    async def create_contact(self, contact: ContactIn, *, company_id: str | None = None) -> str: ...

    async def find_or_create_contact(self, contact: ContactIn, *, company_id: str | None = None) -> Ref:
        """A found contact without a company is linked to ``company_id``; one that already
        belongs to a company keeps it."""
        found = await self.find_contact(contact)
        if found is not None:
            if company_id:
                await self.link_contact(found, company_id)
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
    async def find_deals(self, query: str, *, limit: int = 8) -> list[DealHit]:
        """Deals whose title, company or contact holds ``query`` (ignoring case): title hits first."""

    @abstractmethod
    async def stages(self) -> list[Stage]:
        """Every stage of every deal pipeline, in pipeline order."""

    @abstractmethod
    async def update_deal(self, deal_id: str, update: DealUpdate) -> None:
        """Stage, amount and planned closing date; a field the CRM has no place for is refused
        (``CrmUnsupported``) before anything is written."""

    @abstractmethod
    async def add_lines(self, deal_id: str, lines: tuple[DealLine, ...]) -> None:
        """Catalog products added to a deal that already exists; the lines it has stay. Only lines with
        a ``product_id`` (``CrmValidationError`` otherwise): nothing new reaches the catalog this way."""

    @abstractmethod
    async def discard(self, leftovers: Leftovers) -> None:
        """Remove test records: delete where the CRM allows, otherwise close and tag ``e2e``."""

    # --- win-back: the purchase history of a client and a task with the reason on the client ---

    async def purchases(
        self,
        *,
        company_id: str | None = None,
        contact_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        lines: bool = True,
        cursor: str | None = None,
        limit: int = 100,
    ) -> Page[Purchase]:
        """Won deals closed in ``[since, until)``. Of a company or a contact: the newest ``limit`` at once,
        newest first, no next page. With neither: the whole account page by page (``limit`` a page) in the
        order the CRM keeps. ``lines=False`` leaves the products out (a scan of the account needs dates and
        sums only; Zoho reads the products of a deal with two requests)."""
        raise CrmUnsupported("purchase history", crm=self.kind.value)

    async def create_won_deal(self, deal: DealIn, closed_at: datetime) -> CreatedDeal:
        """A deal won from the start and closed at ``closed_at`` (demo data, tests): created in the first won
        stage of the first pipeline, then its closing date set."""
        stages = await self.stages()
        first = stages[0].pipeline if stages else None
        won = next((s for s in stages if s.kind is DealStatus.WON and s.pipeline == first), None)
        if won is None:
            raise CrmUnsupported("no won stage of deals", crm=self.kind.value)
        made = await self.create_deal(replace(deal, stage_id=won.id))
        await self.update_deal(made.id, DealUpdate(close_date=closed_at.date()))
        return made

    async def add_client_task(
        self, task: TaskIn, *, company_id: str | None = None, contact_id: str | None = None
    ) -> str:
        """A task on a company or a contact itself, not on one of its deals."""
        raise CrmUnsupported("tasks on a client", crm=self.kind.value)

    async def client_tasks(
        self, *, company_id: str | None = None, contact_id: str | None = None
    ) -> list[ClientTask]:
        """Open tasks on a company or a contact itself."""
        raise CrmUnsupported("tasks on a client", crm=self.kind.value)

    async def discard_task(self, task_id: str) -> None:
        """Delete a task where the CRM allows, otherwise complete it; a missing one is no error."""
        raise CrmUnsupported("tasks on a client", crm=self.kind.value)


def same_name(a: str, b: str) -> bool:
    return norm_name(a) == norm_name(b)


def norm_name(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    value = re.sub(r"[\"'«»“”„`]", "", value)
    return re.sub(r"\s+", " ", value).strip()


def host(value: str | None) -> str:
    """The host of a site as people write it (``https://www.Example.uz/contacts`` → ``example.uz``)."""
    text = (value or "").strip().casefold()
    text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", text)
    text = re.split(r"[/?#:\s]", text, maxsplit=1)[0]
    return text.removeprefix("www.").strip(".")


def mail_domain(value: str | None) -> str:
    """The domain of an e-mail address, lowercase; empty for anything that is not one."""
    _, at, domain = (value or "").strip().casefold().rpartition("@")
    return domain.strip(".") if at and "." in domain else ""


# legal forms in front of or after a company's name (Russian, Uzbek, Kazakh, English and a few more), dropped
# when names are compared: «ООО «Ромашка»», «Romashka MChJ» and «Ромашка» are one company
LEGAL_FORMS = frozenset(
    {
        "ооо",
        "оао",
        "зао",
        "пао",
        "ао",
        "нао",
        "ип",
        "тоо",
        "чп",
        "чтуп",
        "одо",
        "уп",
        "гуп",
        "муп",
        "нко",
        "ано",
        "мчж",
        "хк",
        "аж",
        "қк",
        "оаж",
        "ятт",
        "хф",
        "дук",
        "ук",
        "mchj",
        "xk",
        "aj",
        "qk",
        "oaj",
        "yatt",
        "xf",
        "duk",
        "llc",
        "l.l.c.",
        "ltd",
        "ltd.",
        "limited",
        "inc",
        "inc.",
        "corp",
        "corp.",
        "corporation",
        "co",
        "co.",
        "company",
        "gmbh",
        "ag",
        "sa",
        "s.a.",
        "bv",
        "plc",
        "llp",
        "jsc",
        "fze",
        "fzco",
        "fz-llc",
    }
)
_LEGAL_PHRASES = re.compile(
    r"общество с ограниченной ответственностью|(?:публичное |закрытое |открытое )?акционерное общество|"
    r"индивидуальный предприниматель|mas.uliyati cheklangan jamiyati?|масъулияти чекланган жамияти?|"
    r"limited liability company",
    re.IGNORECASE,
)
_QUOTES_PUNCT = re.compile(r"[\"'«»“”„`‘’,;()]")


def core_name(value: str | None) -> str:
    """A company's name without its legal form, quotes and punctuation, as written (for a search)."""
    text = _QUOTES_PUNCT.sub(" ", _LEGAL_PHRASES.sub(" ", unicodedata.normalize("NFKC", value or "")))
    words = [
        w
        for w in text.split()
        if w.casefold().strip(".") not in LEGAL_FORMS and w.casefold() not in LEGAL_FORMS
    ]
    return " ".join(words).strip(" .-")


def same_company_name(a: str | None, b: str | None) -> bool:
    """Equal names once legal forms, quotes, case and «ё» are set aside."""
    ca, cb = core_name(a).casefold().replace("ё", "е"), core_name(b).casefold().replace("ё", "е")
    return bool(ca) and ca == cb


_HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+")
_EMAIL = re.compile(r"[^@\s<>\"]{1,64}@[a-z0-9.-]{3,253}")


def lookup_keys(
    *,
    inn: str | None = None,
    domain: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    name: str | None = None,
) -> dict[str, str]:
    """The keys a CRM is asked for, each cleaned or left out: an INN of 9 to 14 digits, a host name, an
    e-mail address, a phone of at least 7 digits, a name without its legal form (two letters at least)."""
    keys: dict[str, str] = {}
    if 9 <= len(digits(inn)) <= 14:
        keys["inn"] = digits(inn)
    site = host(domain)
    if _HOST.fullmatch(site) and len(site) <= 253:
        keys["domain"] = site
    address = (email or "").strip().casefold()
    if _EMAIL.fullmatch(address):
        keys["email"] = address
    if 7 <= len(digits(phone)) <= 15:
        keys["phone"] = digits(phone)
    core = search_text(core_name(name))
    if len(core) >= 2:
        keys["name"] = core
    return keys


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


def at_most_one(company_id: str | None, contact_id: str | None, crm: str) -> None:
    if company_id and contact_id:
        raise ValueError(f"{crm}: pass company_id or contact_id, not both")


def in_period(moment: datetime | None, since: datetime | None, until: datetime | None) -> bool:
    """``since <= moment < until``; a missing bound is open, a moment without a zone is UTC."""
    if moment is None:
        return False
    moment = aware(moment)
    return (since is None or moment >= aware(since)) and (until is None or moment < aware(until))


def aware(moment: datetime) -> datetime:
    """A moment without a zone is taken as UTC."""
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def require_one(company_id: str | None, contact_id: str | None, crm: str) -> None:
    if bool(company_id) == bool(contact_id):
        raise ValueError(f"{crm}: pass exactly one of company_id and contact_id")


_DIGITS = re.compile(r"[0-9]{1,19}")


def record_id(value: str | None, crm: str) -> str:
    """A CRM record id that may go into a URL path: ASCII digits only, so it cannot leave its path
    segment (``1/../../settings`` is refused before any request)."""
    if value is None or not _DIGITS.fullmatch(value):
        raise ValueError(f"{crm}: a record id is digits only, got {value!r}")
    return value


def dec(value: object) -> Decimal | None:
    if value is None or value == "" or value is False:
        return None
    return Decimal(str(value))


def round_money(value: Decimal, places: str = "0.01") -> Decimal:
    return value.quantize(Decimal(places), rounding=ROUND_HALF_UP)


def hits_in_order(groups: list[list[DealHit]], limit: int) -> list[DealHit]:
    """Title hits first, then company, then contact; a deal once."""
    seen: set[str] = set()
    out: list[DealHit] = []
    for group in groups:
        for hit in group:
            if hit.id not in seen:
                seen.add(hit.id)
                out.append(hit)
    return out[:limit]


def catalog_lines(lines: tuple[DealLine, ...], crm: str) -> None:
    """Lines for ``add_lines``: each a catalog product with a positive quantity, or a refusal."""
    for line in lines:
        if not line.product_id or line.quantity <= 0:
            raise CrmValidationError(
                f"line {line.name!r}: only catalog products with a positive quantity are added", crm=crm
            )
        record_id(line.product_id, crm)


def search_text(query: str) -> str:
    """What goes to a CRM search: no control characters, at most 80 characters."""
    cleaned = "".join(ch if ch.isprintable() else " " for ch in query)
    return re.sub(r"\s+", " ", cleaned).strip()[:80]


def positive(update: DealUpdate, crm: str) -> None:
    if update.amount is not None and update.amount <= 0:
        raise CrmValidationError("a deal amount is above zero", crm=crm)
