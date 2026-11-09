"""The Bot API client. The bot's token sits in every address (``api.telegram.org/bot<token>/…``), so no
address is ever logged: httpx's own request log is held at WARNING, and our messages name the method and
the class of an error only."""

import contextlib
import logging
from typing import Any

import httpx

log = logging.getLogger("alnuzum_voice.telegram")
API = "https://api.telegram.org"
MAX_FILE = 8 * 1024 * 1024
MAX_TEXT = 4000


def quiet_httpx() -> None:
    # httpx logs every request URL at INFO, and these URLs hold the token
    logging.getLogger("httpx").setLevel(max(logging.getLogger("httpx").getEffectiveLevel(), logging.WARNING))


class TelegramError(RuntimeError):
    pass


Buttons = list[list[tuple[str, str]]]  # rows of (label, callback data)


class Bot:
    def __init__(
        self, token: str, *, timeout: float = 20.0, transport: httpx.AsyncBaseTransport | None = None
    ):
        quiet_httpx()
        self._token = token
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport, follow_redirects=False)

    def __repr__(self) -> str:  # the token never reaches a log
        return "Bot()"

    async def aclose(self) -> None:
        await self._client.aclose()

    async def call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        try:
            resp = await self._client.post(f"{API}/bot{self._token}/{method}", json=payload or {})
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("telegram %s failed: %s", method, type(exc).__name__)
            raise TelegramError(method) from None
        if not isinstance(data, dict) or not data.get("ok"):
            code = data.get("error_code") if isinstance(data, dict) else resp.status_code
            log.warning("telegram %s refused: %s", method, code)
            raise TelegramError(method)
        return data.get("result")

    async def send(self, chat_id: int, text: str, buttons: Buttons | None = None) -> int | None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text[:MAX_TEXT],
            "link_preview_options": {"is_disabled": True},
        }
        if buttons:
            payload["reply_markup"] = _markup(buttons)
        try:
            result = await self.call("sendMessage", payload)
        except TelegramError:
            return None
        return int(result["message_id"]) if isinstance(result, dict) and "message_id" in result else None

    async def edit(self, chat_id: int, message_id: int, text: str, buttons: Buttons | None = None) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text[:MAX_TEXT],
            "link_preview_options": {"is_disabled": True},
        }
        payload["reply_markup"] = _markup(buttons or [])
        try:
            await self.call("editMessageText", payload)
        except TelegramError:
            await self.send(chat_id, text, buttons)

    async def answer(self, callback_id: str, text: str = "") -> None:
        with contextlib.suppress(TelegramError):
            await self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:180]})

    async def download(self, file_id: str) -> bytes:
        info = await self.call("getFile", {"file_id": file_id})
        path = str((info or {}).get("file_path") or "")
        size = int((info or {}).get("file_size") or 0)
        if not path or size > MAX_FILE or ".." in path or path.startswith("/"):
            raise TelegramError("getFile")
        try:
            async with self._client.stream("GET", f"{API}/file/bot{self._token}/{path}") as resp:
                if resp.status_code != 200:
                    raise TelegramError("file")
                out = bytearray()
                async for chunk in resp.aiter_bytes():
                    out += chunk
                    if len(out) > MAX_FILE:
                        raise TelegramError("file")
                return bytes(out)
        except httpx.HTTPError as exc:
            log.warning("telegram file failed: %s", type(exc).__name__)
            raise TelegramError("file") from None

    async def set_webhook(self, url: str, secret: str) -> None:
        await self.call(
            "setWebhook",
            {
                "url": url,
                "secret_token": secret,
                "allowed_updates": ["message", "callback_query"],
                "drop_pending_updates": False,
                "max_connections": 10,
            },
        )


def _markup(buttons: Buttons) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": label[:60], "callback_data": data[:64]} for label, data in row] for row in buttons
        ]
    }
