"""Live E2E: every adapter runs the conformance scenario against a real CRM account.

Credentials come from the environment (GitHub Actions secrets in CI); a CRM without them is
skipped locally and fails in CI (E2E_REQUIRE_ALL=1). RECORD=1 rewrites the recordings that the
contract tests replay, with secrets and hosts replaced by placeholders.
"""

import os
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest

from alnuzum_crm.testing import Recorder, Scrubber

CASSETTES = Path(__file__).parent.parent / "cassettes"


def need(*names: str) -> dict[str, str]:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        if os.environ.get("E2E_REQUIRE_ALL") == "1":
            pytest.fail(f"missing secrets: {', '.join(missing)}")
        pytest.skip(f"no {', '.join(missing)}")
    return {n: os.environ[n] for n in names}


@pytest.fixture
def run_id() -> str:
    return uuid.uuid4().hex[:8]


class Recording:
    def __init__(self, name: str, describe: Callable[[httpx.Request], str] | None = None) -> None:
        self.name = name
        self.recorder = Recorder(httpx.AsyncHTTPTransport(), describe=describe)

    def save(self, run_id: str, scrub: Scrubber) -> None:
        # names of the test-account users and every e-mail outside example.com stay out of the repo
        for name in filter(None, os.environ.get("E2E_SCRUB_NAMES", "").split(",")):
            scrub.replacements[name.strip()] = "Test User"
        scrub.patterns.append(
            (
                r"[A-Za-z0-9._%+-]+@(?!example\.com)[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[a-z]{2,}",
                "user@example.com",
            )
        )
        if os.environ.get("RECORD") == "1":
            self.recorder.save(CASSETTES / f"{self.name}.json", {"crm": self.name, "run_id": run_id}, scrub)


@pytest.fixture
def recording() -> Callable[..., Recording]:
    return Recording


@pytest.fixture
async def closing() -> AsyncIterator[list[object]]:
    items: list[object] = []
    yield items
    for item in items:
        await item.aclose()  # type: ignore[attr-defined]
