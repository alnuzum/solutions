"""Which company of the CRM wrote the letter, decided here and not by how a CRM searches: the INN, the domain
of the site or of a corporate mailbox, the name without its legal form, the phone. A company whose INN differs
from the letter's is never the one, whatever else agrees. And the deal a reply belongs to: the open deals of
that company and contact, the one whose title shares most words with the subject first."""

import re
from dataclasses import dataclass
from typing import Any

from alnuzum_crm.base import digits, host, mail_domain, same_company_name, same_email, same_phone
from alnuzum_crm.types import CompanyHit, Deal, DealStatus

# mailboxes anybody can have: their domain says nothing about a company
PUBLIC = frozenset(
    [
        "gmail.com",
        "googlemail.com",
        "mail.ru",
        "bk.ru",
        "inbox.ru",
        "list.ru",
        "internet.ru",
        "yandex.ru",
        "yandex.com",
        "yandex.uz",
        "yandex.kz",
        "ya.ru",
        "rambler.ru",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "msn.com",
        "yahoo.com",
        "icloud.com",
        "me.com",
        "mac.com",
        "aol.com",
        "proton.me",
        "protonmail.com",
        "gmx.com",
        "gmx.de",
        "gmx.net",
        "mail.com",
        "zoho.com",
        "zohomail.com",
        "umail.uz",
        "mail.uz",
        "inbox.uz",
        "ukr.net",
        "tut.by",
        "qip.ru",
    ]
)
WEIGHT = {"inn": 100, "domain": 80, "email": 80, "name": 60, "phone": 50}
REASONS = tuple(WEIGHT)
MAX_COMPANIES = 6
MAX_DEALS = 8
_WORD = re.compile(r"[\w-]{3,}", re.UNICODE)
_PREFIX = re.compile(r"^\s*(?:(?:re|fwd?|aw|sv|ответ|отв|пересл)\s*(?:\[\d+\])?\s*:\s*)+", re.IGNORECASE)


@dataclass(frozen=True)
class Keys:
    """What the letter says about its sender, cleaned for comparing."""

    company: str | None = None
    inn: str | None = None
    domain: str | None = None  # a corporate one: of the site, else of the mailbox
    email: str | None = None
    phone: str | None = None

    @classmethod
    def of(cls, fields: dict[str, str]) -> "Keys":
        email = (fields.get("email") or "").casefold() or None
        site = host(fields.get("website"))
        box = mail_domain(email)
        domain = site if site and site not in PUBLIC else (box if box and box not in PUBLIC else None)
        inn = digits(fields.get("inn"))
        return cls(
            fields.get("company") or None,
            inn if 9 <= len(inn) <= 14 else None,
            domain,
            email,
            fields.get("phone") or None,
        )


def _same_domain(a: str, b: str) -> bool:
    return bool(a and b) and (a == b or a.endswith("." + b) or b.endswith("." + a))


def reasons(hit: CompanyHit, keys: Keys) -> list[str] | None:
    """Why ``hit`` is the sender's company, strongest first; None when its INN says it is another."""
    their_inn = digits(hit.inn)
    if keys.inn and their_inn and their_inn != keys.inn:
        return None
    out = []
    if keys.inn and their_inn == keys.inn:
        out.append("inn")
    hosts = {host(w) for w in hit.web} | {mail_domain(e) for e in hit.emails}
    if keys.domain and any(_same_domain(keys.domain, h) for h in hosts if h and h not in PUBLIC):
        out.append("domain")
    if keys.email and any(same_email(keys.email, e) for e in hit.emails):
        out.append("email")
    if keys.company and same_company_name(hit.name, keys.company):
        out.append("name")
    if keys.phone and any(same_phone(keys.phone, p) for p in hit.phones):
        out.append("phone")
    return out


def rank(hits: list[CompanyHit], keys: Keys) -> list[dict[str, Any]]:
    """The candidates with a reason, the likeliest first: ``{"id", "name", "inn", "by", "score"}``."""
    out: list[dict[str, Any]] = []
    for hit in hits:
        why = reasons(hit, keys)
        if not why:
            continue
        score = max(WEIGHT[w] for w in why) + 5 * (len(why) - 1)
        out.append(
            {"id": hit.id, "name": hit.name[:200], "inn": digits(hit.inn) or None, "by": why, "score": score}
        )
    out.sort(key=lambda c: (-c["score"], len(c["id"]), c["id"]))
    return out[:MAX_COMPANIES]


def best(candidates: list[dict[str, Any]]) -> str | None:
    """The company to take without asking: the top one, unless another is as likely."""
    if not candidates:
        return None
    if len(candidates) > 1 and candidates[1]["score"] == candidates[0]["score"]:
        return None
    return str(candidates[0]["id"])


def subject_words(text: str) -> set[str]:
    return {w.casefold() for w in _WORD.findall(_PREFIX.sub("", text or ""))}


def deals(found: list[Deal], subject: str, *, prefix: str | None = None) -> list[dict[str, Any]]:
    """Open deals first, then by the words their title shares with the subject; ``prefix``: only titles
    that start with it (the demo writes only into its own deals)."""
    words = subject_words(subject)
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for d in found:
        if d.id in seen or (prefix and not d.title.startswith(prefix)):
            continue
        seen.add(d.id)
        shared = len(words & subject_words(d.title))
        out.append(
            {"id": d.id, "title": d.title[:200], "open": d.status is DealStatus.OPEN, "shared": shared}
        )
    out.sort(key=lambda d: (not d["open"], -d["shared"], d["title"]))
    return out[:MAX_DEALS]
