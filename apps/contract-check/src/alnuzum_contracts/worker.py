"""What may run away with time or memory on a stranger's contract, in a process of its own
(``python -I -m alnuzum_contracts.worker <memory> pdf|compare``): the text of a PDF, and the comparison of
two drafts (pairing clauses and the word-by-word walk). A memory ceiling, and the parent kills it on time,
which it cannot do to a thread.

``pdf``: the PDF on stdin; out ``{"kind": "pdf", "blocks": [...], "pages": n}`` or ``{"kind": "scan"}``
(no text layer: its pages are drawn by the renderer). ``compare``: ``{"ours": [...], "theirs": [...]}`` on
stdin; out the comparison. A refusal is ``{"error": code}``; exit 4 is what the parser or the ceiling stopped.
"""

import json
import sys
from typing import Any

from alnuzum_files import FileError
from alnuzum_files.child import limit_memory


def _pdf(data: bytes) -> dict[str, Any]:
    from alnuzum_contracts import intake
    from alnuzum_files import pdf as pdf_mod

    reader, texts = pdf_mod.open_pdf(data, intake.MAX_PAGES)
    if sum(len("".join(t.split())) for t in texts) < intake.MIN_TEXT:
        if len(texts) > intake.MAX_SCAN_PAGES:
            raise FileError("e_scan_pages")
        pdf_mod.pixels(reader)
        return {"kind": "scan"}
    blocks = intake._lines("\n".join(texts))
    return {"kind": "pdf", "pages": len(texts), "blocks": [[b.label, b.key, b.text, b.hard] for b in blocks]}


def _compare(data: bytes) -> dict[str, Any]:
    from alnuzum_contracts.clauses import Clause, compare

    found = json.loads(data)
    ours = [Clause(*c) for c in found["ours"]]
    theirs = [Clause(*c) for c in found["theirs"]]
    return compare(ours, theirs).to_json()


# --- the parent's side ---

COMMAND = [sys.executable, "-I", "-m", "alnuzum_contracts.worker"]


def _ask(op: str, data: bytes, codes: dict[int, str] | None = None) -> dict[str, Any]:
    from alnuzum_files import child

    found: dict[str, Any] = json.loads(child.run([*COMMAND, str(child.MEMORY), op], data, codes=codes))
    if "error" in found:
        raise FileError(str(found["error"]))
    return found


def read_pdf(data: bytes) -> dict[str, Any]:
    return _ask("pdf", data)


def compare_apart(ours: list[Any], theirs: list[Any]) -> Any:
    """``clauses.compare`` in the child; a walk the ceiling stops is a file that takes too long."""
    from alnuzum_contracts.clauses import Comparison

    data = {
        "ours": [[c.label, c.key, c.text] for c in ours],
        "theirs": [[c.label, c.key, c.text] for c in theirs],
    }
    return Comparison.of(_ask("compare", json.dumps(data, ensure_ascii=False).encode(), {4: "e_slow"}))


def main(argv: list[str]) -> int:
    limit_memory(int(argv[1]))
    data = sys.stdin.buffer.read()
    try:
        out = _pdf(data) if argv[2] == "pdf" else _compare(data)
    except FileError as exc:
        out = {"error": exc.code}
    except Exception:  # whatever the parser or the memory ceiling stops, the file is refused
        return 4
    sys.stdout.buffer.write(json.dumps(out, ensure_ascii=False).encode())
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
