"""What the solution keeps: a run for a day (never the file), price rules, the journal of model calls.

Quotas are counted from these tables: runs per session and per address, model calls per day for the
whole solution, demo deals per session. Each is taken before the work it pays for, inside one
transaction under an advisory lock, so parallel requests cannot both take the last one, and an attempt
that fails afterwards stays counted.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Uuid,
    cast,
    delete,
    func,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from alnuzum_crm.amocrm import ACCOUNT_LIMIT
from alnuzum_platform.db import Base
from alnuzum_platform.models import CrmWrite

KEEP = timedelta(days=1)
DEMO_OWNER = "demo"  # the owner of the demo connections
LOCK_KEY = 0x4F524452  # advisory lock of the model-call ceiling ("ORDR")
# advisory lock classes (two-key form): runs of a session, runs of an address, demo writes of a session
LOCK_OWNER, LOCK_IP, LOCK_WRITES = 0x4F4F, 0x4F49, 0x4F57
# demo writes of an address and of a demo CRM
LOCK_WRITES_IP, LOCK_WRITES_CRM = 0x4F58, 0x4F59
# a run is "reading" while the system reads it and "rejected" when that failed: neither is shown,
# both count against the quotas
HIDDEN = ("reading", "rejected")
# a write or a reading takes minutes at most (every call to the CRM and to the model has its time-out);
# one still going after this was left by a stopped process (a deploy, a crash)
STUCK = timedelta(minutes=30)


class OrderRun(Base):
    __tablename__ = "orders_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(128), index=True)
    ip_key: Mapped[str] = mapped_column(String(64), index=True)
    # the address that asked to write it: the demo deals of an address are counted by this key, set under
    # the same lock as status "writing"
    write_ip_key: Mapped[str | None] = mapped_column(String(64), index=True)
    connection_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("crm_connections.id", ondelete="CASCADE"))
    demo: Mapped[bool] = mapped_column(Boolean, default=False)
    crm: Mapped[str] = mapped_column(String(16))
    lang: Mapped[str] = mapped_column(String(5), default="ru")
    result: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16), default="ready")
    deal_id: Mapped[str | None] = mapped_column(String(40))
    deal_url: Mapped[str | None] = mapped_column(String(500))
    readback: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(String(60))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    written_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # when the write began: a run a stopped process left "writing" is released after ``STUCK``
    writing_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # the steps of the write already done ("deal", "note", "task"); with ``deal_id`` a retry goes on in
    # the deal it made
    steps: Mapped[list[str] | None] = mapped_column(JSONB)


class OrderRules(Base):
    __tablename__ = "orders_rules"

    owner: Mapped[str] = mapped_column(String(128), primary_key=True)
    target: Mapped[str] = mapped_column(String(64), primary_key=True)
    rules: Mapped[dict[str, Any]] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ModelCall(Base):
    __tablename__ = "orders_model_calls"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    owner: Mapped[str] = mapped_column(String(128))
    ip_key: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    usd: Mapped[Decimal] = mapped_column(Numeric(10, 5), default=Decimal(0))
    ok: Mapped[bool | None] = mapped_column(Boolean)


class CeilingReached(RuntimeError):
    """The solution's model calls or dollars for the day are spent."""


class RunNotFound(LookupError):
    pass


class RunsExceeded(RuntimeError):
    """The session's or the address's runs for the day are spent."""


@dataclass(frozen=True)
class DemoLimits:
    """Deals the demo accounts take in 24 hours: from a session, from an address, into one CRM (its own
    ceiling, apart from the platform's quota of a connection); ``total``: into that CRM in all (Kommo's
    technical account keeps at most 100 deals)."""

    per_session: int
    per_ip: int
    per_crm: int
    total: int | None = None


@dataclass(frozen=True)
class RunInfo:
    id: uuid.UUID
    owner: str
    connection_id: uuid.UUID
    demo: bool
    crm: str
    lang: str
    result: dict[str, Any]
    status: str
    deal_id: str | None
    deal_url: str | None
    readback: dict[str, Any] | None
    error: str | None
    created_at: datetime | None

    @classmethod
    def of(cls, row: OrderRun) -> "RunInfo":
        return cls(
            row.id, row.owner, row.connection_id, row.demo, row.crm, row.lang, dict(row.result), row.status,
            row.deal_id, row.deal_url, row.readback, row.error, row.created_at,
        )  # fmt: skip


class OrderStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    # --- runs ---

    async def reserve_run(
        self,
        owner: str,
        ip_key: str,
        connection_id: uuid.UUID,
        crm: str,
        lang: str,
        *,
        demo: bool,
        per_session: int,
        per_ip: int,
    ) -> uuid.UUID:
        """A run is counted before the document is read: under locks of the session and of the address,
        every attempt of the last day (read, failing or failed) is counted, then this one is added."""
        since = datetime.now(UTC) - timedelta(days=1)
        async with self._sessions.begin() as session:
            await _lock(session, LOCK_OWNER, owner)
            await _lock(session, LOCK_IP, ip_key)
            base = select(func.count()).select_from(OrderRun).where(OrderRun.created_at > since)
            mine = await session.scalar(base.where(OrderRun.owner == owner))
            theirs = await session.scalar(base.where(OrderRun.ip_key == ip_key))
            if (mine or 0) >= per_session or (theirs or 0) >= per_ip:
                raise RunsExceeded(f"{per_session} per session, {per_ip} per address")
            row = OrderRun(
                id=uuid.uuid4(),
                owner=owner,
                ip_key=ip_key,
                connection_id=connection_id,
                demo=demo,
                crm=crm,
                lang=lang,
                result={},
                status="reading",
                expires_at=datetime.now(UTC) + KEEP,
            )
            session.add(row)
        return row.id

    async def fill_run(self, run_id: uuid.UUID, result: dict[str, Any]) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                update(OrderRun)
                .where(OrderRun.id == run_id, OrderRun.status == "reading")
                .values(result=result, status="ready")
            )

    async def reject_run(self, run_id: uuid.UUID, error: str) -> None:
        """The attempt failed; it stays (without a result) and keeps counting for a day."""
        async with self._sessions.begin() as session:
            await session.execute(
                update(OrderRun)
                .where(OrderRun.id == run_id, OrderRun.status == "reading")
                .values(status="rejected", error=error[:60])
            )

    async def get_run(self, run_id: uuid.UUID, owner: str) -> RunInfo:
        async with self._sessions() as session:
            row = await session.scalar(
                select(OrderRun).where(
                    OrderRun.id == run_id,
                    OrderRun.owner == owner,
                    OrderRun.expires_at > datetime.now(UTC),
                    OrderRun.status.not_in(HIDDEN),
                )
            )
            if row is None:
                raise RunNotFound(str(run_id))
            return RunInfo.of(row)

    async def save_result(self, run_id: uuid.UUID, owner: str, result: dict[str, Any]) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                update(OrderRun)
                .where(OrderRun.id == run_id, OrderRun.owner == owner, OrderRun.status == "ready")
                .values(result=result)
            )

    async def start_writing(
        self,
        run_id: uuid.UUID,
        owner: str,
        *,
        demo: DemoLimits | None = None,
        ip_key: str = "",
        crm: str = "",
    ) -> str:
        """Only one write per run, and the demo limits: the counts (written or being written) and the
        change of status happen in one transaction under locks of the session, the address and the CRM
        (always in this order). Returns "ok", "busy" (written or being written), "quota" (the session's or
        the address's demo deals of the day), "full" (this demo CRM's deals of the day) or "full_total"."""
        async with self._sessions.begin() as session:
            await _lock(session, LOCK_WRITES, owner)
            if demo is not None:
                await _lock(session, LOCK_WRITES_IP, ip_key)
                await _lock(session, LOCK_WRITES_CRM, crm)
                if await _demo_writes(session, OrderRun.owner == owner) >= demo.per_session:
                    return "quota"
                if await _demo_writes(session, OrderRun.write_ip_key == ip_key) >= demo.per_ip:
                    return "quota"
                if state := await _crm_state(session, crm, demo):
                    return state
            done = await session.execute(
                update(OrderRun)
                .where(
                    OrderRun.id == run_id, OrderRun.owner == owner, OrderRun.status.in_(("ready", "failed"))
                )
                .values(status="writing", write_ip_key=ip_key[:64], writing_at=func.now())
            )
            return "ok" if done.rowcount else "busy"  # type: ignore[attr-defined]

    async def step_done(self, run_id: uuid.UUID, step: str, deal_id: str | None = None) -> None:
        """Kept before the next step begins: a retry skips it (and writes into this deal)."""
        values: dict[str, Any] = {
            "steps": func.coalesce(OrderRun.steps, text("'[]'::jsonb")).op("||")(
                func.jsonb_build_array(cast(step, String))
            )
        }
        if deal_id is not None:
            values["deal_id"] = deal_id
        async with self._sessions.begin() as session:
            await session.execute(
                update(OrderRun).where(OrderRun.id == run_id, OrderRun.status == "writing").values(**values)
            )

    async def steps_done(self, run_id: uuid.UUID) -> tuple[frozenset[str], str | None]:
        async with self._sessions() as session:
            row = (
                await session.execute(select(OrderRun.steps, OrderRun.deal_id).where(OrderRun.id == run_id))
            ).one_or_none()
        return (frozenset(row[0] or ()), row[1]) if row else (frozenset(), None)

    async def finish_writing(
        self,
        run_id: uuid.UUID,
        *,
        ok: bool,
        deal_id: str | None = None,
        deal_url: str | None = None,
        readback: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        """A failed write keeps the deal it made (``step_done``): the retry goes on in it."""
        values: dict[str, Any] = {"status": "written" if ok else "failed", "error": error}
        if ok:
            values |= {
                "deal_id": deal_id,
                "deal_url": deal_url,
                "readback": readback,
                "written_at": datetime.now(UTC),
            }
        async with self._sessions.begin() as session:
            await session.execute(update(OrderRun).where(OrderRun.id == run_id).values(**values))

    async def release_stuck(self) -> tuple[int, int]:
        """Runs a stopped process left behind: "writing" since ``STUCK`` becomes "failed" (the visitor may
        try again, the demo quota is free), "reading" becomes "rejected" (it counts as any failed attempt)."""
        cut = datetime.now(UTC) - STUCK
        async with self._sessions.begin() as session:
            written = await session.execute(
                update(OrderRun)
                .where(
                    OrderRun.status == "writing",
                    or_(OrderRun.writing_at.is_(None), OrderRun.writing_at < cut),
                )
                .values(status="failed", error="interrupted")
            )
            read = await session.execute(
                update(OrderRun)
                .where(OrderRun.status == "reading", OrderRun.created_at < cut)
                .values(status="rejected", error="interrupted")
            )
            return int(written.rowcount), int(read.rowcount)  # type: ignore[attr-defined]

    async def runs_today(self, *, owner: str | None = None, ip_key: str | None = None) -> int:
        since = datetime.now(UTC) - timedelta(days=1)
        query = select(func.count()).select_from(OrderRun).where(OrderRun.created_at > since)
        if owner is not None:
            query = query.where(OrderRun.owner == owner)
        if ip_key is not None:
            query = query.where(OrderRun.ip_key == ip_key)
        async with self._sessions() as session:
            return int(await session.scalar(query) or 0)

    async def writes_today(self, owner: str) -> int:
        """Demo deals of the session in the last day: written or being written."""
        async with self._sessions() as session:
            return await _demo_writes(session, OrderRun.owner == owner)

    async def demo_full(self, limits: dict[str, DemoLimits]) -> dict[str, str]:
        """The demo CRMs that take no more deals now: crm -> "full" (for today), "full_total" or "down"
        (the test account refuses records)."""
        out = {}
        async with self._sessions() as session:
            for crm, demo in limits.items():
                if state := await _crm_state(session, crm, demo):
                    out[crm] = state
        return out

    async def purge(self) -> tuple[int, int]:
        """Runs older than a day go with the document's text; model calls are kept 30 days (no text)."""
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            runs = await session.execute(delete(OrderRun).where(OrderRun.expires_at <= now))
            calls = await session.execute(
                delete(ModelCall).where(ModelCall.created_at < now - timedelta(days=30))
            )
            return int(runs.rowcount), int(calls.rowcount)  # type: ignore[attr-defined]

    # --- price rules ---

    async def get_rules(self, owner: str, target: str) -> dict[str, Any] | None:
        async with self._sessions() as session:
            row = await session.get(OrderRules, (owner, target))
            return dict(row.rules) if row else None

    async def set_rules(self, owner: str, target: str, rules: dict[str, Any]) -> None:
        stmt = insert(OrderRules).values(owner=owner, target=target, rules=rules)
        stmt = stmt.on_conflict_do_update(
            index_elements=["owner", "target"], set_={"rules": rules, "updated_at": func.now()}
        )
        async with self._sessions.begin() as session:
            await session.execute(stmt)

    # --- the model-call ceiling ---

    async def reserve_call(
        self, owner: str, ip_key: str, kind: str, ceiling: int, *, budget: Decimal, worst: Decimal
    ) -> uuid.UUID:
        """A call is booked at its worst cost before it is sent (under the global lock), and the day's
        dollars are the booked or the actual cost of every call: two parallel calls cannot both pass
        the last dollars. ``finish_call`` replaces the booking with what the call really cost."""
        async with self._sessions.begin() as session:
            await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": LOCK_KEY})
            day = ModelCall.created_at > datetime.now(UTC) - timedelta(days=1)
            used, spent = (
                await session.execute(
                    select(func.count(), func.coalesce(func.sum(ModelCall.usd), 0)).where(day)
                )
            ).one()
            if (used or 0) >= ceiling:
                raise CeilingReached(f"{ceiling} model calls per day")
            if Decimal(spent) + worst > budget:
                raise CeilingReached(f"${budget} of model calls per day")
            call = ModelCall(id=uuid.uuid4(), owner=owner, ip_key=ip_key, kind=kind, usd=worst)
            session.add(call)
            return call.id

    async def finish_call(
        self, call_id: uuid.UUID, input_tokens: int, output_tokens: int, usd: Decimal | None, ok: bool
    ) -> None:
        """``usd`` None: no answer came, so the worst-case booking stays as the call's cost."""
        values: dict[str, Any] = {"ok": ok}
        if usd is not None:
            values |= {"input_tokens": input_tokens, "output_tokens": output_tokens, "usd": usd}
        async with self._sessions.begin() as session:
            await session.execute(update(ModelCall).where(ModelCall.id == call_id).values(**values))


async def _lock(session: AsyncSession, cls: int, key: str) -> None:
    await session.execute(text("SELECT pg_advisory_xact_lock(:c, hashtext(:k))"), {"c": cls, "k": key})


async def _demo_writes(session: AsyncSession, *where: Any) -> int:
    since = datetime.now(UTC) - timedelta(days=1)
    return int(
        await session.scalar(
            select(func.count())
            .select_from(OrderRun)
            .where(
                *where,
                OrderRun.demo.is_(True),
                or_(OrderRun.written_at > since, OrderRun.status == "writing"),
            )
        )
        or 0
    )


DEMO_DOWN_FOR = timedelta(minutes=30)


async def _demo_down(session: AsyncSession, crm: str) -> bool:
    """The test account refused a demo write as over its records limit, and nothing was written there
    since: for half an hour the demo of that CRM says it is unavailable instead of failing every
    visitor, then the next run tries again."""
    refused = await session.scalar(
        select(func.max(OrderRun.created_at)).where(
            OrderRun.demo.is_(True),
            OrderRun.crm == crm,
            OrderRun.error == ACCOUNT_LIMIT,
            OrderRun.created_at > datetime.now(UTC) - DEMO_DOWN_FOR,
        )
    )
    if refused is None:
        return False
    written = await session.scalar(
        select(func.count())
        .select_from(OrderRun)
        .where(OrderRun.demo.is_(True), OrderRun.crm == crm, OrderRun.written_at > refused)
    )
    return not written


async def _crm_state(session: AsyncSession, crm: str, demo: DemoLimits) -> str | None:
    if await _demo_down(session, crm):
        return "down"
    if await _demo_writes(session, OrderRun.crm == crm) >= demo.per_crm:
        return "full"
    if demo.total is not None:
        # every deal the demo ever created there (the journal of the platform is kept), and those being
        # written; other solutions' writes into a deal that exists create none
        written = await session.scalar(
            select(func.count())
            .select_from(CrmWrite)
            .where(
                CrmWrite.owner == DEMO_OWNER,
                CrmWrite.crm == crm,
                CrmWrite.action == "order_deal",
                CrmWrite.ok.is_(True),
            )
        )
        flying = await session.scalar(
            select(func.count())
            .select_from(OrderRun)
            .where(OrderRun.demo.is_(True), OrderRun.crm == crm, OrderRun.status == "writing")
        )
        if (written or 0) + (flying or 0) >= demo.total:
            return "full_total"
    return None
