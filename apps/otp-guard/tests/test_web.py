"""The stand's pages through HTTP: a login end to end, every refusal on the page, who may do what, and the lab
mode, which must not exist outside the lab."""

import hashlib
import json
import re
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from alnuzum_otp.pow import solve
from alnuzum_otp.web import CSP, OtpSettings, load_results, make_solution, network
from alnuzum_platform.api import create_app
from alnuzum_platform.config import Settings

ORIGIN = "https://apps.test"
SECRET = "session-secret-for-tests-0123456789abcdef"
T = 1_795_302_000


def app_of(
    env: str = "dev", *, lab: bool = False, layers: str = "geo,limits,human,conversion,budget", **kw: object
) -> FastAPI:
    settings = Settings(session_secret=SECRET, public_url=ORIGIN, app_env=env, sweep_minutes=0)
    otp = OtpSettings(layers=layers, pow_bits=8, **kw)  # type: ignore[arg-type]
    return create_app(settings, solutions=[make_solution(otp, lab=lab)])


class Visitor:
    def __init__(self, app: FastAPI, ip: str = "203.0.113.7") -> None:
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=(ip, 5000)), base_url=ORIGIN, follow_redirects=False
        )
        self.csrf = ""
        self.challenge = ""

    async def page(self, path: str = "/ru/otp", **headers: str) -> httpx.Response:
        resp = await self.http.get(path, headers=headers)
        assert resp.status_code == 200, resp.text[:300]
        found = re.search(r'name="csrf" value="([^"]+)"', resp.text)
        self.csrf = found.group(1) if found else self.csrf
        found = re.search(r'name="pow_c" value="([^"]+)"', resp.text)
        self.challenge = found.group(1) if found else self.challenge
        return resp

    async def send(self, phone: str, *, work: bool = True, lang: str = "ru", **headers: str) -> str:
        if not self.csrf:
            await self.page(f"/{lang}/otp", **headers)
        else:
            got = await self.http.get(f"/{lang}/otp/challenge", headers=headers)
            self.challenge = got.json()["challenge"]
        form = {
            "csrf": self.csrf,
            "phone": phone,
            "pow_c": self.challenge,
            "pow_n": solve(self.challenge, 8) if work else "",
            "dev": hashlib.sha256(phone.encode()).hexdigest()[:32],  # a device per number
        }
        resp = await self.http.post(f"/{lang}/otp/send", data=form, headers={"origin": ORIGIN, **headers})
        assert resp.status_code == 303, resp.text[:300]
        return resp.headers["location"]

    async def code(self) -> str:
        found = re.search(r'data-code="(\d{6})"', (await self.page()).text)
        assert found, "no code on the page"
        return found.group(1)

    async def verify(self, code: str, action: str = "verify") -> str:
        data = {"csrf": self.csrf, "code": code, "action": action}
        resp = await self.http.post("/ru/otp/verify", data=data, headers={"origin": ORIGIN})
        assert resp.status_code == 303, resp.text[:300]
        return resp.headers["location"]


@pytest.fixture
def app() -> FastAPI:
    return app_of()


async def test_the_page_opens_without_registration_with_its_own_script(app: FastAPI) -> None:
    for lang in ("ru", "en"):
        resp = await Visitor(app).page(f"/{lang}/otp")
        assert resp.headers["content-security-policy"] == CSP
        assert "script-src 'self'" in CSP and "'unsafe" not in CSP
        assert '<script src="/static/otp/otp.js" defer>' in resp.text
        assert 'id="otp-send"' in resp.text and 'data-bits="8"' in resp.text
    static = await Visitor(app).http.get("/static/otp/otp.js")
    assert static.status_code == 200 and "sha256" in static.text


async def test_an_honest_login_end_to_end(app: FastAPI) -> None:
    v = Visitor(app)
    assert await v.send("+998 90 123 45 67") == "/ru/otp?n=sent"
    code = await v.code()
    page = (await v.page()).text
    assert "+998901234567" in page and 'action="/ru/otp/verify"' in page
    assert await v.verify(code) == "/ru/otp?n=in"
    after = (await v.page("/ru/otp?n=in")).text
    assert "Вы вошли" in after and "data-code" not in after
    assert "+998 901" in after  # the hour's row of its prefix


async def test_a_wrong_code_and_another_number(app: FastAPI) -> None:
    v = Visitor(app)
    await v.send("+998901234567")
    code = await v.code()
    assert await v.verify(f"{(int(code) + 1) % 10**6:06d}") == "/ru/otp?n=wrong"
    assert await v.verify("", "other") == "/ru/otp"
    assert "data-code" not in (await v.page()).text
    assert await v.verify(code) == "/ru/otp?n=expired"


async def test_repeats_run_into_the_limit_and_the_page_says_how_long(app: FastAPI) -> None:
    v = Visitor(app)
    assert await v.send("+998901234567") == "/ru/otp?n=sent"
    where = await v.send("+998901234567")
    assert re.fullmatch(r"/ru/otp\?r=number&w=(29|30)", where), where
    page = (await v.page(where)).text
    assert 'data-reason="number"' in page and re.search(r"Следующий через (29|30) секунд", page)


async def test_every_refusal_names_its_layer() -> None:
    app = app_of(usd_per_hour="0.005")
    v = Visitor(app)
    assert await v.send("+79583100001") == "/ru/otp?r=geo"
    assert await v.send("+998901234567", work=False) == "/ru/otp?r=human"
    assert await v.send("+998901234567") == "/ru/otp?r=budget"
    assert await v.send("12345") == "/ru/otp?n=bad"
    for reason in ("geo", "human", "budget", "closed", "block", "device", "ip", "net"):
        page = (await v.page(f"/en/otp?r={reason}&w=5")).text
        assert f'data-reason="{reason}"' in page
    assert 'data-reason="evil"' not in (await v.page("/ru/otp?r=evil&n=evil&w=x")).text


async def test_a_spent_or_foreign_challenge_is_refused(app: FastAPI) -> None:
    a, b = Visitor(app), Visitor(app)
    await a.page()
    await b.page()
    form = {"csrf": b.csrf, "phone": "+998901234567", "pow_c": a.challenge, "pow_n": solve(a.challenge, 8)}
    resp = await b.http.post("/ru/otp/send", data=form, headers={"origin": ORIGIN})
    assert resp.headers["location"] == "/ru/otp?r=human"


async def test_without_a_session_or_token_nothing_is_sent(app: FastAPI) -> None:
    stranger = Visitor(app)
    resp = await stranger.http.post(
        "/ru/otp/send", data={"phone": "+998901234567"}, headers={"origin": ORIGIN}
    )
    assert resp.status_code == 403
    resp = await stranger.http.post("/ru/otp/verify", data={"code": "123456"}, headers={"origin": ORIGIN})
    assert resp.status_code == 403
    assert (await stranger.http.get("/ru/otp/challenge")).status_code == 401
    v = Visitor(app)
    await v.page()
    resp = await v.http.post(
        "/ru/otp/send", data={"csrf": "forged", "phone": "+998901234567"}, headers={"origin": ORIGIN}
    )
    assert resp.status_code == 403
    resp = await v.http.post(
        "/ru/otp/send",
        data={"csrf": v.csrf, "phone": "+998901234567"},
        headers={"origin": "https://evil.test"},
    )
    assert resp.status_code == 403
    assert (await v.http.get("/de/otp")).status_code == 404
    assert (await v.http.post("/de/otp/send")).status_code == 404


async def test_verify_without_a_code_asked(app: FastAPI) -> None:
    v = Visitor(app)
    await v.page()
    assert await v.verify("123456") == "/ru/otp?n=expired"


async def test_layers_off_by_flag_are_shown_off() -> None:
    app = app_of(layers="geo,budget")
    v = Visitor(app)
    page = (await v.page()).text
    assert page.count('class="otp-pill on"') == 2 and page.count('class="otp-pill off"') == 3
    assert await v.send("+998901234567", work=False) == "/ru/otp?n=sent"


# lab mode


LAB = {"x-lab-ip": "8.8.4.4", "x-lab-time": str(T)}


@pytest.mark.parametrize(
    ("env", "lab", "peer"),
    [
        ("prod", True, "127.0.0.1"),
        ("production", True, "127.0.0.1"),
        ("stand", True, "127.0.0.1"),
        ("dev", False, "127.0.0.1"),
        ("dev", True, "203.0.113.7"),
    ],
)
async def test_outside_the_lab_its_headers_are_ignored(env: str, lab: bool, peer: str) -> None:
    app = app_of(env, lab=lab)
    v = Visitor(app, peer)
    assert await v.send("+998901234567", **LAB) == "/ru/otp?n=sent"
    # a new IP and a time an hour on do not reset the number's pause
    later = {"x-lab-ip": "9.9.9.9", "x-lab-time": str(T + 7200)}
    assert (await v.send("+998901234567", **later)).startswith("/ru/otp?r=number")
    # other IPs claimed: the real one still collects the IP's count
    for i in range(4):
        await v.send(f"+9989{i}7654321", **{"x-lab-ip": f"9.9.9.{i}"})
    assert (await v.send("+998991111111", **{"x-lab-ip": "1.1.1.1"})).startswith("/ru/otp?r=ip")
    reset = await v.http.post("/lab/otp/reset", json={"layers": []})
    assert reset.status_code == (403 if lab and env == "dev" else 404)
    assert (await v.http.get("/lab/otp/state")).status_code == (403 if lab and env == "dev" else 404)


async def test_in_the_lab_a_loopback_peer_names_ip_and_time() -> None:
    app = app_of("lab", lab=True)
    v = Visitor(app, "127.0.0.1")
    assert await v.send("+998901234567", **LAB) == "/ru/otp?n=sent"
    later = {"x-lab-ip": "8.8.4.5", "x-lab-time": str(T + 7200)}
    assert await v.send("+998901234567", **later) == "/ru/otp?n=sent"
    for i in range(6):  # five free sends of an IP, the sixth waits
        where = await v.send(f"+9989{i}7654321", **{"x-lab-ip": "8.8.8.8", "x-lab-time": str(T + 9000 + i)})
    assert where.startswith("/ru/otp?r=ip")
    state = (await v.http.get("/lab/otp/state")).json()
    assert state["sent"] == 7 and state["refused"] == {"ip": 1}
    reset = await v.http.post("/lab/otp/reset", json={"layers": ["geo"], "pow_bits": 4})
    assert reset.json() == {"layers": ["geo"], "pow_bits": 4}
    assert (await v.http.get("/lab/otp/state")).json()["sent"] == 0
    bad = {"x-lab-ip": "not-an-ip", "x-lab-time": "soon"}
    assert await v.send("+998901234567", work=False, **bad) == "/ru/otp?n=sent"


async def test_no_setting_of_the_environment_turns_the_lab_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTP_LAB", "1")
    for env in ("dev", "lab", "prod"):
        settings = Settings(session_secret=SECRET, public_url=ORIGIN, app_env=env, sweep_minutes=0)
        app = create_app(settings, solutions=[make_solution()])
        v = Visitor(app, "127.0.0.1")
        await v.page()
        assert (await v.http.post("/lab/otp/reset", json={})).status_code == 404


async def test_the_lab_exists_only_in_its_environments() -> None:
    for env in ("ci", "staging", "production"):
        v = Visitor(app_of(env, lab=True), "127.0.0.1")
        assert (await v.http.get("/lab/otp/state")).status_code == 404


@pytest.mark.parametrize("header", ["x-forwarded-for", "forwarded"])
async def test_a_forwarded_request_is_not_a_lab_peer(header: str) -> None:
    """``serve`` takes the client's address from X-Forwarded-For: a stranger claiming 127.0.0.1 there is still
    a stranger."""
    app = app_of("lab", lab=True)
    v = Visitor(app, "127.0.0.1")
    claim = {header: "127.0.0.1" if header == "x-forwarded-for" else "for=127.0.0.1"}
    assert (await v.http.get("/lab/otp/state", headers=claim)).status_code == 403
    assert (await v.http.post("/lab/otp/reset", json={}, headers=claim)).status_code == 403
    assert await v.send("+998901234567", **claim, **LAB) == "/ru/otp?n=sent"
    later = {**claim, "x-lab-ip": "9.9.9.9", "x-lab-time": str(T + 7200)}
    assert (await v.send("+998901234567", **later)).startswith("/ru/otp?r=number")


@pytest.mark.parametrize("wait", ["²", "9" * 5000, "-1", "1e3", "٣"])
async def test_a_strange_pause_in_the_address_is_no_error(app: FastAPI, wait: str) -> None:
    resp = await Visitor(app).http.get("/ru/otp", params={"r": "ip", "w": wait})
    assert resp.status_code == 200 and 'data-reason="ip"' in resp.text


# the rest


@pytest.mark.parametrize(
    ("ip", "net"),
    [
        ("203.0.113.7", "203.0.113.0/24"),
        ("2001:db8:1:2::5", "2001:db8:1::/48"),
        ("::ffff:198.51.100.9", "198.51.100.0/24"),
        ("garbage", "garbage"),
    ],
)
def test_the_network_of_an_address(ip: str, net: str) -> None:
    assert network(ip) == net


def test_results_load_or_are_absent(tmp_path: Path) -> None:
    assert load_results(tmp_path / "none.json") is None
    (tmp_path / "r.json").write_text(json.dumps({"rows": []}))
    assert load_results(tmp_path / "r.json") == {"rows": []}


def test_settings_build_the_config() -> None:
    c = OtpSettings(layers="geo, budget,bogus", countries="+998, 7").config()
    assert c.layers == frozenset({"geo", "budget"}) and c.countries == ("998", "7")


@pytest.fixture(autouse=True)
def _no_otp_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("OTP_LAB", "OTP_LAYERS", "OTP_COUNTRIES"):
        monkeypatch.delenv(name, raising=False)
