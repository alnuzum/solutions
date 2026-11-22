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
