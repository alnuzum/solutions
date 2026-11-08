# Al-Nuzum Solutions

[English version](README.md)

Открытые решения для интеграторов CRM от [Al-Nuzum](https://alnuzum.com). Каждое решение подключается к CRM,
в которой компания уже работает (amoCRM, Kommo, Битрикс24, Zoho CRM или Odoo), и работает на живых данных.

В этом монорепозитории общее для всех решений:

| Путь | Что |
|---|---|
| `packages/crm` | `alnuzum_crm`: один асинхронный интерфейс к пяти CRM (компании, контакты, сделки с товарными строками, файлы, заметки, задачи, каталог товаров, история сделок) с лимитами, повторами и обновлением токенов |
| `packages/platform` | `alnuzum_platform`: подключения к CRM хранятся зашифрованными, токены обновляются без гонок, адреса CRM от пользователя проходят защиту от SSRF, HTTP API |
| `apps/` | сами решения (следующий этап) |

## Быстрый старт

Нужны Docker, [uv](https://docs.astral.sh/uv/), Python 3.12.

```bash
cp .env.example .env            # заполнить SECRET_KEYS и ADMIN_TOKEN, см. ниже
docker compose up -d --build
curl http://127.0.0.1:8040/health   # {"status":"ok"}
```

Разработка:

```bash
uv sync
docker compose up -d db
uv run pytest                   # юнит и контрактные пробы, порог покрытия 90 %
uv run ruff check . && uv run ruff format --check . && uv run mypy packages
```

## Лицензия

MIT, см. [LICENSE](LICENSE).
