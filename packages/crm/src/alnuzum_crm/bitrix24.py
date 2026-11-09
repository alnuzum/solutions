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
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar
from urllib.parse import urlencode, urlsplit

import httpx

from alnuzum_crm.base import (
    CRM,
    at_most_one,
    aware,
    catalog_lines,
    dec,
    digits,
    hits_in_order,
    host,
    in_period,
    lookup_keys,
    positive,
    record_id,
    require_one,
    same_name,
    search_text,
)
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
    Stage,
    TaskIn,
    User,
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


def _semantic(value: Any) -> DealStatus:
    return DealStatus.WON if value == "S" else DealStatus.LOST if value == "F" else DealStatus.OPEN


DEAL_FIELDS = ["ID", "TITLE", "STAGE_ID", "STAGE_SEMANTIC_ID", "OPPORTUNITY", "COMPANY_ID", "CONTACT_ID"]


def php_query(params: dict[str, Any]) -> str:
    """The parameters of a REST method as a command of ``batch``: PHP's ``http_build_query``
    (``filter[WEB][0]=…&select[0]=ID``)."""
    pairs: list[tuple[str, str]] = []

    def walk(prefix: str, value: Any) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                walk(f"{prefix}[{k}]" if prefix else str(k), v)
        elif isinstance(value, list | tuple):
            for i, v in enumerate(value):
                walk(f"{prefix}[{i}]", v)
        elif isinstance(value, bool):
            pairs.append((prefix, "Y" if value else "N"))
        elif value is not None:
            pairs.append((prefix, str(value)))

    walk("", params)
    return urlencode(pairs)


def _rows(found: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """The rows one command of a batch gave (an empty answer comes as ``[]`` or not at all)."""
    got = found.get(key)
    return [r for r in got if isinstance(r, dict)] if isinstance(got, list) else []


NO_BBCODE = str.maketrans({"[": "［", "]": "］"})  # fullwidth brackets read the same, parse as text


class Bitrix24(CRM):
    kind: ClassVar[CrmKind] = CrmKind.BITRIX24
    capabilities: ClassVar[frozenset[str]] = frozenset(
        {"inn", "fractional_amount", "delete", "line_prices", "stock", "close_date"}
    )
    #: our units to the symbols of ``crm.measure.list``
    UNITS: ClassVar[dict[str, str]] = {"шт": "шт", "м": "м", "кг": "кг", "л": "л.", "г": "г"}

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
        self._measures: dict[str, str] | None = None
        self._sections: dict[str, str] | None = None
        self._catalog_iblock: int | None = None
        # the webhook has no «catalog» scope (or the portal no product catalog): products are read
        # without stock, and the caller can tell the user which right to add
        self.stock_denied = False

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
        # a missing scope comes as 401 too: it is a right to add, not a webhook to replace
        if code in SCOPE_CODES or (status == 403 and code not in AUTH_CODES):
            raise CrmPermissionError(text, crm=crm, status=status, code=code)
        if code in AUTH_CODES or status == 401:
            raise CrmAuthError(text, crm=crm, status=status, code=code)
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

    async def _batch(self, cmds: dict[str, tuple[str, dict[str, Any]]]) -> dict[str, Any]:
        """Up to 50 methods in one call (one request of the portal's two a second): the result of each by
        its key; one that failed raises as it would alone."""
        if not cmds:
            return {}
        for method, _ in cmds.values():
            if not re.fullmatch(r"[a-z0-9_.]+", method):
                raise ValueError("not a REST method name")
        data = await self.call(
            "batch", {"halt": 0, "cmd": {k: f"{m}?{php_query(p)}" for k, (m, p) in cmds.items()}}
        )
        result = data.get("result")
        result = result if isinstance(result, dict) else {}
        errors = result.get("result_error")
        for key, (method, _) in cmds.items():
            error = errors.get(key) if isinstance(errors, dict) else None
            if error:
                body = error if isinstance(error, dict) else {"error": str(error)}
                self._parse(method, httpx.Response(400, json=body))
        found = result.get("result")
        return found if isinstance(found, dict) else {}

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
        # live 09.11: the INN lives in requisites; the site matches only as written in full («https://x»), so
        # every way of writing it is asked at once; mail and phone through the duplicate finder; «%WEB»,
        # «%EMAIL» and «%PHONE» filters are ignored by the portal and give every company, never used here
        # the portal answers two requests a second: every search goes in one batch, the reading in a second
        keys = lookup_keys(inn=inn, domain=domain, email=email, phone=phone, name=name)
        cmds: dict[str, tuple[str, dict[str, Any]]] = {}
        if keys.get("inn"):
            cmds["inn"] = (
                "crm.requisite.list",
                {"filter": {"RQ_INN": keys["inn"], "ENTITY_TYPE_ID": 4}, "select": ["ENTITY_ID"]},
            )
        if keys.get("domain"):
            sites = [
                f"{scheme}{www}{keys['domain']}"
                for scheme in ("https://", "http://", "")
                for www in ("", "www.")
            ]
            cmds["domain"] = ("crm.company.list", {"filter": {"WEB": sites}, "select": ["ID", "WEB"]})
        for kind, value in (("EMAIL", keys.get("email")), ("PHONE", keys.get("phone"))):
            if value:
                cmds[kind.lower()] = (
                    "crm.duplicate.findbycomm",
                    {"entity_type": "COMPANY", "type": kind, "values": [value]},
                )
        if keys.get("name"):
            cmds["name"] = (
                "crm.company.list",
                {"filter": {"%TITLE": keys["name"]}, "select": ["ID"], "order": {"ID": "ASC"}},
            )
        found = await self._batch(cmds)
        ids = [int(r["ENTITY_ID"]) for r in _rows(found, "inn")]
        # the site again by its host: a portal that ignored the filter would give its first 50 companies
        ids += [
            int(r["ID"])
            for r in _rows(found, "domain")
            if any(host(str(w.get("VALUE") or "")) == keys.get("domain") for w in r.get("WEB") or [])
        ]
        for kind in ("email", "phone"):
            dup = found.get(kind)
            if isinstance(dup, dict):
                ids += [int(x) for x in dup.get("COMPANY", [])]
        ids += [int(r["ID"]) for r in _rows(found, "name")]
        wanted = list(dict.fromkeys(ids))[:limit]
        if not wanted:
            return []
        read = await self._batch(
            {
                "rows": (
                    "crm.company.list",
                    {"filter": {"ID": wanted}, "select": ["ID", "TITLE", "WEB", "EMAIL", "PHONE"]},
                ),
                "reqs": (
                    "crm.requisite.list",
                    {"filter": {"ENTITY_TYPE_ID": 4, "ENTITY_ID": wanted}, "select": ["ENTITY_ID", "RQ_INN"]},
                ),
            }
        )
        inns = {
            str(r["ENTITY_ID"]): digits(r.get("RQ_INN"))
            for r in _rows(read, "reqs")
            if digits(r.get("RQ_INN"))
        }

        def values(row: dict[str, Any], key: str) -> tuple[str, ...]:
            return tuple(str(v.get("VALUE") or "") for v in row.get(key) or [] if v.get("VALUE"))

        by_id = {str(r["ID"]): r for r in _rows(read, "rows")}
        return [
            CompanyHit(
                cid,
                str(row.get("TITLE") or ""),
                inn=inns.get(cid),
                phones=values(row, "PHONE"),
                emails=values(row, "EMAIL"),
                web=tuple(w for w in values(row, "WEB") if host(w)),
            )
            for cid in map(str, wanted)
            if (row := by_id.get(cid)) is not None
        ]

    async def users(self) -> list[User]:
        rows = await self._result("user.get", {"FILTER": {"ACTIVE": True}})
        return [
            User(str(u["ID"]), " ".join(x for x in (u.get("NAME"), u.get("LAST_NAME")) if x) or str(u["ID"]))
            for u in rows or []
        ]

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

    async def _measure_names(self) -> dict[str, str]:
        """Measure id -> symbol."""
        if self._measures is None:
            rows = await self._result("crm.measure.list") or []
            self._measures = {
                str(m["ID"]): str(m.get("SYMBOL_RUS") or m.get("SYMBOL") or m.get("CODE") or "") for m in rows
            }
        return self._measures

    async def _section_names(self) -> dict[str, str]:
        """Product section id -> name."""
        if self._sections is None:
            rows = await self._list("crm.productsection.list", {"select": ["ID", "NAME"]}, 500)
            self._sections = {str(r["ID"]): str(r.get("NAME", "")) for r in rows}
        return self._sections

    async def _catalog_id(self) -> int:
        if self._catalog_iblock is None:
            data = await self._result("catalog.catalog.list") or {}
            rows = data.get("catalogs", []) if isinstance(data, dict) else []
            main = [r for r in rows if r.get("productIblockId") is None]
            if not main:
                raise CrmUnsupported("no product catalog in this portal", crm=self.kind.value)
            self._catalog_iblock = int(main[0]["iblockId"])
        return self._catalog_iblock

    async def create_product(self, product: ProductIn) -> str:
        fields: dict[str, Any] = {
            "NAME": product.name,
            "PRICE": float(product.price),
            "CURRENCY_ID": await self._base_currency(),
        }
        if product.sku:
            fields["XML_ID"] = product.sku
        if product.description:
            fields["DESCRIPTION"] = product.description
        if product.unit and product.unit in self.UNITS:
            symbol = self.UNITS[product.unit]
            found = [i for i, sym in (await self._measure_names()).items() if sym == symbol]
            if found:
                fields["MEASURE"] = int(found[0])
        if product.category:
            sections = await self._section_names()
            section = next((i for i, n in sections.items() if n == product.category), None)
            if section is None:
                section = str(
                    await self._result(
                        "crm.productsection.add",
                        {"fields": {"NAME": product.category, "CATALOG_ID": await self._catalog_id()}},
                    )
                )
                sections[section] = product.category
            fields["SECTION_ID"] = int(section)
        pid = str(await self._result("crm.product.add", {"fields": fields}))
        if product.stock is not None:
            await self._result(
                "catalog.product.update", {"id": int(pid), "fields": {"quantity": float(product.stock)}}
            )
        return pid

    async def _stock(self, ids: list[str]) -> dict[str, Decimal]:
        if not ids:
            return {}
        data = await self._result(
            "catalog.product.list",
            {
                "select": ["id", "iblockId", "quantity"],
                "filter": {"iblockId": await self._catalog_id(), "id": [int(i) for i in ids]},
            },
        )
        rows = data.get("products", []) if isinstance(data, dict) else []
        return {str(r["id"]): Decimal(str(r["quantity"])) for r in rows if r.get("quantity") is not None}

    async def list_products(
        self, *, query: str | None = None, cursor: str | None = None, limit: int = 50
    ) -> Page[Product]:
        params: dict[str, Any] = {
            "select": [
                "ID",
                "NAME",
                "PRICE",
                "CURRENCY_ID",
                "XML_ID",
                "MEASURE",
                "SECTION_ID",
                "DESCRIPTION",
            ],
            "order": {"ID": "ASC"},
            "start": int(cursor or 0),
        }
        if query:
            params["filter"] = {"%NAME": query}
        start = params["start"]
        data = await self.call("crm.product.list", params)
        page = [r for r in data.get("result") or [] if isinstance(r, dict)]
        rows = page[:limit]
        measures = await self._measure_names() if any(r.get("MEASURE") for r in rows) else {}
        sections = await self._section_names() if any(r.get("SECTION_ID") for r in rows) else {}
        stock: dict[str, Decimal] = {}
        if not self.stock_denied:
            try:
                stock = await self._stock([str(r["ID"]) for r in rows])
            except (CrmPermissionError, CrmUnsupported):
                self.stock_denied = True
        items = tuple(
            Product(
                id=str(r["ID"]),
                name=str(r.get("NAME", "")),
                # XML_ID is the external code; Bitrix24 fills it with the ID when nothing was given
                sku=str(r["XML_ID"]) if r.get("XML_ID") and str(r["XML_ID"]) != str(r["ID"]) else None,
                price=dec(r.get("PRICE")),
                currency=r.get("CURRENCY_ID") or None,
                unit=measures.get(str(r.get("MEASURE") or "")) or None,
                category=sections.get(str(r.get("SECTION_ID") or "")) or None,
                description=r.get("DESCRIPTION") or None,
                stock=stock.get(str(r["ID"])),
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
        if deal.stage_id:
            if not re.fullmatch(r"[A-Z0-9_:]{1,50}", deal.stage_id):
                raise CrmValidationError("not a stage of Bitrix24", crm=self.kind.value)
            fields["STAGE_ID"] = deal.stage_id
            pipeline = re.match(r"C(\d{1,9}):", deal.stage_id)
            fields["CATEGORY_ID"] = int(pipeline.group(1)) if pipeline else 0
        if deal.responsible_id:
            fields["ASSIGNED_BY_ID"] = int(record_id(deal.responsible_id, self.kind.value))
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
        status = _semantic(row.get("STAGE_SEMANTIC_ID"))
        return Deal(
            id=str(row["ID"]),
            title=str(row.get("TITLE", "")),
            amount=dec(row.get("OPPORTUNITY")),
            currency=row.get("CURRENCY_ID") or None,
            status=status,
            created_at=_dt(row.get("DATE_CREATE")),
            closed_at=_dt(row.get("CLOSEDATE")) if row.get("CLOSED") == "Y" else None,
            lines=lines,
            stage_id=row.get("STAGE_ID") or None,
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
        # the timeline renders BBCode: a note quoting a person must not become a link or a picture there
        plain = text.translate(NO_BBCODE)
        fields = {"ENTITY_ID": int(deal_id), "ENTITY_TYPE": "deal", "COMMENT": plain}
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

    # --- the voice of a manager: find a deal, move it, add products ---

    async def stages(self) -> list[Stage]:
        try:
            data = await self._result("crm.category.list", {"entityTypeId": 2})
            categories = [
                (int(c["id"]), str(c.get("name") or "")) for c in (data or {}).get("categories", [])
            ]
        except CrmUnsupported:  # a portal without pipelines of deals: the one default pipeline
            categories = [(0, "")]
        out: list[Stage] = []
        for cat, name in categories or [(0, "")]:
            entity = "DEAL_STAGE" if cat == 0 else f"DEAL_STAGE_{cat}"
            rows = await self._result(
                "crm.status.list", {"filter": {"ENTITY_ID": entity}, "order": {"SORT": "ASC"}}
            )
            out += [
                Stage(
                    str(r["STATUS_ID"]), str(r.get("NAME") or ""), _semantic(r.get("SEMANTICS")), name or None
                )
                for r in rows or []
            ]
        return out

    async def _titles(self, method: str, ids: set[str], name: Any) -> dict[str, str]:
        if not ids:
            return {}
        rows = await self._list(
            method, {"filter": {"ID": sorted(ids)}, "select": ["ID", "TITLE", "NAME", "LAST_NAME"]}, 50
        )
        return {str(r["ID"]): name(r) for r in rows}

    async def find_deals(self, query: str, *, limit: int = 8) -> list[DealHit]:
        text = search_text(query)
        if len(text) < 2:
            return []
        by_title = await self._list(
            "crm.deal.list", {"filter": {"%TITLE": text}, "select": DEAL_FIELDS}, limit
        )
        companies = await self._list("crm.company.list", {"filter": {"%TITLE": text}, "select": ["ID"]}, 10)
        by_company = (
            await self._list(
                "crm.deal.list",
                {"filter": {"COMPANY_ID": [int(c["ID"]) for c in companies]}, "select": DEAL_FIELDS},
                limit,
            )
            if companies
            else []
        )
        words = text.split()
        filters: list[dict[str, str]] = [{"%NAME": text}, {"%LAST_NAME": text}]
        if len(words) >= 2:
            filters.append({"%NAME": words[0], "%LAST_NAME": words[-1]})
        people: dict[str, None] = {}
        for flt in filters:
            for c in await self._list("crm.contact.list", {"filter": flt, "select": ["ID"]}, 10):
                people[str(c["ID"])] = None
        by_contact = (
            await self._list(
                "crm.deal.list",
                {"filter": {"CONTACT_ID": [int(i) for i in people]}, "select": DEAL_FIELDS},
                limit,
            )
            if people
            else []
        )
        rows = by_title + by_company + by_contact
        names = await self._titles(
            "crm.company.list",
            {str(r["COMPANY_ID"]) for r in rows if int(r.get("COMPANY_ID") or 0)},
            lambda r: str(r.get("TITLE") or ""),
        )
        persons = await self._titles(
            "crm.contact.list",
            {str(r["CONTACT_ID"]) for r in rows if int(r.get("CONTACT_ID") or 0)},
            lambda r: " ".join(x for x in (r.get("NAME"), r.get("LAST_NAME")) if x),
        )

        def hit(by: str, r: dict[str, Any]) -> DealHit:
            return DealHit(
                str(r["ID"]),
                str(r.get("TITLE") or ""),
                names.get(str(r.get("COMPANY_ID"))),
                persons.get(str(r.get("CONTACT_ID"))),
                r.get("STAGE_ID") or None,
                dec(r.get("OPPORTUNITY")),
                _semantic(r.get("STAGE_SEMANTIC_ID")),
                by,
            )

        return hits_in_order(
            [
                [hit("title", r) for r in by_title],
                [hit("company", r) for r in by_company],
                [hit("contact", r) for r in by_contact],
            ],
            limit,
        )

    async def update_deal(self, deal_id: str, update: DealUpdate) -> None:
        positive(update, self.kind.value)
        fields: dict[str, Any] = {}
        if update.stage_id is not None:
            if not re.fullmatch(r"[A-Z0-9_:]{1,50}", update.stage_id):
                raise CrmValidationError("not a stage of Bitrix24", crm=self.kind.value)
            fields["STAGE_ID"] = update.stage_id
        if update.amount is not None:
            # without the manual flag the amount is counted again from the product rows (checked live)
            fields |= {"OPPORTUNITY": float(update.amount), "IS_MANUAL_OPPORTUNITY": "Y"}
        if update.close_date is not None:
            fields["CLOSEDATE"] = update.close_date.isoformat()
        if fields:
            await self._result(
                "crm.deal.update", {"id": int(record_id(deal_id, self.kind.value)), "fields": fields}
            )

    async def add_lines(self, deal_id: str, lines: tuple[DealLine, ...]) -> None:
        catalog_lines(lines, self.kind.value)
        owner = int(record_id(deal_id, self.kind.value))
        for line in lines:
            fields = {
                "ownerType": "D",
                "ownerId": owner,
                "productId": int(str(line.product_id)),
                "price": float(line.unit_price),
                "quantity": float(line.quantity),
            }
            await self._result("crm.item.productrow.add", {"fields": fields})

    # --- cleanup ---

    # --- win-back: purchases and tasks on a client ---

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
        """Won deals are the success semantics of every pipeline; ``CLOSEDATE`` is a date of the portal and
        sorts the list (checked live 10.11); products come in ``batch`` calls of 50 deals."""
        crm = self.kind.value
        at_most_one(company_id, contact_id, crm)
        flt: dict[str, Any] = {"STAGE_SEMANTIC_ID": "S"}
        if company_id:
            flt["COMPANY_ID"] = int(record_id(company_id, crm))
        if contact_id:
            flt["CONTACT_ID"] = int(record_id(contact_id, crm))
        if since:
            flt[">=CLOSEDATE"] = aware(since).isoformat(timespec="seconds")
        if until:
            flt["<CLOSEDATE"] = aware(until).isoformat(timespec="seconds")
        select = [
            "ID",
            "TITLE",
            "OPPORTUNITY",
            "CURRENCY_ID",
            "STAGE_ID",
            "STAGE_SEMANTIC_ID",
            "CLOSED",
            "CLOSEDATE",
            "DATE_CREATE",
            "COMPANY_ID",
            "CONTACT_ID",
        ]
        params = {"filter": flt, "select": select, "order": {"CLOSEDATE": "DESC", "ID": "DESC"}}
        next_cursor: str | None = None
        if company_id or contact_id:
            rows = await self._list("crm.deal.list", params, limit)
        else:
            data = await self.call("crm.deal.list", {**params, "start": int(cursor or 0)})
            result = data.get("result")
            rows = [r for r in result if isinstance(r, dict)] if isinstance(result, list) else []
            next_cursor = str(data["next"]) if data.get("next") is not None else None
        rows = [r for r in rows if in_period(_dt(r.get("CLOSEDATE")), since, until)]
        products: dict[str, list[dict[str, Any]]] = {}
        for i in range(0, len(rows) if lines else 0, 50):
            cmds = {
                f"d{r['ID']}": ("crm.deal.productrows.get", {"id": int(r["ID"])}) for r in rows[i : i + 50]
            }
            for key, value in (await self._batch(cmds)).items():
                products[key[1:]] = [x for x in value or [] if isinstance(x, dict)]
        out: list[Purchase] = []
        for r in rows:
            deal = self._deal(r)
            got = tuple(
                DealLine(
                    name=str(x.get("PRODUCT_NAME", "")),
                    quantity=Decimal(str(x.get("QUANTITY", 0))),
                    unit_price=Decimal(str(x.get("PRICE", 0))),
                    product_id=str(x["PRODUCT_ID"]) if int(x.get("PRODUCT_ID") or 0) else None,
                )
                for x in products.get(str(r["ID"]), [])
            )
            out.append(
                Purchase(
                    deal_id=deal.id,
                    title=deal.title,
                    closed_at=_dt(r.get("CLOSEDATE")) or datetime.min,
                    amount=deal.amount,
                    currency=deal.currency,
                    company_id=str(r["COMPANY_ID"]) if int(r.get("COMPANY_ID") or 0) else None,
                    contact_id=str(r["CONTACT_ID"]) if int(r.get("CONTACT_ID") or 0) else None,
                    lines=got,
                )
            )
        return Page(tuple(out), next_cursor)

    @staticmethod
    def _owner_type(company_id: str | None, contact_id: str | None, crm: str) -> tuple[int, int]:
        """CRM entity type of a company (4) or a contact (3), and its id."""
        require_one(company_id, contact_id, crm)
        if company_id:
            return 4, int(record_id(company_id, crm))
        return 3, int(record_id(contact_id, crm))

    async def add_client_task(
        self, task: TaskIn, *, company_id: str | None = None, contact_id: str | None = None
    ) -> str:
        owner_type, owner_id = self._owner_type(company_id, contact_id, self.kind.value)
        params: dict[str, Any] = {
            "ownerTypeId": owner_type,
            "ownerId": owner_id,
            "deadline": task.due.isoformat(timespec="seconds"),
            "title": task.text[:250],
            "description": task.text,
        }
        if task.responsible_id:
            params["responsibleId"] = int(record_id(task.responsible_id, self.kind.value))
        result = await self._result("crm.activity.todo.add", params)
        return str(result["id"] if isinstance(result, dict) else result)

    async def client_tasks(
        self, *, company_id: str | None = None, contact_id: str | None = None
    ) -> list[ClientTask]:
        owner_type, owner_id = self._owner_type(company_id, contact_id, self.kind.value)
        flt = {"OWNER_TYPE_ID": owner_type, "OWNER_ID": owner_id, "COMPLETED": "N"}
        rows = await self._list(
            "crm.activity.list", {"filter": flt, "select": ["ID", "SUBJECT", "DESCRIPTION", "DEADLINE"]}, 200
        )
        return [
            ClientTask(
                str(r["ID"]), str(r.get("SUBJECT") or r.get("DESCRIPTION") or ""), _dt(r.get("DEADLINE"))
            )
            for r in rows
        ]

    async def discard_task(self, task_id: str) -> None:
        with contextlib.suppress(CrmNotFound):
            await self._result("crm.activity.delete", {"id": int(record_id(task_id, self.kind.value))})

    async def client_names(self, ids: Iterable[str], *, contacts: bool = False) -> dict[str, str]:
        wanted = sorted({record_id(i, self.kind.value) for i in ids}, key=int)
        out: dict[str, str] = {}
        for n in range(0, len(wanted), 50):
            if contacts:
                out |= await self._titles(
                    "crm.contact.list",
                    set(wanted[n : n + 50]),
                    lambda r: " ".join(x for x in (r.get("NAME"), r.get("LAST_NAME")) if x),
                )
            else:
                out |= await self._titles(
                    "crm.company.list", set(wanted[n : n + 50]), lambda r: str(r.get("TITLE") or "")
                )
        return out

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
