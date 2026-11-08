"""Addresses typed in by a user never take the server elsewhere: a Bitrix24 webhook must be a
cloud Bitrix24 portal, an Odoo address must be https and resolve only to public addresses, on
every connect (DNS rebinding included)."""

import asyncio
import socket
from typing import Any

import pytest

from alnuzum_crm.bitrix24 import Bitrix24, parse_webhook
from alnuzum_crm.http import RetryPolicy
from alnuzum_crm.odoo import Odoo
from alnuzum_crm.types import CompanyIn

GOOD = "https://portal.bitrix24.ru/rest/1/abcdef123456/"


def test_bitrix_webhook_of_a_cloud_portal_passes() -> None:
    assert parse_webhook(GOOD) == ("portal.bitrix24.ru", "https://portal.bitrix24.ru/rest/1/abcdef123456")
    assert parse_webhook("https://my-co.bitrix24.com.br/rest/7/abcdef123456")[0] == "my-co.bitrix24.com.br"


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.com/rest/1/abcdef123456/",
        "https://portal.bitrix24.ru.evil.com/rest/1/abcdef123456/",
        "https://evilbitrix24.ru/rest/1/abcdef123456/",
        "https://bitrix24.ru/rest/1/abcdef123456/",
        "https://127.0.0.1/rest/1/abcdef123456/",
        "https://169.254.169.254/rest/1/abcdef123456/",
        "https://a.b.bitrix24.ru/rest/1/abcdef123456/",
    ],
)
def test_bitrix_webhook_on_a_foreign_host_is_refused(url: str) -> None:
    with pytest.raises(ValueError, match="Bitrix24"):
        parse_webhook(url)
    with pytest.raises(ValueError, match="Bitrix24"):
        Bitrix24(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://portal.bitrix24.ru/rest/1/abcdef123456/",
        "https://portal.bitrix24.ru:8443/rest/1/abcdef123456/",
        "https://user:pw@portal.bitrix24.ru/rest/1/abcdef123456/",
        "https://portal.bitrix24.ru/rest/1/abcdef123456/?x=1",
        "https://portal.bitrix24.ru/oauth/1/abcdef123456/",
        "https://portal.bitrix24.ru/rest/1/../../admin/",
    ],
)
def test_bitrix_webhook_of_another_shape_is_refused(url: str) -> None:
    with pytest.raises(ValueError, match=r"https|query|webhook"):
        parse_webhook(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://odoo.example.com",
        "ftp://odoo.example.com",
        "https://u:p@odoo.example.com",
        "https://odoo.example.com:8069",
        "https://odoo.example.com:22",
    ],
)
def test_odoo_address_must_be_plain_https(url: str) -> None:
    with pytest.raises(ValueError, match=r"https|port|credentials"):
        Odoo(url, "db", "user", "key")


def _resolve_to(monkeypatch: pytest.MonkeyPatch, answers: list[str]) -> list[str]:
    """Every getaddrinfo answers the next address of ``answers`` (the last one repeats)."""
    asked: list[str] = []
    loop = asyncio.get_running_loop()

    async def fake(host: Any, port: Any, *a: Any, **kw: Any) -> list[Any]:
        asked.append(str(host))
        ip = answers[min(len(asked) - 1, len(answers) - 1)]
        fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(fam, socket.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setattr(loop, "getaddrinfo", fake)
    return asked


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.9",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",
        "::1",
        "::ffff:127.0.0.1",
        "fd00::1",
        "0.0.0.0",  # noqa: S104
    ],
)
async def test_odoo_never_connects_to_a_non_public_address(monkeypatch: pytest.MonkeyPatch, ip: str) -> None:
    asked = _resolve_to(monkeypatch, [ip])
    crm = Odoo("https://odoo.example.com", "db", "user", "key", retry=RetryPolicy(max_retries=0), timeout=2)
    async with crm:
        with pytest.raises(ValueError, match="non-public"):
            await crm.find_company(CompanyIn("x"))
    assert asked == ["odoo.example.com"]


async def test_odoo_rebinding_after_the_check_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    from alnuzum_crm.netguard import check_public_url

    _resolve_to(monkeypatch, ["93.184.215.14", "127.0.0.1"])
    assert await check_public_url("https://odoo.example.com/") == "https://odoo.example.com"
    crm = Odoo("https://odoo.example.com", "db", "user", "key", retry=RetryPolicy(max_retries=0), timeout=2)
    async with crm:
        with pytest.raises(ValueError, match="non-public"):
            await crm.find_company(CompanyIn("x"))
