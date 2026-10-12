"""Wake word ("Hey Claude"): a tiny keyword spotter behind a volume gate, cheap enough for a laptop on battery.

- A volume gate (RMS of 30 ms blocks against an adaptive noise floor) decides when someone speaks;
  silence costs next to nothing.
- Only while there is sound, a 3.3M-parameter streaming keyword spotter (sherpa-onnx, Apache-2.0,
  model Apache-2.0) listens for the phrase. Keywords are plain text: no training needed.
- A hit returns the whole utterance; the caller confirms it with Whisper, so a false alarm never
  sends anything.

The model (~5 MB) is downloaded on first use into ~/.agent-voice/kws.
"""

from __future__ import annotations

import collections
import logging
import queue
import shutil
import tarfile
import tempfile
import threading
import urllib.request
from pathlib import Path

import numpy as np

from agent_voice.config import HOME

log = logging.getLogger("agent_voice.wakeword")

RATE = 16000
BLOCK = int(RATE * 0.03)  # 30 ms
MODEL = "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"
MODEL_URL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/{MODEL}.tar.bz2"
KWS_DIR = HOME / "kws"
SUFFIX = "epoch-12-avg-2-chunk-16-left-64.int8.onnx"
FILES = ("tokens.txt", f"encoder-{SUFFIX}", f"decoder-{SUFFIX}", f"joiner-{SUFFIX}")
# The model's BPE spelling of each phrase, including how "Claude" comes out with a French accent
# ("Klod"). Generous on purpose: Whisper confirms every hit before anything is sent, so a false
# alarm only costs one short transcription.
KEYWORDS = (
    "▁HE Y ▁C LA U DE @HEY_CLAUDE\n"
    "▁O K ▁C LA U DE @OK_CLAUDE\n"
    "▁OKAY ▁C LA U DE @OKAY_CLAUDE\n"
    "▁HA Y ▁C LA U DE @HAY_CLAUDE\n"
    "▁HI ▁C LA U DE @HI_CLAUDE\n"
    "▁E H ▁C LA U DE @EH_CLAUDE\n"
    "▁HE Y ▁C LO U D @HEY_CLOUD\n"
    "▁HE Y ▁C LO D @HEY_CLOD\n"
    "▁HE Y ▁C LO DE @HEY_CLODE\n"
    "▁HA Y ▁C LO D @HAY_CLOD\n"
    "▁HE Y ▁C LA W ED @HEY_CLAWED\n"
    "▁HE Y ▁C LA W @HEY_CLAW\n"
)


def ensure_model() -> Path:
    """The keyword-spotter files, downloading them the first time (int8 weights only)."""
    if all((KWS_DIR / f).exists() for f in FILES):
        return KWS_DIR
    KWS_DIR.mkdir(parents=True, exist_ok=True)
    log.info("downloading the wake-word model")
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "kws.tar.bz2"
        with urllib.request.urlopen(MODEL_URL, timeout=60) as resp, archive.open("wb") as out:
            shutil.copyfileobj(resp, out)
        with tarfile.open(archive, "r:bz2") as tar:
            for name in FILES:
                member = tar.getmember(f"{MODEL}/{name}")
                with tar.extractfile(member) as src, (KWS_DIR / name).open("wb") as dst:
                    shutil.copyfileobj(src, dst)
    (KWS_DIR / "keywords.txt").write_text(KEYWORDS, encoding="utf-8")
    return KWS_DIR


def _rms(block: np.ndarray) -> float:
    return float(np.sqrt(np.mean(block * block)))


class WakeListener:
    def __init__(self) -> None:
        import sherpa_onnx

        d = ensure_model()
        (d / "keywords.txt").write_text(KEYWORDS, encoding="utf-8")
        self.spotter = sherpa_onnx.KeywordSpotter(
            tokens=str(d / FILES[0]), encoder=str(d / FILES[1]), decoder=str(d / FILES[2]), joiner=str(d / FILES[3]),
            keywords_file=str(d / "keywords.txt"), num_threads=1, provider="cpu",
            keywords_score=2.0, keywords_threshold=0.1,
        )

    def listen(self, cancel: threading.Event, max_seconds: float = 8.0, end_silence: float = 0.7,
               short_fallback: float = 3.0) -> tuple[np.ndarray, bool] | None:
        """Block until a candidate phrase has been said; return (audio, spotted), or None once `cancel` is set.

        `spotted`: the keyword spotter heard the phrase. A short utterance it missed (up to
        `short_fallback` seconds, e.g. "Hey Claude" said with an accent the English model doesn't
        know) is returned too, unspotted, for a quick check with a small Whisper model.

        The volume gate records each utterance (with a little run-up); the spotter checks it once it ends.
        Fed whole utterances it costs ~2% of a core per second of speech; fed continuously, ONNX Runtime
        keeps a core busy between calls.
        """
        import sounddevice as sd

        blocks: queue.Queue = queue.Queue()

        def cb(indata, frames, time_info, status):  # noqa: ARG001
            blocks.put(indata[:, 0].copy())

        ring: collections.deque = collections.deque(maxlen=15)  # 0.45 s of run-up before the gate opens
        floor, utterance, quiet = None, None, 0.0
        with sd.InputStream(samplerate=RATE, channels=1, dtype="float32", blocksize=BLOCK, callback=cb):
            while not cancel.is_set():
                try:
                    b = blocks.get(timeout=0.5)
                except queue.Empty:
                    continue
                level = _rms(b)
                floor = level if floor is None else floor
                loud = level > max(floor * 2.5, 0.004)  # sensitive: a laptop mic, you may be a metre away
                if utterance is None:
                    if not loud:
                        floor = 0.98 * floor + 0.02 * level  # follow the room's noise while nobody talks
                        ring.append(b)
                        continue
                    utterance, quiet = [*ring, b], 0.0
                    continue
                utterance.append(b)
                quiet = 0.0 if loud else quiet + 0.03
                if quiet >= end_silence or len(utterance) * 0.03 >= max_seconds:
                    audio = np.concatenate(utterance)
                    utterance = None
                    ring.clear()
                    if len(audio) > RATE * 0.5:
                        hit = self.heard_keyword(audio)
                        log.info("wake check: %.1f s of speech, keyword %s", len(audio) / RATE, "yes" if hit else "no")
                        if hit or len(audio) <= RATE * short_fallback:
                            return audio, hit
        return None

    def heard_keyword(self, audio: np.ndarray) -> bool:
        stream = self.spotter.create_stream()
        stream.accept_waveform(RATE, audio)
        stream.input_finished()
        while self.spotter.is_ready(stream):
            self.spotter.decode_stream(stream)
            if self.spotter.get_result(stream):
                return True
        return False
