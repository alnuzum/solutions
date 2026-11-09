"""A stranger's file: the content decides what it is, bounds are checked before anything reads it."""

import io
import zipfile

import pytest
from openpyxl import Workbook
from PIL import Image
from pypdf import PdfWriter

from alnuzum_orders import intake


def xlsx(rows: list[list[object]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    for row in rows:
        ws.append(row)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def pdf(pages: int) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def image(w: int, h: int, fmt: str = "PNG") -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(out, fmt)
    return out.getvalue()


def test_type_is_decided_by_content_not_by_name() -> None:
    exe = b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 64
    with pytest.raises(intake.IntakeError) as caught:
        intake.read("order.pdf", exe)
    assert caught.value.code == "e_type"
    plain_zip = io.BytesIO()
    with zipfile.ZipFile(plain_zip, "w") as z:
        z.writestr("readme.txt", "hello")
    with pytest.raises(intake.IntakeError) as caught:
        intake.read("order.pdf", plain_zip.getvalue())
    assert caught.value.code == "e_type"
    with pytest.raises(intake.IntakeError) as caught:
        intake.read("order.xlsx", b"PK\x03\x04 not really a zip")
    assert caught.value.code == "e_type"
    # a spreadsheet under a .txt name is still a spreadsheet
    src = intake.read("order.txt", xlsx([["Наименование", "Кол-во"], ["Кабель", 5]]))
    assert src.kind == "table" and src.columns == {"name": 0, "qty": 1}


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"", "e_empty"),
        (b"   \n\t ", "e_empty"),
        (b"x" * (intake.MAX_BYTES + 1), "e_size"),
        (b"%PDF-1.7 broken", "e_damaged"),
        (b"\x89PNG\r\n\x1a\n broken", "e_damaged"),
        (b"\xff\xd8\xff broken", "e_damaged"),
    ],
)
def test_refused_files(data: bytes, code: str) -> None:
    with pytest.raises(intake.IntakeError) as caught:
        intake.read("f", data)
    assert caught.value.code == code


def test_pdf_over_ten_pages_is_refused_and_a_scan_goes_to_the_model() -> None:
    with pytest.raises(intake.IntakeError) as caught:
        intake.read("big.pdf", pdf(intake.MAX_PAGES + 1))
    assert caught.value.code == "e_pages"
    scan = intake.read("scan.pdf", pdf(2))
    # a scan goes to the model as one picture a page, never as the PDF
    assert (scan.kind, scan.media_type, scan.pages, scan.lines) == ("image", "image/jpeg", 2, [])
    assert len(scan.images) == 2
    for page in scan.images:
        with Image.open(io.BytesIO(page)) as img:
            assert img.format == "JPEG" and max(img.size) == intake.MAX_SIDE


def test_picture_over_the_pixel_bound_is_refused_and_a_big_one_is_shrunk() -> None:
    with pytest.raises(intake.IntakeError) as caught:
        intake.read("huge.png", image(8000, 5001))
    assert caught.value.code == "e_image_size"
    src = intake.read("photo.webp", image(3000, 1000, "WEBP"))
    assert (src.kind, src.media_type) == ("image", "image/jpeg")
    assert len(src.images) == 1
    with Image.open(io.BytesIO(src.images[0])) as img:
        assert max(img.size) == intake.MAX_SIDE


def test_text_in_two_encodings_and_its_limits() -> None:
    assert intake.read("a.txt", "Кабель - 5 м\n\nЛампа - 2".encode("cp1251")).lines == [
        "Кабель - 5 м",
        "Лампа - 2",
    ]
    assert intake.read("a.txt", "﻿Кабель".encode()).lines == ["Кабель"]
    with pytest.raises(intake.IntakeError) as caught:
        intake.from_text("a", "x" * (intake.MAX_TEXT + 1))
    assert caught.value.code == "e_too_long"
    with pytest.raises(intake.IntakeError) as caught:
        intake.from_text("a", "\n".join(f"line {i}" for i in range(intake.MAX_LINES + 1)))
    assert caught.value.code == "e_too_long"
    with pytest.raises(intake.IntakeError) as caught:
        intake.read("a.bin", b"ab\x00cd")
    assert caught.value.code == "e_type"


def test_csv_with_a_header_is_a_table_and_without_one_is_text() -> None:
    src = intake.read("o.csv", "Артикул;Наименование;Кол-во;Ед.\nEL-1;Кабель;5;м\nEL-2;Лампа;2;шт\n".encode())
    assert src.kind == "table"
    assert src.columns == {"sku": 0, "name": 1, "qty": 2, "unit": 3}
    assert src.rows == [["EL-1", "Кабель", "5", "м"], ["EL-2", "Лампа", "2", "шт"]]
    loose = intake.read("o.csv", "Кабель;5\nЛампа;2\n".encode())
    assert loose.kind == "text"


def test_spreadsheet_header_found_below_a_title_and_damaged_workbook() -> None:
    src = intake.read(
        "o.xlsx", xlsx([["Заявка №5"], [], ["№", "Товар", "Количество", "Ед. изм."], [1, "Кабель", 5.0, "м"]])
    )
    assert src.columns == {"name": 1, "qty": 2, "unit": 3}
    assert [r[:4] for r in src.rows] == [["1", "Кабель", "5", "м"]]
    assert src.lines[0] == "Заявка №5"
    headless = intake.read("o.xlsx", xlsx([["Кабель", 5], ["Лампа", 2]]))
    assert headless.columns is None and headless.lines == ["Кабель | 5", "Лампа | 2"]
    broken = io.BytesIO()
    with zipfile.ZipFile(broken, "w") as z:
        z.writestr("xl/workbook.xml", "<not a workbook")
    with pytest.raises(intake.IntakeError) as caught:
        intake.read("o.xlsx", broken.getvalue())
    assert caught.value.code == "e_damaged"
    with pytest.raises(intake.IntakeError) as caught:
        intake.read("o.xlsx", xlsx([[None, None]]))
    assert caught.value.code == "e_empty"


# --- C1: what a spreadsheet unpacks to is bounded before it is parsed ---


def bomb(part: str, chunk: bytes, megabytes: int) -> bytes:
    """A real workbook whose ``part`` is replaced by ``megabytes`` of ``chunk``, written in pieces (the
    test itself never holds the unpacked part)."""
    good = zipfile.ZipFile(io.BytesIO(xlsx([["Товар", "Количество"], ["Кабель", 5]])))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for info in good.infolist():
            if info.filename != part:
                z.writestr(info.filename, good.read(info.filename))
        with z.open(part, "w", force_zip64=True) as f:
            if part.endswith("sharedStrings.xml"):
                f.write(b'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">')
            piece = chunk * (1024 * 1024 // len(chunk))
            for _ in range(megabytes):
                f.write(piece)
            if part.endswith("sharedStrings.xml"):
                f.write(b"</sst>")
    return out.getvalue()


@pytest.mark.parametrize(
    ("part", "chunk"),
    [("xl/sharedStrings.xml", b"<si><t>a</t></si>"), ("xl/worksheets/sheet1.xml", b"\x00")],
)
def test_spreadsheet_bomb_is_refused_before_it_is_parsed(
    part: str, chunk: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tracemalloc

    import openpyxl

    data = bomb(part, chunk, 60)
    assert len(data) < 1024 * 1024
    assert intake.sniff(data) == "xlsx"

    def never(*args: object, **kw: object) -> None:
        raise AssertionError("openpyxl was asked to parse a bomb")

    monkeypatch.setattr(openpyxl, "load_workbook", never)
    tracemalloc.start()
    try:
        with pytest.raises(intake.IntakeError) as caught:
            intake.read("o.xlsx", data)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert caught.value.code == "e_unpacked"
    assert peak < 16 * 1024 * 1024, peak


def test_unzip_bounds_count_every_part_and_the_ratio() -> None:
    many = io.BytesIO()
    with zipfile.ZipFile(many, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("xl/workbook.xml", "<workbook/>")
        for n in range(4):
            z.writestr(f"xl/media/p{n}.bin", b"\x00" * (15 * 1024 * 1024))  # 60 MB in four parts
    with pytest.raises(intake.IntakeError) as caught:
        intake.unzip_bounds(many.getvalue())
    assert caught.value.code == "e_unpacked"
    # an ordinary order is far inside every bound
    intake.unzip_bounds(xlsx([["Товар", "Количество"]] + [[f"Кабель {n}", n] for n in range(500)]))
