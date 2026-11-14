"""
pipeline/animated_video_generator.py
Produces the visuals for each scene in the chosen style:

  style "animated_3d" / "anime" / "comic"  (animated-movie look)
    1. Google Veo (real AI animation)            — needs billing on the Gemini key
    2. AI images in that style (pipeline/ai_image) + camera motion + ambient FX
    3. Pexels stock footage                       — last resort
  style "stock"
    Pexels stock footage only

Each backend is switched off for the rest of the run the first time it reports
that it is unavailable (404 / no quota), so a missing model costs one request.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import GEMINI_API_KEY, OUTPUT_DIR, VEO_MODEL, VISUAL_STYLE, POLLINATIONS_TOKEN, USE_VEO
from pipeline.image_fetcher import fetch_media_set, clips_needed
from pipeline import ai_image

try:
    from google import genai
    from google.genai import types as genai_types
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False
    print("⚠️  google-genai not installed. Run: pip install google-genai")

class BackendUnavailable(Exception):
    pass


def _is_unavailable(err: Exception) -> bool:
    s = str(err)
    return any(k in s for k in ("404", "NOT_FOUND", "PERMISSION_DENIED", "403",
                                "check your plan and billing", "limit: 0"))


def _is_rate_limited(err: Exception) -> bool:
    s = str(err)
    return "429" in s or "RESOURCE_EXHAUSTED" in s


def _client():
    return genai.Client(api_key=GEMINI_API_KEY)


def _veo_clip(prompt, out_path, aspect_ratio, log):
    client = _client()
    op = None
    for attempt, wait in enumerate([30, 60, 0]):
        try:
            op = client.models.generate_videos(
                model=VEO_MODEL, prompt=f"{prompt} Smooth cinematic camera movement.",
                config=genai_types.GenerateVideosConfig(aspect_ratio=aspect_ratio,
                                                        number_of_videos=1))
            break
        except Exception as e:
            if _is_unavailable(e):
                raise BackendUnavailable(str(e)[:200])
            if _is_rate_limited(e) and wait:
                log(f"  ⏳ Veo rate limited, retrying in {wait}s...")
                time.sleep(wait)
                continue
            raise
    if op is None:
        raise RuntimeError("Veo did not accept the request")

    deadline = time.time() + 360
    while not op.done:
        if time.time() > deadline:
            raise RuntimeError("Veo timed out")
        time.sleep(8)
        op = client.operations.get(op)

    vids = getattr(op.response, "generated_videos", None) if op.response else None
    if not vids:
        raise RuntimeError("Veo returned no video (possibly safety-filtered)")
    client.files.download(file=vids[0].video)
    vids[0].video.save(out_path)
    return out_path


def _shot_prompts(scene, n):
    """Per-shot descriptions: the script's image_prompts, padded with framing variations."""
    shots = [p for p in (scene.get("image_prompts") or []) if p and p.strip()]
    base = (scene.get("visual_description") or scene.get("narration") or "a dramatic scene").strip()
    if not shots:
        shots = [base]
    framings = ["wide establishing shot of", "dramatic close-up of", "low-angle shot of"]
    k = 0
    while len(shots) < n:
        shots.append(f"{framings[k % len(framings)]} {base}")
        k += 1
    return shots[:n]


def generate_clips_for_script(script: dict, job_id: str, video_type: str = "short",
                              progress_callback=None, durations: list = None,
                              style: str = None) -> list:
    """Return, per scene, a media path or a list of paths (.mp4 clips / images)."""
    log = (lambda m: (print(m), progress_callback and progress_callback(m)))
    style = (style or script.get("style") or VISUAL_STYLE).lower()
    aspect = "9:16" if video_type == "short" else "16:9"
    media_dir = os.path.join(OUTPUT_DIR, job_id, "clips")
    os.makedirs(media_dir, exist_ok=True)
    topic = script.get("topic") or script.get("title", "")
    characters = script.get("characters", "")
    setting = script.get("setting", "")

    animated = style != "stock"
    enabled = {"veo": GENAI_AVAILABLE and animated and USE_VEO, "image": animated}
    used_stock = set()
    results = []
    counts = {"veo": 0, "image": 0, "stock": 0}
    seed_base = abs(hash(topic)) % 100000

    for i, scene in enumerate(script.get("scenes", [])):
        sn = scene.setdefault("scene_number", i + 1)
        need = clips_needed(durations[i], video_type) if durations and i < len(durations) else 1
        path = None

        if enabled["veo"]:
            try:
                log(f"  🎬 Scene {sn}: generating animation with {VEO_MODEL}...")
                prompt = ai_image.style_prompt(style, _shot_prompts(scene, 1)[0], characters, setting, aspect,
                                                script.get("template", "mystery"))
                path = _veo_clip(prompt, os.path.join(media_dir, f"scene_{sn:02d}.mp4"), aspect, log)
                counts["veo"] += 1
            except BackendUnavailable as e:
                enabled["veo"] = False
                log(f"  ⚠️  Veo unavailable on this API key — using AI images ({str(e)[:100]})")
            except Exception as e:
                log(f"  ⚠️  Veo failed for scene {sn}: {str(e)[:150]}")

        if not path and enabled["image"]:
            # Free Pollinations tier is throttled (1 image / 45 s): 1 image per scene,
            # the builder re-frames it with different camera moves. Otherwise up to 3.
            cloud_fast = bool(POLLINATIONS_TOKEN) or "gemini" in ai_image._succeeded
            local = ai_image.LOCAL_IMAGES and "local" not in ai_image._disabled
            # cloud: up to 3 shots · local Mac GPU (~25 s each): 2 · free throttled tier: 1
            n = min(3, need) if cloud_fast else min(2, need) if local else 1
            imgs = []
            for k, shot in enumerate(_shot_prompts(scene, n)):
                log(f"  🎨 Scene {sn}: AI {style} image {k + 1}/{n}...")
                out = ai_image.generate_image(
                    ai_image.style_prompt(style, shot, characters, setting, aspect, script.get("template", "mystery")),
                    os.path.join(media_dir, f"scene_{sn:02d}_{k + 1}.jpg"),
                    aspect, seed=seed_base + i * 10 + k, log=log)
                if out:
                    imgs.append(out)
            if imgs:
                path = imgs
                counts["image"] += len(imgs)
            else:
                log(f"  ⚠️  No AI image for scene {sn} — falling back to stock footage")

        if not path:
            path = fetch_media_set(scene, job_id, video_type, topic, used_stock, need)
            counts["stock"] += 1

        results.append(path)

    ai_image.unload_local()
    from pipeline.upscale import upscale_all                    # after the image model is unloaded
    upscale_all(results, style, log)
    log(f"🎬 Visuals ready — Veo clips: {counts['veo']}, AI images: {counts['image']}, "
        f"stock scenes: {counts['stock']}")
    from pipeline import qa
    qa.check_visuals(qa.for_job(job_id), script, results)      # compulsory: raises if any fail
    return results


if __name__ == "__main__":
    out = generate_clips_for_script(
        {"title": "Test", "scenes": [{"visual_description":
            "An ancient sailing ship caught in a violent ocean storm at night, lightning"}]},
        "test_veo", "short")
    print(out)
