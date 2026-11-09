"""What the solution keeps: a run of a letter for 24 hours, the routing rules of an owner per CRM, the money
of every paid call, and the records the demo created in the test accounts.

The letter, what was read out of it and what the CRM was found to hold are sealed with the platform's keys
(AES-GCM, bound to the row's id): the nightly copy of the database lives on the backup host for two weeks and
the keys are not there. A run is erased by the sweep after 24 hours or at once by its owner; its row stays a
tombstone the quotas still count. The demo's records are only ids in the test accounts: they are kept, since
those accounts keep the records (amoCRM and Kommo cannot delete them through the API).

Quotas are counted from these tables, each taken before the work it pays for, in one transaction under an
advisory lock, so parallel requests cannot both take the last one, and an attempt that fails stays counted."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from alnuzum_contracts.store import Seal
from sqlalchemy import (
    Boolean,
    DateTime,
    Integer,
    LargeBinary,
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

KEEP = timedelta(days=1)
STUCK = timedelta(minutes=30)
DEMO_DOWN_FOR = timedelta(minutes=30)
RULES_KEEP = timedelta(days=30)  # the rules of a demo CRM, unchanged this long, are dropped
LOCK_SPEND = 0x4D41494C  # the money ceiling of the solution ("MAIL")
ADDRESS_SHARE = Decimal("0.25")  # of the day's dollars one address may spend (never less than one booking)
LOCK_RUNS, LOCK_DEMO = 0x4D52, 0x4D44  # runs of an owner, an address or a connection; demo writes
MAX_CREATES = 3  # records one write of a new request may create: a company, a contact, a deal
SMALL = frozenset({"amocrm", "kommo"})  # technical accounts of 100 contacts and companies in all
CLIENTS = ("company", "contact")  # what a technical account counts to its 100; deals it does not


class MailRun(Base):
    __tablename__ = "mail_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    owner: Mapped[str] = mapped_column(String(128))
    ip_key: Mapped[str] = mapped_column(String(64))
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    crm: Mapped[str] = mapped_column(String(16))
    demo: Mapped[bool] = mapped_column(Boolean)
    status: Mapped[str] = mapped_column(String(16))
    sealed: Mapped[bytes | None] = mapped_column(LargeBinary)
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # the class and counts, no text
    steps: Mapped[list[str] | None] = mapped_column(
        JSONB
    )  # company:<id>, contact:<id>, deal:<id>, note, task
    deal_url: Mapped[str | None] = mapped_column(String(500))
    error: Mapped[str | None] = mapped_column(String(40))
    written_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    write_ip_key: Mapped[str | None] = mapped_column(String(64))
    writing_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    erase_after: Mapped[bool] = mapped_column(Boolean, default=False)


class MailRules(Base):
    __tablename__ = "mail_rules"

    owner: Mapped[str] = mapped_column(String(128), primary_key=True)
    target: Mapped[str] = mapped_column(String(64), primary_key=True)  # demo:<crm> or a connection's id
    rules: Mapped[dict[str, Any]] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MailSpend(Base):
    __tablename__ = "mail_spend"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    owner: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(16))
    usd: Mapped[Decimal] = mapped_column(Numeric(10, 5))
    units: Mapped[int] = mapped_column(Integer, default=0)
    ok: Mapped[bool | None] = mapped_column(Boolean)
    ip_key: Mapped[str | None] = mapped_column(String(64))  # the address that asked: its share of the day


class MailDemoRecord(Base):
    __tablename__ = "mail_demo_records"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    crm: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(16))  # company | contact | deal
    record_id: Mapped[str] = mapped_column(String(64))
    ip_key: Mapped[str | None] = mapped_column(String(64))  # the address whose write created it


class CeilingReached(RuntimeError):
    """The solution's dollars for the day are spent."""


class AddressCeilingReached(RuntimeError):
    """One address's share of the day's dollars is spent; the others still go."""


class RunsExceeded(RuntimeError):
    pass


class NotFound(LookupError):
    pass


@dataclass(frozen=True)
class Limits:
    runs_per_owner: int = 10
    runs_per_ip: int = 20
    runs_per_connection: int = 30
    demo_per_owner: int = 5
    demo_per_ip: int = 8
    demo_per_crm: int = 40
    # records the demo may create in a test account (companies, contacts, deals): a day and in all
    new_per_day: int = 15
    new_total: int = 200
    # amoCRM and Kommo count companies and contacts only: a day, by one address a day (each kind), and
    # the account's own live count with what is being created never past ``small_room`` of its 100
    small_new_per_day: int = 6
    small_per_ip: int = 1
    small_room: int = 70


@dataclass(frozen=True)
class RunInfo:
    id: uuid.UUID
    created_at: datetime
    owner: str
    connection_id: uuid.UUID
    crm: str
    demo: bool
    status: str
    result: dict[str, Any] | None
    steps: list[str]
    deal_url: str | None
    error: str | None
    erase_after: bool = False


async def _lock(session: AsyncSession, cls: int, key: str) -> None:
    await session.execute(text("SELECT pg_advisory_xact_lock(:c, hashtext(:k))"), {"c": cls, "k": key})


def _day() -> datetime:
    return datetime.now(UTC) - timedelta(days=1)


# what an erased run keeps: the fields the quotas count; the letter, what was read and found are gone
TOMBSTONE: dict[str, Any] = {
    "status": "erased",
    "sealed": None,
    "summary": None,
    "deal_url": None,
    "erase_after": False,
}


async def _demo_down(session: AsyncSession, crm: str) -> bool:
    """The test account refused a demo record as over its limit, and nothing was written there since: for half
    an hour the demo of that CRM says it is unavailable instead of failing every visitor."""
    refused = await session.scalar(
        select(func.max(MailRun.writing_at)).where(
            MailRun.demo,
            MailRun.crm == crm,
            MailRun.error == ACCOUNT_LIMIT,
            MailRun.writing_at > datetime.now(UTC) - DEMO_DOWN_FOR,
        )
    )
    if refused is None:
        return False
    written = await session.scalar(
        select(func.count())
        .select_from(MailRun)
        .where(MailRun.demo, MailRun.crm == crm, MailRun.written_at > refused)
    )
    return not written


async def _room(
    session: AsyncSession,
    crm: str,
    ip_key: str,
    creates: list[str],
    limits: Limits,
    *,
    live: tuple[int, datetime] | None,
    run_id: uuid.UUID | None,
) -> bool:
    """Whether the demo may still create ``creates`` in this test account. Writes of this CRM going on now
    may still create theirs: each is counted at its most."""
    made = select(func.count()).select_from(MailDemoRecord).where(MailDemoRecord.crm == crm)
    going_q = (
        select(func.count())
        .select_from(MailRun)
        .where(MailRun.demo, MailRun.crm == crm, MailRun.status == "writing")
    )
    if run_id is not None:
        going_q = going_q.where(MailRun.id != run_id)
    going = int(await session.scalar(going_q) or 0)
    if crm not in SMALL:
        today = int(await session.scalar(made.where(MailDemoRecord.created_at > _day())) or 0)
        ever = int(await session.scalar(made) or 0)
        held = MAX_CREATES * going
        return (
            today + held + len(creates) <= limits.new_per_day
            and ever + held + len(creates) <= limits.new_total
        )
    clients = [k for k in creates if k in CLIENTS]
    if not clients:
        return True  # a deal: the technical account does not count deals
    if live is None:
        return False  # the account's count is unknown: nothing new is made in it
    mine = made.where(MailDemoRecord.ip_key == ip_key[:64], MailDemoRecord.created_at > _day())
    for kind in set(clients):
        if int(await session.scalar(mine.where(MailDemoRecord.kind == kind)) or 0) >= limits.small_per_ip:
            return False
    kinds = made.where(MailDemoRecord.kind.in_(CLIENTS))
    held = len(CLIENTS) * going
    today = int(await session.scalar(kinds.where(MailDemoRecord.created_at > _day())) or 0)
    if today + held + len(clients) > limits.small_new_per_day:
        return False
    count, taken = live
    since = int(await session.scalar(kinds.where(MailDemoRecord.created_at > taken)) or 0)
    return count + since + held + len(clients) <= limits.small_room


class MailStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], seal: Seal) -> None:
        self._sessions = sessions
        self._seal = seal

    # --- runs ---

    async def reserve_run(
        self, *, owner: str, ip_key: str, connection_id: uuid.UUID, crm: str, demo: bool, limits: Limits
    ) -> uuid.UUID:
        """Counted before anything is read: by the session, the address and an own CRM's connection."""
        async with self._sessions.begin() as session:
            await _lock(session, LOCK_RUNS, owner)
            await _lock(session, LOCK_RUNS, "ip:" + ip_key)
            if not demo:
                await _lock(session, LOCK_RUNS, f"conn:{connection_id}")
            count = select(func.count()).select_from(MailRun).where(MailRun.created_at > _day())
            if int(await session.scalar(count.where(MailRun.owner == owner)) or 0) >= limits.runs_per_owner:
                raise RunsExceeded("owner")
            if int(await session.scalar(count.where(MailRun.ip_key == ip_key)) or 0) >= limits.runs_per_ip:
                raise RunsExceeded("ip")
            if not demo and (
                int(await session.scalar(count.where(MailRun.connection_id == connection_id)) or 0)
                >= limits.runs_per_connection
            ):
                raise RunsExceeded("connection")
            run = MailRun(
                id=uuid.uuid4(),
                owner=owner,
                ip_key=ip_key[:64],
                connection_id=connection_id,
                crm=crm,
                demo=demo,
                status="reading",
            )
            session.add(run)
            return run.id

    async def fill_run(self, run_id: uuid.UUID, result: dict[str, Any], summary: dict[str, Any]) -> None:
        sealed = self._seal.close(result, run_id, "mail")
        async with self._sessions.begin() as session:
            await session.execute(
                update(MailRun)
                .where(MailRun.id == run_id, MailRun.status == "reading")
                .values(status="ready", sealed=sealed, summary=summary)
            )

    async def save_result(self, run_id: uuid.UUID, owner: str, result: dict[str, Any]) -> bool:
        """A fresh look into the CRM or a person's choices; not once a step is written."""
        sealed = self._seal.close(result, run_id, "mail")
        async with self._sessions.begin() as session:
            done = await session.execute(
                update(MailRun)
                .where(
                    MailRun.id == run_id,
                    MailRun.owner == owner,
                    MailRun.status.in_(("ready", "failed")),
                    MailRun.steps.is_(None),
                )
                .values(sealed=sealed)
            )
            return bool(done.rowcount)  # type: ignore[attr-defined]

    async def reject_run(self, run_id: uuid.UUID, error: str) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                update(MailRun)
                .where(MailRun.id == run_id, MailRun.status == "reading")
                .values(status="rejected", error=error[:40])
            )

    async def get_run(self, run_id: uuid.UUID, owner: str) -> RunInfo:
        """A run of this owner younger than 24 hours; anybody else's, an older one or an erased one is not
        found."""
        async with self._sessions() as session:
            row = await session.scalar(
                select(MailRun).where(
                    MailRun.id == run_id,
                    MailRun.owner == owner,
                    MailRun.created_at > _day(),
                    MailRun.status != "erased",
                )
            )
        if row is None:
            raise NotFound(str(run_id))
        result = self._seal.open(row.sealed, row.id, "mail") if row.sealed else None
        return RunInfo(
            row.id,
            row.created_at,
            row.owner,
            row.connection_id,
            row.crm,
            row.demo,
            row.status,
            result,
            list(row.steps or []),
            row.deal_url,
            row.error,
            bool(row.erase_after),
        )

    async def close_junk(self, run_id: uuid.UUID, owner: str) -> bool:
        """Junk: nothing goes to the CRM, the run is closed."""
        async with self._sessions.begin() as session:
            done = await session.execute(
                update(MailRun)
                .where(
                    MailRun.id == run_id,
                    MailRun.owner == owner,
                    MailRun.status.in_(("ready", "failed")),
                    MailRun.steps.is_(None),
                )
                .values(status="closed")
            )
            return bool(done.rowcount)  # type: ignore[attr-defined]

    async def erase(self, run_id: uuid.UUID, owner: str) -> str:
        """At its owner's word: erased at once, the row a tombstone the quotas still count ("erased"); while
        it is being written, erased the moment the write ends ("pending"). "missing": not this owner's, or
        gone."""
        async with self._sessions.begin() as session:
            row = await session.scalar(
                select(MailRun)
                .where(MailRun.id == run_id, MailRun.owner == owner, MailRun.status != "erased")
                .with_for_update()
            )
            if row is None:
                return "missing"
            if row.status == "writing":
                row.erase_after = True
                return "pending"
            for name, value in TOMBSTONE.items():
                setattr(row, name, value)
            return "erased"

    async def start_writing(
        self,
        run_id: uuid.UUID,
        owner: str,
        *,
        demo: bool,
        crm: str,
        ip_key: str,
        creates: list[str],
        limits: Limits,
        live: tuple[int, datetime] | None = None,
    ) -> str:
        """The only way to "writing": from "ready" or "failed", by the owner. Returns "ok", "busy", "quota"
        (the owner's or the address's demo writes of the day), "full" (this demo CRM's of the day), "full_new"
        (the records the demo may still create in this test account) or "down" (it refuses records now).
        ``creates``: the kinds of records the write makes; ``live``: amoCRM's and Kommo's count of companies
        and contacts and when it was taken."""
        async with self._sessions.begin() as session:
            await _lock(session, LOCK_DEMO, owner)
            if demo:
                await _lock(session, LOCK_DEMO, "ip:" + ip_key)
                await _lock(session, LOCK_DEMO, "crm:" + crm)
                if await _demo_down(session, crm):
                    return "down"
                writing = or_(MailRun.written_at > _day(), MailRun.status == "writing")
                count = select(func.count()).select_from(MailRun).where(MailRun.demo, writing)
                if (
                    int(await session.scalar(count.where(MailRun.owner == owner)) or 0)
                    >= limits.demo_per_owner
                ):
                    return "quota"
                if (
                    int(await session.scalar(count.where(MailRun.write_ip_key == ip_key)) or 0)
                    >= limits.demo_per_ip
                ):
                    return "quota"
                if int(await session.scalar(count.where(MailRun.crm == crm)) or 0) >= limits.demo_per_crm:
                    return "full"
                if creates and not await _room(
                    session, crm, ip_key, creates, limits, live=live, run_id=run_id
                ):
                    return "full_new"
            done = await session.execute(
                update(MailRun)
                .where(MailRun.id == run_id, MailRun.owner == owner, MailRun.status.in_(("ready", "failed")))
                .values(status="writing", write_ip_key=ip_key[:64], writing_at=func.now(), error=None)
            )
            return "ok" if done.rowcount else "busy"  # type: ignore[attr-defined]

    async def may_create(
        self, crm: str, ip_key: str, limits: Limits, *, live: tuple[int, datetime] | None
    ) -> bool:
        """Whether a write from this address could make a new company and contact in this test account now
        (asked when the letter is read; the write asks again under its lock)."""
        async with self._sessions() as session:
            return await _room(session, crm, ip_key, list(CLIENTS), limits, live=live, run_id=None)

    async def step_done(
        self,
        run_id: uuid.UUID,
        step: str,
        *,
        crm: str,
        demo: bool,
        created: str | None,
        ip_key: str | None = None,
    ) -> None:
        """Kept before the next step begins: a retry skips it. A record the demo created goes to its journal
        in the same transaction."""
        async with self._sessions.begin() as session:
            await session.execute(
                update(MailRun)
                .where(MailRun.id == run_id, MailRun.status == "writing")
                .values(
                    steps=func.coalesce(MailRun.steps, text("'[]'::jsonb")).op("||")(
                        func.jsonb_build_array(cast(step[:80], String))
                    )
                )
            )
            if demo and created:
                session.add(
                    MailDemoRecord(
                        id=uuid.uuid4(),
                        crm=crm,
                        kind=created,
                        record_id=step.partition(":")[2][:64],
                        ip_key=ip_key[:64] if ip_key else None,
                    )
                )

    async def steps_done(self, run_id: uuid.UUID) -> list[str]:
        async with self._sessions() as session:
            found = await session.scalar(select(MailRun.steps).where(MailRun.id == run_id))
        return list(found or [])

    async def finish_writing(
        self, run_id: uuid.UUID, *, ok: bool, deal_url: str | None = None, error: str | None = None
    ) -> None:
        async with self._sessions.begin() as session:
            row = await session.get(MailRun, run_id, with_for_update=True)
            if row is None:
                return
            row.status = "written" if ok else "failed"
            row.deal_url, row.error = deal_url, error
            row.written_at = datetime.now(UTC) if ok else None
            if row.erase_after:  # its owner asked to erase it while it was being written
                for name, value in TOMBSTONE.items():
                    setattr(row, name, value)

    async def release_stuck(self) -> tuple[int, int]:
        """Runs a stopped process left behind: "writing" since ``STUCK`` becomes "failed" (a retry goes on
        from the first step not done; one its owner asked to erase is erased now), "reading" becomes
        "rejected"."""
        cut = datetime.now(UTC) - STUCK
        async with self._sessions.begin() as session:
            stuck = (
                await session.scalars(
                    select(MailRun)
                    .where(
                        MailRun.status.in_(("writing", "reading")),
                        or_(MailRun.writing_at.is_(None), MailRun.writing_at < cut),
                        MailRun.created_at < cut,
                    )
                    .with_for_update()
                )
            ).all()
            writing = 0
            for row in stuck:
                writing += row.status == "writing"
                row.status, row.error = ("failed" if row.status == "writing" else "rejected"), "interrupted"
                if row.erase_after:
                    for name, value in TOMBSTONE.items():
                        setattr(row, name, value)
            return writing, len(stuck) - writing

    async def demo_records(self, crm: str) -> tuple[int, int]:
        """Records the demo created in this test account: in the last 24 hours, in all."""
        async with self._sessions() as session:
            made = select(func.count()).select_from(MailDemoRecord).where(MailDemoRecord.crm == crm)
            return (
                int(await session.scalar(made.where(MailDemoRecord.created_at > _day())) or 0),
                int(await session.scalar(made) or 0),
            )

    # --- rules ---

    async def get_rules(self, owner: str, target: str) -> dict[str, Any] | None:
        async with self._sessions() as session:
            row = await session.get(MailRules, (owner, target))
            return dict(row.rules) if row else None

    async def set_rules(self, owner: str, target: str, rules: dict[str, Any]) -> None:
        stmt = insert(MailRules).values(owner=owner, target=target[:64], rules=rules)
        stmt = stmt.on_conflict_do_update(
            index_elements=["owner", "target"], set_={"rules": rules, "updated_at": func.now()}
        )
        async with self._sessions.begin() as session:
            await session.execute(stmt)

    # --- money ---

    async def reserve_spend(
        self,
        owner: str,
        kind: str,
        *,
        budget: Decimal,
        worst: Decimal,
        ip_key: str | None = None,
        share: Decimal | None = None,
    ) -> uuid.UUID:
        """Booked at its worst cost before the call, under the global lock: within the day's dollars and, by
        an address, within its ``share`` of them (fixed by the caller, never by the call's own cost)."""
        async with self._sessions.begin() as session:
            await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": LOCK_SPEND})
            day = select(func.coalesce(func.sum(MailSpend.usd), 0)).where(MailSpend.created_at > _day())
            spent = await session.scalar(day)
            if Decimal(spent or 0) + worst > budget:
                raise CeilingReached(f"${budget} per day")
            if ip_key is not None and share is not None:
                mine = await session.scalar(day.where(MailSpend.ip_key == ip_key[:64]))
                if Decimal(mine or 0) + worst > share:
                    raise AddressCeilingReached(f"${share} per day")
            row = MailSpend(
                id=uuid.uuid4(),
                owner=owner,
                kind=kind[:16],
                usd=worst,
                units=0,
                ok=None,
                ip_key=ip_key[:64] if ip_key is not None else None,
            )
            session.add(row)
            return row.id

    async def finish_spend(self, spend_id: uuid.UUID, usd: Decimal | None, units: int, ok: bool) -> None:
        """``usd`` None: no answer came, the booking stays."""
        values: dict[str, Any] = {"ok": ok, "units": units}
        if usd is not None:
            values["usd"] = usd
        async with self._sessions.begin() as session:
            await session.execute(update(MailSpend).where(MailSpend.id == spend_id).values(**values))

    async def spent_today(self) -> Decimal:
        async with self._sessions() as session:
            return Decimal(
                await session.scalar(
                    select(func.coalesce(func.sum(MailSpend.usd), 0)).where(MailSpend.created_at > _day())
                )
                or 0
            )

    # --- the sweep ---

    async def purge(self) -> tuple[int, int]:
        """The letter of a run older than 24 hours always (the row stays a tombstone while a quota still
        counts its write), tombstones nothing counts any more, rules of connections that are not active and of
        demo CRMs unchanged for 30 days, spends older than 30 days. The demo's journal of records stays."""
        cut = datetime.now(UTC) - KEEP
        async with self._sessions.begin() as session:
            await session.execute(
                update(MailRun)
                .where(MailRun.created_at < cut, MailRun.status != "erased")
                .values(**TOMBSTONE)
            )
            runs = await session.execute(
                delete(MailRun).where(
                    MailRun.created_at < cut, or_(MailRun.written_at.is_(None), MailRun.written_at < cut)
                )
            )
            rules = await session.execute(
                text(
                    "DELETE FROM mail_rules r WHERE (r.target LIKE 'demo:%' AND r.updated_at < :old) OR "
                    "(r.target NOT LIKE 'demo:%' AND NOT EXISTS (SELECT 1 FROM crm_connections c "
                    "WHERE c.id::text = r.target AND c.owner = r.owner AND c.status = 'active'))"
                ),
                {"old": datetime.now(UTC) - RULES_KEEP},
            )
            await session.execute(delete(MailSpend).where(MailSpend.created_at < cut - timedelta(days=30)))
        return int(runs.rowcount), int(rules.rowcount)  # type: ignore[attr-defined]
