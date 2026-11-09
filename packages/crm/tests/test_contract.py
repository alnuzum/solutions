"""Contract tests: each adapter replays the traffic of its last live E2E run.

The recordings (``cassettes/``) are written by ``RECORD=1 pytest -m e2e``; a change in what an
adapter sends (method, URL, JSON-RPC call) or in how it reads the answers fails here first.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from alnuzum_crm.amocrm import AmoCRM, Kommo
from alnuzum_crm.base import CRM
from alnuzum_crm.bitrix24 import Bitrix24
from alnuzum_crm.odoo import Odoo
from alnuzum_crm.testing import Replayer, describe_jsonrpc, run_conformance
from alnuzum_crm.tokens import StaticToken
from alnuzum_crm.zoho import ZohoCRM

CASSETTES = Path(__file__).parent / "cassettes"
FAST = 1000.0

BUILD: dict[str, Callable[[httpx.AsyncBaseTransport], CRM]] = {
    "amocrm": lambda t: AmoCRM(
        "example.amocrm.ru", StaticToken("TOKEN", crm="amocrm"), rate=FAST, transport=t
    ),
    "kommo": lambda t: Kommo("example.kommo.com", StaticToken("TOKEN", crm="kommo"), rate=FAST, transport=t),
    "bitrix24": lambda t: Bitrix24(
        "https://example.bitrix24.ru/rest/1/WEBHOOKTOKEN/", rate=FAST, transport=t
    ),
    "zoho": lambda t: ZohoCRM(
        "https://www.zohoapis.com", StaticToken("TOKEN", crm="zoho"), rate=FAST, transport=t
    ),
    "odoo": lambda t: Odoo(
        "https://odoo.example.com", "e2e", "admin", "KEY", trusted_url=True, rate=FAST, transport=t
    ),
}


async def _no_sleep(_seconds: float) -> None:
    return None


@pytest.mark.parametrize("name", sorted(BUILD))
async def test_replay_conformance(name: str) -> None:
    describe = describe_jsonrpc if name == "odoo" else None
    replay = Replayer(CASSETTES / f"{name}.json", describe=describe)
    async with BUILD[name](replay) as crm:
        steps = await run_conformance(
            crm, replay.meta["run_id"], sleep=_no_sleep, now=datetime(2026, 11, 9, tzinfo=UTC)
        )
    assert "test records removed" in steps
    assert "deal read back with lines" in steps
    assert "deal moved, amount set, products added" in steps
    assert replay.exhausted, f"{len(replay.calls) - replay.position} recorded calls were not made"


def test_every_adapter_has_a_recording() -> None:
    assert sorted(p.stem for p in CASSETTES.glob("*.json")) == sorted(BUILD)
