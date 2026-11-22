# Al-Nuzum Solutions

[Русская версия](README.ru.md)

Open-source solutions for CRM integrators by [Al-Nuzum](https://alnuzum.com). Each solution connects to
the CRM a company already runs — amoCRM, Kommo, Bitrix24, Zoho CRM or Odoo — and works on live data.

**Live demos, no sign-up: [apps.alnuzum.com](https://apps.alnuzum.com/en).** A demo writes into a test
account of the CRM you pick and reads the result back from it; your own CRM connects on the same page.

## Solutions

| Solution | What it does | Demo | Reference set, measured | Code |
|---|---|---|---|---|
| Customer order → deal with products | An order as an e-mail, a spreadsheet, a PDF or a photo: every line found in the CRM catalog, priced by the company's rules, unsure lines left to a person; one button writes a deal with products, a note on the calculation and a task | [/en/orders](https://apps.alnuzum.com/en/orders) | 6 orders in four formats, 67 lines: the right product in 67 of 67, the right quantity in 66 of 67, none wrong passed off as sure (2026-11-09) | [`apps/order-to-deal`](apps/order-to-deal) |
| Manager's voice → CRM | A voice note (on the page or to a Telegram bot) becomes a plan of changes of one deal: stage, sum, closing date, products, task; written only after the manager confirms | [/en/voice](https://apps.alnuzum.com/en/voice) | 22 commands (12 Russian, 10 Uzbek, synthesised speech): the deal taken alone and the plan exactly right in 22 of 22; word error rate 8.5 % (2026-11-09) | [`apps/voice-to-crm`](apps/voice-to-crm) |
| A contract against your standard | The counterparty's draft (DOCX, PDF, a scan or text) against the company's standard: every changed clause with its risk for your side; a party's new bank account is always high | [/en/contracts](https://apps.alnuzum.com/en/contracts) | 4 pairs, 44 changes: found 44, false 0, the level of risk matched in 42 of 44 (2026-11-22) | [`apps/contract-check`](apps/contract-check) |
| Incoming mail sorted | A letter: its kind, the sender's requisites, the company found in the CRM by INN, domain or name, then a deal or a note; instructions inside a letter are not followed | [/en/mail](https://apps.alnuzum.com/en/mail) | 31 letters in Russian, English and Uzbek: the kind right in 31 of 31, requisites 118 of 119, the company 24 of 24, 4 of 4 injections ignored (2026-11-09) | [`apps/mail-triage`](apps/mail-triage) |
| Win back lapsed clients | From the purchase history in the CRM: the clients who missed their order cycle, the reason by their orders, a task for the manager on the company | [/en/winback](https://apps.alnuzum.com/en/winback) | A rule, no AI: on the demo history the five clients are sorted as planned, 5 of 5 (2026-11-10) | [`apps/win-back`](apps/win-back) |
| SMS login without pumping | A login by SMS code behind five layers against pumping: countries, limits on the number, its range, the device, the IP and the network, a browser check, the confirmed share per prefix, an hourly spend ceiling; the SMS provider counts the price instead of sending | [/en/otp](https://apps.alnuzum.com/en/otp) | An attack of 12,000 proxied browsers for an hour: unprotected 36,000 SMS for $9,147.83, all layers 90 SMS for $5.93 (honest traffic included), 3.8 % of honest users kept out (22.11.2026) | [`apps/otp-guard`](apps/otp-guard) |

The numbers come from `python -m <package>.quality --write` against a reference set with every answer
known in advance (`samples/` of each solution); misses are shown as they are. On every push to `main` CI runs
the sets against the live model and holds them to a bar.

## Shared code

| Path | What |
|---|---|
| `packages/crm` | `alnuzum_crm`: one async interface over five CRMs (companies, contacts, deals with product lines, files, notes, tasks, product catalog, deal history), with rate limits, retries and token refresh |
| `packages/platform` | `alnuzum_platform`: CRM connections stored encrypted, token refresh without races, SSRF guard for user-supplied CRM addresses, HTTP API, the pages every solution shares |
| `packages/files` | `alnuzum_files`: files from strangers: the type decided by the content, a zip bounded before it unpacks, scans and Word documents read in a process of their own |

## Quick start

Requirements: Docker, [uv](https://docs.astral.sh/uv/), Python 3.12.

```bash
cp .env.example .env            # fill SECRET_KEYS and ADMIN_TOKEN, see below
docker compose up -d --build
curl http://127.0.0.1:8040/health   # {"status":"ok"}
```

Development:

```bash
uv sync
docker compose up -d db
uv run pytest                   # unit and contract tests, coverage gate 90 %
uv run ruff check . && uv run ruff format --check . && uv run mypy packages
```

## CRM adapters

```python
from decimal import Decimal

from alnuzum_crm.bitrix24 import Bitrix24
from alnuzum_crm.types import CompanyIn, ContactIn, DealIn, DealLine

async with Bitrix24(webhook_url) as crm:
    company = await crm.find_or_create_company(CompanyIn("Acme LLC", inn="301234567"))
    contact = await crm.find_or_create_contact(
        ContactIn("Jane Doe", email="jane@acme.example"), company_id=company.id
    )
    deal = await crm.create_deal(
        DealIn(
            "Order 42",
            (DealLine("Widget", Decimal(2), Decimal(10)),),
            company_id=company.id,
            contact_id=contact.id,
        )
    )
```

Every adapter (`AmoCRM`, `Kommo`, `Bitrix24`, `ZohoCRM`, `Odoo`) implements the same methods; where the
CRMs differ (INN, product lines, files, tasks, deletion) is written at the top of each adapter module.
Errors come as `CrmAuthError`, `CrmPermissionError`, `CrmNotFound`, `CrmValidationError`,
`CrmRateLimited`, `CrmUnavailable`, `CrmUnsupported`, never with a token in the text.

## Tests

| Level | Command | Runs |
|---|---|---|
| unit and contract | `uv run pytest` | every push; contract tests replay the recorded traffic of the last live run (`packages/crm/tests/cassettes`) |
| live E2E | `uv run pytest -m e2e --tb=short` | push to `main`, pull requests and nightly in GitHub Actions: real calls into test accounts of amoCRM, Kommo, Bitrix24, Zoho CRM and a fresh Odoo Community |

The amoCRM and Kommo test accounts are technical accounts: 100 contacts and companies in all and 100 open
deals, and the API deletes none (405). Their live run works on a pool, «E2E Co pool», «E2E Person pool» and «E2E Lone
pool» with fixed INN, phone and e-mail: it finds them and creates them only when they are gone; its deal
is closed and tagged `e2e`. The catalog product is the pool's too, «E2E product pool»: Kommo's catalog search lags
behind a new product by minutes while the account is busy with background work (a new one is waited for up to five
minutes). The other product of the run is deleted (API v4 answers 405 there, API v2 deletes). First the run counts the contacts and companies (`amocrm: contacts and
companies N/100` in the log) and stops at 90 with `account near the technical limit`. Clean up in the
browser: in the contact and company lists of the CRM select the «E2E …» records except the three of the
pool and delete them (the integration token gets 403 there); the CRM recounts its limit within about
15 minutes. `E2E_CREATE=1` runs the creating path instead (three new records); `RECORD=1` rewrites
`amocrm.json` and `kommo.json` with it and `amocrm-pool.json` and `kommo-pool.json` on the pool. Past 100 open deals the CRM answers «Payment Required» to
every deal of every solution: the demo of the mail makes a new deal there only while the account's open deals
(counted through the API every ten minutes) with what is being created stay within 80, at most 10 a day and 2 a
day from one address.

Live E2E reads credentials from the environment (`AMOCRM_DOMAIN`, `AMOCRM_TOKEN`, `KOMMO_DOMAIN`, `KOMMO_TOKEN`,
`B24_WEBHOOK`, `ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET`, `ZOHO_REFRESH_TOKEN`, `ODOO_URL`, `ODOO_DB`, `ODOO_LOGIN`,
`ODOO_PASSWORD`); a CRM without them is skipped. The test Zoho is a free Zoho CRM Developer Edition: its
refresh token is issued with the developer organisation picked on the consent page and answers only on
`https://developer.zohoapis.com` (`ZOHO_API_DOMAIN`, the default; the token endpoint names `www.zohoapis.com`
all the same). The demo Zoho is the same edition (`DEMO_ZOHO_API_DOMAIN`). Local Odoo:

```bash
docker compose --profile odoo up -d odoo
uv run python scripts/odoo_bootstrap.py --password "$ODOO_PASSWORD"
```

`RECORD=1` rewrites the recordings; tokens, webhook paths, hosts and e-mails are replaced by placeholders.

## SMS login without pumping (`apps/otp-guard`)

Stand: https://apps.alnuzum.com/en/otp. SMS pumping: a bot asks for login codes to thousands of numbers through proxies,
and you pay for every SMS. Here is a login by SMS code behind five layers, each behind a flag (`OTP_LAYERS`, all five by
default):

| layer | what it does |
|---|---|
| 1 `geo` | codes go only to the prefixes of the countries you serve (`OTP_COUNTRIES`, `998` by default) |
| 2 `limits` | counters on the number, its range of 100 neighbouring numbers, the device, the IP and the provider's network (/24, IPv6 /48); after the free sends of an hour the pause doubles with every next one |
| 3 `human` | a live browser before sending: a proof of work in the page (SHA-256, 20 bits) signed for the session, single use. In place of Cloudflare Turnstile, whose key needs a Cloudflare account. A script without a browser is cut off entirely, a real browser solves it |
| 4 `conversion` | sends and confirmations per prefix (calling code and three digits) per hour; a confirmed share over 30 minutes under half of the normal one closes the prefix for an hour and raises an alert (`WARNING otp: ALERT …` in the log, a block on the page) |
| 5 `budget` | the price of the SMS of the last hour stays under a ceiling (`OTP_USD_PER_HOUR`, $20) |

The stand's SMS provider sends nothing: it counts the messages and their price, and the code is shown on the page.
Prices of one SMS (22.11.2026): Uzbekistan, [Eskiz](https://eskiz.uz/sms), 95 UZS ($0.0079 at the Central Bank's 12,004.48 UZS
for $1 on 21.11.2026); the destinations pumping goes for, Twilio's public outbound price: [Russia](https://www.twilio.com/en-us/sms/pricing/ru)
$0.7522, [Kyrgyzstan](https://www.twilio.com/en-us/sms/pricing/kg) $0.4588, [Tajikistan](https://www.twilio.com/en-us/sms/pricing/tj)
$0.4505, [Indonesia](https://www.twilio.com/en-us/sms/pricing/id) $0.4414, [Azerbaijan](https://www.twilio.com/en-us/sms/pricing/az)
$0.4110, [Kazakhstan](https://www.twilio.com/en-us/sms/pricing/kz) $0.3382, [United Kingdom](https://www.twilio.com/en-us/sms/pricing/gb) $0.0560.

### The attack and the numbers

```bash
uv run python apps/otp-guard/attack/simulate.py                      # eight configurations, a process each
uv run --with playwright python apps/otp-guard/attack/browsers.py    # once before: uv run --with playwright playwright install chromium-headless-shell
```

`simulate.py` drives one simulated hour through the stand's HTTP pages (in process, over ASGI): 12000 browsers behind
residential proxies, each its own IP (6000 /24 subnets), its own fingerprint and session, 3 code requests
20 minutes apart on numbers in a row inside ranges (half Uzbek, half expensive foreign ones); in parallel 600 honest
users (60 % mobile carriers behind shared NAT, 40 % home broadband, 2 % with a Russian number, 12 % ask twice, 90 % enter
the code; a refused one tries again after the pause named, if it is at most 5 minutes). The generator is seeded: a run
gives the same table.

| protection | code requests | SMS sent (attack / honest) | cost of the hour, $ | first refusal | attack stopped | honest users kept out |
|---|---:|---:|---:|---:|---:|---:|
| no protection | 36663 | 36000 / 663 | 9,147.83 | — | — | 0 (0 %) |
| layer 1: countries | 36676 | 18105 / 670 | 148.32 | 00:00 | — | 6 (1 %) |
| layer 2: limits | 36663 | 1336 / 663 | 379.69 | 00:01 | — | 0 (0 %) |
| layer 3: browser check | 36663 | 36000 / 663 | 9,147.83 | — | — | 0 (0 %) |
| layer 3 against a script without a browser | 36663 | 0 / 663 | 10.45 | 00:00 | 00:00 | 0 (0 %) |
| layer 4: confirmed share | 36681 | 1967 / 661 | 514.63 | 03:10 | 04:00 | 20 (3.3 %) |
| layer 5: spend ceiling | 36601 | 194 / 1 | 39.99 | 00:07 | — | 599 (99.8 %) |
| **all five** | 36686 | 90 / 661 | 5.93 | 00:00 | 00:00 | 23 (3.8 %) |

"Attack stopped": from this minute on, every minute less than 5 % of the attack's attempts get an SMS. "Honest users kept
out" got no code at all. The run lasts a little over an hour (the attack's last attempts at minute 61), so in the row of layer 5 the $20 ceiling of the sliding hour opened a second time. The attack's browsers solve the proof of work (they are real browsers), so layer 3 alone does
not hold them; in the lab the difficulty is lowered to 8 bits to keep the run to minutes. What it costs the attacker is
measured by `browsers.py`: a real headless Chromium against the same stand with all layers at 20 bits.

Headless Chromium (playwright), 20 browsers with different fingerprints, 20 bits: all solved it, median 1509 ms, at most 3963 ms (measured on a machine under other load); 3 got codes, layer 2 (the range of 100 numbers) stopped the rest. 20 of 20 script requests without a solution were refused by layer 3. By this measure the attack's 36000 attempts of the table cost the attacker 15.1 CPU hours: minutes on a hundred machines.

**Lab mode.** Claiming an IP and a clock (`X-Lab-Ip`, `X-Lab-Time`) and resetting the stand with other layers
(`POST /lab/otp/reset`) works only when the solution is built with `lab=True` (the attack scripts) or `OTP_LAB=1`, the
environment is neither `prod` nor `stand`, and the request comes from loopback. In production the route is not there
(404) and the headers are not read; `apps/otp-guard/tests/test_web.py` checks it, and the stage report records requests
to apps.alnuzum.com.

Not on the stand: an IP → ASN database (the provider's network is the /24), Turnstile (a proof of work instead), an alert
sent to a messenger (it is in the log and on the page), a shared store of counters for several processes (the platform
runs one; with several the counters move to Redis).

## Security

- CRM secrets (tokens, webhook URLs, API keys) are stored only encrypted: AES-256-GCM under one master key
  from `SECRET_KEYS`, a fresh nonce for every value, and the row's id as associated data, so a ciphertext
  copied to another row does not decrypt. The solutions seal what they keep of a document (contract runs and
  standard contracts, mail runs, win-back scans) the same way.
- A refresh token is spent once even with many workers: a process lock plus a row lock in Postgres.
- Addresses typed in by users: a Bitrix24 webhook must be https on a cloud Bitrix24 portal; an Odoo
  address must be https on port 443 and resolve only to public addresses, checked again on every
  connection (DNS rebinding); amoCRM, Kommo and Zoho hosts come from their own domain lists.
- `/connections` needs `Authorization: Bearer $ADMIN_TOKEN`; without `ADMIN_TOKEN` it refuses everyone.

### Rotating the encryption key

1. Generate a key: `python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"`.
2. Put it first in `SECRET_KEYS`, keep the old one after it: `SECRET_KEYS=k2:<new>,k1:<old>`; recreate the
   container so it reads `.env` again: `docker compose up -d platform` (`restart` keeps the old environment).
   New secrets are encrypted with `k2`, old ones still decrypt with `k1`.
3. Re-encrypt everything with the new key: `docker compose exec platform alnuzum-platform rotate-keys`.
   It takes every sealed value: CRM secrets, the amoCRM keys of an open OAuth window and the columns each
   solution declares (`Solution.sealed`: contract runs and standards, mail runs, win-back scans).
   It exits with 2 when `SECRET_KEYS` holds one key, and with 1 when a value does not decrypt (named as
   `table:id`), a solution does not load, or any bytea column still holds a value under another key.
4. When it exits with 0 and prints `left under other keys: 0`, remove `k1` from `SECRET_KEYS` and run
   `docker compose up -d platform`. Keep `k1` offline for 14 days: the nightly dumps are kept that long
   (`deploy/README.md`), and a dump taken before the rotation opens only with it.

## License

MIT, see [LICENSE](LICENSE).
