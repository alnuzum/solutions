"""Synthesises the audio of the reference set (``samples/truth.json``) with ElevenLabs.

Each command is spoken by the voice named in the file, in its language, and kept as OGG Opus mono
(what Telegram voice notes are). A developer's script: it needs ELEVENLABS_API_KEY and ffmpeg, and it
skips a command whose audio is already there.

    uv run python apps/voice-to-crm/eval/make_audio.py [--force]
"""

import json
import os
import subprocess  # nosec B404: ffmpeg with fixed arguments
import sys
import tempfile
from pathlib import Path

import httpx

SAMPLES = Path(__file__).parent.parent / "src" / "alnuzum_voice" / "samples"
MODEL = "eleven_v4"


def main() -> None:
    key = os.environ.get("ELEVENLABS_API_KEY", "")
    if not key:
        sys.exit("ELEVENLABS_API_KEY is not set")
    force = "--force" in sys.argv
    truth = json.loads((SAMPLES / "truth.json").read_text())
    with httpx.Client(timeout=120) as client:
        for cmd in truth["commands"]:
            out = SAMPLES / f"{cmd['id']}.ogg"
            if out.exists() and not force:
                continue
            resp = client.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{cmd['voice']}",
                params={"output_format": "mp3_44100_128"},
                headers={"xi-api-key": key},
                json={
                    "text": cmd["text"],
                    "model_id": MODEL,
                    "language_code": cmd["lang"],
                    "voice_settings": {"stability": 0.5, "similarity_boost": 0.8},
                },
            )
            if resp.status_code != 200:
                sys.exit(f"{cmd['id']}: {resp.status_code} {resp.text[:200]}")
            with tempfile.NamedTemporaryFile(suffix=".mp3") as mp3:
                mp3.write(resp.content)
                mp3.flush()
                subprocess.run(  # nosec B603 B607
                    ["ffmpeg", "-loglevel", "error", "-y", "-i", mp3.name, "-ac", "1", "-ar", "48000",
                     "-c:a", "libopus", "-b:a", "24k", "-application", "voip", str(out)],
                    check=True,
                )
            print(cmd["id"], out.stat().st_size)


if __name__ == "__main__":
    main()
