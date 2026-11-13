"""
pipeline/ai_image.py
AI image generation for the "animated movie" look. Providers, in order:

  1. Gemini image model (IMAGE_MODEL)  — best quality; needs billing enabled on the Gemini key
  2. Local model on this Mac (LOCAL_IMAGE_MODEL, Apple-Silicon GPU via PyTorch MPS)
                                       — free, unlimited, private, no logo (~7 GB one-time download)
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
    "animated_3d": ("3D animated feature film style, stylized expressive cartoon characters, "
                    "soft global illumination, rich cinematic colour, detailed animated movie frame"),
    "anime": ("anime film style, hand-painted detailed backgrounds, dramatic cel-shaded lighting, "
              "expressive characters, cinematic anime movie frame"),
    "comic": ("graphic-novel illustration style, bold ink outlines, dramatic shadows, "
              "limited moody colour palette, cinematic comic panel"),
}
NEGATIVE = "no text, no letters, no captions, no watermark, no logo"

_disabled = set()
_succeeded = set()     # providers that have produced at least one image this process
_lock = threading.Lock()
_last_free_call = [0.0]
FREE_INTERVAL = 46.0


def style_prompt(style: str, shot: str, characters: str = "", setting: str = "") -> str:
    base = STYLES.get(style, STYLES["animated_3d"])
    where = f" Setting: {setting}; every person, building and landscape must match this place and era." if setting else ""
    chars = f" Recurring characters (keep identical): {characters}." if characters else ""
    return f"{base}. Scene: {shot}.{where}{chars} Vertical cinematic composition. {NEGATIVE}."


LOCAL_STYLES = {
    "animated_3d": "3d animated movie still, pixar style cartoon render, cinematic lighting",
    "anime": "anime movie still, detailed painted background, cel shading, cinematic lighting",
    "comic": "graphic novel panel, bold ink lines, dramatic shadows, muted colours",
}


def local_style_prompt(style: str, shot: str, characters: str = "", setting: str = "") -> str:
    """
    Local SDXL reads only ~77 tokens: style first, then the setting (so place/ethnicity
    always survives truncation — SDXL otherwise drifts to Western faces and towns), then the shot.
    """
    chars = f", {characters}" if characters and any(
        k in shot.lower() for k in ("fisherman", "boy", "girl", "man", "woman", "character", "he ", "she ")) else ""
    where = f", {setting}" if setting else ""
    return f"{LOCAL_STYLES.get(style, LOCAL_STYLES['animated_3d'])}{where}, {shot}{chars}"


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


_local_pipe = [None]
_local_lock = threading.Lock()


def _local_pipeline(log):
    if _local_pipe[0] is None:
        import torch
        from diffusers import StableDiffusionXLPipeline, DPMSolverMultistepScheduler
        if not torch.backends.mps.is_available() and not torch.cuda.is_available():
            raise RuntimeError("no GPU (MPS/CUDA) available for local image generation")
        device = "mps" if torch.backends.mps.is_available() else "cuda"
        log(f"  🖥️  Loading local image model {LOCAL_IMAGE_MODEL} (first run downloads ~7 GB)...")
        pipe = StableDiffusionXLPipeline.from_pretrained(
            LOCAL_IMAGE_MODEL, torch_dtype=torch.float16, variant="fp16", use_safetensors=True)
        pipe.scheduler = DPMSolverMultistepScheduler.from_config(
            pipe.scheduler.config, algorithm_type="sde-dpmsolver++", use_karras_sigmas=True)
        pipe = pipe.to(device)
        # SDXL's VAE overflows in fp16 (→ NaN → black image), notably on Apple MPS.
        # Decode in float32; the UNet stays fp16 so speed/memory barely change.
        pipe.vae.to(torch.float32)
        pipe.vae.config.force_upcast = False
        # NOTE: no enable_attention_slicing() — on Apple MPS at 768x1344 it yields NaN latents
        # (all-black images). 16 GB of unified memory is enough without it.
        _local_pipe[0] = (pipe, device)
    return _local_pipe[0]


def _local(prompt, out_path, aspect, seed, log):
    import torch
    with _local_lock:                       # one generation at a time on the GPU
        pipe, device = _local_pipeline(log)
        # Lightning model: 4 steps, no classifier-free guidance (guidance 1.0 = one UNet pass
        # per step). 640x1152 keeps it ~25 s/image on an M-series Mac with 16 GB; the video
        # builder upscales with its Ken Burns move.
        w, h = (640, 1152) if aspect == "9:16" else (1152, 640)
        gen = torch.Generator("cpu").manual_seed(int(seed))
        latents = pipe(prompt=prompt, width=w, height=h, num_inference_steps=4,
                       guidance_scale=1.0, generator=gen, output_type="latent").images
        with torch.no_grad():
            latents = latents.to(torch.float32) / pipe.vae.config.scaling_factor
            decoded = pipe.vae.decode(latents, return_dict=False)[0]
        img = pipe.image_processor.postprocess(decoded, output_type="pil")[0]
    if img.getextrema() == ((0, 0), (0, 0), (0, 0)):
        raise RuntimeError("local model produced a black image")
    img.save(out_path, quality=95)
    return out_path


def unload_local():
    """Free the local model's ~7 GB before the memory-hungry render step."""
    if _local_pipe[0] is not None:
        _local_pipe[0] = None
        import gc
        gc.collect()
        try:
            import torch
            if torch.backends.mps.is_available():
                torch.mps.empty_cache()
        except Exception:
            pass


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
                   log=print, local_prompt: str = None) -> str:
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
            return _local(local_prompt or prompt, out_path, aspect, seed, log)
        except Exception as e:
            _disabled.add("local")
            log(f"  ⚠️  Local image model unavailable ({str(e)[:140]}) — using Pollinations")
    if "pollinations" not in _disabled:
        try:
            return _pollinations(prompt, out_path, aspect, seed, log)
        except Exception as e:
            log(f"  ⚠️  Pollinations image failed: {str(e)[:160]}")
    return None
