"""The work behind the pages: a scan of an account, the screen of a client and a task on the client.

The demo reads the test accounts as any CRM, but only the purchases «Демо · Покупка · …» and only the
companies of its plan: a scan, a screen and a task never reach the pool of the live tests or anything else
there. Its answer is the same for every visitor, so a scan of a demo CRM is kept for ten minutes and shared.
"""

import asyncio
import logging
import math
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from alnuzum_contracts.store import Seal
from fastapi import FastAPI
from pydantic_settings import BaseSettings, SettingsConfigDict

from alnuzum_crm.amocrm import ACCOUNT_LIMIT
from alnuzum_crm.base import CRM
from alnuzum_crm.errors import CrmAuthError, CrmError, CrmRateLimited, CrmUnavailable, CrmUnsupported
from alnuzum_crm.types import ClientTask, TaskIn
from alnuzum_orders.store import DEMO_OWNER
from alnuzum_platform.crypto import Keyring
from alnuzum_platform.store import (
    ConnectionDisabled,
    ConnectionInfo,
    ConnectionNotFound,
    ConnectionStore,
    QuotaExceeded,
)
from alnuzum_winback import demo as plan
from alnuzum_winback.rule import due, today
from alnuzum_winback.scan import analyse, detail, name_clients, only_demo, period, read_account
from alnuzum_winback.store import DEMO_TASK_TTL, Limits, ScanInfo, WinbackStore

log = logging.getLogger("alnuzum_winback")
MAX_TEXT = 500
CLIENT_PURCHASES = 300  # newest purchases of one client on its screen
ZOHO_LINED = 24  # of them with their products in Zoho (two requests a deal)


class WinbackSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    winback_cap: int = 2000  # won deals a scan of an own CRM reads at most
    winback_seconds: float = 60.0  # and seconds it reads at most
    winback_demo_cache_seconds: float = 600.0
    # the daily top-up of the demo history: None writes on production only (the stand shares the accounts)
    winback_seed: bool | None = None

    def limits(self) -> Limits:
        return Limits()


class WinbackError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def crm_error(exc: CrmError) -> str:
    if isinstance(exc, CrmAuthError):
        return "auth"
    if isinstance(exc, CrmUnavailable | CrmRateLimited):
        return "unavailable"
    if isinstance(exc, CrmUnsupported):
        return "unsupported"
    if exc.code == ACCOUNT_LIMIT:
        return "down"
    return "failed"


@dataclass(frozen=True)
class Standing:
    """An open task on the client: its text only where it is the visitor's own CRM or own task."""

    text: str | None
    due: datetime | None
    minutes_left: int | None  # a demo task closes itself


def client_ref(key: str) -> tuple[str | None, str | None]:
    """``company:<id>`` or ``contact:<id>`` as (company id, contact id)."""
    kind, _, value = key.partition(":")
    return (value, None) if kind == "company" else (None, value)


class Winback:
    def __init__(
        self,
        settings: WinbackSettings | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.settings = settings or WinbackSettings()
        self.clock, self.now = clock, now
        self._seal: Seal | None = None
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._seeded_on: str | None = None
        self._seeded: dict[str, str] = {}  # a CRM topped up today is not asked again

    # --- plumbing ---

    def store(self, app: FastAPI) -> WinbackStore:
        maker = getattr(app.state, "sessionmaker", None)
        conf = getattr(getattr(app.state, "connectors", None), "settings", None)
        keys = getattr(conf, "secret_keys", None)
        if maker is None or keys is None or not keys.get_secret_value():
            raise WinbackError("e_unreachable")
        if self._seal is None:
            self._seal = Seal(Keyring.from_env(keys.get_secret_value()))
        return WinbackStore(maker, self._seal)

    @staticmethod
    def connections(app: FastAPI) -> ConnectionStore:
        found: ConnectionStore | None = getattr(app.state, "store", None)
        if found is None:
            raise WinbackError("e_unreachable")
        return found

    async def demo_ids(self, app: FastAPI) -> dict[str, uuid.UUID]:
        """The demo accounts, as solution 1 set them up (owner ``demo``)."""
        have = getattr(app.state, "orders_demo", None)
        if have:
            return dict(have)
        st: ConnectionStore | None = getattr(app.state, "store", None)
        if st is None:
            return {}
        return {c.crm.value: c.id for c in await st.list(DEMO_OWNER) if c.status == "active"}

    async def adapter(self, app: FastAPI, info: ConnectionInfo) -> CRM:
        st = self.connections(app)
        if info.status != "active":
            raise ConnectionDisabled(str(info.id))
        made: CRM = app.state.connectors.adapter(info, await st.secrets(info.id), st)
        return made

    async def connection_of(self, app: FastAPI, scan: ScanInfo, conn_owner: str) -> ConnectionInfo:
        """The connection a scan read: the demo's, or one of this session (gone or another's: not found)."""
        return await self.connections(app).get(scan.connection_id, DEMO_OWNER if scan.demo else conn_owner)

    def _cached(self, key: str) -> dict[str, Any] | None:
        hit = self._cache.get(key)
        if hit and self.clock() - hit[0] < self.settings.winback_demo_cache_seconds:
            return hit[1]
        self._cache.pop(key, None)
        return None

    # --- a scan ---

    async def scan(self, app: FastAPI, *, scan_id: uuid.UUID, info: ConnectionInfo, demo: bool) -> None:
        """The scan is reserved (counted); here the account is read, the rule applied, the names read and the
        result stored, or the scan rejected with a code."""
        ws = self.store(app)
        kind = info.crm.value
        day = today(self.now())
        found = self._cached(f"scan:{kind}") if demo else None
        try:
            if found is None:
                async with await self.adapter(app, info) as crm:
                    read = await read_account(
                        crm,
                        kind,
                        day,
                        cap=self.settings.winback_cap,
                        seconds=self.settings.winback_seconds,
                        clock=self.clock,
                    )
                    bought = only_demo(read.purchases) if demo else read.purchases
                    found = await asyncio.to_thread(analyse, bought, day)
                    found = await name_clients(crm, found, demo=demo)
                found = found | {"cut": read.cut, "deals": len(read.purchases)}
                if demo:
                    self._cache[f"scan:{kind}"] = (self.clock(), found)
        except (ConnectionDisabled, ConnectionNotFound):
            await ws.reject_scan(scan_id, "auth")
            raise WinbackError("e_reconnect") from None
        except CrmError as exc:
            code = crm_error(exc)
            await ws.reject_scan(scan_id, code)
            if code == "auth" and not demo:
                await self.connections(app).mark(info.id, status="reauth_required", error="auth")
            raise WinbackError(f"e_crm_{code}") from None
        except Exception:  # anything else must not leave the scan "reading"
            await ws.reject_scan(scan_id, "error")
            raise
        summary = {"deals": found["deals"], "cut": found["cut"], "counts": found["counts"]}
        await ws.fill_scan(scan_id, found, summary)

    # --- the screen of a client ---

    async def client(self, app: FastAPI, scan: ScanInfo, n: int, *, conn_owner: str) -> dict[str, Any]:
        """Its orders with products and the reasons, read once per scan (a demo one is shared ten minutes)."""
        kept = scan.result.get("details", {}).get(str(n))
        if kept is not None:
            return dict(kept)
        row = scan.result["clients"][n - 1]
        key = f"client:{scan.crm}:{row['key']}"
        shown = self._cached(key) if scan.demo else None
        if shown is None:
            info = await self.connection_of(app, scan, conn_owner)
            since, _ = period(today(self.now()))
            async with await self.adapter(app, info) as crm:
                company, contact = client_ref(row["key"])
                lined = scan.crm != "zoho"
                page = await crm.purchases(
                    company_id=company, contact_id=contact, since=since, lines=lined, limit=CLIENT_PURCHASES
                )
                items = list(page.items)
                if not lined:  # Zoho reads the products of a deal with two requests: the newest only
                    newest = await crm.purchases(
                        company_id=company, contact_id=contact, since=since, limit=ZOHO_LINED
                    )
                    have = {p.deal_id: p for p in newest.items}
                    items = [have.get(p.deal_id, p) for p in items]
            shown = await asyncio.to_thread(detail, items, today(self.now()), demo=scan.demo)
            if scan.demo:
                self._cache[key] = (self.clock(), shown)
        await self.store(app).put_detail(scan.id, scan.owner, n, shown)
        return shown

    async def standing(self, app: FastAPI, scan: ScanInfo, n: int, *, conn_owner: str) -> list[Standing]:
        """Open tasks on the client as the screen shows them. In the demo only the fresh ones of the demo
        stand (the next task closes the rest), without the text another visitor typed."""
        row = scan.result["clients"][n - 1]
        info = await self.connection_of(app, scan, conn_owner)
        async with await self.adapter(app, info) as crm:
            company, contact = client_ref(row["key"])
            found = await crm.client_tasks(company_id=company, contact_id=contact)
        if not scan.demo:
            return [Standing(t.text[:300], t.due, None) for t in found]
        ages = await self.store(app).demo_ages(scan.crm, [t.id for t in found])
        out: list[Standing] = []
        for t in found:
            at = ages.get(t.id)
            left = (DEMO_TASK_TTL - (self.now() - at)).total_seconds() if at else 0
            if left > 0:
                out.append(Standing(None, t.due, max(1, math.ceil(left / 60))))
        return out

    # --- a task ---

    async def _open_tasks(
        self, crm: CRM, ws: WinbackStore, scan: ScanInfo, ref: tuple[str | None, str | None]
    ) -> list[ClientTask]:
        """Open tasks on the client; the demo first closes its own past their time and any it did not set."""
        found = await crm.client_tasks(company_id=ref[0], contact_id=ref[1])
        if not scan.demo:
            return found
        ages = await ws.demo_ages(scan.crm, [t.id for t in found])
        fresh: list[ClientTask] = []
        for t in found:
            at = ages.get(t.id)
            if at and self.now() - at < DEMO_TASK_TTL:
                fresh.append(t)
                continue
            await crm.discard_task(t.id)
            await ws.closed(scan.crm, t.id)
        return fresh

    async def set_task(
        self,
        app: FastAPI,
        scan: ScanInfo,
        n: int,
        text: str,
        *,
        conn_owner: str,
        ip_key: str,
        quota: int,
    ) -> str:
        """A task with the reason on client ``n`` of this scan, unless one is open there. Returns the notice:
        task_set, standing, busy, quota, full, down, auth, unavailable, failed."""
        ws, st, limits = self.store(app), self.connections(app), self.settings.limits()
        row = scan.result["clients"][n - 1]
        if scan.demo and (not row["key"].startswith("company:") or row.get("name") not in plan.NAMES):
            return "gone"  # the demo tasks only the companies of its plan (a scan keeps no other)
        try:
            info = await self.connection_of(app, scan, conn_owner)
        except ConnectionNotFound:
            return "auth"
        state, task_row = await ws.start_task(
            owner=scan.owner,
            ip_key=ip_key,
            scan_id=scan.id,
            connection_id=info.id,
            crm=scan.crm,
            demo=scan.demo,
            client=row["key"],
            limits=limits,
            now=self.now(),
        )
        if state != "ok" or task_row is None:
            return state
        owner = DEMO_OWNER if scan.demo else conn_owner
        write_id: uuid.UUID | None = None
        ref = client_ref(row["key"])
        try:
            async with await self.adapter(app, info) as crm:
                if await self._open_tasks(crm, ws, scan, ref):
                    await ws.finish_task(task_row, "standing")
                    return "standing"
                # a demo connection keeps a reserve per solution: tasks never eat the deals of solution 1
                write_id = await st.start_write(
                    info.id,
                    owner,
                    "winback_task",
                    max(quota, 2 * limits.demo_tasks_per_crm) if scan.demo else quota,
                    scope="winback_" if scan.demo else None,
                )
                when = due(self.now())
                task_id = await crm.add_client_task(TaskIn(text, when), company_id=ref[0], contact_id=ref[1])
        except QuotaExceeded:
            await ws.finish_task(task_row, "failed", error="quota")
            return "quota"
        except (ConnectionDisabled, ConnectionNotFound):
            await ws.finish_task(task_row, "failed", error="auth")
            return "auth"
        except CrmError as exc:
            code = crm_error(exc)
            if write_id is not None:
                await st.finish_write(write_id, ok=False, error=code)
            await ws.finish_task(task_row, "failed", error=code)
            if code == "auth" and not scan.demo:
                await st.mark(info.id, status="reauth_required", error="auth")
            log.info("win-back task on scan %s not set: %s", scan.id, code)
            return code if code in ("auth", "unavailable", "down") else "failed"
        except Exception:  # the journal row must not stay "writing"
            if write_id is not None:
                await st.finish_write(write_id, ok=False, error="error")
            await ws.finish_task(task_row, "failed", error="error")
            raise
        await st.finish_write(write_id, ok=True, record_id=task_id)
        await ws.finish_task(task_row, "written", task_id=task_id, due=when)
        log.info("win-back task set on scan %s", scan.id)
        return "task_set"

    # --- upkeep ---

    async def close_stale(self, app: FastAPI) -> int:
        """Demo tasks past their time are closed, so a demo company never stays with a task for good."""
        ws, st = self.store(app), self.connections(app)
        ids = await self.demo_ids(app)
        closed = 0
        stale = await ws.stale_demo_tasks(self.now())
        for kind in sorted({k for k, _ in stale}):
            if kind not in ids:
                continue
            try:
                info = await st.get(ids[kind], DEMO_OWNER)
                async with await self.adapter(app, info) as crm:
                    for k, task_id in stale:
                        if k == kind:
                            await crm.discard_task(task_id)
                            await ws.closed(kind, task_id)
                            closed += 1
            except (CrmError, ConnectionDisabled, ConnectionNotFound) as exc:
                log.warning("win-back demo tasks of %s not closed: %s", kind, type(exc).__name__)
        return closed

    def seeds(self, app: FastAPI) -> bool:
        if self.settings.winback_seed is not None:
            return self.settings.winback_seed
        conf = getattr(getattr(app.state, "connectors", None), "settings", None)
        return getattr(conf, "app_env", "") == "prod"

    async def seed(self, app: FastAPI) -> dict[str, int]:
        """Once a day on production: the demo history topped up to today in every demo CRM."""
        day = today(self.now())
        if not self.seeds(app) or self._seeded_on == day.isoformat():
            return {}
        st, made, failed = self.connections(app), {}, False
        for kind, conn_id in sorted((await self.demo_ids(app)).items()):
            if self._seeded.get(kind) == day.isoformat():
                continue
            try:
                info = await st.get(conn_id, DEMO_OWNER)
                async with await self.adapter(app, info) as crm:
                    made[kind] = await plan.seed(crm, kind, day)
                self._seeded[kind] = day.isoformat()
            except (CrmError, ConnectionDisabled, ConnectionNotFound, plan.CountsMoved) as exc:
                failed = True  # the next sweep (half an hour) tries this CRM again
                log.warning("win-back demo of %s not topped up: %s", kind, type(exc).__name__)
        if not failed:
            self._seeded_on = day.isoformat()
        if any(made.values()):
            self._cache.clear()
        return made
