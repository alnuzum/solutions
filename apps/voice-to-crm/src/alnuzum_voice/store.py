"""What the solution keeps: a run of a note for a day (never the audio), the money of every paid call, the
one-time links and the chats bound by them, the Telegram updates already taken.

Quotas are counted from these tables, each taken before the work it pays for, in one transaction under an
advisory lock, so parallel requests cannot both take the last one, and an attempt that fails afterwards
stays counted. A voice write creates no deal, so it leaves the deals solution 1 may still create in Kommo's
technical account alone; its demo writes into Kommo have a ceiling of their own a day.
"""

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from alnuzum_platform.db import Base

KEEP = timedelta(days=1)
LINK_LIFE = timedelta(minutes=10)
LOCK_SPEND = 0x564F4943  # the money ceiling of the solution ("VOIC")
LOCK_RUNS, LOCK_DEMO = 0x5652, 0x5644  # runs of an owner or an address; demo writes of an owner


class VoiceRun(Base):
    __tablename__ = "voice_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    owner: Mapped[str] = mapped_column(String(128))
    ip_key: Mapped[str] = mapped_column(String(64))
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    crm: Mapped[str] = mapped_column(String(16))
    demo: Mapped[bool] = mapped_column(Boolean)
    status: Mapped[str] = mapped_column(String(16))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    readback: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    deal_url: Mapped[str | None] = mapped_column(String(500))
    error: Mapped[str | None] = mapped_column(String(40))
    written_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # the steps of the write already done, each named; once one is done the plan no longer changes
    steps: Mapped[list[str] | None] = mapped_column(JSONB)


class VoiceSpend(Base):
    __tablename__ = "voice_spend"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    owner: Mapped[str] = mapped_column(String(128))
    ip_key: Mapped[str | None] = mapped_column(String(64))  # the page's address; none from the bot
    kind: Mapped[str] = mapped_column(String(16))
    usd: Mapped[Decimal] = mapped_column(Numeric(10, 5))
    units: Mapped[int] = mapped_column(Integer, default=0)
    ok: Mapped[bool | None] = mapped_column(Boolean)


class VoiceLink(Base):
    __tablename__ = "voice_links"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner: Mapped[str] = mapped_column(String(128))
    connection_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class VoiceChat(Base):
    __tablename__ = "voice_chats"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    demo_crm: Mapped[str | None] = mapped_column(String(16))
    owner: Mapped[str | None] = mapped_column(String(128))
    connection_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    mode: Mapped[str] = mapped_column(String(8), default="demo")
    lang: Mapped[str] = mapped_column(String(2), default="ru")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class VoiceUpdate(Base):
    __tablename__ = "voice_updates"

    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CeilingReached(RuntimeError):
    """The solution's dollars for the day are spent."""


class SpeechCeilingReached(CeilingReached):
    """The day's minutes of speech recognition are heard."""


class OwnSpeechCeilingReached(SpeechCeilingReached):
    """The day's minutes of speech of one chat, one session or one address are heard."""


class RunsExceeded(RuntimeError):
    pass


class RunNotFound(LookupError):
    pass


@dataclass(frozen=True)
class Limits:
    runs_per_owner: int
    runs_per_ip: int
    demo_per_owner: int
    demo_per_crm: int
    kommo_per_day: int  # Kommo's demo takes fewer: its technical account is small


@dataclass(frozen=True)
class RunInfo:
    id: uuid.UUID
    owner: str
    chat_id: int | None
    connection_id: uuid.UUID
    crm: str
    demo: bool
    status: str
    result: dict[str, Any] | None
    readback: dict[str, Any] | None
    deal_url: str | None
    error: str | None
    steps: list[str]

    @classmethod
    def of(cls, r: VoiceRun) -> "RunInfo":
        return cls(
            r.id,
            r.owner,
            r.chat_id,
            r.connection_id,
            r.crm,
            r.demo,
            r.status,
            r.result,
            r.readback,
            r.deal_url,
            r.error,
            list(r.steps or []),
        )


@dataclass(frozen=True)
class ChatInfo:
    chat_id: int
    demo_crm: str | None
    owner: str | None
    connection_id: uuid.UUID | None
    mode: str
    lang: str


def chat_owner(chat_id: int) -> str:
    """The quota owner of a Telegram chat: a hash, so the journal does not keep chat ids in the clear."""
    return "tg:" + hashlib.sha256(f"voice-chat:{chat_id}".encode()).hexdigest()[:40]


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def _lock(session: AsyncSession, cls: int, key: str) -> None:
    await session.execute(text("SELECT pg_advisory_xact_lock(:c, hashtext(:k))"), {"c": cls, "k": key})


def _day() -> datetime:
    return datetime.now(UTC) - timedelta(days=1)


class VoiceStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    # --- runs ---

    async def reserve_run(
        self,
        *,
        owner: str,
        ip_key: str,
        chat_id: int | None,
        connection_id: uuid.UUID,
        crm: str,
        demo: bool,
        limits: Limits,
        count_ip: bool,
    ) -> uuid.UUID:
        async with self._sessions.begin() as session:
            await _lock(session, LOCK_RUNS, owner)
            if count_ip:
                await _lock(session, LOCK_RUNS, "ip:" + ip_key)
            since = _day()
            mine = await session.scalar(
                select(func.count())
                .select_from(VoiceRun)
                .where(VoiceRun.owner == owner, VoiceRun.created_at > since)
            )
            if (mine or 0) >= limits.runs_per_owner:
                raise RunsExceeded("owner")
            if count_ip:
                theirs = await session.scalar(
                    select(func.count())
                    .select_from(VoiceRun)
                    .where(VoiceRun.ip_key == ip_key, VoiceRun.created_at > since)
                )
                if (theirs or 0) >= limits.runs_per_ip:
                    raise RunsExceeded("ip")
            run = VoiceRun(
                id=uuid.uuid4(),
                owner=owner,
                ip_key=ip_key[:64],
                chat_id=chat_id,
                connection_id=connection_id,
                crm=crm,
                demo=demo,
                status="reading",
            )
            session.add(run)
            return run.id

    async def fill_run(self, run_id: uuid.UUID, result: dict[str, Any]) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                update(VoiceRun).where(VoiceRun.id == run_id).values(status="ready", result=result)
            )

    async def reject_run(self, run_id: uuid.UUID, error: str) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                update(VoiceRun).where(VoiceRun.id == run_id).values(status="rejected", error=error[:40])
            )

    async def get_run(self, run_id: uuid.UUID, owner: str) -> RunInfo:
        async with self._sessions() as session:
            row = await session.scalar(select(VoiceRun).where(VoiceRun.id == run_id, VoiceRun.owner == owner))
        if row is None:
            raise RunNotFound(str(run_id))
        return RunInfo.of(row)

    async def save_result(self, run_id: uuid.UUID, owner: str, result: dict[str, Any]) -> bool:
        """A person's choice, while nothing of the plan is written: a retry goes on with the plan it began."""
        async with self._sessions.begin() as session:
            done = await session.execute(
                update(VoiceRun)
                .where(
                    VoiceRun.id == run_id,
                    VoiceRun.owner == owner,
                    VoiceRun.status.in_(("ready", "failed")),
                    VoiceRun.steps.is_(None),
                )
                .values(result=result)
            )
            return bool(done.rowcount)  # type: ignore[attr-defined]

    async def cancel(self, run_id: uuid.UUID, owner: str) -> bool:
        async with self._sessions.begin() as session:
            done = await session.execute(
                update(VoiceRun)
                .where(
                    VoiceRun.id == run_id, VoiceRun.owner == owner, VoiceRun.status.in_(("ready", "failed"))
                )
                .values(status="cancelled")
            )
            return bool(done.rowcount)  # type: ignore[attr-defined]

    async def start_writing(
        self, run_id: uuid.UUID, owner: str, *, demo: bool, crm: str, limits: Limits
    ) -> str:
        """The only way to "writing": from "ready" or "failed", by the owner, after a confirmation. Returns
        "ok", "busy" (written, being written or cancelled), "quota" (the owner's demo writes of the day) or
        "full" (this demo CRM's of the day)."""
        async with self._sessions.begin() as session:
            await _lock(session, LOCK_DEMO, owner)
            if demo:
                await _lock(session, LOCK_DEMO, "crm:" + crm)
                since = _day()
                writing = or_(VoiceRun.written_at > since, VoiceRun.status == "writing")
                mine = await session.scalar(
                    select(func.count())
                    .select_from(VoiceRun)
                    .where(VoiceRun.owner == owner, VoiceRun.demo, writing)
                )
                if (mine or 0) >= limits.demo_per_owner:
                    return "quota"
                into = await session.scalar(
                    select(func.count())
                    .select_from(VoiceRun)
                    .where(VoiceRun.crm == crm, VoiceRun.demo, writing)
                )
                ceiling = (
                    min(limits.demo_per_crm, limits.kommo_per_day) if crm == "kommo" else limits.demo_per_crm
                )
                if (into or 0) >= ceiling:
                    return "full"
            done = await session.execute(
                update(VoiceRun)
                .where(
                    VoiceRun.id == run_id, VoiceRun.owner == owner, VoiceRun.status.in_(("ready", "failed"))
                )
                .values(status="writing")
            )
            return "ok" if done.rowcount else "busy"  # type: ignore[attr-defined]

    async def step_done(self, run_id: uuid.UUID, step: str) -> None:
        """Kept before the next step begins: a retry skips it."""
        async with self._sessions.begin() as session:
            await session.execute(
                update(VoiceRun)
                .where(VoiceRun.id == run_id, VoiceRun.status == "writing")
                .values(
                    steps=func.coalesce(VoiceRun.steps, text("'[]'::jsonb")).op("||")(
                        func.jsonb_build_array(cast(step, String))
                    )
                )
            )

    async def steps_done(self, run_id: uuid.UUID) -> list[str]:
        async with self._sessions() as session:
            found = await session.scalar(select(VoiceRun.steps).where(VoiceRun.id == run_id))
        return list(found or [])

    async def finish_writing(
        self,
        run_id: uuid.UUID,
        *,
        ok: bool,
        readback: dict[str, Any] | None = None,
        deal_url: str | None = None,
        error: str | None = None,
    ) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                update(VoiceRun)
                .where(VoiceRun.id == run_id)
                .values(
                    status="written" if ok else "failed",
                    readback=readback,
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
                    .select_from(VoiceRun)
                    .where(VoiceRun.crm == crm, VoiceRun.demo, VoiceRun.written_at > _day())
                )
                or 0
            )

    # --- money ---

    async def reserve_spend(
        self,
        owner: str,
        kind: str,
        *,
        budget: Decimal,
        worst: Decimal,
        seconds: int = 0,
        speech_seconds: int | None = None,
        own_seconds: int | None = None,
        ip_key: str | None = None,
    ) -> uuid.UUID:
        """Booked at its worst cost before the call, under the global lock: the day's dollars are the booked
        or the actual cost of every call of speech and of the model together. Speech (``kind`` "stt") also
        books its ``seconds`` against the day's ``own_seconds`` of the owner (a chat or a session) and of
        the address, then against the day's ``speech_seconds`` of all: a speech service may count them
        against credits it shares with something else, and one person must not hear them all."""
        async with self._sessions.begin() as session:
            await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": LOCK_SPEND})
            since = _day()
            stt = (VoiceSpend.created_at > since, VoiceSpend.kind == "stt")
            heard = select(func.coalesce(func.sum(VoiceSpend.units), 0))
            if kind == "stt" and own_seconds is not None:
                whose = [VoiceSpend.owner == owner] + ([VoiceSpend.ip_key == ip_key] if ip_key else [])
                for who in whose:
                    if int(await session.scalar(heard.where(*stt, who)) or 0) + seconds > own_seconds:
                        raise OwnSpeechCeilingReached(f"{own_seconds} s of speech per day of one person")
            spent = await session.scalar(
                select(func.coalesce(func.sum(VoiceSpend.usd), 0)).where(VoiceSpend.created_at > since)
            )
            if Decimal(spent or 0) + worst > budget:
                raise CeilingReached(f"${budget} per day")
            total = int(await session.scalar(heard.where(*stt)) or 0) if kind == "stt" else 0
            if speech_seconds is not None and kind == "stt" and total + seconds > speech_seconds:
                raise SpeechCeilingReached(f"{speech_seconds} s of speech per day")
            row = VoiceSpend(
                id=uuid.uuid4(),
                owner=owner,
                ip_key=ip_key[:64] if ip_key else None,
                kind=kind,
                usd=worst,
                units=seconds,
            )
            session.add(row)
            return row.id

    async def finish_spend(self, spend_id: uuid.UUID, usd: Decimal | None, units: int, ok: bool) -> None:
        """``usd`` None: no answer came, so the booking stays as the cost."""
        values: dict[str, Any] = {"ok": ok}
        if usd is not None:
            values |= {"usd": usd, "units": units}
        async with self._sessions.begin() as session:
            await session.execute(update(VoiceSpend).where(VoiceSpend.id == spend_id).values(**values))

    async def spent_today(self) -> Decimal:
        async with self._sessions() as session:
            return Decimal(
                await session.scalar(
                    select(func.coalesce(func.sum(VoiceSpend.usd), 0)).where(VoiceSpend.created_at > _day())
                )
                or 0
            )

    # --- one-time links and bound chats ---

    async def new_link(self, owner: str, connection_id: uuid.UUID) -> str:
        token = secrets.token_urlsafe(24)  # 32 characters of [A-Za-z0-9_-]: what a /start parameter allows
        async with self._sessions.begin() as session:
            await session.execute(
                delete(VoiceLink).where(VoiceLink.owner == owner, VoiceLink.connection_id == connection_id)
            )
            session.add(
                VoiceLink(
                    token_hash=token_hash(token),
                    owner=owner,
                    connection_id=connection_id,
                    expires_at=datetime.now(UTC) + LINK_LIFE,
                )
            )
        return token

    async def use_link(self, token: str, chat_id: int, lang: str) -> uuid.UUID | None:
        """The link works once and while it is fresh: it is spent by the same statement that checks it."""
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            row = (
                await session.execute(
                    update(VoiceLink)
                    .where(
                        VoiceLink.token_hash == token_hash(token),
                        VoiceLink.used_at.is_(None),
                        VoiceLink.expires_at > now,
                    )
                    .values(used_at=now)
                    .returning(VoiceLink.owner, VoiceLink.connection_id)
                )
            ).first()
            if row is None:
                return None
            owner, conn = row
            await session.execute(
                insert(VoiceChat)
                .values(chat_id=chat_id, owner=owner, connection_id=conn, mode="own", lang=lang)
                .on_conflict_do_update(
                    index_elements=[VoiceChat.chat_id],
                    set_={"owner": owner, "connection_id": conn, "mode": "own", "updated_at": now},
                )
            )
            return uuid.UUID(str(conn))

    async def chat(self, chat_id: int) -> ChatInfo | None:
        async with self._sessions() as session:
            row = await session.get(VoiceChat, chat_id)
        if row is None:
            return None
        return ChatInfo(row.chat_id, row.demo_crm, row.owner, row.connection_id, row.mode, row.lang)

    async def set_demo(self, chat_id: int, crm: str, lang: str) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                insert(VoiceChat)
                .values(chat_id=chat_id, demo_crm=crm, mode="demo", lang=lang)
                .on_conflict_do_update(
                    index_elements=[VoiceChat.chat_id],
                    set_={"demo_crm": crm, "mode": "demo", "updated_at": datetime.now(UTC)},
                )
            )

    async def unbind(self, chat_id: int) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                update(VoiceChat)
                .where(VoiceChat.chat_id == chat_id)
                .values(owner=None, connection_id=None, mode="demo")
            )

    async def bound_chats(self, owner: str) -> dict[uuid.UUID, int]:
        async with self._sessions() as session:
            rows = await session.execute(
                select(VoiceChat.connection_id, func.count())
                .where(VoiceChat.owner == owner, VoiceChat.connection_id.is_not(None))
                .group_by(VoiceChat.connection_id)
            )
            return {uuid.UUID(str(c)): int(n) for c, n in rows.all()}

    async def unbind_connection(self, owner: str, connection_id: uuid.UUID) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                update(VoiceChat)
                .where(VoiceChat.owner == owner, VoiceChat.connection_id == connection_id)
                .values(owner=None, connection_id=None, mode="demo")
            )

    # --- updates ---

    async def first_time(self, update_id: int) -> bool:
        """True once per Telegram update: a retried delivery is dropped."""
        try:
            async with self._sessions.begin() as session:
                session.add(VoiceUpdate(update_id=update_id))
            return True
        except IntegrityError:
            return False

    async def purge(self) -> tuple[int, int]:
        cut = datetime.now(UTC) - KEEP
        async with self._sessions.begin() as session:
            runs = await session.execute(delete(VoiceRun).where(VoiceRun.created_at < cut))
            await session.execute(delete(VoiceLink).where(VoiceLink.expires_at < cut))
            await session.execute(delete(VoiceUpdate).where(VoiceUpdate.created_at < cut))
            spend = await session.execute(
                delete(VoiceSpend).where(VoiceSpend.created_at < cut - timedelta(days=30))
            )
        return int(runs.rowcount), int(spend.rowcount)  # type: ignore[attr-defined]
