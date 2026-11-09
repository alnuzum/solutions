"""The work behind the page: a draft becomes a stored comparison, a chosen deal gets the note and the task."""

import json
import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal
from importlib import resources
from typing import Any

from fastapi import FastAPI
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from alnuzum_contracts import intake
from alnuzum_contracts.clauses import Clause, compare, split
from alnuzum_contracts.engine import DEMO_PREFIX, comparison, summary, to_result, write
from alnuzum_contracts.model import SIDES, ContractReader
from alnuzum_contracts.store import CeilingReached, ContractStore, Limits, RunInfo, Seal
from alnuzum_crm.errors import CrmAuthError, CrmError, CrmRateLimited, CrmUnavailable
from alnuzum_files import FileError
from alnuzum_orders.model import Gate, ModelError, Usage
from alnuzum_orders.store import DEMO_OWNER
from alnuzum_orders.web import Parsing
from alnuzum_platform.crypto import Keyring
from alnuzum_platform.solutions import Kit
from alnuzum_platform.store import (
    ConnectionDisabled,
    ConnectionInfo,
    ConnectionNotFound,
    ConnectionStore,
    QuotaExceeded,
)

log = logging.getLogger("alnuzum_contracts")
MAX_CANDIDATES = 12


class ContractSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    anthropic_api_key: SecretStr = SecretStr("")
    contracts_model: str = "claude-opus-5-5"
    # the money ceiling of the solution per 24 hours (prod 1.5, stand 0.5): each call booked at its worst
    # cost until it has cost what it cost
    contracts_usd_per_day: Decimal = Decimal("1.5")
    contracts_parse_slots: int = 2
    contracts_parse_seconds: float = 40.0

    def limits(self) -> Limits:
        return Limits()


class ContractError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Standard:
    key: str  # demo:<name> or the id of a stored standard
    side: str
    clauses: list[Clause]
    kind: str


def _truth() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        resources.files("alnuzum_contracts").joinpath("samples/truth.json").read_text()
    )
    return data


DEMO = _truth()


def demo_standards() -> dict[str, dict[str, Any]]:
    return dict(DEMO["standards"])


def samples() -> dict[str, dict[str, Any]]:
    return {p["id"]: p for p in DEMO["pairs"]}


def sample_bytes(name: str) -> bytes:
    return resources.files("alnuzum_contracts").joinpath(f"samples/{name}").read_bytes()


def crm_error(exc: CrmError) -> str:
    if isinstance(exc, CrmAuthError):
        return "auth"
    if isinstance(exc, CrmUnavailable | CrmRateLimited):
        return "unavailable"
    return (exc.code or type(exc).__name__)[:40]


class Contracts:
    def __init__(
        self, settings: ContractSettings | None = None, *, reader: ContractReader | None = None
    ) -> None:
        self.settings = settings or ContractSettings()
        self._reader = reader
        self.parsing = Parsing(self.settings.contracts_parse_slots, self.settings.contracts_parse_seconds)
        self._seal: Seal | None = None
        self._demo: dict[str, Standard] = {}

    def reader(self) -> ContractReader:
        if self._reader is None:
            key = self.settings.anthropic_api_key.get_secret_value()
            if not key:
                raise ContractError("e_model_off")
            self._reader = ContractReader(key, model=self.settings.contracts_model)
        return self._reader

    def store(self, app: FastAPI, kit: Kit) -> ContractStore:
        maker = getattr(app.state, "sessionmaker", None)
        keys = kit.settings.secret_keys.get_secret_value()
        if maker is None or not keys:
            raise ContractError("e_unreachable")
        if self._seal is None:
            self._seal = Seal(Keyring.from_env(keys))
        return ContractStore(maker, self._seal)

    @staticmethod
    def connections(app: FastAPI) -> ConnectionStore:
        found: ConnectionStore | None = app.state.store
        if found is None:
            raise ContractError("e_unreachable")
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

    def gate(self, cs: ContractStore, owner: str) -> Gate:
        budget = self.settings.contracts_usd_per_day

        async def gate(kind: str, worst: Decimal):  # type: ignore[no-untyped-def]
            try:
                spend_id = await cs.reserve_spend(owner, kind, budget=budget, worst=worst)
            except CeilingReached:
                raise ModelError("e_ceiling") from None

            async def done(used: Usage | None, ok: bool) -> None:
                if used is None:
                    await cs.finish_spend(spend_id, None, 0, ok)
                else:
                    await cs.finish_spend(spend_id, used.usd, used.input_tokens + used.output_tokens, ok)

            return done

        return gate

    # --- reading ---

    async def document(self, data: bytes, gate: Gate, usage: Usage) -> tuple[str, list[Clause]]:
        """A file into clauses; a scan is read by the model (booked like any call)."""
        doc = await self.parsing.read(intake.read, data)
        if doc.kind == "scan":
            paragraphs, used = await self.reader().read_scan(doc.images, gate)
            usage.add(used)
            doc.blocks = intake.from_paragraphs(paragraphs)
        found = await self.parsing.read(split, doc.blocks)
        if not found:
            raise FileError("e_empty")
        return doc.kind, found

    async def demo_standard(self, name: str) -> Standard:
        if name not in demo_standards():
            raise ContractError("e_standard")
        if name not in self._demo:
            meta = demo_standards()[name]
            doc = await self.parsing.read(intake.read, sample_bytes(meta["file"]))
            self._demo[name] = Standard(f"demo:{name}", meta["side"], split(doc.blocks), doc.kind)
        return self._demo[name]

    async def own_standard(self, cs: ContractStore, standard_id: uuid.UUID, owner: str) -> Standard:
        info, rows = await cs.get_standard(standard_id, owner)
        return Standard(str(info.id), info.side, [Clause(*r) for r in rows], "stored")

    async def put_standard(
        self, cs: ContractStore, *, owner: str, connection_id: uuid.UUID, side: str, data: bytes
    ) -> int:
        if side not in SIDES:
            raise ContractError("e_side")
        usage = Usage()
        _kind, found = await self.document(data, self.gate(cs, owner), usage)
        await cs.put_standard(owner, connection_id, side, [[c.label, c.key, c.text] for c in found])
        return len(found)

    async def read(
        self,
        cs: ContractStore,
        *,
        run_id: uuid.UUID,
        owner: str,
        standard: Standard,
        data: bytes,
        lang: str,
    ) -> None:
        """The run is already reserved (counted); here it is read, compared, rated and stored, or rejected
        with a code."""
        gate = self.gate(cs, owner)
        usage = Usage()
        try:
            kind, theirs = await self.document(data, gate, usage)
            cmp = await self.parsing.read(compare, standard.clauses, theirs)
            rating = await self.reader().rate(cmp, side=standard.side, lang=lang, gate=gate)
            usage.add(rating.usage)
        except (FileError, ModelError, ContractError) as exc:
            await cs.reject_run(run_id, exc.code)
            raise ContractError(exc.code) from None
        spent = {"calls": usage.calls, "usd": str(usage.usd.quantize(Decimal("0.0001")))}
        result = to_result(
            cmp,
            kind_ours=standard.kind,
            kind_theirs=kind,
            side=standard.side,
            standard=standard.key,
            usage=spent,
        )
        await cs.fill_run(run_id, result, summary(cmp))

    # --- the deal ---

    async def adapter_for(
        self, app: FastAPI, kit: Kit, run: RunInfo, conn_owner: str
    ) -> tuple[ConnectionInfo, Any]:
        st = self.connections(app)
        info = await st.get(run.connection_id, DEMO_OWNER if run.demo else conn_owner)
        if info.status != "active":
            raise ConnectionDisabled(str(info.id))
        return info, kit.connectors.adapter(info, await st.secrets(info.id), st)

    async def search(self, app: FastAPI, kit: Kit, run: RunInfo, *, conn_owner: str, query: str) -> str:
        """Deals by a name: a demo finds only its «Демо · …» deals. Returns a notice."""
        cs = self.store(app, kit)
        query = " ".join(query.split())[:80] or ("Демо" if run.demo else "")
        if not query:
            return "no_query"
        try:
            _info, adapter = await self.adapter_for(app, kit, run, conn_owner)
            async with adapter as crm:
                hits = await crm.find_deals(query, limit=MAX_CANDIDATES * 2)
        except (ConnectionDisabled, ConnectionNotFound):
            return "auth"
        except CrmError as exc:
            return "auth" if crm_error(exc) == "auth" else "unavailable"
        found = [
            {
                "id": h.id,
                "title": h.title[:200],
                "company": (h.company or "")[:120],
                "contact": (h.contact or "")[:120],
            }
            for h in hits
            if not run.demo or h.title.startswith(DEMO_PREFIX)
        ][:MAX_CANDIDATES]
        await cs.save_candidates(run.id, run.owner, found)
        return "found" if found else "none"

    async def confirm(
        self, app: FastAPI, kit: Kit, run: RunInfo, *, conn_owner: str, ip_key: str, lang: str
    ) -> str:
        """Write the note and the task into the chosen deal; a retry after a failure goes on from the first
        step not done. Returns the notice: written, quota, full, busy, failed, auth, no_deal, not_demo."""
        if not run.deal_id or not run.result:
            return "no_deal"
        cs, st, limits = self.store(app, kit), self.connections(app), self.settings.limits()
        state = await cs.start_writing(
            run.id, run.owner, demo=run.demo, crm=run.crm, ip_key=ip_key, limits=limits
        )
        if state != "ok":
            return state
        owner = DEMO_OWNER if run.demo else conn_owner
        quota = max(kit.quota, 2 * limits.demo_per_crm) if run.demo else kit.quota
        try:
            info, adapter = await self.adapter_for(app, kit, run, conn_owner)
            write_id = await st.start_write(info.id, owner, "contract_note", quota)
        except (QuotaExceeded, ConnectionDisabled, ConnectionNotFound) as exc:
            code = "quota" if isinstance(exc, QuotaExceeded) else "auth"
            await cs.finish_writing(run.id, ok=False, error=code)
            return code
        done_before = frozenset(await cs.steps_done(run.id))  # read while "writing" is ours alone

        async def mark(step: str) -> None:
            await cs.step_done(run.id, step)

        try:
            async with adapter as crm:
                if run.demo:
                    deal = await crm.get_deal(run.deal_id)
                    if not deal.title.startswith(DEMO_PREFIX):  # the demo writes only into its own deals
                        await st.finish_write(write_id, ok=False, error="not_demo")
                        await cs.finish_writing(run.id, ok=False, error="not_demo")
                        return "not_demo"
                await write(crm, run.deal_id, comparison(run.result), lang=lang, done=done_before, mark=mark)
                url = None if run.demo else crm.deal_link(kit.connectors.account_of(info), run.deal_id)
        except CrmError as exc:
            code = crm_error(exc)
            await st.finish_write(write_id, ok=False, error=code)
            await cs.finish_writing(run.id, ok=False, error=code)
            if code == "auth" and not run.demo:
                await st.mark(info.id, status="reauth_required", error="auth")
            log.info("contract run %s not written: %s", run.id, code)
            return "auth" if code == "auth" else "failed"
        except Exception:  # anything else: the run must not stay "writing" (a retry goes on from the step)
            await st.finish_write(write_id, ok=False, error="error")
            await cs.finish_writing(run.id, ok=False, error="error")
            raise
        await st.finish_write(write_id, ok=True, record_id=run.deal_id, record_url=url)
        await cs.finish_writing(run.id, ok=True, deal_url=url)
        log.info("contract run %s written to deal %s", run.id, run.deal_id)
        return "written"
