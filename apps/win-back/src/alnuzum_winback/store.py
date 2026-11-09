"""What the solution keeps: a scan for 24 hours (the clients it found and the screens opened, sealed with the
platform's keys and bound to the row), and a journal of the tasks it set.

Quotas are counted from these tables, each taken before the work it pays for, in one transaction under an
advisory lock: scans by the session, the address and an own CRM's connection; tasks by the session, the
address and the demo CRM. A new cookie therefore starts no new day. The journal also tells a demo task's age:
the demo closes its tasks after ``DEMO_TASK_TTL``, so the button never stays dead for the next visitor."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from alnuzum_contracts.store import Seal
from sqlalchemy import Boolean, DateTime, LargeBinary, String, Uuid, delete, func, select, text, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from alnuzum_platform.db import Base

KEEP = timedelta(days=1)
JOURNAL_KEEP = timedelta(days=30)
STUCK = timedelta(minutes=10)  # a scan reads 60 s at most, a task is one request or three
DEMO_TASK_TTL = timedelta(minutes=10)
LOCK_SCANS, LOCK_TASKS = 0x5753, 0x5754  # "WS", "WT"


class WinbackScan(Base):
    __tablename__ = "winback_scans"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    owner: Mapped[str] = mapped_column(String(128))
    ip_key: Mapped[str] = mapped_column(String(64))
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    crm: Mapped[str] = mapped_column(String(16))
    demo: Mapped[bool] = mapped_column(Boolean)
    status: Mapped[str] = mapped_column(String(16))  # reading, ready, rejected
    sealed: Mapped[bytes | None] = mapped_column(LargeBinary)
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # counts only, no names
    error: Mapped[str | None] = mapped_column(String(40))


class WinbackTask(Base):
    __tablename__ = "winback_tasks"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    owner: Mapped[str] = mapped_column(String(128))
    ip_key: Mapped[str] = mapped_column(String(64))
    scan_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    crm: Mapped[str] = mapped_column(String(16))
    demo: Mapped[bool] = mapped_column(Boolean)
    client: Mapped[str] = mapped_column(String(80))  # company:<id> or contact:<id>
    status: Mapped[str] = mapped_column(String(16))  # writing, written, standing, failed, closed
    task_id: Mapped[str | None] = mapped_column(String(64))
    due: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String(40))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ScansExceeded(RuntimeError):
    pass


class NotFound(LookupError):
    pass


@dataclass(frozen=True)
class Limits:
    scans_per_owner: int = 15
    scans_per_ip: int = 30
    scans_per_connection: int = 5  # an own CRM: a scan reads up to 2,000 deals
    tasks_per_owner: int = 20
    tasks_per_ip: int = 40
    demo_tasks_per_owner: int = 5
    demo_tasks_per_ip: int = 8
    demo_tasks_per_crm: int = 40


@dataclass(frozen=True)
class ScanInfo:
    id: uuid.UUID
    created_at: datetime
    owner: str
    connection_id: uuid.UUID
    crm: str
    demo: bool
    status: str
    result: dict[str, Any]
    summary: dict[str, Any]


@dataclass(frozen=True)
class TaskInfo:
    id: uuid.UUID
    created_at: datetime
    client: str
    status: str
    task_id: str | None
    due: datetime | None
    error: str | None


async def _lock(session: AsyncSession, cls: int, key: str) -> None:
    await session.execute(text("SELECT pg_advisory_xact_lock(:c, hashtext(:k))"), {"c": cls, "k": key})


def _day(now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)) - KEEP


def _task(row: WinbackTask) -> TaskInfo:
    return TaskInfo(row.id, row.created_at, row.client, row.status, row.task_id, row.due, row.error)


class WinbackStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], seal: Seal) -> None:
        self._sessions = sessions
        self._seal = seal

    # --- scans ---

    async def reserve_scan(
        self,
        *,
        owner: str,
        ip_key: str,
        connection_id: uuid.UUID,
        crm: str,
        demo: bool,
        limits: Limits,
        now: datetime,
    ) -> uuid.UUID:
        """Counted before anything is read: by the session, the address and an own CRM's connection."""
        async with self._sessions.begin() as session:
            await _lock(session, LOCK_SCANS, owner)
            await _lock(session, LOCK_SCANS, "ip:" + ip_key)
            if not demo:
                await _lock(session, LOCK_SCANS, f"conn:{connection_id}")
            count = select(func.count()).select_from(WinbackScan).where(WinbackScan.created_at > _day(now))
            if (
                int(await session.scalar(count.where(WinbackScan.owner == owner)) or 0)
                >= limits.scans_per_owner
            ):
                raise ScansExceeded("owner")
            if (
                int(await session.scalar(count.where(WinbackScan.ip_key == ip_key)) or 0)
                >= limits.scans_per_ip
            ):
                raise ScansExceeded("ip")
            if not demo and (
                int(await session.scalar(count.where(WinbackScan.connection_id == connection_id)) or 0)
                >= limits.scans_per_connection
            ):
                raise ScansExceeded("connection")
            row = WinbackScan(
                id=uuid.uuid4(),
                created_at=now,
                owner=owner,
                ip_key=ip_key[:64],
                connection_id=connection_id,
                crm=crm,
                demo=demo,
                status="reading",
            )
            session.add(row)
            return row.id

    async def fill_scan(self, scan_id: uuid.UUID, result: dict[str, Any], summary: dict[str, Any]) -> None:
        sealed = self._seal.close(result, scan_id, "winback")
        async with self._sessions.begin() as session:
            await session.execute(
                update(WinbackScan)
                .where(WinbackScan.id == scan_id)
                .values(status="ready", sealed=sealed, summary=summary)
            )

    async def reject_scan(self, scan_id: uuid.UUID, error: str) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                update(WinbackScan)
                .where(WinbackScan.id == scan_id)
                .values(status="rejected", error=error[:40])
            )

    async def get_scan(self, scan_id: uuid.UUID, owner: str, now: datetime) -> ScanInfo:
        """A ready scan of this owner younger than 24 hours; anybody else's, an older one or one not read is
        not found."""
        async with self._sessions() as session:
            row = await session.scalar(
                select(WinbackScan).where(
                    WinbackScan.id == scan_id,
                    WinbackScan.owner == owner,
                    WinbackScan.created_at > _day(now),
                    WinbackScan.status == "ready",
                )
            )
        if row is None or row.sealed is None:
            raise NotFound(str(scan_id))
        result = self._seal.open(row.sealed, row.id, "winback")
        return ScanInfo(
            row.id, row.created_at, row.owner, row.connection_id, row.crm, row.demo, row.status, result,
            dict(row.summary or {}),
        )  # fmt: skip

    async def put_detail(self, scan_id: uuid.UUID, owner: str, n: int, value: dict[str, Any]) -> None:
        """The screen of client ``n`` kept in the scan: its products are read once."""
        async with self._sessions.begin() as session:
            row = await session.scalar(
                select(WinbackScan)
                .where(WinbackScan.id == scan_id, WinbackScan.owner == owner, WinbackScan.status == "ready")
                .with_for_update()
            )
            if row is None or row.sealed is None:
                return
            result = self._seal.open(row.sealed, row.id, "winback")
            result.setdefault("details", {})[str(n)] = value
            row.sealed = self._seal.close(result, row.id, "winback")

    # --- tasks ---

    async def start_task(
        self,
        *,
        owner: str,
        ip_key: str,
        scan_id: uuid.UUID,
        connection_id: uuid.UUID,
        crm: str,
        demo: bool,
        client: str,
        limits: Limits,
        now: datetime,
    ) -> tuple[str, uuid.UUID | None]:
        """A journal row "writing" for one task; "ok" with its id, or "busy" (another request writes a task on
        this client now), "quota" (the session's or the address's) or "full" (the demo CRM's of the day)."""
        async with self._sessions.begin() as session:
            await _lock(session, LOCK_TASKS, f"client:{connection_id}:{client}")
            await _lock(session, LOCK_TASKS, owner)
            await _lock(session, LOCK_TASKS, "ip:" + ip_key)
            if demo:
                await _lock(session, LOCK_TASKS, "crm:" + crm)
            writing = await session.scalar(
                select(func.count())
                .select_from(WinbackTask)
                .where(
                    WinbackTask.connection_id == connection_id,
                    WinbackTask.client == client,
                    WinbackTask.status == "writing",
                    WinbackTask.created_at > now - STUCK,
                )
            )
            if writing:
                return "busy", None
            count = (
                select(func.count())
                .select_from(WinbackTask)
                .where(
                    WinbackTask.created_at > _day(now),
                    WinbackTask.demo.is_(demo),
                    WinbackTask.status.in_(("writing", "written", "closed", "failed")),
                )
            )
            per_owner, per_ip = (
                (limits.demo_tasks_per_owner, limits.demo_tasks_per_ip)
                if demo
                else (limits.tasks_per_owner, limits.tasks_per_ip)
            )
            if int(await session.scalar(count.where(WinbackTask.owner == owner)) or 0) >= per_owner:
                return "quota", None
            if int(await session.scalar(count.where(WinbackTask.ip_key == ip_key)) or 0) >= per_ip:
                return "quota", None
            if demo and (
                int(await session.scalar(count.where(WinbackTask.crm == crm)) or 0)
                >= limits.demo_tasks_per_crm
            ):
                return "full", None
            row = WinbackTask(
                id=uuid.uuid4(),
                created_at=now,
                owner=owner,
                ip_key=ip_key[:64],
                scan_id=scan_id,
                connection_id=connection_id,
                crm=crm,
                demo=demo,
                client=client[:80],
                status="writing",
            )
            session.add(row)
            return "ok", row.id

    async def finish_task(
        self,
        row_id: uuid.UUID,
        status: str,
        *,
        task_id: str | None = None,
        due: datetime | None = None,
        error: str | None = None,
    ) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                update(WinbackTask)
                .where(WinbackTask.id == row_id)
                .values(
                    status=status,
                    task_id=task_id[:64] if task_id else None,
                    due=due,
                    error=error[:40] if error else None,
                )
            )

    async def last_task(self, scan_id: uuid.UUID, owner: str, client: str) -> TaskInfo | None:
        """What this visitor last did on this client in this scan (the screen says it)."""
        async with self._sessions() as session:
            row = await session.scalar(
                select(WinbackTask)
                .where(
                    WinbackTask.scan_id == scan_id, WinbackTask.owner == owner, WinbackTask.client == client
                )
                .order_by(WinbackTask.created_at.desc())
                .limit(1)
            )
        return _task(row) if row else None

    async def demo_ages(self, crm: str, task_ids: list[str]) -> dict[str, datetime]:
        """When the demo set these tasks (those it did not set are absent)."""
        if not task_ids:
            return {}
        async with self._sessions() as session:
            rows = await session.execute(
                select(WinbackTask.task_id, WinbackTask.created_at).where(
                    WinbackTask.demo, WinbackTask.crm == crm, WinbackTask.task_id.in_(task_ids)
                )
            )
        return {str(t): at for t, at in rows.all() if t}

    async def closed(self, crm: str, task_id: str) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                update(WinbackTask)
                .where(WinbackTask.demo, WinbackTask.crm == crm, WinbackTask.task_id == task_id)
                .values(status="closed", closed_at=func.now())
            )

    async def stale_demo_tasks(self, now: datetime, limit: int = 100) -> list[tuple[str, str]]:
        """Demo tasks older than their time and not closed yet: (crm, task id)."""
        async with self._sessions() as session:
            rows = await session.execute(
                select(WinbackTask.crm, WinbackTask.task_id)
                .where(
                    WinbackTask.demo,
                    WinbackTask.status == "written",
                    WinbackTask.task_id.is_not(None),
                    WinbackTask.created_at < now - DEMO_TASK_TTL,
                )
                .order_by(WinbackTask.created_at)
                .limit(limit)
            )
        return [(str(c), str(t)) for c, t in rows.all()]

    # --- upkeep ---

    async def release_stuck(self) -> tuple[int, int]:
        """A stopped process leaves scans "reading" and tasks "writing": they are rejected and failed."""
        cut = datetime.now(UTC) - STUCK
        async with self._sessions.begin() as session:
            scans = await session.execute(
                update(WinbackScan)
                .where(WinbackScan.status == "reading", WinbackScan.created_at < cut)
                .values(status="rejected", error="interrupted")
            )
            tasks = await session.execute(
                update(WinbackTask)
                .where(WinbackTask.status == "writing", WinbackTask.created_at < cut)
                .values(status="failed", error="interrupted")
            )
            return int(scans.rowcount), int(tasks.rowcount)  # type: ignore[attr-defined]

    async def purge(self) -> tuple[int, int]:
        """Scans older than 24 hours go whole; the journal keeps 30 days (ids and states, no names)."""
        async with self._sessions.begin() as session:
            scans = await session.execute(delete(WinbackScan).where(WinbackScan.created_at < _day()))
            tasks = await session.execute(
                delete(WinbackTask).where(WinbackTask.created_at < datetime.now(UTC) - JOURNAL_KEEP)
            )
            return int(scans.rowcount), int(tasks.rowcount)  # type: ignore[attr-defined]
