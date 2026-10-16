"""Piper-based speaker: synthesizes sentence by sentence while the previous one plays."""

from __future__ import annotations

import logging
import queue
import threading
import time

import numpy as np
import sounddevice as sd

from agent_voice import lang as langdet
from agent_voice import textclean
from agent_voice.config import VOICES_DIR

log = logging.getLogger("agent_voice.tts")

KOKORO = "kokoro:"  # a voice spec for the natural engine: "kokoro:af_heart" (Piper's are "en_US-hfc_male-medium")


def download_voice(name: str) -> None:
    from piper.download_voices import download_voice as _download

    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    _download(name, VOICES_DIR)


def _quiet_session(path):
    """An ONNX Runtime session that doesn't busy-wait: battery matters more than a few milliseconds.

    By default ONNX Runtime's worker threads spin between and after runs: measured here, one sentence
    cost ~9 s of CPU (most of it spinning after the audio was ready) against ~0.4 s with 2 threads and
    no spinning, which also synthesises faster.
    """
    import onnxruntime as ort

    opts = ort.SessionOptions()
    opts.intra_op_num_threads = 2
    opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
    opts.add_session_config_entry("session.inter_op.allow_spinning", "0")
    return ort.InferenceSession(str(path), sess_options=opts, providers=["CPUExecutionProvider"])


class Speaker:
    def __init__(self, config: dict):
        self.config = config
        self._voices: dict[str, object] = {}
        self._voices_lock = threading.Lock()
        self._gen = 0
        self._text_q: queue.Queue = queue.Queue()
        self._audio_q: queue.Queue = queue.Queue(maxsize=4)
        self._playing = threading.Event()
        # Cleared while the user is talking: the mic has priority, nothing plays until released.
        self._allowed = threading.Event()
        self._allowed.set()
        self._holds = 0  # several mic sessions can overlap (auto-listen + push-to-talk)
        self._hold_lock = threading.Lock()
        self._synthesizing = threading.Event()
        self.current_spec = ""
        self.current_text = ""  # the sentence being played, for the widget's live caption
        threading.Thread(target=self._synth_loop, name="synth", daemon=True).start()
        threading.Thread(target=self._play_loop, name="play", daemon=True).start()

    # -- voices -----------------------------------------------------------------
    def _spec(self, lang: str) -> str:
        return self.config["voices"].get(lang) or self.config["voices"][self.config["default_lang"]]

    def _voice(self, spec: str):
        """Return (PiperVoice, speaker_id). A voice spec may name a speaker: "en_GB-semaine-medium:obadiah"."""
        name, _, speaker = spec.partition(":")
        with self._voices_lock:
            if name not in self._voices:
                from piper import PiperVoice

                path = VOICES_DIR / f"{name}.onnx"
                if not path.exists():
                    log.info("downloading voice %s", name)
                    download_voice(name)
                voice = PiperVoice.load(path)
                voice.session = _quiet_session(path)
                self._voices[name] = voice
            voice = self._voices[name]
        speaker_id = voice.config.speaker_id_map.get(speaker) if speaker else None
        return voice, speaker_id

    def warm_up(self) -> None:
        from agent_voice import kokoro

        for lang in self.config["voices"]:
            spec = self._spec(lang)
            if spec.startswith(KOKORO):
                if kokoro.ready():  # never a download here: the service isn't answering yet
                    kokoro.engine()
            else:
                self._voice(spec)
        langdet.warm_up()

    @property
    def current_lang(self) -> str:
        """The language of the sentence playing ('en', 'fr'), whichever engine reads it."""
        spec = self.current_spec
        if spec.startswith(KOKORO):
            from agent_voice.kokoro import LANGS

            return LANGS.get(spec[len(KOKORO):][:1], "")[:2]
        return spec[:2]

    # -- public API -------------------------------------------------------------
    def speak(self, text: str, lang: str | None = None, voice: str | None = None, pitch: float = 1.0) -> int:
        """Queue text for speaking (optionally forcing a voice spec). Returns the number of sentences queued.

        `pitch` plays it a little lower or higher (and slower or faster): one voice per conversation, free.
        """
        cleaned = textclean.truncate(textclean.clean(text), int(self.config["max_chars"]))
        paras = textclean.paragraphs(cleaned)
        if not paras:
            return 0
        units = [s for p in paras for s in textclean.sentences(p)]
        segments = [(lang, s) for s in units] if lang else langdet.segment(units, self.config["default_lang"])
        gen = self._gen
        for seg_lang, sentence in segments:
            self._text_q.put((gen, voice or self._spec(seg_lang), sentence, pitch))
        return len(segments)

    def hold(self) -> None:
        """Silence now and keep anything that arrives queued until release()."""
        with self._hold_lock:
            self._holds += 1
            self._allowed.clear()
        sd.stop()

    def release(self) -> None:
        with self._hold_lock:
            self._holds = max(0, self._holds - 1)
            if self._holds == 0:
                self._allowed.set()

    def stop(self) -> None:
        self._gen += 1
        for q in (self._text_q, self._audio_q):
            try:
                while True:
                    q.get_nowait()
            except queue.Empty:
                pass
        sd.stop()

    @property
    def busy(self) -> bool:
        return self._playing.is_set() or self._synthesizing.is_set() or not self._text_q.empty() or not self._audio_q.empty()

    # -- workers ----------------------------------------------------------------
    def _synth_loop(self) -> None:
        while True:
            gen, spec, sentence, pitch = self._text_q.get()
            if gen != self._gen:
                continue
            self._synthesizing.set()
            try:
                audio, rate = self._synthesize(spec, sentence)
                if not len(audio) or gen != self._gen:
                    continue
                # A short pause between sentences sounds more natural.
                audio = np.concatenate([audio, np.zeros(int(rate * 0.12), dtype=np.float32)])
                self._audio_q.put((gen, audio, int(rate * pitch), spec, sentence))
            except Exception:  # never let one bad sentence kill the worker
                log.exception("synthesis failed for %r", sentence)
            finally:
                self._synthesizing.clear()

    def _synthesize(self, spec: str, sentence: str):
        """(float32 audio, sample rate) for one sentence, by Piper or (a "kokoro:" spec) Kokoro."""
        speed, volume = float(self.config["speed"]), float(self.config["volume"])
        if spec.startswith(KOKORO):
            from agent_voice import kokoro

            audio, rate = kokoro.synthesize(sentence, spec[len(KOKORO):], speed=1.0 / speed)  # its speed: a rate
            return (np.asarray(audio, dtype=np.float32) * volume), int(rate)
        from piper import SynthesisConfig

        voice, speaker_id = self._voice(spec)
        chunks = list(voice.synthesize(sentence, syn_config=SynthesisConfig(speaker_id=speaker_id, length_scale=speed,
                                                                           volume=volume)))
        if not chunks:
            return np.zeros(0, dtype=np.float32), 22050
        return np.concatenate([c.audio_float_array for c in chunks]).astype(np.float32), int(chunks[0].sample_rate)

    def _play_loop(self) -> None:
        while True:
            gen, audio, rate, spec, sentence = self._audio_q.get()
            self._allowed.wait()
            if gen != self._gen:  # stopped (e.g. a new prompt was sent) while on hold
                continue
            self.current_spec = spec
            self.current_text = sentence
            self._playing.set()
            try:
                sd.play(audio, rate)
                # Poll instead of sd.wait(): a broken audio driver must not freeze the speaker forever.
                deadline = time.time() + len(audio) / rate + 2.0
                while time.time() < deadline and sd.get_stream().active:
                    time.sleep(0.05)
                if time.time() >= deadline:
                    log.warning("audio device did not finish playback in time; skipping")
                    sd.stop()
            except Exception:
                log.exception("playback failed")
            finally:
                self._playing.clear()
