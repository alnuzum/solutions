"""What the solution keeps: a run for a day (never the file), price rules, the journal of model calls.

Quotas are counted from these tables: runs per session and per address, model calls per day for the
whole solution. A model call is reserved before it is made (under an advisory lock), so parallel
requests cannot both take the last one.
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
    delete,
    func,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from alnuzum_platform.db import Base

KEEP = timedelta(days=1)
LOCK_KEY = 0x4F524452  # advisory lock of the model-call ceiling ("ORDR")


class OrderRun(Base):
    __tablename__ = "orders_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner: Mapped[str] = mapped_column(String(128), index=True)
    ip_key: Mapped[str] = mapped_column(String(64), index=True)
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
    """The solution's model calls for the day are spent."""


class RunNotFound(LookupError):
    pass


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

    async def create_run(
        self,
        owner: str,
        ip_key: str,
        connection_id: uuid.UUID,
        crm: str,
        lang: str,
        result: dict[str, Any],
        *,
        demo: bool,
    ) -> uuid.UUID:
        row = OrderRun(
            id=uuid.uuid4(),
            owner=owner,
            ip_key=ip_key,
            connection_id=connection_id,
            demo=demo,
            crm=crm,
            lang=lang,
            result=result,
            status="ready",
            expires_at=datetime.now(UTC) + KEEP,
        )
        async with self._sessions.begin() as session:
            session.add(row)
        return row.id

    async def get_run(self, run_id: uuid.UUID, owner: str) -> RunInfo:
        async with self._sessions() as session:
            row = await session.scalar(
                select(OrderRun).where(
                    OrderRun.id == run_id, OrderRun.owner == owner, OrderRun.expires_at > datetime.now(UTC)
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

    async def start_writing(self, run_id: uuid.UUID, owner: str) -> bool:
        """Only one write per run: False when it is being written or was written already."""
        async with self._sessions.begin() as session:
            done = await session.execute(
                update(OrderRun)
                .where(
                    OrderRun.id == run_id, OrderRun.owner == owner, OrderRun.status.in_(("ready", "failed"))
                )
                .values(status="writing")
            )
            return bool(done.rowcount)  # type: ignore[attr-defined]

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
        async with self._sessions.begin() as session:
            await session.execute(
                update(OrderRun)
                .where(OrderRun.id == run_id)
                .values(
                    status="written" if ok else "failed",
                    deal_id=deal_id,
                    deal_url=deal_url,
                    readback=readback,
                    error=error,
                    written_at=datetime.now(UTC) if ok else None,
                )
            )

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
        since = datetime.now(UTC) - timedelta(days=1)
        async with self._sessions() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(OrderRun)
                    .where(OrderRun.owner == owner, OrderRun.written_at > since)
                )
                or 0
            )

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

    async def reserve_call(self, owner: str, ip_key: str, kind: str, ceiling: int) -> uuid.UUID:
        async with self._sessions.begin() as session:
            await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": LOCK_KEY})
            used = await session.scalar(
                select(func.count())
                .select_from(ModelCall)
                .where(ModelCall.created_at > datetime.now(UTC) - timedelta(days=1))
            )
            if (used or 0) >= ceiling:
                raise CeilingReached(f"{ceiling} model calls per day")
            call = ModelCall(id=uuid.uuid4(), owner=owner, ip_key=ip_key, kind=kind)
            session.add(call)
            return call.id

    async def finish_call(
        self, call_id: uuid.UUID, input_tokens: int, output_tokens: int, usd: Decimal, ok: bool
    ) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                update(ModelCall)
                .where(ModelCall.id == call_id)
                .values(input_tokens=input_tokens, output_tokens=output_tokens, usd=usd, ok=ok)
            )

    async def calls_today(self) -> tuple[int, Decimal]:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(func.count(), func.coalesce(func.sum(ModelCall.usd), 0)).where(
                        ModelCall.created_at > datetime.now(UTC) - timedelta(days=1)
                    )
                )
            ).one()
            return int(row[0]), Decimal(row[1])
