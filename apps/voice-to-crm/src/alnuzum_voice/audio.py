"""Audio of a voice note, decoded before anything pays for it.

A speech service charges for the audio it hears, not for what a file's header says, so the length is
measured by decoding: in a process of its own (``python -I -m alnuzum_voice.audio``) with its own FFmpeg
(PyAV), a memory ceiling (``RLIMIT_AS`` on Linux) and a kill after ``SECONDS``. It reads the file on
stdin and writes WAV 16 kHz mono 16-bit to stdout. Exit codes: 0 decoded, 3 longer than ``MAX_SECONDS``,
4 no audio in it, 5 not readable, 6 not one of ``FORMATS``.

FFmpeg guesses a format by content, and some formats (an HLS playlist, ``concat``, ``tee``) make the demuxer
open other addresses: a request from our server, a file of the container. So the format comes only from the
first bytes and only from ``FORMATS``, it is named to FFmpeg, nested opens are refused (``io_open``), and the
child cannot get a new descriptor at all: once FFmpeg is loaded, ``RLIMIT_NOFILE`` sits right over the
descriptors already open, so no file and no socket can be opened while it decodes.
"""

import io
import math
import os
import resource
import subprocess  # nosec B404: our own module, fixed arguments
import sys
import threading
import wave
from dataclasses import dataclass
from typing import Any

MAX_BYTES = 8 * 1024 * 1024
MAX_SECONDS = 120
RATE = 16000
SECONDS = 15.0
MEMORY = 256 * 1024 * 1024
COMMAND = [sys.executable, "-I", "-m", "alnuzum_voice.audio"]
_SLOTS = threading.BoundedSemaphore(2)


def sniff(head: bytes) -> str | None:
    """The container by its signature; anything else is refused before FFmpeg sees it."""
    if head.startswith(b"OggS"):
        return "ogg"  # Telegram voice notes: Opus in Ogg
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[4:8] == b"ftyp":
        return "mp4"  # m4a, mp4 (external references stay off: enable_drefs is 0)
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "matroska"  # webm, mka
    if head.startswith(b"ID3") or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        return "mp3"
    return None


FORMATS = ("ogg", "wav", "mp4", "matroska", "mp3")


def _refuse(url: str, flags: int, options: dict[str, str]) -> None:
    raise PermissionError("a nested open is not allowed")


def _load() -> None:
    """Every module of PyAV now: it imports some lazily (a stream type), which needs a descriptor."""
    import importlib
    import pkgutil

    import av  # type: ignore[import-not-found,unused-ignore]

    for module in pkgutil.walk_packages(av.__path__, "av."):
        importlib.import_module(module.name)


def _seal() -> None:
    """No new descriptor from here on: every number under the limit is taken (gaps get /dev/null), and a
    new descriptor is always the lowest free number."""
    folder = "/proc/self/fd" if os.path.isdir("/proc/self/fd") else "/dev/fd"
    top = max(int(name) for name in os.listdir(folder))
    while (fd := os.open(os.devnull, os.O_RDONLY)) <= top:
        pass  # a gap under the highest descriptor, now taken
    os.close(fd)
    resource.setrlimit(resource.RLIMIT_NOFILE, (fd, fd))


def _open(data: bytes, form: str) -> Any:
    """The named demuxer over the bytes in memory; a nested open of any address is refused."""
    import av  # type: ignore[import-not-found,unused-ignore]

    return av.open(
        io.BytesIO(data),
        "r",
        format=form,
        container_options={"protocol_whitelist": "pipe"},
        io_open=_refuse,
    )


class AudioError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Audio:
    wav: bytes
    seconds: int  # rounded up: what a booking counts


def decode(data: bytes) -> tuple[bytes, float]:
    """The child's work: every audio frame resampled, stopping as soon as the length is over the limit."""
    form = sniff(data[:16])
    if form not in FORMATS:
        raise AudioError("e_format")
    import av  # type: ignore[import-not-found,unused-ignore]

    out = io.BytesIO()
    samples = 0
    limit = MAX_SECONDS * RATE
    with _open(data, form) as container:
        if not container.streams.audio:
            raise AudioError("e_no_audio")
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
        with wave.open(out, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            for frame in container.decode(stream):
                for chunk in resampler.resample(frame):
                    samples += chunk.samples
                    if samples > limit + RATE // 2:
                        raise AudioError("e_too_long")
                    w.writeframes(bytes(chunk.planes[0])[: chunk.samples * 2])
            for chunk in resampler.resample(None):
                samples += chunk.samples
                w.writeframes(bytes(chunk.planes[0])[: chunk.samples * 2])
    if samples < RATE // 4:
        raise AudioError("e_no_audio")
    if samples > limit + RATE // 2:
        raise AudioError("e_too_long")
    return out.getvalue(), samples / RATE


def main(argv: list[str]) -> int:
    limit = int(argv[1]) if len(argv) > 1 else MEMORY
    if sys.platform.startswith("linux"):  # the server; macOS does not lower RLIMIT_AS
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    data = sys.stdin.buffer.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        return 5
    if sniff(data[:16]) is None:
        return 6
    _load()
    _seal()
    try:
        wav, _seconds = decode(data)
    except AudioError as exc:
        return {"e_too_long": 3, "e_format": 6}.get(exc.code, 4)
    except Exception:  # anything FFmpeg cannot read
        return 5
    sys.stdout.buffer.write(wav)
    sys.stdout.buffer.flush()
    return 0


def normalize(data: bytes) -> Audio:
    """Blocking: run it in a thread. Two decodes at a time; a third waits up to ``SECONDS``."""
    if not data:
        raise AudioError("e_no_audio")
    if len(data) > MAX_BYTES:
        raise AudioError("e_too_big")
    if sniff(data[:16]) is None:
        raise AudioError("e_format")
    if not _SLOTS.acquire(timeout=SECONDS):
        raise AudioError("e_busy")
    try:
        done = subprocess.run(  # noqa: S603  # nosec B603 - our own module, fixed arguments, no shell
            [*COMMAND, str(MEMORY)],
            input=data,
            capture_output=True,
            timeout=SECONDS,
            env={"MALLOC_ARENA_MAX": "2"},
            check=False,
        )
    except subprocess.TimeoutExpired:  # run() has killed it
        raise AudioError("e_slow") from None
    finally:
        _SLOTS.release()
    if done.returncode == 3:
        raise AudioError("e_too_long")
    if done.returncode == 4:
        raise AudioError("e_no_audio")
    if done.returncode == 6:
        raise AudioError("e_format")
    if done.returncode != 0 or not done.stdout.startswith(b"RIFF"):
        raise AudioError("e_damaged")
    with wave.open(io.BytesIO(done.stdout), "rb") as w:
        frames, rate = w.getnframes(), w.getframerate()
    seconds = math.ceil(frames / rate)
    if seconds > MAX_SECONDS or rate != RATE:
        raise AudioError("e_too_long")
    return Audio(done.stdout, max(seconds, 1))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
