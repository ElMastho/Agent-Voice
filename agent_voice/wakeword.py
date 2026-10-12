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
# The model's BPE spelling of each phrase (English pronunciation of "Claude").
KEYWORDS = (
    "▁HE Y ▁C LA U DE @HEY_CLAUDE\n"
    "▁O K ▁C LA U DE @OK_CLAUDE\n"
    "▁OKAY ▁C LA U DE @OKAY_CLAUDE\n"
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
            keywords_score=1.5, keywords_threshold=0.2,
        )

    def listen(self, cancel: threading.Event, max_seconds: float = 8.0, end_silence: float = 0.7) -> np.ndarray | None:
        """Block until a phrase containing the wake word has been said; return it, or None once `cancel` is set.

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
                loud = level > max(floor * 3.0, 0.006)
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
                    if len(audio) > RATE * 0.5 and self.heard_keyword(audio):
                        return audio
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
