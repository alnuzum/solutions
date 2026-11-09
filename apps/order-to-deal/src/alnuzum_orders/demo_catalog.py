# mypy: disable-error-code="assignment"
"""The demo catalog: an electrical and fasteners wholesaler, 200 items.

It is loaded into the demo account of every CRM (``scripts/load_demo_catalog.py``) and read back from
there like any customer's catalog: the matching never sees this module, only what the CRM returns.
Prices are in roubles and converted to each account's currency when loaded. Stock is pseudo-random
but fixed (a hash of the article), with some items at zero so that analogues show.
"""

import hashlib
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

PREFIX = "EL-"


@dataclass(frozen=True, slots=True)
class DemoItem:
    sku: str
    name: str
    category: str
    unit: str
    price_rub: Decimal
    synonyms: tuple[str, ...] = ()
    pack: str = ""

    @property
    def stock(self) -> int:
        h = int(hashlib.sha256(self.sku.encode()).hexdigest()[:8], 16)
        return 0 if h % 13 == 0 else (h % 480) + 5

    def description(self) -> str:
        """What the CRM keeps in the product's description: the conventions the reader parses."""
        lines = [f"Категория: {self.category}", f"Единица: {self.unit}"]
        if self.synonyms:
            lines.append("Синонимы: " + "; ".join(self.synonyms))
        if self.pack:
            lines.append(f"Упаковка: {self.pack}")
        return "\n".join(lines)


_ITEMS: list[DemoItem] = []


def _add(
    code: str, name: str, category: str, unit: str, price: float, syn: tuple[str, ...] = (), pack: str = ""
) -> None:
    n = sum(1 for i in _ITEMS if i.sku.startswith(f"{PREFIX}{code}-")) + 1
    value = Decimal(str(price)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    _ITEMS.append(DemoItem(f"{PREFIX}{code}-{n:03d}", name, category, unit, value, syn, pack))


# --- cable, sold by the metre ---
CABLE = "Кабель и провод"
for cores, price in (
    ("2x1,5", 52),
    ("2x2,5", 78),
    ("3x1,5", 71),
    ("3x2,5", 109),
    ("3x4", 172),
    ("3x6", 251),
    ("3x10", 410),
    ("5x2,5", 184),
    ("5x4", 288),
    ("5x6", 420),
):
    _add(
        "CB",
        f"Кабель ВВГнг(А)-LS {cores} ок(N,PE)-0,66 кВ",
        CABLE,
        "м",
        price,
        (f"ВВГ {cores}", f"ВВГнг {cores}", f"кабель силовой {cores}", f"VVG cable {cores.replace(',', '.')}"),
        "бухта = 100 м",
    )
for cores, price in (("3x1,5", 88), ("3x2,5", 129), ("5x2,5", 205)):
    _add(
        "CB",
        f"Кабель NYM {cores} (Севкабель)",
        CABLE,
        "м",
        price,
        (f"NYM {cores}", f"кабель НУМ {cores}"),
        "бухта = 100 м",
    )
for cores, price in (("2x0,75", 34), ("3x1,5", 79), ("3x2,5", 118)):
    _add(
        "CB",
        f"Провод ПВС {cores}",
        CABLE,
        "м",
        price,
        (f"ПВС {cores}", f"провод гибкий {cores}", f"flex cable {cores.replace(',', '.')}"),
        "бухта = 100 м",
    )
for colour in ("синий", "красный", "жёлто-зелёный", "белый", "чёрный"):
    _add(
        "CB",
        f"Провод ПуГВ 1x2,5 {colour}",
        CABLE,
        "м",
        27,
        (f"ПуГВ 2,5 {colour}", f"ПВ3 1x2,5 {colour}"),
        "бухта = 100 м",
    )
_add(
    "CB",
    "Кабель UTP 4х2х0,5 cat.5e внутренний",
    CABLE,
    "м",
    24,
    ("витая пара", "UTP 5e", "кабель для интернета", "LAN cable"),
    "бухта = 305 м",
)
_add("CB", "Провод ШВВП 2x0,75", CABLE, "м", 26, ("ШВВП 2х0.75", "провод плоский"), "бухта = 100 м")
_add("CB", "Кабель КГ 3x2,5", CABLE, "м", 165, ("КГ 3х2.5", "кабель гибкий резиновый"), "бухта = 50 м")
_add("CB", "Провод СИП-4 2x16", CABLE, "м", 64, ("СИП 2х16", "самонесущий провод 2x16"), "бухта = 100 м")

# --- circuit breakers: the same rating from several brands makes analogues ---
MCB = "Автоматические выключатели"
for amp in (6, 10, 16, 20, 25, 32, 40, 50):
    _add(
        "AV",
        f"Автоматический выключатель IEK ВА47-29 1P C{amp} 4,5 кА",
        MCB,
        "шт",
        189 + amp,
        (
            f"автомат {amp}А",
            f"автомат 1P {amp}",
            f"АВ C{amp} однополюсный",
            f"breaker {amp}A 1P",
            f"ВА 47-29 C{amp}",
        ),
    )
for amp in (16, 25, 32, 40, 50):
    _add(
        "AV",
        f"Автоматический выключатель IEK ВА47-29 3P C{amp} 4,5 кА",
        MCB,
        "шт",
        690 + 6 * amp,
        (f"автомат 3P {amp}А", f"трёхполюсный автомат {amp}", f"breaker {amp}A 3P"),
    )
for amp in (10, 16, 20, 25, 32):
    _add(
        "AV",
        f"Автоматический выключатель ABB S201 1P C{amp} 6 кА",
        MCB,
        "шт",
        640 + 4 * amp,
        (f"ABB S201 C{amp}", f"автомат ABB {amp}А", f"breaker ABB {amp}A"),
    )
for amp in (16, 25, 32):
    _add(
        "AV",
        f"Автоматический выключатель ABB S203 3P C{amp} 6 кА",
        MCB,
        "шт",
        2350 + 15 * amp,
        (f"ABB S203 C{amp}", f"автомат ABB 3P {amp}А"),
    )
for amp in (10, 16, 25):
    _add(
        "AV",
        f"Автоматический выключатель EKF Basic ВА 47-63 1P C{amp} 4,5 кА",
        MCB,
        "шт",
        165 + amp,
        (f"EKF ВА 47-63 C{amp}", f"автомат EKF {amp}А"),
    )

RCD = "УЗО и дифавтоматы"
for name, price, syn in (
    (
        "Выключатель дифференциальный (УЗО) IEK ВД1-63 2P 25А 30мА",
        1150,
        ("УЗО 25А 30мА", "УЗО 2P 25", "RCD 25A 30mA"),
    ),
    (
        "Выключатель дифференциальный (УЗО) IEK ВД1-63 2P 40А 30мА",
        1290,
        ("УЗО 40А 30мА", "УЗО 2P 40", "RCD 40A 30mA"),
    ),
    ("Выключатель дифференциальный (УЗО) IEK ВД1-63 2P 63А 30мА", 1640, ("УЗО 63А 30мА", "УЗО 2P 63")),
    (
        "Выключатель дифференциальный (УЗО) IEK ВД1-63 4P 40А 30мА",
        2380,
        ("УЗО 4P 40А", "четырёхполюсное УЗО 40"),
    ),
    ("Выключатель дифференциальный ABB F202 2P 25А 30мА", 4650, ("УЗО ABB 25А", "ABB F202 25")),
    ("Выключатель дифференциальный ABB F202 2P 40А 30мА", 4980, ("УЗО ABB 40А", "ABB F202 40")),
    (
        "Автоматический выключатель дифференциального тока IEK АВДТ32 C16 30мА",
        1490,
        ("дифавтомат 16А", "АВДТ 16", "RCBO 16A"),
    ),
    (
        "Автоматический выключатель дифференциального тока IEK АВДТ32 C25 30мА",
        1560,
        ("дифавтомат 25А", "АВДТ 25", "RCBO 25A"),
    ),
    (
        "Автоматический выключатель дифференциального тока ABB DS201 C16 30мА",
        5900,
        ("дифавтомат ABB 16А", "ABB DS201 C16"),
    ),
):
    _add("UZ", name, RCD, "шт", price, syn)

WIRING = "Розетки и выключатели"
for series, colours, price in (
    ("Schneider Electric Glossa", ("белый", "бежевый", "антрацит"), 0),
    ("Schneider Electric AtlasDesign", ("белый", "алюминий"), 60),
):
    for colour in colours:
        for item, base, syn in (
            ("Розетка с заземлением", 210, ("розетка", "розетка евро", "socket")),
            ("Розетка двойная с заземлением", 390, ("двойная розетка", "розетка 2-местная", "double socket")),
            (
                "Выключатель одноклавишный",
                190,
                ("выключатель 1-кл", "выключатель одинарный", "switch 1 gang"),
            ),
            ("Выключатель двухклавишный", 260, ("выключатель 2-кл", "выключатель двойной", "switch 2 gang")),
        ):
            if series.endswith("AtlasDesign") and item.startswith("Выключатель двух"):
                continue
            short = series.split()[-1]
            _add(
                "RZ",
                f"{item} {series} {colour}",
                WIRING,
                "шт",
                base + price,
                tuple(f"{s} {short} {colour}" for s in syn),
            )
for item, price, syn in (
    ("Розетка с заземлением Legrand Valena Life белая", 380, ("розетка Legrand Valena", "розетка Валена")),
    ("Выключатель одноклавишный Legrand Valena Life белый", 350, ("выключатель Legrand Valena 1-кл",)),
    ("Выключатель двухклавишный Legrand Valena Life белый", 470, ("выключатель Legrand Valena 2-кл",)),
    ("Рамка 1-постовая Legrand Valena Life белая", 140, ("рамка одинарная Valena", "рамка 1 пост")),
    ("Рамка 2-постовая Legrand Valena Life белая", 230, ("рамка двойная Valena", "рамка 2 поста")),
    ("Рамка 3-постовая Legrand Valena Life белая", 340, ("рамка тройная Valena", "рамка 3 поста")),
):
    _add("RZ", item, WIRING, "шт", price, syn)

TERMINALS = "Клеммы и соединители"
for name, price, syn, pack in (
    (
        "Клемма WAGO 221-412 на 2 провода 0,2-4 мм²",
        38,
        ("ваго 2", "wago 221-412", "клемма 2-х проводная"),
        "уп = 50 шт",
    ),
    (
        "Клемма WAGO 221-413 на 3 провода 0,2-4 мм²",
        44,
        ("ваго 3", "wago 221-413", "клемма 3-х проводная"),
        "уп = 50 шт",
    ),
    (
        "Клемма WAGO 221-415 на 5 проводов 0,2-4 мм²",
        62,
        ("ваго 5", "wago 221-415", "клемма 5-ти проводная"),
        "уп = 25 шт",
    ),
    ("Клемма WAGO 2273-202 на 2 провода 0,5-2,5 мм²", 18, ("wago 2273-202", "ваго 2273 2"), "уп = 100 шт"),
    ("Клемма WAGO 2273-204 на 4 провода 0,5-2,5 мм²", 24, ("wago 2273-204", "ваго 2273 4"), "уп = 100 шт"),
    ("Клемма WAGO 2273-208 на 8 проводов 0,5-2,5 мм²", 41, ("wago 2273-208", "ваго 2273 8"), "уп = 50 шт"),
    ("Зажим соединительный изолирующий СИЗ-2 (уп. 100 шт)", 260, ("СИЗ-2", "колпачок СИЗ 2"), "уп = 100 шт"),
    ("Зажим соединительный изолирующий СИЗ-3 (уп. 100 шт)", 290, ("СИЗ-3", "колпачок СИЗ 3"), "уп = 100 шт"),
    ("Зажим соединительный изолирующий СИЗ-4 (уп. 100 шт)", 330, ("СИЗ-4", "колпачок СИЗ 4"), "уп = 100 шт"),
    ("Колодка клеммная ЗВИ-10 12 пар", 95, ("ЗВИ 10", "клеммник винтовой 10А"), ""),
):
    _add("KL", name, TERMINALS, "уп" if name.startswith("Зажим") else "шт", price, syn, pack)

PIPES = "Гофра и трубы"
for d, price in ((16, 9.8), (20, 12.5), (25, 17.9), (32, 27.4)):
    _add(
        "GF",
        f"Труба гофрированная ПВХ d{d} мм с зондом серая",
        PIPES,
        "м",
        price,
        (f"гофра {d}", f"гофра ПВХ {d} мм", f"гофротруба {d}", f"corrugated pipe {d}mm"),
        "бухта = 100 м" if d < 32 else "бухта = 50 м",
    )
for d, price in ((20, 15.2), (25, 21.6)):
    _add(
        "GF",
        f"Труба гофрированная ПНД d{d} мм с зондом чёрная",
        PIPES,
        "м",
        price,
        (f"гофра ПНД {d}", f"гофра чёрная {d}"),
        "бухта = 100 м",
    )
for d, price in ((16, 64), (20, 79), (25, 112)):
    _add(
        "GF",
        f"Труба ПВХ жёсткая d{d} мм 3 м серая",
        PIPES,
        "шт",
        price,
        (f"труба жёсткая {d}", f"труба ПВХ {d}"),
    )

DUCT = "Кабель-каналы и короба"
for size, price in (
    ("12x12", 39),
    ("16x16", 49),
    ("20x10", 45),
    ("25x16", 69),
    ("40x25", 129),
    ("60x40", 248),
    ("100x60", 560),
):
    _add(
        "KK",
        f"Кабель-канал {size} мм 2 м белый",
        DUCT,
        "шт",
        price,
        (
            f"кабель-канал {size}",
            f"короб {size}",
            f"кабель канал {size.replace('x', 'х')}",
            f"cable duct {size}",
        ),
    )

BOXES = "Монтажные коробки"
for name, price, syn in (
    (
        "Коробка распаечная 100x100x50 IP55 серая",
        112,
        ("распредкоробка 100х100", "коробка распределительная 100x100"),
    ),
    (
        "Коробка распаечная 80x80x50 IP54 серая",
        84,
        ("распредкоробка 80х80", "коробка распределительная 80x80"),
    ),
    ("Подрозетник 68x45 мм для бетона", 9.5, ("подрозетник бетон", "установочная коробка 68х45")),
    ("Подрозетник 68x42 мм для гипсокартона", 14.5, ("подрозетник ГКЛ", "подрозетник для гипсокартона")),
    ("Подрозетник 68x60 мм углублённый для бетона", 15.9, ("подрозетник глубокий", "подрозетник 68х60")),
):
    _add("KR", name, BOXES, "шт", price, syn)

FASTENERS = "Крепёж"
for size, price in (
    ("3,5x25", 98),
    ("3,5x35", 118),
    ("3,5x41", 132),
    ("3,5x55", 168),
    ("4,2x65", 245),
    ("4,2x76", 290),
):
    _add(
        "KP",
        f"Саморез по дереву 3,5 мм чёрный фосфатированный {size} (уп. 100 шт)"
        if size.startswith("3,5")
        else f"Саморез по дереву чёрный фосфатированный {size} (уп. 100 шт)",
        FASTENERS,
        "уп",
        price,
        (
            f"саморез {size}",
            f"саморез по дереву {size.replace(',', '.')}",
            f"шуруп {size}",
            f"wood screw {size.replace(',', '.')}",
        ),
        "уп = 100 шт; кор = 10 уп",
    )
for size, price in (("3,5x25", 104), ("3,5x35", 126)):
    _add(
        "KP",
        f"Саморез по металлу чёрный фосфатированный {size} (уп. 100 шт)",
        FASTENERS,
        "уп",
        price,
        (f"саморез по металлу {size}", f"саморез ГКЛ металл {size}"),
        "уп = 100 шт; кор = 10 уп",
    )
for size, price in (("4,8x29", 310), ("4,8x35", 335)):
    _add(
        "KP",
        f"Саморез кровельный {size} RAL 9003 белый (уп. 100 шт)",
        FASTENERS,
        "уп",
        price,
        (f"кровельный саморез {size}", f"саморез с шайбой {size}"),
        "уп = 100 шт",
    )
for size, price in (("4,2x16", 92), ("4,2x19", 99)):
    _add(
        "KP",
        f"Саморез с прессшайбой (клоп) острый {size} (уп. 100 шт)",
        FASTENERS,
        "уп",
        price,
        (f"клоп {size}", f"ПШ {size}", f"саморез с прессшайбой {size}"),
        "уп = 100 шт",
    )
for size, price in (("6x30", 79), ("6x40", 92), ("8x40", 128), ("8x60", 169), ("10x50", 214)):
    _add(
        "KP",
        f"Дюбель распорный {size} мм нейлон (уп. 100 шт)",
        FASTENERS,
        "уп",
        price,
        (f"дюбель {size}", f"дюбель распорный {size.replace('x', 'х')}", f"wall plug {size}"),
        "уп = 100 шт; кор = 20 уп",
    )
for size, price in (("6x40", 189), ("6x60", 236), ("8x60", 318)):
    _add(
        "KP",
        f"Дюбель-гвоздь {size} мм потайной бортик (уп. 100 шт)",
        FASTENERS,
        "уп",
        price,
        (f"дюбель-гвоздь {size}", f"быстрый монтаж {size}"),
        "уп = 100 шт",
    )
for size, price, colour in (
    ("2,5x100", 46, "белый"),
    ("3,6x150", 79, "белый"),
    ("3,6x200", 98, "белый"),
    ("4,8x200", 139, "белый"),
    ("4,8x300", 210, "белый"),
    ("7,6x400", 470, "белый"),
    ("2,5x100", 49, "чёрный"),
    ("3,6x200", 104, "чёрный"),
    ("4,8x300", 224, "чёрный"),
    ("7,6x400", 495, "чёрный"),
):
    _add(
        "KP",
        f"Хомут нейлоновый {size} мм {colour} (уп. 100 шт)",
        FASTENERS,
        "уп",
        price,
        (f"хомут {size} {colour}", f"стяжка {size} {colour}", f"cable tie {size.replace(',', '.')} {colour}"),
        "уп = 100 шт",
    )
for d, price in ((16, 145), (20, 168), (25, 215)):
    _add(
        "KP",
        f"Клипса для гофры d{d} мм (уп. 100 шт)",
        FASTENERS,
        "уп",
        price,
        (f"клипса {d}", f"держатель трубы {d}"),
        "уп = 100 шт",
    )
for d, price in (("16-17", 120), ("20-22", 138)):
    _add(
        "KP",
        f"Скоба металлическая двухлапковая {d} мм (уп. 100 шт)",
        FASTENERS,
        "уп",
        price,
        (f"скоба {d}", f"скоба двухлапковая {d}"),
        "уп = 100 шт",
    )

LIGHT = "Лампы и светильники"
for watt, temp, price in ((10, 3000, 89), (10, 4000, 89), (12, 4000, 109), (15, 4000, 139), (20, 4000, 189)):
    _add(
        "SV",
        f"Лампа светодиодная IEK LED A60 {watt} Вт {temp} К E27",
        LIGHT,
        "шт",
        price,
        (f"лампа LED {watt}W E27", f"лампочка {watt} Вт E27", f"LED bulb {watt}W E27 {temp}K"),
    )
for watt, price in ((7, 99),):
    _add(
        "SV",
        f"Лампа светодиодная Gauss C37 свеча {watt} Вт 4100 К E14",
        LIGHT,
        "шт",
        price,
        (f"лампа свеча {watt}W E14", f"свеча E14 {watt} Вт"),
    )
_add(
    "SV",
    "Лампа светодиодная Gauss C37 свеча 7 Вт 3000 К E14",
    LIGHT,
    "шт",
    99,
    ("лампа свеча тёплая E14", "свеча E14 3000К"),
)
_add(
    "SV",
    "Лампа светодиодная IEK PAR16 5 Вт 4000 К GU10",
    LIGHT,
    "шт",
    79,
    ("лампа GU10 5W", "лампа софит GU10"),
)
_add("SV", "Лампа светодиодная IEK PAR16 7 Вт 4000 К GU10", LIGHT, "шт", 95, ("лампа GU10 7W",))
for name, price, syn in (
    (
        "Светильник светодиодный панель 595x595 36 Вт 4000 К",
        890,
        ("панель армстронг 36W", "светильник армстронг", "LED panel 600x600"),
    ),
    ("Светильник светодиодный ДПО 1200 мм 36 Вт 6500 К", 640, ("ДПО 36W", "линейный светильник 1200")),
    ("Прожектор светодиодный СДО 30 Вт 6500 К IP65", 520, ("прожектор 30W", "LED floodlight 30W")),
    ("Прожектор светодиодный СДО 50 Вт 6500 К IP65", 790, ("прожектор 50W", "LED floodlight 50W")),
    ("Прожектор светодиодный СДО 100 Вт 6500 К IP65", 1490, ("прожектор 100W", "LED floodlight 100W")),
):
    _add("SV", name, LIGHT, "шт", price, syn)

EXT = "Удлинители и вилки"
for name, price, syn in (
    ("Удлинитель 3 гнезда с заземлением 3 м", 420, ("удлинитель 3м", "переноска 3 гнезда")),
    ("Удлинитель 3 гнезда с заземлением 5 м", 560, ("удлинитель 5м", "переноска 5 метров")),
    (
        "Сетевой фильтр 5 гнёзд 1,8 м с выключателем",
        690,
        ("сетевой фильтр", "пилот 5 розеток", "surge protector"),
    ),
    ("Вилка с заземлением 16 А прямая", 85, ("вилка евро", "штепсель с заземлением")),
    ("Колодка 4 гнезда с заземлением", 210, ("колодка удлинителя 4", "розеточный блок 4")),
):
    _add("UD", name, EXT, "шт", price, syn)

BOARDS = "Щиты и комплектующие"
for mods, kind, price in (
    (6, "накладной", 390),
    (12, "накладной", 620),
    (18, "накладной", 890),
    (12, "встраиваемый", 710),
    (24, "встраиваемый", 1290),
    (36, "встраиваемый", 1980),
):
    _add(
        "SH",
        f"Бокс ЩРН-П-{mods} модулей {kind} пластиковый IP41"
        if kind == "накладной"
        else f"Бокс ЩРВ-П-{mods} модулей {kind} пластиковый IP41",
        BOARDS,
        "шт",
        price,
        (f"щиток на {mods} модулей", f"бокс {mods} автоматов", f"щит {kind} {mods}"),
    )
_add("SH", "DIN-рейка 1 м оцинкованная", BOARDS, "шт", 120, ("дин рейка", "DIN rail 1m"))
_add(
    "SH",
    "Шина нулевая 6x9 мм 12 групп на DIN-изоляторе",
    BOARDS,
    "шт",
    145,
    ("шина N 12", "нулевая шина 12 отверстий"),
)
_add(
    "SH",
    "Кросс-модуль ШНК 4x7 на DIN-рейку",
    BOARDS,
    "шт",
    690,
    ("кросс-модуль", "распределительный блок ШНК"),
)

INSUL = "Изоляционные материалы"
for colour in ("синяя", "чёрная", "красная", "жёлто-зелёная"):
    _add(
        "IZ",
        f"Изолента ПВХ 19 мм x 20 м {colour}",
        INSUL,
        "шт",
        49,
        (f"изолента {colour}", f"изоляционная лента {colour}", "electrical tape"),
    )
for size, price in (("6/3", 32), ("10/5", 41), ("20/10", 68)):
    _add(
        "IZ",
        f"Трубка термоусаживаемая ТУТ {size} мм чёрная 1 м",
        INSUL,
        "шт",
        price,
        (f"термоусадка {size}", f"ТУТ {size}", f"heat shrink {size}"),
    )
_add(
    "IZ",
    "Набор термоусадочных трубок 1,5-10 мм 100 шт",
    INSUL,
    "шт",
    290,
    ("набор термоусадки", "термоусадка набор"),
)

TOOLS = "Инструмент"
TOOL_ROWS = (
    (
        "Перфоратор Makita HR2470 SDS-plus 780 Вт",
        11990,
        ("перфоратор макита", "Makita HR2470", "rotary hammer Makita"),
    ),
    ("Дрель-шуруповёрт аккумуляторная Makita DF333DWYE 12 В", 9490, ("шуруповёрт макита", "Makita DF333")),
    ("Отвёртка индикаторная 140 мм", 120, ("индикатор фазы", "пробник", "voltage tester")),
    ("Мультиметр цифровой DT-830B", 540, ("мультиметр", "тестер DT830")),
    ("Клещи для зачистки проводов КСИ 0,5-6 мм²", 790, ("стриппер", "инструмент для зачистки")),
    ("Бокорезы 160 мм", 450, ("кусачки", "бокорезы")),
    ("Набор отвёрток диэлектрических 6 шт", 1190, ("отвёртки диэлектрические", "набор отвёрток 1000В")),
    ("Бур SDS-plus 6x110 мм", 95, ("бур 6х110", "бур 6 мм")),
    ("Бур SDS-plus 6x160 мм", 110, ("бур 6х160",)),
    ("Бур SDS-plus 8x160 мм", 125, ("бур 8х160", "бур 8 мм")),
    ("Бур SDS-plus 10x210 мм", 165, ("бур 10х210", "бур 10 мм")),
    ("Сверло по металлу HSS 4 мм", 39, ("сверло 4", "сверло по металлу 4 мм")),
    ("Сверло по металлу HSS 6 мм", 59, ("сверло 6", "сверло по металлу 6 мм")),
    ("Коронка по бетону для подрозетников 68 мм SDS-plus", 890, ("коронка 68", "коронка под подрозетник")),
)
for name, price, syn in TOOL_ROWS:
    _add("IN", name, TOOLS, "шт", price, syn)

DEMO_CATALOG: tuple[DemoItem, ...] = tuple(_ITEMS)

# roubles to the currency of a demo account; rounded so that prices look like prices
RATES: dict[str, Decimal] = {
    "RUB": Decimal(1),
    "UZS": Decimal(155),
    "USD": Decimal("0.0125"),
    "EUR": Decimal("0.0115"),
}


def price_in(item: DemoItem, currency: str) -> Decimal:
    value = item.price_rub * RATES.get(currency.upper(), Decimal(1))
    step = Decimal(100) if currency.upper() == "UZS" else Decimal("0.01")
    return (value / step).quantize(Decimal(1), rounding=ROUND_HALF_UP) * step
