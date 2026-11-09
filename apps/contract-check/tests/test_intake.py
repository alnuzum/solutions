"""What a contract may be: a Word document with its numbering, a PDF with text, a scan, a text; bounds first."""

import io
import zipfile

import pytest
from alnuzum_contracts import intake
from PIL import Image

from alnuzum_files import FileError
from alnuzum_files.testing import docx, paragraph


def text_pdf(lines: list[str], pages: int = 1) -> bytes:
    """A PDF whose pages carry a text layer (Helvetica, Latin letters)."""
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", ""]
    kids = []
    font = 3 + 2 * pages
    for n in range(pages):
        page, content = 3 + 2 * n, 4 + 2 * n
        kids.append(f"{page} 0 R")
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 {font} 0 R >> >> /Contents {content} 0 R >>"
        )
        body = "BT /F1 11 Tf 50 800 Td 14 TL " + " ".join(f"({ln}) '" for ln in lines) + " ET"
        objects.append(f"<< /Length {len(body)} >>\nstream\n{body}\nendstream")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {pages} >>"
    objects.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out, offsets = io.BytesIO(), []
    out.write(b"%PDF-1.4\n")
    for i, obj in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n{obj}\nendobj\n".encode())
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def picture_pdf(pages: int) -> bytes:
    shots = [Image.new("RGB", (600, 800), "white") for _ in range(pages)]
    out = io.BytesIO()
    shots[0].save(out, "PDF", save_all=True, append_images=shots[1:])
    return out.getvalue()


def test_a_word_document_comes_with_its_numbering() -> None:
    doc = intake.read(
        docx([paragraph("Предмет", num=1), paragraph("Поставщик передаёт товар.", num=1, ilvl=1)])
    )
    assert doc.kind == "docx"
    assert [(b.label, b.key, b.text, b.hard) for b in doc.blocks] == [
        ("1.", "1", "Предмет", True),
        ("1.1.", "1.1", "Поставщик передаёт товар.", True),
    ]


def test_a_pdf_with_text_comes_as_lines_without_page_numbers() -> None:
    lines = [
        "SUPPLY AGREEMENT No. 7",
        "1.1. The Supplier shall deliver the Goods within 10 days of the order and bear the risk until delivery.",
        "Page 1 of 2",
    ]
    doc = intake.read(text_pdf(lines, pages=2))
    assert doc.kind == "pdf" and doc.pages == 2
    texts = [b.text for b in doc.blocks if b.text]
    assert "Page 1 of 2" not in texts and texts.count("SUPPLY AGREEMENT No. 7") == 2
    assert not any(b.hard for b in doc.blocks)


def test_a_pdf_without_text_is_a_scan_drawn_as_pictures() -> None:
    doc = intake.read(picture_pdf(2))
    assert doc.kind == "scan" and doc.pages == 2
    assert all(p.startswith(b"\xff\xd8\xff") for p in doc.images)


def test_a_scan_longer_than_ten_pages_is_refused_before_it_is_drawn(monkeypatch: pytest.MonkeyPatch) -> None:
    from alnuzum_files import pdf

    monkeypatch.setattr(pdf, "rasterize", lambda *a, **k: (_ for _ in ()).throw(AssertionError("drawn")))
    with pytest.raises(FileError) as caught:
        intake.read(picture_pdf(11))
    assert caught.value.code == "e_scan_pages"


def test_a_photo_is_a_one_page_scan() -> None:
    out = io.BytesIO()
    Image.new("RGB", (2000, 3000), "white").save(out, "JPEG")
    doc = intake.read(out.getvalue())
    assert doc.kind == "scan" and doc.pages == 1 and len(doc.images) == 1


def test_a_text_in_either_encoding() -> None:
    body = "ДОГОВОР\n1. Предмет\n\nСтраница 2\n1.1. Товар."
    for enc in ("utf-8", "cp1251"):
        doc = intake.read(body.encode(enc))
        assert [b.text for b in doc.blocks] == ["ДОГОВОР", "1. Предмет", "", "1.1. Товар."]


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"", "e_empty"),
        (b"x" * (intake.MAX_BYTES + 1), "e_size"),
        (b"\x00\x01\x02 binary", "e_type"),
        (("Пункт договора. " * 9000).encode(), "e_too_long"),
        (b"   \n\n  ", "e_empty"),
    ],
)
def test_refusals(data: bytes, code: str) -> None:
    with pytest.raises(FileError) as caught:
        intake.read(data)
    assert caught.value.code == code


def test_a_pdf_over_forty_pages_and_a_spreadsheet_are_refused() -> None:
    with pytest.raises(FileError) as caught:
        intake.read(text_pdf(["x"], pages=41))
    assert caught.value.code == "e_pages"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml", "<workbook/>")
    with pytest.raises(FileError) as caught:
        intake.read(buf.getvalue())
    assert caught.value.code == "e_type"


def test_paragraphs_read_off_a_scan_are_bounded() -> None:
    assert [b.text for b in intake.from_paragraphs(["  1.1.  Товар ", "", "2. Цена"])] == [
        "1.1. Товар",
        "2. Цена",
    ]
    with pytest.raises(FileError):
        intake.from_paragraphs(["", " "])
    with pytest.raises(FileError):
        intake.from_paragraphs(["x" * 1000] * 200)
