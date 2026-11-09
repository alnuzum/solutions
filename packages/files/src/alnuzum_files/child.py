"""Work that may run away with memory or time, in a process of its own: a memory ceiling (``RLIMIT_AS``, set
by the child before it loads its parser), killed when it runs over its time, which a thread cannot be.

One child at a time for the whole platform process: each may take 256 MB and the container has 512 MB, so two
solutions drawing a scan and reading a Word document at once would have the container killed.
"""

import subprocess  # nosec B404 - only to run our own modules with fixed arguments
import threading

from alnuzum_files import FileError

SLOT = threading.BoundedSemaphore(1)
MEMORY = 256 * 1024 * 1024  # measured 66-98 MB for scans of up to ten pages and a 40 Mp picture
SECONDS = 15.0  # below a solution's own 20 s: the child is killed before the request gives up


def run(
    command: list[str], data: bytes, *, seconds: float = SECONDS, codes: dict[int, str] | None = None
) -> bytes:
    """The child's stdout; a non-zero exit is a refusal named by ``codes`` (else ``e_damaged``)."""
    if not SLOT.acquire(timeout=seconds):
        raise FileError("e_busy")
    try:
        done = subprocess.run(  # noqa: S603  # nosec B603 - our own module, fixed arguments, no shell
            command,
            input=data,
            capture_output=True,
            timeout=seconds,
            env={"MALLOC_ARENA_MAX": "2"},
            check=False,
        )
    except subprocess.TimeoutExpired:  # run() has killed it
        raise FileError("e_slow") from None
    finally:
        SLOT.release()
    if done.returncode != 0:
        raise FileError((codes or {}).get(done.returncode, "e_damaged"))
    return done.stdout


def frames(raw: bytes) -> list[bytes]:
    """Items written as a 4-byte big-endian length and the bytes."""
    out = []
    while raw:
        size = int.from_bytes(raw[:4], "big")
        out.append(raw[4 : 4 + size])
        raw = raw[4 + size :]
    return out


def limit_memory(limit: int) -> None:
    """In the child, before the parser is loaded. macOS does not lower RLIMIT_AS: the ceiling holds on the
    server and in CI (Linux)."""
    import resource
    import sys

    if sys.platform.startswith("linux"):
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
