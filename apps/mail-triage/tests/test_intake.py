"""A letter in: the MIME tree read in a child process with its ceilings, the history set aside and counted,
attachments read or listed, a pasted letter's header lines; nothing cut without saying so."""

import base64
from email.message import EmailMessage
from typing import Any

import pytest
from alnuzum_mail import intake, worker
from alnuzum_mail.intake import Letter, from_eml, from_text, split_history
from alnuzum_mail.service import sample_bytes

from alnuzum_files import FileError
from alnuzum_files.testing import docx, paragraph


def eml(body: str = "Добрый день, просим КП на щиты учёта.", **headers: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = headers.pop("From", "Алишер Каримов <a.karimov@romashka-demo.uz>")
    msg["To"] = "sales@alfa-elektro-demo.uz"
    msg["Subject"] = headers.pop("Subject", "Запрос КП")
    for key, value in headers.items():
        msg[key.replace("_", "-")] = value
    msg.set_content(body)
    return msg


def test_the_samples_open() -> None:
    letter = from_eml(sample_bytes("new-signature.eml"))
    assert letter.from_email == "a.karimov@romashka-demo.uz" and letter.from_name == "Алишер Каримов"
    assert "ИНН 305123456" in letter.body and not letter.reply_hint and not letter.bulk
    reply = from_eml(sample_bytes("reply.eml"))
    assert reply.reply_hint and reply.history > 100 and "Итого" not in reply.body
    junk = from_eml(sample_bytes("junk.eml"))
    assert junk.bulk
    card = from_eml(sample_bytes("new-pdf-card.eml"))
    (att,) = card.attachments
    assert att.kind == "pdf" and "306234567" in att.text and att.id == "a1"
    assert card.sources()["a1"] == att.text


def test_a_letter_survives_its_json() -> None:
    letter = from_eml(sample_bytes("new-pdf-card.eml"))
    again = Letter.of(letter.to_json())
    assert again == Letter(**{**again.__dict__, "images": []}) and again.attachments == letter.attachments


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        ("Добрый день, смету посмотрели, всё хорошо.\n\n> старое письмо\n> ещё", 2),
        ("Добрый день, смету посмотрели, всё хорошо.\n-----Original Message-----\nFrom: x", 2),
        ("Добрый день, смету посмотрели, всё хорошо.\nOn Mon, 9 Nov 2026 at 10:00, Ann wrote:\n> a", 2),
        ("Добрый день, смету посмотрели, всё хорошо.\nОт: Альфа\nОтправлено: вчера\nТема: смета\nтекст", 4),
        ("> только цитата без нового текста\n> вторая строка", 0),
    ],
)
def test_the_history_of_earlier_letters_is_set_aside_and_counted(text: str, kept: int) -> None:
    body, history = split_history(text)
    if kept:
        assert body == "Добрый день, смету посмотрели, всё хорошо." and history > 0
    else:
        assert history == 0 and body == text.strip()


def test_a_pasted_letter_reads_its_header_lines() -> None:
    letter = from_text(
        "From: Olga Kim <o.kim@tcm-demo.uz>\r\nSubject: Re: quote\r\nDate: today\r\n\r\nThank you, we agree to the price."
    )
    assert (letter.from_email, letter.subject, letter.date, letter.reply_hint) == (
        "o.kim@tcm-demo.uz",
        "Re: quote",
        "today",
        True,
    )
    assert letter.source == "text" and letter.body == "Thank you, we agree to the price."
    plain = from_text("Тема: без адреса\nПросим счёт.")
    assert plain.from_email == "" and plain.subject == "без адреса"


def test_a_forwarded_letter_keeps_what_it_carries() -> None:
    letter = from_text(
        "Тема: Fwd: запрос\n\nСм. ниже\n---------- Forwarded message ---------\nОт: client@x.uz\nПросим КП на щиты."
    )
    assert "Просим КП на щиты." in letter.body and letter.history == 0


def test_too_long_or_empty_is_refused_never_cut() -> None:
    with pytest.raises(FileError, match="e_too_long"):
        from_text("а" * (intake.MAX_PASTE + 1))
    with pytest.raises(FileError, match="e_too_long"):
        from_text("слово " * 4000)
    with pytest.raises(FileError, match="e_empty"):
        from_text("Тема: пусто\n\n   ")
    with pytest.raises(FileError, match="e_empty"):
        intake.read(None, "  ")
    with pytest.raises(FileError, match="e_size"):
        from_eml(b"x" * (intake.MAX_BYTES + 1))
    with pytest.raises(FileError, match="e_empty"):
        from_eml(b"")
    with pytest.raises(FileError, match="e_not_mail"):
        from_eml(b"%PDF-1.7\n")
    with pytest.raises(FileError, match="e_too_long"):
        from_eml(bytes(eml("слово " * 4000)))


def nested(depth: int) -> bytes:
    inner = "Content-Type: text/plain\n\nглубоко\n"
    for n in range(depth):
        inner = f'Content-Type: multipart/mixed; boundary="b{n}"\n\n--b{n}\n{inner}\n--b{n}--\n'
    return ("From: a@b.uz\nSubject: x\nMIME-Version: 1.0\n" + inner).encode()


def test_a_letter_too_deep_or_of_too_many_parts_is_refused_in_the_child() -> None:
    with pytest.raises(FileError, match="e_mime"):
        from_eml(nested(worker.MAX_DEPTH + 2))
    parts = "".join(f"--b\nContent-Type: text/plain\n\n{n}\n" for n in range(worker.MAX_PARTS + 1))
    many = f'From: a@b.uz\nSubject: x\nContent-Type: multipart/mixed; boundary="b"\n\n{parts}--b--\n'.encode()
    with pytest.raises(FileError, match="e_mime"):
        from_eml(many)


def test_the_child_s_failure_is_a_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker, "COMMAND", ["/usr/bin/false"])
    with pytest.raises(FileError):
        worker.read_eml(bytes(eml()))


def test_html_becomes_text_without_scripts_or_styles() -> None:
    text = worker.html_text(
        "<html><head><title>t</title><style>p{}</style></head><body><p>Добрый&nbsp;день</p>"
        "<script>alert(1)</script><table><tr><td>ИНН</td><td>305123456</td></tr></table><br>конец</body></html>"
    )
    assert text == "Добрый день\n\nИНН 305123456\n\nконец"


def test_attachments_read_listed_or_handed_back() -> None:
    msg = eml()
    msg.add_attachment(b"%PDF-1.4 broken", maintype="application", subtype="pdf", filename="bad.pdf")
    msg.add_attachment("ИНН 305123456".encode("cp1251"), maintype="text", subtype="plain", filename="req.txt")
    msg.add_attachment(b"\x89PNG\r\n\x1a\n" + b"0" * 40, maintype="image", subtype="png", filename="card.png")
    msg.add_attachment(b"MZ\x90\x00", maintype="application", subtype="octet-stream", filename="x.exe")
    out = worker.open_letter(bytes(msg))
    got = [(a["name"], a["kind"]) for a in out["attachments"]]
    assert got == [("bad.pdf", "pdf"), ("req.txt", "text"), ("card.png", "image"), ("x.exe", "skipped")]
    assert out["attachments"][0]["error"]
    assert "data" in out["attachments"][2]


def test_a_text_attachment_in_its_charset_and_a_word_document() -> None:
    msg = eml()
    part_text = "ИНН 305123456, ООО «Ромашка»"
    msg.add_attachment(part_text.encode("cp1251"), maintype="text", subtype="plain", filename="r.txt")
    msg.get_payload()[1].set_param("charset", "cp1251")  # type: ignore[index, union-attr]
    msg.add_attachment(
        docx([paragraph("Карточка: ООО «Ромашка», ИНН 305123456")], numbering=None),
        maintype="application",
        subtype="vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename="card.docx",
    )
    letter = from_eml(bytes(msg))
    assert [a.kind for a in letter.attachments] == ["text", "docx"]
    assert "ООО «Ромашка»" in letter.attachments[0].text
    assert "305123456" in letter.attachments[1].text


def test_a_long_attachment_is_read_from_its_start_and_says_so() -> None:
    msg = eml()
    msg.add_attachment(
        ("строка текста " * 1000).encode(), maintype="text", subtype="plain", filename="long.txt"
    )
    (att,) = from_eml(bytes(msg)).attachments
    assert len(att.text) == intake.MAX_ATTACHMENT_TEXT and att.cut > 0


def test_pictures_go_to_the_model_at_most_four(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(intake.image_mod, "normalize", lambda data: b"jpeg")
    monkeypatch.setattr(intake.pdf_mod, "rasterize", lambda data, max_pages: [b"p1", b"p2", b"p3"])
    images: list[tuple[str, bytes]] = []
    raw: dict[str, Any] = {"name": "s.pdf", "kind": "scan", "data": base64.b64encode(b"x").decode()}
    first = intake._read_attachment(1, raw, images)
    second = intake._read_attachment(2, raw, images)
    pic = intake._read_attachment(3, {"name": "p.png", "kind": "image", "data": "eA=="}, images)
    assert (first.pictures, second.pictures, pic.pictures) == (2, 2, 0)
    assert [i for i, _ in images] == ["a1", "a1", "a2", "a2"]


def test_a_letter_of_attachments_only_and_one_of_nothing() -> None:
    msg = EmailMessage()
    msg["From"] = "a@b.uz"
    msg["Subject"] = "карточка"
    msg.add_attachment(b"\x89PNG\r\n\x1a\n" + b"0" * 40, maintype="image", subtype="png", filename="c.png")
    with pytest.raises(FileError):  # a picture that is not one: nothing left to read
        from_eml(bytes(msg))
    empty = EmailMessage()
    empty["From"] = "a@b.uz"
    empty.set_content("   ")
    with pytest.raises(FileError, match="e_empty"):
        from_eml(bytes(empty))
