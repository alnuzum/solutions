"""The shared file checks: a Word document is bounded before it is read, read in a child that is killed on
time, and its numbering comes out as Word draws it."""

import io
import os
import sys
import time
import zipfile
from pathlib import Path

import pytest

from alnuzum_files import FileError, child, docx, sniff, unzip_bounds
from alnuzum_files.testing import deleted, inserted, paragraph, table
from alnuzum_files.testing import docx as make_docx


def test_the_type_comes_from_the_content() -> None:
    assert sniff(make_docx([paragraph("Договор")])) == "docx"
    assert sniff(b"%PDF-1.7 ...") == "pdf"
    assert sniff("Договор поставки".encode()) == "text"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("other.xml", "<x/>")
    assert sniff(buf.getvalue()) == "unknown"
    assert sniff(b"PK\x03\x04 broken") == "unknown"
    assert sniff(b"\x00\x01binary") == "unknown"


def test_multilevel_numbering_is_drawn_as_word_draws_it() -> None:
    data = make_docx(
        [
            paragraph("ДОГОВОР ПОСТАВКИ № 15", heading=True),
            paragraph("Предмет договора", num=1, ilvl=0),
            paragraph("Поставщик обязуется передать товар.", num=1, ilvl=1),
            paragraph("Покупатель обязуется ", "принять и оплатить товар.", num=1, ilvl=1),
            paragraph("Цена и порядок расчётов", num=1, ilvl=0),
            paragraph("Оплата в течение 30 рабочих дней.", num=1, ilvl=1),
            paragraph("в рублях;", num=2),
            paragraph("в сумах.", num=2),
            paragraph("По стилю пункта", style="Clause2"),
            table([["Поставщик: ООО «Альфа»", "Покупатель: ООО «Бета»"]]),
        ]
    )
    got = docx.read(data, max_chars=10_000)
    assert got == [
        ("", "", "ДОГОВОР ПОСТАВКИ № 15"),
        ("1.", "1", "Предмет договора"),
        ("1.1.", "1.1", "Поставщик обязуется передать товар."),
        ("1.2.", "1.2", "Покупатель обязуется принять и оплатить товар."),
        ("2.", "2", "Цена и порядок расчётов"),
        ("2.1.", "2.1", "Оплата в течение 30 рабочих дней."),
        ("а)", "а", "в рублях;"),
        ("б)", "б", "в сумах."),
        ("2.2.", "2.2", "По стилю пункта"),
        ("", "", "Поставщик: ООО «Альфа»"),
        ("", "", "Покупатель: ООО «Бета»"),
    ]


def test_a_nested_list_numbered_on_its_own_gets_its_parent_in_the_key() -> None:
    numbering = (
        '<?xml version="1.0"?><w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:abstractNum w:abstractNumId="7"><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/>'
        '<w:lvlText w:val="%1."/></w:lvl><w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="decimal"/>'
        '<w:lvlText w:val="%2."/></w:lvl></w:abstractNum>'
        '<w:abstractNum w:abstractNumId="8"><w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="decimal"/>'
        '<w:lvlText w:val="%2."/></w:lvl></w:abstractNum>'
        '<w:num w:numId="5"><w:abstractNumId w:val="7"/></w:num><w:num w:numId="6"><w:abstractNumId w:val="8"/></w:num>'
        '<w:num w:numId="9"><w:abstractNumId w:val="8"/><w:lvlOverride w:ilvl="1"><w:startOverride w:val="1"/>'
        "</w:lvlOverride></w:num></w:numbering>"
    )
    data = make_docx(
        [
            paragraph("Предмет", num=5, ilvl=0),
            paragraph("Товар", num=6, ilvl=1),
            paragraph("Цена", num=5, ilvl=0),
            paragraph("Оплата", num=9, ilvl=1),
        ],
        numbering=numbering,
    )
    got = docx.read(data, max_chars=10_000)
    assert [(lab, key) for lab, key, _ in got] == [("1.", "1"), ("1.", "1.1"), ("2.", "2"), ("1.", "2.1")]


def test_tracked_changes_are_read_as_accepted_and_a_text_box_once() -> None:
    box = "<w:r><w:pict><w:txbxContent>" + paragraph("в рамке") + "</w:txbxContent></w:pict></w:r>"
    data = make_docx(
        [
            paragraph("Оплата в течение ", deleted("30"), inserted("60"), " дней.", num=1, ilvl=0),
            paragraph("До рамки ", box, " после рамки"),
        ]
    )
    got = docx.read(data, max_chars=10_000)
    assert got[0] == ("1.", "1", "Оплата в течение 60 дней.")
    assert got[1:] == [("", "", "в рамке"), ("", "", "До рамки после рамки")]


def _never(*args: object, **kw: object) -> bytes:
    raise AssertionError("the child was started for a file refused by its zip")


def test_a_zip_bomb_in_a_word_document_is_refused_before_any_child_reads_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(child, "run", _never)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", b"<w:document>" + b" " * (60 * 1024 * 1024) + b"</w:document>")
    assert len(buf.getvalue()) < 1024 * 1024
    with pytest.raises(FileError) as caught:
        docx.read(buf.getvalue(), max_chars=10_000)
    assert caught.value.code == "e_unpacked"
    # a document.xml over its own bound, compressed below the ratio, is refused too
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        z.writestr("word/document.xml", os.urandom(11 * 1024 * 1024))
    with pytest.raises(FileError) as caught:
        unzip_bounds(buf.getvalue(), part_limits=docx.PART_LIMITS)
    assert caught.value.code == "e_unpacked"


def test_entities_and_a_dtd_are_refused() -> None:
    laughs = (
        '<?xml version="1.0"?><!DOCTYPE w [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;">]>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
        "<w:p><w:r><w:t>&b;</w:t></w:r></w:p></w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", laughs)
    with pytest.raises(FileError) as caught:
        docx.read(buf.getvalue(), max_chars=10_000)
    assert caught.value.code == "e_damaged"


def test_a_document_longer_than_allowed_is_refused() -> None:
    data = make_docx([paragraph("Пункт договора " * 20, num=1, ilvl=1) for _ in range(50)])
    with pytest.raises(FileError) as caught:
        docx.read(data, max_chars=5_000)
    assert caught.value.code == "e_too_long"


def test_a_child_that_hangs_is_killed_on_time(tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    hang = "import os, sys, time; open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(60)"
    began = time.monotonic()
    with pytest.raises(FileError) as caught:
        docx.read(
            make_docx([paragraph("Договор")]),
            max_chars=100,
            command=[sys.executable, "-c", hang, str(pidfile)],
            seconds=1.0,
        )
    assert caught.value.code == "e_slow"
    assert time.monotonic() - began < 3
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


def test_one_child_at_a_time_for_the_whole_process() -> None:
    assert child.SLOT.acquire(timeout=0.1)
    try:
        with pytest.raises(FileError) as caught:
            child.run([sys.executable, "-c", "pass"], b"", seconds=0.2)
        assert caught.value.code == "e_busy"
    finally:
        child.SLOT.release()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="RLIMIT_AS is lowered only on Linux")
def test_the_memory_ceiling_stops_a_child_that_grows() -> None:
    grow = "import sys; from alnuzum_files.child import limit_memory; limit_memory(int(sys.argv[1])); x = bytearray(400 * 1024 * 1024)"
    with pytest.raises(FileError) as caught:
        child.run([sys.executable, "-c", grow, str(child.MEMORY)], b"")
    assert caught.value.code == "e_damaged"
