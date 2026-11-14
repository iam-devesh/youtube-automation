# The Faceless Studio — production workflow

How an episode goes from script to YouTube, which checks it has to pass, and what to do when
something fails. Every check is enforced in code (`pipeline/qa.py`). Nothing is uploaded
until the whole checklist passes.

## The workflow: prepare first, render later

| Step | What happens | Where | Time per episode | Can it fail? |
|---|---|---|---|---|
| 1. Write | Script JSON in `scripts/<series>/epNN_*.json` | editor | — | — |
| 2. Preflight | Script checks: under 60 s, scene length, unknown English words | first step of Prepare/Render | seconds | Yes, but it's cheap. Fix the script and press Prepare again |
| 3. **Prepare** | Records the Gemini voice, cuts it into scenes **by words**, checks every scene, then makes and checks the images | Series tab, **🎙️ Prepare voice & art** | 5–15 min (images about 8 min) | Retries on its own (see below). Episodes waiting on voice quota just wait |
| 4. Render | Builds the video from the prepared assets: captions, music, grade, loudness, final checks | Series tab, **🎬 Render** | about 10 min | Rarely. The voice and images are already verified |
| 5. Upload | Resumable upload, then added to the playlist | Series tab, **⬆ Upload** | 1–2 min | — |

Prepare a whole series in one go (overnight is fine). Render once the badge on each card reads
**Voice + art ready ✅**. Renders then take minutes and don't fail on the voice.

## Voice: the rules that stop words going missing

- **One Gemini take per episode** (voice Charon), because the free voice quota is a few takes per hour.
- **Cut by words, not by pauses.** Local Whisper transcribes the whole take with word
  timestamps (one pass, about 25 s). Each scene is cut in the quietest point between its last
  word and the next scene's first word. This replaced the pause-guessing that cut
  "…और समंदर पागल हो गया" out of Ep1.
- **Every scene is checked on its own.** It must contain at least 85% of its words, and it
  must **not repeat** the last words of the scene before it or the first words of the scene
  after it (a cut in the wrong place makes a phrase play twice, like "और गिर जाते हैं" in Ep5). Real gaps score 30–70%; 85–100% is the transcriber
  spelling words differently.
- **Rejected takes are kept** in `<job>/_rejected_take_*`. Before recording again, the pipeline
  builds the narration from the best version of each scene across the saved takes. Same
  narrator, no extra quota. Ep5 was made this way.
- **No Edge fallback for series** (`"voice": "gemini"` in each episode JSON). If the voice
  quota runs out, the episode waits and retries every 10 min, for up to 2 hours.

## The checklist (64 checks, 5 stages)

1. **Script:** narration and visual description for every scene, title, description and tags,
   fits a Short, no overlong or very short scene, only known English loanwords.
2. **Voice:** audio for every scene, audible, no dead air over 1.6 s, every word heard, no
   repeated words at scene joins.
3. **Images:** every scene has images; none black, blank or low resolution.
4. **Captions:** captions in every scene and to the end; every spoken part has a caption;
   each scene's first caption starts with the voice.
5. **Final video:** 1080×1920 at 30 fps, at least 3 Mb/s, AAC 48 kHz stereo, length covers
   the narration, voice at least 12 dB above the music, −14 LUFS (±1), no black frames,
   **the finished soundtrack transcribed again (every word audible, nothing repeated)**,
   captions burned into
   the picture.

Results: `<job>/checklist.json`, `render.log`, and the Job Queue, which shows live
progress and the exact check that failed. A failed video is renamed `_rejected.mp4`, so it is
never treated as finished.

## Infrastructure

- **Background service:** `./service.sh install` runs the dashboard under launchd. It starts at
  login, restarts itself if it crashes, and keeps rendering when the editor or terminal closes.
  `./service.sh status | restart | logs | uninstall`. `restart` refuses while a render is
  running.
- **One heavy job at a time:** renders and Prepare jobs share one queue, which is saved to disk
  (`static/outputs/.render.pending.json`) and survives restarts. The local image model and the
  video render must never run together on 16 GB.
- **Local models, no quotas:** images (Z-Image Turbo 4-bit, MLX, ~75 s/image) and checking (Whisper
  large-v3-turbo, MLX). The only cloud dependencies are the Gemini voice and, optionally,
  script writing.
- **Hold switch:** `touch static/outputs/.render.hold` stops new renders from starting (for
  maintenance). Delete the file to resume.

## When something fails

| Job Queue says | Meaning | Do this |
|---|---|---|
| `1 Script: … fits a Short` / `overlong scene` | Script too long | Shorten the scene and Prepare again |
| `English words … not known loanwords` | The voice may misread them | Write the word in Devanagari, or add it to `LOANWORDS` in `pipeline/gemini_voice.py` |
| `⏳ Gemini voice quota used up` | Free voice limit reached | Nothing. It retries on its own |
| `Gemini voice required … not available` | 3 takes and splicing still missing a scene | Check `render.log` for the missing words. Often an unusual word: rewrite it, then Prepare again |
| `3 Visuals: … black/blank` | An image failed | Delete that image in `<job>/clips/` and Prepare again (only that image is redone) |
| `5 Final video: …` | Render problem (rare) | Render again. Voice and images are reused |
