"""Which company of the CRM wrote the letter, decided here and not by how a CRM searches: the INN, the domain
of the site or of a corporate mailbox, the name without its legal form, the phone. A company whose INN differs
from the letter's is shown with that mark, never chosen without asking, and an INN that points to one company
while the sender's domain, mailbox or phone points to another is a conflict the person settles: a letter may
quote a client's INN. Social networks, messengers and site builders are no domain of a company. And the deal
a reply belongs to: the open deals of that company and contact, the title sharing most words with the subject
first."""

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
        "yandex.by",
        "yandex.ua",
        "rambler.ua",
        "inbox.lv",
        "yahoo.co.uk",
        "web.de",
        "qq.com",
        "163.com",
        "mail.kz",
        "i.ua",
        "bigmir.net",
    ]
)
# hosts where anybody has a page: a link to one in a signature says nothing about which company it is
PLATFORMS = frozenset(
    [
        "instagram.com", "facebook.com", "fb.com", "fb.me", "vk.com", "vk.ru", "ok.ru", "t.me", "telegram.me",
        "telegram.org", "wa.me", "whatsapp.com", "linktr.ee", "taplink.cc", "taplink.ws", "taplink.at",
        "sites.google.com", "google.com", "goo.gl", "tilda.ws", "tilda.cc", "wixsite.com", "wix.com",
        "youtube.com", "youtu.be", "tiktok.com", "linkedin.com", "twitter.com", "x.com", "github.io",
        "github.com", "blogspot.com", "wordpress.com", "notion.site", "carrd.co", "hh.ru", "hh.uz", "olx.uz",
        "2gis.ru", "2gis.uz", "yandex.ru", "yandex.uz", "dzen.ru", "pinterest.com", "behance.net",
        "tinyurl.com",
        "bit.ly", "clck.ru", "ucoz.ru", "narod.ru", "business.site", "mssg.me", "hipolink.me", "beacons.ai",
    ]
)  # fmt: skip
# second-level labels under which the registered domain has three labels (``romashka.com.uz``)
_SECOND = frozenset(["com", "net", "org", "co", "gov", "edu", "ac", "msk", "spb", "in", "uz", "ru"])
WEIGHT = {"inn": 100, "domain": 80, "email": 80, "name": 60, "phone": 50}
SENDER = frozenset({"domain", "email", "phone"})  # what the sender's own address, site and phone say
OTHER_INN = 30  # taken off a candidate whose INN is not the letter's; kept only when the sender points to it
REASONS = (*WEIGHT, "inn_other")
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
        site, box = registered(host(fields.get("website"))), registered(mail_domain(email))
        domain = site or box or None
        inn = digits(fields.get("inn"))
        return cls(
            fields.get("company") or None,
            inn if 9 <= len(inn) <= 14 else None,
            domain,
            email,
            fields.get("phone") or None,
        )


def _on(name: str, hosts: frozenset[str]) -> bool:
    return any(name == h or name.endswith("." + h) for h in hosts)


def registered(name: str) -> str:
    """The domain a company registers (``mail.romashka.com.uz`` → ``romashka.com.uz``); empty for what says
    nothing about a company: a public mailbox, a social network or a site builder, a bare zone."""
    labels = [x for x in name.casefold().strip(".").split(".") if x]
    if len(labels) < 2 or _on(".".join(labels), PUBLIC | PLATFORMS):
        return ""
    keep = 3 if len(labels) >= 3 and labels[-2] in _SECOND and len(labels[-1]) == 2 else 2
    return ".".join(labels[-keep:])


def reasons(hit: CompanyHit, keys: Keys) -> list[str] | None:
    """Why ``hit`` may be the sender's company, strongest first, ``inn_other`` last when its INN is not the
    letter's; None when nothing agrees."""
    their_inn = digits(hit.inn)
    out = []
    if keys.inn and their_inn == keys.inn:
        out.append("inn")
    hosts = {registered(host(w)) for w in hit.web} | {registered(mail_domain(e)) for e in hit.emails}
    if keys.domain and keys.domain in hosts:
        out.append("domain")
    if keys.email and any(same_email(keys.email, e) for e in hit.emails):
        out.append("email")
    if keys.company and same_company_name(hit.name, keys.company):
        out.append("name")
    if keys.phone and any(same_phone(keys.phone, p) for p in hit.phones):
        out.append("phone")
    if not out:
        return None
    if keys.inn and their_inn and their_inn != keys.inn:
        out.append("inn_other")
    return out


def rank(hits: list[CompanyHit], keys: Keys) -> list[dict[str, Any]]:
    """The candidates with a reason, the likeliest first: ``{"id", "name", "inn", "by", "score"}``."""
    out: list[dict[str, Any]] = []
    for hit in hits:
        why = reasons(hit, keys)
        if not why:
            continue
        agree = [w for w in why if w in WEIGHT]
        if "inn_other" in why and not SENDER & set(agree):  # a namesake with another INN
            continue
        score = max(WEIGHT[w] for w in agree) + 5 * (len(agree) - 1) - OTHER_INN * ("inn_other" in why)
        out.append(
            {"id": hit.id, "name": hit.name[:200], "inn": digits(hit.inn) or None, "by": why, "score": score}
        )
    out.sort(key=lambda c: (-c["score"], len(c["id"]), c["id"]))
    return out[:MAX_COMPANIES]


def sender(candidates: list[dict[str, Any]]) -> str | None:
    """The company the sender's own domain, mailbox or phone points to, when exactly one does (likeliest)."""
    own = [c for c in candidates if SENDER & set(c["by"])]
    if not own or (len(own) > 1 and own[1]["score"] == own[0]["score"]):
        return None
    return str(own[0]["id"])


def conflict(candidates: list[dict[str, Any]]) -> bool:
    """The letter's INN points to one company and the sender's domain, mailbox or phone to another, or to
    one whose INN is not the letter's."""
    own = sender(candidates)
    if own is None:
        return False
    mine = next(c for c in candidates if str(c["id"]) == own)
    by_inn = {str(c["id"]) for c in candidates if "inn" in c["by"]}
    return (bool(by_inn) and own not in by_inn) or (not by_inn and "inn_other" in mine["by"])


def best(candidates: list[dict[str, Any]]) -> str | None:
    """The company to take without asking: the top one, unless another is as likely or the INN and the
    sender disagree."""
    if not candidates or conflict(candidates):
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
