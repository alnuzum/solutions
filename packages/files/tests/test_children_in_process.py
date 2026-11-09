"""What the children run, run here as well: the same answers as through the child, the same exit codes, and
the code is measured (a child gets a bare environment, so coverage does not follow it there)."""

import io
import struct
import sys
from types import SimpleNamespace

import pytest

from alnuzum_files import child, docx, render
from alnuzum_files.testing import deleted, inserted, paragraph, table
from alnuzum_files.testing import docx as make_docx

DOCUMENTS = [
    make_docx(
        [
            paragraph("ДОГОВОР ПОСТАВКИ № 15", heading=True),
            paragraph("Предмет договора", num=1, ilvl=0),
            paragraph("Поставщик обязуется передать товар.", num=1, ilvl=1),
            paragraph("Покупатель обязуется ", "принять и оплатить товар.", num=1, ilvl=1),
            paragraph("в рублях;", num=2),
            paragraph("По стилю пункта", style="Clause2"),
            table([["Поставщик: ООО «Альфа»", "Покупатель: ООО «Бета»"]]),
        ]
    ),
    make_docx([paragraph("Оплата в течение ", deleted("30"), inserted("60"), " дней.", num=1, ilvl=0)]),
    make_docx([paragraph("Без нумерации и стилей")], numbering=None, styles=None),
    make_docx([paragraph(f"Пункт {n} договора поставки.", num=1, ilvl=n % 2) for n in range(40)]),
]


def _scan(pages: int) -> bytes:
    """A PDF of pictures only, as a scanner makes it."""
    from PIL import Image

    sheets = [Image.new("RGB", (620, 877), (255, 255, 255 - 40 * n)) for n in range(pages)]
    buf = io.BytesIO()
    sheets[0].save(buf, "PDF", save_all=True, append_images=sheets[1:], resolution=75)
    return buf.getvalue()


def _run(module: object, argv: list[str], data: bytes, monkeypatch: pytest.MonkeyPatch) -> tuple[int, bytes]:
    """``main`` of a child module in this process: no memory ceiling here, stdin and stdout as bytes."""
    monkeypatch.setattr(child, "limit_memory", lambda limit: None)
    monkeypatch.setattr(module, "limit_memory", lambda limit: None, raising=False)
    out = io.BytesIO()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(data)))
    monkeypatch.setattr(sys, "stdout", SimpleNamespace(buffer=out))
    code = module.main(argv)  # type: ignore[attr-defined]
    return code, out.getvalue()


@pytest.mark.parametrize("n", range(len(DOCUMENTS)))
def test_a_word_document_reads_the_same_here_and_in_its_child(
    n: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = DOCUMENTS[n]
    through_child = docx.read(data, max_chars=200_000)
    here = [tuple(p) for p in docx.paragraphs(data, 200_000)]
    assert here == through_child
    code, out = _run(docx, ["docx", "1", "200000"], data, monkeypatch)
    assert code == 0 and out.startswith(b'{"p": [')


def test_the_word_child_says_too_long_and_damaged_by_its_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run(docx, ["docx", "1", "10"], DOCUMENTS[0], monkeypatch)[0] == 3
    assert _run(docx, ["docx", "1", "1000"], b"PK\x03\x04 not a zip", monkeypatch)[0] == 4


def test_the_pages_of_a_scan_are_drawn_here_as_in_the_child(monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = _scan(2)
    pages = render.draw(pdf)
    assert pages and all(p.startswith(b"\xff\xd8") for p in pages)
    code, out = _run(render, ["render", "1"], pdf, monkeypatch)
    assert code == 0
    sizes, at = [], 0
    while at < len(out):
        (size,) = struct.unpack(">I", out[at : at + 4])
        sizes.append(size)
        at += 4 + size
    assert len(sizes) == len(pages)
    assert _run(render, ["render", "1"], b"%PDF-1.7 broken", monkeypatch)[0] == 4
    monkeypatch.setattr(render, "MAX_PAGES", 0)
    assert _run(render, ["render", "1"], pdf, monkeypatch)[0] == 3
