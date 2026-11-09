"""Make the reference set of the contract check: three standard contracts and four counterparty drafts with
known changes, and ``samples/truth.json`` that lists every change with its topic and level of risk.

    uv run python apps/contract-check/eval/make_samples.py

Needs LibreOffice (``soffice``) for the PDFs; the scan is the PDF drawn to pictures, turned a little, blurred
and saved as a PDF without text. The files are committed: CI and the page read them, nobody runs this there.
"""

import io
import json
import random
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageFilter

from alnuzum_files.testing import docx, paragraph

OUT = Path(__file__).resolve().parent.parent / "src/alnuzum_contracts/samples"

# a paragraph: (level, text). level 0 a numbered section, 1 a numbered clause, None not numbered
SUPPLY_RU = [
    (None, "ДОГОВОР ПОСТАВКИ № 15/26"),
    (None, "г. Ташкент, 2 ноября 2026 г."),
    (
        None,
        "ООО «Альфа Электро», именуемое в дальнейшем «Поставщик», в лице директора Каримова А. Б., действующего на основании Устава, с одной стороны, и Покупатель, указанный в разделе 12, с другой стороны, заключили настоящий договор о нижеследующем.",
    ),
    (0, "ПРЕДМЕТ ДОГОВОРА"),
    (
        1,
        "Поставщик обязуется передать в собственность Покупателя электротехническую продукцию (далее — товар), а Покупатель обязуется принять и оплатить товар на условиях настоящего договора.",
    ),
    (
        1,
        "Наименование, количество, ассортимент и цена товара указываются в спецификациях, которые являются неотъемлемой частью договора.",
    ),
    (
        1,
        "Поставщик гарантирует, что товар принадлежит ему на праве собственности, не заложен и не находится под арестом.",
    ),
    (0, "ЦЕНА ТОВАРА"),
    (1, "Цена товара указывается в спецификациях в сумах и включает НДС по ставке 12 %."),
    (1, "Цена товара, согласованная в спецификации, не подлежит изменению после внесения предоплаты."),
    (0, "ПОСТАВКА ТОВАРА"),
    (
        1,
        "Поставка осуществляется на условиях самовывоза со склада Поставщика по адресу: г. Ташкент, ул. Нурафшон, 12.",
    ),
    (
        1,
        "Поставщик передаёт товар в течение 15 (пятнадцати) рабочих дней с даты поступления предоплаты на его расчётный счёт.",
    ),
    (
        1,
        "Риск случайной гибели товара переходит к Покупателю с момента передачи товара перевозчику или представителю Покупателя.",
    ),
    (
        1,
        "Товар передаётся по товарной накладной и счёту-фактуре, подписанным уполномоченными представителями сторон.",
    ),
    (0, "ПОРЯДОК РАСЧЁТОВ"),
    (
        1,
        "Покупатель вносит предоплату в размере 30 % от стоимости партии товара в течение 5 (пяти) банковских дней с даты выставления счёта.",
    ),
    (
        1,
        "Оставшаяся часть стоимости партии оплачивается в течение 10 (десяти) банковских дней с даты подписания товарной накладной.",
    ),
    (
        1,
        "Обязательство Покупателя по оплате считается исполненным с даты зачисления денег на расчётный счёт Поставщика.",
    ),
    (0, "ПРИЁМКА ТОВАРА"),
    (
        1,
        "Приёмка товара по количеству и качеству производится при его передаче в присутствии представителей обеих сторон.",
    ),
    (1, "При обнаружении несоответствия составляется акт, подписанный представителями обеих сторон."),
    (
        1,
        "Претензии по количеству и качеству принимаются в течение 5 (пяти) рабочих дней с даты приёмки товара.",
    ),
    (0, "ГАРАНТИИ"),
    (1, "Гарантийный срок на товар составляет 12 (двенадцать) месяцев с даты передачи товара Покупателю."),
    (
        1,
        "Гарантия не распространяется на дефекты, возникшие по вине Покупателя, в том числе при нарушении правил монтажа и эксплуатации.",
    ),
    (0, "ОТВЕТСТВЕННОСТЬ СТОРОН"),
    (
        1,
        "За просрочку поставки Поставщик уплачивает неустойку в размере 0,1 % от стоимости непоставленного в срок товара за каждый день просрочки, но не более 10 % от его стоимости.",
    ),
    (
        1,
        "Совокупная ответственность Поставщика по договору ограничена 10 % от цены соответствующей партии товара.",
    ),
    (
        1,
        "Сторона, нарушившая договор, возмещает другой стороне только реальный ущерб; упущенная выгода не взыскивается.",
    ),
    (
        1,
        "За просрочку оплаты Покупатель уплачивает пеню в размере 0,1 % от неоплаченной суммы за каждый день просрочки.",
    ),
    (0, "КОНФИДЕНЦИАЛЬНОСТЬ"),
    (
        1,
        "Стороны обязуются не раскрывать третьим лицам условия договора и сведения, полученные при его исполнении, в течение 3 (трёх) лет после окончания его срока.",
    ),
    (0, "СРОК ДЕЙСТВИЯ И РАСТОРЖЕНИЕ"),
    (1, "Договор вступает в силу с даты подписания и действует до 31 декабря 2027 года."),
    (
        1,
        "Договор может быть расторгнут по соглашению сторон или в судебном порядке по основаниям, предусмотренным законодательством.",
    ),
    (0, "РАЗРЕШЕНИЕ СПОРОВ"),
    (1, "Стороны разрешают споры путём переговоров; срок ответа на претензию — 10 (десять) рабочих дней."),
    (
        1,
        "Споры, не урегулированные в претензионном порядке, рассматриваются в Ташкентском межрайонном экономическом суде.",
    ),
    (0, "ЗАКЛЮЧИТЕЛЬНЫЕ ПОЛОЖЕНИЯ"),
    (
        1,
        "Изменения и дополнения к договору действительны, если они совершены в письменной форме и подписаны обеими сторонами.",
    ),
    (
        1,
        "Уведомления направляются заказным письмом или по электронной почте на адреса, указанные в договоре.",
    ),
    (0, "РЕКВИЗИТЫ СТОРОН"),
    (
        None,
        "Поставщик: ООО «Альфа Электро», ИНН 305 123 456, р/с 2020 8000 1234 5678 9012 в АКБ «Капиталбанк», МФО 01088.",
    ),
    (None, "Покупатель: реквизиты указываются в спецификации."),
]

SERVICES_RU = [  # numbered in the text itself
    "ДОГОВОР ВОЗМЕЗДНОГО ОКАЗАНИЯ УСЛУГ № 7-У",
    "г. Ташкент, 2 ноября 2026 г.",
    "ООО «Бета Софт», именуемое «Исполнитель», и Заказчик, указанный в разделе 10, заключили настоящий договор о нижеследующем.",
    "1. ПРЕДМЕТ ДОГОВОРА",
    "1.1. Исполнитель обязуется оказать Заказчику услуги по настройке и сопровождению CRM-системы, а Заказчик обязуется оплатить эти услуги.",
    "1.2. Состав, сроки и стоимость услуг определяются в заявках, согласованных сторонами.",
    "2. ПРАВА И ОБЯЗАННОСТИ СТОРОН",
    "2.1. Исполнитель приступает к оказанию услуг в течение 3 (трёх) рабочих дней с даты оплаты аванса.",
    "2.2. Заказчик обязуется своевременно предоставлять информацию и доступы, необходимые для оказания услуг.",
    "2.3. Исполнитель вправе привлекать к оказанию услуг третьих лиц, оставаясь ответственным за их действия.",
    "3. СТОИМОСТЬ И ПОРЯДОК ОПЛАТЫ",
    "3.1. Заказчик вносит аванс в размере 50 % стоимости заявки в течение 3 (трёх) банковских дней с даты выставления счёта.",
    "3.2. Заказчик оплачивает оставшуюся часть стоимости услуг в течение 5 (пяти) рабочих дней с даты подписания акта.",
    "3.3. Исполнитель вправе изменить стоимость услуг, уведомив Заказчика не позднее чем за 30 (тридцать) дней.",
    "4. СДАЧА И ПРИЁМКА УСЛУГ",
    "4.1. По окончании оказания услуг Исполнитель направляет Заказчику акт оказанных услуг.",
    "4.2. Если Заказчик не подписал акт и не направил мотивированный отказ в течение 5 (пяти) рабочих дней, услуги считаются принятыми.",
    "5. ОТВЕТСТВЕННОСТЬ СТОРОН",
    "5.1. Ответственность Исполнителя ограничена суммой, уплаченной Заказчиком за последний месяц оказания услуг.",
    "5.2. За просрочку оплаты Заказчик уплачивает пеню в размере 0,1 % от неоплаченной суммы за каждый день просрочки.",
    "5.3. Стороны освобождаются от ответственности при обстоятельствах непреодолимой силы.",
    "6. ИНТЕЛЛЕКТУАЛЬНЫЕ ПРАВА",
    "6.1. Исключительные права на результаты услуг переходят к Заказчику после полной оплаты соответствующей заявки.",
    "7. КОНФИДЕНЦИАЛЬНОСТЬ",
    "7.1. Стороны не раскрывают третьим лицам сведения, полученные при исполнении договора, в течение 3 (трёх) лет после окончания его срока.",
    "8. СРОК ДЕЙСТВИЯ И РАСТОРЖЕНИЕ",
    "8.1. Договор действует в течение одного года с даты подписания.",
    "8.2. Каждая из сторон вправе расторгнуть договор, уведомив другую сторону за 30 (тридцать) дней до даты расторжения.",
    "9. РАЗРЕШЕНИЕ СПОРОВ",
    "9.1. Споры разрешаются путём переговоров, а при недостижении согласия — в Экономическом суде города Ташкента.",
    "10. РЕКВИЗИТЫ СТОРОН",
    "Исполнитель: ООО «Бета Софт», ИНН 306 654 321, адрес: г. Ташкент, ул. Амира Темура, 1.",
    "Заказчик: реквизиты указываются в заявке.",
]

SUPPLY_EN = [
    (None, "SUPPLY AGREEMENT No. 21-E"),
    (None, "Tashkent, 2 November 2026"),
    (None, "Gamma Trade LLC (the Supplier) and the Buyer named in the Order (the Buyer) agree as follows."),
    (0, "SUBJECT"),
    (
        1,
        "The Supplier shall deliver the Goods listed in each Order, and the Buyer shall accept and pay for them.",
    ),
    (1, "Each Order states the Goods, their quantity, price and the date of delivery."),
    (0, "DELIVERY"),
    (1, "The Goods are delivered FCA Tashkent (Incoterms 2020)."),
    (1, "Title and risk in the Goods pass to the Buyer on delivery to the carrier."),
    (0, "PRICE AND PAYMENT"),
    (1, "The Buyer shall pay each invoice within thirty (30) days of the invoice date."),
    (1, "The Buyer shall pay a late payment interest of 0.05% of the overdue amount per day."),
    (1, "All payments are made in US dollars to the account stated in the invoice."),
    (0, "ACCEPTANCE"),
    (
        1,
        "The Buyer shall inspect the Goods on delivery and notify the Supplier of any defects within five (5) business days.",
    ),
    (0, "WARRANTY"),
    (
        1,
        "The Supplier warrants that the Goods are free from defects in materials and workmanship for twelve (12) months from delivery.",
    ),
    (1, "The warranty does not cover wear and tear or damage caused by improper use."),
    (0, "LIABILITY"),
    (
        1,
        "The Supplier's total liability under this Agreement shall not exceed the price of the Goods delivered under the relevant Order.",
    ),
    (1, "Neither party is liable for indirect or consequential losses, including loss of profit."),
    (0, "TERM AND TERMINATION"),
    (1, "This Agreement remains in force until 31 December 2027."),
    (
        1,
        "Either party may terminate this Agreement for material breach that is not remedied within thirty (30) days of notice.",
    ),
    (0, "GOVERNING LAW AND DISPUTES"),
    (1, "This Agreement is governed by the laws of the Republic of Uzbekistan."),
    (1, "Disputes are finally settled by arbitration at the Tashkent International Arbitration Centre."),
    (0, "DETAILS OF THE SUPPLIER"),
    (None, "Gamma Trade LLC, TIN 307 111 222, Bank: Kapitalbank, account 2020 8840 0000 1111 2222."),
]

# every change: where (a unique part of a paragraph), what, and the truth: topic and level of risk for us.
# cosmetic: true for a change that must never be rated high (requisites, wording)
PAIRS = [
    {
        "id": "supply-ru-docx",
        "standard": "supply-ru",
        "file": "redline-supply-ru.docx",
        "format": "docx",
        "title": {"ru": "Поставка: правки покупателя, DOCX", "en": "Supply: the buyer's edits, DOCX"},
        "edits": [
            {
                "op": "replace",
                "find": "в течение 10 (десяти) банковских дней",
                "with": "в течение 45 (сорока пяти) банковских дней",
                "topic": "payment",
                "risk": "high",
            },
            {
                "op": "replace",
                "find": "в размере 30 % от стоимости партии",
                "with": "в размере 50 % от стоимости партии",
                "topic": "payment",
                "risk": "low",
            },
            {
                "op": "replace",
                "find": "в течение 15 (пятнадцати) рабочих дней",
                "with": "в течение 15 (пятнадцати) календарных дней",
                "topic": "delivery",
                "risk": "high",
            },
            {
                "op": "delete",
                "find": "Совокупная ответственность Поставщика",
                "topic": "liability",
                "risk": "high",
            },
            {
                "op": "replace",
                "find": "0,1 % от стоимости непоставленного в срок товара за каждый день просрочки, но не более 10 %",
                "with": "0,5 % от стоимости непоставленного в срок товара за каждый день просрочки, но не более 30 %",
                "topic": "liability",
                "risk": "high",
            },
            {
                "op": "replace",
                "find": "рассматриваются в Ташкентском межрайонном экономическом суде",
                "with": "рассматриваются в экономическом суде по месту нахождения Покупателя",
                "topic": "jurisdiction",
                "risk": "high",
            },
            {
                "op": "add",
                "after": "Договор может быть расторгнут по соглашению сторон",
                "level": 1,
                "text": "Покупатель вправе в одностороннем порядке отказаться от исполнения договора, уведомив Поставщика за 5 (пять) дней.",
                "topic": "termination",
                "risk": "high",
            },
            {
                "op": "replace",
                "find": "в течение 5 (пяти) рабочих дней с даты приёмки товара",
                "with": "в течение 30 (тридцати) дней с даты приёмки товара",
                "topic": "acceptance",
                "risk": "high",
            },
            {
                "op": "replace",
                "find": "составляет 12 (двенадцать) месяцев",
                "with": "составляет 24 (двадцать четыре) месяца",
                "topic": "warranty",
                "risk": "high",
            },
            {
                "op": "replace",
                "find": "с момента передачи товара перевозчику или представителю Покупателя",
                "with": "с момента приёмки товара Покупателем на его складе",
                "topic": "delivery",
                "risk": "high",
            },
            {
                "op": "replace",
                "find": "Уведомления направляются заказным письмом или по электронной почте",
                "with": "Уведомления направляются только заказным письмом",
                "topic": "other",
                "risk": "medium",
            },
            {
                "op": "replace",
                "find": "Поставщик обязуется передать в собственность Покупателя",
                "with": "Поставщик обязуется поставить и передать в собственность Покупателя",
                "topic": "wording",
                "risk": "low",
                "cosmetic": True,
            },
            {
                "op": "replace",
                "find": "р/с 2020 8000 1234 5678 9012 в АКБ «Капиталбанк», МФО 01088",
                "with": "р/с 2020 8000 9876 5432 1098 в АКБ «Хамкорбанк», МФО 00083",
                "topic": "requisites",
                "risk": "low",
                "cosmetic": True,
            },
            {"op": "format", "find": "ЦЕНА ТОВАРА", "with": "Цена товара"},
            {
                "op": "format",
                "find": "Наименование, количество, ассортимент и цена товара указываются в спецификациях",
                "with": "Наименование,  количество, ассортимент и цена товара указываются в «спецификациях»",
            },
        ],
    },
    {
        "id": "services-ru-pdf",
        "standard": "services-ru",
        "file": "redline-services-ru.pdf",
        "format": "pdf",
        "title": {"ru": "Услуги: правки заказчика, PDF", "en": "Services: the customer's edits, PDF"},
        "edits": [
            {
                "op": "replace",
                "find": "в течение 5 (пяти) рабочих дней с даты подписания акта",
                "with": "в течение 30 (тридцати) рабочих дней с даты подписания акта",
                "topic": "payment",
                "risk": "high",
            },
            {"op": "delete", "find": "Если Заказчик не подписал акт", "topic": "acceptance", "risk": "high"},
            {
                "op": "replace",
                "find": "Ответственность Исполнителя ограничена суммой, уплаченной Заказчиком за последний месяц оказания услуг.",
                "with": "Исполнитель возмещает Заказчику причинённые убытки в полном объёме.",
                "topic": "liability",
                "risk": "high",
            },
            {
                "op": "delete",
                "find": "За просрочку оплаты Заказчик уплачивает пеню",
                "topic": "liability",
                "risk": "high",
            },
            {
                "op": "replace",
                "find": "в Экономическом суде города Ташкента",
                "with": "в Арбитражном суде города Москвы",
                "topic": "jurisdiction",
                "risk": "high",
            },
            {
                "op": "replace",
                "find": "Каждая из сторон вправе расторгнуть договор, уведомив другую сторону за 30 (тридцать) дней",
                "with": "Заказчик вправе расторгнуть договор в любое время, уведомив Исполнителя за 3 (три) дня",
                "topic": "termination",
                "risk": "high",
            },
            {
                "op": "replace",
                "find": "переходят к Заказчику после полной оплаты соответствующей заявки",
                "with": "переходят к Заказчику с момента их создания",
                "topic": "rights",
                "risk": "high",
            },
            {
                "op": "add",
                "after": "2.3. Исполнитель вправе привлекать",
                "text": "2.4. Исполнитель ежедневно направляет Заказчику отчёт о ходе оказания услуг.",
                "topic": "other",
                "risk": "medium",
            },
            {
                "op": "replace",
                "find": "в течение 3 (трёх) рабочих дней с даты оплаты аванса",
                "with": "в течение 5 (пяти) рабочих дней с даты оплаты аванса",
                "topic": "other",
                "risk": "low",
            },
            {
                "op": "replace",
                "find": "Исполнитель вправе изменить стоимость услуг, уведомив Заказчика не позднее чем за 30 (тридцать) дней.",
                "with": "Стоимость услуг является твёрдой и не подлежит изменению в течение срока действия договора.",
                "topic": "payment",
                "risk": "high",
            },
            {
                "op": "replace",
                "find": "своевременно предоставлять информацию и доступы",
                "with": "своевременно предоставлять Исполнителю информацию и доступы",
                "topic": "wording",
                "risk": "low",
                "cosmetic": True,
            },
            {
                "op": "replace",
                "find": "ИНН 306 654 321, адрес: г. Ташкент, ул. Амира Темура, 1.",
                "with": "ИНН 306 654 321, адрес: г. Ташкент, ул. Шота Руставели, 45.",
                "topic": "requisites",
                "risk": "low",
                "cosmetic": True,
            },
            {
                "op": "format",
                "find": "5.3. Стороны освобождаются от ответственности",
                "with": "5.3. Стороны  освобождаются от ответственности",
            },
        ],
    },
    {
        "id": "supply-en-txt",
        "standard": "supply-en",
        "file": "redline-supply-en.txt",
        "format": "txt",
        "title": {"ru": "Supply (EN): правки покупателя, TXT", "en": "Supply (EN): the buyer's edits, TXT"},
        "edits": [
            {
                "op": "replace",
                "find": "within thirty (30) days of the invoice date",
                "with": "within ninety (90) days of the invoice date",
                "topic": "payment",
                "risk": "high",
            },
            {
                "op": "delete",
                "find": "The Supplier's total liability under this Agreement",
                "topic": "liability",
                "risk": "high",
            },
            {
                "op": "replace",
                "find": "governed by the laws of the Republic of Uzbekistan",
                "with": "governed by the laws of England and Wales",
                "topic": "jurisdiction",
                "risk": "high",
            },
            {
                "op": "replace",
                "find": "at the Tashkent International Arbitration Centre",
                "with": "at the London Court of International Arbitration",
                "topic": "jurisdiction",
                "risk": "high",
            },
            {
                "op": "replace",
                "find": "FCA Tashkent (Incoterms 2020)",
                "with": "DDP the Buyer's warehouse (Incoterms 2020)",
                "topic": "delivery",
                "risk": "high",
            },
            {
                "op": "replace",
                "find": "within five (5) business days",
                "with": "within sixty (60) days",
                "topic": "acceptance",
                "risk": "high",
            },
            {
                "op": "add",
                "after": "All payments are made in US dollars",
                "level": 1,
                "text": "The Buyer may set off any amount the Supplier owes it against any payment due to the Supplier.",
                "topic": "payment",
                "risk": "high",
            },
            {
                "op": "replace",
                "find": "for twelve (12) months from delivery",
                "with": "for twelve (12) months from installation",
                "topic": "warranty",
                "risk": "medium",
            },
            {
                "op": "replace",
                "find": "The Supplier shall deliver the Goods listed",
                "with": "The Supplier shall supply and deliver the Goods listed",
                "topic": "wording",
                "risk": "low",
                "cosmetic": True,
            },
            {
                "op": "replace",
                "find": "late payment interest of 0.05% of the overdue amount",
                "with": "late payment interest of 0.1% of the overdue amount",
                "topic": "payment",
                "risk": "low",
            },
            {
                "op": "replace",
                "find": "Bank: Kapitalbank, account 2020 8840 0000 1111 2222",
                "with": "Bank: Hamkorbank, account 2020 8840 0000 3333 4444",
                "topic": "requisites",
                "risk": "low",
                "cosmetic": True,
            },
            {"op": "format", "find": "SUPPLY AGREEMENT No. 21-E", "with": "Supply Agreement No. 21-E"},
        ],
    },
    {
        "id": "supply-ru-scan",
        "standard": "supply-ru",
        "file": "redline-supply-ru-scan.pdf",
        "format": "scan",
        "title": {"ru": "Поставка: правки покупателя, скан", "en": "Supply: the buyer's edits, a scan"},
        "edits": [
            {
                "op": "replace",
                "find": "в течение 10 (десяти) банковских дней",
                "with": "в течение 20 (двадцати) банковских дней",
                "topic": "payment",
                "risk": "high",
            },
            {
                "op": "delete",
                "find": "Сторона, нарушившая договор, возмещает",
                "topic": "liability",
                "risk": "high",
            },
            {
                "op": "replace",
                "find": "на условиях самовывоза со склада Поставщика по адресу: г. Ташкент, ул. Нурафшон, 12",
                "with": "силами и за счёт Поставщика до склада Покупателя",
                "topic": "delivery",
                "risk": "high",
            },
            {
                "op": "replace",
                "find": "действует до 31 декабря 2027 года",
                "with": "действует до 31 декабря 2028 года",
                "topic": "termination",
                "risk": "medium",
            },
            {
                "op": "add",
                "after": "Оставшаяся часть стоимости партии",
                "level": 1,
                "text": "Поставщик предоставляет Покупателю банковскую гарантию возврата предоплаты на всю её сумму.",
                "topic": "payment",
                "risk": "high",
            },
            {
                "op": "delete",
                "find": "Гарантия не распространяется на дефекты",
                "topic": "warranty",
                "risk": "high",
            },
            {
                "op": "replace",
                "find": "в течение 3 (трёх) лет после окончания его срока",
                "with": "в течение 5 (пяти) лет после окончания его срока",
                "topic": "confidentiality",
                "risk": "medium",
            },
            {
                "op": "replace",
                "find": "указываются в спецификациях, которые являются",
                "with": "указываются в спецификациях (приложениях), которые являются",
                "topic": "wording",
                "risk": "low",
                "cosmetic": True,
            },
        ],
    },
]

STANDARDS = {
    "supply-ru": {
        "file": "standard-supply-ru.docx",
        "side": "supplier",
        "lang": "ru",
        "title": {
            "ru": "Договор поставки, мы — поставщик",
            "en": "Supply contract (RU), we are the supplier",
        },
    },
    "services-ru": {
        "file": "standard-services-ru.docx",
        "side": "contractor",
        "lang": "ru",
        "title": {
            "ru": "Договор услуг, мы — исполнитель",
            "en": "Services contract (RU), we are the contractor",
        },
    },
    "supply-en": {
        "file": "standard-supply-en.docx",
        "side": "supplier",
        "lang": "en",
        "title": {
            "ru": "Supply agreement (EN), мы — поставщик",
            "en": "Supply agreement (EN), we are the supplier",
        },
    },
}


def numbered(paras: list[tuple[int | None, str]]) -> list[str]:
    """The text as printed: numbers of sections and clauses written out."""
    out, sec, cl = [], 0, 0
    for level, text in paras:
        if level == 0:
            sec, cl = sec + 1, 0
            out.append(f"{sec}. {text}")
        elif level == 1:
            cl += 1
            out.append(f"{sec}.{cl}. {text}")
        else:
            out.append(text)
    return out


def apply(paras: list, edits: list[dict]) -> list:
    """The counterparty's draft: every edit once, at a paragraph found by a unique part of its text."""
    out = list(paras)

    def at(part: str) -> int:
        hits = [i for i, p in enumerate(out) if part in (p[1] if isinstance(p, tuple) else p)]
        assert len(hits) == 1, (part, hits)
        return hits[0]

    for e in edits:
        i = at(e.get("find") or e["after"])
        if e["op"] in ("replace", "format"):
            if isinstance(out[i], tuple):
                out[i] = (out[i][0], out[i][1].replace(e["find"], e["with"]))
            else:
                out[i] = out[i].replace(e["find"], e["with"])
        elif e["op"] == "delete":
            out.pop(i)
        elif e["op"] == "add":
            out.insert(i + 1, (e["level"], e["text"]) if isinstance(out[i], tuple) else e["text"])
    return out


def auto_docx(paras: list[tuple[int | None, str]]) -> bytes:
    body = [
        paragraph(text, num=1, ilvl=level) if level is not None else paragraph(text, heading=n == 0)
        for n, (level, text) in enumerate(paras)
    ]
    return docx(body)


def text_docx(lines: list[str]) -> bytes:
    return docx([paragraph(t, heading=n == 0) for n, t in enumerate(lines)], numbering=None, styles=None)


def to_pdf(data: bytes, tmp: Path, name: str) -> bytes:
    src = tmp / f"{name}.docx"
    src.write_bytes(data)
    soffice = shutil.which("soffice") or "/opt/homebrew/bin/soffice"
    subprocess.run(
        [soffice, "--headless", "--convert-to", "pdf", "--outdir", str(tmp), str(src)],
        check=True,
        capture_output=True,
        timeout=120,
    )
    return (tmp / f"{name}.pdf").read_bytes()


def to_scan(pdf: bytes) -> bytes:
    import pypdfium2 as pdfium

    rng = random.Random(15)
    pages = []
    doc = pdfium.PdfDocument(pdf)
    for n in range(len(doc)):
        img = doc[n].render(scale=150 / 72).to_pil().convert("L")
        img = img.rotate(
            rng.uniform(-0.6, 0.6), expand=False, fillcolor=255, resample=Image.Resampling.BICUBIC
        )
        img = img.filter(ImageFilter.GaussianBlur(0.6))
        noise = Image.effect_noise(img.size, 18).point(lambda v: 255 if v > 120 else 230)
        img = Image.composite(img, noise, Image.new("L", img.size, 200))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=55)
        pages.append(Image.open(io.BytesIO(buf.getvalue())))
    out = io.BytesIO()
    pages[0].save(out, "PDF", save_all=True, append_images=pages[1:], resolution=150)
    return out.getvalue()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    texts = {"supply-ru": SUPPLY_RU, "services-ru": SERVICES_RU, "supply-en": SUPPLY_EN}
    (OUT / "standard-supply-ru.docx").write_bytes(auto_docx(SUPPLY_RU))
    (OUT / "standard-services-ru.docx").write_bytes(text_docx(SERVICES_RU))
    (OUT / "standard-supply-en.docx").write_bytes(auto_docx(SUPPLY_EN))
    truth_pairs = []
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        for pair in PAIRS:
            theirs = apply(texts[pair["standard"]], pair["edits"])
            printed = numbered(theirs) if theirs and isinstance(theirs[0], tuple) else list(theirs)
            fmt = pair["format"]
            if fmt == "docx":
                data = auto_docx(theirs)
            elif fmt == "pdf":
                data = to_pdf(text_docx(theirs), tmp, pair["id"])
            elif fmt == "txt":
                data = ("\n".join(printed) + "\n").encode()
            else:
                data = to_scan(to_pdf(auto_docx(theirs), tmp, pair["id"]))
            (OUT / pair["file"]).write_bytes(data)
            truth_pairs.append(pair | {"theirs": printed})
            print(
                pair["file"],
                len(data),
                "bytes",
                len([e for e in pair["edits"] if e["op"] != "format"]),
                "changes",
            )
    standards = {
        k: v | {"text": numbered(texts[k]) if isinstance(texts[k][0], tuple) else list(texts[k])}
        for k, v in STANDARDS.items()
    }
    (OUT / "truth.json").write_text(
        json.dumps({"standards": standards, "pairs": truth_pairs}, ensure_ascii=False, indent=1) + "\n"
    )
    total = sum(1 for p in PAIRS for e in p["edits"] if e["op"] != "format")
    print("changes in all:", total, file=sys.stderr)


if __name__ == "__main__":
    main()
