# Al-Nuzum Solutions

[Русская версия](README.ru.md)

Open-source solutions for CRM integrators by [Al-Nuzum](https://alnuzum.com). Each solution connects to
the CRM a company already runs — amoCRM, Kommo, Bitrix24, Zoho CRM or Odoo — and works on live data.

This monorepo holds what every solution shares:

| Path | What |
|---|---|
| `packages/crm` | `alnuzum_crm`: one async interface over five CRMs (companies, contacts, deals with product lines, files, notes, tasks, product catalog, deal history), with rate limits, retries and token refresh |
| `packages/platform` | `alnuzum_platform`: CRM connections stored encrypted, token refresh without races, SSRF guard for user-supplied CRM addresses, HTTP API |
| `apps/` | the solutions themselves (coming next) |

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

The amoCRM and Kommo test accounts are technical accounts: 100 contacts and companies in all, and the
API deletes none (405). Their live run works on a pool, «E2E Co pool», «E2E Person pool» and «E2E Lone
pool» with fixed INN, phone and e-mail: it finds them and creates them only when they are gone; its deal
is closed and tagged `e2e`. First the run counts the contacts and companies (`amocrm: contacts and
companies N/100` in the log) and stops at 90 with `account near the technical limit`. Clean up in the
browser: in the contact and company lists of the CRM select the «E2E …» records except the three of the
pool and delete them (the integration token gets 403 there); the CRM recounts its limit within about
15 minutes. `E2E_CREATE=1` runs the creating path instead (three new records); `RECORD=1` rewrites
`amocrm.json` and `kommo.json` with it and `amocrm-pool.json` and `kommo-pool.json` on the pool.

Live E2E reads credentials from the environment (`AMOCRM_DOMAIN`, `AMOCRM_TOKEN`, `KOMMO_DOMAIN`, `KOMMO_TOKEN`,
`B24_WEBHOOK`, `ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET`, `ZOHO_REFRESH_TOKEN`, `ODOO_URL`, `ODOO_DB`, `ODOO_LOGIN`,
`ODOO_PASSWORD`); a CRM without them is skipped. Local Odoo:

```bash
docker compose --profile odoo up -d odoo
uv run python scripts/odoo_bootstrap.py --password "$ODOO_PASSWORD"
```

`RECORD=1` rewrites the recordings; tokens, webhook paths, hosts and e-mails are replaced by placeholders.

## Security

- CRM secrets (tokens, webhook URLs, API keys) are stored only encrypted: AES-256-GCM, one envelope per
  connection, bound to its row, so a ciphertext copied to another row does not decrypt.
- A refresh token is spent once even with many workers: a process lock plus a row lock in Postgres.
- Addresses typed in by users: a Bitrix24 webhook must be https on a cloud Bitrix24 portal; an Odoo
  address must be https on port 443 and resolve only to public addresses, checked again on every
  connection (DNS rebinding); amoCRM, Kommo and Zoho hosts come from their own domain lists.
- `/connections` needs `Authorization: Bearer $ADMIN_TOKEN`; without `ADMIN_TOKEN` it refuses everyone.

### Rotating the encryption key

1. Generate a key: `python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"`.
2. Put it first in `SECRET_KEYS`, keep the old one after it: `SECRET_KEYS=k2:<new>,k1:<old>`; restart.
   New secrets are encrypted with `k2`, old ones still decrypt with `k1`.
3. Re-encrypt everything with the new key: `docker compose exec platform alnuzum-platform rotate-keys`.
4. When it prints `re-encrypted: 0` on a second run, remove `k1` from `SECRET_KEYS` and restart.

## License

MIT, see [LICENSE](LICENSE).
