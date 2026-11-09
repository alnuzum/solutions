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

## Адаптеры CRM

```python
from decimal import Decimal

from alnuzum_crm.bitrix24 import Bitrix24
from alnuzum_crm.types import CompanyIn, ContactIn, DealIn, DealLine

async with Bitrix24(webhook_url) as crm:
    company = await crm.find_or_create_company(CompanyIn("ООО Акме", inn="301234567"))
    contact = await crm.find_or_create_contact(
        ContactIn("Иван Петров", email="ivan@acme.example"), company_id=company.id
    )
    deal = await crm.create_deal(
        DealIn(
            "Заказ 42",
            (DealLine("Товар", Decimal(2), Decimal(10)),),
            company_id=company.id,
            contact_id=contact.id,
        )
    )
```

Все адаптеры (`AmoCRM`, `Kommo`, `Bitrix24`, `ZohoCRM`, `Odoo`) реализуют одни и те же методы; чем CRM
различаются (ИНН, товарные строки, файлы, задачи, удаление), написано в начале модуля каждого адаптера.
Ошибки приходят типами `CrmAuthError`, `CrmPermissionError`, `CrmNotFound`, `CrmValidationError`,
`CrmRateLimited`, `CrmUnavailable`, `CrmUnsupported`, и токенов в их тексте нет.

## Тесты

| Уровень | Команда | Когда |
|---|---|---|
| юнит и контрактные | `uv run pytest` | на каждый push; контрактные проигрывают записанный трафик последнего живого прогона (`packages/crm/tests/cassettes`) |
| живые E2E | `uv run pytest -m e2e --tb=short` | push в `main`, pull request и каждую ночь в GitHub Actions: настоящие вызовы в тестовые аккаунты amoCRM, Kommo, Битрикс24, Zoho CRM и в свежий Odoo Community |

Тестовые аккаунты amoCRM и Kommo технические: 100 контактов и компаний вместе и 100 открытых сделок, а API
записи не удаляет (405). Поэтому их живой прогон работает на пуле: «E2E Co pool», «E2E Person pool» и «E2E Lone pool» с
постоянными ИНН, телефоном и почтой. Прогон их находит и создаёт, только если их нет; его сделка
закрывается и получает тег `e2e`. Товар каталога тоже из пула, «E2E product pool»: поиск по каталогу Kommo
отстаёт от нового товара на минуты, пока аккаунт занят фоновой работой (новый ждём до пяти минут). Второй товар
прогона удаляется (API v4 отвечает там 405, API v2 удаляет). Сначала прогон считает контакты и компании (в логе `amocrm: contacts
and companies N/100`) и на 90 останавливается с `account near the technical limit`. Убирать в браузере:
в списках контактов и компаний CRM выбрать записи «E2E …», кроме трёх записей пула, и удалить (токену
интеграции там отвечают 403); лимит CRM пересчитывает минут за 15. `E2E_CREATE=1` вместо пула идёт путём
создания (три новые записи); `RECORD=1` с ним перезаписывает `amocrm.json` и `kommo.json`, на пуле
`amocrm-pool.json` и `kommo-pool.json`. Сверх 100 открытых сделок CRM отвечает «Payment Required» на любую сделку
любого решения: демо почты создаёт там новую сделку, только пока открытые сделки аккаунта (счёт через API раз в
десять минут) вместе с создаваемыми не больше 80, не больше 10 в сутки и 2 в сутки с одного адреса.

Живые E2E берут доступы из окружения (`AMOCRM_DOMAIN`, `AMOCRM_TOKEN`, `KOMMO_DOMAIN`, `KOMMO_TOKEN`,
`B24_WEBHOOK`, `ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET`, `ZOHO_REFRESH_TOKEN`, `ODOO_URL`, `ODOO_DB`, `ODOO_LOGIN`,
`ODOO_PASSWORD`); CRM без них пропускается. Тестовый Zoho это бесплатная Zoho CRM Developer Edition: её
refresh token выдаётся с выбором организации разработчика в окне согласия и отвечает только на
`https://developer.zohoapis.com` (`ZOHO_API_DOMAIN`, по умолчанию; сервер токенов при этом называет
`www.zohoapis.com`). Демо Zoho на той же редакции (`DEMO_ZOHO_API_DOMAIN`). Odoo локально:

```bash
docker compose --profile odoo up -d odoo
uv run python scripts/odoo_bootstrap.py --password "$ODOO_PASSWORD"
```

`RECORD=1` перезаписывает записи трафика; токены, пути вебхуков, хосты и почты заменяются заглушками.

## Безопасность

- Секреты CRM (токены, адреса вебхуков, API-ключи) хранятся только зашифрованными: AES-256-GCM, один
  конверт на подключение, привязанный к своей строке: шифртекст, перенесённый в другую строку, не расшифруется.
- Refresh token тратится один раз даже при многих воркерах: блокировка в процессе и блокировка строки в Postgres.
- Адреса, которые вводит пользователь: вебхук Битрикс24 только https на облачном портале Битрикс24; адрес
  Odoo только https на порту 443 и только с публичными адресами после резолва, с повторной проверкой на
  каждом соединении (DNS rebinding); хосты amoCRM, Kommo и Zoho сверяются со списками их доменов.
- `/connections` требует `Authorization: Bearer $ADMIN_TOKEN`; без `ADMIN_TOKEN` не пускает никого.

### Ротация ключа шифрования

1. Создать ключ: `python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"`.
2. Поставить его первым в `SECRET_KEYS`, старый оставить за ним: `SECRET_KEYS=k2:<новый>,k1:<старый>`; перезапустить.
   Новые секреты шифруются `k2`, старые по-прежнему расшифровываются `k1`.
3. Перешифровать всё новым ключом: `docker compose exec platform alnuzum-platform rotate-keys`.
4. Когда повторный запуск печатает `re-encrypted: 0`, убрать `k1` из `SECRET_KEYS` и перезапустить.

## Лицензия

MIT, см. [LICENSE](LICENSE).
