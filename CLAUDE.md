# Project rules

## Narration must be clear: every word, in the final video

The owner listens closely and catches a single masked syllable. A video is not done until
every word of every scene is clearly heard **in the final mixed video** (voice + music +
ambience + sound effects), not just in the voice recording.

- `pipeline/word_check.py` runs automatically after every render (long videos and Shorts). It transcribes
  each scene of the finished video alone and checks every script word, including each scene's
  first word. A failure stops the render; don't loosen it to get a video through.
- Never place a sound effect (whoosh, boom, impact) on or near the start of a spoken line.
  Seen: a per-scene whoosh peaked on each scene's first syllable ("पैरों" heard as "प्रों").
- The older chapter checks only need 70% of a scene's words. Passing them is not proof the
  narration is clear.
- If a word is unclear, find the cause (the mix, a cut in the wrong place, the recording)
  and fix that. Don't just re-render and hope.
