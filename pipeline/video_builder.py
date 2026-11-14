"""
pipeline/video_builder.py
Assembles the final video with a viral-Shorts template (see pipeline/templates.py):
per-scene media (stock/AI clips or stills) + TTS audio → MoviePy v2 render →
captions burned in with ffmpeg/libass.

Why libass for text: Pillow (used by MoviePy's TextClip) mis-shapes Devanagari
unless built with libraqm. libass uses HarfBuzz, so Hindi renders correctly.
"""

import json
import os
import sys
import subprocess

try:
    import imageio_ffmpeg
    FFMPEG_BIN = imageio_ffmpeg.get_ffmpeg_exe()
    os.environ["IMAGEIO_FFMPEG_EXE"] = FFMPEG_BIN
    os.environ.setdefault("FFMPEG_BINARY", FFMPEG_BIN)
except Exception:
    FFMPEG_BIN = "ffmpeg"

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import (OUTPUT_DIR, VIDEO_WIDTH, VIDEO_HEIGHT, SHORT_WIDTH,
                    SHORT_HEIGHT, FPS, SUBTITLE_FONTS_DIR, SUBTITLE_FONT)
from pipeline import templates as T
from pipeline import parallax
from pipeline import music

# 2.5D depth-parallax animation for stills (GPU); plain Ken Burns otherwise
SYNTH_MUSIC = os.getenv("SYNTH_MUSIC", "false").lower() in ("1", "true", "yes")
PARALLAX = os.getenv("PARALLAX", "true").lower() in ("1", "true", "yes") and parallax.available()

MOVIEPY_AVAILABLE = False
try:
    from moviepy import (AudioFileClip, AudioArrayClip, CompositeAudioClip, VideoClip,
                         VideoFileClip, ColorClip, concatenate_videoclips, vfx)
    MOVIEPY_AVAILABLE = True
except ImportError:
    print("⚠️  MoviePy v2 not installed. Run: pip install 'moviepy>=2'")

VIDEO_EXTS = (".mp4", ".mov", ".avi", ".webm", ".mkv")


def _dims(video_type):
    return (SHORT_WIDTH, SHORT_HEIGHT) if video_type == "short" else (VIDEO_WIDTH, VIDEO_HEIGHT)


# ─── Visual layers ────────────────────────────────────────────────────────────

def _cover_video(clip, w, h):
    """Scale to fully cover w×h, then center-crop. Never stretches."""
    clip = clip.resized(max(w / clip.w, h / clip.h))
    return clip.cropped(x_center=clip.w / 2, y_center=clip.h / 2, width=w, height=h)


def _ken_burns_clip(image_path, w, h, duration, variant=0):
    """Smooth zoom/pan over a still image (direction alternates per scene)."""
    img = Image.open(image_path).convert("RGB")
    over = 1.2
    scale = max(w * over / img.width, h * over / img.height)
    img = img.resize((int(img.width * scale + 0.5), int(img.height * scale + 0.5)), Image.LANCZOS)
    bw, bh = img.size
    mode = variant % 4   # 0 zoom-in, 1 zoom-out, 2 pan-right, 3 pan-left

    def frame(t):
        p = min(max(t / max(duration, 0.01), 0.0), 1.0)
        p = p * p * (3 - 2 * p)
        z = (1 + (over - 1) * p) if mode == 0 else (over - (over - 1) * p) if mode == 1 else 1.1
        cw, ch = bw / z, bh / z
        cx, cy = bw / 2, bh / 2
        if mode == 2:
            cx = cw / 2 + (bw - cw) * p
        elif mode == 3:
            cx = bw - cw / 2 - (bw - cw) * p
        box = (cx - cw / 2, cy - ch / 2, cx + cw / 2, cy + ch / 2)
        return np.asarray(img.resize((w, h), Image.BILINEAR, box=box))

    return VideoClip(frame_function=frame, duration=duration)


def _media_layer(media_path, w, h, duration, variant, offset=0.0):
    if media_path and media_path.lower().endswith(VIDEO_EXTS) and os.path.exists(media_path):
        clip = VideoFileClip(media_path).without_audio()
        start = 0.4 + offset
        if clip.duration >= start + duration + 0.05:
            clip = clip.subclipped(start, start + duration)
        elif clip.duration >= duration + 0.1:
            start = max(clip.duration - duration - 0.05, 0)
            clip = clip.subclipped(start, start + duration)
        else:
            clip = clip.with_effects([vfx.Loop(duration=duration)])
        return _cover_video(clip, w, h).with_duration(duration)
    if media_path and os.path.exists(media_path):
        if PARALLAX:
            try:
                return parallax.parallax_clip(media_path, w, h, duration, variant)
            except Exception as e:
                print(f"  ⚠️  Parallax failed ({str(e)[:100]}), using Ken Burns")
        return _ken_burns_clip(media_path, w, h, duration, variant)
    return ColorClip(size=(w, h), color=(10, 10, 25)).with_duration(duration)


def _shots(media, total, video_type):
    """Split a scene into fast shots (~2.2 s on Shorts). Reuses clips at new offsets if short."""
    media = media if isinstance(media, (list, tuple)) else [media]
    media = [m for m in media if m] or [None]
    target = 2.2 if video_type == "short" else 4.0
    n = max(len(media), min(5, round(total / target))) if total > target * 1.4 else 1
    n = max(n, 1)
    seg = total / n
    out = []
    for k in range(n):
        m = media[k % len(media)]
        out.append((m, seg, (k // len(media)) * seg))
    return out


def build_scene_clip(tpl, media, audio_path, duration, w, h, index, video_type="short",
                     ambient=None):
    total = duration + tpl["tail"]
    card = tpl["layout"] == "card"
    bg = T.FactsBackground(w, h, index) if card else None
    sw, sh = (bg.cw, bg.ch) if card else (w, h)

    shots = []
    for k, (m, seg, offset) in enumerate(_shots(media, total, video_type)):
        layer = _media_layer(m, sw, sh, seg, index * 7 + k, offset)
        is_still = bool(m) and not m.lower().endswith(VIDEO_EXTS)
        hook_shot = index == 0 and k == 0
        layer = layer.transform(T.motion(sw, sh, seg, punch=True,
                                         shake=0.012 if hook_shot else 0.0,
                                         drift=0.0 if is_still else 0.07,
                                         seed=index * 7 + k))
        shots.append(layer)
    inner = shots[0] if len(shots) == 1 else concatenate_videoclips(shots)
    inner = inner.with_duration(total)

    if card:
        clip = VideoClip(frame_function=lambda t: bg.compose(inner.get_frame(t), t), duration=total)
    else:
        clip = inner
    if ambient and ambient != "none":
        clip = clip.transform(T.AmbientFX(ambient, w, h, seed=index))
    if tpl["cut_fx"] == "flash_glitch":
        clip = clip.transform(T.cut_effect(tpl, w, h))

    audio = AudioFileClip(audio_path)
    audio = audio.subclipped(0, min(duration, audio.duration))
    return clip.with_audio(CompositeAudioClip([audio]).with_duration(total))


# ─── Audio bed + sfx ──────────────────────────────────────────────────────────

def _stereo(x, gain):
    x = (x * gain).astype(np.float32)
    return AudioArrayClip(np.stack([x, x], axis=1), fps=T.SR)


def _mix_audio(final, tpl, template_name, scene_starts):
    layers = [final.audio]
    total = final.duration
    layers.append(_stereo(T.impact(), 0.30).with_start(0))          # boom on the hook
    sfx = T.whoosh() if tpl["sfx"] == "whoosh" else T.pop()
    for i, st in enumerate(scene_starts):
        if i == 0:
            continue
        lead = 0.28 if tpl["sfx"] == "whoosh" else 0.0
        layers.append(_stereo(sfx, 0.12 if tpl["sfx"] == "whoosh" else 0.18)
                      .with_start(max(st - lead, 0)))
    if SHOW_CTA:
        layers.append(_stereo(T.pop(), 0.22).with_start(max(total - CTA_SECONDS + 0.15, 0)))  # CTA pop
    # music is mixed at the final encode with ducking (see _burn_subtitles)
    return CompositeAudioClip(layers).with_duration(total)


# ─── Captions (ASS) ───────────────────────────────────────────────────────────

def _ass_time(t):
    t = max(t, 0)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _esc(text):
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def _proportional_words(narration, dur):
    words = narration.split()
    total = sum(len(x) + 1 for x in words) or 1
    out, t = [], 0.0
    for wd in words:
        d = dur * (len(wd) + 1) / total
        out.append((t, t + d, wd))
        t += d
    return out


def _chunks(words, max_words):
    chunks, cur = [], []
    for wd in words:
        if cur and (len(cur) >= max_words or wd[0] - cur[-1][1] > 0.4):
            chunks.append(cur)
            cur = []
        cur.append(wd)
    if cur:
        chunks.append(cur)
    return chunks


def _caption_line(chunk, k, base, hi, bounce):
    """Chunk text with word k highlighted; `bounce` = pop-in animation on chunk entry."""
    parts = []
    for j, (_, _, x) in enumerate(chunk):
        target = 116 if j == k else 100
        col = hi if j == k else base
        if bounce:
            tag = (f"{{\\c{col}\\fscx55\\fscy55\\t(0,90,\\fscx{target + 14}\\fscy{target + 14})"
                   f"\\t(90,170,\\fscx{target}\\fscy{target})}}")
        else:
            tag = f"{{\\c{col}\\fscx{target}\\fscy{target}}}"
        parts.append(tag + _esc(x))
    return " ".join(parts)


CTA_LINES = {
    "mystery": "ऐसी ही रहस्यमयी कहानियों के लिए",
    "story": "ऐसी ही कहानियों के लिए",
    "facts": "रोज़ नए फ़ैक्ट्स के लिए",
}
CTA_SECONDS = 2.8
# End-screen "FOLLOW" button — off by default. Set SHOW_CTA=true in .env to bring it back.
SHOW_CTA = os.getenv("SHOW_CTA", "false").lower() in ("1", "true", "yes")


def _pill(x0, y0, bw, bh):
    """ASS vector path for a rounded pill (cubic arcs) in absolute coords."""
    r = bh / 2
    k = 1.333 * r
    L, R, T, B = x0 + r, x0 + bw - r, y0, y0 + bh
    return (f"m {L:.0f} {T:.0f} l {R:.0f} {T:.0f} b {R + k:.0f} {T:.0f} {R + k:.0f} {B:.0f} {R:.0f} {B:.0f} "
            f"l {L:.0f} {B:.0f} b {L - k:.0f} {B:.0f} {L - k:.0f} {T:.0f} {L:.0f} {T:.0f}")


def _cta_events(tpl, w, h, total, line, button):
    """
    Viral end screen: the video dims, a one-line pitch slides up, then a glowing
    accent-coloured pill button pops in, pulses, gets a light sweep, and an arrow
    bounces underneath pointing at it.
    """
    t0 = max(total - CTA_SECONDS, 0)
    tb = t0 + 0.15                                   # button appears a beat after the line
    cx = w // 2
    bw, bh = int(w * 0.66), int(h * 0.075)
    y_line, y_btn = int(h * 0.35), int(h * 0.43)
    x0, y0 = cx - bw // 2, y_btn - bh // 2
    accent = tpl["hook"]["box"]
    st, en, sb = _ass_time(t0), _ass_time(total), _ass_time(tb)
    pill = _pill(0, 0, bw, bh)                       # local coords (drawn with \an5 at centre)
    pop = ("\\fscx30\\fscy30\\t(0,180,\\fscx112\\fscy112)\\t(180,300,\\fscx100\\fscy100)"
           "\\t(800,950,\\fscx107\\fscy107)\\t(950,1100,\\fscx100\\fscy100)"
           "\\t(1500,1650,\\fscx107\\fscy107)\\t(1650,1800,\\fscx100\\fscy100)")
    ev = [
        # dim the video so the end card pops
        f"Dialogue: 5,{st},{en},Shape,,0,0,0,,{{\\an7\\pos(0,0)\\c&H000000&\\alpha&HFF&"
        f"\\t(0,250,\\alpha&H70&)\\p1}}m 0 0 l {w} 0 l {w} {h} l 0 {h}{{\\p0}}",
        f"Dialogue: 7,{st},{en},CTALine,,0,0,0,,{{\\move({cx},{y_line + 40},{cx},{y_line},0,250)\\fad(150,0)}}{_esc(line)}",
        # soft glow
        f"Dialogue: 6,{sb},{en},Shape,,0,0,0,,{{\\an5\\pos({cx},{y_btn})\\c{accent}\\alpha&H60&\\blur18"
        f"\\bord0{pop}\\p1}}{pill}{{\\p0}}",
        # pill with white rim
        f"Dialogue: 7,{sb},{en},Shape,,0,0,0,,{{\\an5\\pos({cx},{y_btn})\\c{accent}\\3c&HFFFFFF&\\bord4"
        f"{pop}\\p1}}{pill}{{\\p0}}",
        # label
        f"Dialogue: 9,{sb},{en},CTAText,,0,0,0,,{{\\pos({cx},{y_btn}){pop}}}{_esc(button)}",
    ]
    # light sweep across the button (clipped to the pill), twice
    clip = _pill(x0, y0, bw, bh)
    sw = int(bh * 0.6)
    band = f"m 0 0 l {sw} 0 l {sw // 2} {bh} l {-sw // 2} {bh}"
    for start in (tb + 0.45, tb + 1.45):
        if start + 0.5 < total:
            ev.append(f"Dialogue: 8,{_ass_time(start)},{_ass_time(start + 0.5)},Shape,,0,0,0,,"
                      f"{{\\an7\\move({x0 - sw},{y0},{x0 + bw + sw},{y0})\\c&HFFFFFF&\\alpha&H90&"
                      f"\\blur3\\clip({clip})\\p1}}{band}{{\\p0}}")
    # arrow under the button pointing up at it, bouncing
    s_ = max(16, w // 32)
    arrow = f"m {-s_} {s_} l 0 {-s_} l {s_} {s_}"
    y_ar = y_btn + bh // 2 + int(h * 0.04)
    k, t = 0, tb + 0.35
    while t < total - 0.05:
        t1 = min(t + 0.32, total)
        a_, b_ = (y_ar + 18, y_ar) if k % 2 == 0 else (y_ar, y_ar + 18)
        ev.append(f"Dialogue: 7,{_ass_time(t)},{_ass_time(t1)},Shape,,0,0,0,,"
                  f"{{\\an5\\move({cx},{a_},{cx},{b_})\\c&HFFFFFF&\\3c&H000000&\\bord3\\p1}}{arrow}{{\\p0}}")
        t, k = t1, k + 1
    return ev


def build_ass(tpl, title, scenes_info, w, h, video_type, total, hook_text=None,
              cta_line=None, cta_button=None, template_name="mystery"):
    """
    scenes_info: list of dicts {start, duration, words, narration, number}
    Viral-style captions: 2–3 big words at a time, pop-in bounce, the spoken word
    highlighted (real TTS word timings when available), plus a boxed hook banner.
    """
    short = video_type == "short"
    card = tpl["layout"] == "card"
    cap = tpl["caption"]
    hook = tpl["hook"]
    cap_size = int(h * (0.074 if short else 0.08))
    hook_size = int(h * (0.052 if short else 0.075))
    num_size = int(h * (0.075 if short else 0.11))
    cap_margin = int(h * (0.14 if short else 0.06)) if card else int(h * (0.32 if short else 0.10))
    hook_margin = int(h * 0.04) if card else int(h * (0.17 if short else 0.12))
    words_per_chunk = 3 if short else 5
    outline = max(5, cap_size // 10)

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{SUBTITLE_FONT},{cap_size},{cap['base']},&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,{outline},4,2,60,60,{cap_margin},1
Style: Hook,{SUBTITLE_FONT},{hook_size},{hook['text']},&H00FFFFFF,{hook['box']},&H00000000,-1,0,0,0,100,100,0,0,3,{max(14, hook_size // 4)},0,8,90,90,{hook_margin},1
Style: Number,{SUBTITLE_FONT},{num_size},{cap['hi']},&H00FFFFFF,&H00000000,&H90000000,-1,0,0,0,100,100,0,0,1,{max(4, num_size // 12)},4,8,0,0,{int(h * 0.04)},1
Style: CTALine,{SUBTITLE_FONT},{int(h * (0.043 if short else 0.055))},&H00FFFFFF,&H00FFFFFF,&H00000000,&H90000000,-1,0,0,0,100,100,0,0,1,{max(4, cap_size // 14)},3,5,60,60,0,1
Style: CTAText,{SUBTITLE_FONT},{int(h * (0.05 if short else 0.07))},{hook['text']},&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1
Style: Shape,Arial,10,&H00FFFFFF,&H00FFFFFF,&H00FFFFFF,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = []
    base, hi = cap["base"], cap["hi"]
    first_end = scenes_info[0]["start"] + scenes_info[0]["duration"] if scenes_info else 3
    hook_end = min(max(first_end, 1.8), 2.8)
    ev.append(f"Dialogue: 2,{_ass_time(0)},{_ass_time(hook_end)},Hook,,0,0,0,,"
              f"{{\\fscx40\\fscy40\\frz-3\\t(0,150,\\fscx112\\fscy112)\\t(150,260,\\fscx100\\fscy100)"
              f"\\fad(0,200)}}{_esc(hook_text or title)}")

    for s in scenes_info:
        if s.get("number"):
            ev.append(f"Dialogue: 2,{_ass_time(s['start'])},{_ass_time(s['start'] + s['duration'] + 0.1)},Number,,0,0,0,,"
                      f"{{\\fscx30\\fscy30\\t(0,160,\\fscx118\\fscy118)\\t(160,280,\\fscx100\\fscy100)}}#{s['number']}")
        words = s["words"] or _proportional_words(s["narration"], s["duration"])
        words = [(s["start"] + a, s["start"] + b, wd) for a, b, wd in words]
        scene_end = s["start"] + s["duration"] + 0.1
        chunks = _chunks(words, words_per_chunk)
        for ci, chunk in enumerate(chunks):
            # hold the last word briefly, but never overlap the next chunk (libass would stack them)
            nxt = chunks[ci + 1][0][0] if ci + 1 < len(chunks) else scene_end
            for k, (a, b, wd) in enumerate(chunk):
                end = chunk[k + 1][0] if k + 1 < len(chunk) else min(b + 0.3, nxt)
                ev.append(f"Dialogue: 6,{_ass_time(a)},{_ass_time(end)},Caption,,0,0,0,,"
                          f"{_caption_line(chunk, k, base, hi, bounce=(k == 0))}")

    if SHOW_CTA:
        ev += _cta_events(tpl, w, h, total, cta_line or CTA_LINES.get(template_name, CTA_LINES["mystery"]),
                          cta_button or "FOLLOW करें")
    return header + "\n".join(ev) + "\n"


# ─── Audio mix ────────────────────────────────────────────────────────────────
# Music tracks differ by 10+ dB in loudness, so the bed is first normalised to a fixed quiet
# level, the voice band (1–4 kHz) is carved out of it, it ducks hard under speech, and the
# result is MEASURED: voice must stay ≥ MIN_VOICE_MARGIN dB above the music while speaking,
# otherwise the music is turned down and the mix redone.
MUSIC_LUFS = float(os.getenv("MUSIC_LUFS", "-28"))
MIN_VOICE_MARGIN = float(os.getenv("MIN_VOICE_MARGIN", "15"))


def _pcm(path, sr=24000):
    r = subprocess.run([FFMPEG_BIN, "-v", "error", "-i", path, "-f", "s16le", "-ac", "1", "-ar", str(sr), "-"],
                       capture_output=True)
    return np.frombuffer(r.stdout, np.int16).astype(np.float32) / 32768.0


def _speech_mask(x, sr=24000, frame=0.02):
    n = int(sr * frame)
    k = len(x) // n
    if not k:
        return np.zeros(len(x), bool)
    rms = np.sqrt((x[: k * n].reshape(k, n) ** 2).mean(1) + 1e-12)
    db = 20 * np.log10(rms)
    voiced = db > max(np.percentile(db, 95) - 30, -50)
    mask = np.zeros(len(x), bool)                       # same length as x (tail frame unvoiced)
    mask[: k * n] = np.repeat(voiced, n)
    return mask


def mix_audio_track(voice_wav, music, total, out_wav, log=print):
    """Mix narration(+sfx) with ducked music into out_wav (-14 LUFS). Returns the measured
    voice-over-music margin in dB during speech."""
    d = os.path.dirname(out_wav)
    stem = os.path.join(d, "_music_stem.wav")
    level = 0.0
    margin = None
    for attempt in range(4):
        fade_out = max(total - 2.5, 0)
        fc = (f"[1:a]aformat=sample_rates=48000:channel_layouts=stereo,atrim=0:{total:.3f},asetpts=N/SR/TB,"
              f"loudnorm=I={MUSIC_LUFS}:TP=-8:LRA=11,"
              f"equalizer=f=2200:t=q:w=1.6:g=-6,volume={level:.1f}dB,"
              f"afade=t=in:d=1.2,afade=t=out:st={fade_out:.3f}:d=2.5[m];"
              f"[0:a]aformat=sample_rates=48000:channel_layouts=stereo,asplit=2[vo][sc];"
              f"[m][sc]sidechaincompress=threshold=0.012:ratio=10:attack=10:release=300,asplit=2[md][mo];"
              f"[vo][md]amix=inputs=2:duration=first:normalize=0,loudnorm=I=-14:TP=-1.0:LRA=11[a]")
        r = subprocess.run([FFMPEG_BIN, "-y", "-v", "error", "-i", voice_wav, "-stream_loop", "-1", "-i", music,
                            "-filter_complex", fc, "-map", "[a]", "-ar", "48000", out_wav,
                            "-map", "[mo]", "-ar", "48000", stem], capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(r.stderr[-600:])
        v, m = _pcm(voice_wav), _pcm(stem)
        n = min(len(v), len(m))
        mask = _speech_mask(v[:n])
        if mask.sum() == 0:
            margin = 99.0
            break
        db = lambda x: 20 * np.log10(np.sqrt((x ** 2).mean()) + 1e-9)
        margin = db(v[:n][mask]) - db(m[:n][mask])
        if margin >= MIN_VOICE_MARGIN:
            break
        level -= (MIN_VOICE_MARGIN - margin) + 2
        log(f"  🎚️  Music too loud under the voice ({margin:.1f} dB margin) — lowering it {level:.1f} dB")
    try:
        os.remove(stem)
    except OSError:
        pass
    log(f"  🎚️  Voice sits {margin:.1f} dB above the music while speaking")
    return margin


def _burn_subtitles(raw_path, ass_path, out_path, look="", audio=None, normalize=True):
    """
    Final encode: template look (grade/vignette/grain/progress bar) + captions.
    `audio`: a finished mix (wav) to use instead of the raw render's audio.
    """
    # `ass` filter with shaping=complex: the `subtitles` filter falls back to simple shaping,
    # which breaks Devanagari reph clusters (सिर्फ़ → "सरि्फ़", धर्म → "धर्‍म").
    subs = f"ass={os.path.basename(ass_path)}:shaping=complex"
    if SUBTITLE_FONTS_DIR and os.path.isdir(SUBTITLE_FONTS_DIR):
        subs += f":fontsdir={SUBTITLE_FONTS_DIR}"
    vf = f"{look},{subs}" if look else subs
    enc = ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-maxrate", "20M", "-bufsize", "40M",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
           "-movflags", "+faststart", out_path]
    if audio:
        cmd = [FFMPEG_BIN, "-y", "-i", raw_path, "-i", audio, "-map", "0:v", "-map", "1:a",
               "-vf", vf, "-shortest"] + enc
    elif normalize:
        cmd = [FFMPEG_BIN, "-y", "-i", raw_path, "-vf", vf, "-af", "loudnorm=I=-14:TP=-1.0:LRA=11"] + enc
    else:                                         # chapters: keep the audio as is (mixed at the end)
        cmd = [FFMPEG_BIN, "-y", "-i", raw_path, "-vf", vf, "-ac", "2"] + enc
    res = subprocess.run(cmd, cwd=os.path.dirname(ass_path), capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(res.stderr[-800:])


def master_loudness(path, target=-14.0):
    """
    Second loudness pass: measure integrated LUFS (EBU R128) and apply the exact gain
    with a true-peak limiter. Single-pass loudnorm can land several LU short. Audio only;
    the video stream is copied.
    """
    import re
    r = subprocess.run([FFMPEG_BIN, "-hide_banner", "-i", path, "-af", "ebur128=framelog=quiet",
                        "-f", "null", "-"], capture_output=True, text=True)
    m = re.findall(r"I:\s+(-?[\d.]+) LUFS", r.stderr)
    if not m:
        return None
    gain = target - float(m[-1])
    if abs(gain) < 0.5:
        return float(m[-1])
    tmp = path + ".master.mp4"
    res = subprocess.run([FFMPEG_BIN, "-y", "-v", "error", "-i", path, "-map", "0:v", "-map", "0:a",
                          "-c:v", "copy", "-af", f"volume={gain:.2f}dB,alimiter=limit=0.89:level=false",
                          "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", tmp],
                         capture_output=True, text=True)
    if res.returncode == 0:
        os.replace(tmp, path)
    return float(m[-1]) + gain


# ─── Chapters (long videos) ──────────────────────────────────────────────────
# A long video is rendered as ~1-minute chapters. Each chapter is built, graded, captioned,
# CHECKED and saved on its own with a fingerprint of its inputs; re-running the render reuses
# every chapter whose fingerprint still matches, so a failure or a crash costs one chapter,
# never the whole video. The chapters are then joined WITHOUT re-encoding the picture, and
# only the soundtrack (music + loudness) is processed for the whole video.

CHAPTER_SECONDS = float(os.getenv("CHAPTER_SECONDS", "55"))
CHAPTER_VERSION = "1"            # bump when chapter rendering changes → old chapters are rebuilt


def plan_chapters(script, audio_results, tpl):
    """[(first scene index, last+1)] — from script["chapters"] (scene counts) if given, else
    greedy groups of ~CHAPTER_SECONDS that never split a scene."""
    n = min(len(script.get("scenes", [])), len(audio_results))
    if script.get("chapters"):
        out, i = [], 0
        for c in script["chapters"]:
            k = int(c["scenes"] if isinstance(c, dict) else c)
            out.append((i, min(i + k, n)))
            i += k
        if i < n:
            out.append((i, n))
        return [c for c in out if c[0] < c[1]]
    out, start, acc = [], 0, 0.0
    for i in range(n):
        acc += audio_results[i]["duration"] + tpl["tail"] - tpl["overlap"]
        if acc >= CHAPTER_SECONDS and i + 1 < n:
            out.append((start, i + 1))
            start, acc = i + 1, 0.0
    out.append((start, n))
    return out


def _fingerprint(obj):
    import hashlib
    return hashlib.sha1(json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def _file_sig(p):
    paths = p if isinstance(p, list) else [p]
    return [(os.path.basename(x), os.path.getsize(x), int(os.path.getmtime(x)))
            for x in paths if x and os.path.exists(x)]


def assemble_chapters(script: dict, image_paths: list, audio_results: list,
                      job_id: str, progress_callback=None, checklist=None) -> str:
    """Long-video assembler (see the section comment above). Returns the output .mp4 path."""
    from pipeline import qa
    template_name = (script.get("template") or T.DEFAULT_TEMPLATE).lower()
    tpl = T.get_template(template_name)
    video_type = script.get("video_type", "long")
    title = script.get("title", "Untitled Video")
    scenes = script.get("scenes", [])
    w, h = _dims(video_type)
    overlap = tpl["overlap"]
    out_dir = os.path.join(OUTPUT_DIR, job_id)
    ch_dir = os.path.join(out_dir, "chapters")
    os.makedirs(ch_dir, exist_ok=True)
    checklist = checklist or qa.for_job(job_id)
    n = min(len(scenes), len(image_paths), len(audio_results))
    chapters = plan_chapters(script, audio_results[:n], tpl)
    # timeline of the WHOLE video (needed for the global progress bar + final checks)
    starts, t = [], 0.0
    for i in range(n):
        starts.append(t)
        t += audio_results[i]["duration"] + tpl["tail"] - overlap
    total = t + overlap
    numbered = range(1, n - 1) if n >= 4 else range(1, n)
    print(f"  📚 {len(chapters)} chapters for {n} scenes (~{total / 60:.1f} min)", flush=True)

    done_paths = []
    for k, (a, b) in enumerate(chapters):
        mp4 = os.path.join(ch_dir, f"ch_{k + 1:02d}.mp4")
        meta_path = mp4[:-4] + ".json"
        key = _fingerprint({
            "v": CHAPTER_VERSION, "w": w, "h": h, "tpl": template_name, "k": k, "first": k == 0,
            "offset": round(starts[a], 3), "total": round(total, 3),
            "scenes": [{"text": audio_results[i].get("narration") or scenes[i].get("narration", ""),
                        "audio": _file_sig(audio_results[i]["audio_path"]),
                        "dur": round(audio_results[i]["duration"], 3),
                        "media": _file_sig(image_paths[i]), "ambient": scenes[i].get("ambient")}
                       for i in range(a, b)]})
        try:
            meta = json.load(open(meta_path))
        except Exception:
            meta = {}
        if meta.get("key") == key and meta.get("passed") and os.path.exists(mp4):
            print(f"  ♻️  Chapter {k + 1}/{len(chapters)}: unchanged and checked — reused", flush=True)
            done_paths.append(mp4)
            continue
        if progress_callback:
            progress_callback(k + 1, len(chapters) + 1, f"Rendering chapter {k + 1}/{len(chapters)}")
        print(f"  🎬 Chapter {k + 1}/{len(chapters)}: scenes {a + 1}-{b}", flush=True)
        clips, info, cursor = [], [], 0.0
        for i in range(a, b):
            au = audio_results[i]
            try:
                clip = build_scene_clip(tpl, image_paths[i], au["audio_path"], au["duration"], w, h, i,
                                        video_type, ambient=scenes[i].get("ambient"))
            except Exception as e:
                raise RuntimeError(f"scene {i + 1} media failed: {e}")
            if clips and overlap > 0:
                clip = clip.with_effects([vfx.CrossFadeIn(overlap)])
            clips.append(clip)
            info.append({"start": cursor, "duration": au["duration"], "words": au.get("words") or [],
                         "narration": au.get("narration") or scenes[i].get("narration", ""),
                         "number": (i if tpl["layout"] == "card" and i in numbered else None)})
            cursor += clip.duration - overlap
        ch = concatenate_videoclips(clips, method="compose", padding=-overlap if overlap else 0)
        local_starts = [s_["start"] for s_ in info]
        layers = [ch.audio]
        if k == 0:
            layers.append(_stereo(T.impact(), 0.30).with_start(0))
        sfx = T.whoosh() if tpl["sfx"] == "whoosh" else T.pop()
        for j, st in enumerate(local_starts):
            if j == 0 and k == 0:
                continue
            lead = 0.28 if tpl["sfx"] == "whoosh" else 0.0
            layers.append(_stereo(sfx, 0.12 if tpl["sfx"] == "whoosh" else 0.18).with_start(max(st - lead, 0)))
        ch = ch.with_audio(CompositeAudioClip(layers).with_duration(ch.duration))
        ch_total = ch.duration
        ass_path = mp4[:-4] + ".ass"
        with open(ass_path, "w", encoding="utf-8") as f:
            f.write(build_ass(tpl, title, info, w, h, video_type, ch_total,
                              hook_text=script.get("hook_text") if k == 0 else None,
                              cta_line=None, template_name=template_name))
        sub = audio_results[a:b]
        spans = qa.check_captions(checklist, ass_path, sub, ch_total, local_starts)
        raw = mp4[:-4] + "_raw.mp4"
        ch.write_videofile(raw, fps=FPS, codec="libx264", audio_codec="aac", audio_bitrate="192k",
                           preset="veryfast", threads=4, ffmpeg_params=["-crf", "16", "-pix_fmt", "yuv420p"],
                           logger=None)
        ch.close()
        look = T.ffmpeg_look(tpl, w, h, total, offset=starts[a])
        _burn_subtitles(raw, ass_path, mp4, look, normalize=False)
        try:
            qa.check_chapter(checklist, k + 1, mp4, raw, look, spans, sub, local_starts, w, h)
        except qa.ChecklistFailed:
            os.replace(mp4, mp4[:-4] + "_rejected.mp4")
            raise
        finally:
            if os.path.exists(raw):
                os.remove(raw)
        json.dump({"key": key, "passed": True, "scenes": [a + 1, b], "seconds": round(ch_total, 2)},
                  open(meta_path, "w"))
        done_paths.append(mp4)

    # ── join (no picture re-encode) + one soundtrack pass for the whole video ──
    if progress_callback:
        progress_callback(len(chapters) + 1, len(chapters) + 1, "Joining chapters + music")
    import unicodedata
    safe_title = "".join(c for c in title if c.isalnum() or c in " _-"
                         or unicodedata.category(c).startswith("M"))[:50].strip() or "video"
    out_path = os.path.join(out_dir, f"{safe_title}.mp4")
    joined = os.path.join(out_dir, "_joined.mp4")
    lst = os.path.join(ch_dir, "_list.txt")
    with open(lst, "w") as f:
        f.writelines(f"file '{os.path.abspath(p_)}'\n" for p_ in done_paths)
    subprocess.run([FFMPEG_BIN, "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst,
                    "-c", "copy", "-movflags", "+faststart", joined], check=True)
    real_total = qa._probe(joined)["duration"] or total
    music_path, credit = music.track_for(template_name, key=title)
    if credit and credit not in script.get("description", ""):
        script["description"] = (script.get("description", "").rstrip() + "\n\n" + credit).strip()
    voice_wav = os.path.join(out_dir, "_voice.wav")
    mixed = os.path.join(out_dir, "_mix.wav")
    subprocess.run([FFMPEG_BIN, "-y", "-v", "error", "-i", joined, "-vn", "-ac", "2", "-ar", "48000", voice_wav],
                   check=True)
    margin = None
    if music_path:
        margin = mix_audio_track(voice_wav, music_path, real_total, mixed)
    else:
        subprocess.run([FFMPEG_BIN, "-y", "-v", "error", "-i", voice_wav, "-af",
                        "loudnorm=I=-14:TP=-1.0:LRA=11", "-ar", "48000", mixed], check=True)
    subprocess.run([FFMPEG_BIN, "-y", "-v", "error", "-i", joined, "-i", mixed, "-map", "0:v", "-map", "1:a",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-shortest",
                    "-movflags", "+faststart", out_path], check=True)
    master_loudness(out_path)
    for f in (joined, voice_wav, mixed, lst, os.path.join(out_dir, "_music_stem.wav")):
        if os.path.exists(f):
            os.remove(f)
    try:
        qa.check_final(checklist, out_path, None, "", None, [], audio_results[:n], margin, w, h, starts,
                       burn_checked_per_chapter=True)
    except qa.ChecklistFailed:
        os.replace(out_path, os.path.join(out_dir, "_rejected.mp4"))
        raise
    print(f"  📋 Checklist: all {len(checklist.items)} checks passed", flush=True)
    print(f"  ✅ Video saved: {out_path}")
    return out_path


# ─── Main Assembler ───────────────────────────────────────────────────────────

def assemble_video(script: dict, image_paths: list, audio_results: list,
                   job_id: str, progress_callback=None, checklist=None) -> str:
    """
    Assemble the final video using script["template"] (mystery | story | facts).
    image_paths: per scene, a media path or a list of paths (.mp4 clips / stills);
                 several clips per scene → fast ~2 s cuts.
    Returns the output .mp4 path.
    """
    if not MOVIEPY_AVAILABLE:
        raise RuntimeError("MoviePy v2 is not installed. Run: pip install 'moviepy>=2'")
    if script.get("video_type", "long") != "short" or script.get("chapters"):
        return assemble_chapters(script, image_paths, audio_results, job_id, progress_callback, checklist)

    template_name = (script.get("template") or T.DEFAULT_TEMPLATE).lower()
    tpl = T.get_template(template_name)
    video_type = script.get("video_type", "long")
    title = script.get("title", "Untitled Video")
    scenes = script.get("scenes", [])
    w, h = _dims(video_type)
    overlap = tpl["overlap"]

    total_steps = len(scenes) + 2
    step = [0]

    def _progress(msg):
        step[0] += 1
        if progress_callback:
            progress_callback(step[0], total_steps, msg)
        print(f"  [{step[0]}/{total_steps}] {msg}")

    numbered = range(1, len(scenes) - 1) if len(scenes) >= 4 else range(1, len(scenes))
    clips, info = [], []
    cursor = 0.0
    for i, scene in enumerate(scenes):
        sn = scene.get("scene_number", i + 1)
        if i >= len(image_paths) or i >= len(audio_results):
            print(f"  ⚠️  Skipping scene {sn}: missing media or audio")
            continue
        a = audio_results[i]
        _progress(f"Building scene {sn} ({a['duration']:.1f}s, {template_name})...")
        try:
            clip = build_scene_clip(tpl, image_paths[i], a["audio_path"], a["duration"], w, h, i,
                                    video_type, ambient=scene.get("ambient"))
        except Exception as e:
            print(f"  ⚠️  Scene {sn} media failed ({e}); using plain background")
            clip = build_scene_clip(tpl, None, a["audio_path"], a["duration"], w, h, i, video_type)
        if clips and overlap > 0:
            clip = clip.with_effects([vfx.CrossFadeIn(overlap)])
        clips.append(clip)
        info.append({"start": cursor, "duration": a["duration"], "words": a.get("words") or [],
                     "narration": a.get("narration") or scene.get("narration", ""),
                     "number": (i if tpl["layout"] == "card" and i in numbered else None)})
        cursor += clip.duration - overlap

    if not clips:
        raise RuntimeError("No clips were successfully built — cannot render video")

    _progress("Rendering video...")
    final = concatenate_videoclips(clips, method="compose", padding=-overlap if overlap else 0)
    final = final.with_audio(_mix_audio(final, tpl, template_name, [s["start"] for s in info]))

    out_dir = os.path.join(OUTPUT_DIR, job_id)
    os.makedirs(out_dir, exist_ok=True)
    import unicodedata
    safe_title = "".join(c for c in title if c.isalnum() or c in " _-"
                         or unicodedata.category(c).startswith("M"))[:50].strip() or "video"
    suffix = "_short" if video_type == "short" else ""
    out_path = os.path.join(out_dir, f"{safe_title}{suffix}.mp4")
    raw_path = os.path.join(out_dir, "_raw.mp4")

    total = final.duration
    # captions are written + checked (coverage, sync with the voice) BEFORE the slow render
    ass_path = os.path.join(out_dir, "captions.ass")
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(build_ass(tpl, title, info, w, h, video_type, total,
                          hook_text=script.get("hook_text"), cta_line=script.get("cta_text"),
                          template_name=template_name))
    starts = [s["start"] for s in info]
    from pipeline import qa                       # checks are compulsory for every render
    checklist = checklist or qa.for_job(job_id)
    spans = qa.check_captions(checklist, ass_path, audio_results[:len(info)], total, starts)

    final.write_videofile(raw_path, fps=FPS, codec="libx264", audio_codec="aac",
                          audio_bitrate="192k", preset="veryfast", threads=4,
                          ffmpeg_params=["-crf", "16", "-pix_fmt", "yuv420p"], logger=None)
    final.close()

    _progress("Grading + burning in captions...")
    music_path, credit = music.track_for(template_name, key=title)
    if credit and credit not in script.get("description", ""):
        script["description"] = (script.get("description", "").rstrip() + "\n\n" + credit).strip()
    # Final stage: music mix + colour grade + captions + loudness. Retried once; if it still
    # fails the render FAILS (never ship a video without captions / music / grade).
    voice_wav = os.path.join(out_dir, "_voice.wav")
    mixed = os.path.join(out_dir, "_mix.wav") if music_path else None
    last_err = None
    margin = None
    for attempt in (1, 2):
        try:
            if mixed:
                subprocess.run([FFMPEG_BIN, "-y", "-v", "error", "-i", raw_path, "-vn", "-ac", "2", "-ar", "48000",
                                voice_wav], check=True, capture_output=True)
                margin = mix_audio_track(voice_wav, music_path, total, mixed)
            _burn_subtitles(raw_path, ass_path, out_path, T.ffmpeg_look(tpl, w, h, total), audio=mixed)
            master_loudness(out_path)
            last_err = None
            break
        except Exception as e:
            import traceback
            last_err = e
            print(f"  ⚠️  Final stage failed (attempt {attempt}/2): {e}\n{traceback.format_exc()}", flush=True)
            if os.path.exists(out_path):
                os.remove(out_path)
    for f in (voice_wav, mixed, os.path.join(out_dir, "_music_stem.wav")):
        if f and os.path.exists(f):
            os.remove(f)
    if last_err is not None:
        raise RuntimeError(f"final stage (music/captions/grade) failed: {last_err}")
    try:
        qa.check_final(checklist, out_path, raw_path, T.ffmpeg_look(tpl, w, h, total), ass_path, spans,
                       audio_results[:len(info)], margin, w, h, starts)
    except qa.ChecklistFailed:
        # never leave a failed video where it could be mistaken for a finished one
        os.replace(out_path, os.path.join(out_dir, "_rejected.mp4"))
        os.remove(raw_path)
        raise
    print(f"  📋 Checklist: all {len(checklist.items)} checks passed", flush=True)
    os.remove(raw_path)

    print(f"  ✅ Video saved: {out_path}")
    return out_path
