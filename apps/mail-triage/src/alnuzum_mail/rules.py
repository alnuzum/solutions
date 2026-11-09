"""The company's own routing: a list of rules, the first that matches the letter decides the stage of the new deal
and who leads it. A rule is «a word in the subject or the text», «the sender's domain» or «the language of the
letter»; a letter no rule matches starts at the CRM's first stage, led by the owner of the connection.
"""

import re
from dataclasses import dataclass
from typing import Any

from alnuzum_crm.base import host, mail_domain

MAX_RULES = 10
KINDS = ("keyword", "domain", "language")
LANGUAGES = ("ru", "en", "uz", "other")
_ID = re.compile(r"[A-Za-z0-9_:]{1,50}")


@dataclass(frozen=True)
class Rule:
    when: str  # keyword | domain | language
    value: str
    stage: str | None = None
    user: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {"when": self.when, "value": self.value, "stage": self.stage, "user": self.user}


@dataclass(frozen=True)
class Route:
    rule: int | None  # its number, from 1; None: no rule matched
    stage: str | None
    user: str | None


class RuleError(ValueError):
    def __init__(self, code: str, n: int) -> None:
        super().__init__(code)
        self.code, self.n = code, n


def parse(form: dict[str, str], stages: set[str], users: set[str]) -> list[Rule]:
    """Rows of the rules form (``when0``, ``value0``, ``stage0``, ``user0``…): empty rows are skipped; a stage or a
    user the CRM does not have is refused with the row's number."""
    out = []
    for i in range(MAX_RULES):
        when = form.get(f"when{i}", "")
        value = " ".join(form.get(f"value{i}", "").split())[:80]
        if not value:
            continue
        if when not in KINDS:
            raise RuleError("e_rule_kind", i + 1)
        if when == "domain":
            value = host(value)
            if "." not in value:
                raise RuleError("e_rule_domain", i + 1)
        if when == "language" and value not in LANGUAGES:
            raise RuleError("e_rule_language", i + 1)
        stage = form.get(f"stage{i}") or None
        user = form.get(f"user{i}") or None
        if (stage and (stage not in stages or not _ID.fullmatch(stage))) or (user and user not in users):
            raise RuleError("e_rule_target", i + 1)
        out.append(Rule(when, value.casefold() if when != "language" else value, stage, user))
    return out


def load(data: Any) -> list[Rule]:
    rules = []
    for raw in (data or {}).get("rules", [])[:MAX_RULES] if isinstance(data, dict) else []:
        if isinstance(raw, dict) and raw.get("when") in KINDS and raw.get("value"):
            rules.append(Rule(str(raw["when"]), str(raw["value"]), raw.get("stage") or None, raw.get("user") or None))
    return rules


def route(rules: list[Rule], *, subject: str, body: str, email: str | None, site: str | None, language: str) -> Route:
    text = f"{subject}\n{body}".casefold()
    domains = {d for d in (mail_domain(email), host(site)) if d}
    for n, rule in enumerate(rules, start=1):
        if (
            (rule.when == "keyword" and rule.value in text)
            or (rule.when == "domain" and any(d == rule.value or d.endswith("." + rule.value) for d in domains))
            or (rule.when == "language" and rule.value == language)
        ):
            return Route(n, rule.stage, rule.user)
    return Route(None, None, None)
