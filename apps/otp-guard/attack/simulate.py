"""SMS pumping against the stand, one simulated hour per configuration of the layers.

    uv run python apps/otp-guard/attack/simulate.py

Every request goes through the stand's HTTP pages (in process, over ASGI) in lab mode: the stand takes the IP
and the clock from ``X-Lab-Ip`` and ``X-Lab-Time``, which it honours only in lab mode and only from the
loopback address. The crowd is drawn from a seeded generator, so a run gives the same table.

The attack: ``BROWSERS`` browsers behind residential proxies, each its own IP (thousands of /24 subnets), its
own fingerprint and its own session, ``ATTEMPTS`` code requests each, 20 minutes apart, on numbers taken in a
row inside ranges: half Uzbek, half expensive foreign destinations. They run JavaScript, so they solve the
proof of work (``--bits``, lowered in the lab to keep the run short; ``browsers.py`` measures the real cost).
The ``human_script`` row is the same attack by a script that does not solve it.

Honest users at the same time: ``HONEST`` people, 60 % on mobile carriers behind shared NAT (three carriers,
80 addresses each), 40 % on home broadband, 2 % with a Russian number; 12 % ask for a second code a minute or
two later, 90 % enter the code; a refused one tries once more after the pause the page names, if it is at most
five minutes, and otherwise gives up.
"""

import argparse
import asyncio
import heapq
import ipaddress
import json
import os
import random
import re
import sys
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from alnuzum_otp.guard import LAYERS
from alnuzum_otp.pow import solve
from alnuzum_otp.web import OtpSettings, make_solution
from alnuzum_platform.api import create_app
from alnuzum_platform.config import Settings

ORIGIN = "http://127.0.0.1:8040"
T0 = 1_795_302_000  # an hour boundary; the stand only sees the clock the headers give it
BROWSERS = 12_000
ATTEMPTS = 3
SUBNETS = 6_000
HONEST = 600
ABROAD = 0.02
CONFIGS: tuple[tuple[str, tuple[str, ...], bool], ...] = (
    ("none", (), True),
    ("geo", ("geo",), True),
    ("limits", ("limits",), True),
    ("human", ("human",), True),
    ("human_script", ("human",), False),
    ("conversion", ("conversion",), True),
    ("budget", ("budget",), True),
    ("all", LAYERS, True),
)
# number ranges the attack walks in a row: (start of the number, digits after it, first value)
UZ_RANGES = (("99833", 7, 1_200_000), ("99888", 7, 5_400_000), ("99877", 7, 2_100_000))
FOREIGN_RANGES = (
    ("7958", 7, 3_100_000),  # Russia, $0.7522
    ("99655", 7, 5_000_000),  # Kyrgyzstan, $0.4588
    ("99293", 7, 4_400_000),  # Tajikistan, $0.4505
    ("99450", 7, 6_000_000),  # Azerbaijan, $0.4110
    ("62812", 8, 30_000_000),  # Indonesia, $0.4414
)
UZ_OPERATORS = ("90", "91", "93", "94", "95", "97", "98", "99", "33", "88", "50", "77")
_CSRF = re.compile(r'name="csrf" value="([^"]+)"')
_POW = re.compile(r'name="pow_c" value="([^"]+)"')
_CODE = re.compile(r'data-code="(\d{6})"')


def public_subnets(rng: random.Random, n: int) -> list[str]:
    """``n`` distinct /24s of global unicast IPv4, as proxies of many providers would give."""
    found: set[str] = set()
    while len(found) < n:
        net = ipaddress.ip_network(f"{rng.randint(1, 223)}.{rng.randint(0, 255)}.{rng.randint(0, 255)}.0/24")
        if net.is_global:
            found.add(str(net.network_address)[: -len(".0")])
    return sorted(found)


@dataclass
class Visitor:
    ip: str
    device: str
    cookie: str = ""
    csrf: str = ""
    challenge: str = ""
    fresh: bool = False  # the challenge is not spent yet
    got: int = 0  # codes received
    refused: list[str] = field(default_factory=list)
    retried: bool = False
    asked_twice: bool = False


@dataclass
class Attempt:
    at: float
    sent: bool
    reason: str = ""


@dataclass
class Run:
    attack: list[Attempt] = field(default_factory=list)
    honest_requests: int = 0
    honest_sms: int = 0
    solved: int = 0
    blocked_by: Counter[str] = field(default_factory=Counter)


class Lab:
    """The stand in process; every request names its IP and time."""

    def __init__(self, layers: tuple[str, ...], bits: int) -> None:
        settings = Settings(
            session_secret="lab-" + "s" * 40,
            public_url=ORIGIN,
            app_env="lab",
            # the platform's own per-address limit sees every lab request from 127.0.0.1: out of the way
            rate_per_minute=10**9,
            rate_post_per_minute=10**9,
            sweep_minutes=0,
        )
        otp = OtpSettings(layers=",".join(layers), pow_bits=bits)
        self.app = create_app(settings, solutions=[make_solution(otp, lab=True)])
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, client=("127.0.0.1", 9)), base_url=ORIGIN
        )
        self.bits = bits

    async def call(self, v: Visitor, at: float, method: str, path: str, **kw: Any) -> httpx.Response:
        self.http.cookies.clear()
        headers = {"x-lab-ip": v.ip, "x-lab-time": f"{T0 + at:.3f}", "origin": ORIGIN}
        if v.cookie:
            headers["cookie"] = f"an_sid={v.cookie}"
        resp = await self.http.request(method, path, headers=headers, **kw)
        v.cookie = resp.cookies.get("an_sid") or v.cookie
        return resp

    async def open_page(self, v: Visitor, at: float) -> str:
        page = (await self.call(v, at, "GET", "/ru/otp")).text
        csrf, pow_c = _CSRF.search(page), _POW.search(page)
        if csrf is None:
            raise RuntimeError("the page has no form")
        v.csrf = csrf.group(1)
        if pow_c is not None:  # the page holding a code shows the code's form instead
            v.challenge, v.fresh = pow_c.group(1), True
        return page

    async def fresh_challenge(self, v: Visitor, at: float) -> None:
        v.challenge = (await self.call(v, at, "GET", "/ru/otp/challenge")).json()["challenge"]
        v.fresh = True

    async def send(self, v: Visitor, at: float, phone: str, *, solves: bool) -> tuple[bool, str, int]:
        if not v.fresh:
            await self.fresh_challenge(v, at)
        v.fresh = False
        nonce = solve(v.challenge, self.bits) if solves else ""
        form = {"csrf": v.csrf, "phone": phone, "pow_c": v.challenge, "pow_n": nonce, "dev": v.device}
        resp = await self.call(v, at, "POST", "/ru/otp/send", data=form)
        q = parse_qs(urlsplit(resp.headers.get("location", "")).query)
        if q.get("n") == ["sent"]:
            return True, "", 0
        return False, q.get("r", q.get("n", ["?"]))[0], int(q.get("w", ["0"])[0])

    async def code(self, v: Visitor, at: float) -> str | None:
        found = _CODE.search(await self.open_page(v, at))
        return found.group(1) if found else None

    async def verify(self, v: Visitor, at: float, code: str) -> bool:
        resp = await self.call(v, at, "POST", "/ru/otp/verify", data={"csrf": v.csrf, "code": code})
        return str(resp.headers.get("location", "")).endswith("n=in")

    async def state(self) -> dict[str, Any]:
        resp = await self.http.get("/lab/otp/state")
        data: dict[str, Any] = resp.json()
        return data


Event = tuple[float, int, str, int]  # time, order, kind, actor


def plan(seed: int, scale: float = 1.0) -> tuple[list[Event], list[Visitor], list[Visitor], dict[int, str]]:
    """The crowd: who acts when, from where, with which number (honest numbers drawn here; the attack's
    numbers are taken in a row as it goes). ``scale`` shrinks it for a quick look."""
    rng = random.Random(seed)  # nosec B311 - a seeded crowd for a reproducible run, not a secret
    subnets = public_subnets(rng, max(1, int(SUBNETS * scale)))
    attackers = [
        Visitor(f"{rng.choice(subnets)}.{rng.randint(1, 254)}", f"{rng.getrandbits(128):032x}")
        for _ in range(int(BROWSERS * scale))
    ]
    carriers = [[f"100.{64 + c}.{k}.{h}" for k in (10, 11) for h in range(10, 50)] for c in range(3)]
    honest: list[Visitor] = []
    numbers: dict[int, str] = {}
    for i in range(int(HONEST * scale)):
        ip = (
            rng.choice(rng.choice(carriers))
            if rng.random() < 0.6
            else f"198.{18 + rng.randint(0, 1)}.{rng.randint(0, 255)}.{rng.randint(1, 254)}"
        )
        honest.append(Visitor(ip, f"{rng.getrandbits(128):032x}"))
        numbers[i] = (
            f"+79{rng.randint(10**8, 10**9 - 1)}"
            if rng.random() < ABROAD
            else f"+998{rng.choice(UZ_OPERATORS)}{rng.randint(10**6, 10**7 - 1)}"
        )
    events: list[Event] = []
    n = 0
    for a in range(len(attackers)):
        first = rng.uniform(0, 1200)
        for k in range(ATTEMPTS):
            at = first if k == 0 else first + 1200 * k + rng.uniform(-60, 60)
            events.append((at, n, "attack", a))
            n += 1
    for h in range(len(honest)):
        events.append((rng.uniform(0, 3300), n, "honest", h))
        n += 1
    heapq.heapify(events)
    return events, attackers, honest, numbers


async def run_config(
    layers: tuple[str, ...], browser: bool, bits: int, seed: int, scale: float = 1.0
) -> tuple[Run, Any]:
    events, attackers, honest, numbers = plan(seed, scale)
    rng = random.Random(seed + 1)  # nosec B311 - a seeded crowd for a reproducible run, not a secret
    lab = Lab(layers, bits)
    run = Run()
    counters = [0] * (len(UZ_RANGES) + len(FOREIGN_RANGES))
    order = len(events)

    def later(at: float, kind: str, who: int) -> None:
        nonlocal order
        heapq.heappush(events, (at, order, kind, who))
        order += 1

    def next_number() -> str:
        r = (
            rng.randrange(len(UZ_RANGES))
            if rng.random() < 0.5
            else len(UZ_RANGES) + rng.randrange(len(FOREIGN_RANGES))
        )
        head, width, start = (UZ_RANGES + FOREIGN_RANGES)[r]
        counters[r] += 1
        return f"+{head}{start + counters[r]:0{width}d}"

    async with lab.http:
        while events:
            at, _, kind, who = heapq.heappop(events)
            if kind == "attack":
                v = attackers[who]
                if not v.csrf:
                    await lab.open_page(v, at)
                sent, reason, _ = await lab.send(v, at, next_number(), solves=browser)
                run.solved += browser
                run.attack.append(Attempt(at, sent, reason))
                continue
            v = honest[who]
            if kind == "honest":  # opens the page, the page solves the work in a few seconds, asks
                await lab.open_page(v, at)
                later(at + 5, "ask", who)
            elif kind == "ask":
                sent, reason, wait = await lab.send(v, at, numbers[who], solves=True)
                run.honest_requests += 1
                run.solved += 1
                if sent:
                    v.got += 1
                    run.honest_sms += 1
                    if not v.asked_twice and rng.random() < 0.12:
                        v.asked_twice = True
                        later(at + rng.uniform(45, 120), "ask", who)
                    elif rng.random() < 0.9:
                        later(at + rng.uniform(15, 90), "enter", who)
                else:
                    v.refused.append(reason)
                    if not v.retried and 0 < wait <= 300:
                        v.retried = True
                        later(at + wait + rng.uniform(1, 10), "ask", who)
                    elif not v.got:
                        run.blocked_by[reason] += 1
            elif kind == "enter":
                code = await lab.code(v, at)
                if code is not None:
                    await lab.verify(v, at, code)
        state = await lab.state()
    return run, state


def stop_time(attempts: list[Attempt]) -> float | None:
    """The start of the minute from which on, in every minute, less than 5 % of the attack's attempts get an
    SMS; None when the last minutes still let more through."""
    minutes: dict[int, list[int]] = {}
    for a in attempts:
        m = minutes.setdefault(int(a.at // 60), [0, 0])
        m[0] += 1
        m[1] += a.sent
    stop = None
    for minute in sorted(minutes, reverse=True):
        tried, sent = minutes[minute]
        if sent >= 0.05 * tried:
            break
        stop = minute * 60.0
    return stop


def one(job: tuple[str, tuple[str, ...], bool, int, int, float]) -> dict[str, Any]:
    """A configuration in a process of its own."""
    name, layers, browser, bits, seed, scale = job
    run, state = asyncio.run(run_config(layers, browser, bits, seed, scale))
    return row(name, run, state)


def row(name: str, run: Run, state: dict[str, Any]) -> dict[str, Any]:
    attack = sorted(run.attack, key=lambda a: a.at)
    refused = [a.at for a in attack if not a.sent]
    first = min(refused) if refused else None
    stop = stop_time(attack) if refused else None
    sms_attack = sum(a.sent for a in attack)
    return {
        "config": name,
        "requests": len(attack) + run.honest_requests,
        "sms_attack": sms_attack,
        "sms_honest": run.honest_sms,
        "usd": str(Decimal(state["usd"]).quantize(Decimal("0.01"))),
        "first_block": None if first is None else int(first),
        "stop": None if stop is None else int(stop),
        "honest_out": sum(run.blocked_by.values()),
        "honest_out_by": dict(run.blocked_by),
        "attack_refused_by": dict(Counter(a.reason for a in attack if not a.sent)),
        "solved": run.solved,
        "alerts": len(state["alerts"]),
        "by_country": state["by_country"],
    }


def table(rows: list[dict[str, Any]]) -> str:
    out = [
        "| configuration | code requests | SMS sent (attack / honest) | cost, $ | first refusal "
        "| attack stopped by | honest users kept out |",
        "|---|---:|---:|---:|---|---|---:|",
    ]

    def mmss(s: int | None) -> str:
        return "—" if s is None else f"{s // 60:02d}:{s % 60:02d}"

    for r in rows:
        out.append(
            f"| {r['config']} | {r['requests']} | {r['sms_attack']} / {r['sms_honest']} | {r['usd']} "
            f"| {mmss(r['first_block'])} | {mmss(r['stop'])} | {r['honest_out']} of {HONEST} |"
        )
    return "\n".join(out)


def main(argv: list[str] | None = None, *, on_row: Callable[[str], None] = print) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--bits", type=int, default=8, help="proof-of-work difficulty in the lab")
    parser.add_argument(
        "--only", nargs="*", help="configurations to run (default: all); the table is not saved"
    )
    parser.add_argument("--scale", type=float, default=1.0, help="shrink the crowd; the table is not saved")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "src" / "alnuzum_otp" / "data" / "results.json",
    )
    args = parser.parse_args(argv)
    started = time.monotonic()
    jobs = [
        (n, layers, b, args.bits, args.seed, args.scale)
        for n, layers, b in CONFIGS
        if not args.only or n in args.only
    ]
    # the configurations do not share anything: one process each
    with ProcessPoolExecutor(max_workers=min(len(jobs), os.cpu_count() or 1)) as pool:
        rows = list(pool.map(one, jobs))
    for r in rows:
        on_row(f"{r['config']}: {json.dumps(r, ensure_ascii=False)}")
    on_row(f"[{time.monotonic() - started:.0f} s]")
    previous: dict[str, Any] = {}
    if args.out.exists():
        previous = json.loads(args.out.read_text())
    browser_part = previous.get("browser")
    result = {
        "command": "uv run python apps/otp-guard/attack/simulate.py && "
        "uv run --with playwright python apps/otp-guard/attack/browsers.py",
        "seed": args.seed,
        "lab_bits": args.bits,
        "scenario": {
            "browsers": BROWSERS,
            "attempts": ATTEMPTS,
            "subnets": SUBNETS,
            "honest": HONEST,
            "abroad_pct": round(ABROAD * 100),
        },
        "rows": rows,
        "attack_solved": BROWSERS * ATTEMPTS,  # one proof of work for every attempt of the attack
        "browser": browser_part,
    }
    if browser_part:
        hours = BROWSERS * ATTEMPTS * browser_part["median_ms"] / 1000 / 3600
        result["attack_cpu_hours"] = f"{hours:.1f}"
    if not args.only and args.scale == 1.0:
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n")
    on_row(table(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
