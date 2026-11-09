"""The work behind the page: a letter becomes a stored reading bound to the CRM, a person's choice becomes a lead
with its company and contact, or a note and a task in a deal, or nothing."""

import asyncio
import json
import logging
import uuid
from decimal import Decimal
from importlib import resources
from typing import Any

from alnuzum_contracts.store import Seal
from fastapi import FastAPI
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from alnuzum_crm.amocrm import ACCOUNT_LIMIT
from alnuzum_crm.base import CRM
from alnuzum_crm.errors import CrmAuthError, CrmError, CrmRateLimited, CrmUnavailable
from alnuzum_crm.types import ContactIn
from alnuzum_files import FileError
from alnuzum_mail import intake
from alnuzum_mail.engine import DEMO_PREFIX, made_of, plan_of, values, write
from alnuzum_mail.intake import Letter
from alnuzum_mail.match import Keys, best, deals, rank
from alnuzum_mail.model import MailReader, Reading
from alnuzum_mail.rules import load, route
from alnuzum_mail.store import CeilingReached, Limits, MailStore, RunInfo
from alnuzum_orders.model import Gate, ModelError, Usage
from alnuzum_orders.store import DEMO_OWNER
from alnuzum_orders.web import Parsing
from alnuzum_platform.crypto import Keyring
from alnuzum_platform.solutions import Kit
from alnuzum_platform.store import ConnectionDisabled, ConnectionInfo, ConnectionNotFound, ConnectionStore, QuotaExceeded

log = logging.getLogger("alnuzum_mail")
MAX_LOOKS = 5  # looks into the CRM a run may ask for after its reading (fresh look, deal search)


class MailSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    anthropic_api_key: SecretStr = SecretStr("")
    anthropic_oauth_token: SecretStr = SecretStr("")  # first; the key takes what it refuses
    mail_model: str = "claude-opus-5-5"
    # the money ceiling of the solution per 24 hours (prod 1.5, stand 0.5): each call booked at its worst cost
    mail_usd_per_day: Decimal = Decimal("1.5")
    mail_parse_slots: int = 2
    mail_parse_seconds: float = 40.0
    mail_crm_seconds: float = 25.0  # looking into the CRM while a letter is read
    mail_demo_new_per_day: int = 15
    mail_demo_new_total: int = 200
    mail_demo_new_per_day_small: int = 3  # amoCRM and Kommo: 100 contacts and companies, 100 deals
    mail_demo_new_total_small: int = 20

    def limits(self) -> Limits:
        return Limits(
            new_per_day=self.mail_demo_new_per_day,
            new_total=self.mail_demo_new_total,
            small_new_per_day=self.mail_demo_new_per_day_small,
            small_new_total=self.mail_demo_new_total_small,
        )


class MailError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _truth() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(resources.files("alnuzum_mail").joinpath("samples/truth.json").read_text())
    return data


DEMO = _truth()


def samples() -> dict[str, dict[str, Any]]:
    return {s["id"]: s for s in DEMO["samples"]}


def sample_bytes(name: str) -> bytes:
    return resources.files("alnuzum_mail").joinpath(f"samples/{name}").read_bytes()


def crm_error(exc: CrmError) -> str:
    if isinstance(exc, CrmAuthError):
        return "auth"
    if isinstance(exc, CrmUnavailable | CrmRateLimited):
        return "unavailable"
    return (exc.code or type(exc).__name__)[:40]


def target_key(info: ConnectionInfo, demo: bool) -> str:
    return f"demo:{info.crm.value}" if demo else str(info.id)


def summary(reading: Reading, letter: Letter) -> dict[str, Any]:
    """What the row keeps in the open: the class and counts, no word of the letter."""
    return {
        "cls": reading.cls,
        "fields": len(reading.fields),
        "dropped": len(reading.dropped),
        "attachments": len(letter.attachments),
    }


class Mail:
    def __init__(self, settings: MailSettings | None = None, *, reader: MailReader | None = None) -> None:
        self.settings = settings or MailSettings()
        self._reader = reader
        self.parsing = Parsing(self.settings.mail_parse_slots, self.settings.mail_parse_seconds)
        self._seal: Seal | None = None

    def reader(self) -> MailReader:
        if self._reader is None:
            key = self.settings.anthropic_api_key.get_secret_value()
            token = self.settings.anthropic_oauth_token.get_secret_value()
            if not key and not token:
                raise MailError("e_model_off")
            self._reader = MailReader(key, oauth_token=token, model=self.settings.mail_model)
        return self._reader

    def store(self, app: FastAPI, kit: Kit) -> MailStore:
        maker = getattr(app.state, "sessionmaker", None)
        keys = kit.settings.secret_keys.get_secret_value()
        if maker is None or not keys:
            raise MailError("e_unreachable")
        if self._seal is None:
            self._seal = Seal(Keyring.from_env(keys))
        return MailStore(maker, self._seal)

    @staticmethod
    def connections(app: FastAPI) -> ConnectionStore:
        found: ConnectionStore | None = app.state.store
        if found is None:
            raise MailError("e_unreachable")
        return found

    async def demo_ids(self, app: FastAPI) -> dict[str, uuid.UUID]:
        """The demo accounts, as solution 1 set them up (owner ``demo``)."""
        have = getattr(app.state, "orders_demo", None)
        if have:
            return dict(have)
        st: ConnectionStore | None = app.state.store
        if st is None:
            return {}
        return {c.crm.value: c.id for c in await st.list(DEMO_OWNER) if c.status == "active"}

    def gate(self, ms: MailStore, owner: str) -> Gate:
        budget = self.settings.mail_usd_per_day

        async def gate(kind: str, worst: Decimal):  # type: ignore[no-untyped-def]
            if worst > budget:  # no day would ever fit it: «tomorrow again» would be a lie
                raise ModelError("e_too_big")
            try:
                spend_id = await ms.reserve_spend(owner, kind, budget=budget, worst=worst)
            except CeilingReached:
                raise ModelError("e_ceiling") from None

            async def done(used: Usage | None, ok: bool) -> None:
                if used is None:
                    await ms.finish_spend(spend_id, None, 0, ok)
                else:
                    await ms.finish_spend(spend_id, used.usd, used.input_tokens + used.output_tokens, ok)

            return done

        return gate

    async def adapter(self, app: FastAPI, kit: Kit, info: ConnectionInfo) -> CRM:
        st = self.connections(app)
        if info.status != "active":
            raise ConnectionDisabled(str(info.id))
        return kit.connectors.adapter(info, await st.secrets(info.id), st)

    # --- reading ---

    async def look(self, crm: CRM, letter: Letter, reading: Reading, *, demo: bool) -> dict[str, Any]:
        """What the CRM holds of the sender: candidate companies, the contact, the deals a reply may go into."""
        v = values(reading)
        keys = Keys.of(v)
        hits = await crm.find_companies(
            inn=keys.inn, domain=keys.domain, email=keys.email, phone=keys.phone, name=keys.company
        )
        companies = rank(hits, keys)
        company = best(companies)
        contact = None
        if v.get("email") or v.get("phone"):
            contact = await crm.find_contact(ContactIn(v.get("person") or "", email=v.get("email"), phone=v.get("phone")))
        found = []
        if company:
            found += await crm.deal_history(company_id=company, limit=50)
        if contact:
            found += await crm.deal_history(contact_id=contact, limit=50)
        listed = deals(found, f"{letter.subject} {reading.request}", prefix=DEMO_PREFIX if demo else None)
        return {
            "checked": True,
            "error": None,
            "companies": companies,
            "company": company,
            "contact": contact,
            "deals": listed,
            "deal": listed[0]["id"] if listed and listed[0]["open"] else None,
        }

    async def bind(
        self, app: FastAPI, kit: Kit, info: ConnectionInfo, letter: Letter, reading: Reading, *, demo: bool
    ) -> dict[str, Any]:
        """``look`` within its time; a CRM that fails or is slow leaves the letter unbound, said on the page."""
        try:
            async with await self.adapter(app, kit, info) as crm:
                return await asyncio.wait_for(self.look(crm, letter, reading, demo=demo), self.settings.mail_crm_seconds)
        except (ConnectionDisabled, ConnectionNotFound):
            error = "auth"
        except CrmError as exc:
            error = "auth" if crm_error(exc) == "auth" else "unavailable"
        except TimeoutError:
            error = "slow"
        return {"checked": False, "error": error, "companies": [], "company": None, "contact": None, "deals": [], "deal": None}

    async def route_of(self, ms: MailStore, owner: str, target: str, letter: Letter, reading: Reading) -> dict[str, Any]:
        saved = await ms.get_rules(owner, target) or {}
        rules = load(saved)
        v = values(reading)
        found = route(rules, subject=letter.subject, body=letter.body, email=v.get("email"), site=v.get("website"), language=reading.language)
        rule = rules[found.rule - 1] if found.rule else None
        names = saved.get("names") or {}
        return {
            "rule": found.rule,
            "when": rule.when if rule else None,
            "value": rule.value if rule else None,
            "stage": found.stage,
            "user": found.user,
            "stage_name": names.get(f"stage:{found.stage}") if found.stage else None,
            "user_name": names.get(f"user:{found.user}") if found.user else None,
        }

    async def read(
        self,
        app: FastAPI,
        kit: Kit,
        ms: MailStore,
        *,
        run_id: uuid.UUID,
        owner: str,
        info: ConnectionInfo,
        demo: bool,
        data: bytes | None,
        text: str | None,
        lang: str,
    ) -> None:
        """The run is already reserved (counted): here the letter is read, bound and routed, or rejected."""
        gate = self.gate(ms, owner)
        try:
            letter = await self.parsing.read(intake.read, data, text)
            reading = await self.reader().triage(letter, lang=lang, gate=gate)
        except (FileError, ModelError, MailError) as exc:
            await ms.reject_run(run_id, exc.code)
            raise MailError(exc.code) from None
        except Exception:  # anything else must not leave the run "reading"
            await ms.reject_run(run_id, "error")
            raise
        binding = await self.bind(app, kit, info, letter, reading, demo=demo)
        result = {
            "letter": letter.to_json(),
            "reading": reading.to_json(),
            "usage": {"calls": reading.usage.calls, "usd": str(reading.usage.usd.quantize(Decimal("0.0001")))},
            "binding": binding,
            "route": await self.route_of(ms, owner, target_key(info, demo), letter, reading),
            "choice": {"cls": reading.cls},
            "looks": 0,
            "lang": lang,
        }
        await ms.fill_run(run_id, result, summary(reading, letter))

    async def look_again(self, app: FastAPI, kit: Kit, run: RunInfo, *, conn_owner: str, query: str | None) -> str:
        """A fresh look into the CRM, or a search of deals by a name (a reply whose deal was not found)."""
        ms = self.store(app, kit)
        result = dict(run.result or {})
        if int(result.get("looks") or 0) >= MAX_LOOKS:
            return "looks"
        result["looks"] = int(result.get("looks") or 0) + 1
        info = await self.connections(app).get(run.connection_id, DEMO_OWNER if run.demo else conn_owner)
        letter, reading = Letter.of(result["letter"]), Reading.of(result["reading"])
        if query is None:
            result["binding"] = await self.bind(app, kit, info, letter, reading, demo=run.demo)
            await ms.save_result(run.id, run.owner, result)
            return "looked" if result["binding"]["checked"] else str(result["binding"]["error"])
        query = " ".join(query.split())[:80] or ("Демо" if run.demo else "")
        if not query:
            return "no_query"
        try:
            async with await self.adapter(app, kit, info) as crm:
                hits = await crm.find_deals(query, limit=20)
        except (ConnectionDisabled, ConnectionNotFound):
            return "auth"
        except CrmError as exc:
            return "auth" if crm_error(exc) == "auth" else "unavailable"
        binding = dict(result["binding"])
        have = {str(d["id"]) for d in binding.get("deals") or []}
        extra = [
            {"id": h.id, "title": h.title[:200], "open": h.status.value == "open", "shared": 0}
            for h in hits
            if h.id not in have and (not run.demo or h.title.startswith(DEMO_PREFIX))
        ]
        binding["deals"] = (list(binding.get("deals") or []) + extra)[:12]
        result["binding"] = binding
        await ms.save_result(run.id, run.owner, result)
        return "found" if extra else "none"

    # --- the write ---

    async def confirm(self, app: FastAPI, kit: Kit, run: RunInfo, *, conn_owner: str, ip_key: str) -> str:
        """Write by the plan of the run's choice; a retry goes on from the first step not done. Returns the
        notice: written, quota, full, full_new, down, busy, failed, auth, not_demo."""
        if not run.result:
            return "failed"
        ms, st, limits = self.store(app, kit), self.connections(app), self.settings.limits()
        result = run.result
        lang = str(result.get("lang") or "ru")
        letter, reading = Letter.of(result["letter"]), Reading.of(result["reading"])
        plan = plan_of(
            letter, reading, result["binding"], result["route"], result["choice"], crm=run.crm, lang=lang, demo=run.demo
        )
        done_before = await ms.steps_done(run.id)
        creates = plan.creates(made_of(done_before))
        state = await ms.start_writing(
            run.id, run.owner, demo=run.demo, crm=run.crm, ip_key=ip_key, creates=len(creates), limits=limits
        )
        if state != "ok":
            return state
        owner = DEMO_OWNER if run.demo else conn_owner
        quota = max(kit.quota, 2 * limits.demo_per_crm) if run.demo else kit.quota
        try:
            info = await st.get(run.connection_id, owner)
            crm_ = await self.adapter(app, kit, info)
            # a demo connection keeps a reserve per solution: letters never eat the writes of the other three
            write_id = await st.start_write(info.id, owner, "mail_write", quota, scope="mail_" if run.demo else None)
        except (QuotaExceeded, ConnectionDisabled, ConnectionNotFound) as exc:
            code = "quota" if isinstance(exc, QuotaExceeded) else "auth"
            await ms.finish_writing(run.id, ok=False, error=code)
            return code
        steps = await ms.steps_done(run.id)  # read while "writing" is ours alone

        async def mark(step: str, created: str | None) -> None:
            await ms.step_done(run.id, step, crm=run.crm, demo=run.demo, created=created)

        try:
            async with crm_ as crm:
                if run.demo and plan.kind == "reply":
                    deal = await crm.get_deal(str(plan.deal_id))
                    if not deal.title.startswith(DEMO_PREFIX):  # the demo writes only into its own deals
                        await st.finish_write(write_id, ok=False, error="not_demo")
                        await ms.finish_writing(run.id, ok=False, error="not_demo")
                        return "not_demo"
                deal_id = await write(crm, plan, done=steps, mark=mark)
                url = None if run.demo or not deal_id else crm.deal_link(kit.connectors.account_of(info), deal_id)
        except CrmError as exc:
            code = crm_error(exc)
            await st.finish_write(write_id, ok=False, error=code)
            await ms.finish_writing(run.id, ok=False, error=code)
            if code == "auth" and not run.demo:
                await st.mark(info.id, status="reauth_required", error="auth")
            log.info("mail run %s not written: %s", run.id, code)
            if code == ACCOUNT_LIMIT and run.demo:
                return "down"  # the test account takes no records now: for half an hour the demo says so
            return "auth" if code == "auth" else "failed"
        except Exception:  # anything else: the run must not stay "writing" (a retry goes on from the step)
            await st.finish_write(write_id, ok=False, error="error")
            await ms.finish_writing(run.id, ok=False, error="error")
            raise
        await st.finish_write(write_id, ok=True, record_id=deal_id, record_url=url)
        await ms.finish_writing(run.id, ok=True, deal_url=url)
        log.info("mail run %s written to deal %s", run.id, deal_id)
        return "written"
