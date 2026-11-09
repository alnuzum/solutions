"""The pages of a scan drawn as JPEGs, in a process of its own
(``python -I -m alnuzum_files.render <memory>``).

PDFium decodes the pictures of a page whole whatever the size it draws, and it is not thread-safe: a process
per file has its own PDFium, a memory ceiling of its own and can be killed when it runs over its time. The PDF
comes on stdin; the pages go to stdout, each as a 4-byte big-endian length and the JPEG. Exit codes: 0 drawn,
3 too many pages, 4 not drawable (damaged, or over the memory ceiling).
"""

import io
import struct
import sys

from alnuzum_files.child import limit_memory

MAX_SIDE = 1568
MAX_PAGES = 10
MAX_BYTES = 8 * 1024 * 1024


def draw(data: bytes) -> list[bytes]:
    import pypdfium2 as pdfium
    from PIL import Image

    out: list[bytes] = []
    doc = pdfium.PdfDocument(data)
    try:
        if len(doc) > MAX_PAGES:
            raise OverflowError
        for n in range(len(doc)):
            page = doc[n]
            try:
                w, h = page.get_size()
                if min(w, h) <= 0:
                    raise ValueError("a page without a size")
                picture = page.render(scale=MAX_SIDE / max(w, h)).to_pil().convert("RGB")
                picture.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
                buf = io.BytesIO()
                picture.save(buf, "JPEG", quality=85)
                out.append(buf.getvalue())
            finally:
                page.close()
    finally:
        doc.close()
    return out


def main(argv: list[str]) -> int:
    limit_memory(int(argv[1]))
    data = sys.stdin.buffer.read(MAX_BYTES + 1)
    try:
        pages = draw(data)
    except OverflowError:
        return 3
    except Exception:  # whatever PDFium or the memory ceiling stops, the file is refused
        return 4
    for jpeg in pages:
        sys.stdout.buffer.write(struct.pack(">I", len(jpeg)) + jpeg)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
