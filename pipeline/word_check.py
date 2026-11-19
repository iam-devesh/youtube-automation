"""
pipeline/word_check.py: strict "every word is clear" check on the FINISHED video.

The chapter checks only need 70% of a scene's words (Whisper is noisy), so a mix that masks
one syllable per scene passes them. Seen: a per-scene whoosh peaked on every scene's first
syllable ("पैरों" heard as "प्रों"), and all 448 checks passed.

Here every scene of the final video (voice + music + ambience + sound effects) is transcribed
alone and compared word by word with the script. To avoid false alarms, a word only counts as
LOST if it is heard in the scene's clean voice recording but not in the final video. Then the
mix (or a cut) took it away, not the transcriber. A word the transcriber can't get even from
the clean voice is listed as a note, not a failure.
"""
import os

SR = 24000
STAGE = "6 Every word clear"


_VOICING = str.maketrans("जगदबडझघधभढ", "चकतपटचकतपट")    # voiced → unvoiced (Whisper mixes them)


def _sounds_heard(word, hyp):
    """True if `word` (or a close sound-alike) is in transcript `hyp`."""
    import difflib
    from pipeline.qa import _canon
    from pipeline.gemini_voice import _norm_tokens
    key = lambda w: _canon(w).translate(_VOICING).replace("य", "")
    toks = [key(t) for t in _norm_tokens(hyp)]
    for w in _norm_tokens(word):
        k = key(w)
        if not any(t == k or difflib.SequenceMatcher(None, k, t).ratio() >= 0.7 for t in toks):
            return False
    return True


def _missing(text, clip, tries):
    """Script words not heard in `clip`: the best of `tries` transcriptions (Whisper varies).
    Numbers count too: the transcriber never matches digits word by word, so a scene that STARTS
    with a number must start with a spoken number ("800" → "आठ सौ" or "800"), and a scene with
    numbers must have some number in it (seen: Ramappa scene 10 lost "800 साल" and passed)."""
    from pipeline import qa
    from pipeline.align import _numberish
    from pipeline.gemini_voice import _transcribe
    raw = text.split()
    # numbers written as digits ("800") or as words ("आठ सौ") — both are skipped by the word
    # matcher, so they are checked here
    lead_num = bool(raw) and _numberish(raw[0])
    has_num = any(_numberish(w) for w in raw)
    best = None
    for _ in range(tries):
        hyp = _transcribe([clip])[0]
        sc, miss = qa.heard_words(text, hyp)
        miss = list(miss)
        words = [w for w in hyp.split() if w.strip(" ,.?!।…")]
        if lead_num and not (words and any(_numberish(w) for w in words[:2])):
            miss.insert(0, raw[0])
        elif has_num and not any(_numberish(w) for w in words):
            miss.append(next(w for w in raw if _numberish(w)))
        if best is None or len(miss) < len(best):
            best = miss
        if not best:
            break
    return best


def _edge_words(text):
    """The first two and last two spoken words of a line: where a misplaced cut bites."""
    toks = text.split()
    return set(toks[:2] + toks[-2:])


def check_final(cl, video_path, audio_results, starts, tries=3, log=print):
    """cl: the job's checklist. starts[i]: scene i's start (s) in the final video.
    Fails the checklist if any scene lost a word in the final mix."""
    from pipeline import qa
    from pipeline.gemini_voice import _pcm, _norm_tokens, _transcribe
    x = _pcm(video_path)
    lost, notes = [], []
    for i, (au, st) in enumerate(zip(audio_results, starts)):
        text = au.get("narration") or ""
        if not text.strip():
            continue
        a, b = max(st - 0.1, 0.0), st + au["duration"] + 0.1
        final_miss = _missing(text, x[int(a * SR): int(b * SR)], tries)
        if not final_miss:
            continue
        voice_miss = _missing(text, _pcm(au["audio_path"]), tries)
        # missing from the clean voice too = the transcriber's limit... unless it's a word at the
        # scene's edge: then the voice clip itself was cut in the wrong place (a real loss)
        edges = _edge_words(text)
        real = [w for w in final_miss if w not in voice_miss or w in edges]
        if real:
            # second hearing: a wider window (a tight edge clips Whisper's context) and Hindi
            # sound-alike spellings Whisper drifts between (जनवरी ↔ चनवरी, थ्योरीज ↔ थेरीज)
            a2, b2 = max(st - 0.4, 0.0), st + au["duration"] + 0.4
            hyp = " ".join(_transcribe([x[int(a2 * SR): int(b2 * SR)]])[0] for _ in range(2))
            real = [w for w in real if not _sounds_heard(w, hyp)]
        first = (_norm_tokens(text) or [""])[0]
        tag = " (FIRST WORD)" if any(first in _norm_tokens(w) for w in real) else ""
        if real:
            lost.append(f"scene {i + 1}: {' '.join(real)}{tag}")
        else:
            notes.append(f"scene {i + 1}: {' '.join(final_miss)}")
    if notes:
        log(f"  ℹ️  Words the transcriber misses even in the clean voice (not a mix problem): "
            f"{'; '.join(notes)}")
    cl.check(STAGE, "every script word heard in the final video (each scene vs its clean voice)",
             not lost, "; ".join(lost) if lost else f"all {len(audio_results)} scenes")
    cl.gate(STAGE)
    return lost
