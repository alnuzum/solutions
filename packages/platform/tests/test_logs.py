"""The container's log: INFO of the platform and the solutions reaches stdout with the time and the logger's name;
httpx and httpcore stay at WARNING (a Bitrix24 webhook URL holds its token)."""

import io
import logging
import re
from collections.abc import Iterator

import pytest
from fastapi import FastAPI

from alnuzum_platform import api

TOKEN = "k3yOfTheWebhook0123"
HOOK = f"https://example.bitrix24.ru/rest/1/{TOKEN}/crm.lead.add.json"
LINE = re.compile(
    r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} INFO alnuzum_winback\.web: win-back sweep: 2 scans$", re.M
)


@pytest.fixture
def clean_logging() -> Iterator[None]:
    """The logging of the test process as it was: the root, httpx, httpcore and every handler of ours."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    http = {name: logging.getLogger(name).level for name in ("httpx", "httpcore")}
    ours = {
        name: list(logger.handlers)
        for name, logger in logging.root.manager.loggerDict.items()
        if name.startswith("alnuzum_") and isinstance(logger, logging.Logger)
    }
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    for name, was in http.items():
        logging.getLogger(name).setLevel(was)
    for name, kept in ours.items():
        logging.getLogger(name).handlers[:] = kept


@pytest.mark.usefixtures("clean_logging")
def test_lines_of_ours_from_info_with_time_and_name_and_httpx_stays_at_warning() -> None:
    out = io.StringIO()
    api.configure_logs(out)
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore.connection").getEffectiveLevel() >= logging.WARNING
    logging.getLogger("alnuzum_winback.web").info("win-back sweep: %d scans", 2)
    logging.getLogger("httpx").info("HTTP Request: POST %s", HOOK)
    logging.getLogger("httpcore.http11").info("send_request_headers.started %s", HOOK)
    logging.getLogger("some.library").info("chatter")
    logging.getLogger("some.library").warning("a library's trouble")
    # even where a level of httpx was reset (a test, a library), its INFO does not pass the handler
    logging.getLogger("httpx").setLevel(logging.NOTSET)
    logging.getLogger("httpx").info("HTTP Request: POST %s", HOOK)
    text = out.getvalue()
    assert LINE.search(text), text
    assert "WARNING some.library: a library's trouble" in text
    assert TOKEN not in text and "/rest/" not in text and "chatter" not in text


@pytest.mark.usefixtures("clean_logging")
def test_serve_app_prints_a_line_of_a_logger_with_its_own_handler_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    own = logging.getLogger("alnuzum_orders.spend")
    own.setLevel(logging.INFO)
    own.addHandler(logging.StreamHandler())  # as the spend journal prints where nothing configures logging
    monkeypatch.setattr(api, "create_app", FastAPI)
    assert isinstance(api.serve_app(), FastAPI)
    assert not own.handlers
    own.info("spend: oauth 1000 in")
    printed = capsys.readouterr()
    assert printed.out.count("spend: oauth 1000 in") == 1 and "INFO alnuzum_orders.spend:" in printed.out
    assert "spend: oauth 1000 in" not in printed.err
