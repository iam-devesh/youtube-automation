"""
server.py — Flask Backend for YouTube Automation Dashboard
Exposes a REST API consumed by the frontend dashboard.
"""

import os
import sys

# ─── Use bundled FFmpeg from imageio-ffmpeg (no brew install needed) ──────────
try:
    import imageio_ffmpeg as _iio_ffmpeg
    _ffmpeg = _iio_ffmpeg.get_ffmpeg_exe()
    os.environ.setdefault("FFMPEG_BINARY",  _ffmpeg)
    os.environ.setdefault("FFPROBE_BINARY", _ffmpeg)   # ffmpeg can sub for ffprobe
    os.environ.setdefault("IMAGEIO_FFMPEG_EXE", _ffmpeg)
    # Configure pydub
    from pydub import AudioSegment as _AS
    _AS.converter = _ffmpeg
    _AS.ffprobe   = _ffmpeg
    # Configure MoviePy
    try:
        import moviepy.config as _mpy_cfg
        _mpy_cfg.FFMPEG_BINARY  = _ffmpeg
        _mpy_cfg.FFPROBE_BINARY = _ffmpeg
    except Exception:
        pass
except Exception:
    pass

import uuid
import urllib.parse
import threading
import time
import json
import re
import traceback
from datetime import datetime

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS


sys.path.insert(0, os.path.dirname(__file__))
from config import FLASK_PORT, FLASK_DEBUG, OUTPUT_DIR, CHANNEL_ID
from pipeline.script_generator import generate_script
from pipeline.image_fetcher import fetch_images_for_script
from pipeline.tts_generator import generate_tts_for_script
from pipeline.video_builder import assemble_video
from pipeline.youtube_uploader import upload_video, get_channel_stats, find_or_create_playlist, add_to_playlist
from pipeline.animated_video_generator import generate_clips_for_script

# ─── App Setup ────────────────────────────────────────────────────────────────

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="/static")
CORS(app)

# In-memory job store  {job_id: {status, progress, steps, result, error}}
JOBS: dict[str, dict] = {}

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_job() -> str:
    job_id = str(uuid.uuid4())[:8]
    JOBS[job_id] = {
        "job_id": job_id,
        "status": "queued",       # queued | running | done | error
        "progress": 0,
        "steps": [],
        "result": None,
        "error": None,
        "created_at": datetime.now().isoformat(),
    }
    return job_id


def _update_job(job_id: str, status: str = None, progress: int = None,
                step_msg: str = None, result: dict = None, error: str = None):
    job = JOBS.get(job_id)
    if not job:
        return
    if status:
        job["status"] = status
    if progress is not None:
        job["progress"] = progress
    if step_msg:
        job["steps"].append({"time": datetime.now().isoformat(), "message": step_msg})
        print(f"  [Job {job_id}] {step_msg}")
    if result:
        job["result"] = result
    if error:
        job["error"] = error
    _checkpoint(job_id)


def _checkpoint(job_id):
    """Pause point: if the job was paused, its worker thread waits here until resumed."""
    job = JOBS.get(job_id)
    if not job or not job.get("pause_requested") or job.get("worker") != threading.get_ident():
        return
    prev = job["status"]
    job["status"] = "paused"
    job["steps"].append({"time": datetime.now().isoformat(), "message": "⏸️ Paused"})
    while job.get("pause_requested"):
        time.sleep(0.5)
    job["status"] = prev if prev != "paused" else "running"
    job["steps"].append({"time": datetime.now().isoformat(), "message": "▶️ Resumed"})


def _bind_worker(job_id):
    """Mark the current thread as this job's worker (so _checkpoint may block it)."""
    if job_id in JOBS:
        JOBS[job_id]["worker"] = threading.get_ident()


# ─── Background Pipeline ──────────────────────────────────────────────────────

def _run_pipeline(job_id: str, topic: str, video_type: str,
                   privacy: str, auto_upload: bool, template: str = "mystery",
                   style: str = "animated_3d"):
    """Full automation pipeline running in a background thread."""
    _bind_worker(job_id)
    try:
        _update_job(job_id, status="running", progress=5,
                    step_msg=f"🚀 Starting pipeline for: '{topic}'")

        # Step 1: Generate Script
        _update_job(job_id, progress=10, step_msg="✍️  Generating AI script...")
        script = generate_script(topic, video_type, style)
        script["template"] = template
        script["style"] = style
        JOBS[job_id]["script"] = script
        _update_job(job_id, progress=25,
                    step_msg=f"📝 Script ready: '{script['title']}' ({len(script['scenes'])} scenes)")

        # Step 2: Generate TTS first — narration length decides how many shots each scene gets
        lang = script.get("lang", "hi")
        _update_job(job_id, progress=30, step_msg="🎙️  Generating voice narration (Edge TTS)...")
        audio_results = generate_tts_for_script(script, job_id, lang=lang)
        _update_job(job_id, progress=45,
                    step_msg=f"🎵 Audio ready: {len(audio_results)} scene tracks")

        # Step 3: Visuals — animated-movie AI art (Veo → AI images) or stock footage
        durations = [a["duration"] for a in audio_results]
        if style == "stock":
            _update_job(job_id, progress=50, step_msg="🖼️  Fetching stock footage (Pexels)...")
            image_paths = fetch_images_for_script(script, job_id, video_type, durations=durations)
        else:
            _update_job(job_id, progress=50, step_msg=f"🎨 Generating {style} artwork...")
            image_paths = generate_clips_for_script(
                script, job_id, video_type, durations=durations, style=style,
                progress_callback=lambda m: _update_job(job_id, step_msg=m))
        _update_job(job_id, progress=70,
                    step_msg=f"🖼️  Footage ready for {len(image_paths)} scenes")

        # Step 4: Assemble Video
        _update_job(job_id, progress=72, step_msg="🎬 Assembling video with MoviePy...")

        def _progress_cb(step, total, msg):
            pct = 72 + int((step / total) * 20)
            _update_job(job_id, progress=pct, step_msg=f"🎬 {msg}")

        video_path = assemble_video(script, image_paths, audio_results, job_id,
                                     progress_callback=_progress_cb)
        _update_job(job_id, progress=92,
                    step_msg=f"✅ Video built: {os.path.basename(video_path)}")

        # Thumbnail = first scene image
        thumbnail_path = None   # media may be video clips; let YouTube pick a frame

        result = {
            "script": script,
            "video_path": video_path,
            "video_filename": os.path.basename(video_path),
            "video_url_local": f"/static/outputs/{job_id}/{os.path.basename(video_path)}",
            "thumbnail_path": thumbnail_path,
            "youtube_url": None,
            "video_id": None,
        }

        # Step 5: Upload to YouTube (optional)
        if auto_upload:
            _update_job(job_id, progress=93, step_msg="📤 Uploading to YouTube...")
            try:
                upload_result = upload_video(video_path, script, thumbnail_path, privacy)
                result["youtube_url"] = upload_result["video_url"]
                result["video_id"] = upload_result["video_id"]
                _update_job(job_id, progress=100,
                            step_msg=f"🎉 Uploaded! {upload_result['video_url']}")
            except FileNotFoundError as e:
                _update_job(job_id, step_msg=f"⚠️  Upload skipped: {e}")
                result["upload_error"] = str(e)
        else:
            _update_job(job_id, progress=100, step_msg="✅ Pipeline complete (upload skipped)")

        _update_job(job_id, status="done", result=result)

    except Exception as e:
        tb = traceback.format_exc()
        _update_job(job_id, status="error", error=str(e),
                    step_msg=f"❌ Error: {e}")
        print(tb)


def _run_animated_pipeline(job_id: str, script: dict, video_type: str,
                            privacy: str, auto_upload: bool):
    """Animated film pipeline — user provides the script, Veo generates clips."""
    _bind_worker(job_id)
    try:
        title = script.get("title", "Untitled Film")
        script["video_type"] = video_type

        _update_job(job_id, status="running", progress=5,
                    step_msg=f"🎬 Starting animated film pipeline: '{title}'")

        # Step 1: Narration first — its length decides how many shots each scene gets
        lang = script.get("lang", "hi")
        _update_job(job_id, progress=10,
                    step_msg="🎙️  Generating voice narration (Edge TTS)...")
        audio_results = generate_tts_for_script(script, job_id, lang=lang)
        _update_job(job_id, progress=25,
                    step_msg=f"🎵 Audio ready: {len(audio_results)} scene tracks")

        # Step 2: Visuals — Veo → AI image → stock footage
        _update_job(job_id, progress=30,
                    step_msg="🎬 Generating visuals (Veo → AI image → stock footage)...")

        def _clip_progress(msg):
            _update_job(job_id, step_msg=msg)

        clip_paths = generate_clips_for_script(
            script, job_id, video_type, progress_callback=_clip_progress,
            durations=[a["duration"] for a in audio_results])
        _update_job(job_id, progress=70,
                    step_msg=f"🎬 Visuals ready for {len(clip_paths)} scenes")

        # Step 3: Assemble Video
        _update_job(job_id, progress=72,
                    step_msg="🎬 Assembling final animated film...")

        def _progress_cb(step, total, msg):
            pct = 72 + int((step / total) * 20)
            _update_job(job_id, progress=pct, step_msg=f"🎬 {msg}")

        video_path = assemble_video(script, clip_paths, audio_results, job_id,
                                     progress_callback=_progress_cb)
        _update_job(job_id, progress=92,
                    step_msg=f"✅ Animated film built: {os.path.basename(video_path)}")

        thumbnail_path = None   # media may be video clips; let YouTube pick a frame

        result = {
            "script": script,
            "video_path": video_path,
            "video_filename": os.path.basename(video_path),
            "video_url_local": f"/static/outputs/{job_id}/{os.path.basename(video_path)}",
            "thumbnail_path": thumbnail_path,
            "youtube_url": None,
            "video_id": None,
        }

        # Step 4: Upload to YouTube (optional)
        if auto_upload:
            _update_job(job_id, progress=93, step_msg="📤 Uploading to YouTube...")
            try:
                upload_result = upload_video(video_path, script, thumbnail_path, privacy)
                result["youtube_url"] = upload_result["video_url"]
                result["video_id"] = upload_result["video_id"]
                _update_job(job_id, progress=100,
                            step_msg=f"🎉 Uploaded! {upload_result['video_url']}")
            except FileNotFoundError as e:
                _update_job(job_id, step_msg=f"⚠️  Upload skipped: {e}")
                result["upload_error"] = str(e)
        else:
            _update_job(job_id, progress=100,
                        step_msg="✅ Animated film complete (upload skipped)")

        _update_job(job_id, status="done", result=result)

    except Exception as e:
        tb = traceback.format_exc()
        _update_job(job_id, status="error", error=str(e),
                    step_msg=f"❌ Error: {e}")
        print(tb)


# ─── API Routes ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_file(os.path.join(STATIC_DIR, "index.html"))


@app.route("/api/generate-script", methods=["POST"])
def api_generate_script():
    """Generate an AI script without running the full pipeline."""
    data = request.get_json()
    topic = data.get("topic", "").strip()
    video_type = data.get("video_type", "long")

    if not topic:
        return jsonify({"error": "Topic is required"}), 400

    try:
        script = generate_script(topic, video_type)
        return jsonify({"success": True, "script": script})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/run-pipeline", methods=["POST"])
def api_run_pipeline():
    """Start the full automation pipeline in the background."""
    data = request.get_json()
    topic = data.get("topic", "").strip()
    video_type = data.get("video_type", "long")
    privacy = data.get("privacy", "public")
    auto_upload = data.get("auto_upload", False)
    template = data.get("template", "mystery")
    style = data.get("style", "animated_3d")

    if not topic:
        return jsonify({"error": "Topic is required"}), 400

    job_id = _make_job()
    thread = threading.Thread(
        target=_run_pipeline,
        args=(job_id, topic, video_type, privacy, auto_upload, template, style),
        daemon=True,
    )
    thread.start()

    return jsonify({"success": True, "job_id": job_id})


@app.route("/api/run-animated-pipeline", methods=["POST"])
def api_run_animated_pipeline():
    """Start the animated film pipeline with a user-provided script."""
    data = request.get_json()
    title = data.get("title", "Untitled Film").strip()
    video_type = data.get("video_type", "short")
    privacy = data.get("privacy", "public")
    auto_upload = data.get("auto_upload", False)
    scenes = data.get("scenes", [])
    description = data.get("description", "")
    tags = data.get("tags", [])
    lang = data.get("lang", "hi")
    template = data.get("template", "mystery")

    if not scenes:
        return jsonify({"error": "At least one scene is required"}), 400

    # Build the script dict from user input
    script = {
        "title": title,
        "description": description,
        "tags": tags,
        "lang": lang,
        "video_type": video_type,
        "template": template,
        "hook_text": data.get("hook_text", ""),
        "style": data.get("style", "animated_3d"),
        "characters": data.get("characters", ""),
        "setting": data.get("setting", ""),
        "topic": data.get("topic", title),
        "scenes": scenes,
    }

    job_id = _make_job()
    JOBS[job_id]["script"] = script
    thread = threading.Thread(
        target=_run_animated_pipeline,
        args=(job_id, script, video_type, privacy, auto_upload),
        daemon=True,
    )
    thread.start()

    return jsonify({"success": True, "job_id": job_id})


@app.route("/api/jobs", methods=["GET"])
def api_list_jobs():
    """Return all jobs (most recent first)."""
    rank = {"running": 0, "paused": 0, "queued": 1}
    jobs_list = sorted(list(JOBS.values()) + _series_jobs(),
                       key=lambda j: (rank.get(j["status"], 2), "" if j["status"] in rank else j["created_at"]),
                       reverse=False)
    # running first, then queued (in episode order), then the rest newest-first
    jobs_list = [{k: v for k, v in j.items() if k not in ("worker",)} for j in jobs_list]
    active = [j for j in jobs_list if j["status"] in rank]
    rest = sorted([j for j in jobs_list if j["status"] not in rank], key=lambda j: j["created_at"], reverse=True)
    active.sort(key=lambda j: (rank[j["status"]], j.get("queue_pos", 0), j["job_id"]))
    k = 0
    start_at = _not_before()
    for j in active:
        if j["status"] == "queued":
            k += 1
            when = (f"starts at {datetime.fromtimestamp(start_at).strftime('%H:%M')} (waiting for the Gemini voice quota)"
                    if start_at > time.time() and k == 1 else "starts automatically")
            j["steps"] = [{"time": "", "message": f"⏳ #{k} in the render queue — {when}"}]
    return jsonify({"jobs": active + rest})


@app.route("/api/jobs/<job_id>", methods=["GET"])
def api_get_job(job_id):
    """Return status of a specific job."""
    job = JOBS.get(job_id)
    if not job and job_id.startswith("series__"):
        job = next((j for j in _series_jobs() if j["job_id"] == job_id), None)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({k: v for k, v in job.items() if k not in ("worker",)})


@app.route("/api/jobs/<job_id>/<action>", methods=["POST"])
def api_pause_resume(job_id, action):
    """Pause or resume a job. Series renders are frozen at once (SIGSTOP on the render's
    process group); pipeline jobs and uploads pause at their next checkpoint."""
    import signal as _sig
    if action not in ("pause", "resume"):
        return jsonify({"error": "Unknown action"}), 404
    if job_id.startswith("series__"):
        pgid = _render_pgid()
        if not pgid:
            return jsonify({"error": "No series render is running"}), 400
        if action == "pause":
            os.killpg(pgid, _sig.SIGSTOP)
            open(PAUSE_FLAG, "w").write(str(pgid))
        else:
            os.killpg(pgid, _sig.SIGCONT)
            if os.path.exists(PAUSE_FLAG):
                os.remove(PAUSE_FLAG)
        return jsonify({"success": True, "status": "paused" if action == "pause" else "running"})
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if action == "pause":
        if job["status"] not in ("running", "queued"):
            return jsonify({"error": f"Can't pause a job that is {job['status']}"}), 400
        job["pause_requested"] = True
        job["steps"].append({"time": datetime.now().isoformat(),
                             "message": "⏸️ Pausing at the next checkpoint…"})
    else:
        job["pause_requested"] = False
    return jsonify({"success": True})


@app.route("/api/jobs/<job_id>/upload", methods=["POST"])
def api_upload_job(job_id):
    """Manually trigger YouTube upload for a completed job."""
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if job["status"] != "done":
        return jsonify({"error": "Job is not completed yet"}), 400

    result = job.get("result", {})
    video_path = result.get("video_path")
    script = result.get("script", {})

    if not video_path or not os.path.exists(video_path):
        return jsonify({"error": "Video file not found"}), 404

    data = request.get_json() or {}
    privacy = data.get("privacy", "public")
    def after(res):
        job["result"]["youtube_url"] = res["video_url"]
        job["result"]["video_id"] = res["video_id"]
        return []

    up_id = _start_upload(script.get("title", job_id), video_path, script, privacy, on_done=after)
    return jsonify({"success": True, "job_id": up_id})


# ─── Background uploads with progress ─────────────────────────────────────────

def _start_upload(label, video_path, meta, privacy, series=None, on_done=None):
    """Upload in a thread; progress shows up as a job in the Job Queue. Returns the job id."""
    job_id = _make_job()
    job = JOBS[job_id]
    job.update(status="running", kind="upload", video_type=meta.get("video_type", "short"),
               script={"title": f"⬆️ {label}"}, series=series)
    _update_job(job_id, step_msg=f"⬆️ Uploading to YouTube as {privacy.upper()}…")
    t0 = time.time()
    marks = set()

    def progress(pct, sent, total):
        _checkpoint(job_id)
        job["progress"] = pct
        el = max(time.time() - t0, 0.001)
        speed = sent / el
        eta = (total - sent) / speed if speed > 0 else 0
        job["upload"] = {"sent_mb": round(sent / 1e6, 1), "total_mb": round(total / 1e6, 1),
                         "speed_mbps": round(speed / 1e6, 2), "eta_s": int(eta)}
        bucket = pct // 25 * 25
        if 0 < bucket < 100 and bucket not in marks:
            marks.add(bucket)
            _update_job(job_id, step_msg=f"⬆️ {bucket}% · {sent / 1e6:.1f}/{total / 1e6:.1f} MB · {speed / 1e6:.1f} MB/s")

    def work():
        _bind_worker(job_id)
        try:
            res = upload_video(video_path, meta, None, privacy, progress_callback=progress)
            _update_job(job_id, step_msg=f"✅ Uploaded: {res['video_url']}")
            if on_done:
                for msg in on_done(res) or []:
                    _update_job(job_id, step_msg=msg)
            job["result"] = {"youtube_url": res["video_url"], "video_id": res["video_id"],
                             "playlist_url": res.get("playlist_url")}
            _update_job(job_id, status="done", progress=100)
        except Exception as e:
            _update_job(job_id, status="error", error=str(e), step_msg=f"❌ Upload failed: {e}")

    threading.Thread(target=work, daemon=True).start()
    return job_id


def _active_upload(series_id, episode_id):
    for j in JOBS.values():
        se = j.get("series") or {}
        if j.get("kind") == "upload" and j["status"] == "running" and \
                se.get("series_id") == series_id and se.get("episode_id") == episode_id:
            return j
    return None


@app.route("/api/channel-stats", methods=["GET"])
def api_channel_stats():
    """Fetch YouTube channel statistics."""
    stats = get_channel_stats(CHANNEL_ID)
    return jsonify(stats)


@app.route("/api/download/<job_id>", methods=["GET"])
def api_download_video(job_id):
    """Download the generated video."""
    job = JOBS.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "Job not done"}), 404

    video_path = job.get("result", {}).get("video_path")
    if not video_path or not os.path.exists(video_path):
        return jsonify({"error": "Video not found"}), 404

    return send_file(video_path, as_attachment=True,
                     download_name=os.path.basename(video_path))


# ─── Series ───────────────────────────────────────────────────────────────────
# A series lives in scripts/<series_id>/ (series.json + epNN_<slug>.json). Episodes render
# one at a time (render_series.py) — parallel renders exhaust RAM on a 16 GB Mac.

import glob as _glob
import render_series as _rs

SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "scripts")
SERIES_STATE = {"running": False, "current": None, "queue": [], "error": None, "failed": {}}
PENDING_FILE = os.path.join(OUTPUT_DIR, ".render.pending.json")


def _pending():
    try:
        return [p for p in json.load(open(PENDING_FILE)) if os.path.exists(p)]
    except Exception:
        return []


def _set_pending(paths):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    json.dump(paths, open(PENDING_FILE, "w"))


NOT_BEFORE_FILE = os.path.join(OUTPUT_DIR, ".render.not_before")


def _not_before():
    try:
        return float(open(NOT_BEFORE_FILE).read().strip())
    except Exception:
        return 0.0


def _dispatcher():
    """Starts queued renders as soon as nothing else is rendering (one at a time), but not before
    a scheduled start time if one is set (e.g. waiting for the voice quota to reset)."""
    while True:
        time.sleep(10)
        try:
            pend = _pending()
            if pend and time.time() < _not_before():
                continue
            if pend and not SERIES_STATE["running"] and not _rs.lock_holder():
                _set_pending([])
                threading.Thread(target=_series_render_worker, args=(pend,), daemon=True).start()
        except Exception as e:
            print(f"⚠️ render dispatcher: {e}")


def _episode_info(series_id, path):
    stem = os.path.splitext(os.path.basename(path))[0]
    job = f"{series_id}_{stem}"
    out_dir = os.path.join(OUTPUT_DIR, job)
    sc = json.load(open(path, encoding="utf-8"))
    vids = [f for f in _glob.glob(os.path.join(out_dir, "*.mp4")) if not os.path.basename(f).startswith("_")]
    prepared = os.path.exists(os.path.join(out_dir, "assets_ready.json"))
    meta_path = os.path.join(out_dir, "upload_meta.json")
    meta = json.load(open(meta_path, encoding="utf-8")) if os.path.exists(meta_path) else sc
    up_path = os.path.join(out_dir, "upload_result.json")
    uploaded = json.load(open(up_path)) if os.path.exists(up_path) else None
    up = _active_upload(series_id, stem)
    if up:
        status = "uploading"
    elif vids and not os.path.exists(os.path.join(out_dir, "_raw.mp4")):
        status = "rendered"
    elif uploaded and uploaded.get("video_id"):
        status = "uploaded"                         # on YouTube; local video removed to save space
    elif os.path.basename(SERIES_STATE["current"] or "") == os.path.basename(path) or \
            os.path.basename((_rs.lock_holder() or {}).get("episode") or "") == os.path.basename(path):
        status = "rendering"
    elif os.path.basename(path) in {os.path.basename(q.split("::")[-1]) for q in SERIES_STATE["queue"] + _pending()}:
        status = "queued"
    else:
        status = "ready"
    return {
        "id": stem, "episode": sc.get("episode"), "title": meta.get("title"), "hook": sc.get("hook_text"),
        "scenes": len(sc.get("scenes", [])), "status": status, "prepared": prepared,
        "video_url": (f"/static/outputs/{job}/{urllib.parse.quote(os.path.basename(vids[0]))}"
                      if vids and status in ("rendered", "uploading") else None),
        "job": job, "out_dir": out_dir, "path": path, "n_scenes": len(sc.get("scenes", [])),
        "description": meta.get("description", ""), "tags": meta.get("tags", []),
        "youtube_url": (uploaded or {}).get("video_url"),
        "upload_pct": up["progress"] if up else None,
    }


def _series_render_worker(paths):
    """Render episodes one at a time, each in its own process group (so Pause can freeze it,
    including its ffmpeg children)."""
    import subprocess as _sp
    SERIES_STATE.update(running=True, queue=list(paths), error=None)
    try:
        for item in paths:
            prep = item.startswith("prepare::")
            p = item.split("::", 1)[1] if prep else item
            SERIES_STATE["current"] = p
            SERIES_STATE["queue"] = [q for q in SERIES_STATE["queue"] if q != item]
            job_dir = os.path.join(OUTPUT_DIR, f"{os.path.basename(os.path.dirname(p))}_"
                                               f"{os.path.splitext(os.path.basename(p))[0]}")
            os.makedirs(job_dir, exist_ok=True)
            log = open(os.path.join(job_dir, "render.log"), "a", encoding="utf-8")
            env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
            proc = _sp.Popen([sys.executable, "-W", "ignore", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "render_series.py"), os.path.dirname(p), os.path.splitext(os.path.basename(p))[0]]
                             + (["--prepare"] if prep else []),
                             cwd=os.path.dirname(os.path.abspath(__file__)), start_new_session=True,
                             stdout=log, stderr=_sp.STDOUT, env=env)
            SERIES_STATE["proc"] = proc
            log.close()
            if proc.wait() != 0:
                tail = ""
                try:
                    lines = open(os.path.join(job_dir, "render.log"), encoding="utf-8").read().splitlines()
                    tail = next((l for l in reversed(lines) if "❌" in l or "Error" in l or "failed" in l), lines[-1])
                except Exception:
                    pass
                SERIES_STATE["error"] = f"{os.path.basename(p)}: {tail[:300]}"
                SERIES_STATE["failed"][os.path.basename(p)] = tail[:300] or f"exit code {proc.returncode}"
            else:
                SERIES_STATE["failed"].pop(os.path.basename(p), None)
    finally:
        SERIES_STATE.update(running=False, current=None, queue=[], proc=None)


PAUSE_FLAG = os.path.join(OUTPUT_DIR, ".render.paused")


def _render_pgid():
    holder = _rs.lock_holder()
    if not holder:
        return None
    try:
        return os.getpgid(int(holder["pid"]))
    except Exception:
        return None


def _series_paused():
    return os.path.exists(PAUSE_FLAG) and _rs.lock_holder() is not None


def _infer_progress(out_dir, n_scenes):
    """Stage + percent of an episode render, read from the files each stage leaves behind
    (works for renders started from the CLI too)."""
    j = lambda *a: os.path.join(out_dir, *a)
    audio = os.path.exists(j("audio", "narration.wav")) or os.path.exists(j("audio", "scene_01.mp3"))
    imgs = len(_glob.glob(j("clips", "*.jpg")) + _glob.glob(j("clips", "*.png")))
    expected = max(2 * n_scenes, 1)
    raw, ass = os.path.exists(j("_raw.mp4")), os.path.exists(j("captions.ass"))
    stages = [("🎙️ Narration", audio), (f"🎨 AI images {min(imgs, expected)}/{expected}", imgs >= expected or raw or ass),
              ("🎞️ Building scenes (parallax, captions)", raw or ass), ("🎬 Rendering video", ass),
              ("🎚️ Colour grade + music + loudness", False)]
    if not audio:
        pct, now = 3, "🎙️ Generating narration…"
    elif imgs < expected and not (raw or ass):
        pct, now = 10 + int(55 * imgs / expected), f"🎨 Generating AI images {imgs}/{expected}…"
    elif not raw and not ass:
        pct, now = 68, "🎞️ Building scenes…"
    elif not ass:
        pct, now = 76, "🎬 Rendering video…"
    else:
        pct, now = 90, "🎚️ Colour grade, captions, music & loudness…"
    steps = [{"time": "", "message": ("✅ " if done else "⏳ ") + name} for name, done in stages]
    live, passed = _log_status(out_dir)
    if live:
        now = live                                   # e.g. "🧩 Checking take 2/3 …", "☑ [2 Voice] …"
    steps += [{"time": "", "message": f"📋 {p}"} for p in passed]
    return pct, now, steps


_LIVE = re.compile(r"^\s*(🧩|✂️|⏳|🎙️|☑|☒|✅ \d|🎨|🎬 Visuals|🎚️|\[\d+/\d+\])")


def _log_status(out_dir):
    """Latest progress line of the CURRENT run in render.log + checklist stages passed so far."""
    try:
        with open(os.path.join(out_dir, "render.log"), encoding="utf-8", errors="ignore") as f:
            lines = f.read().splitlines()
    except OSError:
        return None, []
    start = max((i for i, l in enumerate(lines) if re.match(r"^🎬 \w+: ", l)), default=0)   # episode header
    run = [l.strip() for l in lines[start:]]
    live = next((l for l in reversed(run) if _LIVE.match(l)), None)
    passed = [l[2:].replace(": all checks passed", " ✔") for l in run if re.match(r"^✅ \d ", l)]
    return (live[:140] if live else None), passed


def _series_snapshot():
    out = []
    for meta in sorted(_glob.glob(os.path.join(SCRIPTS_DIR, "*", "series.json"))):
        sid = os.path.basename(os.path.dirname(meta))
        info = json.load(open(meta, encoding="utf-8"))
        eps = sorted(_glob.glob(os.path.join(os.path.dirname(meta), "ep*.json")))
        out.append({"id": sid, "name": info.get("series"), "name_hindi": info.get("series_hindi"),
                    "playlist_url": (f"https://www.youtube.com/playlist?list={info['playlist_id']}"
                                     if info.get("playlist_id") else None),
                    "hashtag": info.get("hashtag"), "episodes": [_episode_info(sid, p) for p in eps]})
    holder = _rs.lock_holder()
    busy_with = os.path.basename((holder or {}).get("episode") or SERIES_STATE["current"] or "")
    cli_batch = bool(holder) and not SERIES_STATE["running"]     # a CLI run renders every remaining ep
    if holder:
        # a batch run moves on to the next episode; follow it if the locked one is finished
        for se in out:
            eps = se["episodes"]
            idx = next((i for i, e in enumerate(eps) if e["id"] + ".json" == busy_with), None)
            if idx is not None and eps[idx]["status"] == "rendered":
                # the batch only moves FORWARD through its episode list
                nxt = next((e for e in eps[idx + 1:] if e["status"] in ("ready", "queued")), None)
                if nxt:
                    nxt["status"] = "rendering"
                    busy_with = nxt["id"] + ".json"
    if cli_batch:
        for se in out:
            ids = [e["id"] + ".json" for e in se["episodes"]]
            if busy_with in ids:
                for e in se["episodes"][ids.index(busy_with) + 1:]:
                    if e["status"] == "ready":
                        e["status"] = "queued"
    return out, bool(SERIES_STATE["running"] or holder), busy_with


def _public(ep):
    return {k: v for k, v in ep.items() if k not in ("out_dir", "path", "n_scenes", "job")}


@app.route("/api/series", methods=["GET"])
def api_series():
    out, busy, busy_with = _series_snapshot()
    for se in out:
        se["episodes"] = [_public(e) for e in se["episodes"]]
    return jsonify({"series": out, "busy": busy, "busy_with": busy_with, "paused": _series_paused(),
                    "error": SERIES_STATE["error"]})


def _series_jobs():
    """Series episodes as Job Queue entries (rendering / queued / rendered)."""
    jobs = []
    out, _, _ = _series_snapshot()
    for se in out:
        for e in se["episodes"]:
            fail = SERIES_STATE["failed"].get(e["id"] + ".json")
            if e["status"] == "ready" and fail:
                jobs.append({
                    "job_id": f"series__{se['id']}__{e['id']}", "status": "error", "progress": 0,
                    "steps": [{"time": "", "message": f"❌ {fail}"},
                              {"time": "", "message": f"Full log: static/outputs/{e['job']}/render.log"}],
                    "error": fail, "created_at": datetime.now().isoformat(), "video_type": "short",
                    "script": {"title": f"📺 {se.get('name_hindi') or se['name']} · EP {e['episode']} — {e['title']}"},
                    "series": {"series_id": se["id"], "episode_id": e["id"], "video_url": None,
                               "youtube_url": None, "title": e["title"]}, "result": None})
                continue
            if e["status"] == "ready":
                continue
            od = e["out_dir"]
            if e["status"] in ("rendered", "uploading", "uploaded"):
                vids = [f for f in _glob.glob(os.path.join(od, "*.mp4")) if not os.path.basename(f).startswith("_")]
                stamp = vids[0] if vids else os.path.join(od, "upload_result.json")
                created = datetime.fromtimestamp(os.path.getmtime(stamp)).isoformat()
                pct, status = 100, "done"
                steps = [{"time": "", "message": "✅ On YouTube" if e["status"] == "uploaded" else "✅ Rendered"}]
            elif e["status"] == "rendering":
                pct, now, steps = _infer_progress(od, e["n_scenes"])
                steps.append({"time": "", "message": "⏸️ Paused — click Resume to continue" if _series_paused()
                              else f"→ {now}"})
                created = datetime.fromtimestamp(os.path.getctime(od)).isoformat() if os.path.isdir(od) \
                    else datetime.now().isoformat()
                status = "paused" if _series_paused() else "running"
            else:
                pct, steps, status = 0, [{"time": "", "message": "⏳ In the render queue — starts automatically "
                                                              "when the current episode finishes"}], "queued"
                created = datetime.now().isoformat()
            pend = [os.path.basename(q.split("::")[-1]) for q in _pending()]
            qpos = (100 + pend.index(e["id"] + ".json")) if e["id"] + ".json" in pend else e["episode"] or 0
            jobs.append({
                "queue_pos": qpos,
                "job_id": f"series__{se['id']}__{e['id']}", "status": status, "progress": pct,
                "steps": steps, "error": None, "created_at": created, "video_type": "short",
                "script": {"title": f"📺 {se.get('name_hindi') or se['name']} · EP {e['episode']} — {e['title']}"},
                "series": {"series_id": se["id"], "episode_id": e["id"], "video_url": e["video_url"],
                           "youtube_url": e["youtube_url"], "title": e["title"]},
                "result": None,
            })
    return jobs


@app.route("/api/series/<series_id>/prepare", methods=["POST"])
def api_series_prepare(series_id):
    """Queue voice + image production (with the full checklist) for every episode that has
    neither a video nor prepared assets. Rendering those episodes later is quick and can't fail
    on voice or images."""
    folder = os.path.join(SCRIPTS_DIR, os.path.basename(series_id))
    if not os.path.isdir(folder):
        return jsonify({"error": "Series not found"}), 404
    paths = [os.path.abspath(p) for p in sorted(_glob.glob(os.path.join(folder, "ep*.json")))]
    paths = [p for p in paths if (lambda e: e["status"] == "ready" and not e["prepared"])(
        _episode_info(os.path.basename(folder), p))]
    if not paths:
        return jsonify({"error": "Nothing to prepare — every episode is prepared, queued or rendered"}), 400
    items = [f"prepare::{p}" for p in paths]
    pend = _pending()
    _set_pending(pend + [i for i in items if i not in pend])
    return jsonify({"success": True, "waiting": True, "queued": [os.path.basename(p) for p in paths]})


@app.route("/api/series/<series_id>/render", methods=["POST"])
def api_series_render(series_id):
    folder = os.path.join(SCRIPTS_DIR, os.path.basename(series_id))
    if not os.path.isdir(folder):
        return jsonify({"error": "Series not found"}), 404
    only = (request.get_json(silent=True) or {}).get("episode", "")
    paths = sorted(os.path.abspath(p) for p in _glob.glob(os.path.join(folder, "ep*.json"))
                   if only in os.path.basename(p))
    # only episodes that aren't rendered, rendering or already queued
    paths = [p for p in paths if _episode_info(os.path.basename(folder), p)["status"] == "ready"]
    if not paths:
        return jsonify({"error": "Nothing to render — already rendered or queued"}), 400
    if SERIES_STATE["running"] or _rs.lock_holder() or time.time() < _not_before():
        pend = _pending()
        _set_pending(pend + [p for p in paths if p not in pend])     # starts when the current one ends
        return jsonify({"success": True, "waiting": True, "queued": [os.path.basename(p) for p in paths]})
    threading.Thread(target=_series_render_worker, args=(paths,), daemon=True).start()
    return jsonify({"success": True, "queued": [os.path.basename(p) for p in paths]})


@app.route("/api/series/<series_id>/<episode_id>/upload", methods=["POST"])
def api_series_upload(series_id, episode_id):
    sid, eid = os.path.basename(series_id), os.path.basename(episode_id)
    path = os.path.join(SCRIPTS_DIR, sid, f"{eid}.json")
    if not os.path.exists(path):
        return jsonify({"error": "Episode not found"}), 404
    out_dir = os.path.join(OUTPUT_DIR, f"{sid}_{eid}")
    vids = [f for f in _glob.glob(os.path.join(out_dir, "*.mp4")) if not os.path.basename(f).startswith("_")]
    if not vids:
        return jsonify({"error": "Render the episode first"}), 400
    meta_path = os.path.join(out_dir, "upload_meta.json")
    meta = json.load(open(meta_path if os.path.exists(meta_path) else path, encoding="utf-8"))
    privacy = (request.get_json(silent=True) or {}).get("privacy", "private")
    if _active_upload(sid, eid):
        return jsonify({"error": "This episode is already uploading"}), 409

    def after(res):
        json.dump(res, open(os.path.join(out_dir, "upload_result.json"), "w"))
        try:    # series playlist (created on first upload) — never fails the upload itself
            res["playlist_id"] = _series_playlist(sid)
            add_to_playlist(res["playlist_id"], res["video_id"], meta.get("episode"))
            res["playlist_url"] = f"https://www.youtube.com/playlist?list={res['playlist_id']}"
            json.dump(res, open(os.path.join(out_dir, "upload_result.json"), "w"))
            return [f"📃 Added to playlist: {res['playlist_url']}"]
        except Exception as e:
            return [f"⚠️ Playlist step failed: {e}"]

    job_id = _start_upload(f"EP {meta.get('episode', '')} — {meta.get('title', eid)}", vids[0], meta, privacy,
                           series={"series_id": sid, "episode_id": eid, "title": meta.get("title")},
                           on_done=after)
    return jsonify({"success": True, "job_id": job_id})


def _series_playlist(series_id):
    """Playlist for a series: id cached in scripts/<series>/series.json."""
    meta_path = os.path.join(SCRIPTS_DIR, series_id, "series.json")
    info = json.load(open(meta_path, encoding="utf-8"))
    if info.get("playlist_id"):
        return info["playlist_id"]
    title = f"{info.get('series_hindi', '')} | {info.get('series', series_id)}".strip(" |")
    desc = info.get("playlist_description") or (
        f"{info.get('series_hindi', '')} — The Faceless ki series. "
        f"{info.get('tagline', '')}\n\n{info.get('hashtag', '')} #TheFaceless")
    info["playlist_id"] = find_or_create_playlist(title, desc, "public")
    json.dump(info, open(meta_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return info["playlist_id"]


# ─── Run ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    print("\n🚀 The Faceless Studio")
    print(f"   Open: http://localhost:{FLASK_PORT}\n")
    # No auto-reloader: a reload restarts the process and kills running video jobs
    # (they live in memory). Restart the server manually after editing code.
    threading.Thread(target=_dispatcher, daemon=True).start()
    app.run(host="0.0.0.0", port=FLASK_PORT, debug=FLASK_DEBUG, use_reloader=False)
