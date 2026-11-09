"""Odoo (Community or Enterprise, 16+) over JSON-RPC ``execute_kw`` with a user API key.

What differs from other CRMs, checked live on Odoo 19:
- a company is a ``res.partner`` with ``is_company``; its INN is ``vat``; a contact is a partner
  with ``parent_id``;
- an opportunity (``crm.lead``) has no lines: lines go to a quotation (``sale.order``) linked by
  ``opportunity_id``, which needs the ``sale_crm`` module, and each line needs a product;
- a file is an ``ir.attachment`` on the lead, posted to its chatter; a note is a chatter note;
  a task is a "To-Do" activity;
- records are deleted with ``unlink``.

The Odoo address is typed in by the user, so by default every connection goes through the
network guard (public addresses only, https). JSON-RPC stays available up to Odoo 21; Odoo 22
moves to the JSON-2 API.
"""

import base64
import contextlib
import itertools
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, ClassVar
from urllib.parse import quote

import httpx

from alnuzum_crm.base import (
    CRM,
    E2E_TAG,
    dec,
    digits,
    record_id,
    require_one,
    same_email,
    same_name,
    same_phone,
)
from alnuzum_crm.errors import (
    CrmAuthError,
    CrmError,
    CrmNotFound,
    CrmPermissionError,
    CrmUnavailable,
    CrmUnsupported,
    CrmValidationError,
)
from alnuzum_crm.http import HttpCore, RetryPolicy, shared_limiter
from alnuzum_crm.netguard import guarded_transport, split_https_url
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

_ERRORS: dict[str, type[CrmError]] = {
    "AccessDenied": CrmAuthError,
    "AccessError": CrmPermissionError,
    "MissingError": CrmNotFound,
    "ValidationError": CrmValidationError,
    "UserError": CrmValidationError,
}
_ids = itertools.count(1)


def like_literal(value: str) -> str:
    """A value for ``=ilike`` that matches only itself: % _ and backslash are escaped."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value)).replace(tzinfo=UTC)


class Odoo(CRM):
    kind: ClassVar[CrmKind] = CrmKind.ODOO
    capabilities: ClassVar[frozenset[str]] = frozenset({"inn", "fractional_amount", "delete", "line_prices"})
    #: our units to the external ids of Odoo's standard units of measure
    UNITS: ClassVar[dict[str, str]] = {
        "шт": "product_uom_unit",
        "м": "product_uom_meter",
        "кг": "product_uom_kgm",
        "л": "product_uom_litre",
    }

    def __init__(
        self,
        url: str,
        database: str,
        login: str,
        api_key: str,
        *,
        trusted_url: bool = False,
        rate: float = 10,
        retry: RetryPolicy | None = None,
        timeout: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """``trusted_url`` skips the https and public-address checks: only for an address the
        operator configured (a local test Odoo), never for one a user typed in."""
        if trusted_url:
            self.url = url.rstrip("/")
        else:
            _, _, self.url = split_https_url(url)
            transport = transport or guarded_transport()
        self.database = database
        self._login = login
        self._key = api_key
        self._uid: int | None = None
        self._core = HttpCore(
            self.kind.value,
            limiter=shared_limiter(f"odoo:{self.url}:{database}", rate),
            retry=retry,
            timeout=timeout,
            transport=transport,
        )
        self._cache: dict[str, Any] = {}

    def __repr__(self) -> str:
        return f"Odoo(url={self.url!r}, database={self.database!r})"

    async def aclose(self) -> None:
        await self._core.aclose()

    # --- transport ---

    async def _rpc(self, service: str, method: str, *args: Any) -> Any:
        body = {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {"service": service, "method": method, "args": list(args)},
            "id": next(_ids),
        }
        resp = await self._core.send("POST", f"{self.url}/jsonrpc", json=body)
        crm, status = self.kind.value, resp.status_code
        if status == 404:
            raise CrmUnsupported("this Odoo has no JSON-RPC endpoint", crm=crm, status=status)
        if status >= 500:
            raise CrmUnavailable(f"answered {status}", crm=crm, status=status)
        if status >= 300:
            raise CrmError(f"answered {status}", crm=crm, status=status)
        try:
            data = resp.json()
        except ValueError:
            raise CrmError("answered without JSON", crm=crm, status=status) from None
        error = data.get("error") if isinstance(data, dict) else None
        if error:
            info = error.get("data") or {}
            name = str(info.get("name", ""))
            text = str(info.get("message") or error.get("message") or "error")[:300]
            cls = _ERRORS.get(name.rsplit(".", 1)[-1], CrmError)
            raise cls(text, crm=crm, status=status, code=name or None)
        return data.get("result") if isinstance(data, dict) else None

    async def account(self) -> AccountInfo:
        self._uid = None  # a liveness probe: authenticate again
        uid = await self._uid_()
        return AccountInfo(f"{self.url}|{self.database}|{uid}", self.database, self.url)

    def deal_link(self, account: AccountInfo, deal_id: str) -> str:
        db = quote(self.database, safe="")
        lead = record_id(deal_id, self.kind.value)
        return f"{account.web}/web?db={db}#id={lead}&model=crm.lead&view_type=form"

    async def _uid_(self) -> int:
        if self._uid is None:
            uid = await self._rpc("common", "authenticate", self.database, self._login, self._key, {})
            if not uid:
                raise CrmAuthError("login or API key refused", crm=self.kind.value, status=200)
            self._uid = int(uid)
        return self._uid

    async def kw(self, model: str, method: str, args: list[Any], kwargs: dict[str, Any] | None = None) -> Any:
        uid = await self._uid_()
        return await self._rpc(
            "object", "execute_kw", self.database, uid, self._key, model, method, args, kwargs or {}
        )

    async def _read(
        self, model: str, domain: list[Any], fields: list[str], **kw: Any
    ) -> list[dict[str, Any]]:
        rows = await self.kw(model, "search_read", [domain], {"fields": fields, **kw})
        return list(rows or [])

    async def _search(self, model: str, domain: list[Any], **kw: Any) -> list[int]:
        return [int(i) for i in await self.kw(model, "search", [domain], kw) or []]

    async def _xmlid(self, module: str, name: str) -> int:
        key = f"xmlid:{module}.{name}"
        if key not in self._cache:
            rows = await self._read(
                "ir.model.data", [["module", "=", module], ["name", "=", name]], ["res_id"]
            )
            if not rows:
                raise CrmUnsupported(f"{module}.{name} is missing", crm=self.kind.value)
            self._cache[key] = int(rows[0]["res_id"])
        return int(self._cache[key])

    async def _has_model_field(self, model: str, field: str) -> bool:
        key = f"field:{model}.{field}"
        if key not in self._cache:
            self._cache[key] = bool(
                await self._search("ir.model.fields", [["model", "=", model], ["name", "=", field]], limit=1)
            )
        return bool(self._cache[key])

    # --- companies and contacts ---

    async def find_company(self, company: CompanyIn) -> str | None:
        if company.inn:
            want = digits(company.inn)
            rows = await self._read(
                "res.partner", [["is_company", "=", True], ["vat", "ilike", want]], ["vat"], order="id"
            )
            ids = [r["id"] for r in rows if digits(r.get("vat") or "") == want]
            if ids:
                return str(ids[0])
        # =ilike with escaped wildcards, then an exact comparison: a pattern never merges records
        domain = [["is_company", "=", True], ["name", "=ilike", like_literal(company.name.strip())]]
        rows = await self._read("res.partner", domain, ["name", "vat"], order="id", limit=50)
        ids = [
            r["id"]
            for r in rows
            if same_name(str(r.get("name") or ""), company.name)
            # a name match counts only when the company has no INN or the same one
            and (not company.inn or digits(r.get("vat") or "") in ("", digits(company.inn)))
        ]
        return str(ids[0]) if ids else None

    async def create_company(self, company: CompanyIn) -> str:
        vals: dict[str, Any] = {"name": company.name, "is_company": True}
        if company.inn:
            vals["vat"] = digits(company.inn)
        if company.phone:
            vals["phone"] = company.phone
        if company.email:
            vals["email"] = company.email
        return str(await self.kw("res.partner", "create", [vals]))

    async def find_contact(self, contact: ContactIn) -> str | None:
        if contact.email:
            domain = [["is_company", "=", False], ["email", "=ilike", like_literal(contact.email.strip())]]
            rows = await self._read("res.partner", domain, ["email"], order="id", limit=50)
            ids = [r["id"] for r in rows if same_email(str(r.get("email") or ""), contact.email)]
            if ids:
                return str(ids[0])
        phone = digits(contact.phone)
        if len(phone) >= 7:
            # Odoo keeps the phone as typed: search the last digits, then compare whole numbers
            tail = "%".join(phone[-7:])
            rows = await self._read(
                "res.partner",
                [["is_company", "=", False], ["phone", "=like", f"%{tail}%"]],
                ["phone"],
                order="id",
            )
            ids = [r["id"] for r in rows if same_phone(r.get("phone") or "", phone)]
            if ids:
                return str(ids[0])
        return None

    async def link_contact(self, contact_id: str, company_id: str) -> bool:
        crm = self.kind.value
        rows = await self._read("res.partner", [["id", "=", int(record_id(contact_id, crm))]], ["parent_id"])
        if not rows:
            raise CrmNotFound(f"contact {contact_id} not found", crm=crm)
        if rows[0].get("parent_id"):
            return False
        await self.kw(
            "res.partner", "write", [[int(contact_id)], {"parent_id": int(record_id(company_id, crm))}]
        )
        return True

    async def server_version(self) -> str:
        """Odoo version; needs no login (used to check that the address is an Odoo)."""
        info = await self._rpc("common", "version")
        return str((info or {}).get("server_version", ""))

    async def create_contact(self, contact: ContactIn, *, company_id: str | None = None) -> str:
        vals: dict[str, Any] = {"name": contact.name, "is_company": False}
        if contact.email:
            vals["email"] = contact.email.strip()
        if contact.phone:
            vals["phone"] = contact.phone
        if company_id:
            vals["parent_id"] = int(company_id)
        return str(await self.kw("res.partner", "create", [vals]))

    # --- catalog ---

    async def create_product(self, product: ProductIn) -> str:
        vals: dict[str, Any] = {"name": product.name, "list_price": float(product.price), "sale_ok": True}
        if product.sku:
            vals["default_code"] = product.sku
        if product.description:
            vals["description_sale"] = product.description
        if product.unit and product.unit in self.UNITS:
            with contextlib.suppress(CrmUnsupported):
                vals["uom_id"] = await self._xmlid("uom", self.UNITS[product.unit])
        if product.category:
            found = await self._search("product.category", [["name", "=", product.category]], limit=1)
            vals["categ_id"] = (
                found[0]
                if found
                else int(await self.kw("product.category", "create", [{"name": product.category}]))
            )
        return str(await self.kw("product.product", "create", [vals]))

    async def list_products(
        self, *, query: str | None = None, cursor: str | None = None, limit: int = 50
    ) -> Page[Product]:
        domain: list[Any] = [["sale_ok", "=", True]]
        if query:
            domain = ["&", *domain, "|", ["name", "ilike", query], ["default_code", "ilike", query]]
        offset = int(cursor or 0)
        fields = [
            "name",
            "default_code",
            "list_price",
            "currency_id",
            "uom_id",
            "categ_id",
            "description_sale",
        ]
        # stock exists only with the Inventory app
        stocked = await self._has_model_field("product.product", "qty_available")
        if stocked:
            fields.append("qty_available")
        rows = await self._read("product.product", domain, fields, offset=offset, limit=limit, order="id")
        items = tuple(
            Product(
                id=str(r["id"]),
                name=str(r.get("name", "")),
                sku=r.get("default_code") or None,
                price=dec(r.get("list_price")),
                currency=r["currency_id"][1] if r.get("currency_id") else None,
                unit=r["uom_id"][1] if r.get("uom_id") else None,
                category=r["categ_id"][1] if r.get("categ_id") else None,
                description=r.get("description_sale") or None,
                stock=dec(r.get("qty_available")) if stocked else None,
            )
            for r in rows
        )
        return Page(items, str(offset + limit) if len(rows) == limit else None)

    # --- deals ---

    async def _tag_ids(self, names: tuple[str, ...]) -> list[int]:
        ids: list[int] = []
        for name in names:
            found = await self._search("crm.tag", [["name", "=", name]], limit=1)
            ids.append(found[0] if found else int(await self.kw("crm.tag", "create", [{"name": name}])))
        return ids

    async def _pricelist_for(self, currency: str | None) -> int | None:
        """None for the company currency, a pricelist in ``currency`` otherwise, or a refusal."""
        if not currency:
            return None
        crm = self.kind.value
        found = await self._search(
            "res.currency", [["name", "=", currency.upper()]], limit=1, context={"active_test": False}
        )
        if not found:
            raise CrmUnsupported(f"currency {currency} is not known to this Odoo", crm=crm)
        company = await self._read("res.company", [], ["currency_id"], limit=1)
        if company and company[0].get("currency_id") and company[0]["currency_id"][0] == found[0]:
            return None
        lists = await self._search("product.pricelist", [["currency_id", "=", found[0]]], limit=1)
        if not lists:
            raise CrmUnsupported(
                f"no pricelist in {currency}: add one in Sales or use the company currency", crm=crm
            )
        return lists[0]

    async def create_deal(self, deal: DealIn) -> CreatedDeal:
        partner = deal.contact_id or deal.company_id
        pricelist = await self._pricelist_for(deal.currency)
        if pricelist and not deal.lines:
            raise CrmUnsupported(
                f"an opportunity keeps the company currency; {deal.currency} needs lines (a quotation)",
                crm=self.kind.value,
            )
        if deal.lines and not await self._has_model_field("sale.order", "opportunity_id"):
            raise CrmUnsupported(
                "deal lines need the Sales and CRM link (module sale_crm)", crm=self.kind.value
            )
        if deal.lines and not partner:
            raise CrmValidationError(
                "a quotation needs a customer: pass company_id or contact_id", crm=self.kind.value
            )
        vals: dict[str, Any] = {
            "name": deal.title,
            "type": "opportunity",
            "expected_revenue": float(deal.amount),
        }
        if partner:
            vals["partner_id"] = int(partner)
        if deal.tags:
            vals["tag_ids"] = [[6, 0, await self._tag_ids(deal.tags)]]
        lead_id = int(await self.kw("crm.lead", "create", [vals]))
        created: list[str] = []
        if deal.lines:
            order_lines = []
            for line in deal.lines:
                pid = line.product_id or await self.create_product(
                    ProductIn(line.name, line.unit_price, line.sku)
                )
                if not line.product_id:
                    created.append(pid)
                values = {
                    "product_id": int(pid),
                    "product_uom_qty": float(line.quantity),
                    "price_unit": float(line.unit_price),
                    "name": line.name,
                }
                order_lines.append([0, 0, values])
            order: dict[str, Any] = {
                "partner_id": int(partner or 0),
                "opportunity_id": lead_id,
                "order_line": order_lines,
            }
            if pricelist:
                order["pricelist_id"] = pricelist
            await self.kw("sale.order", "create", [order])
        return CreatedDeal(str(lead_id), tuple(created))

    async def _deal(self, row: dict[str, Any], lines: tuple[DealLine, ...] = ()) -> Deal:
        if row.get("won_status"):
            ws = row["won_status"]
            status = DealStatus.WON if ws == "won" else DealStatus.LOST if ws == "lost" else DealStatus.OPEN
        else:  # Odoo before 17 has no won_status
            won = float(row.get("probability") or 0) >= 100 and row.get("active", True)
            status = (
                DealStatus.WON if won else DealStatus.OPEN if row.get("active", True) else DealStatus.LOST
            )
        currency = row.get("company_currency")
        return Deal(
            id=str(row["id"]),
            title=str(row.get("name", "")),
            amount=dec(row.get("expected_revenue")),
            currency=currency[1] if isinstance(currency, list) else None,
            status=status,
            created_at=_dt(row.get("create_date")),
            closed_at=_dt(row.get("date_closed")),
            lines=lines,
        )

    async def _lead_fields(self) -> list[str]:
        fields = [
            "name",
            "expected_revenue",
            "probability",
            "active",
            "create_date",
            "date_closed",
            "company_currency",
        ]
        if await self._has_model_field("crm.lead", "won_status"):
            fields.append("won_status")
        return fields

    async def get_deal(self, deal_id: str) -> Deal:
        ctx = {"context": {"active_test": False}}
        rows = await self._read("crm.lead", [["id", "=", int(deal_id)]], await self._lead_fields(), **ctx)
        if not rows:
            raise CrmNotFound(f"deal {deal_id} not found", crm=self.kind.value)
        lines: tuple[DealLine, ...] = ()
        if await self._has_model_field("sale.order", "opportunity_id"):
            orders = await self._search(
                "sale.order", [["opportunity_id", "=", int(deal_id)], ["state", "!=", "cancel"]]
            )
            if orders:
                fields = ["name", "product_id", "product_uom_qty", "price_unit", "display_type"]
                rows_l = await self._read("sale.order.line", [["order_id", "in", orders]], fields, order="id")
                lines = tuple(
                    DealLine(
                        name=str(r.get("name", "")),
                        quantity=Decimal(str(r.get("product_uom_qty", 0))),
                        unit_price=Decimal(str(r.get("price_unit", 0))),
                        product_id=str(r["product_id"][0]) if r.get("product_id") else None,
                    )
                    for r in rows_l
                    if not r.get("display_type")
                )
        return await self._deal(rows[0], lines)

    async def deal_history(
        self, *, company_id: str | None = None, contact_id: str | None = None, limit: int = 200
    ) -> list[Deal]:
        require_one(company_id, contact_id, self.kind.value)
        partner = int(company_id or contact_id or 0)
        rows = await self._read(
            "crm.lead",
            [["partner_id", "child_of", partner]],
            await self._lead_fields(),
            order="create_date desc, id desc",
            limit=limit,
            context={"active_test": False},
        )
        return [await self._deal(r) for r in rows]

    # --- files, notes, tasks ---

    async def _post(self, deal_id: str, body: str, attachment_ids: list[int] | None = None) -> str:
        kwargs: dict[str, Any] = {"body": body, "message_type": "comment", "subtype_xmlid": "mail.mt_note"}
        if attachment_ids:
            kwargs["attachment_ids"] = attachment_ids
        result = await self.kw("crm.lead", "message_post", [[int(deal_id)]], kwargs)
        return str(result[0] if isinstance(result, list) else result)

    async def attach_file(self, deal_id: str, file: FileIn) -> str:
        vals = {
            "name": file.name,
            "datas": base64.b64encode(file.content).decode(),
            "res_model": "crm.lead",
            "res_id": int(deal_id),
            "mimetype": file.mime_type,
        }
        att = int(await self.kw("ir.attachment", "create", [vals]))
        await self._post(deal_id, file.name, [att])
        return str(att)

    async def add_note(self, deal_id: str, text: str) -> str:
        return await self._post(deal_id, text)

    async def add_task(self, deal_id: str, task: TaskIn) -> str:
        model_ids = await self._search("ir.model", [["model", "=", "crm.lead"]], limit=1)
        vals = {
            "res_model_id": model_ids[0],
            "res_id": int(deal_id),
            "activity_type_id": await self._xmlid("mail", "mail_activity_data_todo"),
            "summary": task.text[:250],
            "note": task.text,
            "date_deadline": task.due.date().isoformat(),
            "user_id": int(task.responsible_id) if task.responsible_id else await self._uid_(),
        }
        return str(await self.kw("mail.activity", "create", [vals]))

    # --- cleanup ---

    async def discard(self, leftovers: Leftovers) -> None:
        leads = [int(i) for i in leftovers.deal_ids]
        if leads and await self._has_model_field("sale.order", "opportunity_id"):
            orders = await self._search("sale.order", [["opportunity_id", "in", leads]])
            if orders:
                await self.kw("sale.order", "action_cancel", [orders])
                await self.kw("sale.order", "unlink", [orders])
        for model, ids in (
            ("crm.lead", leads),
            ("res.partner", [int(i) for i in leftovers.contact_ids]),
            ("res.partner", [int(i) for i in leftovers.company_ids]),
            ("product.product", [int(i) for i in leftovers.product_ids]),
        ):
            existing = (
                await self._search(model, [["id", "in", ids]], context={"active_test": False}) if ids else []
            )
            if existing:
                await self.kw(model, "unlink", [existing])
        tags = await self._search("crm.tag", [["name", "=", E2E_TAG]])
        if tags and not await self._search(
            "crm.lead", [["tag_ids", "in", tags]], context={"active_test": False}
        ):
            await self.kw("crm.tag", "unlink", [tags])
