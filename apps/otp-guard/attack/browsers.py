"""The attack's browser part: real headless Chromium against the stand with all five layers and the full
proof of work, and a script without a browser beside it.

    uv run --with playwright python apps/otp-guard/attack/browsers.py   # once: playwright install chromium

The stand runs in this process on 127.0.0.1 in lab mode; every browser is a context of its own (its user
agent, language, time zone and screen: another fingerprint) and claims its own proxy address through
``X-Lab-Ip``. Each opens the page, types a number of one range in a row, waits for the page to solve the work
and asks for the code. The time the page took is what one attempt costs an attacker's CPU; ``simulate.py``
multiplies it by the attack's attempts. Writes the ``browser`` part of ``results.json``.
"""

import argparse
import asyncio
import json
import re
import socket
import statistics
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import uvicorn
from playwright.async_api import async_playwright

from alnuzum_otp.web import OtpSettings, make_solution
from alnuzum_platform.api import create_app
from alnuzum_platform.config import Settings

_KHTML = "AppleWebKit/537.36 (KHTML, like Gecko)"
AGENTS = (
    f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) {_KHTML} Chrome/131.0 Safari/537.36",
    f"Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) {_KHTML} Chrome/130.0 Safari/537.36",
    f"Mozilla/5.0 (X11; Linux x86_64) {_KHTML} Chrome/129.0 Safari/537.36",
    f"Mozilla/5.0 (Linux; Android 14; SM-A546B) {_KHTML} Chrome/131.0 Mobile Safari/537.36",
)
ZONES = ("Asia/Tashkent", "Europe/Moscow", "Asia/Almaty", "Asia/Jakarta", "Europe/Istanbul")
LOCALES = ("ru-RU", "uz-UZ", "en-US", "id-ID", "kk-KZ")
SCREENS = ((1920, 1080), (1366, 768), (1536, 864), (412, 915), (1440, 900))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def serve(bits: int) -> tuple[str, uvicorn.Server]:
    port = free_port()
    origin = f"http://127.0.0.1:{port}"
    settings = Settings(
        session_secret="lab-" + "s" * 40,
        public_url=origin,
        app_env="lab",
        sweep_minutes=0,
        rate_per_minute=10**6,
        rate_post_per_minute=10**6,
    )
    app = create_app(settings, solutions=[make_solution(OtpSettings(pow_bits=bits), lab=True)])
    # no proxy headers: the peer is this process's loopback, so the lab headers are honoured
    server = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="warning", proxy_headers=False, lifespan="off"
        )
    )
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    return origin, server


async def browsers(origin: str, n: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    async with async_playwright() as pw:
        chromium = await pw.chromium.launch(headless=True)
        for i in range(n):
            w, h = SCREENS[i % len(SCREENS)]
            ctx = await chromium.new_context(
                user_agent=AGENTS[i % len(AGENTS)],
                locale=LOCALES[i % len(LOCALES)],
                timezone_id=ZONES[i % len(ZONES)],
                viewport={"width": w, "height": h},
                extra_http_headers={"X-Lab-Ip": f"{11 + i}.{40 + i}.{i * 7 % 250}.{20 + i}"},
            )
            page = await ctx.new_page()
            await page.goto(f"{origin}/ru/otp")
            await page.fill("input[name=phone]", f"+99833120{i:04d}")
            # the page's CSP allows no eval: wait on the attribute the page sets when the work is done
            await page.wait_for_selector("#otp-send[data-solved-ms]", state="attached", timeout=120_000)
            ms = int(await page.get_attribute("#otp-send", "data-solved-ms") or 0)
            device = await page.input_value("input[name=dev]")
            await page.click("#otp-send button[type=submit]")
            await page.wait_for_load_state()
            q = parse_qs(urlsplit(page.url).query)
            out.append({"ms": ms, "device": device, "outcome": q.get("n", q.get("r", ["?"]))[0]})
            await ctx.close()
        await chromium.close()
    return out


async def scripts(origin: str, n: int) -> list[str]:
    """Plain HTTP with the form's token but without solving the work."""
    outcomes: list[str] = []
    async with httpx.AsyncClient(base_url=origin) as http:
        for i in range(n):
            http.cookies.clear()
            headers = {"x-lab-ip": f"77.{i}.3.9", "origin": origin}
            page = await http.get("/ru/otp", headers=headers)
            found = re.search(r'name="csrf" value="([^"]+)"', page.text)
            challenge = re.search(r'name="pow_c" value="([^"]+)"', page.text)
            form = {
                "csrf": found.group(1) if found else "",
                "phone": f"+99888540{i:04d}",
                "pow_c": challenge.group(1) if challenge else "",
                "pow_n": "0",
            }
            resp = await http.post("/ru/otp/send", data=form, headers=headers)
            q = parse_qs(urlsplit(resp.headers.get("location", "")).query)
            outcomes.append(q.get("n", q.get("r", ["?"]))[0])
    return outcomes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--browsers", type=int, default=20)
    parser.add_argument("--scripts", type=int, default=20)
    parser.add_argument("--bits", type=int, default=OtpSettings().pow_bits)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "src" / "alnuzum_otp" / "data" / "results.json",
    )
    args = parser.parse_args(argv)
    origin, server = serve(args.bits)
    try:
        seen = asyncio.run(browsers(origin, args.browsers))
        refused = asyncio.run(scripts(origin, args.scripts))
    finally:
        server.should_exit = True
    times = [s["ms"] for s in seen]
    outcomes: dict[str, int] = {}
    for s in seen:
        outcomes[s["outcome"]] = outcomes.get(s["outcome"], 0) + 1
    part = {
        "bits": args.bits,
        "solved": len(times),
        "median_ms": round(statistics.median(times)),
        "max_ms": max(times),
        "devices": len({s["device"] for s in seen}),
        "outcomes": outcomes,
        "script_refused": sum(o == "human" for o in refused),
        "scripts": len(refused),
    }
    print(json.dumps(part, ensure_ascii=False))
    data: dict[str, Any] = json.loads(args.out.read_text()) if args.out.exists() else {}
    data["browser"] = part
    scenario = data.get("scenario")
    if scenario:
        attempts = scenario["browsers"] * scenario["attempts"]
        data["attack_cpu_hours"] = f"{attempts * part['median_ms'] / 1000 / 3600:.1f}"
    args.out.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
