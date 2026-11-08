"""Bitrix24 (cloud) through an inbound webhook.

What differs from other CRMs, checked live:
- the INN lives in a requisite (``crm.requisite``, preset "Organisation") of the company;
- duplicates are found by ``crm.duplicate.findbycomm`` (e-mail, phone) right after a write;
- deal lines are product rows with their own price and quantity; a row may have no product;
  the deal amount is not recounted from the rows, so it is set explicitly;
- a file goes to the deal timeline as a comment with the file attached;
- a task is a CRM to-do (``crm.activity.todo.add``): a webhook with the crm scope cannot create
  tasks of the Tasks module without the extra task scope;
- the portal allows about two requests a second and answers 503 ``QUERY_LIMIT_EXCEEDED``.

The webhook URL is a secret (the token is in the path): it never appears in errors or logs.
"""

import base64
import contextlib
import logging
import re
from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar
from urllib.parse import urlsplit

import httpx

from alnuzum_crm.base import CRM, dec, digits, record_id, require_one, same_name
from alnuzum_crm.errors import (
    CrmAuthError,
    CrmError,
    CrmNotFound,
    CrmPermissionError,
    CrmRateLimited,
    CrmUnavailable,
    CrmUnsupported,
    CrmValidationError,
)
from alnuzum_crm.http import HttpCore, RetryPolicy, shared_limiter
from alnuzum_crm.types import (
    AccountInfo,
    CompanyIn,
    ContactIn,
    CreatedDeal,
    CrmKind,
    Deal,
    DealIn,
    DealLine,
    DealStatus,
    FileIn,
    Leftovers,
    Page,
    Product,
    ProductIn,
    TaskIn,
)

#: cloud portals only: the webhook host is where the server sends requests
CLOUD_SUFFIXES = (
    ".bitrix24.ru",
    ".bitrix24.by",
    ".bitrix24.kz",
    ".bitrix24.com",
    ".bitrix24.de",
    ".bitrix24.eu",
    ".bitrix24.es",
    ".bitrix24.fr",
    ".bitrix24.it",
    ".bitrix24.pl",
    ".bitrix24.uk",
    ".bitrix24.in",
    ".bitrix24.id",
    ".bitrix24.vn",
    ".bitrix24.jp",
    ".bitrix24.cn",
    ".bitrix24.co",
    ".bitrix24.com.br",
    ".bitrix24.com.tr",
)
_PORTAL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_PATH = re.compile(r"^/rest/(\d{1,10})/([A-Za-z0-9]{8,64})/?$")
AUTH_CODES = frozenset(
    {"INVALID_CREDENTIALS", "NO_AUTH_FOUND", "invalid_token", "expired_token", "WRONG_AUTH_TYPE"}
)
SCOPE_CODES = frozenset(
    {"insufficient_scope", "ACCESS_DENIED", "PAYMENT_REQUIRED", "ERROR_REST_ACCESS_DENIED"}
)


def parse_webhook(url: str) -> tuple[str, str]:
    """ "https://portal.bitrix24.ru/rest/1/token/" → (host, base URL without the trailing slash).

    Only https on the default port, a cloud Bitrix24 host and the inbound-webhook path pass."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme != "https" or parts.port not in (None, 443) or parts.username or parts.password:
        raise ValueError("a Bitrix24 webhook is an https address")
    if parts.query or parts.fragment:
        raise ValueError("a Bitrix24 webhook has no query")
    suffix = next((s for s in CLOUD_SUFFIXES if host.endswith(s)), None)
    if suffix is None or not _PORTAL.match(host[: -len(suffix)]):
        raise ValueError("not a Bitrix24 cloud portal")
    m = _PATH.match(parts.path)
    if not m:
        raise ValueError("not an inbound webhook address (/rest/<user>/<token>/)")
    return host, f"https://{host}/rest/{m.group(1)}/{m.group(2)}"


def _limit_answer(resp: httpx.Response) -> bool:
    return resp.status_code in (429, 503) and "QUERY_LIMIT_EXCEEDED" in resp.text


def _dt(value: Any) -> datetime | None:
    return datetime.fromisoformat(str(value)) if value else None


class Bitrix24(CRM):
    kind: ClassVar[CrmKind] = CrmKind.BITRIX24
    capabilities: ClassVar[frozenset[str]] = frozenset({"inn", "fractional_amount", "delete"})

    def __init__(
        self,
        webhook_url: str,
        *,
        rate: float = 2,
        retry: RetryPolicy | None = None,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.host, self._base = parse_webhook(webhook_url)
        # httpx logs every request URL at INFO, and this URL holds the token
        logging.getLogger("httpx").setLevel(
            max(logging.getLogger("httpx").getEffectiveLevel(), logging.WARNING)
        )
        self._core = HttpCore(
            self.kind.value,
            limiter=shared_limiter(f"bitrix24:{self.host}", rate),
            retry=retry or RetryPolicy(max_retries=4, backoff=1.0),
            timeout=timeout,
            transport=transport,
        )
        self._currency: str | None = None
        self._preset: int | None = None

    def __repr__(self) -> str:
        return f"Bitrix24(host={self.host!r})"

    async def aclose(self) -> None:
        await self._core.aclose()

    # --- transport ---

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """One REST method; the whole answer (``result``, ``total``, ``next``)."""
        if not re.fullmatch(r"[a-z0-9_.]+", method):
            raise ValueError("not a REST method name")
        resp = await self._core.send(
            "POST", f"{self._base}/{method}.json", json=params or {}, retry_if=_limit_answer
        )
        return self._parse(method, resp)

    def _parse(self, method: str, resp: httpx.Response) -> dict[str, Any]:
        crm, status = self.kind.value, resp.status_code
        if 300 <= status < 400:
            raise CrmError(
                f"answered a redirect to {method}: check the webhook address", crm=crm, status=status
            )
        try:
            data = resp.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            if status >= 500:
                raise CrmUnavailable(f"answered {status} to {method}", crm=crm, status=status)
            raise CrmError(f"answered {status} without JSON to {method}", crm=crm, status=status)
        if status < 400 and "error" not in data:
            return data
        code = str(data.get("error", ""))
        text = f"{method}: {str(data.get('error_description', '')).strip() or code or status}"
        if code in AUTH_CODES or status == 401:
            raise CrmAuthError(text, crm=crm, status=status, code=code)
        if code in SCOPE_CODES or status == 403:
            raise CrmPermissionError(text, crm=crm, status=status, code=code)
        if code == "QUERY_LIMIT_EXCEEDED":
            raise CrmRateLimited(text, crm=crm, status=status, code=code)
        if code == "ERROR_METHOD_NOT_FOUND":
            raise CrmUnsupported(text, crm=crm, status=status, code=code)
        if "not found" in text.lower():
            raise CrmNotFound(text, crm=crm, status=status, code=code)
        if status >= 500:
            raise CrmUnavailable(text, crm=crm, status=status, code=code)
        raise CrmValidationError(text, crm=crm, status=status, code=code)

    async def account(self) -> AccountInfo:
        # an empty deal list proves the webhook is alive and has the crm scope
        await self._result("crm.deal.list", {"filter": {"ID": 0}, "select": ["ID"], "start": -1})
        return AccountInfo(self.host, self.host, f"https://{self.host}")

    def deal_link(self, account: AccountInfo, deal_id: str) -> str:
        return f"{account.web}/crm/deal/details/{record_id(deal_id, self.kind.value)}/"

    async def _result(self, method: str, params: dict[str, Any] | None = None) -> Any:
        return (await self.call(method, params)).get("result")

    async def _list(self, method: str, params: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        start: Any = 0
        while start is not None and len(rows) < limit:
            data = await self.call(method, {**params, "start": start})
            result = data.get("result")
            if isinstance(result, dict):
                result = next((v for v in result.values() if isinstance(v, list)), [])
            rows += [r for r in result or [] if isinstance(r, dict)]
            start = data.get("next")
        return rows[:limit]

    async def _base_currency(self) -> str:
        if self._currency is None:
            self._currency = str(await self._result("crm.currency.base.get") or "")
        return self._currency

    # --- companies and contacts ---

    async def find_company(self, company: CompanyIn) -> str | None:
        if company.inn:
            reqs = await self._result(
                "crm.requisite.list",
                {"filter": {"RQ_INN": digits(company.inn), "ENTITY_TYPE_ID": 4}, "select": ["ENTITY_ID"]},
            )
            ids = [int(r["ENTITY_ID"]) for r in reqs or []]
            if ids:
                return str(min(ids))
        rows = await self._list(
            "crm.company.list",
            {"filter": {"TITLE": company.name}, "select": ["ID", "TITLE"], "order": {"ID": "ASC"}},
            5000,
        )
        ids = sorted(int(r["ID"]) for r in rows if same_name(str(r.get("TITLE", "")), company.name))
        if ids and company.inn:
            # a name match counts only when the company has no INN or the same one
            reqs = await self._list(
                "crm.requisite.list",
                {"filter": {"ENTITY_TYPE_ID": 4, "ENTITY_ID": ids}, "select": ["ENTITY_ID", "RQ_INN"]},
                5000,
            )
            other = {
                int(r["ENTITY_ID"]) for r in reqs if digits(r.get("RQ_INN")) not in ("", digits(company.inn))
            }
            ids = [i for i in ids if i not in other]
        return str(ids[0]) if ids else None

    async def _org_preset(self) -> int | None:
        if self._preset is None:
            presets = await self._result(
                "crm.requisite.preset.list", {"select": ["ID", "ENTITY_TYPE_ID", "XML_ID"]}
            )
            ids = [int(p["ID"]) for p in presets or []]
            self._preset = min(ids) if ids else 0
        return self._preset or None

    async def create_company(self, company: CompanyIn) -> str:
        fields: dict[str, Any] = {"TITLE": company.name}
        if company.phone:
            fields["PHONE"] = [{"VALUE": company.phone, "VALUE_TYPE": "WORK"}]
        if company.email:
            fields["EMAIL"] = [{"VALUE": company.email, "VALUE_TYPE": "WORK"}]
        company_id = str(await self._result("crm.company.add", {"fields": fields}))
        preset = await self._org_preset() if company.inn else None
        if company.inn and preset:
            req = {
                "ENTITY_TYPE_ID": 4,
                "ENTITY_ID": int(company_id),
                "PRESET_ID": preset,
                "NAME": company.name,
            }
            await self._result("crm.requisite.add", {"fields": {**req, "RQ_INN": digits(company.inn)}})
        return company_id

    async def _duplicates(self, kind: str, value: str) -> list[int]:
        found = await self._result(
            "crm.duplicate.findbycomm", {"entity_type": "CONTACT", "type": kind, "values": [value]}
        )
        return [int(x) for x in (found or {}).get("CONTACT", [])] if isinstance(found, dict) else []

    async def find_contact(self, contact: ContactIn) -> str | None:
        if contact.email:
            ids = await self._duplicates("EMAIL", contact.email.strip())
            if ids:
                return str(min(ids))
        if len(digits(contact.phone)) >= 7:
            ids = await self._duplicates("PHONE", digits(contact.phone))
            if ids:
                return str(min(ids))
        return None

    async def link_contact(self, contact_id: str, company_id: str) -> bool:
        crm = self.kind.value
        bound = await self._result("crm.contact.company.items.get", {"id": int(record_id(contact_id, crm))})
        if bound:
            return False
        fields = {"COMPANY_ID": int(record_id(company_id, crm))}
        await self._result("crm.contact.company.add", {"id": int(contact_id), "fields": fields})
        return True

    async def create_contact(self, contact: ContactIn, *, company_id: str | None = None) -> str:
        fields: dict[str, Any] = {"NAME": contact.name}
        if contact.email:
            fields["EMAIL"] = [{"VALUE": contact.email.strip(), "VALUE_TYPE": "WORK"}]
        if contact.phone:
            fields["PHONE"] = [{"VALUE": contact.phone, "VALUE_TYPE": "WORK"}]
        if company_id:
            fields["COMPANY_ID"] = int(company_id)
        return str(await self._result("crm.contact.add", {"fields": fields}))

    # --- catalog ---

    async def create_product(self, product: ProductIn) -> str:
        fields: dict[str, Any] = {
            "NAME": product.name,
            "PRICE": float(product.price),
            "CURRENCY_ID": await self._base_currency(),
        }
        if product.sku:
            fields["XML_ID"] = product.sku
        return str(await self._result("crm.product.add", {"fields": fields}))

    async def list_products(
        self, *, query: str | None = None, cursor: str | None = None, limit: int = 50
    ) -> Page[Product]:
        params: dict[str, Any] = {
            "select": ["ID", "NAME", "PRICE", "CURRENCY_ID", "XML_ID"],
            "order": {"ID": "ASC"},
            "start": int(cursor or 0),
        }
        if query:
            params["filter"] = {"%NAME": query}
        start = params["start"]
        data = await self.call("crm.product.list", params)
        page = [r for r in data.get("result") or [] if isinstance(r, dict)]
        rows = page[:limit]
        items = tuple(
            Product(
                id=str(r["ID"]),
                name=str(r.get("NAME", "")),
                # XML_ID is the external code; Bitrix24 fills it with the ID when nothing was given
                sku=str(r["XML_ID"]) if r.get("XML_ID") and str(r["XML_ID"]) != str(r["ID"]) else None,
                price=dec(r.get("PRICE")),
                currency=r.get("CURRENCY_ID") or None,
            )
            for r in rows
        )
        # Bitrix24 pages by 50 from any offset: a smaller limit continues right after the last row
        if len(page) > limit:
            return Page(items, str(start + limit))
        nxt = data.get("next")
        return Page(items, str(nxt) if nxt is not None else None)

    # --- deals ---

    async def create_deal(self, deal: DealIn) -> CreatedDeal:
        fields: dict[str, Any] = {
            "TITLE": deal.title,
            "CURRENCY_ID": deal.currency or await self._base_currency(),
            "OPPORTUNITY": float(deal.amount),
            "IS_MANUAL_OPPORTUNITY": "Y",
        }
        if deal.company_id:
            fields["COMPANY_ID"] = int(deal.company_id)
        if deal.contact_id:
            fields["CONTACT_ID"] = int(deal.contact_id)
        deal_id = str(await self._result("crm.deal.add", {"fields": fields}))
        if deal.lines:
            rows = [
                {
                    "PRODUCT_ID": int(line.product_id) if line.product_id else 0,
                    "PRODUCT_NAME": line.name,
                    "PRICE": float(line.unit_price),
                    "QUANTITY": float(line.quantity),
                }
                for line in deal.lines
            ]
            await self._result("crm.deal.productrows.set", {"id": int(deal_id), "rows": rows})
        return CreatedDeal(deal_id)

    def _deal(self, row: dict[str, Any], lines: tuple[DealLine, ...] = ()) -> Deal:
        semantic = row.get("STAGE_SEMANTIC_ID")
        status = (
            DealStatus.WON if semantic == "S" else DealStatus.LOST if semantic == "F" else DealStatus.OPEN
        )
        return Deal(
            id=str(row["ID"]),
            title=str(row.get("TITLE", "")),
            amount=dec(row.get("OPPORTUNITY")),
            currency=row.get("CURRENCY_ID") or None,
            status=status,
            created_at=_dt(row.get("DATE_CREATE")),
            closed_at=_dt(row.get("CLOSEDATE")) if row.get("CLOSED") == "Y" else None,
            lines=lines,
        )

    async def get_deal(self, deal_id: str) -> Deal:
        row = await self._result("crm.deal.get", {"id": int(deal_id)})
        rows = await self._result("crm.deal.productrows.get", {"id": int(deal_id)})
        lines = tuple(
            DealLine(
                name=str(r.get("PRODUCT_NAME", "")),
                quantity=Decimal(str(r.get("QUANTITY", 0))),
                unit_price=Decimal(str(r.get("PRICE", 0))),
                product_id=str(r["PRODUCT_ID"]) if int(r.get("PRODUCT_ID") or 0) else None,
            )
            for r in rows or []
        )
        return self._deal(row, lines)

    async def deal_history(
        self, *, company_id: str | None = None, contact_id: str | None = None, limit: int = 200
    ) -> list[Deal]:
        require_one(company_id, contact_id, self.kind.value)
        flt = {"COMPANY_ID": int(company_id)} if company_id else {"CONTACT_ID": int(contact_id or 0)}
        select = [
            "ID",
            "TITLE",
            "OPPORTUNITY",
            "CURRENCY_ID",
            "STAGE_SEMANTIC_ID",
            "CLOSED",
            "CLOSEDATE",
            "DATE_CREATE",
        ]
        rows = await self._list(
            "crm.deal.list", {"filter": flt, "select": select, "order": {"DATE_CREATE": "DESC"}}, limit
        )
        return [self._deal(r) for r in rows]

    # --- files, notes, tasks ---

    async def attach_file(self, deal_id: str, file: FileIn) -> str:
        fields = {
            "ENTITY_ID": int(deal_id),
            "ENTITY_TYPE": "deal",
            "COMMENT": file.name,
            "FILES": [[file.name, base64.b64encode(file.content).decode()]],
        }
        return str(await self._result("crm.timeline.comment.add", {"fields": fields}))

    async def add_note(self, deal_id: str, text: str) -> str:
        fields = {"ENTITY_ID": int(deal_id), "ENTITY_TYPE": "deal", "COMMENT": text}
        return str(await self._result("crm.timeline.comment.add", {"fields": fields}))

    async def add_task(self, deal_id: str, task: TaskIn) -> str:
        params: dict[str, Any] = {
            "ownerTypeId": 2,
            "ownerId": int(deal_id),
            "deadline": task.due.isoformat(timespec="seconds"),
            "title": task.text[:250],
            "description": task.text,
        }
        if task.responsible_id:
            params["responsibleId"] = int(task.responsible_id)
        result = await self._result("crm.activity.todo.add", params)
        return str(result["id"] if isinstance(result, dict) else result)

    # --- cleanup ---

    async def _delete(self, method: str, ids: tuple[str, ...]) -> None:
        for i in ids:
            with contextlib.suppress(CrmNotFound):
                await self._result(method, {"id": int(i)})

    async def discard(self, leftovers: Leftovers) -> None:
        # the deal takes its to-dos and timeline with it
        await self._delete("crm.deal.delete", leftovers.deal_ids)
        await self._delete("crm.contact.delete", leftovers.contact_ids)
        await self._delete("crm.company.delete", leftovers.company_ids)
        await self._delete("crm.product.delete", leftovers.product_ids)
