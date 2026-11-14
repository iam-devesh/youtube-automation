"""
align.py — word-level alignment of a recorded narration take to the script (local Whisper).

One Whisper pass over the whole take (word timestamps) is matched word-by-word against the
script. From that single pass we get, without guessing from pauses:
  • the exact scene boundaries (cut in the quietest point between scene i's last word and
    scene i+1's first word),
  • a per-scene score (share of the scene's words found in order),
  • real per-word timings for the captions.
"""
import difflib
import os

import numpy as np

SR = 24000
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "mlx-community/whisper-large-v3-turbo")


def _whisper_pass(x, offset=0.0):
    import mlx_whisper
    x16 = np.interp(np.linspace(0, len(x) - 1, int(len(x) * 16000 / SR)), np.arange(len(x)), x)
    r = mlx_whisper.transcribe(x16.astype(np.float32), path_or_hf_repo=WHISPER_MODEL, language="hi",
                               task="transcribe", word_timestamps=True,
                               condition_on_previous_text=False, verbose=None)
    out = []
    for seg in r.get("segments", []):
        for w in seg.get("words", []):
            t = (w.get("word") or "").strip()
            if t:
                out.append((offset + float(w["start"]), offset + float(w["end"]), t))
    return out


def _speech_in(x, a, b, frame=0.05):
    """Seconds of speech-level audio between a and b."""
    seg = x[int(a * SR): int(b * SR)]
    n = int(frame * SR)
    if len(seg) < n:
        return 0.0
    rms = np.sqrt((seg[: len(seg) // n * n].reshape(-1, n) ** 2).mean(axis=1))
    return float((rms > 0.02).sum() * frame)


def _chunks(x, target=60.0, search=10.0, frame=0.05):
    """[(start, end)] covering the clip: one piece up to 90 s, else ~target-second pieces
    split at the longest pause within ±search s of each mark (never inside a word)."""
    dur = len(x) / SR
    if dur <= 90:
        return [(0.0, dur)]
    n = int(frame * SR)
    rms = np.sqrt((x[: len(x) // n * n].reshape(-1, n) ** 2).mean(axis=1))
    quiet = rms < max(0.01, np.percentile(rms, 20) * 1.5)
    out, a = [], 0.0
    while dur - a > target + search:
        lo, hi = int((a + target - search) / frame), int((a + target + search) / frame)
        best, run, best_mid = 0, 0, (a + target)
        for k in range(lo, min(hi, len(quiet))):
            run = run + 1 if quiet[k] else 0
            if run > best:
                best, best_mid = run, (k - run / 2 + 0.5) * frame
        out.append((a, best_mid))
        a = best_mid
    out.append((a, dur))
    return out


def _whisper_words(x):
    """[(start, end, word)] for the whole clip (x: float32 mono at 24 kHz).
    Whisper sometimes skips a whole stretch of a long recording (seen: 13–18 s holes in a
    9-min take that was complete). Any hole with speech in it is transcribed again on its own."""
    dur = len(x) / SR
    out = []
    for a, b in _chunks(x):                 # long takes: ~60 s pieces, where Whisper is reliable
        out += [w for w in _whisper_pass(x[int(a * SR): int(b * SR)], a) if w[0] < b]
    edges = [0.0] + [t for w in out for t in (w[0], w[1])] + [dur]
    holes = [(edges[i], edges[i + 1]) for i in range(0, len(edges) - 1, 2)
             if edges[i + 1] - edges[i] > 2.5 and _speech_in(x, edges[i], edges[i + 1]) > 1.0]
    for a, b in holes:
        a0, b0 = max(0.0, a - 0.2), min(dur, b + 0.2)
        extra = [w for w in _whisper_pass(x[int(a0 * SR): int(b0 * SR)], a0) if a - 0.1 <= w[0] < b]
        out += extra
    return sorted(out, key=lambda w: w[0])


def _quietest(x, a, b, frame=0.02):
    """Time of the quietest 20 ms frame between a and b seconds."""
    i0, i1 = int(a * SR), int(b * SR)
    n = int(frame * SR)
    if i1 - i0 < 2 * n:
        return (a + b) / 2
    seg = x[i0:i1]
    k = len(seg) // n
    rms = np.sqrt((seg[: k * n].reshape(k, n) ** 2).mean(1))
    return a + (int(np.argmin(rms)) + 0.5) * frame


def _cut_before_onset(x, a, b, frame=0.02, lead=0.12):
    """Cut point for a scene break: Whisper says the previous scene ends at ~a and the next
    starts at ~b (its word starts are often early). Find the longest silence around there in
    the AUDIO and cut `lead` seconds before the voice comes back — so the next scene (and its
    first caption) starts with the voice and the pause stays at the end of the previous one."""
    n = int(frame * SR)
    lo, hi = max(a - 0.2, 0), min(b + 1.0, len(x) / SR)
    seg = x[int(lo * SR): int(hi * SR)]
    k = len(seg) // n
    if k < 3:
        return (a + b) / 2
    full = x[: len(x) // n * n].reshape(-1, n)
    thr = max(np.percentile(np.sqrt((full ** 2).mean(1)), 90) * 0.08, 1e-4)
    rms = np.sqrt((seg[: k * n].reshape(k, n) ** 2).mean(1))
    quiet = rms < thr
    best, cur, best_end = 0, 0, None
    for i, q in enumerate(quiet):
        cur = cur + 1 if q else 0
        if cur > best:
            best, best_end = cur, i
    if not best_end or best < 3:                  # no clear silence: quietest point in the gap
        return _quietest(x, a, max(b, a + 0.04))
    onset = lo + (best_end + 1) * frame           # first voiced frame after the longest silence
    start = onset - best * frame
    return max(start + 0.02, onset - lead)


def align_take(x, texts):
    """
    x: narration take (float32, 24 kHz). texts: per-scene script lines.
    Returns {"cuts": [0, …, dur], "scores": [per scene], "missing": [[words]],
             "words": [[(start, end, word) relative to the scene start]]}.
    """
    from pipeline.qa import _canon
    from pipeline.gemini_voice import _norm_tokens
    dur = len(x) / SR
    hyp = _whisper_words(x)
    # script words (punctuation kept for captions) + their canonical form for matching
    ref = []                                       # (scene, caption word, canon or None)
    for si, t in enumerate(texts):
        for w in t.split():
            toks = _norm_tokens(w)
            ref.append((si, w, _canon(toks[0]) if toks else None))
    hyp_c = []
    for s, e, w in hyp:
        toks = _norm_tokens(w)
        hyp_c.append(_canon(toks[0]) if toks else "")

    # match: exact canon blocks, then fuzzy pairing inside the replaced spans.
    # Long takes are matched in order, BLOCK scenes at a time, each against the stretch of heard
    # words that follows the previous block. A whole-script match lets a repeated line pair with
    # the wrong part of the take and derail every scene after it.
    ref_c = [c or "" for _, _, c in ref]
    match = {}                                     # ref index → hyp index
    BLOCK = 12
    first_ref = [next(i for i, r in enumerate(ref) if r[0] == si) for si in range(len(texts))] + [len(ref)]
    j_next = 0
    for b0 in range(0, len(texts), BLOCK):
        r0, r1 = first_ref[b0], first_ref[min(b0 + BLOCK, len(texts))]
        h0 = j_next
        h1 = len(hyp_c) if b0 + BLOCK >= len(texts) else min(len(hyp_c), h0 + int((r1 - r0) * 1.6) + 20)
        sm = difflib.SequenceMatcher(None, ref_c[r0:r1], hyp_c[h0:h1], autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            i1, i2, j1, j2 = i1 + r0, i2 + r0, j1 + h0, j2 + h0
            if tag == "equal":
                for k in range(i2 - i1):
                    match[i1 + k] = j1 + k
            elif tag == "replace":
                j = j1
                for i in range(i1, i2):
                    best, bj = 0.0, None
                    for jj in range(j, min(j2, j + 3)):
                        r = difflib.SequenceMatcher(None, ref_c[i], hyp_c[jj]).ratio()
                        if r > best:
                            best, bj = r, jj
                    if bj is not None and best >= 0.6:
                        match[i] = bj
                        j = bj + 1
        got = [match[i] for i in range(r0, r1) if i in match]
        j_next = max(got) + 1 if got else min(len(hyp_c), h0 + (r1 - r0))
    # split/joined compounds: a ref word whose canon is a prefix of an already-matched hyp word
    # (लौटकर ↔ लौट कर) counts as heard
    scenes = len(texts)
    idx = [[i for i, r in enumerate(ref) if r[0] == si] for si in range(scenes)]
    scored = [[i for i in ids if ref[i][2]] for ids in idx]   # words that carry sound
    scores, missing = [], []
    for si in range(scenes):
        got = [i for i in scored[si] if i in match]
        scores.append(len(got) / max(len(scored[si]), 1))
        missing.append([ref[i][1] for i in scored[si] if i not in match])

    # boundaries: quietest point between scene i's last heard word and scene i+1's first
    cuts = [0.0]
    for si in range(scenes - 1):
        last = [match[i] for i in idx[si] if i in match]
        first = [match[i] for i in idx[si + 1] if i in match]
        if last and first and max(last) < min(first):
            # words in between (misheard / unmatched) belong to whichever side the scene break
            # pause puts them on → cut in the longest gap between consecutive words
            gaps = [(hyp[j + 1][0] - hyp[j][1], j) for j in range(max(last), min(first))]
            g, j = max(gaps)
            a, b = hyp[j][1], hyp[j + 1][0]
            # cut just before the next scene's first word: the pause stays at the END of the
            # previous scene, so each scene (and its first caption) starts with the voice
            cut = _cut_before_onset(x, a, b)
        elif last and first:                       # overlapping stamps: split the difference
            cut = (hyp[max(last)][1] + hyp[min(first)][0]) / 2
        else:                                      # scene not heard at all: proportional guess
            done = sum(len(t) for t in texts[: si + 1]) / max(sum(len(t) for t in texts), 1)
            cut = done * dur
        cuts.append(min(max(cut, cuts[-1] + 0.3), dur - 0.3 * (scenes - si - 1)))
    cuts.append(dur)

    # caption timings per scene (relative to the scene start); unheard words interpolated
    words = []
    for si in range(scenes):
        a, b = cuts[si], cuts[si + 1]
        ids = idx[si]
        times = [(hyp[match[i]][0] - a, hyp[match[i]][1] - a) if i in match else None for i in ids]
        known = [k for k, t in enumerate(times) if t]
        out = []
        for k, i in enumerate(ids):
            if times[k]:
                s, e = times[k]
            else:
                prev = max((j for j in known if j < k), default=None)
                nxt = min((j for j in known if j > k), default=None)
                s = times[prev][1] if prev is not None else 0.05
                e = times[nxt][0] if nxt is not None else (b - a) - 0.05
                gap = [j for j in range(k) if j > (prev if prev is not None else -1)]
                span = (nxt if nxt is not None else len(ids)) - (prev if prev is not None else -1) - 1
                pos = len(gap)
                s, e = s + (e - s) * pos / max(span, 1), s + (e - s) * (pos + 1) / max(span, 1)
            s = max(0.0, min(s, b - a))
            e = max(s + 0.05, min(e, b - a))
            out.append((s, e, ref[i][1]))
        words.append(out)
    return {"cuts": cuts, "scores": scores, "missing": missing, "words": words, "heard": len(hyp)}
