"""Render the README images from the real widget code.

    python scripts/render_docs.py          # writes docs/images/*.png|gif

Frames are drawn offscreen with a controlled clock, then composited over a soft wallpaper: the notch
hangs from its top edge; the floating bar gets a blurred backdrop to mimic the Windows acrylic glass.
Needs Pillow (dev only).
"""

from __future__ import annotations

import math
import os
import random
import sys
from pathlib import Path

os.environ.setdefault("QT_SCALE_FACTOR", "2")  # crisp 2x renders

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageFilter, ImageFont  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / "docs" / "images"

from agent_voice import widget as W  # noqa: E402

S = 2  # scale factor (matches QT_SCALE_FACTOR)


class Clock:
    """Stands in for the `time` module inside the widget: animation time is driven frame by frame."""

    now = 0.0

    def time(self) -> float:
        return self.now

    @staticmethod
    def sleep(seconds: float) -> None:
        import time

        time.sleep(seconds)


CLOCK = Clock()
W.time = CLOCK  # the widget animates from time.time(); drive it frame by frame


def backdrop(w: int, h: int, seed: int = 7) -> Image.Image:
    """A soft, colourful abstract wallpaper so the glass has something to blur."""
    rnd = random.Random(seed)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    img = np.zeros((h, w, 3), np.float32)
    img[:] = (14, 16, 26)
    palette = [(124, 92, 255), (34, 211, 238), (244, 114, 182), (52, 211, 153), (251, 191, 36), (99, 102, 241)]
    for _ in range(9):
        cx, cy = rnd.uniform(0, w), rnd.uniform(0, h)
        r = rnd.uniform(0.18, 0.42) * max(w, h)
        col = np.array(rnd.choice(palette), np.float32)
        a = np.exp(-(((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * r * r))) * rnd.uniform(0.35, 0.7)
        img = img * (1 - a[..., None]) + col * a[..., None]
    noise = np.random.default_rng(seed).normal(0, 2.0, img.shape)
    return Image.fromarray(np.clip(img + noise, 0, 255).astype(np.uint8), "RGB")


def to_pil(q: QImage) -> Image.Image:
    q = q.convertToFormat(QImage.Format_RGBA8888)
    return Image.frombuffer("RGBA", (q.width(), q.height()), bytes(q.constBits()), "raw", "RGBA", 0, 1).copy()


def rounded_mask(size, radius) -> Image.Image:
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=255)
    return m


def glass_composite(bg: Image.Image, widget_img: Image.Image, x: int, y: int) -> Image.Image:
    """Shadow + blurred, tinted backdrop (acrylic) + the widget's own translucent painting."""
    out = bg.convert("RGBA")
    w, h = widget_img.size
    radius = W.RADIUS * S
    shadow = Image.new("RGBA", out.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((x, y + 10, x + w, y + h + 10), radius=radius + 6, fill=(0, 0, 0, 120))
    out = Image.alpha_composite(out, shadow.filter(ImageFilter.GaussianBlur(18)))
    under = bg.crop((x - 40, y - 40, x + w + 40, y + h + 40)).filter(ImageFilter.GaussianBlur(26))
    under = under.crop((40, 40, 40 + w, 40 + h)).convert("RGBA")
    under = Image.alpha_composite(under, Image.new("RGBA", (w, h), (18, 24, 32, 102)))  # acrylic tint
    panel = Image.alpha_composite(under, widget_img)
    out.paste(panel, (x, y), rounded_mask((w, h), radius))
    return out


def make_widget(st: dict | None, expanded: bool = False, notch: bool = False) -> W.VoiceWidget:
    w = W.VoiceWidget()
    w.poller.status.disconnect()
    w.glass = True
    # The mode is set here, whatever mode the widget on this machine was left in.
    w.notch, w.compact = notch, False
    w.expanded = expanded
    w._on_status(st)
    if notch:
        w._reveal, w._reveal_v = 1.0, 0.0
    w._apply_size()
    return w


def settle(w: W.VoiceWidget, seconds: float = 1.2) -> None:
    for _ in range(int(seconds / 0.033)):
        CLOCK.now += 0.033
        w._tick()
    w.title_t, w.flash = 1.0, 0.0


def status(phase: str, **kw) -> dict:
    base = {"phase": phase, "level": 0.0, "speaking_lang": "", "hands_free": False, "muted": phase == "muted",
            "last_heard": "", "last_heard_lang": "", "output": "voice", "speed_x": 1.5}
    base.update(kw)
    return base


def frame(w: W.VoiceWidget) -> Image.Image:
    return to_pil(w.grab().toImage())


def scale_of(w: W.VoiceWidget, shot: Image.Image) -> float:
    """Image pixels per layout pixel (Qt's scale factor times the screen's)."""
    return shot.width / w.width()


def drop(bg: Image.Image, img: Image.Image, x: int, y: int) -> Image.Image:
    """Alpha-composite `img` at (x, y); a negative y (a notch tucked above the screen) is cut off."""
    if y < 0:
        img = img.crop((0, -y, img.width, img.height))
        y = 0
    out = bg.convert("RGBA")
    if img.height > 0:
        out.alpha_composite(img, (x, y))
    return out


def notch_y(w: W.VoiceWidget, k: float) -> int:
    """Where the notch sits relative to the top of the screen (negative while tucked away)."""
    return int(round((w.y() - QApplication.primaryScreen().geometry().top()) * k))


def notch_shot(st: dict, expanded: bool = False, hover: bool = False, seconds: float = 1.6):
    w = make_widget(st, expanded=expanded, notch=True)
    w._hovering = hover
    w._reveal_until = 1e12
    settle(w, seconds)
    shot = frame(w)
    return w, shot, scale_of(w, shot)


THREADS = [
    {"session": "a", "title": "Notch redesign", "color": "#F59E0B", "state": "working", "ago": 20,
     "reply": "I moved the banner under the notch and all the tests pass."},
    {"session": "b", "title": "API refactor", "color": "#34D399", "state": "", "ago": 420,
     "reply": "The new endpoint is live; the client needs a version bump."},
]
AGENTS = {"claude": "on", "codex": "on", "gemini": "off", "cursor": "off", "cline": "missing"}
HISTORY = [
    {"text": "Yes, go ahead and deploy.", "lang": "en"},
    {"text": "Can you add tests for the server?", "lang": "en"},
    {"text": "Peux-tu relancer le widget ?", "lang": "fr"},
]


# -- images -------------------------------------------------------------------------------
def render_hero() -> None:
    _, shot, k = notch_shot(status(
        "speaking", speaking_lang="en", caption="I updated the widget and all the tests pass.",
        thread={"title": "Notch redesign", "color": "#F59E0B"}), seconds=2.2)
    w_, h_ = int(1280 * k), int(480 * k)
    img = drop(backdrop(w_, h_, seed=5), shot, (w_ - shot.width) // 2, 0)
    draw = ImageDraw.Draw(img)
    try:
        title = ImageFont.truetype("seguisb.ttf", int(74 * k))
        sub = ImageFont.truetype("segoeui.ttf", int(28 * k))
        small = ImageFont.truetype("segoeui.ttf", int(18 * k))
    except OSError:
        title = sub = small = ImageFont.load_default()
    for text, font, y, fill in (("agent-voice", title, 180, (255, 255, 255, 255)),
                                ("Your AI coding agents, out loud.", sub, 290, (241, 245, 249, 245)),
                                ("Local  ·  CPU-only  ·  French & English  ·  hands-free", small, 342,
                                 (226, 232, 240, 215))):
        tw = draw.textlength(text, font=font)
        draw.text(((w_ - tw) / 2, int(y * k)), text, font=font, fill=fill)
    img.convert("RGB").save(OUT / "hero.jpg", quality=90, optimize=True)


def render_states() -> None:
    """The notch in its main states, each hanging from the top of a slice of desktop."""
    states = [
        status("idle", last_heard="Yes, go ahead and deploy."),
        status("idle", activity={"session": "a", "title": "Notch redesign", "color": "#F59E0B",
                                 "label": "Edit widget.py", "steps": 14, "elapsed": 102, "others": 1}),
        status("speaking", speaking_lang="fr", caption="J'ai mis à jour le widget, tous les tests passent.",
               thread={"title": "Notch redesign", "color": "#F59E0B"}),
        status("recording", level=0.05, hands_free=True),
        status("transcribing", queued=1),
        status("idle", output="chime", chime=True, in_call="Zoom"),
    ]
    tiles, k = [], 1.0
    for i, st in enumerate(states):
        _, shot, k = notch_shot(st, hover=i in (0, 5))
        tw, th = shot.width + int(60 * k), shot.height + int(24 * k)
        tiles.append(drop(backdrop(tw, th, seed=31 + i), shot, (tw - shot.width) // 2, 0))
    gap = int(16 * k)
    cols, tw, th = 2, tiles[0].width, tiles[0].height
    rows = math.ceil(len(tiles) / cols)
    out = Image.new("RGB", (cols * tw + (cols - 1) * gap, rows * th + (rows - 1) * gap), (13, 17, 23))
    for i, tile in enumerate(tiles):
        r, c = divmod(i, cols)
        out.paste(tile.convert("RGB"), (c * (tw + gap), r * (th + gap)), rounded_mask(tile.size, int(10 * k)))
    out.save(OUT / "states.jpg", quality=90, optimize=True)


def render_panel() -> None:
    st = status("idle", last_heard=HISTORY[0]["text"], last_heard_lang="en", history=HISTORY, agents=AGENTS,
                threads=THREADS, wake=True, approvals=True, summary=True, last_reply={"session": "a"})
    w, shot, k = notch_shot(st, expanded=True, hover=True)
    w.hover, w.hover_t["replay"] = "replay", 1.0
    w.update()
    shot = frame(w)
    pad = int(50 * k)
    img = drop(backdrop(shot.width + 2 * pad, shot.height + pad, seed=3), shot, pad, 0)
    img.convert("RGB").save(OUT / "panel.jpg", quality=92, optimize=True)


def render_banner() -> None:
    """Chimes (or Silent): the notch stays tucked away and the reply drops down as a banner."""
    w = make_widget(status("idle", output="chime", chime=True), notch=True)
    w._reveal, w._reveal_until = 0.0, 0.0
    settle(w, 0.6)
    shot = frame(w)
    k = scale_of(w, shot)
    b = W.Banner()
    b.present({"id": 1, "title": "Notch redesign", "color": "#F59E0B", "session": "a",
               "text": "Done: in chime mode the reply drops down under the notch. Hover to keep it, swipe it up "
                       "to dismiss it, click it to open the conversation."}, int(w.pw))
    b.t = 1.0
    banner = to_pil(b.grab().toImage())
    w_, h_ = banner.width + int(120 * k), banner.height + int(46 * k)
    img = drop(backdrop(w_, h_, seed=8), shot, (w_ - shot.width) // 2, notch_y(w, k))
    img = drop(img, banner, (w_ - banner.width) // 2, int(12 * k))
    img.convert("RGB").save(OUT / "banner.jpg", quality=90, optimize=True)


def render_read_along() -> None:
    """Voice mode: the text being read out hangs under the notch, from the sentence being spoken."""
    text = ("I reworked the notch: a black island with a light on its edge. The panel now uses its full "
            "width, and chime mode shows replies as a banner. All the tests pass.")
    reading = {"text": text, "title": "Notch redesign", "color": "#F59E0B", "session": "a"}
    w, shot, k = notch_shot(status("speaking", speaking_lang="en", thread={"title": "Notch redesign", "color": "#F59E0B"},
                                   caption="I reworked the notch: a black island with a light on its edge.",
                                   reading=reading), seconds=2.0)
    b = w.banner
    b.t = 1.0
    banner = to_pil(b.grab().toImage())
    w_, h_ = shot.width + int(120 * k), shot.height + banner.height + int(40 * k)
    img = drop(backdrop(w_, h_, seed=14), shot, (w_ - shot.width) // 2, 0)
    img = drop(img, banner, (w_ - banner.width) // 2, shot.height + int(8 * k))
    img.convert("RGB").save(OUT / "read-along.jpg", quality=90, optimize=True)


def render_you_said() -> None:
    """What you said, framed in the listening colour, as it goes to the conversation."""
    w, shot, k = notch_shot(status("idle", activity={"session": "a", "title": "Notch redesign", "color": "#F59E0B",
                                                     "label": "Thinking", "steps": 0, "elapsed": 1, "others": 0}))
    b = W.Banner()
    b.present({"id": 2, "kind": "you", "tag": "sent", "title": "Notch redesign", "color": "#F59E0B",
               "session": "a", "text": "Yes, ship it, and bump the version to 0.4.0 while you're at it."},
              int(w.pw))
    b.t = 1.0
    banner = to_pil(b.grab().toImage())
    w_, h_ = shot.width + int(120 * k), shot.height + banner.height + int(40 * k)
    img = drop(backdrop(w_, h_, seed=17), shot, (w_ - shot.width) // 2, 0)
    img = drop(img, banner, (w_ - banner.width) // 2, shot.height + int(8 * k))
    img.convert("RGB").save(OUT / "you-said.jpg", quality=90, optimize=True)


def render_bar() -> None:
    """The floating glass bar, the other way to show it (right-click > Notch to switch)."""
    wd = make_widget(status("speaking", speaking_lang="en"))
    settle(wd, 2.0)
    shot = frame(wd)
    k = scale_of(wd, shot)
    pad = int(30 * k)
    img = backdrop(shot.width + 2 * pad, shot.height + 2 * pad, seed=11)
    glass_composite(img, shot, pad, pad).convert("RGB").save(OUT / "bar.jpg", quality=90, optimize=True)


def render_gif() -> None:
    """One turn, as the notch shows it: work, the answer, your reply, then it tucks itself away."""
    thread = {"title": "Notch redesign", "color": "#F59E0B"}
    act = {"session": "a", "title": "Notch redesign", "color": "#F59E0B", "others": 0}
    steps = ["Read widget.py", "Edit widget.py", "Run pytest", "Edit README.md"]
    lines = ["I reworked the notch: a black island with a light on its edge.", "All the tests pass."]
    script = [("tucked", 0.8), ("working", 3.2), ("speaking", 4.0), ("waiting", 1.0), ("recording", 2.2),
              ("transcribing", 1.0), ("done", 4.4)]
    w = make_widget(status("idle"), notch=True)
    w._reveal, w._reveal_until = 0.0, 0.0
    settle(w, 0.4)
    k = scale_of(w, frame(w))
    width, height = int((W.NOTCH_W + 120) * k), int((W.NOTCH_H + 28) * k)
    bg = backdrop(width, height, seed=21)
    frames, dt = [], 0.07
    rng = random.Random(1)
    t_work = 0.0
    for phase, seconds in script:
        n = int(seconds / dt)
        for i in range(n):
            u = i / max(1, n - 1)
            if phase == "tucked":
                st = status("idle")
            elif phase == "working":
                t_work += dt
                st = status("idle", activity={**act, "label": steps[min(3, int(u * 4))], "steps": 3 + int(u * 9),
                                              "elapsed": 74 + int(t_work * 3)})
            elif phase == "speaking":
                st = status("speaking", speaking_lang="en", thread=thread, caption=lines[0 if u < 0.6 else 1])
            elif phase == "recording":
                level = max(0.0, 0.045 * abs(math.sin(i * 0.45)) + rng.uniform(-0.01, 0.02))
                st = status("recording", level=level, thread=thread)
            elif phase == "done":
                st = status("idle", last_heard="Yes, ship it.", last_heard_lang="en")
            else:
                st = status(phase, thread=thread)
            w._on_status(st)
            for _ in range(2):  # finer animation steps per GIF frame
                CLOCK.now += dt / 2
                w._tick()
            shot = frame(w)
            frames.append(drop(bg, shot, (width - shot.width) // 2, notch_y(w, k)).convert("RGB"))
    target = 640
    small = [f.resize((target, int(f.height * target / f.width)), Image.LANCZOS) for f in frames]
    # One shared palette (from a few varied frames) keeps the file small and the colours steady.
    picks = [small[len(small) * j // 8] for j in range(1, 8)]
    sheet = Image.new("RGB", (target, picks[0].height * len(picks)))
    for i, f in enumerate(picks):
        sheet.paste(f, (0, i * f.height))
    swatch = ImageDraw.Draw(sheet)  # the state colours must survive the palette, however small they are
    for i, colour in enumerate(("#F59E0B", "#8B7CFF", "#22D3EE", "#10E0A0", "#34D399", "#FBBF24", "#94A3B8")):
        swatch.rectangle((i * 12, 0, i * 12 + 11, 11), fill=colour)
    palette = sheet.quantize(colors=128, method=Image.Quantize.MEDIANCUT)
    pal = [f.quantize(palette=palette, dither=Image.Dither.NONE) for f in small]
    pal[0].save(OUT / "demo.gif", save_all=True, append_images=pal[1:], duration=int(dt * 1000), loop=0,
                optimize=True, disposal=2)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    QApplication(sys.argv)
    render_hero()
    render_states()
    render_panel()
    render_banner()
    render_read_along()
    render_you_said()
    render_bar()
    render_gif()
    for f in sorted(OUT.iterdir()):
        print(f"{f.name}: {f.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
