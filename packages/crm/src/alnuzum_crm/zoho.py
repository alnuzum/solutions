"""Zoho CRM (API v8) with OAuth tokens of any Zoho data centre.

What differs from other CRMs, checked live:
- accounts have no INN field; a custom field labelled INN/ИНН/TIN/STIR is used when it exists;
- the search index lags behind writes (a record created a second ago is not found for some
  seconds), so a miss falls back to ``upsert`` on the system duplicate-check field
  (``Account_Name``, ``Email``), which matches without regard to case and never duplicates;
- deals carry no product lines: lines go to a quote linked to the deal (``Quoted_Items``), each
  line needs a product, so a line without one creates it; the deal ``Amount`` is the sum;
- a missing record answers 204;
- an access token lives an hour and Zoho issues at most ten per ten minutes per refresh token.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, ClassVar
from urllib.parse import quote

import httpx

from alnuzum_crm.base import (
    CRM,
    dec,
    digits,
    norm_name,
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
    CrmRateLimited,
    CrmUnavailable,
    CrmValidationError,
)
from alnuzum_crm.http import HttpCore, RetryPolicy, shared_limiter
from alnuzum_crm.oauth import ZOHO_API_HOSTS, zoho_host
from alnuzum_crm.tokens import TokenSource
from alnuzum_crm.types import (
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
    Ref,
    TaskIn,
)

INN_NAMES = frozenset({"inn", "инн", "tin", "stir", "tax id"})
SCOPES = (
    "ZohoCRM.modules.ALL,ZohoCRM.settings.fields.READ,ZohoCRM.settings.modules.READ,"
    "ZohoCRM.users.READ,ZohoCRM.org.READ"
)


def _criteria_value(value: str) -> str:
    """Zoho criteria need ( ) , escaped with a backslash."""
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").replace(",", "\\,")


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value)
    if len(text) == 10:
        return datetime.combine(date.fromisoformat(text), datetime.min.time(), UTC)
    return datetime.fromisoformat(text)


def _split_name(name: str) -> tuple[str | None, str]:
    parts = name.strip().rsplit(" ", 1)
    return (parts[0], parts[1]) if len(parts) == 2 else (None, parts[0])


class ZohoCRM(CRM):
    kind: ClassVar[CrmKind] = CrmKind.ZOHO
    capabilities: ClassVar[frozenset[str]] = frozenset({"inn", "fractional_amount", "delete"})

    def __init__(
        self,
        api_domain: str,
        tokens: TokenSource,
        *,
        inn_field: str | None = None,
        rate: float = 10,
        retry: RetryPolicy | None = None,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_host = zoho_host(api_domain, ZOHO_API_HOSTS)
        self._tokens = tokens
        self._core = HttpCore(
            self.kind.value,
            limiter=shared_limiter(f"zoho:{self.api_host}:{id(tokens)}", rate),
            retry=retry,
            timeout=timeout,
            transport=transport,
            base_url=f"https://{self.api_host}/crm/v8",
        )
        self._inn_field = inn_field
        self._inn_checked = inn_field is not None
        self._stages: dict[str, str] | None = None

    def _id(self, value: str | None) -> str:
        return record_id(value, self.kind.value)

    async def aclose(self) -> None:
        await self._core.aclose()

    # --- transport ---

    async def _request(self, method: str, path: str, **kw: Any) -> Any:
        token = await self._tokens.token()
        refreshed = False
        while True:
            resp = await self._core.send(
                method, path, headers={"Authorization": f"Zoho-oauthtoken {token}"}, **kw
            )
            if (
                resp.status_code == 401
                and not refreshed
                and self._code(resp) in ("INVALID_TOKEN", "AUTHENTICATION_FAILURE")
            ):
                refreshed = True
                token = await self._tokens.refresh(token)
                continue
            return self._parse(resp)

    @staticmethod
    def _code(resp: httpx.Response) -> str:
        try:
            data = resp.json()
        except ValueError:
            return ""
        return str(data.get("code", "")) if isinstance(data, dict) else ""

    def _parse(self, resp: httpx.Response) -> Any:
        status, crm = resp.status_code, self.kind.value
        if status == 204:
            return None
        try:
            data = resp.json()
        except ValueError:
            data = None
        if status < 400:
            self._record_errors(data)
            return data
        body = data if isinstance(data, dict) else {}
        code = str(body.get("code", ""))
        text = f"{code or status}: {body.get('message', resp.reason_phrase)}"[:400]
        if status == 401 and code == "OAUTH_SCOPE_MISMATCH":
            raise CrmPermissionError(text, crm=crm, status=status, code=code)
        if status == 401:
            raise CrmAuthError(text, crm=crm, status=status, code=code)
        if status == 403:
            raise CrmPermissionError(text, crm=crm, status=status, code=code)
        if status == 404:
            raise CrmNotFound(text, crm=crm, status=status, code=code)
        if status == 429:
            raise CrmRateLimited(text, crm=crm, status=status, code=code)
        if status >= 500:
            raise CrmUnavailable(text, crm=crm, status=status, code=code)
        raise CrmValidationError(text, crm=crm, status=status, code=code)

    def _record_errors(self, data: Any) -> None:
        """Bulk endpoints answer 200/201 and put a per-record error inside."""
        if not isinstance(data, dict):
            return
        for item in data.get("data") or []:
            if isinstance(item, dict) and item.get("status") == "error":
                detail = item.get("details") or {}
                field = detail.get("api_name") if isinstance(detail, dict) else None
                text = f"{item.get('code')}: {item.get('message')}" + (f" ({field})" if field else "")
                raise CrmValidationError(text[:400], crm=self.kind.value, code=str(item.get("code")))

    async def _create(self, module: str, record: dict[str, Any]) -> str:
        data = await self._request("POST", f"/{module}", json={"data": [record]})
        return str(data["data"][0]["details"]["id"])

    async def _upsert(self, module: str, record: dict[str, Any], key: str) -> Ref:
        data = await self._request(
            "POST", f"/{module}/upsert", json={"data": [record], "duplicate_check_fields": [key]}
        )
        item = data["data"][0]
        return Ref(str(item["details"]["id"]), item.get("action") == "insert")

    async def _search(self, module: str, query: str) -> list[dict[str, Any]]:
        data = await self._request("GET", f"/{module}/search?{query}")
        return list((data or {}).get("data") or [])

    # --- settings ---

    async def _fields(self, module: str) -> list[dict[str, Any]]:
        data = await self._request("GET", "/settings/fields", params={"module": module})
        return list((data or {}).get("fields") or [])

    async def _inn(self) -> str | None:
        if not self._inn_checked:
            for f in await self._fields("Accounts"):
                labels = {norm_name(str(f.get("field_label", ""))), norm_name(str(f.get("api_name", "")))}
                if f.get("custom_field") and labels & INN_NAMES:
                    self._inn_field = str(f["api_name"])
                    break
            self._inn_checked = True
        return self._inn_field

    async def _stage_types(self) -> dict[str, str]:
        """Stage name → "Open" | "Closed Won" | "Closed Lost", in the order of the pipeline."""
        if self._stages is None:
            stages: dict[str, str] = {}
            for f in await self._fields("Deals"):
                if f.get("api_name") == "Stage":
                    for v in f.get("pick_list_values") or []:
                        stages[str(v.get("actual_value") or v.get("display_value"))] = str(
                            v.get("forecast_type", "Open")
                        )
            self._stages = stages
        return self._stages

    # --- companies and contacts ---

    async def find_company(self, company: CompanyIn) -> str | None:
        inn_field = await self._inn() if company.inn else None
        if company.inn and inn_field:
            crit = f"({inn_field}:equals:{_criteria_value(digits(company.inn))})"
            found = await self._search("Accounts", f"criteria={quote(crit)}")
            hits = [r for r in found if digits(str(r.get(inn_field) or "")) == digits(company.inn)]
            if hits:
                return str(min(hits, key=lambda r: int(r["id"]))["id"])
        crit = f"(Account_Name:equals:{_criteria_value(company.name)})"
        hits = [
            r
            for r in await self._search("Accounts", f"criteria={quote(crit)}")
            if same_name(str(r.get("Account_Name", "")), company.name)
            # a name match counts only when the company has no INN or the same one
            and (
                not company.inn
                or not inn_field
                or digits(str(r.get(inn_field) or "")) in ("", digits(company.inn))
            )
        ]
        return str(min(hits, key=lambda r: int(r["id"]))["id"]) if hits else None

    async def _company_record(self, company: CompanyIn) -> dict[str, Any]:
        record: dict[str, Any] = {"Account_Name": company.name}
        inn_field = await self._inn() if company.inn else None
        if company.inn and inn_field:
            record[inn_field] = digits(company.inn)
        if company.phone:
            record["Phone"] = company.phone
        return record

    async def create_company(self, company: CompanyIn) -> str:
        return await self._create("Accounts", await self._company_record(company))

    async def _create_company_ref(self, company: CompanyIn) -> Ref:
        # a company written seconds ago is not searchable yet: upsert never duplicates it
        return await self._upsert("Accounts", await self._company_record(company), "Account_Name")

    async def find_contact(self, contact: ContactIn) -> str | None:
        if contact.email:
            found = await self._search("Contacts", f"email={quote(contact.email.strip())}")
            hits = [r for r in found if same_email(r.get("Email"), contact.email)]
            if hits:
                return str(min(hits, key=lambda r: int(r["id"]))["id"])
        phone = digits(contact.phone)
        if len(phone) >= 7:
            hits = [
                r
                for r in await self._search("Contacts", f"phone={quote(phone)}")
                if same_phone(r.get("Phone"), phone) or same_phone(r.get("Mobile"), phone)
            ]
            if hits:
                return str(min(hits, key=lambda r: int(r["id"]))["id"])
        return None

    async def link_contact(self, contact_id: str, company_id: str) -> bool:
        data = await self._request(
            "GET", f"/Contacts/{self._id(contact_id)}", params={"fields": "Account_Name"}
        )
        if not data:
            raise CrmNotFound(f"contact {contact_id} not found", crm=self.kind.value, status=204)
        if (data["data"][0] or {}).get("Account_Name"):
            return False
        record = {"Account_Name": {"id": self._id(company_id)}}
        await self._request("PUT", f"/Contacts/{contact_id}", json={"data": [record]})
        return True

    @staticmethod
    def _contact_record(contact: ContactIn, company_id: str | None) -> dict[str, Any]:
        first, last = _split_name(contact.name)
        record: dict[str, Any] = {"Last_Name": last}
        if first:
            record["First_Name"] = first
        if contact.email:
            record["Email"] = contact.email.strip()
        if contact.phone:
            record["Phone"] = contact.phone
        if company_id:
            record["Account_Name"] = {"id": company_id}
        return record

    async def create_contact(self, contact: ContactIn, *, company_id: str | None = None) -> str:
        return await self._create("Contacts", self._contact_record(contact, company_id))

    async def _create_contact_ref(self, contact: ContactIn, company_id: str | None) -> Ref:
        record = self._contact_record(contact, company_id)
        if contact.email:
            return await self._upsert("Contacts", record, "Email")
        return Ref(await self._create("Contacts", record), True)

    # --- catalog ---

    async def create_product(self, product: ProductIn) -> str:
        record: dict[str, Any] = {"Product_Name": product.name, "Unit_Price": float(product.price)}
        if product.sku:
            record["Product_Code"] = product.sku
        return await self._create("Products", record)

    async def list_products(
        self, *, query: str | None = None, cursor: str | None = None, limit: int = 50
    ) -> Page[Product]:
        page = int(cursor or 1)
        fields = "id,Product_Name,Product_Code,Unit_Price,Currency"
        per_page = min(limit, 200)
        if query:
            data = await self._request(
                "GET", f"/Products/search?word={quote(query)}&page={page}&per_page={per_page}"
            )
        else:
            data = await self._request(
                "GET", "/Products", params={"fields": fields, "page": page, "per_page": per_page}
            )
        rows = list((data or {}).get("data") or [])
        items = tuple(
            Product(
                id=str(r["id"]),
                name=str(r.get("Product_Name", "")),
                sku=r.get("Product_Code") or None,
                price=dec(r.get("Unit_Price")),
                currency=r.get("Currency") or None,
            )
            for r in rows
        )
        more = bool((data or {}).get("info", {}).get("more_records"))
        return Page(items, str(page + 1) if more else None)

    # --- deals ---

    async def create_deal(self, deal: DealIn) -> CreatedDeal:
        created: list[str] = []
        product_ids: list[str] = []
        for line in deal.lines:
            if line.product_id:
                product_ids.append(line.product_id)
            else:
                new = await self.create_product(ProductIn(line.name, line.unit_price, line.sku))
                created.append(new)
                product_ids.append(new)
        stages = await self._stage_types()
        stage = next((s for s, t in stages.items() if t == "Open"), "Qualification")
        record: dict[str, Any] = {
            "Deal_Name": deal.title,
            "Stage": stage,
            "Amount": float(deal.amount),
            "Closing_Date": date.today().isoformat(),
        }
        if deal.company_id:
            record["Account_Name"] = {"id": deal.company_id}
        if deal.contact_id:
            record["Contact_Name"] = {"id": deal.contact_id}
        if deal.tags:
            record["Tag"] = [{"name": t} for t in deal.tags]
        if deal.currency:
            record["Currency"] = deal.currency
        deal_id = await self._create("Deals", record)
        if deal.lines:
            items = [
                {
                    "Product_Name": {"id": pid},
                    "Quantity": float(line.quantity),
                    "List_Price": float(line.unit_price),
                    "Description": line.name,
                }
                for pid, line in zip(product_ids, deal.lines, strict=True)
            ]
            quote_rec: dict[str, Any] = {
                "Subject": deal.title[:120],
                "Deal_Name": {"id": deal_id},
                "Quoted_Items": items,
            }
            if deal.company_id:
                quote_rec["Account_Name"] = {"id": deal.company_id}
            if deal.contact_id:
                quote_rec["Contact_Name"] = {"id": deal.contact_id}
            await self._create("Quotes", quote_rec)
        return CreatedDeal(deal_id, tuple(created))

    def _deal(self, row: dict[str, Any], stages: dict[str, str], lines: tuple[DealLine, ...] = ()) -> Deal:
        kind = stages.get(str(row.get("Stage", "")), "Open")
        status = (
            DealStatus.WON
            if kind == "Closed Won"
            else DealStatus.LOST
            if kind == "Closed Lost"
            else DealStatus.OPEN
        )
        return Deal(
            id=str(row["id"]),
            title=str(row.get("Deal_Name", "")),
            amount=dec(row.get("Amount")),
            currency=row.get("Currency") or None,
            status=status,
            created_at=_dt(row.get("Created_Time")),
            closed_at=_dt(row.get("Closing_Date")) if status is not DealStatus.OPEN else None,
            lines=lines,
        )

    async def _quote_ids(self, deal_id: str) -> list[str]:
        data = await self._request("GET", f"/Deals/{self._id(deal_id)}/Quotes", params={"fields": "id"})
        return [str(r["id"]) for r in (data or {}).get("data") or []]

    async def get_deal(self, deal_id: str) -> Deal:
        data = await self._request("GET", f"/Deals/{self._id(deal_id)}")
        if not data:
            raise CrmNotFound(f"deal {deal_id} not found", crm=self.kind.value, status=204)
        lines: list[DealLine] = []
        for qid in await self._quote_ids(deal_id):
            quote_row = (await self._request("GET", f"/Quotes/{self._id(qid)}"))["data"][0]
            for item in quote_row.get("Quoted_Items") or []:
                product = item.get("Product_Name") or {}
                lines.append(
                    DealLine(
                        name=str(item.get("Description") or product.get("name", "")),
                        quantity=Decimal(str(item.get("Quantity", 0))),
                        unit_price=Decimal(str(item.get("List_Price", 0))),
                        product_id=str(product["id"]) if product.get("id") else None,
                    )
                )
        return self._deal(data["data"][0], await self._stage_types(), tuple(lines))

    async def deal_history(
        self, *, company_id: str | None = None, contact_id: str | None = None, limit: int = 200
    ) -> list[Deal]:
        require_one(company_id, contact_id, self.kind.value)
        path = (
            f"/Accounts/{self._id(company_id)}/Deals"
            if company_id
            else f"/Contacts/{self._id(contact_id)}/Deals"
        )
        fields = "id,Deal_Name,Amount,Stage,Closing_Date,Created_Time,Currency"
        stages, page = await self._stage_types(), 1
        rows: list[dict[str, Any]] = []
        while len(rows) < limit:
            data = await self._request("GET", path, params={"fields": fields, "page": page, "per_page": 200})
            rows += list((data or {}).get("data") or [])
            if not (data or {}).get("info", {}).get("more_records"):
                break
            page += 1
        deals = [self._deal(r, stages) for r in rows[:limit]]
        return sorted(
            deals, key=lambda d: (d.created_at or datetime.min.replace(tzinfo=UTC), d.id), reverse=True
        )

    # --- files, notes, tasks ---

    async def attach_file(self, deal_id: str, file: FileIn) -> str:
        data = await self._request(
            "POST",
            f"/Deals/{self._id(deal_id)}/Attachments",
            files={"file": (file.name, file.content, file.mime_type)},
        )
        return str(data["data"][0]["details"]["id"])

    async def add_note(self, deal_id: str, text: str) -> str:
        record = {
            "Note_Content": text,
            "Parent_Id": {"module": {"api_name": "Deals"}, "id": self._id(deal_id)},
        }
        return await self._create("Notes", record)

    async def add_task(self, deal_id: str, task: TaskIn) -> str:
        record: dict[str, Any] = {
            "Subject": task.text[:250],
            "Description": task.text,
            "Due_Date": task.due.date().isoformat(),
            "What_Id": {"id": self._id(deal_id)},
            "$se_module": "Deals",
        }
        if task.responsible_id:
            record["Owner"] = {"id": task.responsible_id}
        return await self._create("Tasks", record)

    # --- cleanup ---

    async def _delete(self, module: str, ids: list[str]) -> None:
        for i in range(0, len(ids), 100):
            batch = [self._id(x) for x in ids[i : i + 100]]
            await self._request("DELETE", f"/{module}", params={"ids": ",".join(batch)})

    async def discard(self, leftovers: Leftovers) -> None:
        quotes: list[str] = []
        for deal_id in leftovers.deal_ids:
            try:
                quotes += await self._quote_ids(deal_id)
            except CrmError:
                continue
        await self._delete("Quotes", quotes)
        await self._delete("Tasks", list(leftovers.task_ids))
        await self._delete("Deals", list(leftovers.deal_ids))
        await self._delete("Contacts", list(leftovers.contact_ids))
        await self._delete("Accounts", list(leftovers.company_ids))
        await self._delete("Products", list(leftovers.product_ids))
