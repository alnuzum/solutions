"""Pages of the stand: a login by SMS code (the code is shown on the page, no SMS leaves), the live counters
of the five layers and the table of the attack run. The page runs one script of its own: the proof of work and
the device's fingerprint.

Lab mode lets the attack script tell the stand which IP a request comes from and what time it is
(``X-Lab-Ip``, ``X-Lab-Time``) and reset the stand with other layers (``POST /lab/otp/reset``). It exists only
when the code builds the solution with ``lab=True`` (the attack scripts do; no setting of the environment
turns it on) in a ``dev`` or ``lab`` environment, and only for a request from the loopback address that
carries no ``X-Forwarded-For`` or ``Forwarded`` (``serve`` trusts those for the client's address); anywhere
else the headers are ignored and the route is not there.
"""

import hashlib
import ipaddress
import json
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic_settings import BaseSettings, SettingsConfigDict

from alnuzum_otp.guard import DEFAULT_LIMITS, LAYERS, Config, Guard, Who
from alnuzum_otp.i18n import texts
from alnuzum_otp.numbers import COUNTRIES, BadNumber, parse
from alnuzum_otp.pow import ProofOfWork
from alnuzum_platform.solutions import Kit, Solution
from alnuzum_platform.web.routes import PageError, redirect
from alnuzum_platform.web.security import SECURITY_HEADERS, RateLimiter

log = logging.getLogger("alnuzum_otp.web")
HERE = Path(__file__).parent
RESULTS = HERE / "data" / "results.json"
LOOPBACK = frozenset({"127.0.0.1", "::1"})
LAB_ENVS = frozenset({"dev", "lab"})  # environments where lab mode may exist at all
NOTICES = frozenset({"sent", "in", "wrong", "expired", "bad", "csrf"})
VERIFIED = {"ok": "in"}
REASONS = frozenset({"geo", "number", "block", "device", "ip", "net", "human", "closed", "budget"})
# the page's own script: the proof of work and the fingerprint; everything else as on every other page
CSP = SECURITY_HEADERS["Content-Security-Policy"].replace(
    "default-src 'none'", "default-src 'none'; script-src 'self'; connect-src 'self'"
)


class OtpSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OTP_", env_file=None, extra="ignore")

    layers: str = ",".join(LAYERS)
    countries: str = "998"
    usd_per_hour: Decimal = Decimal(20)
    pow_bits: int = 20

    def config(self) -> Config:
        layers = frozenset(x.strip() for x in self.layers.split(",") if x.strip() in LAYERS)
        countries = tuple(x.strip().lstrip("+") for x in self.countries.split(",") if x.strip())
        return Config(layers=layers, countries=countries, usd_per_hour=self.usd_per_hour)


@dataclass
class Pending:
    e164: str
    code: str
    expires: float


@dataclass
class Stand:
    guard: Guard
    pow: ProofOfWork
    pending: dict[str, Pending] = field(default_factory=dict)
    pruned: float = 0.0

    def tidy(self, now: float) -> None:
        """Forget what has expired, at most every ten minutes (the page of a quiet stand calls it)."""
        if now - self.pruned < 600 and len(self.pending) < 50_000:
            return
        self.pruned = now
        self.guard.prune(now)
        self.pending = {k: v for k, v in self.pending.items() if v.expires >= now}


def network(ip: str) -> str:
    """The provider's network a stand without an ASN database can see: the /24 (IPv6: the /48)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip[:64]
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is None:
        return str(ipaddress.ip_network(f"{addr}/48", strict=False))
    v4 = addr.ipv4_mapped if isinstance(addr, ipaddress.IPv6Address) else addr
    return str(ipaddress.ip_network(f"{v4}/24", strict=False))


def load_results(path: Path = RESULTS) -> dict[str, Any] | None:
    try:
        data: dict[str, Any] = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data


def _lang(value: str) -> str:
    if value not in ("ru", "en"):
        raise PageError(404, "e_404_t", "e_404")
    return {"ru": "ru", "en": "en"}[value]


def build(kit: Kit, otp: OtpSettings, lab: bool, config: Config | None = None) -> APIRouter:
    router = APIRouter()
    env = kit.settings.app_env
    lab_on = lab and env in LAB_ENVS
    if lab and not lab_on:
        log.error("otp: lab mode refused in the %s environment", env)
    key = kit.settings.session_secret.get_secret_value().encode()
    stand = Stand(Guard(config or otp.config()), ProofOfWork(key, otp.pow_bits))
    results = load_results()

    def lab_peer(request: Request) -> bool:
        return (
            lab_on
            and request.client is not None
            and request.client.host in LOOPBACK
            and "x-forwarded-for" not in request.headers
            and "forwarded" not in request.headers
        )

    def now_of(request: Request) -> float:
        if lab_peer(request):
            given = request.headers.get("x-lab-time")
            if given:
                try:
                    return float(given)
                except ValueError:
                    pass
        return time.time()

    def ip_of(request: Request) -> str:
        real = request.client.host if request.client else "?"
        if lab_peer(request):
            given = request.headers.get("x-lab-ip", "")
            try:
                return str(ipaddress.ip_address(given))
            except ValueError:
                pass
        return real

    def device_of(request: Request, given: str) -> str:
        if 8 <= len(given) <= 64 and all(ch in "0123456789abcdef" for ch in given):
            return "f:" + given
        seen = f"{request.headers.get('user-agent', '')}|{request.headers.get('accept-language', '')}"
        return "h:" + hashlib.sha256(seen.encode()).hexdigest()[:16]

    def page(request: Request, lang: str, status: int = 200, **ctx: Any) -> HTMLResponse:
        sid = kit.ensure_session(request)
        owner = kit.owner(sid)
        now = now_of(request)
        stand.tidy(now)
        held = stand.pending.get(owner)
        if held is not None and held.expires < now:
            del stand.pending[owner]
            held = None
        guard = stand.guard
        resp = kit.page(
            request,
            "otp.html",
            lang,
            status=status,
            challenge=stand.pow.issue(owner, now),
            bits=stand.pow.bits,
            held=held,
            held_plus="+" + held.e164 if held else "",
            layers=[(name, guard.config.on(name)) for name in LAYERS],
            config=guard.config,
            limits=DEFAULT_LIMITS,
            spent=guard.spent_in_hour(now),
            sent_total=guard.provider.count,
            closed=[(p, int((until - now) // 60) + 1) for p, until in guard.closed(now)],
            alerts=list(reversed(guard.alerts))[:5],
            hours=[(_hour(h), p, s, c) for h, p, s, c in guard.hours(now)][:24],
            countries=COUNTRIES,
            results=results,
            **ctx,
        )
        resp.headers["Content-Security-Policy"] = CSP
        return resp

    @router.get("/{lang}/otp")
    async def index(request: Request, lang: str) -> HTMLResponse:
        lang = _lang(lang)
        q = request.query_params
        notice = q.get("n") if q.get("n") in NOTICES else None
        reason = q.get("r") if q.get("r") in REASONS else None
        wait = q.get("w", "")
        return page(
            request,
            lang,
            notice=notice,
            reason=reason,
            wait=int(wait) if wait.isascii() and wait.isdigit() and len(wait) <= 5 else 0,
        )

    @router.get("/{lang}/otp/challenge")
    async def challenge(request: Request, lang: str) -> JSONResponse:
        _lang(lang)
        sid = request.state.sid
        if sid is None:
            return JSONResponse({"error": "no session"}, status_code=401)
        return JSONResponse(
            {"challenge": stand.pow.issue(kit.owner(sid), now_of(request)), "bits": stand.pow.bits},
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/{lang}/otp/send")
    async def send(request: Request, lang: str) -> Response:
        lang = _lang(lang)
        form = await request.form(max_files=0, max_fields=6, max_part_size=512)
        sid = kit.need_csrf(request, form, lang)
        owner = kit.owner(sid)
        now = now_of(request)
        back = f"/{lang}/otp"
        guard = stand.guard
        if guard.config.on("human") and not stand.pow.verify(
            str(form.get("pow_c") or ""), str(form.get("pow_n") or ""), owner, now
        ):
            guard.refused["human"] = guard.refused.get("human", 0) + 1
            return redirect(f"{back}?r=human")
        try:
            number = parse(str(form.get("phone") or ""))
        except BadNumber:
            return redirect(f"{back}?n=bad")
        ip = ip_of(request)
        who = Who(RateLimiter.client(ip), network(ip), device_of(request, str(form.get("dev") or "")))
        code, refusal = guard.request(number, who, owner, now)
        if refusal is not None:
            wait = f"&w={refusal.wait}" if refusal.wait else ""
            return redirect(f"{back}?r={refusal.reason}{wait}")
        stand.pending[owner] = Pending(number.e164, code or "", now + guard.config.code_ttl)
        return redirect(f"{back}?n=sent")

    @router.post("/{lang}/otp/verify")
    async def verify(request: Request, lang: str) -> Response:
        lang = _lang(lang)
        form = await request.form(max_files=0, max_fields=4, max_part_size=512)
        sid = kit.need_csrf(request, form, lang)
        owner = kit.owner(sid)
        back = f"/{lang}/otp"
        held = stand.pending.get(owner)
        if form.get("action") == "other" or held is None:
            stand.pending.pop(owner, None)
            return redirect(back if form.get("action") == "other" else f"{back}?n=expired")
        code = "".join(ch for ch in str(form.get("code") or "") if ch.isdigit())[:6]
        result = stand.guard.verify(parse("+" + held.e164), code, owner, now_of(request))
        if result != "wrong":
            stand.pending.pop(owner, None)
        return redirect(f"{back}?n={VERIFIED.get(result, result)}")

    if lab_on:

        @router.post("/lab/otp/reset")
        async def reset(request: Request) -> JSONResponse:
            if not lab_peer(request):
                return JSONResponse({"error": "lab only"}, status_code=403)
            body = await request.json()
            layers = frozenset(x for x in body.get("layers", LAYERS) if x in LAYERS)
            bits = int(body.get("pow_bits", otp.pow_bits))
            stand.guard = Guard(
                Config(layers=layers, countries=otp.config().countries, usd_per_hour=otp.usd_per_hour)
            )
            stand.pow = ProofOfWork(key, bits)
            stand.pending.clear()
            return JSONResponse({"layers": sorted(layers), "pow_bits": bits})

        @router.get("/lab/otp/state")
        async def state(request: Request) -> JSONResponse:
            if not lab_peer(request):
                return JSONResponse({"error": "lab only"}, status_code=403)
            g = stand.guard
            return JSONResponse(
                {
                    "sent": g.provider.count,
                    "usd": str(g.provider.usd),
                    "by_country": g.provider.by_country,
                    "refused": g.refused,
                    "alerts": [[a.at, a.prefix, a.sent, a.confirmed] for a in g.alerts],
                }
            )

    return router


def _hour(h: int) -> str:
    """The hour in Tashkent (UTC+5)."""
    return f"{(h + 5) % 24:02d}:00"


def make_solution(
    otp: OtpSettings | None = None, *, lab: bool = False, config: Config | None = None
) -> Solution:
    """``lab`` turns lab mode on (in ``dev`` or ``lab`` only); ``config`` replaces the layers' settings."""
    otp = otp or OtpSettings()
    return Solution(
        slug="otp",
        titles={"ru": "Вход по SMS без накачки", "en": "SMS login without pumping"},
        templates=HERE / "templates",
        static=HERE / "static",
        texts=texts,
        router=lambda kit: build(kit, otp, lab, config),
    )


solution = make_solution()

__all__ = ["make_solution", "solution"]
