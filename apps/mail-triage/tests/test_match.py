"""Which company of the CRM wrote the letter, decided by our own comparison, and the routing rules."""

import pytest
from alnuzum_mail.match import Keys, best, conflict, deals, rank, reasons, registered, sender
from alnuzum_mail.rules import Rule, RuleError, load, parse, route

from alnuzum_crm.types import CompanyHit, Deal, DealStatus

ROMASHKA = CompanyHit("1", "Ромашка", "305123456", ("+998 71 200 11 01",), ("info@romashka-demo.uz",),
                      ("https://www.romashka-demo.uz/",))  # fmt: skip
TWIN = CompanyHit("3", "ООО «Ромашка»", "399999999", ("+998 71 200 11 01",), (), ())
NAMESAKE = CompanyHit("4", "Romashka LLC", None, (), (), ())
STRANGER = CompanyHit("5", "Bahor Market", None, (), ("info@gmail.com",), ())


def keys(**kw: str) -> Keys:
    return Keys.of(kw)


def test_the_inn_decides_and_another_inn_is_never_the_company() -> None:
    k = keys(
        company="ООО «Ромашка»", inn="305 123 456", email="a.karimov@romashka-demo.uz", phone="+998712001101"
    )
    assert reasons(ROMASHKA, k) == ["inn", "domain", "name", "phone"]
    assert reasons(TWIN, k) == ["name", "phone", "inn_other"]  # the same name and phone, another INN
    ranked = rank([TWIN, NAMESAKE, ROMASHKA, STRANGER], k)
    assert [c["id"] for c in ranked] == ["1", "3"] and ranked[0]["by"][0] == "inn"
    assert best(ranked) == "1" and not conflict(ranked)  # the INN and the sender agree on Ромашка


def test_a_public_mailbox_says_nothing_about_a_company() -> None:
    k = keys(email="someone@gmail.com")
    assert k.domain is None and reasons(STRANGER, k) is None
    assert keys(website="https://gmail.com", email="x@romashka-demo.uz").domain == "romashka-demo.uz"
    assert keys(inn="12345").inn is None


def test_a_tie_is_not_decided_for_the_person() -> None:
    k = keys(company="Ромашка")
    ranked = rank([ROMASHKA, NAMESAKE, CompanyHit("6", "Ромашка", None)], k)
    assert len(ranked) == 2 and best(ranked) is None
    assert best([]) is None


def test_the_mail_matches_a_company_s_address_exactly() -> None:
    k = Keys(email="info@romashka-demo.uz")
    assert reasons(ROMASHKA, k) == ["email"]


def test_deals_open_first_then_by_the_words_of_the_subject() -> None:
    found = [
        Deal("1", "Демо · Ромашка: электрощитовая", None, None, DealStatus.OPEN),
        Deal("2", "Демо · Ромашка: старый заказ", None, None, DealStatus.WON),
        Deal("3", "Ромашка: чужая сделка", None, None, DealStatus.OPEN),
        Deal("1", "Демо · Ромашка: электрощитовая", None, None, DealStatus.OPEN),
        Deal("4", "Демо · Ромашка: склад", None, None, DealStatus.OPEN),
    ]
    got = deals(found, "Re: RE: Ромашка: электрощитовая, смета", prefix="Демо · ")
    assert [d["id"] for d in got] == ["1", "4", "2"] and got[0]["open"] and not got[2]["open"]
    assert [d["id"] for d in deals(found, "")] == ["4", "1", "3", "2"]  # then by title


def test_rules_are_parsed_checked_and_the_first_match_routes() -> None:
    form = {
        "when0": "keyword", "value0": "  Щиты  учёта ", "stage0": "C0:QUOTE", "user0": "8",
        "when1": "domain", "value1": "https://www.Romashka-demo.uz/", "stage1": "", "user1": "7",
        "when2": "language", "value2": "en", "stage2": "C0:NEW",
        "when3": "keyword", "value3": "",
    }  # fmt: skip
    rules = parse(form, {"C0:NEW", "C0:QUOTE"}, {"7", "8"})
    assert rules == [
        Rule("keyword", "щиты учёта", "C0:QUOTE", "8"),
        Rule("domain", "romashka-demo.uz", None, "7"),
        Rule("language", "en", "C0:NEW", None),
    ]
    assert load({"rules": [r.to_json() for r in rules] + [{"when": "x"}, "junk"]}) == rules
    assert load(None) == [] and load([1]) == []
    hit = route(rules, subject="Запрос", body="нужны ЩИТЫ УЧЁТА", email=None, site=None, language="ru")
    assert (hit.rule, hit.stage, hit.user) == (1, "C0:QUOTE", "8")
    by_domain = route(rules, subject="", body="", email="a@sub.romashka-demo.uz", site=None, language="ru")
    assert by_domain.rule == 2
    assert route(rules, subject="", body="", email=None, site=None, language="en").rule == 3
    assert route(rules, subject="", body="", email=None, site=None, language="uz").rule is None


@pytest.mark.parametrize(
    ("form", "code"),
    [
        ({"when0": "regex", "value0": "x"}, "e_rule_kind"),
        ({"when0": "domain", "value0": "localhost"}, "e_rule_domain"),
        ({"when0": "language", "value0": "de"}, "e_rule_language"),
        ({"when0": "keyword", "value0": "x", "stage0": "C9:NOPE"}, "e_rule_target"),
        ({"when0": "keyword", "value0": "x", "user0": "99"}, "e_rule_target"),
    ],
)
def test_a_bad_rule_is_refused_with_its_row(form: dict[str, str], code: str) -> None:
    with pytest.raises(RuleError) as caught:
        parse(form, {"C0:NEW"}, {"7"})
    assert (caught.value.code, caught.value.n) == (code, 1)


def test_an_inn_of_another_company_never_hides_the_sender_s_own() -> None:
    """A letter from Ромашка's address and site quoting the INN of a big client of theirs."""
    big = CompanyHit("2", "Крупный клиент", "309999999", (), (), ())
    k = keys(inn="309999999", email="buh@romashka-demo.uz", website="romashka-demo.uz")
    ranked = rank([ROMASHKA, big], k)
    assert {c["id"]: c["by"] for c in ranked} == {"2": ["inn"], "1": ["domain", "inn_other"]}
    assert conflict(ranked) and best(ranked) is None and sender(ranked) == "1"
    # an INN the CRM does not know, the sender's own company keeping another one
    alone = rank([ROMASHKA], keys(inn="312345678", email="buh@romashka-demo.uz"))
    assert alone[0]["by"] == ["domain", "inn_other"] and conflict(alone) and sender(alone) == "1"
    # a namesake with another INN and nothing of the sender is no candidate at all
    assert rank([TWIN], keys(company="Ромашка", inn="305123456")) == []


@pytest.mark.parametrize(
    "site",
    ["https://instagram.com/new_client", "t.me/romashka", "wa.me/998901001101", "romashka.taplink.ws",
     "sites.google.com/view/romashka", "linktr.ee/romashka", "uz", "romashka.tilda.ws"],
)  # fmt: skip
def test_a_social_network_or_a_site_builder_is_no_domain(site: str) -> None:
    shop = CompanyHit("9", "Чужой магазин", None, (), (), ("https://instagram.com/chuzhoy_shop", "uz"))
    k = keys(website=site, email="client@gmail.com")
    assert k.domain is None and rank([shop], k) == []


def test_domains_compare_by_what_is_registered() -> None:
    assert registered("mail.romashka-demo.uz") == "romashka-demo.uz"
    assert registered("shop.romashka.com.uz") == "romashka.com.uz"
    assert registered("uz") == registered("yandex.by") == registered("m.vk.com") == ""
    sub = CompanyHit("1", "Ромашка", None, (), (), ("https://shop.romashka-demo.uz",))
    assert reasons(sub, keys(email="a@mail.romashka-demo.uz")) == ["domain"]
    assert reasons(sub, keys(email="a@romashka-demo.uz.evil.com")) is None


def test_a_stage_is_what_the_crm_listed_whatever_its_name() -> None:
    """Zoho names its stages: spaces, slashes, Cyrillic; Bitrix24 and amoCRM give codes and numbers."""
    stages = {"Value Proposition", "Квалификация", "Proposal/Price Quote", "C3:NEW", "142"}
    form = {f"when{i}": "keyword" for i in range(5)} | {f"value{i}": f"слово{i}" for i in range(5)}
    form |= {f"stage{i}": s for i, s in enumerate(sorted(stages))}
    assert sorted(r.stage or "" for r in parse(form, stages, set())) == sorted(stages)
    with pytest.raises(RuleError):
        parse({"when0": "keyword", "value0": "x", "stage0": "Closed Won"}, stages, set())
