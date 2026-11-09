"""Updates of the Telegram bot. A private chat writes into a demo CRM it picked, or into the one connection a
one-time link bound it to; nothing is written before the person presses «Write» under the plan."""

import logging
import uuid
from decimal import Decimal
from typing import Any

from fastapi import FastAPI

from alnuzum_crm.types import CrmKind
from alnuzum_platform.solutions import Kit
from alnuzum_platform.store import ConnectionNotFound
from alnuzum_platform.web.i18n import texts as platform_texts
from alnuzum_voice.engine import Change, Result
from alnuzum_voice.i18n import demo_deals, texts
from alnuzum_voice.service import Note, Voice, VoiceError
from alnuzum_voice.store import RunNotFound, RunsExceeded, chat_owner
from alnuzum_voice.telegram import Bot, Buttons, TelegramError

log = logging.getLogger("alnuzum_voice.bot")
CRMS = tuple(k.value for k in CrmKind)
MAX_AUDIO = 8 * 1024 * 1024


def crm_name(crm: str, lang: str) -> str:
    return platform_texts(lang).get(f"crm_{crm}", crm)


def _num(value: str | None) -> str:
    if value is None:
        return ""
    d = Decimal(value)
    whole = d == d.to_integral_value()
    return f"{d:,.0f}".replace(",", " ") if whole else f"{d:,.2f}".replace(",", " ")


def change_line(ch: Change, t: dict[str, str]) -> str:
    if ch.kind == "stage":
        return f"• {t['k_stage']} → {ch.stage_name}" + (f" ({t['why_' + ch.why]})" if ch.why else "")
    if ch.kind == "amount":
        return f"• {t['k_amount']} → {_num(ch.value)}"
    if ch.kind == "close_date":
        return f"• {t['k_close_date']} → {ch.value}" + (f" ({t['why_' + ch.why]})" if ch.why else "")
    if ch.kind == "product":
        if ch.state == "ok":
            return f"• {t['k_product']}: {ch.name} — {_num(ch.quantity)} {ch.unit or ''}".rstrip()
        return f"• {t['k_product']} «{ch.said}»: {t.get('why_' + (ch.why or 'unsure'), '')}, {t['b_unsure']}"
    if ch.kind == "task":
        return f"• {t['k_task']}: {t['t_' + str(ch.task_kind)]} — {ch.due}" + (f" {ch.at}" if ch.at else "")
    return f"✖ {t['k_refused']}: {t['r_' + str(ch.what)]}"


def plan_text(result: Result, crm: str, lang: str) -> str:
    t = texts(lang)
    said = " ".join(result.phrases)
    lines = [f"{t['b_said']}: «{said[:600]}{'…' if len(said) > 600 else ''}»", ""]
    deal = result.deal
    if deal:
        lines.append(t["b_plan"].format(deal=deal["title"]) + f" · {crm_name(crm, lang)}")
    elif result.candidates:
        lines.append(t["b_pick_deal"])
    elif result.queries:
        lines.append(t["v_none_found"].format(q=", ".join(result.queries)))
    else:
        lines.append(t["v_no_deal_said"])
    lines += [change_line(c, t) for c in result.changes]
    lines.append(f"• {t['v_note']}")
    return "\n".join(lines)


def plan_buttons(run_id: uuid.UUID, result: Result, lang: str) -> Buttons:
    t = texts(lang)
    if result.deal_id:
        return [[(t["b_confirm"], f"w:{run_id.hex}"), (t["b_cancel"], f"x:{run_id.hex}")]]
    rows: Buttons = [
        [(f"{i + 1}. {c['title']}"[:60], f"d:{run_id.hex}:{i}")] for i, c in enumerate(result.candidates)
    ]
    rows.append([(t["b_cancel"], f"x:{run_id.hex}")])
    return rows


def readback_text(run_readback: dict[str, Any], crm: str, url: str | None, lang: str) -> str:
    t = texts(lang)
    rb = run_readback
    lines = [
        t["b_done"].format(crm=crm_name(crm, lang)),
        t["v_readback"].format(crm=crm_name(crm, lang)),
        f"{rb.get('title')}",
        f"{t['k_stage']}: {rb.get('stage')} · {t['k_amount']}: {_num(rb.get('amount'))} "
        f"{rb.get('currency') or ''}".rstrip(),
    ]
    for line in rb.get("lines", [])[:15]:
        lines.append(f"• {line['name']} × {_num(line['quantity'])}")
    if url:
        lines.append(url)
    return "\n".join(lines)


def demo_buttons(lang: str, crms: list[str]) -> Buttons:
    row = [(crm_name(c, lang), f"c:{c}") for c in crms]
    return [row[i : i + 3] for i in range(0, len(row), 3)]


class Updates:
    def __init__(self, voice: Voice, kit: Kit) -> None:
        self.voice, self.kit = voice, kit

    async def handle(self, app: FastAPI, update: dict[str, Any]) -> None:
        bot = self.voice.bot()
        if bot is None:
            return
        vs = self.voice.store(app)
        update_id = update.get("update_id")
        if not isinstance(update_id, int) or not await vs.first_time(update_id):
            return
        try:
            if isinstance(update.get("callback_query"), dict):
                await self.callback(app, bot, update["callback_query"])
            elif isinstance(update.get("message"), dict):
                await self.message(app, bot, update["message"])
        except TelegramError:
            pass
        except Exception:
            log.exception("a voice update failed")  # its id is the sender's text to a log: not named

    @staticmethod
    def _lang(sender: Any) -> str:
        code = str((sender or {}).get("language_code") or "")
        return "en" if code.startswith("en") else "ru"

    async def message(self, app: FastAPI, bot: Bot, msg: dict[str, Any]) -> None:
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        if not isinstance(chat_id, int):
            return
        lang = self._lang(msg.get("from"))
        t = texts(lang)
        if chat.get("type") != "private":
            await bot.send(chat_id, t["b_group"])
            return
        vs = self.voice.store(app)
        demos = sorted(await self.voice.demo_ids(app))
        text = msg.get("text") if isinstance(msg.get("text"), str) else None
        if text and text.startswith("/start"):
            arg = text.split(maxsplit=1)[1].strip() if " " in text.strip() else ""
            if arg:
                conn = await vs.use_link(arg[:64], chat_id, lang)
                if conn is None:
                    await bot.send(chat_id, t["b_link_bad"])
                    return
                chat_info = await vs.chat(chat_id)
                try:
                    info = await self.voice.connections(app).get(
                        conn, str(chat_info.owner if chat_info else "")
                    )
                    await bot.send(chat_id, t["b_linked"].format(crm=crm_name(info.crm.value, lang)))
                except ConnectionNotFound:
                    await bot.send(chat_id, t["b_own_off"])
                return
            await bot.send(chat_id, t["b_hello"], demo_buttons(lang, demos))
            return
        if text in ("/demo", "/stop"):
            await vs.unbind(chat_id)
            await bot.send(chat_id, t["b_unlinked"] + "\n" + t["b_pick_first"], demo_buttons(lang, demos))
            return
        chat_info = await vs.chat(chat_id)
        demo = True
        if chat_info and chat_info.mode == "own" and chat_info.connection_id and chat_info.owner:
            try:
                info = await self.voice.connections(app).get(chat_info.connection_id, chat_info.owner)
            except ConnectionNotFound:
                await bot.send(chat_id, t["b_own_off"])
                return
            if info.status != "active":
                await bot.send(chat_id, t["b_own_off"])
                return
            demo = False
        elif chat_info and chat_info.demo_crm in demos:
            ids = await self.voice.demo_ids(app)
            info = await self.voice.connections(app).get(ids[str(chat_info.demo_crm)], "demo")
        else:
            await bot.send(chat_id, t["b_pick_first"], demo_buttons(lang, demos))
            return
        media = msg.get("voice") or msg.get("audio")
        document = msg.get("document")
        if (
            media is None
            and isinstance(document, dict)
            and str(document.get("mime_type", "")).startswith("audio/")
        ):
            media = document
        if not (isinstance(media, dict) and media.get("file_id")) and not text:
            await bot.send(chat_id, t["e_empty"])
            return
        if isinstance(media, dict) and int(media.get("file_size") or 0) > MAX_AUDIO:
            await bot.send(chat_id, t["e_too_big"])
            return
        owner = chat_owner(chat_id)
        try:
            run_id = await vs.reserve_run(
                owner=owner,
                ip_key="telegram",
                chat_id=chat_id,
                connection_id=info.id,
                crm=info.crm.value,
                demo=demo,
                limits=self.voice.settings.limits(),
                count_ip=False,
            )
        except RunsExceeded:
            await bot.send(chat_id, t["e_runs"])
            return
        waiting = await bot.send(chat_id, t["b_reading"])
        try:
            note = (
                Note(audio=await bot.download(str(media["file_id"])))
                if isinstance(media, dict)
                else Note(text=text)
            )
        except TelegramError:
            await vs.reject_run(run_id, "e_damaged")
            await bot.send(chat_id, t["e_damaged"])
            return
        try:
            result = await self.voice.read(
                app, self.kit, run_id=run_id, owner=owner, info=info, demo=demo, note=note
            )
        except VoiceError as exc:
            reply = t.get(exc.code, t["e_model_unreadable"])
            if waiting:
                await bot.edit(chat_id, waiting, reply)
            else:
                await bot.send(chat_id, reply)
            return
        body, buttons = plan_text(result, info.crm.value, lang), plan_buttons(run_id, result, lang)
        if waiting:
            await bot.edit(chat_id, waiting, body, buttons)
        else:
            await bot.send(chat_id, body, buttons)

    async def callback(self, app: FastAPI, bot: Bot, cq: dict[str, Any]) -> None:
        data = str(cq.get("data") or "")
        message = cq.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        message_id = message.get("message_id")
        cid = str(cq.get("id") or "")
        if not isinstance(chat_id, int) or not isinstance(message_id, int):
            return
        lang = self._lang(cq.get("from"))
        t = texts(lang)
        vs = self.voice.store(app)
        kind, _, rest = data.partition(":")
        if kind == "c":
            demos = await self.voice.demo_ids(app)
            if rest in demos:
                await vs.set_demo(chat_id, rest, lang)
                await bot.answer(cid)
                await bot.send(
                    chat_id, t["b_crm_set"].format(crm=crm_name(rest, lang), deals=demo_deals(lang, rest))
                )
            else:
                await bot.answer(cid, t["e_demo_off"])
            return
        run_hex, _, extra = rest.partition(":")
        try:
            run = await vs.get_run(uuid.UUID(hex=run_hex), chat_owner(chat_id))
        except (ValueError, RunNotFound):
            await bot.answer(cid, t["b_old"])
            return
        if run.status not in ("ready", "failed"):
            await bot.answer(cid, t["b_old"])
            return
        result = Result.from_json(run.result or {})
        if kind == "x":
            await vs.cancel(run.id, run.owner)
            await bot.answer(cid)
            await bot.edit(chat_id, message_id, plan_text(result, run.crm, lang) + "\n\n" + t["v_cancelled"])
            return
        if kind == "d":
            try:
                chosen = result.candidates[int(extra)]
            except (ValueError, IndexError):
                await bot.answer(cid, t["b_old"])
                return
            result.deal_id, result.deal_by = str(chosen["id"]), "person"
            await vs.save_result(run.id, run.owner, result.to_json())
            await bot.answer(cid)
            await bot.edit(
                chat_id, message_id, plan_text(result, run.crm, lang), plan_buttons(run.id, result, lang)
            )
            return
        if kind != "w":
            await bot.answer(cid)
            return
        conn_owner = "demo"
        if not run.demo:
            # only the chat bound to this very connection writes into it
            chat_info = await vs.chat(chat_id)
            if not chat_info or chat_info.connection_id != run.connection_id or not chat_info.owner:
                await bot.answer(cid, t["b_own_off"])
                return
            conn_owner = chat_info.owner
        await bot.answer(cid)
        notice = await self.voice.confirm(app, self.kit, run, conn_owner=conn_owner, lang=lang)
        if notice == "written":
            done = await vs.get_run(run.id, run.owner)
            await bot.edit(
                chat_id, message_id, readback_text(done.readback or {}, run.crm, done.deal_url, lang)
            )
            return
        text = t.get("n_" + notice, t["n_failed"]).format(crm=crm_name(run.crm, lang))
        buttons = plan_buttons(run.id, result, lang) if notice == "failed" else None
        await bot.edit(chat_id, message_id, plan_text(result, run.crm, lang) + "\n\n" + text, buttons)
