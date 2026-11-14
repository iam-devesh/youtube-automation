"""
pipeline/templates.py
Viral-Shorts visual templates. Everything here is rendered in code (numpy/PIL),
so it works without any paid video/image-generation API.

  mystery — dark teal grade, film grain, heavy vignette, flash+glitch hard cuts,
            red keyword captions, red progress bar, low ambient drone, whooshes
  story   — warm grade, smooth crossfades, zoom-punch on each cut,
            yellow word-by-word captions, whooshes
  facts   — animated gradient + floating particles, footage shown in a rounded
            card, big "#1 / #2…" counters, green captions, pop sounds

Text is NOT drawn here: all text goes through ASS/libass in video_builder, because
Pillow without libraqm mis-renders Devanagari.
"""

import os
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

ASSETS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets")
SR = 44100

# ASS colours are &HAABBGGRR
TEMPLATES = {
    "mystery": {
        "label": "Mystery / Documentary",
        "grade": "mystery", "vignette": 0.75, "grain": 4,
        "overlap": 0.0, "tail": 0.12, "cut_fx": "flash_glitch",
        "caption": {"base": "&H00FFFFFF", "hi": "&H003C3CFF", "box": False},
        "title_colour": "&H003C3CFF",
        "hook": {"text": "&H00FFFFFF", "box": "&H002020E0"},
        "bar": (255, 60, 60), "sfx": "whoosh", "bed": "heartbeat", "layout": "full",
    },
    "story": {
        "label": "Story / What-if",
        "grade": "warm", "vignette": 0.45, "grain": 0,
        "overlap": 0.2, "tail": 0.2, "cut_fx": "punch",
        "caption": {"base": "&H00FFFFFF", "hi": "&H0000E1FF", "box": False},
        "title_colour": "&H0000E1FF",
        "hook": {"text": "&H00000000", "box": "&H0000E1FF"},
        "bar": (255, 210, 0), "sfx": "whoosh", "bed": "beat90", "layout": "full",
    },
    "facts": {
        "label": "Facts / Countdown",
        "grade": None, "vignette": 0.0, "grain": 0,
        "overlap": 0.0, "tail": 0.12, "cut_fx": "punch",
        "caption": {"base": "&H00FFFFFF", "hi": "&H0076E600", "box": False},
        "title_colour": "&H0076E600",
        "hook": {"text": "&H00000000", "box": "&H0076E600"},
        "bar": (0, 230, 118), "sfx": "pop", "bed": "beat110", "layout": "card",
    },
}
DEFAULT_TEMPLATE = "mystery"


def get_template(name):
    return TEMPLATES.get((name or "").lower(), TEMPLATES[DEFAULT_TEMPLATE])


# ─── Per-frame look (done by ffmpeg filters at the final encode — fast, in C) ──

def ffmpeg_look(tpl, w, h, total, offset=0.0):
    """Colour grade, vignette, grain and progress bar as an ffmpeg -vf chain."""
    parts = []
    if tpl["grade"] == "mystery":
        parts += ["eq=contrast=1.15:brightness=-0.04:saturation=0.6",
                  "colorbalance=rs=-0.06:bs=0.07:rh=0.05:bh=-0.04"]
    elif tpl["grade"] == "warm":
        parts += ["eq=contrast=1.06:saturation=1.15",
                  "colorbalance=rm=0.04:bm=-0.04:rh=0.03"]
    if tpl["vignette"]:
        parts.append(f"vignette=angle={tpl['vignette']:.2f}")
    if tpl["grain"]:
        parts.append(f"noise=alls={tpl['grain']}:allf=t")
    r, g, b = tpl["bar"]
    bar_h = max(6, h // 240)
    # offset: where this piece starts in the whole video (chapters) — the bar runs across all of it
    parts.append(f"drawbox=x=0:y=0:w=iw*(t+{offset:.3f})/{total:.3f}:h={bar_h}:"
                 f"color=0x{r:02X}{g:02X}{b:02X}@1:t=fill")
    return ",".join(parts)


def cut_effect(tpl, w, h):
    """Returns f(get_frame, t) applied to each scene, t relative to scene start."""
    kind = tpl["cut_fx"]

    def flash_glitch(get_frame, t):
        f = get_frame(t)
        if t < 0.22:
            f = f.astype(np.int16)
            shift = int(w * 0.012 * (1 - t / 0.22)) + 1
            f[..., 0] = np.roll(f[..., 0], -shift, axis=1)
            f[..., 2] = np.roll(f[..., 2], shift, axis=1)
            if t < 0.12:
                f = f + int(170 * (1 - t / 0.12))
            f = np.clip(f, 0, 255).astype(np.uint8)
        return f

    def punch(get_frame, t):
        f = get_frame(t)
        dur = 0.35
        if t < dur:
            p = t / dur
            z = 1 + 0.09 * (1 - p) ** 2
            cw, ch = int(w / z), int(h / z)
            x0, y0 = (w - cw) // 2, (h - ch) // 2
            img = Image.fromarray(f[y0:y0 + ch, x0:x0 + cw])
            f = np.asarray(img.resize((w, h), Image.BILINEAR))
        return f

    return {"flash_glitch": flash_glitch, "punch": punch}.get(kind)


def motion(w, h, seg_dur, punch=True, shake=0.0, drift=0.07, seed=0):
    """
    Constant camera motion for one shot: slow push-in over the whole shot, a fast
    zoom "punch" on the cut, and optional camera shake (used on the hook).
    One crop+resize per frame.
    """
    rng = np.random.default_rng(seed)
    jitter = rng.normal(0, 1, (int(seg_dur * 30) + 2, 2))
    direction = 1 if seed % 2 == 0 else -1

    def fx(get_frame, t):
        f = get_frame(t)
        p = min(t / max(seg_dur, 0.01), 1.0)
        z = 1.0 + drift * p if direction > 0 else 1.0 + drift * (1 - p)
        if punch and t < 0.3:
            z *= 1 + 0.10 * (1 - t / 0.3) ** 2
        dx = dy = 0.0
        if shake and t < 0.6:
            amp = shake * (1 - t / 0.6) * w
            j = jitter[min(int(t * 30), len(jitter) - 1)]
            dx, dy = j[0] * amp, j[1] * amp
            z = max(z, 1.0 + 2.2 * shake + 0.02)
        if z <= 1.001 and not dx and not dy:
            return f
        fh, fw = f.shape[:2]
        cw, ch = fw / z, fh / z
        cx = min(max(fw / 2 + dx, cw / 2), fw - cw / 2)
        cy = min(max(fh / 2 + dy, ch / 2), fh - ch / 2)
        box = (cx - cw / 2, cy - ch / 2, cx + cw / 2, cy + ch / 2)
        return np.asarray(Image.fromarray(f).resize((fw, fh), Image.BILINEAR, box=box))

    return fx


# ─── Ambient animation over still artwork (makes AI images feel alive) ───────

class AmbientFX:
    """
    kind: rain | storm | fog | dust | embers | none — chosen per scene by the script.
    Precomputes textures once; per frame it only slices + blends.
    """

    def __init__(self, kind, w, h, seed=0):
        self.kind, self.w, self.h = (kind or "none").lower(), w, h
        rng = np.random.default_rng(seed + 101)
        self.flashes = sorted(rng.uniform(0.6, 30, 6)) if self.kind == "storm" else []
        if self.kind in ("rain", "storm"):
            layer = Image.new("L", (w, h * 2), 0)
            d = ImageDraw.Draw(layer)
            n = 420 if self.kind == "storm" else 260
            for _ in range(n):
                x, y = rng.uniform(-60, w), rng.uniform(0, h * 2)
                ln = rng.uniform(h * 0.03, h * 0.07)
                d.line([x, y, x + ln * 0.18, y + ln], fill=int(rng.uniform(70, 150)),
                       width=max(1, w // 540))
            self.tex = np.asarray(layer.filter(ImageFilter.GaussianBlur(0.8)), np.float32)[..., None] / 255
            self.speed = h * 1.6
        elif self.kind == "fog":
            small = rng.uniform(0, 1, (12, 10)).astype(np.float32)
            img = Image.fromarray((small * 255).astype(np.uint8)).resize((w * 2, h), Image.BICUBIC)
            self.tex = np.asarray(img.filter(ImageFilter.GaussianBlur(w * 0.03)), np.float32)[..., None] / 255
            self.speed = w * 0.04
        elif self.kind in ("dust", "embers"):
            layer = Image.new("L", (w, h * 2), 0)
            d = ImageDraw.Draw(layer)
            for _ in range(70 if self.kind == "embers" else 110):
                r = rng.uniform(0.0015, 0.005) * w
                x, y = rng.uniform(0, w), rng.uniform(0, h)
                a = int(rng.uniform(90, 230))
                for yy in (y, y + h):
                    d.ellipse([x - r, yy - r, x + r, yy + r], fill=a)
            self.tex = np.asarray(layer.filter(ImageFilter.GaussianBlur(w * 0.002)), np.float32)[..., None] / 255
            self.speed = h * (0.12 if self.kind == "embers" else 0.03)
            self.tint = np.array([255, 140, 40] if self.kind == "embers" else [255, 245, 225], np.float32)
        else:
            self.tex = None

    def __call__(self, get_frame, t):
        f = get_frame(t)
        if self.tex is None and not self.flashes:
            return f
        out = f.astype(np.float32)
        h, w = self.h, self.w
        if self.kind in ("rain", "storm"):
            off = int((t * self.speed) % h)
            out += self.tex[h - off: 2 * h - off] * 140
            out *= 0.93
        elif self.kind == "fog":
            off = int((t * self.speed) % w)
            fog = self.tex[:, off: off + w]
            out = out * (1 - fog * 0.45) + 200 * fog * 0.45
        elif self.kind in ("dust", "embers"):
            # embers rise, dust drifts down slowly
            off = int((t * self.speed) % h)
            sl = self.tex[off: off + h] if self.kind == "embers" else self.tex[h - off: 2 * h - off]
            out += sl * self.tint * (0.9 if self.kind == "embers" else 0.5)
        for ft in self.flashes:
            if 0 <= t - ft < 0.18:
                out = out + 140 * (1 - (t - ft) / 0.18)
        return np.clip(out, 0, 255).astype(np.uint8)


# ─── Facts template: generated background + media card ───────────────────────

_PALETTES = [((20, 12, 60), (120, 30, 140)), ((5, 40, 70), (10, 130, 140)),
             ((60, 10, 30), (180, 60, 40)), ((10, 50, 30), (30, 140, 90)),
             ((30, 20, 70), (40, 90, 200))]


def card_geometry(w, h):
    if h > w:   # vertical short
        cw = int(w * 0.86)
        ch = int(cw * 1.1)
        return cw, ch, (w - cw) // 2, int(h * 0.17)
    ch = int(h * 0.62)
    cw = int(ch * 16 / 9)
    return cw, ch, (w - cw) // 2, int(h * 0.1)


class FactsBackground:
    def __init__(self, w, h, scene_index):
        self.w, self.h = w, h
        c1, c2 = (np.array(c, np.float32) for c in _PALETTES[scene_index % len(_PALETTES)])
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        diag = (xx / w * 0.4 + yy / h * 0.6)[..., None]
        self.base = c1 * (1 - diag) + c2 * diag
        # particle layer twice the height so it can scroll seamlessly
        rng = np.random.default_rng(scene_index + 1)
        layer = Image.new("L", (w, h * 2), 0)
        dr = ImageDraw.Draw(layer)
        for _ in range(38):
            r = int(rng.uniform(0.004, 0.02) * w)
            x, y = rng.uniform(0, w), rng.uniform(0, h)
            a = int(rng.uniform(40, 120))
            for yy_ in (y, y + h):
                dr.ellipse([x - r, yy_ - r, x + r, yy_ + r], fill=a)
        layer = layer.filter(ImageFilter.GaussianBlur(w * 0.004))
        self.particles = np.asarray(layer, np.float32)[..., None] / 255.0
        self.cw, self.ch, self.cx, self.cy = card_geometry(w, h)
        rad = int(self.cw * 0.05)
        m = Image.new("L", (self.cw, self.ch), 0)
        ImageDraw.Draw(m).rounded_rectangle([0, 0, self.cw - 1, self.ch - 1], rad, fill=255)
        self.mask = np.asarray(m, np.float32)[..., None] / 255.0
        sh = Image.new("L", (w, h), 0)
        ImageDraw.Draw(sh).rounded_rectangle(
            [self.cx - 6, self.cy + 14, self.cx + self.cw + 6, self.cy + self.ch + 26], rad, fill=170)
        self.shadow = np.asarray(sh.filter(ImageFilter.GaussianBlur(22)), np.float32)[..., None] / 255.0
        border = Image.new("L", (self.cw, self.ch), 0)
        ImageDraw.Draw(border).rounded_rectangle([0, 0, self.cw - 1, self.ch - 1], rad,
                                                 outline=255, width=max(4, w // 200))
        self.border = np.asarray(border, np.float32)[..., None] / 255.0

    def compose(self, card_frame, t):
        off = int((t * 40) % self.h)
        p = self.particles[self.h - off: 2 * self.h - off]
        out = self.base * (1 - self.shadow * 0.8) + p * 200
        if card_frame is not None:
            region = out[self.cy:self.cy + self.ch, self.cx:self.cx + self.cw]
            card = card_frame.astype(np.float32)
            card = card * (1 - self.border) + 255 * self.border
            out[self.cy:self.cy + self.ch, self.cx:self.cx + self.cw] = \
                region * (1 - self.mask) + card * self.mask
        return np.clip(out, 0, 255).astype(np.uint8)


# ─── Audio: sfx + ambience (synthesized, royalty-free) ───────────────────────

def whoosh(dur=0.55):
    n = int(SR * dur)
    t = np.linspace(0, 1, n, dtype=np.float32)
    noise = np.random.default_rng(3).normal(0, 1, n).astype(np.float32)
    # sweeping one-pole low-pass: dull → bright → dull
    cutoff = 0.02 + 0.25 * np.sin(np.pi * t) ** 2
    out = np.empty_like(noise)
    y = 0.0
    for i in range(n):
        y += cutoff[i] * (noise[i] - y)
        out[i] = y
    env = np.sin(np.pi * t) ** 1.5
    out = out * env
    return out / (np.abs(out).max() + 1e-6)


def pop(dur=0.12):
    n = int(SR * dur)
    t = np.arange(n, dtype=np.float32) / SR
    freq = 950 * np.exp(-t * 18) + 380
    phase = 2 * np.pi * np.cumsum(freq) / SR
    return (np.sin(phase) * np.exp(-t * 35)).astype(np.float32)


def drone(dur=30.0):
    """Loopable dark ambient bed (30 s)."""
    n = int(SR * dur)
    t = np.arange(n, dtype=np.float32) / SR
    lfo = 0.6 + 0.4 * np.sin(2 * np.pi * t / 10.0)
    sig = (0.5 * np.sin(2 * np.pi * 55 * t) + 0.3 * np.sin(2 * np.pi * 82.5 * t)
           + 0.18 * np.sin(2 * np.pi * 110.3 * t) * lfo + 0.08 * np.sin(2 * np.pi * 164.8 * t))
    noise = np.random.default_rng(5).normal(0, 1, n).astype(np.float32)
    noise = np.convolve(noise, np.ones(400, np.float32) / 400, mode="same")
    sig = sig * lfo + noise * 2.0
    return (sig / (np.abs(sig).max() + 1e-6)).astype(np.float32)


def kick(dur=0.35, f0=120, f1=45, decay=9):
    n = int(SR * dur)
    t = np.arange(n, dtype=np.float32) / SR
    freq = f1 + (f0 - f1) * np.exp(-t * 25)
    return (np.sin(2 * np.pi * np.cumsum(freq) / SR) * np.exp(-t * decay)).astype(np.float32)


def hat(dur=0.05):
    n = int(SR * dur)
    x = np.random.default_rng(11).normal(0, 1, n).astype(np.float32)
    x = np.diff(x, prepend=0)          # crude high-pass
    return x * np.exp(-np.arange(n) / SR * 90) * 0.25


def impact(dur=1.4):
    """Cinematic 'boom' for the hook."""
    b = kick(dur, f0=90, f1=32, decay=3.2)
    n = len(b)
    noise = np.random.default_rng(13).normal(0, 1, n).astype(np.float32)
    noise = np.convolve(noise, np.ones(60, np.float32) / 60, mode="same")
    noise *= np.exp(-np.arange(n) / SR * 6)
    out = b + noise * 1.5
    return out / (np.abs(out).max() + 1e-6)


def _place(buf, sample, at):
    i = int(at * SR)
    j = min(len(buf), i + len(sample))
    if i < len(buf):
        buf[i:j] += sample[: j - i]


def beat(bpm=100, bars=4):
    """Loopable minimal trap-ish groove: kick, offbeat hats, soft minor pad."""
    beat_s = 60.0 / bpm
    n = int(SR * beat_s * 4 * bars)
    buf = np.zeros(n, np.float32)
    k, hh = kick(), hat()
    for b in range(4 * bars):
        if b % 4 in (0, 2) or (b % 8 == 7):
            _place(buf, k * 0.9, b * beat_s)
        _place(buf, hh, b * beat_s + beat_s / 2)
        if b % 2 == 1:
            _place(buf, hh * 0.6, b * beat_s)
    t = np.arange(n, dtype=np.float32) / SR
    chords = [(220.0, 261.6, 329.6), (174.6, 220.0, 261.6)]   # Am → F
    pad = np.zeros(n, np.float32)
    seg = n // len(chords)
    for ci, ch in enumerate(chords):
        sl = slice(ci * seg, (ci + 1) * seg)
        tt = t[sl] - t[sl][0]
        env = np.minimum(tt / 0.4, 1) * np.minimum((t[sl][-1] - t[sl][0] - tt) / 0.4 + 0.01, 1)
        pad[sl] = sum(np.sin(2 * np.pi * f * tt) for f in ch) * env
    out = buf + pad * 0.12
    return out / (np.abs(out).max() + 1e-6)


def heartbeat(bpm=72, beats=24):
    """Tense lub-dub pulse over the dark drone."""
    beat_s = 60.0 / bpm
    n = int(SR * beat_s * beats)
    buf = np.zeros(n, np.float32)
    lub, dub = kick(0.25, 70, 40, 14), kick(0.2, 60, 38, 16) * 0.7
    for b in range(beats):
        _place(buf, lub, b * beat_s)
        _place(buf, dub, b * beat_s + 0.24)
    d = drone(len(buf) / SR)
    out = buf * 0.9 + d * 0.5
    return out / (np.abs(out).max() + 1e-6)


def bed_for(kind):
    if kind == "heartbeat":
        return heartbeat()
    if kind == "beat90":
        return beat(90)
    if kind == "beat110":
        return beat(110)
    return None


def music_file_for(template_name):
    """Optional: drop your own royalty-free track at assets/music/<template>.mp3."""
    for ext in ("mp3", "m4a", "wav"):
        p = os.path.join(ASSETS_DIR, "music", f"{template_name}.{ext}")
        if os.path.exists(p):
            return p
    return None
