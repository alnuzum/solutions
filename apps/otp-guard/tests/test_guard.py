"""The layers one by one, the codes, the provider that counts, the numbers and the proof of work."""

import hashlib
from decimal import Decimal

import pytest

from alnuzum_otp.guard import DEFAULT_LIMITS, HOUR, LAYERS, Config, CountingProvider, Guard, Limit, Who
from alnuzum_otp.numbers import COUNTRIES, BadNumber, parse
from alnuzum_otp.pow import ProofOfWork, solve, zero_bits

T = 1_795_302_000.0
UZ = Decimal(COUNTRIES["998"].usd)


def who(n: int = 1, *, ip: str | None = None, net: str | None = None, device: str | None = None) -> Who:
    return Who(ip or f"203.0.{n % 250}.{n % 250}", net or f"203.0.{n % 250}.0/24", device or f"d{n}")


def only(*layers: str, **kw: object) -> Guard:
    return Guard(Config(layers=frozenset(layers), **kw))  # type: ignore[arg-type]


# numbers


@pytest.mark.parametrize(
    ("raw", "e164", "iso", "prefix", "block"),
    [
        ("+998 (90) 123-45-67", "998901234567", "UZ", "+998 901", "9989012345"),
        ("00998901234567", "998901234567", "UZ", "+998 901", "9989012345"),
        ("+7 958 310 00 01", "79583100001", "RU", "+7 958", "795831000"),
        ("+7 701 123 45 67", "77011234567", "KZ", "+7 701", "770112345"),
        ("+62 812 3000 0001", "6281230000001", "ID", "+62 812", "62812300000"),
    ],
)
def test_a_number_knows_its_country_prefix_and_range(
    raw: str, e164: str, iso: str, prefix: str, block: str
) -> None:
    n = parse(raw)
    assert (n.e164, n.country.iso, n.prefix, n.block) == (e164, iso, prefix, block)
    assert n.plus == "+" + e164
    assert n.masked().startswith(prefix) and n.masked().endswith(e164[-2:]) and e164[5:9] not in n.masked()


@pytest.mark.parametrize(
    "raw", ["", "998901234567", "+99890123456", "+9989012345678", "+49 151 2345678", "+٩٩٨"]
)
def test_an_unknown_or_malformed_number_is_refused(raw: str) -> None:
    with pytest.raises(BadNumber):
        parse(raw)


def test_prices_come_from_the_lists_named_in_the_module() -> None:
    assert COUNTRIES["998"].usd == Decimal("0.0079")  # 95 UZS at 12 004.48
    assert COUNTRIES["7"].usd == Decimal("0.7522")
    assert all(c.source.startswith("https://") for c in COUNTRIES.values())


# the provider


def test_the_provider_counts_and_prices_but_sends_nothing() -> None:
    p = CountingProvider()
    assert p.send(parse("+998901234567"), "x") == UZ
    p.send(parse("+79583100001"), "x")
    assert (p.count, p.usd, p.by_country) == (2, UZ + Decimal("0.7522"), {"UZ": 1, "RU": 1})


# no layer


def test_without_layers_every_known_number_gets_a_code() -> None:
    g = only()
    for i in range(50):
        code, refusal = g.request(parse(f"+7958310{i:04d}"), who(1), "o", T)
        assert refusal is None and code is not None and len(code) == 6
    assert g.provider.count == 50


# layer 1


def test_geo_sends_only_to_the_countries_served() -> None:
    g = only("geo", countries=("998",))
    assert g.request(parse("+998901234567"), who(), "o", T)[1] is None
    _, refusal = g.request(parse("+79583100001"), who(), "o", T)
    assert refusal is not None and (refusal.layer, refusal.reason) == ("geo", "geo")
    assert g.refused == {"geo": 1}


def test_geo_takes_a_longer_prefix_too() -> None:
    g = only("geo", countries=("77",))
    assert g.request(parse("+77011234567"), who(), "o", T)[1] is None
    assert g.request(parse("+79583100001"), who(), "o", T)[1] is not None


# layer 2


def test_a_number_waits_a_pause_that_doubles() -> None:
    g = only("limits")
    n = parse("+998901234567")
    assert g.request(n, who(1), "o", T)[1] is None
    _, r = g.request(n, who(2), "o", T + 10)
    assert r is not None and (r.reason, r.wait) == ("number", 20)
    assert g.request(n, who(3), "o", T + 30)[1] is None
    _, r = g.request(n, who(4), "o", T + 60)
    assert r is not None and (r.reason, r.wait) == ("number", 30)  # 60 s after the second
    assert g.request(n, who(5), "o", T + 90)[1] is None
    _, r = g.request(n, who(6), "o", T + 100)
    assert r is not None and r.wait == 110  # 120 s after the third
    # an hour later the counter has forgotten
    assert g.request(n, who(7), "o", T + 30 + HOUR + 61)[1] is None


@pytest.mark.parametrize(
    ("kind", "make"),
    [
        ("block", lambda i: (f"+99890123450{i}", who(i))),
        ("device", lambda i: (f"+9989{i}1234567", who(i, device="same"))),
        ("ip", lambda i: (f"+9989{i}1234567", who(i, ip="198.51.100.7"))),
    ],
)
def test_neighbouring_numbers_one_device_one_ip_are_limited(kind: str, make: object) -> None:
    g = only("limits")
    free = DEFAULT_LIMITS[kind].free
    for i in range(free):
        phone, w = make(i)  # type: ignore[operator]
        assert g.request(parse(phone), w, "o", T + i)[1] is None, i
    phone, w = make(free)  # type: ignore[operator]
    _, r = g.request(parse(phone), w, "o", T + free)
    assert r is not None and r.reason == kind and r.wait > 0


def test_a_network_is_limited_past_its_free_sends() -> None:
    limits = dict(DEFAULT_LIMITS) | {"net": Limit(2, 5, 600)}
    g = only("limits", limits=limits)
    for i in range(2):
        assert g.request(parse(f"+9989{i}1234567"), who(i, net="n"), "o", T)[1] is None
    _, r = g.request(parse("+998951234567"), who(9, net="n"), "o", T + 1)
    assert r is not None and (r.reason, r.wait) == ("net", 4)


# layer 4


def pump(g: Guard, start: float, count: int, prefix: str = "+99833120") -> None:
    for i in range(count):
        g.request(parse(f"{prefix}{i:04d}"), who(i), "o", start + i)


def test_a_prefix_whose_codes_are_not_confirmed_closes_and_raises_an_alert() -> None:
    g = only("conversion")
    pump(g, T, 30)  # nobody confirms
    # the sends are not mature yet: the prefix stays open
    assert g.request(parse("+998331209999"), who(99), "o", T + 60)[1] is None
    _, r = g.request(parse("+998331208888"), who(98), "o", T + 300)
    assert r is not None and (r.layer, r.reason) == ("conversion", "closed")
    assert [(a.prefix, a.sent, a.confirmed) for a in g.alerts] == [("+998 331", 31, 0)]
    assert g.closed(T + 300) == [("+998 331", T + 300 + HOUR)]
    # another prefix of the same operator stays open
    assert g.request(parse("+998339999999"), who(97), "o", T + 301)[1] is None
    # an hour later the prefix opens again
    assert g.request(parse("+998331207777"), who(96), "o", T + 300 + HOUR + 1)[1] is None
    assert g.closed(T + 300 + HOUR + 1) == []


def test_a_prefix_whose_codes_are_confirmed_stays_open() -> None:
    g = only("conversion")
    for i in range(40):
        n = parse(f"+99833120{i:04d}")
        code, _ = g.request(n, who(i), f"o{i}", T + i)
        assert code is not None
        if i % 10:  # 90 %
            assert g.verify(n, code, f"o{i}", T + i + 20) == "ok"
    assert g.request(parse("+998331209999"), who(99), "o", T + 400)[1] is None
    assert not g.alerts
    rows = g.hours(T + 400)
    assert rows[0][1:] == ("+998 331", 41, 36)


def test_the_normal_share_is_learnt_from_the_day_before() -> None:
    g = only("conversion")
    assert g.normal_share("+998 331", T) == 0.8
    for i in range(250):  # a day ago, half confirmed
        n = parse(f"+99833120{i:04d}")
        code, _ = g.request(n, who(i), "o", T - 20 * HOUR + i)
        if i % 2 and code:
            g.verify(n, code, "o", T - 20 * HOUR + i + 1)
    assert g.normal_share("+998 331", T) == pytest.approx(0.5, abs=0.01)


# layer 5


def test_spend_stops_at_the_hourly_ceiling_and_frees_up_an_hour_later() -> None:
    g = only("budget", usd_per_hour=Decimal("2.00"))
    sent = 0
    for i in range(10):
        if g.request(parse(f"+795831000{i:02d}"), who(i), "o", T + i)[1] is None:
            sent += 1
    assert sent == 2 and g.spent_in_hour(T + 10) == Decimal("1.5044")
    _, r = g.request(parse("+79583100099"), who(50), "o", T + 20)
    assert r is not None and r.layer == "budget"
    assert g.request(parse("+79583100098"), who(51), "o", T + HOUR + 2)[1] is None


# codes


def test_a_code_takes_three_tries_and_belongs_to_its_session() -> None:
    g = only()
    n = parse("+998901234567")
    code, _ = g.request(n, who(), "me", T)
    assert code is not None
    assert g.verify(n, code, "someone else", T + 1) == "expired"
    wrong = f"{(int(code) + 1) % 10**6:06d}"
    assert g.verify(n, wrong, "me", T + 2) == "wrong"
    assert g.verify(n, wrong, "me", T + 3) == "wrong"
    assert g.verify(n, wrong, "me", T + 4) == "expired"
    assert g.verify(n, code, "me", T + 5) == "expired"  # spent by the tries


def test_a_code_lives_five_minutes_and_is_spent_once() -> None:
    g = only()
    n = parse("+998901234567")
    code, _ = g.request(n, who(), "me", T)
    assert code is not None
    assert g.verify(n, code, "me", T + 301) == "expired"
    code, _ = g.request(n, who(), "me", T + 400)
    assert code is not None
    assert g.verify(n, code, "me", T + 401) == "ok"
    assert g.verify(n, code, "me", T + 402) == "expired"


def test_prune_forgets_what_no_layer_looks_at() -> None:
    g = Guard(Config(layers=frozenset(LAYERS)))
    pump(g, T, 5)
    g.prune(T + 2 * 86400)
    assert g.hours(T + 2 * 86400) == [] and g.spent_in_hour(T + 2 * 86400) == 0
    assert g.provider.count == 3  # the range of 100 let three through


def test_prune_runs_by_itself_every_5000_sends() -> None:
    g = only()
    for i in range(5001):
        g.request(parse(f"+7958{i:07d}"), who(i), "o", T + i)
    assert g._since_prune == 1


# layer 3


def test_proof_of_work_is_bound_signed_fresh_and_spent_once() -> None:
    pow_ = ProofOfWork(b"k" * 32, 8)
    c = pow_.issue("me", T)
    n = solve(c, 8)
    assert zero_bits(hashlib.sha256(f"{c}.{n}".encode()).digest()) >= 8
    assert not pow_.verify(c, n, "someone else", T + 1)
    assert not pow_.verify(c, "", "me", T + 1)
    assert not pow_.verify(c, n, "me", T + 700)  # stale
    assert pow_.verify(c, n, "me", T + 1)
    assert not pow_.verify(c, n, "me", T + 2)  # spent
    forged = c.rsplit(".", 1)[0] + "." + "0" * 24
    assert not pow_.verify(forged, solve(forged, 8), "me", T + 3)
    easier = ProofOfWork(b"k" * 32, 4).issue("me", T)
    assert not pow_.verify(easier, solve(easier, 4), "me", T + 3)  # another key and fewer bits
    assert not pow_.verify("a.b.c", "1", "me", T)
    assert not pow_.verify("x.0123456789abcdef.8.y", "1", "me", T)


def test_proof_of_work_refuses_a_wrong_nonce() -> None:
    pow_ = ProofOfWork(b"k" * 32, 16)
    c = pow_.issue("me", T)
    good = int(solve(c, 16))
    bad = str(good - 1 if good else 1)  # solve() gives the first solution: the one before is none
    assert not pow_.verify(c, bad, "me", T + 1)
    assert pow_.verify(c, str(good), "me", T + 1)


def test_zero_bits_counts_whole_bytes_and_a_partial_one() -> None:
    assert zero_bits(b"\x00\x00\x01") == 23
    assert zero_bits(b"\x00\x80") == 8
    assert zero_bits(b"\x00\x00") == 16
