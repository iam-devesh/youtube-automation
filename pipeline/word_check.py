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


def _missing(text, clip, tries):
    """Script words not heard in `clip`: the best of `tries` transcriptions (Whisper varies)."""
    from pipeline import qa
    from pipeline.gemini_voice import _transcribe
    best = None
    for _ in range(tries):
        sc, miss = qa.heard_words(text, _transcribe([clip])[0])
        if best is None or len(miss) < len(best):
            best = miss
        if not best:
            break
    return best


def check_final(cl, video_path, audio_results, starts, tries=3, log=print):
    """cl: the job's checklist. starts[i]: scene i's start (s) in the final video.
    Fails the checklist if any scene lost a word in the final mix."""
    from pipeline import qa
    from pipeline.gemini_voice import _pcm, _norm_tokens
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
        real = [w for w in final_miss if w not in voice_miss]
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
