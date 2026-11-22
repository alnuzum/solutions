"""Phone numbers: E.164 parsing, the destination's country and the price of one SMS there.

Prices of one SMS, in US dollars, read on 22.11.2026:

* Uzbekistan, a local provider: Eskiz, 95 UZS for one SMS to any Uzbek operator (https://eskiz.uz/sms),
  at the Central Bank's rate of 12 004,48 UZS for a dollar on 21.11.2026 (https://cbu.uz).
* Other countries, the destinations SMS pumping goes for: Twilio's public price of an outbound SMS
  (https://www.twilio.com/en-us/sms/pricing/<country code>, e.g. ``/ru``).

A destination outside this table is refused as unknown: the stand has no price for it.
"""

import re
from dataclasses import dataclass
from decimal import Decimal

UZS_PER_USD = Decimal("12004.48")


@dataclass(frozen=True)
class Country:
    code: str  # the calling code, without +
    iso: str
    usd: Decimal  # one SMS
    digits: tuple[int, int]  # length of the whole number with the calling code, min and max
    source: str


_ESKIZ = "https://eskiz.uz/sms"


def _twilio(iso: str) -> str:
    return f"https://www.twilio.com/en-us/sms/pricing/{iso.lower()}"


COUNTRIES: dict[str, Country] = {
    c.code: c
    for c in (
        Country("998", "UZ", (Decimal(95) / UZS_PER_USD).quantize(Decimal("0.0001")), (12, 12), _ESKIZ),
        Country("7", "RU", Decimal("0.7522"), (11, 11), _twilio("RU")),
        # +7 6xx and +7 7xx are Kazakhstan
        Country("76", "KZ", Decimal("0.3382"), (11, 11), _twilio("KZ")),
        Country("77", "KZ", Decimal("0.3382"), (11, 11), _twilio("KZ")),
        Country("996", "KG", Decimal("0.4588"), (12, 12), _twilio("KG")),
        Country("992", "TJ", Decimal("0.4505"), (12, 12), _twilio("TJ")),
        Country("994", "AZ", Decimal("0.4110"), (12, 12), _twilio("AZ")),
        Country("62", "ID", Decimal("0.4414"), (10, 14), _twilio("ID")),
        Country("44", "GB", Decimal("0.0560"), (12, 12), _twilio("GB")),
    )
}
_LONGEST = sorted(COUNTRIES, key=len, reverse=True)
_JUNK = re.compile(r"[\s()\-.]")


class BadNumber(ValueError):
    """Not a number of a country the stand knows."""


@dataclass(frozen=True)
class Number:
    e164: str  # digits only, with the calling code
    country: Country

    @property
    def prefix(self) -> str:
        """The calling code and the first three digits after it (an operator's code and the first digit of
        its range): the unit the share of confirmed codes is counted and closed by, narrow enough that closing
        a pumped range leaves the operator's other subscribers alone."""
        cc = self.country.code[:1] if self.country.iso in ("RU", "KZ") else self.country.code
        return f"+{cc} {self.e164[len(cc) : len(cc) + 3]}"

    @property
    def block(self) -> str:
        """A range of 100 numbers: pumping walks numbers in a row."""
        return self.e164[:-2]

    def masked(self) -> str:
        return f"{self.prefix} ***{self.e164[-2:]}"

    @property
    def plus(self) -> str:
        return "+" + self.e164


def parse(raw: str) -> Number:
    text = _JUNK.sub("", raw or "")
    if text.startswith("00"):
        text = "+" + text[2:]
    if not text.startswith("+") or not text[1:].isdigit() or not text[1:].isascii():
        raise BadNumber("format")
    digits = text[1:]
    for code in _LONGEST:
        if digits.startswith(code):
            country = COUNTRIES[code]
            low, high = country.digits
            if not low <= len(digits) <= high:
                raise BadNumber("length")
            return Number(digits, country)
    raise BadNumber("country")
