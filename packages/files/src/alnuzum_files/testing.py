"""Word documents made from parts, for tests and for the reference sets of the solutions."""

import io
import zipfile
# escape writes text into XML we make; nothing here parses XML
from xml.sax.saxutils import escape  # nosec B406

NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '<Override PartName="/word/numbering.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>'
    '<Override PartName="/word/styles.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
    "</Types>"
)
RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
    'officeDocument" Target="word/document.xml"/></Relationships>'
)
DOC_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
    'numbering" Target="numbering.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
    'styles" Target="styles.xml"/></Relationships>'
)
# one multilevel list as Word makes it for a contract: 1. / 1.1. / 1.1.1.
MULTILEVEL = (
    f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:numbering {NS}>'
    '<w:abstractNum w:abstractNumId="1">'
    '<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl>'
    '<w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1.%2."/></w:lvl>'
    '<w:lvl w:ilvl="2"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1.%2.%3."/></w:lvl>'
    "</w:abstractNum>"
    '<w:abstractNum w:abstractNumId="2">'
    '<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="russianLower"/><w:lvlText w:val="%1)"/></w:lvl>'
    "</w:abstractNum>"
    '<w:num w:numId="1"><w:abstractNumId w:val="1"/></w:num>'
    '<w:num w:numId="2"><w:abstractNumId w:val="2"/></w:num>'
    "</w:numbering>"
)
STYLES = (
    f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:styles {NS}>'
    '<w:style w:type="paragraph" w:styleId="Clause"><w:name w:val="Clause"/>'
    '<w:pPr><w:numPr><w:ilvl w:val="1"/><w:numId w:val="1"/></w:numPr></w:pPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="Clause2"><w:basedOn w:val="Clause"/></w:style>'
    "</w:styles>"
)


def run(text: str, *, bold: bool = False) -> str:
    props = "<w:rPr><w:b/></w:rPr>" if bold else ""
    return f'<w:r>{props}<w:t xml:space="preserve">{escape(text)}</w:t></w:r>'


def paragraph(
    *runs: str, num: int | None = None, ilvl: int = 0, style: str | None = None, heading: bool = False
) -> str:
    """``runs``: plain strings become runs; strings starting with ``<`` are taken as XML."""
    props = ""
    if style:
        props += f'<w:pStyle w:val="{style}"/>'
    if num is not None:
        props += f'<w:numPr><w:ilvl w:val="{ilvl}"/><w:numId w:val="{num}"/></w:numPr>'
    if heading:
        props += '<w:jc w:val="center"/>'
    body = "".join(r if r.startswith("<") else run(r, bold=heading) for r in runs)
    return f"<w:p>{'<w:pPr>' + props + '</w:pPr>' if props else ''}{body}</w:p>"


def inserted(text: str) -> str:
    return f'<w:ins w:id="1" w:author="Counterparty">{run(text)}</w:ins>'


def deleted(text: str) -> str:
    return (
        f'<w:del w:id="2" w:author="Counterparty"><w:r><w:delText xml:space="preserve">{escape(text)}'
        "</w:delText></w:r></w:del>"
    )


def table(rows: list[list[str]]) -> str:
    cells = "".join(
        "<w:tr>" + "".join(f"<w:tc>{paragraph(c)}</w:tc>" for c in row) + "</w:tr>" for row in rows
    )
    return f"<w:tbl>{cells}</w:tbl>"


def docx(body: list[str], *, numbering: str | None = MULTILEVEL, styles: str | None = STYLES) -> bytes:
    document = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document {NS}><w:body>'
        + "".join(body)
        + "<w:sectPr/></w:body></w:document>"
    )
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", RELS)
        z.writestr("word/_rels/document.xml.rels", DOC_RELS)
        z.writestr("word/document.xml", document)
        if numbering:
            z.writestr("word/numbering.xml", numbering)
        if styles:
            z.writestr("word/styles.xml", styles)
    return out.getvalue()
