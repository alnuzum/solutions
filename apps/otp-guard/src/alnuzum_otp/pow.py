"""Layer 3, a live browser before the code is sent: a proof of work the page computes.

The page gets a challenge signed for its session and finds a number whose SHA-256 with the challenge starts
with ``bits`` zero bits (about 2^bits hashes). A challenge lives ten minutes and is spent once. A request
without the work (a script that posts the form straight away) is refused.

It stands in for Cloudflare Turnstile, which needs a Cloudflare account for a real key: Turnstile also looks
at the browser itself, proof of work only makes every attempt cost CPU time. A real browser, headless too,
solves it, so this layer slows a browser farm down but does not stop it.
"""

import hashlib
import hmac
import secrets
from collections import OrderedDict

TTL = 600.0
MAX_USED = 200_000


def zero_bits(digest: bytes) -> int:
    n = 0
    for byte in digest:
        if byte == 0:
            n += 8
            continue
        return n + 8 - byte.bit_length()
    return n


def solve(challenge: str, bits: int) -> str:
    """What the page does, for tests and the attack script."""
    n = 0
    while zero_bits(hashlib.sha256(f"{challenge}.{n}".encode()).digest()) < bits:
        n += 1
    return str(n)


class ProofOfWork:
    def __init__(self, key: bytes, bits: int, ttl: float = TTL) -> None:
        self._key = hmac.new(key, b"otp-pow", hashlib.sha256).digest()
        self.bits = bits
        self.ttl = ttl
        self._used: OrderedDict[str, float] = OrderedDict()

    def _mac(self, body: str, owner: str) -> str:
        return hmac.new(self._key, f"{body}|{owner}".encode(), hashlib.sha256).hexdigest()[:24]

    def issue(self, owner: str, now: float) -> str:
        body = f"{int(now)}.{secrets.token_hex(8)}.{self.bits}"
        return f"{body}.{self._mac(body, owner)}"

    def verify(self, challenge: str, nonce: str, owner: str, now: float) -> bool:
        parts = (challenge or "").split(".")
        if len(parts) != 4 or not (nonce or "").isdigit() or len(nonce) > 12:
            return False
        stamp, rand, bits, mac = parts
        if not (stamp.isdigit() and bits.isdigit() and len(rand) == 16):
            return False
        body = f"{stamp}.{rand}.{bits}"
        if not hmac.compare_digest(mac.encode(), self._mac(body, owner).encode()):
            return False
        if int(bits) < self.bits or not (now - self.ttl <= int(stamp) <= now + 60):
            return False
        self._forget(now)
        if rand in self._used:
            return False
        if zero_bits(hashlib.sha256(f"{challenge}.{nonce}".encode()).digest()) < int(bits):
            return False
        self._used[rand] = now
        return True

    def _forget(self, now: float) -> None:
        while self._used and (
            len(self._used) > MAX_USED or next(iter(self._used.values())) < now - self.ttl - 60
        ):
            self._used.popitem(last=False)
