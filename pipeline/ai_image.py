"""
pipeline/ai_image.py
AI image generation for the "animated movie" look. Providers, in order:

  1. Gemini image model (IMAGE_MODEL)  — best quality; needs billing enabled on the Gemini key
  2. Z-Image Turbo on this Mac (LOCAL_IMAGE_MODEL, MLX 4-bit in .venv-mflux)
                                       — free, unlimited, private, no logo (~6 GB one-time download)
  3. Pollinations.ai                   — with POLLINATIONS_TOKEN: fast, no logo, better models
                                         without a token: free, 1 image / 45 s, small logo

A provider that reports it is unavailable (no quota / not found) is skipped for the rest
of the process, so one refusal doesn't cost a retry on every shot.
"""

import os
import re
import sys
import time
import threading
import urllib.parse

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import (GEMINI_API_KEY, IMAGE_MODEL, POLLINATIONS_TOKEN, POLLINATIONS_MODEL,
                    LOCAL_IMAGES, LOCAL_IMAGE_MODEL)

try:
    from google import genai
    from google.genai import types as genai_types
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False

# Visual style presets — prepended to every shot prompt so the whole video looks like one film.
STYLES = {
    "animated_3d": ("3D animated feature film style, stylized like a modern animated movie, "
                    "soft global illumination, detailed animated movie frame"),
    "anime": ("anime film style, hand-painted detailed backgrounds, dramatic cel-shaded lighting, "
              "cinematic anime movie frame"),
    "comic": ("graphic-novel illustration style, bold ink outlines, dramatic shadows, "
              "limited moody colour palette, cinematic comic panel"),
}
NEGATIVE = "no text, no letters, no captions, no watermark, no logo"

_disabled = set()
_succeeded = set()     # providers that have produced at least one image this process
_lock = threading.Lock()
_last_free_call = [0.0]
FREE_INTERVAL = 46.0


# Mood per video template (Z-Image follows mood words closely; it ignores negative prompts)
MOODS = {
    "mystery": "Mood: eerie, mysterious and moody; dim light, muted desaturated colours, haze or fog, deep shadows.",
    "story": "Mood: warm, emotional, cinematic golden light.",
    "facts": "Mood: bright, clean, vivid and clear.",
}
# whole words only (a plain substring test matched "he " inside "the …" and put people in every shot)
PEOPLE = ("person", "people", "man", "men", "woman", "women", "boy", "girl", "child", "children", "kid", "kids",
          "villager", "fisherman", "fishermen", "soldier", "crowd", "family", "priest", "king", "queen",
          "prince", "princess", "tantric", "ranger", "scientist", "traveller", "traveler", "pilgrim",
          "figure", "silhouette", "character", "he", "she", "they", "hiker", "searcher", "student",
          "doctor", "investigator", "official", "herder", "hunter", "mother", "father", "friend", "group",
          "captain", "sailor", "crew", "keeper", "passenger", "guard", "face", "hand", "hands")
_PEOPLE_RE = __import__("re").compile(r"\b(" + "|".join(PEOPLE) + r")s?\b")


def style_prompt(style: str, shot: str, characters: str = "", setting: str = "", aspect: str = "9:16",
                 template: str = "mystery") -> str:
    base = STYLES.get(style, STYLES["animated_3d"])
    where = f" Setting: {setting}; every person, building and landscape must match this place and era." if setting else ""
    text = f" {shot.lower()} "
    for neg in ("no people", "no one", "nobody", "no person", "no humans", "without people", "deserted", "empty"):
        text = text.replace(neg, " ")
    has_people = bool(_PEOPLE_RE.search(text))
    if has_people:
        chars = f" Recurring characters (keep identical): {characters}." if characters else ""
    else:                                   # Z-Image adds people unless told the scene is empty
        chars = " Empty, deserted scene: no people, no figures, no characters."
    mood = " " + MOODS.get(template, MOODS["mystery"])
    frame = "Vertical 9:16" if aspect == "9:16" else "Wide 16:9"
    return f"{base}. Scene: {shot}.{where}{chars}{mood} {frame} cinematic composition. {NEGATIVE}."


def _unavailable(err) -> bool:
    s = str(err)
    return any(k in s for k in ("404", "NOT_FOUND", "PERMISSION_DENIED", "403",
                                "check your plan and billing", "limit: 0"))


def _gemini(prompt, out_path, aspect):
    client = genai.Client(api_key=GEMINI_API_KEY)
    r = client.models.generate_content(
        model=IMAGE_MODEL, contents=prompt,
        config=genai_types.GenerateContentConfig(
            response_modalities=["IMAGE"],
            image_config=genai_types.ImageConfig(aspect_ratio=aspect)))
    for part in (r.candidates[0].content.parts if r.candidates else []):
        if getattr(part, "inline_data", None) and part.inline_data.data:
            with open(out_path, "wb") as f:
                f.write(part.inline_data.data)
            return out_path
    raise RuntimeError("Gemini returned no image")


_local_lock = threading.Lock()


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ZIMAGE_PY = os.path.join(ROOT, ".venv-mflux", "bin", "python")
ZIMAGE_WORKER = os.path.join(ROOT, "pipeline", "zimage_worker.py")
_worker = [None]


def _zimage_proc(log):
    """Start (once) the Z-Image Turbo worker: a separate Python 3.12 + MLX process that keeps
    the model loaded between images."""
    import json
    import subprocess
    p = _worker[0]
    if p is not None and p.poll() is None:
        return p
    if not os.path.exists(ZIMAGE_PY):
        raise RuntimeError("Z-Image env missing (.venv-mflux) — see SETUP.md")
    log(f"  🖥️  Loading local image model Z-Image Turbo ({LOCAL_IMAGE_MODEL})...")
    p = subprocess.Popen([ZIMAGE_PY, ZIMAGE_WORKER], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True, bufsize=1,
                         env=dict(os.environ, ZIMAGE_MODEL=LOCAL_IMAGE_MODEL))
    hello = json.loads(p.stdout.readline() or '{"ok": false, "error": "worker exited"}')
    if not hello.get("ready"):
        p.kill()
        raise RuntimeError(hello.get("error", "Z-Image worker failed to start"))
    log(f"  🖥️  Z-Image Turbo ready (loaded in {hello.get('load_seconds')} s)")
    _worker[0] = p
    return p


def _local(prompt, out_path, aspect, seed, log):
    """Z-Image Turbo on this Mac (MLX, 4-bit): 704x1232 (or 1232x704), 6 steps — ~75 s/image on
    an M5; looks as good as 8 steps at 768x1344 (tested), 30% faster."""
    import json
    with _local_lock:                       # one generation at a time on the GPU
        p = _zimage_proc(log)
        w, h = (704, 1232) if aspect == "9:16" else (1232, 704)
        p.stdin.write(json.dumps({"prompt": prompt, "width": w, "height": h, "seed": int(seed),
                                  "steps": 6, "out": out_path}) + "\n")
        p.stdin.flush()
        r = json.loads(p.stdout.readline() or '{"ok": false, "error": "worker exited"}')
    if not r.get("ok"):
        raise RuntimeError(r.get("error", "Z-Image failed"))
    from PIL import Image
    if Image.open(out_path).convert("L").getextrema()[1] < 8:
        raise RuntimeError("local model produced a black image")
    return out_path


def unload_local():
    """Stop the image worker (frees its ~8 GB) before the memory-hungry render step."""
    p = _worker[0]
    _worker[0] = None
    if p is not None and p.poll() is None:
        try:
            p.stdin.close()
            p.wait(timeout=20)
        except Exception:
            p.kill()


def _pollinations(prompt, out_path, aspect, seed, log):
    w, h = (768, 1344) if aspect == "9:16" else (1344, 768)
    params = {"width": w, "height": h, "seed": seed}
    headers = {}
    if POLLINATIONS_TOKEN:
        headers["Authorization"] = f"Bearer {POLLINATIONS_TOKEN}"
        params.update({"nologo": "true", "model": POLLINATIONS_MODEL})
    url = "https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt[:1800])

    for attempt in range(4):
        if not POLLINATIONS_TOKEN:
            with _lock:
                wait = FREE_INTERVAL - (time.time() - _last_free_call[0])
                if wait > 0:
                    log(f"  ⏳ Free image tier: waiting {wait:.0f}s (set POLLINATIONS_TOKEN to remove)")
                    time.sleep(wait)
                _last_free_call[0] = time.time()
        r = requests.get(url, params=params, headers=headers, timeout=180)
        if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
            with open(out_path, "wb") as f:
                f.write(r.content)
            return out_path
        m = re.search(r"Retry after (\d+)s", r.text)
        if r.status_code in (402, 429) and m and attempt < 3:
            time.sleep(int(m.group(1)) + 2)
            continue
        raise RuntimeError(f"Pollinations {r.status_code}: {r.text[:160]}")
    raise RuntimeError("Pollinations: retries exhausted")


def generate_image(prompt: str, out_path: str, aspect: str = "9:16", seed: int = 0,
                   log=print) -> str:
    """Return out_path on success, or None if every provider failed."""
    if os.path.exists(out_path) and os.path.getsize(out_path) > 5000:
        return out_path          # resume: already generated in an earlier (interrupted) run
    if GENAI_AVAILABLE and "gemini" not in _disabled:
        try:
            out = _gemini(prompt, out_path, aspect)
            _succeeded.add("gemini")
            return out
        except Exception as e:
            if _unavailable(e):
                _disabled.add("gemini")
                log("  ⚠️  Gemini image model unavailable on this key (enable billing to use it) — "
                    "using Pollinations")
            else:
                log(f"  ⚠️  Gemini image failed: {str(e)[:120]}")
    if LOCAL_IMAGES and "local" not in _disabled:
        try:
            return _local(prompt, out_path, aspect, seed, log)
        except Exception as e:
            _disabled.add("local")
            log(f"  ⚠️  Local image model unavailable ({str(e)[:140]}) — using Pollinations")
    if "pollinations" not in _disabled:
        try:
            return _pollinations(prompt, out_path, aspect, seed, log)
        except Exception as e:
            log(f"  ⚠️  Pollinations image failed: {str(e)[:160]}")
    return None
