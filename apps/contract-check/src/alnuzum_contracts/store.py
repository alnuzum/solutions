"""What the solution keeps: a run of a comparison for 24 hours, a standard per CRM connection, the money of
every paid call.

The texts of contracts (a run's result, a standard's clauses) are sealed with the platform's keys (AES-GCM,
bound to the row's id): the nightly copy of the database lives on the backup host for two weeks, and the keys
are not there. A run is erased by the sweep after 24 hours or at once by its owner; a standard stays while its
connection does, or until its owner deletes it.

Quotas are counted from these tables, each taken before the work it pays for, in one transaction under an
advisory lock, so parallel requests cannot both take the last one, and an attempt that fails afterwards stays
counted. A write creates no deal: Kommo's demo has a ceiling of its own a day and the deals solution 1 may
still create there are left alone.
"""

import json
import uuid
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from alnuzum_platform.crypto import Keyring
from alnuzum_platform.db import Base

KEEP = timedelta(days=1)
LOCK_SPEND = 0x434F4E54  # the money ceiling of the solution ("CONT")
LOCK_RUNS, LOCK_DEMO = 0x4352, 0x4344  # runs of an owner, an address or a connection; demo writes


class ContractRun(Base):
    __tablename__ = "contract_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    owner: Mapped[str] = mapped_column(String(128))
    ip_key: Mapped[str] = mapped_column(String(64))
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    crm: Mapped[str] = mapped_column(String(16))
    demo: Mapped[bool] = mapped_column(Boolean)
    standard: Mapped[str] = mapped_column(String(64))  # demo:<key> or the id of a standard
    status: Mapped[str] = mapped_column(String(16))
    sealed: Mapped[bytes | None] = mapped_column(LargeBinary)
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # counts only, no text of the contract
    candidates: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)  # deals found by a search
    deal_id: Mapped[str | None] = mapped_column(String(64))
    deal_title: Mapped[str | None] = mapped_column(String(300))
    deal_url: Mapped[str | None] = mapped_column(String(500))
    error: Mapped[str | None] = mapped_column(String(40))
    written_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    write_ip_key: Mapped[str | None] = mapped_column(String(64))
    steps: Mapped[list[str] | None] = mapped_column(JSONB)


class ContractStandard(Base):
    __tablename__ = "contract_standards"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    owner: Mapped[str] = mapped_column(String(128))
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    side: Mapped[str] = mapped_column(String(16))
    clauses: Mapped[int] = mapped_column(Integer)
    sealed: Mapped[bytes] = mapped_column(LargeBinary)


class ContractSpend(Base):
    __tablename__ = "contract_spend"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    owner: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(16))
    usd: Mapped[Decimal] = mapped_column(Numeric(10, 5))
    units: Mapped[int] = mapped_column(Integer, default=0)
    ok: Mapped[bool | None] = mapped_column(Boolean)


class CeilingReached(RuntimeError):
    """The solution's dollars for the day are spent."""


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
    kommo_per_day: int = 10  # Kommo's demo: its technical account is small


@dataclass(frozen=True)
class RunInfo:
    id: uuid.UUID
    created_at: datetime
    owner: str
    connection_id: uuid.UUID
    crm: str
    demo: bool
    standard: str
    status: str
    result: dict[str, Any] | None
    summary: dict[str, Any] | None
    candidates: list[dict[str, Any]]
    deal_id: str | None
    deal_title: str | None
    deal_url: str | None
    error: str | None
    steps: list[str]


@dataclass(frozen=True)
class StandardInfo:
    id: uuid.UUID
    created_at: datetime
    connection_id: uuid.UUID
    side: str
    clauses: int


class Seal:
    """JSON sealed for one row: compressed, then encrypted with the row's id as associated data, so a sealed
    value copied into another row does not open."""

    def __init__(self, keyring: Keyring) -> None:
        self._keyring = keyring

    def __repr__(self) -> str:
        return "Seal()"

    def close(self, value: Any, row: uuid.UUID, kind: str) -> bytes:
        raw = zlib.compress(json.dumps(value, ensure_ascii=False).encode(), 6)
        return self._keyring.encrypt(raw, f"{kind}:{row}".encode())

    def open(self, sealed: bytes, row: uuid.UUID, kind: str) -> Any:
        raw = self._keyring.decrypt(sealed, f"{kind}:{row}".encode())
        return json.loads(zlib.decompress(raw, bufsize=1 << 20))


async def _lock(session: AsyncSession, cls: int, key: str) -> None:
    await session.execute(text("SELECT pg_advisory_xact_lock(:c, hashtext(:k))"), {"c": cls, "k": key})


def _day() -> datetime:
    return datetime.now(UTC) - timedelta(days=1)


class ContractStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], seal: Seal) -> None:
        self._sessions = sessions
        self._seal = seal

    # --- runs ---

    async def reserve_run(
        self,
        *,
        owner: str,
        ip_key: str,
        connection_id: uuid.UUID,
        crm: str,
        demo: bool,
        standard: str,
        limits: Limits,
    ) -> uuid.UUID:
        """Counted before anything is read: by the session, the address and an own CRM's connection."""
        async with self._sessions.begin() as session:
            await _lock(session, LOCK_RUNS, owner)
            await _lock(session, LOCK_RUNS, "ip:" + ip_key)
            if not demo:
                await _lock(session, LOCK_RUNS, f"conn:{connection_id}")
            since = _day()
            count = select(func.count()).select_from(ContractRun).where(ContractRun.created_at > since)
            if (
                int(await session.scalar(count.where(ContractRun.owner == owner)) or 0)
                >= limits.runs_per_owner
            ):
                raise RunsExceeded("owner")
            if (
                int(await session.scalar(count.where(ContractRun.ip_key == ip_key)) or 0)
                >= limits.runs_per_ip
            ):
                raise RunsExceeded("ip")
            if not demo and (
                int(await session.scalar(count.where(ContractRun.connection_id == connection_id)) or 0)
                >= limits.runs_per_connection
            ):
                raise RunsExceeded("connection")
            run = ContractRun(
                id=uuid.uuid4(),
                owner=owner,
                ip_key=ip_key[:64],
                connection_id=connection_id,
                crm=crm,
                demo=demo,
                standard=standard[:64],
                status="reading",
            )
            session.add(run)
            return run.id

    async def fill_run(self, run_id: uuid.UUID, result: dict[str, Any], summary: dict[str, Any]) -> None:
        sealed = self._seal.close(result, run_id, "run")
        async with self._sessions.begin() as session:
            await session.execute(
                update(ContractRun)
                .where(ContractRun.id == run_id)
                .values(status="ready", sealed=sealed, summary=summary)
            )

    async def reject_run(self, run_id: uuid.UUID, error: str) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                update(ContractRun)
                .where(ContractRun.id == run_id)
                .values(status="rejected", error=error[:40])
            )

    async def get_run(self, run_id: uuid.UUID, owner: str) -> RunInfo:
        """A run of this owner younger than 24 hours; anybody else's, an older one or an erased one is not
        found."""
        async with self._sessions() as session:
            row = await session.scalar(
                select(ContractRun).where(
                    ContractRun.id == run_id, ContractRun.owner == owner, ContractRun.created_at > _day()
                )
            )
        if row is None:
            raise NotFound(str(run_id))
        result = self._seal.open(row.sealed, row.id, "run") if row.sealed else None
        return RunInfo(
            row.id,
            row.created_at,
            row.owner,
            row.connection_id,
            row.crm,
            row.demo,
            row.standard,
            row.status,
            result,
            row.summary,
            list(row.candidates or []),
            row.deal_id,
            row.deal_title,
            row.deal_url,
            row.error,
            list(row.steps or []),
        )

    async def erase(self, run_id: uuid.UUID, owner: str) -> bool:
        """At its owner's word, not while it is being written."""
        async with self._sessions.begin() as session:
            done = await session.execute(
                delete(ContractRun).where(
                    ContractRun.id == run_id, ContractRun.owner == owner, ContractRun.status != "writing"
                )
            )
            return bool(done.rowcount)  # type: ignore[attr-defined]

    async def save_candidates(self, run_id: uuid.UUID, owner: str, found: list[dict[str, Any]]) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                update(ContractRun)
                .where(
                    ContractRun.id == run_id,
                    ContractRun.owner == owner,
                    ContractRun.status.in_(("ready", "failed")),
                    ContractRun.steps.is_(None),
                )
                .values(candidates=found[:20])
            )

    async def choose_deal(self, run_id: uuid.UUID, owner: str, deal_id: str, title: str) -> bool:
        """A deal among those the search found; not once a step is written into another."""
        async with self._sessions.begin() as session:
            done = await session.execute(
                update(ContractRun)
                .where(
                    ContractRun.id == run_id,
                    ContractRun.owner == owner,
                    ContractRun.status.in_(("ready", "failed")),
                    ContractRun.steps.is_(None),
                )
                .values(deal_id=deal_id[:64], deal_title=title[:300])
            )
            return bool(done.rowcount)  # type: ignore[attr-defined]

    async def start_writing(
        self, run_id: uuid.UUID, owner: str, *, demo: bool, crm: str, ip_key: str, limits: Limits
    ) -> str:
        """The only way to "writing": from "ready" or "failed", by the owner. Returns "ok", "busy" (written,
        being written), "quota" (the owner's or the address's demo writes of the day) or "full" (this demo
        CRM's of the day)."""
        async with self._sessions.begin() as session:
            await _lock(session, LOCK_DEMO, owner)
            if demo:
                await _lock(session, LOCK_DEMO, "ip:" + ip_key)
                await _lock(session, LOCK_DEMO, "crm:" + crm)
                writing = or_(ContractRun.written_at > _day(), ContractRun.status == "writing")
                count = select(func.count()).select_from(ContractRun).where(ContractRun.demo, writing)
                if (
                    int(await session.scalar(count.where(ContractRun.owner == owner)) or 0)
                    >= limits.demo_per_owner
                ):
                    return "quota"
                if (
                    int(await session.scalar(count.where(ContractRun.write_ip_key == ip_key)) or 0)
                    >= limits.demo_per_ip
                ):
                    return "quota"
                ceiling = (
                    min(limits.demo_per_crm, limits.kommo_per_day) if crm == "kommo" else limits.demo_per_crm
                )
                if int(await session.scalar(count.where(ContractRun.crm == crm)) or 0) >= ceiling:
                    return "full"
            done = await session.execute(
                update(ContractRun)
                .where(
                    ContractRun.id == run_id,
                    ContractRun.owner == owner,
                    ContractRun.status.in_(("ready", "failed")),
                    ContractRun.deal_id.is_not(None),
                )
                .values(status="writing", write_ip_key=ip_key[:64])
            )
            return "ok" if done.rowcount else "busy"  # type: ignore[attr-defined]

    async def step_done(self, run_id: uuid.UUID, step: str) -> None:
        """Kept before the next step begins: a retry skips it."""
        async with self._sessions.begin() as session:
            await session.execute(
                update(ContractRun)
                .where(ContractRun.id == run_id, ContractRun.status == "writing")
                .values(
                    steps=func.coalesce(ContractRun.steps, text("'[]'::jsonb")).op("||")(
                        func.jsonb_build_array(cast(step, String))
                    )
                )
            )

    async def steps_done(self, run_id: uuid.UUID) -> list[str]:
        async with self._sessions() as session:
            found = await session.scalar(select(ContractRun.steps).where(ContractRun.id == run_id))
        return list(found or [])

    async def finish_writing(
        self, run_id: uuid.UUID, *, ok: bool, deal_url: str | None = None, error: str | None = None
    ) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                update(ContractRun)
                .where(ContractRun.id == run_id)
                .values(
                    status="written" if ok else "failed",
                    deal_url=deal_url,
                    error=error,
                    written_at=datetime.now(UTC) if ok else None,
                )
            )

    async def demo_writes(self, crm: str) -> int:
        async with self._sessions() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(ContractRun)
                    .where(ContractRun.crm == crm, ContractRun.demo, ContractRun.written_at > _day())
                )
                or 0
            )

    # --- standards ---

    async def put_standard(
        self, owner: str, connection_id: uuid.UUID, side: str, clauses: list[list[str]]
    ) -> uuid.UUID:
        """One per connection: a new one replaces the old."""
        sid = uuid.uuid4()
        sealed = self._seal.close(clauses, sid, "standard")
        async with self._sessions.begin() as session:
            await _lock(session, LOCK_RUNS, f"standard:{connection_id}")
            await session.execute(
                delete(ContractStandard).where(
                    ContractStandard.owner == owner, ContractStandard.connection_id == connection_id
                )
            )
            session.add(
                ContractStandard(
                    id=sid,
                    owner=owner,
                    connection_id=connection_id,
                    side=side,
                    clauses=len(clauses),
                    sealed=sealed,
                )
            )
        return sid

    async def standards(self, owner: str) -> dict[uuid.UUID, StandardInfo]:
        """By connection."""
        async with self._sessions() as session:
            rows = (
                await session.scalars(select(ContractStandard).where(ContractStandard.owner == owner))
            ).all()
        return {
            r.connection_id: StandardInfo(r.id, r.created_at, r.connection_id, r.side, r.clauses)
            for r in rows
        }

    async def get_standard(self, standard_id: uuid.UUID, owner: str) -> tuple[StandardInfo, list[list[str]]]:
        async with self._sessions() as session:
            row = await session.scalar(
                select(ContractStandard).where(
                    ContractStandard.id == standard_id, ContractStandard.owner == owner
                )
            )
        if row is None:
            raise NotFound(str(standard_id))
        info = StandardInfo(row.id, row.created_at, row.connection_id, row.side, row.clauses)
        return info, list(self._seal.open(row.sealed, row.id, "standard"))

    async def delete_standard(self, owner: str, connection_id: uuid.UUID) -> bool:
        async with self._sessions.begin() as session:
            done = await session.execute(
                delete(ContractStandard).where(
                    ContractStandard.owner == owner, ContractStandard.connection_id == connection_id
                )
            )
            return bool(done.rowcount)  # type: ignore[attr-defined]

    # --- money ---

    async def reserve_spend(self, owner: str, kind: str, *, budget: Decimal, worst: Decimal) -> uuid.UUID:
        """Booked at its worst cost before the call, under the global lock: the day's dollars are the booked
        or the actual cost of every call."""
        async with self._sessions.begin() as session:
            await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": LOCK_SPEND})
            spent = await session.scalar(
                select(func.coalesce(func.sum(ContractSpend.usd), 0)).where(ContractSpend.created_at > _day())
            )
            if Decimal(spent or 0) + worst > budget:
                raise CeilingReached(f"${budget} per day")
            row = ContractSpend(id=uuid.uuid4(), owner=owner, kind=kind[:16], usd=worst, units=0, ok=None)
            session.add(row)
            return row.id

    async def finish_spend(self, spend_id: uuid.UUID, usd: Decimal | None, units: int, ok: bool) -> None:
        """``usd`` None: no answer came, the booking stays."""
        values: dict[str, Any] = {"ok": ok, "units": units}
        if usd is not None:
            values["usd"] = usd
        async with self._sessions.begin() as session:
            await session.execute(update(ContractSpend).where(ContractSpend.id == spend_id).values(**values))

    async def spent_today(self) -> Decimal:
        async with self._sessions() as session:
            return Decimal(
                await session.scalar(
                    select(func.coalesce(func.sum(ContractSpend.usd), 0)).where(
                        ContractSpend.created_at > _day()
                    )
                )
                or 0
            )

    # --- the sweep ---

    async def purge(self) -> tuple[int, int]:
        """Runs older than 24 hours, standards of connections that are gone, spends older than 30 days."""
        cut = datetime.now(UTC) - KEEP
        async with self._sessions.begin() as session:
            runs = await session.execute(delete(ContractRun).where(ContractRun.created_at < cut))
            standards = await session.execute(
                text(
                    "DELETE FROM contract_standards s WHERE NOT EXISTS (SELECT 1 FROM crm_connections c "
                    "WHERE c.id = s.connection_id AND c.owner = s.owner)"
                )
            )
            await session.execute(
                delete(ContractSpend).where(ContractSpend.created_at < cut - timedelta(days=30))
            )
        return int(runs.rowcount), int(standards.rowcount)  # type: ignore[attr-defined]
