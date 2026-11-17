"""
pipeline/gemini_voice.py
Expressive narration with Gemini TTS (free tier works) — far more natural than Edge TTS.

The whole script is spoken in ONE request (quota-friendly, and the voice keeps one
consistent tone), then split back into per-scene clips at the natural pauses.
Gemini returns no word timings, so captions are aligned from the audio itself: speech
regions are detected from loudness and the words are laid onto the speech time in order.
"""

import json
import os
import re
import subprocess
import time
import sys
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import GEMINI_API_KEY, GEMINI_TTS_MODEL, GEMINI_TTS_VOICE, OUTPUT_DIR

try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG = "ffmpeg"

SR = 24000
FRAME = 0.02   # 20 ms analysis frames

# How the narrator should perform, per video template
DIRECTION = {
    "mystery": ("You are the narrator of a gripping Hindi mystery documentary on YouTube Shorts. "
                "Speak in a deep, low, suspenseful voice with real emotion, at a brisk pace, "
                "with short dramatic pauses at '...' and a clear pause between paragraphs. "
                "Leave a clear pause of about one second between paragraphs (each paragraph is one scene). "
                "Say EVERY word clearly — never skip or swallow a word. English words are written in "
                "English: pronounce them naturally, the way an Indian narrator says them."),
    "story":   ("You are a warm, captivating Hindi storyteller on YouTube Shorts. Speak with "
                "emotion and wonder, at a lively pace, pausing briefly between paragraphs."),
    "facts":   ("You are an energetic Hindi facts presenter on YouTube Shorts. Speak with "
                "excitement and punch, at a fast pace, pausing briefly between paragraphs."),
}

# Voice chain: rumble cut, gentle compression, presence lift, loudness for mixing
VOICE_FX = ("highpass=f=70,acompressor=threshold=-20dB:ratio=3:attack=5:release=90:makeup=2,"
            "equalizer=f=3200:t=q:w=1.2:g=2.5,loudnorm=I=-16:TP=-1.5:LRA=9")


def _client(timeout_s):
    """Gemini client with a hard request timeout (a stuck request must never hang a render)."""
    from google import genai
    from google.genai import types as t
    return genai.Client(api_key=GEMINI_API_KEY, http_options=t.HttpOptions(timeout=int(timeout_s * 1000)))


def _helper(contents, timeout_s=45):
    """Run a helper request on the first responsive model (lite first — the big ones queue up)."""
    from config import GEMINI_HELPER_MODELS
    last = None
    for m in GEMINI_HELPER_MODELS:
        try:
            client = _client(timeout_s)          # keep a reference: a discarded client closes itself
            return client.models.generate_content(model=m.strip(), contents=contents)
        except Exception as e:
            last = e
    raise last


def _ff(args):
    r = subprocess.run([FFMPEG, "-y", "-v", "error"] + args, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-300:])


_MODEL = [None]       # per-script override: script["voice_model"] (each Gemini TTS model has its own free quota)


def _speak(text_block: str, template: str, wav_path: str):
    from google.genai import types as t
    client = _client(180)
    prompt = (f"{DIRECTION.get(template, DIRECTION['mystery'])}\n"
              f"Read the following Hindi narration exactly as written, nothing added:\n\n{text_block}")
    r = client.models.generate_content(
        model=_MODEL[0] or GEMINI_TTS_MODEL, contents=prompt,
        config=t.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=t.SpeechConfig(voice_config=t.VoiceConfig(
                prebuilt_voice_config=t.PrebuiltVoiceConfig(voice_name=GEMINI_TTS_VOICE)))))
    data = r.candidates[0].content.parts[0].inline_data.data
    with wave.open(wav_path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(data)


def _speak_retry(text, template, wav_path, log=print):
    """_speak with patience for the free tier's rate limits (429)."""
    for attempt in range(5):
        try:
            return _speak(text, template, wav_path)
        except Exception as e:
            msg = str(e)
            if ("429" in msg or "RESOURCE_EXHAUSTED" in msg or "503" in msg) and attempt < 4:
                m = re.search(r"retry in ([\d.]+)s|retryDelay.{0,5}?(\d+)s", msg)
                wait = float(next(g for g in m.groups() if g)) + 2 if m else 20 * (attempt + 1)
                if wait > 120:
                    raise RuntimeError(f"Gemini TTS quota exhausted (resets in ~{wait / 60:.0f} min)")
                log(f"  ⏳ Gemini TTS rate limit — waiting {wait:.0f}s")
                time.sleep(min(wait, 90))
                continue
            raise


def _pcm(path):
    r = subprocess.run([FFMPEG, "-v", "error", "-i", path, "-f", "s16le", "-ac", "1", "-ar", str(SR), "-"],
                       capture_output=True)
    return np.frombuffer(r.stdout, np.int16).astype(np.float32) / 32768.0


def _speech_regions(x):
    """[(start, end)] seconds of speech, from frame loudness with an adaptive threshold."""
    n = int(SR * FRAME)
    frames = len(x) // n
    if frames == 0:
        return []
    rms = np.sqrt((x[: frames * n].reshape(frames, n) ** 2).mean(1) + 1e-12)
    db = 20 * np.log10(rms)
    thr = max(np.percentile(db, 95) - 30, -50)
    voiced = db > thr
    regions, start = [], None
    for i, v in enumerate(voiced):
        if v and start is None:
            start = i
        elif not v and start is not None:
            regions.append([start, i])
            start = None
    if start is not None:
        regions.append([start, frames])
    merged = []                                    # bridge gaps shorter than 120 ms
    for r in regions:
        if merged and (r[0] - merged[-1][1]) * FRAME < 0.12:
            merged[-1][1] = r[1]
        else:
            merged.append(r)
    return [(a * FRAME, b * FRAME) for a, b in merged if (b - a) * FRAME > 0.05]


def _align(words, regions):
    """Lay words (by character weight) onto speech time, skipping the pauses."""
    total_speech = sum(b - a for a, b in regions) or 1.0
    weights = [len(w) + 1 for w in words]
    tot_w = float(sum(weights)) or 1.0

    def to_real(s):                               # speech-time → wall-clock time
        for a, b in regions:
            if s <= b - a:
                return a + s
            s -= b - a
        return regions[-1][1] if regions else 0.0

    out, acc = [], 0.0
    for w, wt in zip(words, weights):
        s0 = acc / tot_w * total_speech
        acc += wt
        s1 = acc / tot_w * total_speech
        out.append((to_real(s0), to_real(s1 - 1e-4), w))
    return out


# ─── Loanwords: spoken in English, captioned in Hindi ─────────────────────────
# Gemini reads English loanwords written in Devanagari (स्कूल, चर्च, रेलवे स्टेशन) oddly.
# The text sent to the VOICE gets them in normal English spelling; captions keep Devanagari.
LOANWORDS = {
    "स्कूल": "school", "चर्च": "church", "रेलवे": "railway", "स्टेशन": "station", "ट्रेन": "train",
    "साइक्लोन": "cyclone", "रेडियो": "radio", "कम्पास": "compass", "कंपास": "compass", "किलोमीटर": "kilometre",
    "फ़ीट": "feet", "फीट": "feet", "फ़ॉरेस्ट": "forest", "फॉरेस्ट": "forest", "रेंजर": "ranger", "टैक्स": "tax",
    "कमेंट": "comment", "डीएनए": "DNA", "सैनिक": "सैनिक", "पैसेंजर": "passenger", "लैब": "lab",
    "वैज्ञानिक": "वैज्ञानिक", "मीटर": "metre", "फ़ोन": "phone", "बस": "बस",
}


def _tts_texts(texts, log=print):
    """Same lines, with English-origin loanwords in Latin script for the voice."""
    try:
        prompt = ("For each Hindi line below, rewrite ONLY the English-origin loanwords that are written in "
                  "Devanagari into normal English spelling in Latin letters (e.g. स्कूल→school, चर्च→church, "
                  "रेलवे स्टेशन→railway station, ट्रेन→train, साइक्लोन→cyclone). Keep every Hindi word exactly as "
                  "it is in Devanagari, keep numbers and punctuation (… । , ?) unchanged, do not add or remove "
                  f"words. Return ONLY a JSON array of {len(texts)} strings.\n\n" +
                  "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts)))
        r = _helper(prompt, 40)
        out = json.loads(re.search(r"\[.*\]", r.text, re.S).group(0))
        if len(out) == len(texts) and all(abs(len(o.split()) - len(t.split())) <= 2 for o, t in zip(out, texts)):
            return [str(o) for o in out]
        log("  ⚠️  Loanword rewrite didn't match the script; using the built-in word list")
    except Exception as e:
        log(f"  ⚠️  Loanword rewrite unavailable ({str(e)[:80]}); using the built-in word list")
    res = []
    for t in texts:
        words = []
        for w in t.split():
            core = w.strip(",.।?!…")
            words.append(w.replace(core, LOANWORDS.get(core, core)) if core else w)
        res.append(" ".join(words))
    return res


# ─── Word-level QA: transcribe, compare with the script, re-record bad scenes ─
NUMBER_WORDS = set("एक दो तीन चार पांच छह सात आठ नौ दस बीस तीस चालीस पचास साठ सत्तर अस्सी नब्बे सौ हज़ार हजार "
                   "लाख करोड़ करोड".split())


def _norm_tokens(text):
    t = text.replace("\u093c", "").replace("ँ", "ं")
    t = re.sub(r"[\u0964\u0965]", " ", t)                       # । ॥ are punctuation
    t = re.sub(r"[^\u0900-\u097Fa-zA-Z ]", " ", t)               # digits, commas, … etc.
    syn = {"यह": "ये", "वह": "वो"}
    toks = [syn.get(w, w) for w in t.lower().split()]
    return [w for w in toks if w not in {x.replace("ँ", "ं").replace("\u093c", "") for x in NUMBER_WORDS}]


def _wav_bytes(x):
    import io
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes())
    return buf.getvalue()


WHISPER_MODEL = os.getenv("WHISPER_MODEL", "mlx-community/whisper-large-v3-turbo")


def _transcribe(clips):
    """Transcripts (Devanagari) of each clip, in order. Local Whisper on the Mac's GPU first
    (free, no quota); Gemini only if Whisper isn't installed."""
    try:
        import mlx_whisper
    except ImportError:
        return _transcribe_gemini(clips)
    out = []
    for c in clips:
        x16 = np.interp(np.linspace(0, len(c) - 1, int(len(c) * 16000 / SR)), np.arange(len(c)), c) \
            if len(c) else c
        r = mlx_whisper.transcribe(x16.astype(np.float32), path_or_hf_repo=WHISPER_MODEL, language="hi",
                                   task="transcribe", condition_on_previous_text=False, verbose=None)
        out.append((r.get("text") or "").strip())
    return out


def _transcribe_gemini(clips):
    """One request: transcripts (Devanagari) of each clip, in order."""
    from google.genai import types as t
    parts = []
    for i, c in enumerate(clips):
        parts += [f"Clip {i + 1}:", t.Part.from_bytes(data=_wav_bytes(c), mime_type="audio/wav")]
    parts.append(f"Transcribe the Hindi speech of each of the {len(clips)} clips exactly as spoken, in "
                 f"Devanagari (write English words in Devanagari too). Write the COMPLETE speech of each "
                 f"clip from its first word to its last — keep going after pauses, do not stop at the first "
                 f"phrase. If a word is not spoken, do not invent it. Return ONLY a JSON array of "
                 f"{len(clips)} strings.")
    r = _helper(parts, 90)
    out = json.loads(re.search(r"\[.*\]", r.text, re.S).group(0))
    if len(out) != len(clips):
        raise RuntimeError("transcript count mismatch")
    return [_to_devanagari(str(o)) for o in out]


def _to_devanagari(text):
    """The transcriber sometimes answers in romanised Hindi ("sab ko chonka diya") — convert
    such a transcript to Devanagari word for word so it can be compared with the script."""
    latin = sum(c.isascii() and c.isalpha() for c in text)
    deva = sum("\u0900" <= c <= "\u097f" for c in text)
    if latin <= deva:
        return text
    try:
        r = _helper("Rewrite this romanised Hindi sentence in Devanagari script, word for word. Do not "
                    "translate, add, drop or reorder words; keep numbers and English acronyms (DNA) as "
                    "they are. Return ONLY the sentence.\n\n" + text, 40)
        out = (r.text or "").strip().strip('"')
        return out or text
    except Exception:
        return text


def _process(raw_path, out_path):
    """Trim edge silence + voice chain."""
    _ff(["-i", raw_path, "-af",
         "silenceremove=start_periods=1:start_threshold=-45dB,areverse,"
         "silenceremove=start_periods=1:start_threshold=-45dB,areverse," + VOICE_FX,
         "-ar", str(SR), "-ac", "1", out_path])


def _find_cuts(x, texts):
    return [0.0] + [c[0] for c in _cut_candidates(x, texts)] + [len(x) / SR]


def _alignment_ok(x, texts, al):
    """The ONE acceptance rule for a take (used by recording and by splicing alike): the
    whole-take alignment hears ≥70% of every scene's words, no scene has speech that isn't in
    the script, and each scene cut — checked alone — has ≥70% of its words (≥85% if it was
    under 85% in the whole-take pass) and repeats nothing from its neighbours.
    Returns (ok, why, {0-based indices of the failing scenes})."""
    from pipeline import qa
    lost = {i for i, s in enumerate(al["scores"]) if s < qa.ISOLATED_MIN}
    extra = {i for i, e in enumerate(al.get("extra") or []) if e}
    if lost or extra:
        why = []
        if lost:
            why.append("words not heard in scenes " + " ".join(
                f"{i + 1}:{al['scores'][i] * 100:.0f}%" for i in sorted(lost)))
        if extra:
            why.append("speech that is not in the script in scenes " + "; ".join(
                f"{i + 1}: {' '.join(al['extra'][i][:8])}…" for i in sorted(extra)))
        return False, " · ".join(why), lost | extra
    weak = {i for i, s in enumerate(al["scores"]) if s < qa.MIN_RECALL}
    segs = [x[int(al["cuts"][i] * SR): int(al["cuts"][i + 1] * SR)] for i in range(len(texts))]
    iso = qa.verify_words(texts, segs, tries=2, min_recall=qa.ISOLATED_MIN)
    bad = {i for i, (sc, m) in enumerate(iso) if sc < (qa.MIN_RECALL if i in weak else qa.ISOLATED_MIN)}
    return (not bad), "; ".join(f"{i + 1}: {iso[i][0] * 100:.0f}% {' '.join(iso[i][1])}" for i in sorted(bad)), bad


def _cut_by_words(x, texts, cuts, log=print):
    """Scene cuts from a word-level Whisper alignment of the whole take (exact, one pass);
    every scene is then re-checked ALONE. Falls back to pause-based cut repair only if the
    alignment can't place every scene. Returns (cuts, [(score, missing)])."""
    try:
        from pipeline.align import align_take
        from pipeline.qa import MIN_RECALL as qa_min
        al = align_take(x, texts)
        ok, why, bad = _alignment_ok(x, texts, al)
        if ok:
            log(f"  ✂️  Scenes cut by words (Whisper alignment): "
                f"{' '.join(f'{c:.2f}' for c in al['cuts'][1:-1])}")
            return al["cuts"], list(zip(al["scores"], al["missing"]))
        # the words were heard and placed: a failing scene is a real problem with the take (a line
        # skipped or slurred), which pause-guessing can't fix. Reject now; the voice step then
        # re-records only those scenes (patch take).
        log(f"  ⚠️  Word check failed ({why})")
        return al["cuts"], [(0.0 if (i in bad or sc < qa_min) else sc, m)
                            for i, (sc, m) in enumerate(zip(al["scores"], al["missing"]))]
    except Exception as e:
        log(f"  ⚠️  Word alignment unavailable ({str(e)[:100]}) — using pause cuts")
    return _repair_cuts(x, texts, cuts, log)


def _drop_breaths(x, regions):
    """Remove short, quiet blips (breaths, lip clicks) so captions start with the first WORD."""
    if len(regions) < 2:
        return regions
    db = lambda a, b: 20 * np.log10(np.sqrt((x[int(a * SR): int(b * SR)] ** 2).mean() + 1e-12))
    levels = [db(a, b) for a, b in regions]
    loud = max(levels)
    keep = [r for r, l in zip(regions, levels) if not (r[1] - r[0] < 0.35 and l < loud - 15)]
    return keep or regions


def _cut_candidates(x, texts):
    """For every scene boundary: candidate cut times (pause centres), best guess first."""
    regions = _speech_regions(x)
    if not regions:
        raise RuntimeError("no speech detected in Gemini audio")
    words, owner = [], []
    for si, tx in enumerate(texts):
        for w in tx.split():
            words.append(w)
            owner.append(si)
    timed = _align(words, regions)
    gaps = [(regions[i][1], regions[i + 1][0]) for i in range(len(regions) - 1)]
    out, prev = [], 0.0
    for si in range(len(texts) - 1):
        last = max(i for i, o in enumerate(owner) if o == si)
        est = (timed[last][1] + timed[last + 1][0]) / 2
        # paragraph breaks are directed as long pauses; commas/“...” inside a line are short. Rank
        # the pauses in a window around the estimate: longest first (with a small distance penalty).
        near = [g for g in gaps if abs((g[0] + g[1]) / 2 - est) < 3.5 and (g[0] + g[1]) / 2 > prev + 0.8]
        near.sort(key=lambda g: -((g[1] - g[0]) - 0.08 * abs((g[0] + g[1]) / 2 - est)))
        cands = [max((g[0] + g[1]) / 2, prev + 0.3) for g in near] or [max(est, prev + 0.3)]
        out.append(cands)
        prev = cands[0]
    return out


def _repair_cuts(x, texts, cuts, log=print):
    """Check every scene ALONE (each must contain exactly its own line); where one fails, try
    the other pauses around its boundaries and keep the cut that makes both scenes correct.
    Returns (cuts, [(score, missing)])."""
    from pipeline import qa
    seg = lambda c, i: x[int(c[i] * SR): int(c[i + 1] * SR)]
    res = qa.verify_words(texts, [seg(cuts, i) for i in range(len(texts))])
    cands = _cut_candidates(x, texts)
    n = len(texts)
    for _ in range(3):
        bad = [i for i, (sc, _) in enumerate(res) if sc < qa.MIN_RECALL]
        if not bad:
            break
        changed = False
        for i in bad:
            for b in (i + 1, i):                       # boundary after the scene, then before it
                if not 1 <= b <= n - 1 or min(res[b - 1][0], res[b][0]) >= qa.MIN_RECALL:
                    continue
                pool = sorted(set(round(c, 3) for c in cands[b - 1]) |
                              {round(g, 3) for g in _pause_points(x, cuts[b - 1] + 0.5, cuts[b + 1] - 0.5)})
                for alt in sorted(pool, key=lambda t: abs(t - cuts[b]))[:8]:
                    if abs(alt - cuts[b]) < 0.05 or not cuts[b - 1] + 0.5 < alt < cuts[b + 1] - 0.5:
                        continue
                    trial = cuts[:b] + [alt] + cuts[b + 1:]
                    r = qa.verify_words(texts[b - 1: b + 1], [seg(trial, b - 1), seg(trial, b)], tries=1)
                    if min(r[0][0], r[1][0]) > min(res[b - 1][0], res[b][0]):
                        log(f"  ✂️  Moved the cut between scenes {b} and {b + 1}: "
                            f"{cuts[b]:.2f}s → {alt:.2f}s ({min(r[0][0], r[1][0]) * 100:.0f}% words)")
                        cuts, res[b - 1], res[b], changed = trial, r[0], r[1], True
                        if min(r[0][0], r[1][0]) >= qa.MIN_RECALL:
                            break
        if not changed:
            break
    return cuts, res


def _pause_points(x, lo, hi):
    """Centres of every pause between lo and hi seconds."""
    regions = _speech_regions(x)
    return [(regions[i][1] + regions[i + 1][0]) / 2 for i in range(len(regions) - 1)
            if lo < (regions[i][1] + regions[i + 1][0]) / 2 < hi]


def _sources(job_id):
    """Saved recordings of an episode: full rejected takes, then patch takes (only some scenes).
    [(name, wav path, scene indices or None for a full take)]"""
    import glob
    out = [(os.path.basename(os.path.dirname(p)), p, None)
           for p in sorted(glob.glob(os.path.join(OUTPUT_DIR, job_id, "_rejected_take_*", "narration.wav")))]
    for p in sorted(glob.glob(os.path.join(OUTPUT_DIR, job_id, "_patch_take_*", "narration.wav"))):
        try:
            idx = json.load(open(os.path.join(os.path.dirname(p), "scenes.json")))
            out.append((os.path.basename(os.path.dirname(p)), p, idx))
        except Exception:
            pass
    return out


def _check_source(x, texts, wav_path, log=print):
    """Per-scene (score, missing) + cuts of one recording, against ITS texts. A scene counts
    only if both the whole-take alignment and the scene heard alone pass (as _alignment_ok).
    Cached next to the recording: recordings never change, and a long take takes minutes."""
    import hashlib
    from pipeline import qa
    key = hashlib.sha1(json.dumps(texts, ensure_ascii=False).encode()).hexdigest()
    cache = os.path.join(os.path.dirname(wav_path), "check.json")
    try:
        c = json.load(open(cache))
        if c.get("key") == key:
            return c["cuts"], [tuple(r) for r in c["res"]]
    except Exception:
        pass
    from pipeline.align import align_take
    al = align_take(x, texts)
    cuts = al["cuts"]
    segs = [x[int(cuts[i] * SR): int(cuts[i + 1] * SR)] for i in range(len(texts))]
    iso = qa.verify_words(texts, segs, tries=2, min_recall=qa.ISOLATED_MIN)
    res = []
    for i, (s, m) in enumerate(zip(al["scores"], al["missing"])):
        need = qa.MIN_RECALL if s < qa.MIN_RECALL else qa.ISOLATED_MIN    # same second-hearing rule
        extra = (al.get("extra") or [[]] * len(texts))[i]
        ok = s >= qa.ISOLATED_MIN and iso[i][0] >= need and not extra
        res.append((max(s, iso[i][0]) if ok else 0.0,
                    m if ok else (m + iso[i][1] + ([f"[extra speech: {' '.join(extra[:6])}]"] if extra else []))))
    json.dump({"key": key, "cuts": cuts, "res": res}, open(cache, "w"), ensure_ascii=False)
    return cuts, res


def splice_missing(job_id):
    """Scene indices (0-based) the last splice attempt had no good recording for."""
    try:
        return json.load(open(os.path.join(OUTPUT_DIR, job_id, "_splice_missing.json")))
    except Exception:
        return None


def splice_takes(script: dict, job_id: str, log=print):
    """Build a narration from the episode's saved Gemini recordings (same narrator, same
    voice): each scene is taken from a recording in which that scene passed the word check.
    Recordings are full rejected takes and patch takes (just the scenes that failed, see
    record_patch). Writes audio/narration.wav + cuts.json and returns True when every scene
    is covered; otherwise saves the uncovered scenes (splice_missing) and returns False."""
    from pipeline import qa
    texts = [(s.get("narration") or "").strip() for s in script.get("scenes", [])]
    sources = _sources(job_id)
    if not sources:
        return False
    best = [None] * len(texts)                     # (score, source name, segment)
    ok_ = lambda b: b is not None and b[0] >= qa.ISOLATED_MIN
    for name, path, idx in sources:
        todo = [i for i, b in enumerate(best) if not ok_(b)]
        if not todo:
            break
        ids = idx if idx is not None else list(range(len(texts)))
        if not set(ids) & set(todo):
            continue
        log(f"  🧩 Checking {name} (scenes still needed: {', '.join(str(i + 1) for i in todo)})...")
        try:
            x = _pcm(path)
            cuts, res = _check_source(x, [texts[i] for i in ids], path, log)
        except Exception as e:
            log(f"  ⚠️  {name} unusable for splicing ({str(e)[:80]})")
            continue
        for k, i in enumerate(ids):
            sc = res[k][0]
            if best[i] is None or sc > best[i][0]:
                best[i] = (sc, name, x[int(cuts[k] * SR): int(cuts[k + 1] * SR)])
    missing = [i for i, b in enumerate(best) if not ok_(b)]
    json.dump(missing, open(os.path.join(OUTPUT_DIR, job_id, "_splice_missing.json"), "w"))
    if missing:
        log(f"  ⚠️  Splicing: no good recording yet for scenes {[i + 1 for i in missing]}")
        return False
    if len({b[1] for b in best}) < 2:
        return False                               # one rejected take alone: nothing to combine
    audio_dir = os.path.join(OUTPUT_DIR, job_id, "audio")
    os.makedirs(audio_dir, exist_ok=True)
    full = np.concatenate([b[2] for b in best])
    cuts = [0.0]
    for b in best:
        cuts.append(cuts[-1] + len(b[2]) / SR)
    pcm = (np.clip(full, -1, 1) * 32767).astype(np.int16)
    with wave.open(os.path.join(audio_dir, "narration.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    json.dump(cuts, open(os.path.join(audio_dir, "cuts.json"), "w"))
    json.dump({"takes": [b[1] for b in best]}, open(os.path.join(audio_dir, "spliced.json"), "w"))
    used = sorted({b[1] for b in best})
    log(f"  🧩 Narration spliced from {len(used)} recordings: " + "; ".join(
        f"{n}: scenes {', '.join(str(i + 1) for i, b in enumerate(best) if b[1] == n)}" for n in used))
    return True


def record_patch(script: dict, job_id: str, scene_idx, template: str = "mystery", log=print):
    """Re-record ONLY the given scenes (0-based) in one short Gemini request, saved as
    <job>/_patch_take_<time>/ for splice_takes. A long video with one slurred scene costs a
    few seconds of voice quota instead of a whole new 10-minute take."""
    # each failed scene is recorded WITH its neighbours: a 1-line request makes the voice model
    # read its instructions aloud (seen), and context keeps the delivery like the full take.
    # Neighbours are only spare copies; splice_takes picks the best recording of every scene.
    _MODEL[0] = script.get("voice_model")
    n = len(script["scenes"])
    scene_idx = sorted({j for i in scene_idx for j in (i - 1, i, i + 1) if 0 <= j < n})
    texts = [(script["scenes"][i].get("narration") or "").strip() for i in scene_idx]
    d = os.path.join(OUTPUT_DIR, job_id, f"_patch_take_{int(time.time())}")
    os.makedirs(d, exist_ok=True)
    raw, full = os.path.join(d, "narration_raw.wav"), os.path.join(d, "narration.wav")
    log(f"  🎙️  Re-recording only scenes {', '.join(str(i + 1) for i in scene_idx)} (patch take)...")
    try:
        _speak_retry("\n\n".join(_tts_texts(texts, log)), template, raw, log)
        _process(raw, full)
    except Exception:
        import shutil
        shutil.rmtree(d, ignore_errors=True)
        raise
    json.dump(list(scene_idx), open(os.path.join(d, "scenes.json"), "w"))
    return d


def narrate_script(script: dict, job_id: str, template: str = "mystery", log=print):
    """
    Returns a list like tts_generator.generate_tts_for_script (one dict per scene with
    audio_path, duration, words, narration), or raises if Gemini TTS is unavailable.

    One take for the whole script → split into scenes at the pauses → every scene is
    transcribed and checked word-by-word against the script; scenes with missing words are
    re-recorded. The final narration + scene cut points are saved, so later re-renders and
    remixes reuse exactly the same audio.
    """
    _MODEL[0] = script.get("voice_model")
    scenes = script.get("scenes", [])
    texts = [(s.get("narration") or "").strip() for s in scenes]
    if not texts or not all(texts):
        raise RuntimeError("every scene needs narration")
    audio_dir = os.path.join(OUTPUT_DIR, job_id, "audio")
    os.makedirs(audio_dir, exist_ok=True)
    raw = os.path.join(audio_dir, "narration_raw.wav")
    full = os.path.join(audio_dir, "narration.wav")
    cuts_path = os.path.join(audio_dir, "cuts.json")

    if os.path.exists(full):                                        # reuse the recorded take
        x = _pcm(full)
        cuts = json.load(open(cuts_path)) if os.path.exists(cuts_path) else _find_cuts(x, texts)
    else:
        tts = _tts_texts(texts, log)
        if not (os.path.exists(raw) and os.path.getsize(raw) > 10000):
            log(f"  🎙️  Gemini TTS ({_MODEL[0] or GEMINI_TTS_MODEL}, voice {GEMINI_TTS_VOICE}) — one take for all scenes...")
            _speak_retry("\n\n".join(tts), template, raw, log)
        _process(raw, full)
        x = _pcm(full)
        cuts = _find_cuts(x, texts)
    # every scene must say exactly ITS line: each slice is transcribed alone; bad cuts are moved
    from pipeline import qa
    cuts, checked = _cut_by_words(x, texts, cuts, log)
    scores = [sc for sc, _ in checked]
    bad = [(i + 1, round(sc * 100), " ".join(m)) for i, (sc, m) in enumerate(checked) if sc < qa.MIN_RECALL]
    if bad and os.path.exists(os.path.join(audio_dir, "spliced.json")):
        for f in (full, cuts_path, os.path.join(audio_dir, "spliced.json")):
            if os.path.exists(f):
                os.remove(f)
        raise RuntimeError(f"spliced narration rejected — scenes {bad}")
    if bad:
        import shutil, time as _t
        keep = os.path.join(OUTPUT_DIR, job_id, f"_rejected_take_{int(_t.time())}")
        os.makedirs(keep, exist_ok=True)
        for f in (raw, full, cuts_path):
            if os.path.exists(f):
                shutil.move(f, keep)
        raise RuntimeError(f"narration check failed — scenes (no., % words heard, missing) {bad}")
    log(f"  ✅ Narration check: every scene says exactly its own line ({min(scores) * 100:.0f}%+ of words)")
    json.dump(cuts, open(cuts_path, "w"))

    results = []
    for si, scene in enumerate(scenes):
        a, b = cuts[si], cuts[si + 1]
        seg = x[int(a * SR): int(b * SR)]
        sub = _drop_breaths(seg, _speech_regions(seg))
        mp3 = os.path.join(audio_dir, f"scene_{scene.get('scene_number', si + 1):02d}.mp3")
        # -ss/-t BEFORE -i: the slice starts at t=0, so the fade-out time is relative to the scene
        _ff(["-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", full, "-af",
             f"afade=t=in:d=0.02,afade=t=out:st={max(b - a - 0.04, 0):.3f}:d=0.04",
             "-ar", "44100", "-b:a", "192k", mp3])
        lvl = _pcm(mp3)
        if len(lvl) == 0 or np.sqrt((lvl ** 2).mean()) < 1e-3:
            raise RuntimeError(f"scene {si + 1} narration clip is silent after splitting")
        results.append({"scene_number": scene.get("scene_number", si + 1), "audio_path": mp3,
                        "duration": b - a, "words": _align(texts[si].split(), sub) if sub else [],
                        "narration": texts[si]})
    log(f"  🎙️  Narration ready: {len(x) / SR:.1f}s, {len(results)} scenes (word-checked)")
    return results
