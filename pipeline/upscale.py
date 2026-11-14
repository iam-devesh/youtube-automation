"""
pipeline/upscale.py: 2× upscale of the generated images, so 1080p long videos (and 1080×1920
Shorts) stay sharp. Z-Image makes 1232×704 (or 704×1232) images; the camera motion zooms in on
top of that, so without this they are stretched ~1.7×.

Real-ESRGAN (ncnn/Vulkan build, runs on the Mac GPU, ~1–3 s per image), chosen by style:
  anime        → realesr-animevideov3 (×2): clean outlines, flat colours
  everything else (3D, comic) → realesrgan-x4plus (×4, then down to ×2). NOT used yet: on a 3D
               image it looked painted, so upscale_all only runs for anime
Runs after ALL images exist and the image model is unloaded (never two GPU jobs at once).
Already-upscaled images are skipped, so an interrupted Prepare resumes.
Setup: models/realesrgan/ (official macOS release of xinntao/Real-ESRGAN v0.2.5.0).
"""
import os
import subprocess
import tempfile

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN_DIR = os.getenv("REALESRGAN_DIR", os.path.join(ROOT, "models", "realesrgan"))
BIN = os.path.join(BIN_DIR, "realesrgan-ncnn-vulkan")
DONE_SIDE = 2000           # an image whose long side is ≥ this is already upscaled


def _model(style):
    return ("realesr-animevideov3", 2) if (style or "").lower() == "anime" else ("realesrgan-x4plus", 4)


def upscale_image(path, style, log=print):
    """Upscale one image in place to 2×. Returns True if it was upscaled now."""
    with Image.open(path) as im:
        w, h = im.size
    if max(w, h) >= DONE_SIDE:
        return False
    name, scale = _model(style)
    fd, tmp = tempfile.mkstemp(suffix=".png", dir=os.path.dirname(path))
    os.close(fd)
    try:
        r = subprocess.run([BIN, "-i", path, "-o", tmp, "-n", name, "-s", str(scale), "-m",
                            os.path.join(BIN_DIR, "models")], capture_output=True, text=True, timeout=300)
        if r.returncode or not os.path.exists(tmp) or os.path.getsize(tmp) < 10000:
            raise RuntimeError((r.stderr or "no output")[-200:])
        with Image.open(tmp) as up:
            up = up.convert("RGB")
            if up.size != (w * 2, h * 2):
                up = up.resize((w * 2, h * 2), Image.LANCZOS)
            up.save(path, quality=95)
        return True
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def upscale_all(media, style, log=print):
    """media: per scene, a path or a list of paths (as generate_clips_for_script returns).
    Upscales every still image; never fails the Prepare (a sharp-enough image beats none)."""
    if (style or "").lower() != "anime":
        return              # tested: the 3D upscaler makes 3D renders look painted — anime only for now
    if not os.path.exists(BIN):
        log("  ⚠️  Upscaler not installed (models/realesrgan) — images used at their original size")
        return
    paths = [p for m in media for p in (m if isinstance(m, (list, tuple)) else [m])
             if p and p.lower().endswith((".jpg", ".jpeg", ".png")) and os.path.exists(p)]
    name, _ = _model(style)
    done = 0
    for i, p in enumerate(paths):
        try:
            if upscale_image(p, style, log):
                done += 1
                if done % 10 == 1:
                    log(f"  🔍 Upscaling images ({name}, 2×): {i + 1}/{len(paths)}")
        except Exception as e:
            log(f"  ⚠️  Upscale failed for {os.path.basename(p)} ({str(e)[:120]}) — kept original")
    if done:
        log(f"  🔍 {done} images upscaled 2× with {name}")
