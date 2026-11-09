"""Texts of the pages in Russian and English. ``{name}`` placeholders are filled by the templates."""

from typing import Final

LANGS: Final = ("ru", "en")

_T: dict[str, tuple[str, str]] = {
    "crumb": ("Открытые решения", "Open solutions"),
    "github": ("Код на GitHub", "Code on GitHub"),
    "stand": ("стенд", "stand"),
    "other_lang": ("English", "Русский"),
    "title": ("Подключите свою CRM", "Connect your CRM"),
    "lead": (
        "Решения Al-Nuzum работают на данных вашей CRM. Регистрация не нужна: подключение живёт в этом "
        "браузере, видно только ему и отключается одной кнопкой. Если 30 дней не открывать эту страницу, "
        "подключение отключится само и ключи будут стёрты.",
        "Al-Nuzum solutions work on the data of your CRM. No sign-up: a connection lives in this browser, "
        "only this browser sees it, and one button turns it off. If this page is not opened for 30 days, "
        "the connection turns itself off and its keys are erased.",
    ),
    "pick": ("Ваша CRM", "Your CRM"),
    "solutions": ("Решения на этой CRM", "Solutions on this CRM"),
    "yours": ("Ваши подключения", "Your connections"),
    "none_yet": ("Пока ничего не подключено.", "Nothing connected yet."),
    "open": ("Открыть", "Open"),
    "st_active": ("Подключено", "Connected"),
    "st_reauth_required": ("Подключите заново", "Reconnect needed"),
    "st_disabled": ("Отключено", "Disconnected"),
    "how_kept": ("Как хранятся ключи", "How keys are kept"),
    "how_kept_text": (
        "Токены и ключи шифруются (AES-256-GCM) и доступны только сессии этого браузера. Мы пишем в CRM "
        "только то, что вы запускаете сами, и ведём журнал каждой записи.",
        "Tokens and keys are encrypted (AES-256-GCM) and reachable only from this browser's session. We "
        "write into the CRM only what you start yourself, and every write is logged.",
    ),
    "oauth_note": (
        "Пароль от {crm} к нам не попадает: вы входите на сайте самой CRM.",
        "Your {crm} password never reaches us: you sign in on the CRM's own site.",
    ),
    "amocrm_text": (
        "Откроется окно amoCRM. Выберите в нём свой аккаунт и нажмите «Разрешить»: amoCRM заведёт в "
        "аккаунте интеграцию «Al-Nuzum» со своими ключами.",
        "An amoCRM window opens. Pick your account there and press «Allow»: amoCRM creates an «Al-Nuzum» "
        "integration in the account with keys of its own.",
    ),
    "amocrm_hint": (
        "Если «Разрешить» не нажимается, сначала выберите аккаунт в списке.",
        "If «Allow» does nothing, pick the account in the list first.",
    ),
    "kommo_text": (
        "Откроется окно Kommo: выберите аккаунт и нажмите «Allow».",
        "A Kommo window opens: pick the account and press «Allow».",
    ),
    "kommo_hint": (
        "Пока интеграция не опубликована в маркетплейсе Kommo, подключаются только аккаунты из её списка.",
        "Until the integration is published in the Kommo marketplace, only accounts on its list can connect.",
    ),
    "zoho_text": (
        "Откроется окно Zoho: войдите, отметьте согласие и нажмите «Accept».",
        "A Zoho window opens: sign in, tick the consent and press «Accept».",
    ),
    "zoho_hint": (
        "Сейчас подключаются организации дата-центра zoho.com.",
        "Organisations of the zoho.com data centre can connect now.",
    ),
    "bitrix24_text": (
        "Вставьте адрес входящего вебхука: Битрикс24 → Разработчикам → Другое → Входящий вебхук, "
        "права «CRM (crm)» и «Торговый каталог (catalog)»: без второго решения не увидят остатки товаров. "
        "Адрес содержит ключ: мы храним его зашифрованным и больше не показываем.",
        "Paste the address of an inbound webhook: Bitrix24 → Developer resources → Other → Inbound "
        "webhook, scopes «CRM (crm)» and «Trade catalog (catalog)»: without the second, solutions do not see "
        "product stock. The address holds a key: we keep it encrypted and never show it again.",
    ),
    "bitrix24_hint": (
        "Вебхуки работают на платных тарифах Битрикс24 с подпиской «Маркетплейс».",
        "Webhooks work on paid Bitrix24 plans with the Market subscription.",
    ),
    "webhook": ("Адрес вебхука", "Webhook address"),
    "odoo_text": (
        "API-ключ: Настройки → Пользователи → ваш пользователь → «Безопасность» → «Новый API-ключ». "
        "Пароль не нужен, а ключ можно отозвать в любой момент.",
        "API key: Settings → Users → your user → «Security» → «New API key». No password needed, and the "
        "key can be revoked at any time.",
    ),
    "odoo_hint": (
        "Нужен внешний API: на Odoo Online он есть только на тарифе Custom; своя Odoo — любая с https.",
        "The external API is required: on Odoo Online only the Custom plan has it; your own Odoo works "
        "over https.",
    ),
    "odoo_url": ("Адрес Odoo", "Odoo address"),
    "odoo_db": ("База", "Database"),
    "odoo_login": ("Логин", "Login"),
    "odoo_key": ("API-ключ", "API key"),
    "connect": ("Подключить {crm}", "Connect {crm}"),
    "account": ("Аккаунт", "Account"),
    "connected_at": ("Подключено", "Connected"),
    "checked_at": ("Проверено", "Checked"),
    "state": ("Состояние", "Status"),
    "never": ("ещё нет", "not yet"),
    "check": ("Проверить подключение", "Check the connection"),
    "check_text": (
        "Создаст в CRM тестовую сделку с тегом e2e и даст ссылку на неё. Не больше {quota} записей в CRM "
        "за сутки.",
        "Creates a test deal tagged e2e in the CRM and links to it. At most {quota} writes into the CRM "
        "per 24 hours.",
    ),
    "deal_no": ("Сделка № {id}", "Deal #{id}"),
    "open_in": ("Открыть в {crm}", "Open in {crm}"),
    "last_check": ("Последняя проверка", "Last check"),
    "journal": ("Журнал записей в CRM", "Writes into the CRM"),
    "journal_empty": ("Записей ещё не было.", "No writes yet."),
    "w_ok": ("тестовая сделка № {id}", "test deal #{id}"),
    "w_fail": ("не записано: {why}", "not written: {why}"),
    "w_pending": ("пишется…", "writing…"),
    "disconnect": ("Отключить", "Disconnect"),
    "disconnect_text": (
        "Ключи будут стёрты у нас. Саму интеграцию в CRM можно удалить там: {where}.",
        "The keys are erased on our side. The integration itself can be removed in the CRM: {where}.",
    ),
    "disconnect_yes": ("Да, отключить", "Yes, disconnect"),
    "where_amocrm": ("amoМаркет → Установленные → Al-Nuzum", "amoMarket → Installed → Al-Nuzum"),
    "where_kommo": ("Settings → Integrations → Al-Nuzum", "Settings → Integrations → Al-Nuzum"),
    "where_bitrix24": (
        "Разработчикам → Интеграции → ваш вебхук",
        "Developer resources → Integrations → your webhook",
    ),
    "where_zoho": (
        "токен мы отзываем сами, список — accounts.zoho.com → Connected Apps",
        "we revoke the token ourselves; the list is at accounts.zoho.com → Connected Apps",
    ),
    "where_odoo": ("Настройки → Пользователи → API-ключи", "Settings → Users → API keys"),
    "back": ("Все подключения", "All connections"),
    "reconnect": ("Подключить снова", "Connect again"),
    "why_auth": (
        "CRM не приняла ключи: похоже, доступ отозван. Подключите заново.",
        "The CRM refused the keys: access seems to be revoked. Connect again.",
    ),
    "why_crm_uninstalled": (
        "Интеграцию удалили в самой CRM, подключение отключено.",
        "The integration was removed in the CRM itself, the connection is off.",
    ),
    "why_replaced": (
        "Этот аккаунт подключён заново, действует новое подключение.",
        "This account was connected again; the new connection is in use.",
    ),
    "why_quota": (
        "Суточная квота исчерпана: не больше {quota} записей в CRM за 24 часа.",
        "The daily quota is used up: at most {quota} writes into the CRM per 24 hours.",
    ),
    "why_unavailable": (
        "CRM сейчас не отвечает. Попробуйте позже.",
        "The CRM does not answer right now. Try again later.",
    ),
    "why_rejected": (
        "CRM отказала в записи ({code}).",
        "The CRM refused the write ({code}).",
    ),
    "e_401_t": ("Сессия не найдена", "Session not found"),
    "e_401": (
        "Подключения живут в браузере, где их создали. Откройте страницу там или подключите CRM заново.",
        "Connections live in the browser that created them. Open the page there or connect the CRM again.",
    ),
    "e_403_t": ("Форма устарела", "The form has expired"),
    "e_403": ("Обновите страницу и повторите.", "Reload the page and try again."),
    "e_404_t": ("Не найдено", "Not found"),
    "e_404": ("Такой страницы или подключения нет.", "There is no such page or connection."),
    "e_state_t": ("Подключение не завершено", "The connection was not finished"),
    "e_state": (
        "Ссылка подключения устарела или открыта в другом браузере. Начните подключение заново.",
        "The connection link has expired or was opened in another browser. Start connecting again.",
    ),
    "e_denied": ("Доступ не выдан в окне CRM.", "Access was not granted in the CRM window."),
    "e_oauth": (
        "{crm} не выдала доступ. Попробуйте подключить ещё раз.",
        "{crm} did not grant access. Try connecting again.",
    ),
    "e_not_configured": (
        "Подключение {crm} на этом сервере не настроено.",
        "Connecting {crm} is not configured on this server.",
    ),
    "e_fill": ("Заполните все поля.", "Fill in every field."),
    "e_webhook": (
        "Это не адрес входящего вебхука облачного Битрикс24.",
        "This is not an inbound webhook address of a cloud Bitrix24.",
    ),
    "e_webhook_refused": (
        "Битрикс24 не принял вебхук: проверьте адрес и права «CRM (crm)».",
        "Bitrix24 refused the webhook: check the address and the «CRM (crm)» scope.",
    ),
    "e_odoo_url": (
        "Адрес Odoo должен начинаться с https:// и быть доступен из интернета.",
        "The Odoo address must start with https:// and be reachable from the internet.",
    ),
    "e_odoo_refused": (
        "Odoo не принял базу, логин или API-ключ.",
        "Odoo refused the database, login or API key.",
    ),
    "e_unreachable": (
        "Не удалось связаться с CRM. Попробуйте позже.",
        "Could not reach the CRM. Try again later.",
    ),
    "e_busy_t": ("Слишком много запросов", "Too many requests"),
    "home": ("На главную", "Home"),
}

CRM_NAMES: dict[str, tuple[str, str]] = {
    "amocrm": ("amoCRM", "amoCRM"),
    "kommo": ("Kommo", "Kommo"),
    "bitrix24": ("Битрикс24", "Bitrix24"),
    "zoho": ("Zoho CRM", "Zoho CRM"),
    "odoo": ("Odoo", "Odoo"),
}


def texts(lang: str) -> dict[str, str]:
    i = LANGS.index(lang)
    out = {key: pair[i] for key, pair in _T.items()}
    out.update({f"crm_{key}": pair[i] for key, pair in CRM_NAMES.items()})
    return out


def pick_lang(accept_language: str | None) -> str:
    """Russian for ru/uz/kk/be readers, English for everyone else."""
    for part in (accept_language or "").split(","):
        code = part.split(";")[0].strip().lower()[:2]
        if code in ("ru", "uz", "kk", "be", "ky", "tg"):
            return "ru"
        if code:
            return "en"
    return "ru"


def percent(share: float, lang: str) -> str:
    """A share (0.985) as a reader writes it: one decimal at most and no ``.0``; Russian with a decimal
    comma and a non-breaking space before the sign (98,5 %), English without the space (98.5%)."""
    shown = f"{round(float(share) * 100, 1):.1f}".removesuffix(".0")
    if lang == "ru":
        return shown.replace(".", ",") + " %"
    return shown + "%"
