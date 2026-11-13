"""
remix_audio.py — redo an episode's audio mix (voice + sfx + ducked music) WITHOUT re-rendering
the video. Use it after changing music or mixing settings.

    python3 remix_audio.py scripts/bhootiya_bharat/ep01_dhanushkodi.json
    python3 remix_audio.py scripts/bhootiya_bharat            # every rendered episode

The voice track is rebuilt sample-accurately from audio/narration.wav using the same scene
cuts and timeline as the original render (scene audio + template tail), so captions stay in sync.
"""
import glob
import json
import os
import subprocess
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import OUTPUT_DIR
from pipeline import templates as T
from pipeline import music
from pipeline import video_builder as V
from pipeline.gemini_voice import narrate_script

SR = 48000


def _load(path):
    r = subprocess.run([V.FFMPEG_BIN, "-v", "error", "-i", path, "-f", "s16le", "-ac", "1", "-ar", str(SR), "-"],
                       capture_output=True)
    return np.frombuffer(r.stdout, np.int16).astype(np.float32) / 32768.0


def _resample(x, src=T.SR):
    if src == SR:
        return x
    n = int(len(x) * SR / src)
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


def remix(script_path):
    script = json.load(open(script_path, encoding="utf-8"))
    folder = os.path.basename(os.path.dirname(os.path.abspath(script_path)))
    job = f"{folder}_{os.path.splitext(os.path.basename(script_path))[0]}"
    out_dir = os.path.join(OUTPUT_DIR, job)
    vids = [f for f in glob.glob(os.path.join(out_dir, "*.mp4")) if not os.path.basename(f).startswith("_")]
    if not vids or os.path.exists(os.path.join(out_dir, "_raw.mp4")):
        print(f"⏭️  {job}: not rendered yet")
        return
    video = vids[0]
    tpl = T.get_template(script.get("template", "mystery"))
    tname = (script.get("template") or "mystery").lower()

    # exact scene durations from the same deterministic split as the render
    audio = narrate_script(script, job, tname, log=lambda *_: None)
    narration = _load(os.path.join(out_dir, "audio", "narration.wav"))
    r = subprocess.run([V.FFMPEG_BIN, "-i", video], capture_output=True, text=True).stderr
    hh, mm, ss = r.split("Duration: ")[1].split(",")[0].split(":")
    total = int(hh) * 3600 + int(mm) * 60 + float(ss)

    track = np.zeros(int(total * SR) + SR, np.float32)
    src_pos, t, starts = 0.0, 0.0, []
    for a in audio:
        seg = narration[int(src_pos * SR): int((src_pos + a["duration"]) * SR)]
        i = int(t * SR)
        track[i: i + len(seg)] += seg[: max(len(track) - i, 0)]
        starts.append(t)
        src_pos += a["duration"]
        t += a["duration"] + tpl["tail"] - tpl["overlap"]

    def place(x, at, gain):
        x = _resample(x) * gain
        i = max(int(at * SR), 0)
        track[i: i + len(x)] += x[: max(len(track) - i, 0)]

    place(T.impact(), 0, 0.30)                                  # same sfx as the render
    sfx = T.whoosh() if tpl["sfx"] == "whoosh" else T.pop()
    for i, st in enumerate(starts):
        if i:
            place(sfx, st - (0.28 if tpl["sfx"] == "whoosh" else 0.0), 0.12 if tpl["sfx"] == "whoosh" else 0.18)
    track = track[: int(total * SR)]

    voice_wav = os.path.join(out_dir, "_voice.wav")
    mixed = os.path.join(out_dir, "_mix.wav")
    pcm = (np.clip(track, -1, 1) * 32767).astype(np.int16)
    subprocess.run([V.FFMPEG_BIN, "-y", "-v", "error", "-f", "s16le", "-ar", str(SR), "-ac", "1", "-i", "-",
                    "-ac", "2", voice_wav], input=pcm.tobytes(), check=True)
    track_path, credit = music.track_for(tname, key=script.get("title", ""), log=lambda *_: None)
    margin = V.mix_audio_track(voice_wav, track_path, total, mixed, log=lambda *_: None) if track_path else None
    tmp = video + ".remix.mp4"
    subprocess.run([V.FFMPEG_BIN, "-y", "-v", "error", "-i", video, "-i", mixed if track_path else voice_wav,
                    "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
                    "-shortest", "-movflags", "+faststart", tmp], check=True)
    os.replace(tmp, video)
    lufs = V.master_loudness(video)
    for f in (voice_wav, mixed):
        if os.path.exists(f):
            os.remove(f)
    print(f"✅ {job}: remixed — voice {margin:.1f} dB above music, {lufs:.1f} LUFS")


if __name__ == "__main__":
    target = sys.argv[1]
    paths = sorted(glob.glob(os.path.join(target, "ep*.json"))) if os.path.isdir(target) else [target]
    for p in paths:
        remix(p)
