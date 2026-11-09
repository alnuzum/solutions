"""Audio of a voice note, decoded before anything pays for it.

A speech service charges for the audio it hears, not for what a file's header says, so the length is
measured by decoding: in a process of its own (``python -I -m alnuzum_voice.audio``) with its own FFmpeg
(PyAV), a memory ceiling (``RLIMIT_AS`` on Linux) and a kill after ``SECONDS``. It reads the file on
stdin and writes WAV 16 kHz mono 16-bit to stdout. Exit codes: 0 decoded, 3 longer than ``MAX_SECONDS``,
4 no audio in it, 5 not readable.
"""

import io
import math
import resource
import subprocess  # nosec B404: our own module, fixed arguments
import sys
import threading
import wave
from dataclasses import dataclass

MAX_BYTES = 8 * 1024 * 1024
MAX_SECONDS = 120
RATE = 16000
SECONDS = 15.0
MEMORY = 256 * 1024 * 1024
COMMAND = [sys.executable, "-I", "-m", "alnuzum_voice.audio"]
_SLOTS = threading.BoundedSemaphore(2)


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
    import av  # type: ignore[import-not-found,unused-ignore]

    out = io.BytesIO()
    samples = 0
    limit = MAX_SECONDS * RATE
    with av.open(io.BytesIO(data), "r") as container:
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
    try:
        wav, _seconds = decode(data)
    except AudioError as exc:
        return 3 if exc.code == "e_too_long" else 4
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
