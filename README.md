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

## License

MIT, see [LICENSE](LICENSE).
