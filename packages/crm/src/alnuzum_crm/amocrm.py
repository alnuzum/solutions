"""amoCRM and Kommo (API v4): the same API on two separate platforms.

What differs from other CRMs, checked live:
- a company has no INN field; a custom field with code ``INN`` (or named ИНН/INN/TIN/STIR) is used;
- deal lines are catalog elements linked with a quantity; the price lives on the element, so a
  line without a product becomes a new element with its price, and the deal budget is the sum
  (an integer: amoCRM keeps no kopecks);
- deals, contacts and catalog elements cannot be deleted through the API (405): test records
  are closed (status 143) and tagged ``e2e``;
- a missing record answers 204, not 404;
- 7 requests a second per account.
"""

from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, ClassVar

import httpx

from alnuzum_crm.base import (
    CRM,
    E2E_TAG,
    catalog_lines,
    dec,
    digits,
    hits_in_order,
    norm_name,
    positive,
    record_id,
    require_one,
    same_email,
    same_name,
    same_phone,
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
from alnuzum_crm.oauth import normalize_amo_domain
from alnuzum_crm.tokens import TokenSource
from alnuzum_crm.types import (
    AccountInfo,
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
    Stage,
    TaskIn,
)

WON, LOST = 142, 143
INN_NAMES = frozenset({"инн", "inn", "tin", "stir", "инн/tin", "tax id"})


def _error_text(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.reason_phrase or "error"
    if not isinstance(data, dict):
        return resp.reason_phrase or "error"
    parts = [str(data[k]) for k in ("title", "detail", "hint") if data.get(k)]
    errors = data.get("validation-errors")
    if errors:
        parts.append(str(errors)[:300])
    return "; ".join(parts)[:500] or (resp.reason_phrase or "error")


def _inn_compatible(want: str | None, have: list[str]) -> bool:
    """A name match counts only when the company has no INN or the same one."""
    known = [digits(v) for v in have if digits(v)]
    return not want or not known or digits(want) in known


def _platform_url(url: str) -> bool:
    if not url.startswith("https://"):
        return False
    host = httpx.URL(url).host
    return host.endswith((".amocrm.ru", ".amocrm.com", ".kommo.com"))


def _ts(value: Any) -> datetime | None:
    return datetime.fromtimestamp(int(value), UTC) if value else None


def _status(status_id: int) -> DealStatus:
    return DealStatus.WON if status_id == WON else DealStatus.LOST if status_id == LOST else DealStatus.OPEN


def _stage_id(lead: dict[str, Any]) -> str | None:
    if not lead.get("status_id") or not lead.get("pipeline_id"):
        return None
    return f"{int(lead['pipeline_id'])}:{int(lead['status_id'])}"


def _field_values(
    entity: dict[str, Any], *, code: str | None = None, field_id: int | None = None
) -> list[str]:
    out: list[str] = []
    for f in entity.get("custom_fields_values") or []:
        if (code and f.get("field_code") == code) or (field_id and f.get("field_id") == field_id):
            out += [str(v.get("value", "")) for v in f.get("values") or []]
    return out


class AmoCRM(CRM):
    kind: ClassVar[CrmKind] = CrmKind.AMOCRM
    capabilities: ClassVar[frozenset[str]] = frozenset({"inn"})
    #: our units to the values of the catalog's UNIT list (the rest go into the description only)
    UNITS: ClassVar[dict[str, str]] = {"шт": "шт", "кг": "кг", "л": "л"}

    def __init__(
        self,
        domain: str,
        tokens: TokenSource,
        *,
        catalog_id: int | None = None,
        inn_field_id: int | None = None,
        rate: float = 7,
        retry: RetryPolicy | None = None,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.domain = normalize_amo_domain(domain, self.kind)
        self._tokens = tokens
        self._core = HttpCore(
            self.kind.value,
            limiter=shared_limiter(f"{self.kind.value}:{self.domain}", rate),
            retry=retry,
            timeout=timeout,
            transport=transport,
            base_url=f"https://{self.domain}",
        )
        self._catalog_id = catalog_id
        self._inn_field_id = inn_field_id
        self._inn_checked = inn_field_id is not None
        self._price_field: int | None = None
        self._account: dict[str, Any] | None = None

    async def aclose(self) -> None:
        await self._core.aclose()

    # --- transport ---

    async def _request(self, method: str, url: str, **kw: Any) -> Any:
        token = await self._tokens.token()
        refreshed = False
        while True:
            resp = await self._core.send(method, url, headers={"Authorization": f"Bearer {token}"}, **kw)
            if resp.status_code == 401 and not refreshed:
                refreshed = True
                token = await self._tokens.refresh(token)
                continue
            return self._parse(resp)

    def _parse(self, resp: httpx.Response) -> Any:
        status = resp.status_code
        if status == 204:
            return None
        if status < 400:
            return resp.json() if resp.content else None
        crm, text = self.kind.value, _error_text(resp)
        if status == 401:
            raise CrmAuthError(text, crm=crm, status=status)
        if status in (402, 403):
            raise CrmPermissionError(text, crm=crm, status=status)
        if status == 404:
            raise CrmNotFound(text, crm=crm, status=status)
        if status == 405:
            raise CrmUnsupported(text, crm=crm, status=status)
        if status == 429:
            raise CrmRateLimited(text, crm=crm, status=status)
        if status >= 500:
            raise CrmUnavailable(text, crm=crm, status=status)
        raise CrmValidationError(text, crm=crm, status=status)

    async def _embedded(self, method: str, url: str, key: str, **kw: Any) -> list[dict[str, Any]]:
        data = await self._request(method, url, **kw)
        result: list[dict[str, Any]] = (data or {}).get("_embedded", {}).get(key, [])
        return result

    async def _search(self, path: str, key: str, text: str, *, max_pages: int = 20) -> list[dict[str, Any]]:
        """Every page of a substring search: an exact match may sit beyond the first 250 hits."""
        out: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            data = await self._request("GET", path, params={"query": text, "limit": 250, "page": page})
            if not data:
                break
            out += data.get("_embedded", {}).get(key, [])
            if not data.get("_links", {}).get("next"):
                break
        return out

    async def _first_id(self, method: str, url: str, key: str, body: Any) -> str:
        items = await self._embedded(method, url, key, json=body)
        if not items:
            raise CrmError("empty answer to a create", crm=self.kind.value)
        return str(items[0]["id"])

    # --- account settings, cached ---

    async def account(self) -> AccountInfo:
        self._account = None  # a liveness probe: always ask the CRM
        info = await self._account_info()
        return AccountInfo(str(info.get("id", "")), str(info.get("name") or ""), f"https://{self.domain}")

    def deal_link(self, account: AccountInfo, deal_id: str) -> str:
        return f"{account.web}/leads/detail/{record_id(deal_id, self.kind.value)}"

    async def _account_info(self) -> dict[str, Any]:
        if self._account is None:
            self._account = await self._request("GET", "/api/v4/account", params={"with": "drive_url"})
        return self._account or {}

    async def _currency(self) -> str | None:
        cur = (await self._account_info()).get("currency")
        return str(cur) if cur else None

    async def _inn_field(self) -> int | None:
        if not self._inn_checked:
            fields = await self._embedded("GET", "/api/v4/companies/custom_fields", "custom_fields")
            for f in fields:
                if f.get("code") == "INN" or norm_name(str(f.get("name", ""))) in INN_NAMES:
                    self._inn_field_id = int(f["id"])
                    break
            self._inn_checked = True
        return self._inn_field_id

    async def _catalog(self) -> int:
        if self._catalog_id is None:
            catalogs = await self._embedded("GET", "/api/v4/catalogs", "catalogs")
            products = [c for c in catalogs if c.get("type") == "products"]
            if not products:
                raise CrmUnsupported(
                    "the products catalog is turned off in this account", crm=self.kind.value
                )
            self._catalog_id = int(products[0]["id"])
        return self._catalog_id

    async def _price_field_id(self) -> int | None:
        if self._price_field is None:
            cat = await self._catalog()
            fields = await self._embedded("GET", f"/api/v4/catalogs/{cat}/custom_fields", "custom_fields")
            prices = [int(f["id"]) for f in fields if f.get("code") == "PRICE"]
            self._price_field = prices[0] if prices else 0
        return self._price_field or None

    # --- companies and contacts ---

    async def find_company(self, company: CompanyIn) -> str | None:
        inn_field = await self._inn_field() if company.inn else None
        if company.inn and inn_field:
            want = digits(company.inn)
            hits = await self._search("/api/v4/companies", "companies", want)
            ids = [
                int(c["id"])
                for c in hits
                if want in (digits(v) for v in _field_values(c, field_id=inn_field))
            ]
            if ids:
                return str(min(ids))
        hits = await self._search("/api/v4/companies", "companies", company.name)
        ids = [
            int(c["id"])
            for c in hits
            if same_name(str(c.get("name", "")), company.name)
            and _inn_compatible(company.inn, _field_values(c, field_id=inn_field) if inn_field else [])
        ]
        return str(min(ids)) if ids else None

    async def create_company(self, company: CompanyIn) -> str:
        fields: list[dict[str, Any]] = []
        inn_field = await self._inn_field() if company.inn else None
        if company.inn and inn_field:
            fields.append({"field_id": inn_field, "values": [{"value": company.inn}]})
        if company.phone:
            fields.append({"field_code": "PHONE", "values": [{"value": company.phone, "enum_code": "WORK"}]})
        if company.email:
            fields.append({"field_code": "EMAIL", "values": [{"value": company.email, "enum_code": "WORK"}]})
        body = [{"name": company.name, "custom_fields_values": fields or None}]
        return await self._first_id("POST", "/api/v4/companies", "companies", body)

    async def find_contact(self, contact: ContactIn) -> str | None:
        if contact.email:
            hits = await self._search("/api/v4/contacts", "contacts", contact.email.strip())
            ids = [
                int(c["id"])
                for c in hits
                if any(same_email(v, contact.email) for v in _field_values(c, code="EMAIL"))
            ]
            if ids:
                return str(min(ids))
        phone = digits(contact.phone)
        if len(phone) >= 7:
            hits = await self._search("/api/v4/contacts", "contacts", phone[-9:])
            ids = [
                int(c["id"])
                for c in hits
                if any(same_phone(v, phone) for v in _field_values(c, code="PHONE"))
            ]
            if ids:
                return str(min(ids))
        return None

    async def link_contact(self, contact_id: str, company_id: str) -> bool:
        found = await self._request("GET", f"/api/v4/contacts/{record_id(contact_id, self.kind.value)}")
        if not found:
            raise CrmNotFound(f"contact {contact_id} not found", crm=self.kind.value, status=204)
        if found.get("_embedded", {}).get("companies"):
            return False
        link = [{"to_entity_id": int(record_id(company_id, self.kind.value)), "to_entity_type": "companies"}]
        await self._request("POST", f"/api/v4/contacts/{int(contact_id)}/link", json=link)
        return True

    async def create_contact(self, contact: ContactIn, *, company_id: str | None = None) -> str:
        fields: list[dict[str, Any]] = []
        if contact.email:
            fields.append(
                {"field_code": "EMAIL", "values": [{"value": contact.email.strip(), "enum_code": "WORK"}]}
            )
        if contact.phone:
            fields.append({"field_code": "PHONE", "values": [{"value": contact.phone, "enum_code": "WORK"}]})
        item: dict[str, Any] = {"name": contact.name, "custom_fields_values": fields or None}
        if company_id:
            item["_embedded"] = {"companies": [{"id": int(company_id)}]}
        return await self._first_id("POST", "/api/v4/contacts", "contacts", [item])

    # --- catalog ---

    def _element_body(
        self,
        name: str,
        price: Decimal,
        sku: str | None,
        price_field: int | None,
        *,
        unit: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        fields: list[dict[str, Any]] = []
        if price_field:
            fields.append({"field_id": price_field, "values": [{"value": float(price)}]})
        if sku:
            fields.append({"field_code": "SKU", "values": [{"value": sku}]})
        if unit and unit in self.UNITS:
            fields.append({"field_code": "UNIT", "values": [{"value": self.UNITS[unit]}]})
        if description:
            fields.append({"field_code": "DESCRIPTION", "values": [{"value": description}]})
        return {"name": name, "custom_fields_values": fields or None}

    async def create_product(self, product: ProductIn) -> str:
        cat, price_field = await self._catalog(), await self._price_field_id()
        body = [
            self._element_body(
                product.name,
                product.price,
                product.sku,
                price_field,
                unit=product.unit,
                description=product.description,
            )
        ]
        return await self._first_id("POST", f"/api/v4/catalogs/{cat}/elements", "elements", body)

    def _product(self, element: dict[str, Any], currency: str | None) -> Product:
        prices = _field_values(element, code="PRICE")
        skus = _field_values(element, code="SKU")
        units = _field_values(element, code="UNIT")
        about = _field_values(element, code="DESCRIPTION")
        return Product(
            id=str(element["id"]),
            name=str(element.get("name", "")),
            sku=skus[0] if skus else None,
            price=dec(prices[0]) if prices else None,
            currency=currency,
            unit=units[0] if units else None,
            description=about[0] if about else None,
        )

    async def list_products(
        self, *, query: str | None = None, cursor: str | None = None, limit: int = 50
    ) -> Page[Product]:
        cat = await self._catalog()
        params: dict[str, Any] = {"page": int(cursor or 1), "limit": min(limit, 250)}
        if query:
            params["query"] = query
        data = await self._request("GET", f"/api/v4/catalogs/{cat}/elements", params=params)
        if not data:
            return Page(())
        currency = await self._currency()
        items = tuple(self._product(e, currency) for e in data.get("_embedded", {}).get("elements", []))
        has_next = bool(data.get("_links", {}).get("next"))
        return Page(items, str(params["page"] + 1) if has_next else None)

    async def _elements(self, ids: Iterable[int]) -> dict[int, dict[str, Any]]:
        cat, out = await self._catalog(), {}
        id_list = sorted(set(ids))
        for i in range(0, len(id_list), 50):
            params = {"filter[id][]": id_list[i : i + 50], "limit": 250}
            for e in await self._embedded(
                "GET", f"/api/v4/catalogs/{cat}/elements", "elements", params=params
            ):
                out[int(e["id"])] = e
        return out

    # --- deals ---

    async def _check_deal(self, deal: DealIn) -> None:
        """amoCRM keeps one currency per account, one link per product and the price on the
        product: a deal that needs more is refused instead of being written wrong."""
        crm = self.kind.value
        account_currency = await self._currency()
        if deal.currency and account_currency and deal.currency.upper() != account_currency.upper():
            raise CrmUnsupported(
                f"the account currency is {account_currency}; a deal in {deal.currency} cannot be kept",
                crm=crm,
            )
        product_ids = [record_id(x.product_id, crm) for x in deal.lines if x.product_id]
        if len(product_ids) != len(set(product_ids)):
            raise CrmUnsupported("a product is linked to a deal once: merge its lines", crm=crm)
        if product_ids:
            elements = await self._elements(int(i) for i in product_ids)
            for line in deal.lines:
                if not line.product_id:
                    continue
                prices = _field_values(elements.get(int(line.product_id), {}), code="PRICE")
                catalog = dec(prices[0]) if prices else None
                if catalog != line.unit_price:
                    raise CrmUnsupported(
                        f"line {line.name!r}: price {line.unit_price} differs from "
                        f"the catalog price {catalog}; "
                        "amoCRM keeps the price on the product",
                        crm=crm,
                    )

    async def create_deal(self, deal: DealIn) -> CreatedDeal:
        cat = await self._catalog() if deal.lines else None
        price_field = await self._price_field_id() if deal.lines else None
        await self._check_deal(deal)
        new_lines = [line for line in deal.lines if not line.product_id]
        created: list[str] = []
        if new_lines:
            body = [self._element_body(x.name, x.unit_price, x.sku, price_field) for x in new_lines]
            items = await self._embedded("POST", f"/api/v4/catalogs/{cat}/elements", "elements", json=body)
            created = [str(e["id"]) for e in items]
        embedded: dict[str, Any] = {"tags": [{"name": t} for t in deal.tags]}
        if deal.contact_id:
            embedded["contacts"] = [{"id": int(deal.contact_id)}]
        if deal.company_id:
            embedded["companies"] = [{"id": int(deal.company_id)}]
        lead = {"name": deal.title, "price": int(deal.amount.to_integral_value()), "_embedded": embedded}
        deal_id = await self._first_id("POST", "/api/v4/leads", "leads", [lead])
        quantities: dict[int, Decimal] = {}
        new_ids = iter(created)
        for line in deal.lines:
            pid = int(line.product_id) if line.product_id else int(next(new_ids))
            quantities[pid] = line.quantity
        if quantities:
            links = []
            for pid, qty in quantities.items():
                meta: dict[str, Any] = {"quantity": float(qty), "catalog_id": cat}
                if price_field:
                    meta["price_id"] = price_field
                links.append({"to_entity_id": pid, "to_entity_type": "catalog_elements", "metadata": meta})
            await self._request("POST", f"/api/v4/leads/{deal_id}/link", json=links)
            if deal.total is not None:
                # linking products with a price field recounts the budget at catalog prices: the
                # total after the company's rules goes back on top
                await self._request("PATCH", f"/api/v4/leads/{deal_id}", json={"price": lead["price"]})
        return CreatedDeal(deal_id, tuple(created))

    def _deal(self, lead: dict[str, Any], currency: str | None, lines: tuple[DealLine, ...] = ()) -> Deal:
        status_id = int(lead.get("status_id") or 0)
        status = (
            DealStatus.WON if status_id == WON else DealStatus.LOST if status_id == LOST else DealStatus.OPEN
        )
        return Deal(
            id=str(lead["id"]),
            title=str(lead.get("name", "")),
            amount=dec(lead.get("price")),
            currency=currency,
            status=status,
            created_at=_ts(lead.get("created_at")),
            closed_at=_ts(lead.get("closed_at")),
            lines=lines,
            stage_id=_stage_id(lead),
        )

    async def get_deal(self, deal_id: str) -> Deal:
        lead = await self._request(
            "GET", f"/api/v4/leads/{int(deal_id)}", params={"with": "catalog_elements"}
        )
        if not lead:
            raise CrmNotFound(f"deal {deal_id} not found", crm=self.kind.value, status=204)
        links = lead.get("_embedded", {}).get("catalog_elements", [])
        elements = await self._elements(int(x["id"]) for x in links) if links else {}
        lines = []
        for link in links:
            element = elements.get(int(link["id"]), {})
            prices = _field_values(element, code="PRICE")
            skus = _field_values(element, code="SKU")
            lines.append(
                DealLine(
                    name=str(element.get("name", "")),
                    quantity=Decimal(str(link.get("metadata", {}).get("quantity", 0))),
                    unit_price=dec(prices[0]) or Decimal(0) if prices else Decimal(0),
                    product_id=str(link["id"]),
                    sku=skus[0] if skus else None,
                )
            )
        return self._deal(lead, await self._currency(), tuple(lines))

    async def deal_history(
        self, *, company_id: str | None = None, contact_id: str | None = None, limit: int = 200
    ) -> list[Deal]:
        require_one(company_id, contact_id, self.kind.value)
        path = (
            f"/api/v4/companies/{int(company_id)}"
            if company_id
            else f"/api/v4/contacts/{int(contact_id or 0)}"
        )
        owner = await self._request("GET", path, params={"with": "leads"})
        if not owner:
            raise CrmNotFound("client not found", crm=self.kind.value, status=204)
        ids = [int(x["id"]) for x in owner.get("_embedded", {}).get("leads", [])][:limit]
        currency = await self._currency()
        deals: list[Deal] = []
        for i in range(0, len(ids), 50):
            params = {"filter[id][]": ids[i : i + 50], "limit": 250}
            deals += [
                self._deal(x, currency)
                for x in await self._embedded("GET", "/api/v4/leads", "leads", params=params)
            ]
        return sorted(
            deals, key=lambda d: (d.created_at or datetime.min.replace(tzinfo=UTC), d.id), reverse=True
        )

    # --- files, notes, tasks ---

    async def attach_file(self, deal_id: str, file: FileIn) -> str:
        drive = str((await self._account_info()).get("drive_url") or "").rstrip("/")
        if not _platform_url(drive):
            raise CrmUnsupported("the account has no file storage", crm=self.kind.value)
        session = await self._request(
            "POST",
            f"{drive}/v1.0/sessions",
            json={"file_name": file.name, "file_size": len(file.content), "content_type": file.mime_type},
        )
        upload_url = str(session.get("upload_url", ""))
        part = int(session.get("max_part_size") or 524288)
        result: dict[str, Any] = {}
        for start in range(0, max(len(file.content), 1), part):
            # the token goes to this address too, so it has to be the CRM's own host
            if not _platform_url(upload_url):
                raise CrmError("file storage answered an unexpected upload address", crm=self.kind.value)
            result = await self._request("POST", upload_url, content=file.content[start : start + part]) or {}
            upload_url = str(result.get("next_url") or upload_url)
        file_uuid = str(result.get("uuid", ""))
        if not file_uuid:
            raise CrmError("file storage did not return the file id", crm=self.kind.value)
        await self._request("PUT", f"/api/v4/leads/{int(deal_id)}/files", json=[{"file_uuid": file_uuid}])
        return file_uuid

    async def add_note(self, deal_id: str, text: str) -> str:
        body = [{"entity_id": int(deal_id), "note_type": "common", "params": {"text": text}}]
        return await self._first_id("POST", "/api/v4/leads/notes", "notes", body)

    async def add_task(self, deal_id: str, task: TaskIn) -> str:
        item: dict[str, Any] = {
            "text": task.text,
            "complete_till": int(task.due.timestamp()),
            "entity_id": int(deal_id),
            "entity_type": "leads",
            "task_type_id": 1,
        }
        if task.responsible_id:
            item["responsible_user_id"] = int(task.responsible_id)
        return await self._first_id("POST", "/api/v4/tasks", "tasks", [item])

    # --- the voice of a manager: find a deal, move it, add products ---

    async def stages(self) -> list[Stage]:
        pipelines = await self._embedded("GET", "/api/v4/leads/pipelines", "pipelines")
        out: list[Stage] = []
        for pipe in sorted(pipelines, key=lambda p: (not p.get("is_main"), int(p.get("sort") or 0))):
            for st in sorted(
                pipe.get("_embedded", {}).get("statuses", []), key=lambda x: int(x.get("sort") or 0)
            ):
                if (
                    int(st.get("type") or 0) == 1
                ):  # «Неразобранное»: leads land there, nobody moves them there
                    continue
                sid = int(st["id"])
                out.append(
                    Stage(
                        f"{int(pipe['id'])}:{sid}",
                        str(st.get("name", "")),
                        _status(sid),
                        str(pipe.get("name") or ""),
                    )
                )
        return out

    async def _names(self, path: str, key: str, ids: set[int]) -> dict[int, str]:
        if not ids:
            return {}
        params = {"filter[id][]": sorted(ids)[:50], "limit": 50}
        return {
            int(x["id"]): str(x.get("name") or "")
            for x in await self._embedded("GET", path, key, params=params)
        }

    async def _leads_by_ids(self, ids: list[int]) -> list[dict[str, Any]]:
        if not ids:
            return []
        params = {"filter[id][]": ids[:50], "limit": 50, "with": "contacts"}
        found = {
            int(x["id"]): x for x in await self._embedded("GET", "/api/v4/leads", "leads", params=params)
        }
        return [found[i] for i in ids if i in found]

    async def find_deals(self, query: str, *, limit: int = 8) -> list[DealHit]:
        text = search_text(query)
        if len(text) < 2:
            return []
        groups: list[tuple[str, list[dict[str, Any]]]] = []
        data = await self._request(
            "GET", "/api/v4/leads", params={"query": text, "limit": 50, "with": "contacts"}
        )
        groups.append(("title", list((data or {}).get("_embedded", {}).get("leads", []))))
        for path, key, by in (
            ("/api/v4/companies", "companies", "company"),
            ("/api/v4/contacts", "contacts", "contact"),
        ):
            data = await self._request("GET", path, params={"query": text, "limit": 10, "with": "leads"})
            ids: list[int] = []
            for owner in (data or {}).get("_embedded", {}).get(key, []):
                ids += [int(x["id"]) for x in owner.get("_embedded", {}).get("leads", [])]
            groups.append((by, await self._leads_by_ids(list(dict.fromkeys(ids))[: limit * 2])))
        leads = [(by, lead) for by, group in groups for lead in group]
        company_ids = {int(c["id"]) for _, ld in leads for c in ld.get("_embedded", {}).get("companies", [])}
        contact_ids = {
            int(c["id"]) for _, ld in leads for c in ld.get("_embedded", {}).get("contacts", [])[:1]
        }
        companies = await self._names("/api/v4/companies", "companies", company_ids)
        contacts = await self._names("/api/v4/contacts", "contacts", contact_ids)

        def hit(by: str, lead: dict[str, Any]) -> DealHit:
            emb = lead.get("_embedded", {})
            co = next((companies.get(int(c["id"])) for c in emb.get("companies", [])), None)
            ct = next((contacts.get(int(c["id"])) for c in emb.get("contacts", [])), None)
            return DealHit(
                str(lead["id"]),
                str(lead.get("name") or ""),
                co,
                ct,
                _stage_id(lead),
                dec(lead.get("price")),
                _status(int(lead.get("status_id") or 0)),
                by,
            )

        return hits_in_order([[hit(by, ld) for ld in group] for by, group in groups], limit)

    async def update_deal(self, deal_id: str, update: DealUpdate) -> None:
        crm = self.kind.value
        positive(update, crm)
        if update.close_date is not None:
            raise CrmUnsupported("amoCRM keeps no planned closing date of a deal", crm=crm)
        body: dict[str, Any] = {}
        if update.stage_id is not None:
            pipeline, _, status = update.stage_id.partition(":")
            body["pipeline_id"] = int(record_id(pipeline, crm))
            body["status_id"] = int(record_id(status, crm))
        if update.amount is not None:
            body["price"] = int(update.amount.to_integral_value())
        if body:
            await self._request("PATCH", f"/api/v4/leads/{int(record_id(deal_id, crm))}", json=body)

    async def add_lines(self, deal_id: str, lines: tuple[DealLine, ...]) -> None:
        crm = self.kind.value
        catalog_lines(lines, crm)
        cat, price_field = await self._catalog(), await self._price_field_id()
        have = {line.product_id: line.quantity for line in (await self.get_deal(deal_id)).lines}
        # a link of a product the deal already has replaces its quantity (checked live): add to it
        quantities: dict[str, Decimal] = {}
        for line in lines:
            pid = str(line.product_id)
            quantities[pid] = quantities.get(pid, have.get(pid, Decimal(0))) + line.quantity
        links = []
        for pid, qty in quantities.items():
            meta: dict[str, Any] = {"quantity": float(qty), "catalog_id": cat}
            if price_field:
                meta["price_id"] = price_field
            links.append({"to_entity_id": int(pid), "to_entity_type": "catalog_elements", "metadata": meta})
        await self._request("POST", f"/api/v4/leads/{int(record_id(deal_id, crm))}/link", json=links)

    # --- cleanup ---

    async def discard(self, leftovers: Leftovers) -> None:
        tag = [{"name": E2E_TAG}]
        if leftovers.task_ids:
            body = [
                {"id": int(t), "is_completed": True, "result": {"text": E2E_TAG}} for t in leftovers.task_ids
            ]
            await self._request("PATCH", "/api/v4/tasks", json=body)
        if leftovers.deal_ids:
            body = [{"id": int(i), "status_id": LOST, "tags_to_add": tag} for i in leftovers.deal_ids]
            await self._request("PATCH", "/api/v4/leads", json=body)
        if leftovers.contact_ids:
            body = [{"id": int(i), "tags_to_add": tag} for i in leftovers.contact_ids]
            await self._request("PATCH", "/api/v4/contacts", json=body)
        if leftovers.company_ids:
            body = [{"id": int(i), "tags_to_add": tag} for i in leftovers.company_ids]
            await self._request("PATCH", "/api/v4/companies", json=body)


class Kommo(AmoCRM):
    """Kommo, the international amoCRM: same API, own servers, ``*.kommo.com``."""

    kind: ClassVar[CrmKind] = CrmKind.KOMMO
    UNITS: ClassVar[dict[str, str]] = {"шт": "pc", "кг": "kg", "л": "l"}
