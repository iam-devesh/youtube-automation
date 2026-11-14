"""
pipeline/tts_generator.py
Narration: Gemini TTS (expressive, one take for the whole script — see gemini_voice.py),
falling back to Microsoft Edge neural voices per scene. One MP3 per scene + word timings.
"""

import os
import sys
from typing import List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import OUTPUT_DIR, TTS_RATE, TTS_PITCH, TTS_ENGINE

# ─── Configure bundled FFmpeg for pydub (no system ffmpeg needed) ─────────────
try:
    import imageio_ffmpeg as _iio_ffmpeg
    _ffmpeg_bin = _iio_ffmpeg.get_ffmpeg_exe()
    from pydub import AudioSegment
    AudioSegment.converter = _ffmpeg_bin
    AudioSegment.ffprobe   = _ffmpeg_bin   # pydub accepts ffmpeg as ffprobe fallback
    PYDUB_AVAILABLE = True
except Exception:
    PYDUB_AVAILABLE = False


# ─── Pure-Python MP3 duration via mutagen (no ffprobe needed) ─────────────────
try:
    from mutagen.mp3 import MP3 as MutagenMP3
    MUTAGEN_AVAILABLE = True
except ImportError:
    MUTAGEN_AVAILABLE = False


def _get_mp3_duration(mp3_path: str) -> float:
    """Get MP3 duration in seconds — uses mutagen (pure Python, no ffprobe)."""
    if MUTAGEN_AVAILABLE:
        try:
            audio = MutagenMP3(mp3_path)
            return audio.info.length
        except Exception:
            pass

    # Fallback: estimate from file size (128kbps CBR assumption)
    size_bytes = os.path.getsize(mp3_path)
    estimated = size_bytes / (128 * 1024 / 8)   # bytes / (kbps → bytes/s)
    return max(estimated, 1.0)


def _synthesize(text: str, voice: str, rate: str, pitch: str, mp3_path: str) -> list:
    """Synthesize with edge-tts; returns word timings [(start_s, end_s, word), ...]."""
    import asyncio
    import edge_tts

    async def run():
        words = []
        comm = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch, boundary="WordBoundary")
        with open(mp3_path, "wb") as f:
            async for chunk in comm.stream():
                if chunk["type"] == "audio":
                    f.write(chunk["data"])
                elif chunk["type"] == "WordBoundary":
                    st = chunk["offset"] / 1e7
                    words.append((st, st + chunk["duration"] / 1e7, chunk["text"]))
        return words

    words = asyncio.run(run())
    if not os.path.exists(mp3_path) or os.path.getsize(mp3_path) < 500:
        raise RuntimeError("no audio received")
    return words


def generate_tts_for_scene(narration: str, scene_number: int, job_id: str,
                             lang: str = "hi", words_out: list = None) -> Tuple[str, float]:
    """
    Generate cinema-quality TTS audio for a single scene using Edge TTS Neural Voices.

    Args:
        narration: The narration text
        scene_number: Scene index
        job_id: Job ID for file organization
        lang: Language code ("hi" for Hindi, "en" for English)

    Returns:
        (mp3_path, duration_seconds)
    """
    audio_dir = os.path.join(OUTPUT_DIR, job_id, "audio")
    os.makedirs(audio_dir, exist_ok=True)
    mp3_path = os.path.join(audio_dir, f"scene_{scene_number:02d}.mp3")

    # Hindi: 'hi-IN-MadhurNeural' (male, deep), 'hi-IN-SwaraNeural' (female)
    # English: 'en-US-GuyNeural' (deep male), 'en-US-ChristopherNeural' (narrator)
    voice_map = {"hi": "hi-IN-MadhurNeural", "en": "en-US-GuyNeural"}
    voice = voice_map.get(lang, "en-US-GuyNeural")

    # Viral pacing: a little faster than default, slightly deeper. Falls back to defaults.
    words = []
    last_err = None
    for rate, pitch in ((TTS_RATE, TTS_PITCH), ("+0%", "+0Hz")):
        try:
            words = _synthesize(narration, voice, rate, pitch, mp3_path)
            last_err = None
            break
        except Exception as e:
            last_err = e
            print(f"  ⚠️  Edge TTS attempt failed for scene {scene_number} ({rate},{pitch}): {e}")
    if last_err is not None:
        hint = (" — Microsoft rotated the Edge TTS token; run: pip install -U edge-tts"
                if "403" in str(last_err) else "")
        raise RuntimeError(f"Edge TTS failed for scene {scene_number}{hint}: {last_err}")
    if words_out is not None:
        words_out.extend(words)

    # Post-process: loudness-normalize (consistent volume across scenes) + tiny fades.
    # Done with the bundled ffmpeg — pydub needs a system ffprobe, which isn't installed.
    try:
        import subprocess
        import imageio_ffmpeg
        tmp = mp3_path + ".norm.mp3"
        res = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-i", mp3_path, "-af",
                              "highpass=f=70,acompressor=threshold=-20dB:ratio=3:attack=5:release=90:makeup=2,"
                              "equalizer=f=3200:t=q:w=1.2:g=2.5,loudnorm=I=-16:TP=-1.5:LRA=9,afade=t=in:d=0.04",
                              "-ar", "44100", "-b:a", "192k", tmp],
                             capture_output=True, text=True)
        if res.returncode == 0:
            os.replace(tmp, mp3_path)
        else:
            print(f"  ⚠️  Audio normalization skipped: {res.stderr[-200:]}")
    except Exception as e:
        print(f"  ⚠️  Audio normalization skipped: {e}")

    # Use mutagen for duration
    duration = _get_mp3_duration(mp3_path)
    print(f"  🎙️  Edge TTS for scene {scene_number} ({voice}): {duration:.1f}s")
    return mp3_path, duration


def generate_tts_for_script(script: dict, job_id: str, lang: str = "en") -> List[dict]:
    """Narration for every scene + the compulsory script and voice checks (raises if any fail)."""
    from pipeline import qa
    cl = qa.for_job(job_id, fresh=True)          # first stage of every render → new checklist
    qa.check_script(cl, script)
    results = _generate_tts_for_script(script, job_id, lang)
    try:
        qa.check_voice(cl, script, results, job_id)
    except qa.ChecklistFailed as e:
        audio_dir = os.path.join(OUTPUT_DIR, job_id, "audio")
        if not os.path.exists(os.path.join(audio_dir, "narration.wav")) or script.get("voice") == "gemini":
            raise                                     # Edge failed too / Gemini-only episode → stop
        print(f"  ⚠️  Gemini voice failed the checklist ({e}) — re-recording with the Edge voice", flush=True)
        import shutil, time as _t
        shutil.move(audio_dir, audio_dir + f"_rejected_{int(_t.time())}")   # kept, not deleted
        cl.items = [i for i in cl.items if i["stage"] != "2 Voice"]
        results = _generate_tts_for_script(script, job_id, lang, force_edge=True)
        qa.check_voice(cl, script, results, job_id)
    return results


def _gemini_only(script, job_id, max_wait_min=120):
    """Episode set to "voice": "gemini" (same narrator as the rest of the series): never fall
    back to Edge. When a take fails the word check on a FEW scenes, only those scenes are
    re-recorded (a patch take) and spliced in; otherwise a new full take is recorded. When the
    free quota is used up, wait for it to reset and try again (up to max_wait_min)."""
    import time as _t
    from pipeline import gemini_voice as gv
    deadline, takes = _t.time() + max_wait_min * 60, 0
    n_scenes = len(script.get("scenes", []))
    st = {"tried": None, "patches": 0}
    narration = os.path.join(OUTPUT_DIR, job_id, "audio", "narration.wav")

    def patch_or_splice():
        """Saved recordings first (no quota); then a patch take for the scenes still missing."""
        if os.path.exists(narration):
            return
        sig = len(gv._sources(job_id))
        if not sig:
            return
        if sig != st["tried"]:
            st["tried"] = sig
            if gv.splice_takes(script, job_id):
                return
        missing = gv.splice_missing(job_id) or []
        if missing and len(missing) <= max(3, n_scenes // 5) and st["patches"] < 3:
            st["patches"] += 1
            gv.record_patch(script, job_id, missing, script.get("template", "mystery"))
            st["tried"] = len(gv._sources(job_id))
            gv.splice_takes(script, job_id)
        # else: too many scenes missing (or 3 patches used): narrate_script records a full take

    while True:
        try:
            patch_or_splice()
            return gv.narrate_script(script, job_id, script.get("template", "mystery"))
        except Exception as e:
            msg = str(e)
            quota = "quota" in msg.lower() or "429" in msg or "RESOURCE_EXHAUSTED" in msg
            takes += 0 if quota else 1
            if takes >= 3 + st["patches"] or _t.time() > deadline:
                raise RuntimeError(f"Gemini voice required for this episode but not available: {msg[:200]}")
            wait = 600 if quota else 5
            print(f"  ⏳ Gemini voice {'quota used up' if quota else 'take rejected'} ({msg[:120]}) — "
                  f"retrying in {max(wait // 60, 1) if quota else 0} min (Gemini voice only, no Edge fallback)",
                  flush=True)
            _t.sleep(wait)


def _generate_tts_for_script(script: dict, job_id: str, lang: str = "en",
                             force_edge: bool = False) -> List[dict]:
    """
    Generate TTS audio for all scenes in a script.

    Returns:
        List of dicts: [{scene_number, audio_path, duration}, ...]
    """
    if TTS_ENGINE != "edge" and not force_edge and script.get("voice") == "gemini":
        return _gemini_only(script, job_id)
    if TTS_ENGINE != "edge" and not force_edge:
        try:
            from pipeline.gemini_voice import narrate_script
            return narrate_script(script, job_id, script.get("template", "mystery"))
        except Exception as e:
            print(f"  ⚠️  Gemini voice not used ({str(e)[:160]}) — recording the whole video with the "
                  f"Edge Hindi voice instead (exact per-scene timing)", flush=True)
            import shutil
            shutil.rmtree(os.path.join(OUTPUT_DIR, job_id, "audio"), ignore_errors=True)

    results = []
    scenes = script.get("scenes", [])

    for scene in scenes:
        narration = scene.get("narration", "")
        scene_num = scene.get("scene_number", len(results) + 1)

        if not narration.strip():
            narration = "No narration available for this scene."

        words = []
        audio_path, duration = generate_tts_for_scene(narration, scene_num, job_id, lang,
                                                      words_out=words)
        results.append({
            "scene_number": scene_num,
            "audio_path": audio_path,
            "duration": duration,
            "words": words,
            "narration": narration,
        })

    return results


def combine_audio(audio_results: List[dict], job_id: str,
                   gap_ms: int = 300) -> Tuple[str, float]:
    """
    Combine all scene audio files into one full narration track.
    Uses ffmpeg subprocess directly — no ffprobe needed at all.

    Returns:
        (combined_mp3_path, total_duration_seconds)
    """
    import subprocess

    audio_dir = os.path.join(OUTPUT_DIR, job_id, "audio")
    combined_path = os.path.join(audio_dir, "full_narration.mp3")

    # Get the bundled ffmpeg binary
    try:
        import imageio_ffmpeg as _iio
        ffmpeg_bin = _iio.get_ffmpeg_exe()
    except Exception:
        ffmpeg_bin = "ffmpeg"

    # Build ffmpeg concat filter: [in1][sil][in2][sil]...[inN] concat
    # Write a concat list file
    concat_file = os.path.join(audio_dir, "concat_list.txt")
    gap_path = os.path.join(audio_dir, "gap.mp3")

    # Create silence gap file using ffmpeg
    gap_sec = gap_ms / 1000.0
    subprocess.run([
        ffmpeg_bin, "-y", "-f", "lavfi",
        "-i", "anullsrc=r=22050:cl=mono",
        "-t", str(gap_sec),
        "-q:a", "9", "-acodec", "libmp3lame",
        gap_path
    ], capture_output=True)

    # Write concat list
    lines = []
    for i, result in enumerate(audio_results):
        lines.append(f"file '{result['audio_path']}'")
        if i < len(audio_results) - 1:
            lines.append(f"file '{gap_path}'")

    with open(concat_file, "w") as f:
        f.write("\n".join(lines) + "\n")

    # Concatenate with ffmpeg
    subprocess.run([
        ffmpeg_bin, "-y",
        "-f", "concat", "-safe", "0",
        "-i", concat_file,
        "-q:a", "2",
        combined_path,
    ], capture_output=True)

    # Get total duration via mutagen (pure Python)
    total_duration = sum(r["duration"] for r in audio_results)
    total_duration += gap_ms / 1000.0 * (len(audio_results) - 1)

    print(f"  🎵 Combined audio: {total_duration:.1f}s total")
    return combined_path, total_duration



if __name__ == "__main__":
    test_script = {
        "scenes": [
            {"scene_number": 1, "narration": "Welcome to our channel! Today we explore the fall of the Roman Empire."},
            {"scene_number": 2, "narration": "The Roman Empire was one of the greatest civilizations in history, spanning centuries of power and culture."},
        ]
    }
    results = generate_tts_for_script(test_script, "test_job_tts")
    path, dur = combine_audio(results, "test_job_tts")
    print(f"Combined audio: {path} ({dur:.1f}s)")
