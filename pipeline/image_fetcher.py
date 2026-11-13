"""
pipeline/image_fetcher.py
Fetches scene media from Pexels: a matching stock video clip, or (if none) a
stock photo that the video builder animates with a Ken Burns move.
"""

import os
import sys
import re
import subprocess
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import (PEXELS_API_KEY, OUTPUT_DIR, VIDEO_WIDTH, VIDEO_HEIGHT,
                    SHORT_WIDTH, SHORT_HEIGHT)

PEXELS_VIDEO_URL = "https://api.pexels.com/videos/search"
PEXELS_PHOTO_URL = "https://api.pexels.com/v1/search"

# Words that describe *how* a shot looks, not *what* is in it. They wreck stock searches
# (e.g. "Cinematic aerial view of..." used to return random aerial footage).
_FILLER = set("""
cinematic dramatic epic aerial view shot shots close closeup close-up wide angle slow motion
camera pan panning zoom zooming tracking scene footage ultra detailed 4k 8k hd hdr realistic
photorealistic hyperrealistic render rendering style lighting light lit beautiful stunning
mysterious dark moody vibrant glowing intense massive huge giant tiny showing shows depicting
with from into onto over under above below of the a an and or in on at to for by its it is are
this that these those very some many while as like then near behind front background foreground
""".split())

try:
    import imageio_ffmpeg
    _FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    _FFMPEG = "ffmpeg"


def _keywords(text: str, n: int = 3) -> str:
    words = re.findall(r"[A-Za-z][A-Za-z'-]+", text or "")
    kept = [w.lower() for w in words if w.lower() not in _FILLER and len(w) > 2]
    return " ".join(kept[:n])


def _queries_for(scene: dict, topic: str) -> list:
    """Most specific first, broadest last; de-duplicated."""
    cands = [
        scene.get("search_query", ""),
        _keywords(scene.get("visual_description", ""), 3),
        _keywords(scene.get("visual_description", ""), 2),
        _keywords(topic, 2),
    ]
    out = []
    for q in cands:
        q = (q or "").strip()
        if q and q not in out:
            out.append(q)
    return out


def _pick_video_file(files, target_w, target_h):
    """Smallest file that still covers the target frame; else the largest available."""
    portrait = target_h > target_w
    usable = [f for f in files if f.get("link") and f.get("width") and f.get("height")
              and (f["height"] > f["width"]) == portrait]
    if not usable:
        return None
    short_side = min(target_w, target_h)
    big_enough = [f for f in usable if min(f["width"], f["height"]) >= short_side]
    if big_enough:
        return min(big_enough, key=lambda f: f["width"] * f["height"])
    return max(usable, key=lambda f: f["width"] * f["height"])


def _download(url, path):
    r = requests.get(url, stream=True, timeout=60)
    r.raise_for_status()
    with open(path, "wb") as f:
        for chunk in r.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)
    return path


def _brightness(path):
    """Mean luma (0-255) of a frame ~1 s in; None if it can't be measured."""
    try:
        res = subprocess.run([_FFMPEG, "-v", "error", "-ss", "1", "-i", path, "-frames:v", "1",
                              "-vf", "scale=32:32,format=gray", "-f", "rawvideo", "-"],
                             capture_output=True, timeout=30)
        return sum(res.stdout) / len(res.stdout) if res.stdout else None
    except Exception:
        return None


MIN_BRIGHTNESS = 38   # below this the clip reads as a black screen on a phone


def _search_video(query, video_type, used_ids, min_duration):
    w, h = (SHORT_WIDTH, SHORT_HEIGHT) if video_type == "short" else (VIDEO_WIDTH, VIDEO_HEIGHT)
    params = {"query": query, "per_page": 15, "size": "medium",
              "orientation": "portrait" if video_type == "short" else "landscape"}
    resp = requests.get(PEXELS_VIDEO_URL, headers={"Authorization": PEXELS_API_KEY},
                        params=params, timeout=15)
    if resp.status_code != 200:
        print(f"  ⚠️  Pexels video API {resp.status_code}: {resp.text[:200]}")
        return None, None
    videos = [v for v in resp.json().get("videos", []) if v.get("id") not in used_ids]
    # Prefer clips long enough to avoid visible looping, keep Pexels' relevance order
    videos.sort(key=lambda v: v.get("duration", 0) < min_duration)
    out = []
    for v in videos:
        f = _pick_video_file(v.get("video_files", []), w, h)
        if f:
            out.append((v["id"], f["link"]))
    return out


def _search_photo(query, video_type, used_ids):
    params = {"query": query, "per_page": 15,
              "orientation": "portrait" if video_type == "short" else "landscape"}
    resp = requests.get(PEXELS_PHOTO_URL, headers={"Authorization": PEXELS_API_KEY},
                        params=params, timeout=15)
    if resp.status_code != 200:
        return None, None
    for p in resp.json().get("photos", []):
        if p.get("id") in used_ids:
            continue
        src = p.get("src", {})
        link = src.get("large2x") or src.get("original")
        if link:
            return p["id"], link
    return None, None


def fetch_media_set(scene: dict, job_id: str, video_type: str = "long", topic: str = "",
                    used_ids: set = None, count: int = 1) -> list:
    """
    Return up to `count` distinct media paths (.mp4 stock clips, else .jpg photos) for a
    scene. Several clips per scene let the video cut every ~2 s like viral Shorts.
    Always returns at least one path.
    """
    used_ids = used_ids if used_ids is not None else set()
    sn = scene.get("scene_number", 0)
    media_dir = os.path.join(OUTPUT_DIR, job_id, "images")
    os.makedirs(media_dir, exist_ok=True)
    min_dur = 3 if video_type == "short" else 8
    count = max(1, int(count))
    paths = []

    def name(ext):
        return os.path.join(media_dir, f"scene_{sn:02d}_{len(paths) + 1}.{ext}")

    if not PEXELS_API_KEY or PEXELS_API_KEY.lower().startswith("your_"):
        print("  ⚠️  PEXELS_API_KEY missing — cannot fetch stock media")
        return [_generate_placeholder(name("mp4"), video_type)]

    queries = _queries_for(scene, topic)
    for q in queries:
        if len(paths) >= count:
            break
        try:
            print(f"  🎬 Pexels video search for scene {sn}: '{q}'")
            tries = 0
            for vid, link in _search_video(q, video_type, used_ids, min_dur):
                if len(paths) >= count or tries >= count + 3:
                    break
                tries += 1
                used_ids.add(vid)
                path = _download(link, name("mp4"))
                lum = _brightness(path)
                if lum is not None and lum < MIN_BRIGHTNESS:
                    print(f"  ↪️  Clip too dark (luma {lum:.0f}), skipping")
                    os.remove(path)
                    continue
                paths.append(path)
        except Exception as e:
            print(f"  ⚠️  Pexels video search failed ('{q}'): {e}")

    if not paths:
        for q in queries:
            try:
                pid, link = _search_photo(q, video_type, used_ids)
                if link:
                    used_ids.add(pid)
                    print(f"  🖼️  Using Pexels photo for scene {sn} ('{q}')")
                    paths.append(_download(link, name("jpg")))
                    break
            except Exception as e:
                print(f"  ⚠️  Pexels photo search failed ('{q}'): {e}")

    if not paths:
        print(f"  ⚠️  No stock media found for scene {sn} — using placeholder")
        paths.append(_generate_placeholder(name("mp4"), video_type))
    return paths


def fetch_media_for_scene(scene: dict, job_id: str, video_type: str = "long",
                          topic: str = "", used_ids: set = None, count: int = 1):
    """count == 1 → single path (back-compat); count > 1 → list of paths."""
    paths = fetch_media_set(scene, job_id, video_type, topic, used_ids, count)
    return paths[0] if count == 1 else paths


def clips_needed(duration: float, video_type: str = "short") -> int:
    """Viral pacing: a new shot every ~2.2 s on Shorts, ~4 s on long-form."""
    seg = 2.2 if video_type == "short" else 4.0
    return max(1, min(5, round(duration / seg)))


def _generate_placeholder(output_path: str, video_type: str) -> str:
    w, h = (SHORT_WIDTH, SHORT_HEIGHT) if video_type == "short" else (VIDEO_WIDTH, VIDEO_HEIGHT)
    cmd = [_FFMPEG, "-y", "-f", "lavfi", "-i", f"color=c=0x0a0a19:s={w}x{h}:d=5:r=30",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", output_path]
    subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return output_path


# Back-compat name
def fetch_image_for_scene(visual_description: str, scene_number: int,
                          job_id: str, video_type: str = "long") -> str:
    return fetch_media_for_scene({"scene_number": scene_number,
                                  "visual_description": visual_description},
                                 job_id, video_type)


def fetch_images_for_script(script_json: dict, job_id: str, video_type: str = None,
                            durations: list = None) -> list:
    """
    Fetch media for every scene, never reusing the same Pexels asset twice.
    With `durations` (seconds of narration per scene) each entry is a LIST of clips
    sized for fast cuts; without it each entry is a single path.
    """
    video_type = video_type or script_json.get("video_type", "long")
    topic = script_json.get("topic") or script_json.get("title", "")
    used = set()
    results = []
    print(f"  🖼️  Fetching stock media (Pexels, {video_type})...")
    for i, scene in enumerate(script_json.get("scenes", [])):
        scene.setdefault("scene_number", i + 1)
        if durations and i < len(durations):
            results.append(fetch_media_set(scene, job_id, video_type, topic, used,
                                           clips_needed(durations[i], video_type)))
        else:
            results.append(fetch_media_for_scene(scene, job_id, video_type, topic, used))
    print(f"  [Job {job_id}] 🖼️  media ready for {len(results)} scenes")
    from pipeline import qa
    qa.check_visuals(qa.for_job(job_id), script_json, results)   # compulsory
    return results
