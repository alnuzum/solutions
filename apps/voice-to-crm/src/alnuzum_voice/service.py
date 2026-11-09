"""The work behind both doors, the page and the bot: a note becomes a stored plan, a confirmed plan is
written."""

import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast

import anyio
from fastapi import FastAPI
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from alnuzum_crm.errors import CrmAuthError, CrmError, CrmRateLimited, CrmUnavailable
from alnuzum_orders.catalog import Catalog, CatalogCache, read_catalog
from alnuzum_orders.demo_catalog import PREFIX
from alnuzum_orders.model import Gate, ModelError, Usage
from alnuzum_orders.store import DEMO_OWNER
from alnuzum_platform.solutions import Kit
from alnuzum_platform.store import (
    ConnectionDisabled,
    ConnectionInfo,
    ConnectionNotFound,
    ConnectionStore,
    QuotaExceeded,
)
from alnuzum_voice import audio as audio_mod
from alnuzum_voice.engine import Result, analyze, write
from alnuzum_voice.model import Scribe, SpeechError, SpeechGate, Spent, VoiceReader, stt_cost
from alnuzum_voice.store import CeilingReached, Limits, RunInfo, SpeechCeilingReached, VoiceStore
from alnuzum_voice.telegram import Bot
from alnuzum_voice.text import MAX_TEXT, clean, phrases

log = logging.getLogger("alnuzum_voice")


class VoiceSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    anthropic_api_key: SecretStr = SecretStr("")
    elevenlabs_api_key: SecretStr = SecretStr("")
    voice_model: str = "claude-opus-5-5"
    # the money ceiling of the whole solution per 24 hours, speech and model together (each call booked at its
    # worst cost until it has cost what it cost)
    voice_usd_per_day: Decimal = Decimal("1.0")
    # minutes of speech heard per 24 hours: ElevenLabs counts them against the plan's monthly credits, the
    # same credits its speech synthesis spends (prod 20, stand 5)
    voice_stt_minutes_per_day: int = 20
    voice_runs_per_owner: int = 10  # notes per 24 hours from a session or a Telegram chat
    voice_runs_per_ip: int = 20  # from an address (the page)
    voice_demo_writes_per_owner: int = 5
    voice_demo_writes_per_crm: int = 40
    # Kommo's technical account keeps at most 100 deals: its demo writes are bounded in all, with solution 1
    voice_kommo_total: int = 40
    voice_tg_token: SecretStr = SecretStr("")
    voice_tg_secret: SecretStr = SecretStr("")
    voice_tg_bot: str = ""  # the bot's username, for links

    def limits(self) -> Limits:
        return Limits(
            self.voice_runs_per_owner,
            self.voice_runs_per_ip,
            self.voice_demo_writes_per_owner,
            self.voice_demo_writes_per_crm,
            self.voice_kommo_total,
        )


class VoiceError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class Note:
    """What arrived: audio bytes or a typed text."""

    audio: bytes | None = None
    text: str | None = None


def crm_error(exc: CrmError) -> str:
    if isinstance(exc, CrmAuthError):
        return "auth"
    if isinstance(exc, CrmUnavailable | CrmRateLimited):
        return "unavailable"
    return (exc.code or type(exc).__name__)[:40]


class Voice:
    def __init__(
        self,
        settings: VoiceSettings | None = None,
        *,
        reader: VoiceReader | None = None,
        scribe: Scribe | None = None,
        bot: Bot | None = None,
    ) -> None:
        self.settings = settings or VoiceSettings()
        self._reader, self._scribe, self._bot = reader, scribe, bot
        self.catalogs = CatalogCache()
        self.tasks: set[Any] = set()

    def reader(self) -> VoiceReader:
        if self._reader is None:
            key = self.settings.anthropic_api_key.get_secret_value()
            if not key:
                raise VoiceError("e_model_off")
            self._reader = VoiceReader(key, model=self.settings.voice_model)
        return self._reader

    def scribe(self) -> Scribe:
        if self._scribe is None:
            key = self.settings.elevenlabs_api_key.get_secret_value()
            if not key:
                raise VoiceError("e_stt_off")
            self._scribe = Scribe(key)
        return self._scribe

    def bot(self) -> Bot | None:
        if self._bot is None and (token := self.settings.voice_tg_token.get_secret_value()):
            self._bot = Bot(token)
        return self._bot

    @staticmethod
    def store(app: FastAPI) -> VoiceStore:
        maker = getattr(app.state, "sessionmaker", None)
        if maker is None:
            raise VoiceError("e_unreachable")
        return VoiceStore(maker)

    @staticmethod
    def connections(app: FastAPI) -> ConnectionStore:
        found: ConnectionStore | None = app.state.store
        if found is None:
            raise VoiceError("e_unreachable")
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

    def gate(self, app: FastAPI, owner: str) -> Gate:
        vs, budget = self.store(app), self.settings.voice_usd_per_day

        speech = self.settings.voice_stt_minutes_per_day * 60

        async def gate(kind: str, worst: Decimal, seconds: int = 0):  # type: ignore[no-untyped-def]
            try:
                spend_id = await vs.reserve_spend(
                    owner, kind, budget=budget, worst=worst, seconds=seconds, speech_seconds=speech
                )
            except SpeechCeilingReached:
                raise ModelError("e_stt_ceiling") from None
            except CeilingReached:
                raise ModelError("e_ceiling") from None

            async def done(used: Usage | Spent | None, ok: bool) -> None:
                if used is None:
                    await vs.finish_spend(spend_id, None, 0, ok)
                elif isinstance(used, Spent):
                    await vs.finish_spend(spend_id, used.usd, used.seconds, ok)
                else:
                    await vs.finish_spend(spend_id, used.usd, used.input_tokens + used.output_tokens, ok)

            return done

        return gate

    async def catalog(self, kit: Kit, st: ConnectionStore, info: ConnectionInfo, demo: bool) -> Catalog:
        key = str(info.id)
        if hit := self.catalogs.get(key):
            return hit
        async with kit.connectors.adapter(info, await st.secrets(info.id), st) as crm:
            catalog = await read_catalog(crm, prefix=PREFIX if demo else None)
        self.catalogs.put(key, catalog)
        return catalog

    async def read(
        self,
        app: FastAPI,
        kit: Kit,
        *,
        run_id: uuid.UUID,
        owner: str,
        info: ConnectionInfo,
        demo: bool,
        note: Note,
    ) -> Result:
        """The run is already reserved (counted); here it is read and stored, or rejected with a code."""
        vs = self.store(app)
        gate = self.gate(app, owner)
        try:
            usage: dict[str, Any] = {"calls": 0, "usd": "0", "seconds": 0}
            if note.audio is not None:
                sound = await anyio.to_thread.run_sync(audio_mod.normalize, note.audio)
                heard = await self.scribe().transcribe(sound.wav, sound.seconds, cast(SpeechGate, gate))
                text, language, source = heard.text, heard.language, "voice"
                usage["seconds"] = sound.seconds
                usage["usd"] = str(stt_cost(sound.seconds))
            else:
                text, language, source = clean(note.text or "", MAX_TEXT + 1), None, "text"
                if len(text) > MAX_TEXT:
                    raise VoiceError("e_too_long_text")
            parts = phrases(text)
            if not parts:
                raise VoiceError("e_no_speech" if source == "voice" else "e_empty")
            st = self.connections(app)
            catalog = await self.catalog(kit, st, info, demo)
            async with kit.connectors.adapter(info, await st.secrets(info.id), st) as crm:
                result = await analyze(
                    phrases=parts,
                    language=language,
                    source=source,
                    crm=crm,
                    catalog=catalog,
                    reader=self.reader(),
                    gate=gate,
                    demo=demo,
                    usage=usage,
                )
        except audio_mod.AudioError as exc:
            await vs.reject_run(run_id, exc.code)
            raise VoiceError(exc.code) from None
        except (SpeechError, ModelError, VoiceError) as exc:
            await vs.reject_run(run_id, exc.code)
            raise VoiceError(exc.code) from None
        except CrmError as exc:
            await vs.reject_run(run_id, "crm:" + crm_error(exc))
            raise VoiceError("e_crm") from None
        await vs.fill_run(run_id, result.to_json())
        return result

    async def confirm(self, app: FastAPI, kit: Kit, run: RunInfo, *, conn_owner: str, lang: str) -> str:
        """Write a confirmed plan. Returns the notice: written, quota, full, full_total, busy, failed, auth,
        no_deal."""
        result = Result.from_json(run.result or {})
        if not result.deal_id:
            return "no_deal"
        vs, st, s = self.store(app), self.connections(app), self.settings
        state = await vs.start_writing(run.id, run.owner, demo=run.demo, crm=run.crm, limits=s.limits())
        if state != "ok":
            return state
        owner = DEMO_OWNER if run.demo else conn_owner
        quota = max(kit.quota, 2 * s.voice_demo_writes_per_crm) if run.demo else kit.quota
        try:
            info = await st.get(run.connection_id, owner)
            if info.status != "active":
                raise ConnectionDisabled(str(info.id))
            write_id = await st.start_write(info.id, owner, "voice_update", quota)
        except (QuotaExceeded, ConnectionDisabled, ConnectionNotFound) as exc:
            code = "quota" if isinstance(exc, QuotaExceeded) else "auth"
            await vs.finish_writing(run.id, ok=False, error=code)
            return code
        try:
            async with kit.connectors.adapter(info, await st.secrets(info.id), st) as crm:
                done = await write(crm, result, lang=lang)
                url = None if run.demo else crm.deal_link(kit.connectors.account_of(info), done.deal_id)
        except CrmError as exc:
            code = crm_error(exc)
            await st.finish_write(write_id, ok=False, error=code)
            await vs.finish_writing(run.id, ok=False, error=code)
            if code == "auth" and not run.demo:
                await st.mark(info.id, status="reauth_required", error="auth")
            log.info("voice run %s not written: %s", run.id, code)
            return "auth" if code == "auth" else "failed"
        await st.finish_write(write_id, ok=True, record_id=done.deal_id, record_url=url)
        await vs.finish_writing(run.id, ok=True, readback=done.readback, deal_url=url)
        log.info("voice run %s written to deal %s", run.id, done.deal_id)
        return "written"
