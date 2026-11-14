"""
pipeline/thumbnail.py: 1280×720 YouTube thumbnail for long videos.

The picture is one of the episode's own images (script["thumbnail"]["scene"] / ["image"],
default scene 1, image 1), punched up (contrast, saturation, vignette, dark side for the text).
The text is 2–4 big Hindi words (script["thumbnail"]["text"], else hook_text) in white and
yellow with a heavy outline. It is drawn with libass, which shapes Devanagari correctly
(Pillow here can't: no raqm). Shorts don't use this: YouTube picks their frame.

    "thumbnail": {"scene": 12, "image": 1, "text": "टेंट अंदर से कटा था", "side": "left"}
"""
import os
import shutil
import subprocess
import tempfile

from config import OUTPUT_DIR, SUBTITLE_FONT, SUBTITLE_FONTS_DIR

try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG = "ffmpeg"

W, H = 1280, 720


def _lines(text):
    """Split into two balanced lines (the second, in yellow, gets the punchline)."""
    words = text.split()
    if len(words) <= 2:
        return [text]
    k = (len(words) + 1) // 2
    return [" ".join(words[:k]), " ".join(words[k:])]


def make_thumbnail(script, job_id, log=print):
    """Writes <job>/thumbnail.jpg and returns its path, or None if there is no image/text."""
    cfg = script.get("thumbnail") or {}
    out_dir = os.path.join(OUTPUT_DIR, job_id)
    sn, k = int(cfg.get("scene", 1)), int(cfg.get("image", 1))
    img = os.path.join(out_dir, "clips", f"scene_{sn:02d}_{k}.jpg")
    if not os.path.exists(img):
        alts = sorted(f for f in os.listdir(os.path.join(out_dir, "clips")) if f.endswith(".jpg")) \
            if os.path.isdir(os.path.join(out_dir, "clips")) else []
        if not alts:
            return None
        img = os.path.join(out_dir, "clips", alts[0])
    text = (cfg.get("text") or script.get("hook_text") or "").replace("!", "").strip()
    if not text:
        return None
    side = cfg.get("side", "left")
    lines = _lines(text)
    size = 140 if max(len(l) for l in lines) <= 14 else 112
    align, margin = (4, 70) if side == "left" else (6, 70)
    body = r"\N".join(
        (r"{\c&H00FFFFFF&}" if i == 0 and len(lines) > 1 else r"{\c&H0000E1FF&}") + l
        for i, l in enumerate(lines))
    ass = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: T,{SUBTITLE_FONT},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H99000000,-1,0,0,0,100,100,0,0,1,9,5,{align},{margin},{margin},0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:05.00,T,,0,0,0,,{body}
"""
    # dark side behind the text: a horizontal gradient, strongest at the text edge
    grad = "(1-X/W)" if side == "left" else "(X/W)"
    tmp = tempfile.mkdtemp()
    try:
        open(os.path.join(tmp, "t.ass"), "w", encoding="utf-8").write(ass)
        out = os.path.join(out_dir, "thumbnail.jpg")
        vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
              f"eq=contrast=1.12:saturation=1.25:brightness=0.03:gamma=1.12,unsharp=5:5:0.3,vignette=PI/4.5,"
              f"format=rgb24,geq=r='r(X,Y)*(1-0.55*pow({grad},1.6))':"
              f"g='g(X,Y)*(1-0.55*pow({grad},1.6))':b='b(X,Y)*(1-0.55*pow({grad},1.6))',"
              f"ass=t.ass:shaping=complex:fontsdir={SUBTITLE_FONTS_DIR}")
        r = subprocess.run([FFMPEG, "-y", "-v", "error", "-i", os.path.abspath(img), "-vf", vf,
                            "-frames:v", "1", "-q:v", "2", os.path.abspath(out)],
                           cwd=tmp, capture_output=True, text=True)
        if r.returncode or not os.path.exists(out):
            log(f"  ⚠️  Thumbnail failed: {r.stderr[-300:]}")
            return None
        if os.path.getsize(out) > 2_000_000:          # YouTube limit: 2 MB
            subprocess.run([FFMPEG, "-y", "-v", "error", "-i", out, "-q:v", "5", out + ".jpg"], check=True)
            shutil.move(out + ".jpg", out)
        log(f"  🖼️  Thumbnail: scene {sn} image {k} + \"{text}\" → {out}")
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
