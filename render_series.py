"""
render_series.py — render every episode of a series, one at a time (parallel renders
would exhaust RAM on a 16 GB Mac). Resumable: finished episodes are skipped, and images /
narration already generated for an episode are reused.

    python3 render_series.py scripts/bhootiya_bharat            # all episodes
    python3 render_series.py scripts/bhootiya_bharat ep03       # only matching episodes
"""
import glob
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import OUTPUT_DIR
from pipeline.tts_generator import generate_tts_for_script
from pipeline.animated_video_generator import generate_clips_for_script
from pipeline.video_builder import assemble_video
from pipeline import qa


LOCK = os.path.join(OUTPUT_DIR, ".render.lock")


def lock_holder():
    """{"pid", "episode"} of a live render process, else None (stale locks are ignored)."""
    try:
        info = json.load(open(LOCK))
        os.kill(int(info["pid"]), 0)
        return info
    except Exception:
        return None


def _set_lock(episode):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    json.dump({"pid": os.getpid(), "episode": episode}, open(LOCK, "w"))


def _clear_lock():
    try:
        if json.load(open(LOCK)).get("pid") == os.getpid():
            os.remove(LOCK)
    except Exception:
        pass


HOLD = os.path.join(OUTPUT_DIR, ".render.hold")


def render(path, prepare=False):
    if os.path.exists(HOLD):                   # maintenance switch: don't start new renders
        raise RuntimeError("render queue is on hold (.render.hold) — not started")
    _set_lock(path)
    try:
        return _render(path, prepare)
    finally:
        _clear_lock()


def _render(path, prepare=False):
    script = json.load(open(path, encoding="utf-8"))
    job = f"{os.path.basename(os.path.dirname(os.path.abspath(path)))}_{os.path.splitext(os.path.basename(path))[0]}"
    out_dir = os.path.join(OUTPUT_DIR, job)
    done = [f for f in glob.glob(os.path.join(out_dir, "*.mp4")) if not os.path.basename(f).startswith("_")]
    if done and not os.path.exists(os.path.join(out_dir, "_raw.mp4")):
        print(f"⏭️  {job}: already rendered → {done[0]}")
        return done[0]
    if os.path.exists(os.path.join(out_dir, "upload_result.json")) and not script.get("rerender"):
        print(f"⏭️  {job}: already on YouTube — not re-rendering (set \"rerender\": true to force)")
        return None
    if script.get("reuse_job"):                       # seed with assets from an earlier job
        src = os.path.join(OUTPUT_DIR, script["reuse_job"])
        for sub in ("clips", "audio"):
            if os.path.isdir(os.path.join(src, sub)) and not os.path.isdir(os.path.join(out_dir, sub)):
                shutil.copytree(os.path.join(src, sub), os.path.join(out_dir, sub))
    t0 = time.time()
    print(f"\n🎬 {job}: {script['title']}")
    # every stage runs its compulsory checklist (pipeline/qa.py) and stops the render on a failure
    audio = generate_tts_for_script(script, job, lang=script.get("lang", "hi"))
    media = generate_clips_for_script(script, job, script.get("video_type", "short"),
                                      durations=[a["duration"] for a in audio])
    if prepare:                                  # assets made + checked; the video is rendered later
        cl = qa.for_job(job)
        json.dump({"time": time.strftime("%Y-%m-%d %H:%M"), "voice": next(
            (i["detail"] for i in cl.items if i["check"] == "voice engine"), ""),
            "checks_passed": len(cl.items)}, open(os.path.join(out_dir, "assets_ready.json"), "w"))
        print(f"📦 {job}: voice + images ready and checked ({len(cl.items)} checks) in "
              f"{(time.time() - t0) / 60:.1f} min — render whenever you like", flush=True)
        return None
    video = assemble_video(script, media, audio, job)
    print(f"📋 Checklist: all {len(qa.for_job(job).items)} checks passed", flush=True)
    json.dump(script, open(os.path.join(out_dir, "upload_meta.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)            # final title/description/tags incl. music credit
    print(f"✅ {job} done in {(time.time() - t0) / 60:.1f} min → {video}")
    return video


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    prepare = "--prepare" in sys.argv
    folder = args[0]
    only = args[1] if len(args) > 1 else ""
    eps = sorted(p for p in glob.glob(os.path.join(folder, "ep*.json")) if only in os.path.basename(p))
    failed = 0
    for p in eps:
        try:
            render(p, prepare)
        except Exception as e:
            import traceback
            failed += 1
            print(f"❌ {p}: {e}\n{traceback.format_exc()}", flush=True)
    sys.exit(1 if failed else 0)
