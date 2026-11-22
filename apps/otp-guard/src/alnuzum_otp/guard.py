"""The five layers in front of the SMS provider, each behind a flag, and the codes themselves.

Order of checks, cheapest first: ``human`` (proof of work, checked by the page's handler before this),
``geo``, a prefix closed by ``conversion``, ``limits``, ``budget``. A refusal names its layer and, for a
limit, how long to wait. State is kept in the memory of the one process the platform runs; the same counters
would live in Redis with several processes. Every method takes ``now`` (seconds), so the attack can run a
simulated hour in a minute.
"""

import hashlib
import hmac
import logging
import secrets
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal

from alnuzum_otp.numbers import Number

log = logging.getLogger("alnuzum_otp")

LAYERS = ("geo", "limits", "human", "conversion", "budget")
KEYS = ("number", "block", "device", "ip", "net")
HOUR = 3600.0
DAY = 86400.0


@dataclass(frozen=True)
class Limit:
    free: int  # sends within an hour that need no pause
    base: float  # the first pause after them, seconds; every next one doubles
    cap: float  # the longest pause


DEFAULT_LIMITS: dict[str, Limit] = {
    "number": Limit(1, 30, HOUR),
    "block": Limit(3, 60, HOUR),
    "device": Limit(3, 60, HOUR),
    "ip": Limit(5, 60, HOUR),
    "net": Limit(100, 5, 600),
}


@dataclass(frozen=True)
class Config:
    layers: frozenset[str] = frozenset(LAYERS)
    countries: tuple[str, ...] = ("998",)  # calling codes (or longer prefixes) codes are sent to
    limits: dict[str, Limit] = field(default_factory=lambda: dict(DEFAULT_LIMITS))
    usd_per_hour: Decimal = Decimal(20)
    # conversion: confirmed share over ``window`` of sends older than ``mature``, at least ``least`` of them;
    # a prefix closes for ``close_for`` when the share falls under ``drop`` × its normal share
    window: float = 1800
    mature: float = 180
    least: int = 20
    normal: float = 0.8
    drop: float = 0.5
    close_for: float = HOUR
    code_ttl: float = 300
    code_tries: int = 3

    def on(self, layer: str) -> bool:
        return layer in self.layers


@dataclass(frozen=True)
class Who:
    ip: str
    net: str
    device: str


@dataclass(frozen=True)
class Refusal:
    layer: str  # geo, limits, human, conversion, budget
    reason: str  # geo, number, block, device, ip, net, human, closed, budget
    wait: int = 0  # seconds, for a limit


@dataclass
class Sent:
    at: float
    prefix: str
    usd: Decimal
    confirmed: bool = False


@dataclass
class Code:
    digest: bytes
    owner: str
    expires: float
    tries: int
    sent: Sent


@dataclass
class Alert:
    at: float
    prefix: str
    sent: int
    confirmed: int
    normal: float


@dataclass
class Counter:
    times: deque[float] = field(default_factory=deque)


class CountingProvider:
    """An SMS provider that sends nothing: it counts the messages and what they would have cost."""

    def __init__(self) -> None:
        self.count = 0
        self.usd = Decimal(0)
        self.by_country: dict[str, int] = {}

    def send(self, number: Number, text: str) -> Decimal:
        del text  # nothing leaves the stand
        self.count += 1
        self.usd += number.country.usd
        iso = number.country.iso
        self.by_country[iso] = self.by_country.get(iso, 0) + 1
        return number.country.usd


class Guard:
    def __init__(self, config: Config, provider: CountingProvider | None = None) -> None:
        self.config = config
        self.provider = provider or CountingProvider()
        self._key = secrets.token_bytes(32)
        self._counters: dict[tuple[str, str], Counter] = {}
        self._recent: dict[str, deque[Sent]] = {}  # prefix -> sends within the conversion window
        self._hours: dict[tuple[str, int], list[int]] = {}  # (prefix, hour) -> [sent, confirmed]
        self._closed: dict[str, float] = {}  # prefix -> until
        self._spend: deque[tuple[float, Decimal]] = deque()
        self._spent = Decimal(0)
        self._codes: dict[str, Code] = {}
        self.alerts: deque[Alert] = deque(maxlen=50)
        self.refused: dict[str, int] = {}
        self._since_prune = 0

    # the layers

    def refuse(self, number: Number, who: Who, now: float) -> Refusal | None:
        c = self.config
        if c.on("geo") and not any(number.e164.startswith(p) for p in c.countries):
            return Refusal("geo", "geo")
        if c.on("conversion") and self._closed_now(number.prefix, now):
            return Refusal("conversion", "closed")
        if c.on("limits"):
            for kind, key in self._keys(number, who):
                wait = self._wait(kind, key, now)
                if wait > 0:
                    return Refusal("limits", kind, max(1, round(wait)))
        if c.on("budget") and self._spent_in_hour(now) + number.country.usd > c.usd_per_hour:
            return Refusal("budget", "budget")
        return None

    def request(self, number: Number, who: Who, owner: str, now: float) -> tuple[str | None, Refusal | None]:
        """A code for the number, or the refusal. The code is returned only because the stand shows it."""
        refusal = self.refuse(number, who, now)
        if refusal is not None:
            self.refused[refusal.reason] = self.refused.get(refusal.reason, 0) + 1
            return None, refusal
        code = f"{secrets.randbelow(10**6):06d}"
        usd = self.provider.send(number, f"Your code: {code}")
        sent = Sent(now, number.prefix, usd)
        self._codes[number.e164] = Code(
            self._digest(number.e164, code), owner, now + self.config.code_ttl, self.config.code_tries, sent
        )
        for kind, key in self._keys(number, who):
            self._counters.setdefault((kind, key), Counter()).times.append(now)
        self._recent.setdefault(sent.prefix, deque()).append(sent)
        self._hours.setdefault((sent.prefix, int(now // HOUR)), [0, 0])[0] += 1
        self._spend.append((now, usd))
        self._spent += usd
        self._since_prune += 1
        if self._since_prune >= 5000:
            self.prune(now)
        log.info("otp: code sent to %s ($%s)", number.masked(), usd)
        return code, None

    def verify(self, number: Number, code: str, owner: str, now: float) -> str:
        """``ok``, ``wrong`` (tries left), ``expired`` (no code, an old one, or no tries left)."""
        held = self._codes.get(number.e164)
        if held is None or held.owner != owner or held.expires < now:
            return "expired"
        if not hmac.compare_digest(held.digest, self._digest(number.e164, code)):
            held.tries -= 1
            if held.tries <= 0:
                del self._codes[number.e164]
                return "expired"
            return "wrong"
        del self._codes[number.e164]
        held.sent.confirmed = True
        hour = self._hours.get((held.sent.prefix, int(held.sent.at // HOUR)))
        if hour is not None:
            hour[1] += 1
        return "ok"

    # limits

    @staticmethod
    def _keys(number: Number, who: Who) -> tuple[tuple[str, str], ...]:
        return (
            ("number", number.e164),
            ("block", number.block),
            ("device", who.device),
            ("ip", who.ip),
            ("net", who.net),
        )

    def _wait(self, kind: str, key: str, now: float) -> float:
        counter = self._counters.get((kind, key))
        if counter is None:
            return 0.0
        times = counter.times
        while times and times[0] <= now - HOUR:
            times.popleft()
        limit = self.config.limits[kind]
        over = len(times) - limit.free
        if over < 0 or not times:
            return 0.0
        pause = min(limit.cap, limit.base * (1 << min(over, 30)))
        return max(0.0, times[-1] + pause - now)

    # conversion

    def _closed_now(self, prefix: str, now: float) -> bool:
        until = self._closed.get(prefix)
        if until is not None:
            if until > now:
                return True
            del self._closed[prefix]
        c = self.config
        recent = self._recent.get(prefix)
        if not recent:
            return False
        while recent and recent[0].at < now - c.window:
            recent.popleft()
        sent = confirmed = 0
        for s in recent:
            if s.at > now - c.mature:
                break
            sent += 1
            confirmed += s.confirmed
        if sent < c.least:
            return False
        normal = self.normal_share(prefix, now)
        if confirmed / sent >= c.drop * normal:
            return False
        self._closed[prefix] = now + c.close_for
        recent.clear()
        self.alerts.append(Alert(now, prefix, sent, confirmed, normal))
        log.warning(
            "otp: ALERT prefix %s closed for %d min: %d of %d codes confirmed in %d min, normal %.0f %%",
            prefix,
            c.close_for // 60,
            confirmed,
            sent,
            c.window // 60,
            normal * 100,
        )
        return True

    def normal_share(self, prefix: str, now: float) -> float:
        """The confirmed share of the prefix over the day before the current hour; the configured one until
        200 sends make it."""
        hour = int(now // HOUR)
        sent = confirmed = 0
        for h in range(hour - 24, hour - 1):
            s, c = self._hours.get((prefix, h), (0, 0))
            sent += s
            confirmed += c
        return confirmed / sent if sent >= 200 else self.config.normal

    # budget

    def _spent_in_hour(self, now: float) -> Decimal:
        while self._spend and self._spend[0][0] <= now - HOUR:
            self._spent -= self._spend.popleft()[1]
        return self._spent

    # what the page shows

    def closed(self, now: float) -> list[tuple[str, float]]:
        return sorted((p, until) for p, until in self._closed.items() if until > now)

    def spent_in_hour(self, now: float) -> Decimal:
        return self._spent_in_hour(now)

    def hours(self, now: float, back: int = 3) -> list[tuple[int, str, int, int]]:
        """(hour, prefix, sent, confirmed) for the last ``back`` hours, newest first."""
        first = int(now // HOUR) - back + 1
        rows = [(h, p, v[0], v[1]) for (p, h), v in self._hours.items() if h >= first]
        return sorted(rows, key=lambda r: (-r[0], -r[2], r[1]))

    def prune(self, now: float) -> None:
        """Forget what no layer looks at any more: counters past their hour, codes past their life, hours past
        a day."""
        self._since_prune = 0
        for ck in [k for k, v in self._counters.items() if not v.times or v.times[-1] <= now - HOUR]:
            del self._counters[ck]
        for e164 in [k for k, v in self._codes.items() if v.expires < now]:
            del self._codes[e164]
        for prefix in [k for k, v in self._recent.items() if not v or v[-1].at < now - self.config.window]:
            del self._recent[prefix]
        oldest = int((now - DAY) // HOUR) - 1
        for hk in [k for k in self._hours if k[1] < oldest]:
            del self._hours[hk]
        for prefix in [k for k, until in self._closed.items() if until <= now]:
            del self._closed[prefix]

    def _digest(self, e164: str, code: str) -> bytes:
        return hmac.new(self._key, f"{e164}:{code}".encode(), hashlib.sha256).digest()
