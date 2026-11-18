"""
qa.py — the render checklist. Every stage is checked before the next (expensive) one starts;
the first failed check stops the episode, so a broken video is never finished, listed or
uploaded. Everything already generated (narration take, images) is kept, so a retry only
redoes what failed.

    Stage 1  Script   — every scene has narration + visual, title/description/tags present
    Stage 2  Voice    — every scene has audio, not silent, no long dead air, every word heard
    Stage 3  Visuals  — every scene has images, none black/blank, full resolution
    Stage 4  Captions — caption file covers the whole narration
    Stage 5  Final    — captions burned in, 1080x1920 30 fps, bitrate, voice above music,
                        -14 LUFS, no black frames, duration matches the narration

Results go to <job>/checklist.json and render.log.
"""
import json
import os
import re
import subprocess

import numpy as np

from config import OUTPUT_DIR

try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG = "ffmpeg"

ISOLATED_MIN = 0.70        # a scene transcribed ALONE (no context) misspells more; a wrong
                           # cut drops whole phrases (≤55%) or repeats words — both still fail
MIN_RECALL = 0.85          # share of script words that must be heard in each scene (missing
                           # narration shows up as 30-70%; 85-100% is transcriber spelling noise)
MAX_DEAD_AIR = 1.6         # seconds of silence allowed inside a scene
MIN_VIDEO_KBPS = 3000
MIN_MARGIN_DB = 12.0       # voice above music while speaking
LUFS_TARGET, LUFS_TOL = -14.0, 1.0


class ChecklistFailed(RuntimeError):
    pass


_JOBS = {}


def for_job(job_id, fresh=False):
    """The checklist of a job (shared by every stage). fresh=True starts a new one."""
    if fresh or job_id not in _JOBS:
        _JOBS[job_id] = Checklist(job_id)
    return _JOBS[job_id]


class Checklist:
    def __init__(self, job_id):
        self.path = os.path.join(OUTPUT_DIR, job_id, "checklist.json")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.items = []

    def check(self, stage, name, ok, detail=""):
        self.items.append({"stage": stage, "check": name, "ok": bool(ok), "detail": str(detail)})
        print(f"  {'☑' if ok else '☒'} [{stage}] {name}" + (f" — {detail}" if detail else ""), flush=True)
        self._save()
        return ok

    def gate(self, stage):
        """Stop the render if any check of this stage failed."""
        bad = [i for i in self.items if i["stage"] == stage and not i["ok"]]
        if bad:
            self._save(failed_stage=stage)
            raise ChecklistFailed(f"checklist failed at '{stage}': " +
                                  "; ".join(f"{i['check']} ({i['detail']})" for i in bad))
        print(f"  ✅ {stage}: all checks passed", flush=True)

    def _save(self, failed_stage=None):
        json.dump({"failed_stage": failed_stage, "items": self.items},
                  open(self.path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


# ─── helpers ──────────────────────────────────────────────────────────────────
def _pcm(path, sr=24000):
    r = subprocess.run([FFMPEG, "-v", "error", "-i", path, "-f", "s16le", "-ac", "1", "-ar", str(sr), "-"],
                       capture_output=True)
    return np.frombuffer(r.stdout, np.int16).astype(np.float32) / 32768.0


def _longest_silence(x, sr=24000, frame=0.02):
    n = int(sr * frame)
    if len(x) < n:
        return 0.0
    rms = np.sqrt((x[: len(x) // n * n].reshape(-1, n) ** 2).mean(axis=1))
    thr = max(np.percentile(rms, 90) * 0.06, 1e-4)
    quiet = rms < thr
    best = run = 0
    for q in quiet[3:-3]:                         # ignore the natural lead-in / tail
        run = run + 1 if q else 0
        best = max(best, run)
    return best * frame


def _probe(path):
    r = subprocess.run([FFMPEG, "-i", path], capture_output=True, text=True).stderr
    d = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r)
    v = re.search(r"Video: (\w+).*?, (\d+)x(\d+).*?(\d+) kb/s.*?([\d.]+) fps", r)
    a = re.search(r"Audio: (\w+).*?(\d+) Hz, (\w+)", r)
    return {"duration": int(d[1]) * 3600 + int(d[2]) * 60 + float(d[3]) if d else 0,
            "vcodec": v and v[1], "w": v and int(v[2]), "h": v and int(v[3]),
            "kbps": v and int(v[4]), "fps": v and float(v[5]),
            "acodec": a and a[1], "ahz": a and int(a[2]), "ach": a and a[3]}


def _frame(path, t, w=270, h=480, vf=None):
    """One frame, downscaled, as an HxWx3 float array."""
    path = os.path.abspath(path)
    chain = (vf + "," if vf else "") + f"scale={w}:{h}"
    r = subprocess.run([FFMPEG, "-v", "error", "-ss", f"{t:.3f}", "-i", path, "-frames:v", "1", "-vf", chain,
                        "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                       capture_output=True, cwd=os.path.dirname(os.path.abspath(path)))
    if len(r.stdout) < w * h * 3:
        return None
    return np.frombuffer(r.stdout[: w * h * 3], np.uint8).reshape(h, w, 3).astype(np.float32)


def _lufs(path):
    r = subprocess.run([FFMPEG, "-nostats", "-i", path, "-af", "ebur128", "-f", "null", "-"],
                       capture_output=True, text=True)
    m = re.findall(r"I:\s+(-?[\d.]+) LUFS", r.stderr)
    return float(m[-1]) if m else None


# ─── word matching (tolerant of spelling variants, strict about missing words) ─
_LATIN = None


def _latin_map():
    """English loanwords → their Devanagari spelling (DNA ↔ डीएनए, school ↔ स्कूल …)."""
    global _LATIN
    if _LATIN is None:
        from pipeline.gemini_voice import LOANWORDS
        _LATIN = {v.lower(): k for k, v in LOANWORDS.items() if v.isascii()}
        _LATIN.update({"dna": "डीएनए", "km": "किलोमीटर"})
    return _LATIN


def _canon(w):
    import unicodedata
    w = unicodedata.normalize("NFC", w.lower())
    w = _latin_map().get(w, w)
    w = w.replace("ज्ञ", "ग्य")                      # ज्ञ is spoken "gya" (वैज्ञानिक ↔ वैग्यानिक)
    # sound-alike spellings a transcriber mixes up (झील/जील, के/की, सैनिक/सैनेक …)
    if w in ("के", "की", "का"):
        return "का"
    if w in ("है", "हैं", "हे", "हें"):
        return "है"
    for a_, b_ in (("झ", "ज"), ("ध", "द"), ("भ", "ब"), ("घ", "ग"), ("थ", "त"), ("फ", "प"), ("ख", "क"),
                   ("छ", "च"), ("ठ", "ट"), ("ढ", "ड"), ("ण", "न"), ("श", "स"), ("ष", "स"),
                   ("ई", "इ"), ("ऊ", "उ"), ("औ", "ओ"), ("ै", "े"), ("ौ", "ो"), ("ो", "ु"), ("ू", "ु"),
                   ("े", "ि")):
        w = w.replace(a_, b_)
    if len(w) > 2 and w[-1] in "ओउ":                # गाँव ↔ गाओं ↔ गाउं
        w = w[:-1] + "व"
    if len(w) > 2 and w.endswith("यि"):            # आये ↔ आए
        w = w[:-2] + "ए"
    for ch in "\u093c\u0902\u0901\u094d":            # nukta, anusvara, chandrabindu, halant
        w = w.replace(ch, "")
    return w.replace("ी", "ि").replace("ू", "ु")


def heard_words(ref, hyp):
    """(share of script words heard, [missing words]). A word counts as heard if it appears in
    order in the transcript, allowing spelling variants and split/joined compounds."""
    import difflib
    from pipeline.gemini_voice import _norm_tokens
    r = _norm_tokens(ref)
    if not r:
        return 1.0, []
    hyp_toks = [_canon(t) for t in _norm_tokens(hyp)]
    joined = "".join(hyp_toks)
    pos, missing = 0, []
    for w in r:
        c = _canon(w)
        i = joined.find(c, max(pos - 4, 0))
        if i >= 0 and i - pos < 40:
            pos = i + len(c)
            continue
        best = max((difflib.SequenceMatcher(None, c, t).ratio() for t in hyp_toks), default=0)
        if best >= (0.6 if len(c) <= 3 else 0.75):      # गिर ↔ किर
            continue
        missing.append(w)
    return 1 - len(missing) / len(r), missing


def verify_words(texts, clips, tries=3, min_recall=None):
    """Transcribe each clip, re-checking failures once (the transcriber itself is noisy).
    Returns [(score, missing words)] per clip."""
    # Each clip is transcribed ALONE: in one batched request the transcriber fills gaps with words
    # from the neighbouring clip (and sometimes stops at the first pause).
    from pipeline.gemini_voice import _transcribe
    need = MIN_RECALL if min_recall is None else min_recall
    res = [(0.0, ["(not checked)"])] * len(clips)
    for _ in range(tries):
        for i in [i for i, (sc, _) in enumerate(res) if sc < need]:
            try:
                hyp = _transcribe([clips[i]])[0]
                again = _score(texts[i], hyp)
                rep = _repeats(texts, i, hyp)
                if rep:
                    again = (0.0, again[1] + [rep])
            except Exception:
                continue
            if again[0] > res[i][0] or (rep and res[i][0] == 0.0):
                res[i] = again
    return res


def _repeats(texts, i, hyp):
    """Words at the START of clip i that belong to the end of line i-1, or words at its END
    that belong to the start of line i+1 (a cut in the wrong place → the phrase is heard twice)."""
    from pipeline.gemini_voice import _norm_tokens
    import difflib
    h = [_canon(t) for t in _norm_tokens(hyp)]
    own = [_canon(t) for t in _norm_tokens(texts[i])]
    def same(a, b):
        return a == b or difflib.SequenceMatcher(None, a, b).ratio() >= 0.6
    def run(words, neighbour):            # how many leading `words` match the neighbour's edge
        n = 0
        for w, v in zip(words, neighbour):
            if not same(w, v):
                break
            n += 1
        return n
    out = []
    if i > 0:
        prev = [_canon(t) for t in _norm_tokens(texts[i - 1])]
        for k in range(min(5, len(h)), 1, -1):          # clip starts with the last k words of i-1
            if len(prev) >= k and run(h[:k], prev[-k:]) == k and run(h[:k], own[:k]) < k:
                out.append(f"repeats end of scene {i}: {' '.join(_norm_tokens(hyp)[:k])}")
                break
    if i + 1 < len(texts):
        nxt = [_canon(t) for t in _norm_tokens(texts[i + 1])]
        for k in range(min(5, len(h)), 1, -1):          # clip ends with the first k words of i+1
            if len(nxt) >= k and run(h[-k:], nxt[:k]) == k and run(h[-k:], own[-k:]) < k:
                out.append(f"has the start of scene {i + 2}: {' '.join(_norm_tokens(hyp)[-k:])}")
                break
    return "; ".join(out)


def _score(ref, hyp):
    """Words of the line that were heard — and no big chunk of words from another line."""
    latin = sum(c.isascii() and c.isalpha() for c in hyp)
    deva = sum("\u0900" <= c <= "\u097f" for c in hyp)
    if latin > deva:                                  # transcriber answered in English letters → retry
        return 0.0, ["(transcript came back in Latin script — not usable)"]
    rec, missing = heard_words(ref, hyp)
    prec, extra = heard_words(hyp, ref)               # transcript words that are in this line
    if len(extra) >= 3 and prec < 0.75:
        missing = missing + [f"(+ extra words: {' '.join(extra)})"]
        rec = min(rec, prec)
    return rec, missing


# ─── Stage 1: script ──────────────────────────────────────────────────────────
def check_script(cl, script):
    S = "1 Script"
    scenes = script.get("scenes", [])
    cl.check(S, "has scenes", len(scenes) >= 3, f"{len(scenes)} scenes")
    empty = [i + 1 for i, s in enumerate(scenes) if not (s.get("narration") or "").strip()]
    cl.check(S, "every scene has narration", not empty, f"empty: {empty}" if empty else "")
    novis = [i + 1 for i, s in enumerate(scenes) if not (s.get("visual_description") or "").strip()]
    cl.check(S, "every scene has a visual description", not novis, f"missing: {novis}" if novis else "")
    for k in ("title", "description", "tags"):
        cl.check(S, f"{k} present", bool(script.get(k)))
    # preflight for the voice + Shorts format (measured narrator pace ≈ 2.3 words/s)
    words = [len((sc.get("narration") or "").split()) for sc in scenes]
    est = sum(words) / 2.3 + 0.12 * len(scenes) + 1.0
    if script.get("video_type", "short") == "short":
        cl.check(S, "fits a Short (under 60 s)", est <= 58, f"~{est:.0f}s estimated from {sum(words)} words")
    long_ = [i + 1 for i, n in enumerate(words) if n > 24]
    cl.check(S, "no overlong scene (≤24 words)", not long_, f"too long: scenes {long_}" if long_ else "")
    short_ = [i + 1 for i, n in enumerate(words) if 0 < n < 3]
    cl.check(S, "no scene under 3 words", not short_, f"too short: scenes {short_}" if short_ else "")
    import re as _re
    odd = sorted({w for sc in scenes for w in _re.findall(r"[A-Za-z]{2,}", sc.get("narration") or "")
                  if w.lower() not in _latin_map()})
    cl.check(S, "English words in narration are known loanwords", not odd,
             f"write these in Devanagari or add to LOANWORDS: {', '.join(odd)}" if odd else "")
    cl.gate(S)


# ─── Stage 2: voice ───────────────────────────────────────────────────────────
def check_voice(cl, script, audio, job_id):
    S = "2 Voice"
    scenes = script.get("scenes", [])
    cl.check(S, "audio for every scene", len(audio) == len(scenes), f"{len(audio)}/{len(scenes)}")
    engine = "Gemini" if os.path.exists(os.path.join(OUTPUT_DIR, job_id, "audio", "narration.wav")) else "Edge"
    cl.check(S, "voice engine", True, engine)
    clips = []
    for i, a in enumerate(audio):
        sn = a.get("scene_number", i + 1)
        p = a.get("audio_path")
        if not (p and os.path.exists(p)):
            cl.check(S, f"scene {sn} audio file", False, "missing")
            clips.append(None)
            continue
        x = _pcm(p)
        clips.append(x)
        rms = float(np.sqrt((x ** 2).mean())) if len(x) else 0.0
        cl.check(S, f"scene {sn} audible", rms > 0.01 and len(x) / 24000 >= 1.0,
                 f"{len(x) / 24000:.1f}s, level {20 * np.log10(rms + 1e-9):.0f} dB")
        gap = _longest_silence(x)
        cl.check(S, f"scene {sn} no dead air", gap <= MAX_DEAD_AIR, f"longest pause {gap:.1f}s")
    # every word spoken: transcribe each scene and compare with the script, word by word
    texts = [(a.get("narration") or "") for a in audio]
    try:
        valid = [i for i, c in enumerate(clips) if c is not None]
        thr = ISOLATED_MIN if engine == "Gemini" else MIN_RECALL     # Gemini: whole-take check already ≥85%
        for i, (sc, miss) in zip(valid, verify_words([texts[i] for i in valid], [clips[i] for i in valid],
                                                       min_recall=thr)):
            cl.check(S, f"scene {audio[i].get('scene_number', i + 1)} every word heard", sc >= thr,
                     f"{sc * 100:.0f}% of words" + (f", missing: {' '.join(miss)}" if miss else ""))
    except Exception as e:
        # transcription service down: fall back to the voice engine's own word timings
        print(f"  ⚠️  Transcription unavailable ({str(e)[:100]}) — checking word timings instead", flush=True)
        for i, a in enumerate(audio):
            n_ref = len(texts[i].split())
            n_got = len(a.get("words") or [])
            ok = engine == "Gemini" or (n_ref and n_got / n_ref >= MIN_RECALL)
            cl.check(S, f"scene {a.get('scene_number', i + 1)} every word voiced", ok,
                     "verified at recording" if engine == "Gemini" else f"{n_got}/{n_ref} words")
    cl.gate(S)                                   # never deletes anything — only stops


# ─── Stage 3: visuals ─────────────────────────────────────────────────────────
def check_visuals(cl, script, media):
    S = "3 Visuals"
    scenes = script.get("scenes", [])
    cl.check(S, "visuals for every scene", len(media) == len(scenes), f"{len(media)}/{len(scenes)}")
    from PIL import Image
    for i, m in enumerate(media):
        paths = m if isinstance(m, list) else [m]
        bad = []
        for p in paths:
            if not (p and os.path.exists(p)):
                bad.append(f"{os.path.basename(str(p))} missing")
                continue
            if p.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                try:
                    im = Image.open(p).convert("L")
                    w, h = im.size
                    a = np.asarray(im.resize((64, 112)), np.float32)
                    if min(w, h) < 576:
                        bad.append(f"{os.path.basename(p)} low-res {w}x{h}")
                    elif a.mean() < 12 or a.std() < 6:
                        bad.append(f"{os.path.basename(p)} black/blank")
                except Exception as e:
                    bad.append(f"{os.path.basename(p)} unreadable ({e})")
        cl.check(S, f"scene {i + 1} images OK", not bad and paths, "; ".join(bad) or f"{len(paths)} image(s)")
    cl.gate(S)


# ─── Stage 4: captions ────────────────────────────────────────────────────────
def check_captions(cl, ass_path, audio, total, starts=None, onset_tol=0.45):
    S = "4 Captions"
    ok = os.path.exists(ass_path)
    cl.check(S, "caption file written", ok)
    if ok:
        txt = open(ass_path, encoding="utf-8").read()
        cl.check(S, "Hindi caption font set", "Kohinoor Devanagari" in txt or "Devanagari" in txt)
        events = re.findall(r"^Dialogue: \d+,([\d:.]+),([\d:.]+),Caption,", txt, re.M)
        to_s = lambda t: sum(float(p) * m for p, m in zip(t.split(":"), (3600, 60, 1)))
        spans = [(to_s(a), to_s(b)) for a, b in events]
        cl.check(S, "captions exist", len(spans) > 0, f"{len(spans)} caption events")
        shown = set()
        for a, b in spans:
            shown.update(range(int(a * 4), int(b * 4) + 1))
        # every scene must have captions during its narration
        missing = []
        st_list = starts or np.cumsum([0] + [a["duration"] for a in audio[:-1]]).tolist()
        for i, (au, t) in enumerate(zip(audio, st_list)):
            mid = int((t + au["duration"] / 2) * 4)
            if not any(k in shown for k in range(mid - 4, mid + 5)):
                missing.append(i + 1)
        cl.check(S, "captions in every scene", not missing, f"no captions in scenes {missing}" if missing else "")
        last = max((b for _, b in spans), default=0)
        cl.check(S, "captions run to the end", last >= sum(a["duration"] for a in audio) * 0.85,
                 f"last caption ends {last:.1f}s")
        if starts:
            check_sync(cl, S, spans, audio, starts)
    cl.gate(S)
    return spans if ok else []


def check_sync(cl, S, spans, audio, starts, step=0.05):
    """Captions vs. voice: captions must be on screen while the words are spoken, and the
    first caption of each scene must start with the voice (not early / late)."""
    total = max(max((b for _, b in spans), default=0), starts[-1] + audio[-1]["duration"]) + 1
    n = int(total / step) + 1
    speech = np.zeros(n, bool)
    onsets = []
    for a, st in zip(audio, starts):
        x = _pcm(a["audio_path"])
        k = int(24000 * step)
        if len(x) < k:
            onsets.append(None)
            continue
        rms = np.sqrt((x[: len(x) // k * k].reshape(-1, k) ** 2).mean(axis=1))
        voiced = rms > max(np.percentile(rms, 90) * 0.1, 3e-3)
        i0 = int(st / step)
        seg = speech[i0: i0 + len(voiced)]
        seg |= voiced[: len(seg)]
        idx = np.flatnonzero(voiced)
        onsets.append(st + idx[0] * step if len(idx) else None)
    shown = np.zeros(n, bool)
    for a, b in spans:
        shown[int(a / step): int(b / step) + 1] = True
    covered = (speech & shown).sum() / max(speech.sum(), 1)
    cl.check(S, "every spoken part has a caption", covered >= 0.85, f"{covered * 100:.0f}% of speech captioned")
    on_voice = (shown & speech).sum() / max(shown.sum(), 1)
    cl.check(S, "captions shown while voice is speaking", on_voice >= 0.6,
             f"{on_voice * 100:.0f}% of caption time has voice")
    off = []
    for i, (st, on) in enumerate(zip(starts, onsets)):
        end = st + audio[i]["duration"]
        first = min((a for a, b in spans if st - 0.5 <= a < end), default=None)
        if on is not None and first is not None and abs(first - on) > onset_tol:
            off.append(f"scene {i + 1}: {first - on:+.2f}s")
    cl.check(S, "first caption of each scene starts with the voice", not off, "; ".join(off))


# ─── Stage 5: final video ─────────────────────────────────────────────────────
def check_final(cl, out_path, raw_path, look, ass_path, spans, audio, margin, w, h, starts=None,
                burn_checked_per_chapter=False):
    S = "5 Final video"
    ok = os.path.exists(out_path) and os.path.getsize(out_path) > 500_000
    cl.check(S, "video file written", ok)
    if not ok:
        cl.gate(S)
    p = _probe(out_path)
    cl.check(S, f"resolution {w}x{h}", (p["w"], p["h"]) == (w, h), f"{p['w']}x{p['h']}")
    cl.check(S, "30 fps h264", p["vcodec"] == "h264" and p["fps"] and abs(p["fps"] - 30) < 0.5,
             f"{p['vcodec']} {p['fps']} fps")
    cl.check(S, "video quality (bitrate)", (p["kbps"] or 0) >= MIN_VIDEO_KBPS, f"{p['kbps']} kb/s")
    cl.check(S, "audio stream AAC 48 kHz stereo",
             p["acodec"] == "aac" and p["ahz"] == 48000 and p["ach"] == "stereo",
             f"{p['acodec']} {p['ahz']} {p['ach']}")
    voice_total = sum(a["duration"] for a in audio)
    cl.check(S, "duration covers the narration", p["duration"] >= voice_total - 0.5,
             f"video {p['duration']:.1f}s, voice {voice_total:.1f}s")
    cl.check(S, "voice clearly above music", margin is None or margin >= MIN_MARGIN_DB,
             "no music" if margin is None else f"{margin:.1f} dB")
    lufs = _lufs(out_path)
    cl.check(S, "loudness -14 LUFS", lufs is not None and abs(lufs - LUFS_TARGET) <= LUFS_TOL,
             f"{lufs} LUFS")
    # no black frames: sample every ~4 s
    dark = []
    for t in np.arange(0.5, max(p["duration"] - 0.5, 1), 4.0):
        f = _frame(out_path, t)
        if f is None or f.mean() < 10:
            dark.append(round(float(t), 1))
    cl.check(S, "no black frames", not dark, f"dark at {dark}s" if dark else "")
    # every word audible in the FINISHED video (after cutting, mixing with music, mastering)
    if starts:
        try:
            x = _pcm(out_path)
            segs = [x[int(st * 24000): int((st + a["duration"]) * 24000)] for a, st in zip(audio, starts)]
            bad = []
            for i, (sc, miss) in enumerate(verify_words([a.get("narration") or "" for a in audio], segs,
                                                        min_recall=ISOLATED_MIN)):
                if sc < ISOLATED_MIN:
                    bad.append(f"scene {i + 1}: {sc * 100:.0f}% (missing: {' '.join(miss)})")
            cl.check(S, "every word audible in the final video", not bad,
                     "; ".join(bad) or f"all {len(audio)} scenes heard, nothing repeated")
        except Exception as e:
            cl.check(S, "every word audible in the final video", False,
                     f"could not verify (transcription failed: {str(e)[:80]})")
    # captions really burned in: same frame with vs. without the caption layer must differ in
    # the caption band far more than elsewhere (the grade alone changes the frame uniformly)
    if burn_checked_per_chapter:
        cl.check(S, "captions", True, "checked in every chapter")
    else:
        burned = []
        picks = [s for s in spans if s[1] - s[0] >= 0.4][:: max(len(spans) // 4, 1)][:4]
        for a, b in picks:
            t = (a + b) / 2
            fin = _frame(out_path, t)
            ref = _frame(raw_path, t, vf=look or None) if raw_path and os.path.exists(raw_path) else None
            if fin is None or ref is None:
                continue
            d = np.abs(fin - ref).mean(axis=(1, 2))           # per-row difference
            burned.append(float(d.max() / (np.median(d) + 1e-3)))
        cl.check(S, "captions burned into the picture",
                 bool(burned) and sum(r > 4 for r in burned) >= max(1, len(burned) - 1),
                 f"caption contrast {', '.join(f'{r:.0f}x' for r in burned)}" if burned else "could not compare")
    cl.gate(S)


# ─── Chapter check (long videos) ──────────────────────────────────────────────
def check_chapter(cl, k, out_path, raw_path, look, spans, audio, starts, w, h, captions_burned=True):
    """One chapter of a long video, before it is accepted and saved: picture, no black frames,
    every word audible + nothing repeated in each scene, captions burned in."""
    S = f"5 Chapter {k}"
    ok = os.path.exists(out_path) and os.path.getsize(out_path) > 100_000
    cl.check(S, "chapter file written", ok)
    if not ok:
        cl.gate(S)
    p = _probe(out_path)
    cl.check(S, f"resolution {w}x{h}, 30 fps", (p["w"], p["h"]) == (w, h) and p["fps"] and abs(p["fps"] - 30) < 0.5,
             f"{p['w']}x{p['h']} {p['fps']} fps")
    cl.check(S, "video quality (bitrate)", (p["kbps"] or 0) >= MIN_VIDEO_KBPS, f"{p['kbps']} kb/s")
    dark = []
    for t in np.arange(0.5, max(p["duration"] - 0.5, 1), 4.0):
        f = _frame(out_path, t)
        if f is None or f.mean() < 10:
            dark.append(round(float(t), 1))
    cl.check(S, "no black frames", not dark, f"dark at {dark}s" if dark else "")
    x = _pcm(out_path)
    segs = [x[int(st * 24000): int((st + a["duration"]) * 24000)] for a, st in zip(audio, starts)]
    bad = [f"scene {i + 1}: {sc * 100:.0f}% ({' '.join(m)})"
           for i, (sc, m) in enumerate(verify_words([a.get("narration") or "" for a in audio], segs,
                                                    min_recall=ISOLATED_MIN)) if sc < ISOLATED_MIN]
    cl.check(S, "every word audible, nothing repeated", not bad, "; ".join(bad))
    if not captions_burned:                       # long video: captions go to YouTube as a track
        cl.check(S, "captions uploaded as a YouTube caption track (not burned in)", True)
        cl.gate(S)
        return
    burned = []
    for a_, b_ in [s_ for s_ in spans if s_[1] - s_[0] >= 0.4][:: max(len(spans) // 3, 1)][:3]:
        fin = _frame(out_path, (a_ + b_) / 2)
        ref = _frame(raw_path, (a_ + b_) / 2, vf=look or None)
        if fin is not None and ref is not None:
            d = np.abs(fin - ref).mean(axis=(1, 2))
            burned.append(float(d.max() / (np.median(d) + 1e-3)))
    cl.check(S, "captions burned into the picture",
             bool(burned) and sum(r > 4 for r in burned) >= max(1, len(burned) - 1),
             ", ".join(f"{r:.0f}x" for r in burned) if burned else "could not compare")
    cl.gate(S)
