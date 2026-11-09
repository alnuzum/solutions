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
    assert (scan.kind, scan.media_type, scan.pages, scan.lines) == ("image", "application/pdf", 2, [])
    assert scan.blob is not None and scan.blob.startswith(b"%PDF-")


def test_picture_over_the_pixel_bound_is_refused_and_a_big_one_is_shrunk() -> None:
    with pytest.raises(intake.IntakeError) as caught:
        intake.read("huge.png", image(8000, 5001))
    assert caught.value.code == "e_image_size"
    src = intake.read("photo.webp", image(3000, 1000, "WEBP"))
    assert (src.kind, src.media_type) == ("image", "image/jpeg")
    assert src.blob is not None
    with Image.open(io.BytesIO(src.blob)) as img:
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
