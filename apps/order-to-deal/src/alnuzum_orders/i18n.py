"""Texts of the solution's pages, Russian and English. The word for what reads the document is
«система» / "the system"."""

_T: dict[str, tuple[str, str]] = {
    "o_title": ("Заявка клиента → сделка с товарами", "Customer order → deal with products"),
    "o_lead": (
        "Клиент прислал заявку своими словами: письмом, таблицей, PDF или фото. Система находит каждую позицию в "
        "каталоге CRM, считает цену по правилам компании, показывает остаток и аналоги, а спорное отдаёт вам. "
        "По кнопке в CRM появляется сделка с товарами, заметкой с расчётом и задачей на то, что осталось.",
        "A customer sends an order in their own words: an e-mail, a spreadsheet, a PDF or a photo. The system finds "
        "every line in the CRM catalog, prices it by the company's rules, shows stock and alternatives, and leaves "
        "the unsure lines to you. One button puts a deal with products into the CRM, with a note on the "
        "calculation and a task for what is left.",
    ),
    "o_quality": (
        "Точность на эталонном наборе: {acc} строк ({lines} строк, {orders} заявок в четырёх форматах); "
        "{mistakes}. Замер {date}.",
        "Accuracy on the reference set: {acc} of lines ({lines} lines, {orders} orders in four formats); "
        "{mistakes}. Measured {date}.",
    ),
    "o_quality_flagged": (
        "ошибок {wrong}, из них система сама пометила спорными {flagged}",
        "mistakes: {wrong}, of which the system itself marked {flagged} as unsure",
    ),
    "o_quality_clean": ("ошибок нет", "no mistakes"),
    "o_where": ("Куда записать", "Where to write"),
    "o_demo": ("Демо без регистрации", "Demo, no sign-up"),
    "o_demo_note": (
        "Пишет в демо-аккаунт Al-Nuzum выбранной CRM с каталогом электромонтажа и крепежа (198 позиций). "
        "Войти туда нельзя: сделку система прочитает обратно из CRM и покажет здесь.",
        "Writes into Al-Nuzum's demo account of the chosen CRM, with a catalog of electrical supplies and "
        "fasteners (198 items). You cannot sign in there: the system reads the deal back from the CRM and shows it here.",
    ),
    "o_own": ("Своя CRM", "Your own CRM"),
    "o_own_note": (
        "Каталог, цены и остатки — из вашей CRM, сделка — туда же.",
        "Catalog, prices and stock come from your CRM, and the deal goes there.",
    ),
    "o_connect": ("Подключить свою CRM", "Connect your CRM"),
    "o_order": ("Заявка", "The order"),
    "o_try": ("Пример заявки", "A sample order"),
    "o_mine": ("Своя заявка", "My own order"),
    "o_or_file": ("или свой файл", "or your own file"),
    "o_file_hint": (
        "PDF до 10 страниц, Excel, CSV, фото (JPEG, PNG, WebP) или текст, до 8 МБ. Файл не сохраняется: он живёт "
        "только пока система его читает. Разбор хранится сутки.",
        "PDF up to 10 pages, Excel, CSV, a photo (JPEG, PNG, WebP) or text, up to 8 MB. The file is not kept: it "
        "lives only while the system reads it. The result is kept for a day.",
    ),
    "o_or_text": ("или текст письма", "or the text of an e-mail"),
    "o_go": ("Разобрать", "Read the order"),
    "o_left": (
        "Разборов осталось: {n} (счёт за последние 24 часа)",
        "Orders left: {n} (counted over the last 24 hours)",
    ),
    "o_wait": ("Разбор занимает 5–30 секунд.", "Reading takes 5–30 seconds."),
    "s_photo": ("Фото списка", "Photo of a list"),
    "s_xlsx": ("Excel со столбцами", "Excel with columns"),
    "s_pdf": ("PDF-заявка с реквизитами", "PDF order with requisites"),
    "s_email": ("Письмо", "E-mail"),
    "s_email_en": ("Письмо на английском с инъекцией", "English e-mail with an injection"),
    "s_messy": ("Excel без шапки", "Excel without a header"),
    "o_source": ("Исходник", "Source"),
    "o_source_photo": (
        "Так система прочла фото; само фото не хранится.",
        "This is how the system read the photo; the photo itself is not kept.",
    ),
    "o_understood": ("Что поняла система", "What the system understood"),
    "o_hover": (
        "Наведите на строку — в исходнике загорится её место.",
        "Hover over a line to light its place in the source.",
    ),
    "o_as_read": ("как написано", "as written"),
    "o_qty": ("кол-во", "qty"),
    "o_stock": ("на складе", "in stock"),
    "o_short": ("не хватает: на складе {n}", "short: {n} in stock"),
    "o_rounded": ("округлено до целых", "rounded up to whole packs"),
    "o_by_article": ("по артикулу", "by article"),
    "o_by_name": ("по названию", "by name"),
    "o_by_model": ("выбрала система", "picked by the system"),
    "o_by_person": ("выбрано вами", "picked by you"),
    "o_unsure": ("Спорно — выберите", "Unsure — pick one"),
    "o_not_found": ("Нет в каталоге", "Not in the catalog"),
    "o_analogs": ("Аналоги", "Alternatives"),
    "o_skip": ("Не добавлять", "Leave out"),
    "o_qty_fix": ("Количество в ед. товара", "Quantity in the item's unit"),
    "o_problem_qty": ("Количество не указано", "No quantity"),
    "o_problem_unit": (
        "Единица не сводится к единице товара",
        "The unit does not convert to the item's unit",
    ),
    "o_customer": ("Клиент из документа", "Customer from the document"),
    "o_no_customer": ("Реквизитов в документе нет.", "The document has no requisites."),
    "o_totals": ("Расчёт", "Calculation"),
    "o_base_total": ("По ценам каталога", "At catalog prices"),
    "o_cat_total": ("По правилам категорий", "After category rules"),
    "o_volume": ("Скидка от суммы {p} % (от {f})", "Volume discount {p} % (from {f})"),
    "o_total": ("Итого", "Total"),
    "o_rules": ("Правила цены", "Price rules"),
    "o_rules_edit": ("Изменить правила", "Edit the rules"),
    "o_recalc": ("Пересчитать", "Recount"),
    "o_write": ("Записать в {crm}", "Write into {crm}"),
    "o_write_note": (
        "В CRM: компания и контакт из реквизитов, сделка с {n} поз., заметка с расчётом{task}.",
        "Into the CRM: company and contact from the requisites, a deal with {n} line(s), a note on the calculation{task}.",
    ),
    "o_task_part": (", задача «проверить {n} поз.»", ", a task to check {n} line(s)"),
    "o_demo_left": (
        "Записей в демо осталось: {n} (счёт за последние 24 часа).",
        "Demo writes left: {n} (counted over the last 24 hours).",
    ),
    "o_written": ("Записано в {crm}", "Written into {crm}"),
    "o_readback": (
        "Так сделку вернул API {crm}: строки, суммы и бюджет прочитаны из CRM после записи.",
        "This is the deal as the {crm} API returned it: lines, sums and the budget were read back after writing.",
    ),
    "o_deal": ("Сделка № {id}", "Deal #{id}"),
    "o_open_crm": ("Открыть в CRM", "Open in the CRM"),
    "o_amount": ("Бюджет сделки", "Deal amount"),
    "o_lines_crm": ("Товары в сделке", "Products in the deal"),
    "o_new": ("Новая заявка", "Another order"),
    "o_cost": ("Вызовов системы: {calls}, расход ${usd}", "System calls: {calls}, cost ${usd}"),
    "n_written": ("Сделка записана.", "The deal is written."),
    "n_quota": (
        "Лимит записей за последние 24 часа исчерпан: место освобождается через сутки после каждой записи.",
        "The write limit of the last 24 hours is reached: a place frees up 24 hours after each write.",
    ),
    "n_full": (
        "Демо {crm} заполнено: лимит сделок за последние 24 часа исчерпан.",
        "The {crm} demo is full: its deal limit of the last 24 hours is reached.",
    ),
    "n_full_total": (
        "Демо {crm} заполнено: тестовый аккаунт больше не принимает сделок.",
        "The {crm} demo is full: the test account takes no more deals.",
    ),
    "n_down": (
        "Демо {crm} временно недоступно: тестовый аккаунт CRM сейчас не принимает записи.",
        "The {crm} demo is temporarily unavailable: the CRM test account takes no records right now.",
    ),
    "n_full_try": ("Демо других CRM открыто:", "The demo of other CRMs is open:"),
    "n_full_again": ("начать заявку заново", "start the order again"),
    "o_full_mark": ("заполнено", "full"),
    "o_down_mark": ("временно недоступно", "temporarily unavailable"),
    "w_b24_catalog": (
        "Остатков не видно: у вебхука Битрикс24 нет права «Торговый каталог (catalog)». Добавьте его в "
        "настройках входящего вебхука, и остатки появятся в следующей заявке.",
        "Stock is not shown: the Bitrix24 webhook has no «Trade catalog (catalog)» scope. Add it in the "
        "inbound webhook's settings, and stock shows in the next order.",
    ),
    "n_failed": (
        "CRM не приняла запись. Попробуйте ещё раз или проверьте подключение.",
        "The CRM refused the write. Try again or check the connection.",
    ),
    "n_auth": (
        "Подключение отключено или требует входа заново.",
        "The connection is off or needs signing in again.",
    ),
    "n_empty": ("Нет ни одной выбранной позиции.", "No line is picked."),
    "n_saved": ("Правила сохранены.", "Rules saved."),
    "r_title": ("Правила цены", "Price rules"),
    "r_lead": (
        "Цена строки = цена каталога × (1 + наценка категории) × (1 − скидка от суммы). Плюс — наценка, минус — "
        "скидка. Правила живут в этом браузере и действуют на заявки в «{target}».",
        "Line price = catalog price × (1 + category markup) × (1 − volume discount). Plus is a markup, minus a "
        "discount. The rules live in this browser and apply to orders into «{target}».",
    ),
    "r_default": ("Для остальных категорий, %", "Other categories, %"),
    "r_category": ("Категория", "Category"),
    "r_percent": ("%", "%"),
    "r_volume": ("Скидка от суммы заказа", "Discount by order sum"),
    "r_from": ("От суммы", "From"),
    "r_round": ("Округлять цену до", "Round prices to"),
    "r_save": ("Сохранить", "Save"),
    "r_back": ("К заявке", "Back to the order"),
    "e_quota_runs": (
        "Разборы закончились: не больше 10 на браузер и 20 на адрес за последние 24 часа.",
        "No orders left: at most 10 per browser and 20 per address in the last 24 hours.",
    ),
    "e_ceiling": (
        "Лимит системы на все заявки за последние 24 часа исчерпан. Попробуйте через несколько часов или напишите нам.",
        "The system's limit for all orders in the last 24 hours is reached. Try again in a few hours or write to us.",
    ),
    "e_model_busy": (
        "Система сейчас не отвечает. Попробуйте через минуту.",
        "The system is not answering. Try in a minute.",
    ),
    "e_model_unreadable": (
        "Система не смогла прочитать документ.",
        "The system could not read the document.",
    ),
    "e_model_off": ("Чтение документов сейчас выключено.", "Reading documents is off right now."),
    "e_type": (
        "Такой файл не читается: нужен PDF, Excel, CSV, текст или фото.",
        "This file cannot be read: send a PDF, Excel, CSV, text or a photo.",
    ),
    "e_size": ("Файл больше 8 МБ.", "The file is over 8 MB."),
    "e_size_t": ("Слишком большой файл", "The file is too large"),
    "e_pages": ("В PDF больше 10 страниц.", "The PDF has more than 10 pages."),
    "e_image_size": ("Картинка больше 40 мегапикселей.", "The picture is over 40 megapixels."),
    "e_damaged": ("Файл повреждён или защищён паролем.", "The file is damaged or protected by a password."),
    "e_scan_size": (
        "Файл слишком большой: картинки в PDF больше 40 мегапикселей на страницу.",
        "The file is too large: the pictures in the PDF are over 40 megapixels a page.",
    ),
    "e_unpacked": (
        "Файл слишком большой: в распакованном виде таблица больше 50 МБ.",
        "The file is too large: unpacked, the spreadsheet is over 50 MB.",
    ),
    "e_slow": (
        "Файл слишком тяжёлый: разбор не уложился во время. Сохраните его меньше или текстом.",
        "The file is too heavy: reading it took too long. Save it smaller or as text.",
    ),
    "e_busy": (
        "Сейчас разбирается слишком много файлов. Повторите через минуту.",
        "Too many files are being read right now. Try again in a minute.",
    ),
    "e_empty": ("Выберите пример, файл или вставьте текст.", "Pick a sample, a file or paste text."),
    "e_too_long": ("Документ слишком длинный для одной заявки.", "The document is too long for one order."),
    "e_no_lines": ("В документе не нашлось ни одной позиции.", "No order lines were found in the document."),
    "e_catalog": ("Не удалось прочитать каталог CRM.", "The CRM catalog could not be read."),
    "e_catalog_empty": ("В каталоге CRM нет товаров.", "The CRM catalog has no products."),
    "e_demo_off": ("Демо этой CRM сейчас недоступно.", "The demo of this CRM is not available now."),
    "e_reconnect": ("Подключение требует входа заново.", "The connection needs signing in again."),
    "e_rules_number": ("Проценты и суммы — числа.", "Percents and sums are numbers."),
    "e_rules_percent": (
        "Процент категории от −90 до 300, скидка от суммы от 0 до 90.",
        "A category percent is from −90 to 300, a volume discount from 0 to 90.",
    ),
    "e_rules_many": ("Не больше 50 категорий и 5 ступеней.", "At most 50 categories and 5 tiers."),
    "e_rules_round": ("Округление: 0.01, 1, 10 или 100.", "Rounding: 0.01, 1, 10 or 100."),
}


def texts(lang: str) -> dict[str, str]:
    i = 0 if lang == "ru" else 1
    return {key: pair[i] for key, pair in _T.items()}
