"""The model by the subscription token: the system line the channel wants, no key next to the token, the key
for what the token refuses (and for a while after), no credential in a log."""

import json
import logging
from decimal import Decimal
from typing import Any

import httpx2
import pytest

from alnuzum_orders.model import (
    BENCH_SECONDS,
    CHOOSE_SYSTEM,
    OAUTH_BETA,
    OAUTH_SYSTEM,
    Bench,
    ClaudeReader,
    ModelError,
    Question,
    Usage,
)
from alnuzum_orders.web import Orders, OrderSettings

TOKEN = "sk-ant-oat01-" + "t0ken" * 12
KEY = "sk-ant-api03-" + "k3y" * 16
ANSWER = {"answers": [{"line": 1, "choice": 1, "confidence": 0.9}]}
QUESTIONS = [Question(1, "кабель ВВГ 3х2.5", ("ВВГнг 3х2.5, арт. 1, м",))]


def _message(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": [{"type": "text", "text": json.dumps(data)}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 1000, "output_tokens": 100},
    }


def _refusal(status: int, message: str = "Error") -> httpx2.Response:
    kind = "rate_limit_error" if status == 429 else "invalid_request_error"
    return httpx2.Response(status, json={"type": "error", "error": {"type": kind, "message": message}})


class Api:
    """api.anthropic.com as the reader sees it: the token's answer is set per test, the key always answers."""

    def __init__(self, token_answer: httpx2.Response | None = None) -> None:
        self.token_answer = token_answer
        self.seen: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.seen.append(request)
        if request.headers.get("authorization") == f"Bearer {TOKEN}" and self.token_answer is not None:
            return self.token_answer
        return httpx2.Response(200, json=_message(ANSWER))

    def by(self) -> list[str]:
        return ["oauth" if r.headers.get("authorization") == f"Bearer {TOKEN}" else "key" for r in self.seen]

    def body(self, n: int) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(self.seen[n].content)
        return loaded


def _reader(api: Api, *, key: str = KEY, token: str = TOKEN) -> ClaudeReader:
    reader = ClaudeReader(
        key,
        oauth_token=token,
        bench=Bench(),
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(api)),
    )
    reader.retry_pause = 0.0
    reader.clock = lambda: 1000.0
    return reader


class Gate:
    def __init__(self) -> None:
        self.booked: list[Decimal] = []
        self.done: list[tuple[Usage | None, bool]] = []

    async def __call__(self, kind: str, worst: Decimal):  # type: ignore[no-untyped-def]
        self.booked.append(worst)

        async def done(used: Usage | None, ok: bool) -> None:
            self.done.append((used, ok))

        return done


@pytest.fixture(autouse=True)
def _a_key_in_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # what production has: the key in the environment next to the token
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", KEY)


async def test_the_token_asks_first_and_the_channel_line_is_the_first_system_block() -> None:
    api = Api()
    answers, usage = await _reader(api).choose(QUESTIONS, Gate())
    assert answers[0].choice == 0 and usage.calls == 1
    assert api.by() == ["oauth"]
    system = api.body(0)["system"]
    assert system == [{"type": "text", "text": OAUTH_SYSTEM}, {"type": "text", "text": CHOOSE_SYSTEM}]
    assert OAUTH_BETA in api.seen[0].headers["anthropic-beta"]


async def test_the_token_request_carries_no_key() -> None:
    api = Api()
    await _reader(api).choose(QUESTIONS, Gate())
    headers = api.seen[0].headers
    assert "x-api-key" not in headers
    assert headers["authorization"] == f"Bearer {TOKEN}"
    assert not any(KEY in value for value in headers.values())


@pytest.mark.parametrize(
    "refusal",
    [
        _refusal(401),
        _refusal(403),
        _refusal(429),
        _refusal(529),
        _refusal(400, "You're out of extra usage. Add more at claude.ai/settings/usage"),
    ],
    ids=["401", "403", "429", "529", "out-of-usage"],
)
async def test_a_refusal_of_the_token_goes_by_the_key_and_the_key_keeps_it_15_minutes(
    refusal: httpx2.Response, caplog: pytest.LogCaptureFixture
) -> None:
    api = Api(refusal)
    reader = _reader(api)
    gate = Gate()
    caplog.set_level(logging.INFO, logger="alnuzum_orders.spend")
    # the journal line reaches the container's log with no logging configured by the app
    assert logging.getLogger("alnuzum_orders.spend").handlers
    answers, usage = await reader.choose(QUESTIONS, gate)
    assert answers[0].choice == 0 and usage.input_tokens == 1000
    assert api.by() == ["oauth", "key"]
    # the key's request is the request of before: its own system prompt, no channel line
    assert api.body(1)["system"] == CHOOSE_SYSTEM
    assert "x-api-key" not in api.seen[1].headers and TOKEN not in str(api.seen[1].headers)
    # one booking, closed with what the key's answer cost
    assert len(gate.booked) == 1 and gate.done == [(usage, True)]
    assert "model choose by key" in caplog.text

    reader.clock = lambda: 1000.0 + BENCH_SECONDS - 1
    await reader.choose(QUESTIONS, Gate())
    assert api.by() == ["oauth", "key", "key"]

    api.token_answer = None  # the plan came back
    reader.clock = lambda: 1000.0 + BENCH_SECONDS
    await reader.choose(QUESTIONS, Gate())
    assert api.by() == ["oauth", "key", "key", "oauth"]


async def test_a_refused_request_is_not_a_refused_token() -> None:
    api = Api(_refusal(400, "max_tokens: too large"))
    with pytest.raises(ModelError) as raised:
        await _reader(api).choose(QUESTIONS, Gate())
    assert raised.value.code == "e_model_busy"
    assert api.by() == ["oauth"]


async def test_a_refusal_with_no_key_behind_it_is_what_it_was() -> None:
    api = Api(_refusal(429))
    gate = Gate()
    with pytest.raises(ModelError) as raised:
        await _reader(api, key="").choose(QUESTIONS, gate)
    assert raised.value.code == "e_model_busy"
    assert api.by() == ["oauth", "oauth"]  # one more try under a booking of its own, as before
    assert len(gate.booked) == 2 and [ok for _, ok in gate.done] == [False, False]


async def test_by_the_key_alone_the_request_is_the_one_of_before() -> None:
    api = Api()
    await _reader(api, token="").choose(QUESTIONS, Gate())
    assert api.by() == ["key"] and api.body(0)["system"] == CHOOSE_SYSTEM
    assert "anthropic-beta" not in api.seen[0].headers


async def test_without_a_token_and_a_key_there_is_no_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    with pytest.raises(ValueError, match="ANTHROPIC_OAUTH_TOKEN or ANTHROPIC_API_KEY"):
        ClaudeReader()
    with pytest.raises(ValueError, match="ANTHROPIC_OAUTH_TOKEN"):
        ClaudeReader.from_env()
    assert Orders(OrderSettings()).reader() is None
    monkeypatch.setenv("ANTHROPIC_OAUTH_TOKEN", TOKEN)
    assert repr(ClaudeReader.from_env()) == "ClaudeReader(model='claude-opus-5-5')"
    reader = Orders(OrderSettings(anthropic_oauth_token=TOKEN)).reader()
    assert isinstance(reader, ClaudeReader) and reader._client is None and reader._oauth is not None


async def test_neither_the_token_nor_the_key_reaches_a_log(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    api = Api(_refusal(429))
    reader = _reader(api)
    assert logging.getLogger("httpx2").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    await reader.choose(QUESTIONS, Gate())
    api.token_answer = _refusal(401)
    reader.bench = Bench()
    await reader.choose(QUESTIONS, Gate())
    assert caplog.records
    assert TOKEN not in caplog.text and KEY not in caplog.text
    assert TOKEN not in repr(reader) and KEY not in repr(reader)
