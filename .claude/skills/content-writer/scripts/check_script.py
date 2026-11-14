"""
check_script.py: validate a script JSON before Prepare.

Runs the pipeline's own preflight (pipeline/qa.py::check_script) without creating a job, then
adds the checks that matter for long videos (length, images per scene, Prepare time).

    python3 .claude/skills/content-writer/scripts/check_script.py scripts/<series>/<file>.json
"""
import json
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
sys.path.insert(0, ROOT)

from pipeline import qa  # noqa: E402

WPS = 2.3                 # measured narrator pace, words per second
SEC_PER_IMAGE = 77        # Z-Image Turbo on this Mac
AMBIENTS = {"rain", "storm", "fog", "dust", "embers", "none"}
FIXED = {"lang": "hi", "template": "mystery", "voice": "gemini"}
STYLES = {"animated_3d", "anime", "comic"}


class DryChecklist(qa.Checklist):
    def __init__(self):
        self.items = []

    def _save(self, failed_stage=None):
        pass


def main(path):
    script = json.load(open(path, encoding="utf-8"))
    scenes = script.get("scenes", [])
    long_video = script.get("video_type", "short") != "short"
    cl = DryChecklist()
    try:
        qa.check_script(cl, script)
    except qa.ChecklistFailed:
        pass                                   # keep going: report everything at once

    S = "Writer"
    for k, v in FIXED.items():
        cl.check(S, f'"{k}" is "{v}"', script.get(k) == v, f"got {script.get(k)!r}")
    cl.check(S, "style is one of " + "/".join(sorted(STYLES)), script.get("style") in STYLES, str(script.get("style")))
    cl.check(S, "setting present", bool((script.get("setting") or "").strip()))
    cl.check(S, "hook_text ≤5 words", 0 < len((script.get("hook_text") or "").split()) <= 5)
    title = script.get("title") or ""
    cl.check(S, "title ≤70 chars, Roman script", len(title) <= 70 and not re.search(r"[ऀ-ॿ]", title),
             f"{len(title)} chars")
    tags = script.get("tags") or []
    cl.check(S, "12–16 tags", 12 <= len(tags) <= 16, f"{len(tags)} tags")
    nums = [s.get("scene_number") for s in scenes]
    cl.check(S, "scene numbers 1..N in order", nums == list(range(1, len(scenes) + 1)))
    bad_amb = [s.get("scene_number") for s in scenes if s.get("ambient", "none") not in AMBIENTS]
    cl.check(S, "ambient values valid", not bad_amb, f"scenes {bad_amb}" if bad_amb else "")
    need = 2
    few = [s.get("scene_number") for s in scenes if len(s.get("image_prompts") or []) < need]
    cl.check(S, f"≥{need} image prompts per scene", not few, f"scenes {few}" if few else "")
    nosearch = [s.get("scene_number") for s in scenes if not (s.get("search_query") or "").strip()]
    cl.check(S, "search_query on every scene", not nosearch, f"scenes {nosearch}" if nosearch else "")

    words = sum(len((s.get("narration") or "").split()) for s in scenes)
    secs = words / WPS + 0.12 * len(scenes) + 1.0
    if long_video:
        # one image per ~4 s of narration on long videos
        thin = [s.get("scene_number") for s in scenes
                if len(s.get("image_prompts") or []) < max(2, round(len((s.get("narration") or "").split())
                                                                    / WPS / 4.0))]
        cl.check(S, "enough images for each long-video scene (~1 per 4 s)", not thin,
                 f"add prompts to scenes {thin}" if thin else "")
        cl.check(S, "long video 3–20 min", 180 <= secs <= 1200, f"~{secs / 60:.1f} min")
        ch = script.get("chapter_titles") or []
        sc = [int(c.get("scene", 0)) for c in ch]
        cl.check(S, "4–8 chapter_titles, first at scene 1, in order", 4 <= len(ch) <= 8 and sc[:1] == [1]
                 and sc == sorted(set(sc)) and max(sc) <= len(scenes), f"{len(ch)} chapters, scenes {sc}")
        th = script.get("thumbnail") or {}
        cl.check(S, "thumbnail: scene + 2–5 Hindi words", 1 <= int(th.get("scene", 0)) <= len(scenes)
                 and 2 <= len((th.get("text") or "").split()) <= 5, str(th)[:80])

    images = sum(len(s.get("image_prompts") or []) for s in scenes)
    bad = [i for i in cl.items if not i["ok"]]
    print()
    print(f"{len(scenes)} scenes · {words} words · ~{secs / 60:.1f} min spoken · {images} images "
          f"(~{images * SEC_PER_IMAGE / 3600:.1f} h of image Prepare)")
    print("READY ✅" if not bad else f"NOT READY ☒ — {len(bad)} check(s) failed")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
