"""The natural voice: Kokoro (82M parameters, Apache-2.0), run locally through kokoro-onnx.

More expressive than Piper and still offline, for about three times the CPU per sentence (~1.2 s for a 3-second
sentence on a laptop, against ~0.4 s): opt-in, Piper stays the default. The model (~90 MB, int8) and its voices
(~28 MB) are downloaded once, checked against pinned SHA-256 sums, into ~/.agent-voice/voices/kokoro/.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import urllib.request

from agent_voice.config import VOICES_DIR

log = logging.getLogger(__name__)

FOLDER = VOICES_DIR / "kokoro"
RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
FILES = {  # name -> (size, sha256): anything else is refused
    "kokoro-v1.0.int8.onnx": (92361271, "6e742170d309016e5891a994e1ce1559c702a2ccd0075e67ef7157974f6406cb"),
    "voices-v1.0.bin": (28214398, "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d"),
}
# A voice's first letter is its language: American and British English, French, Spanish, Italian, Portuguese...
LANGS = {"a": "en-us", "b": "en-gb", "f": "fr-fr", "e": "es", "i": "it", "p": "pt-br", "h": "hi", "j": "ja",
         "z": "cmn"}
DEFAULT_VOICES = {"en": "af_heart", "fr": "ff_siwis"}

_engine = None
_lock = threading.Lock()


def available() -> bool:
    try:
        import kokoro_onnx  # noqa: F401
    except Exception:
        return False
    return True


def ready() -> bool:
    """The model is on disk (nothing to download before it can speak)."""
    return all((FOLDER / name).is_file() and (FOLDER / name).stat().st_size == size for name, (size, _) in FILES.items())


def download(progress=None) -> None:
    """Fetch what's missing, verified before it's kept (a partial or altered file is deleted)."""
    FOLDER.mkdir(parents=True, exist_ok=True)
    total = sum(size for size, _ in FILES.values())
    done = 0
    for name, (size, sha) in FILES.items():
        path = FOLDER / name
        if path.is_file() and path.stat().st_size == size:
            done += size
            continue
        part = path.with_suffix(path.suffix + ".part")
        digest = hashlib.sha256()
        with urllib.request.urlopen(RELEASE + name, timeout=30) as reply, part.open("wb") as out:
            while chunk := reply.read(1 << 20):
                out.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if progress:
                    progress(min(1.0, done / total))
        if part.stat().st_size != size or digest.hexdigest() != sha:
            part.unlink(missing_ok=True)
            raise ValueError(f"{name} didn't match its checksum: not kept")
        part.replace(path)
        log.info("kokoro: %s downloaded", name)


def engine():
    """The model, loaded once (and downloaded first if it isn't there)."""
    global _engine
    with _lock:
        if _engine is None:
            from kokoro_onnx import Kokoro

            from agent_voice.tts import _quiet_session

            if not ready():
                download()
            _engine = Kokoro.from_session(_quiet_session(FOLDER / "kokoro-v1.0.int8.onnx"),
                                          str(FOLDER / "voices-v1.0.bin"))
        return _engine


def synthesize(text: str, voice: str, speed: float = 1.0):
    """(float32 audio, sample rate) for one sentence in `voice` ('af_heart'), its language from its name."""
    audio, rate = engine().create(text, voice=voice, speed=speed, lang=LANGS.get(voice[:1], "en-us"))
    return audio, rate
