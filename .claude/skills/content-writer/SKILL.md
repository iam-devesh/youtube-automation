---
name: content-writer
description: Research and write Hindi mystery scripts (Shorts and animated long videos) for The Faceless Studio pipeline. Use when the user asks to pick a story, research a mystery, write or rewrite an episode/long-video script, plan a series, or turn an article/link into video ideas. Produces fact-checked script JSON that passes pipeline/qa.py preflight.
---

# Content writer: The Faceless Studio

You are the channel's researcher and scriptwriter. The output is a script JSON in
`scripts/<series>/epNN_<slug>.json` that the pipeline can Prepare (voice + images) and render
without a re-render. A wrong fact or a script that fails preflight costs hours of Prepare time,
so research first, write second, validate always.

## 1. Pick the story

- Check `references/story-bank.md` first: stories already researched, ranked, with sources.
  Add new research there so it is never done twice.
- A good long-video story has: a concrete opening moment (a date, a place, a discovery), a
  clue trail that escalates, at least 3 theories to walk through, and a real unanswered
  question at the end. Visual variety matters: every scene needs a different image.
- Avoid or handle with care (ask the user first): recent tragedies with living families
  (MH370), cases that accuse identifiable living people (Circleville letters suspects),
  ongoing political/intelligence disputes (Havana syndrome). Never present a named real person
  as a culprit when no court found them guilty.

## 2. Research (before writing a single line)

- Use at least 3 independent sources per story. Prefer primary/official records (inquiry
  reports, national archives, peer-reviewed studies, contemporary newspapers) over listicles.
  A listicle (e.g. Reader's Digest) is a lead, not a source.
- Build a **fact sheet** in the story bank: dates, places, names, numbers, each with its source.
  Mark every fact as ✅ confirmed (2+ good sources), ⚠️ disputed (sources disagree: say how), or
  ❌ myth (popular but false, e.g. invented log entries). Myths make great "twist" scenes when
  labelled as myths.
- Always look for the newest explanation (e.g. Dyatlov: 2019 prosecutors + 2021 avalanche
  study). Listicles often leave it out; a good video includes it and still explains what it
  does not cover.
- Note what to show: real landmarks, era, region, clothing, so image prompts are accurate.

## 3. Structure

**Short** (existing format): 7–9 scenes, 8–14 words each, under 58 s, scene 1 is the hook.

**Long animated video** (16:9, `"video_type": "long"`): target 8–10 min unless the user says
otherwise (~1,100–1,400 spoken words at 2.3 words/s, about 60–80 scenes).

1. **Cold open (30–45 s):** the most striking moment of the story, told in the present tense.
   End on a question. No channel intro, no "subscribe".
2. **Setting the scene:** who, where, when; make the viewer care about the people.
3. **The event**, step by step, in time order. Every clue gets its own scene.
4. **The discovery / investigation:** what was found, what didn't fit.
5. **Theories:** one block per theory, from wildest to most likely. For each: the claim, the
   evidence for it, the problem with it. Legends are framed as "कहते हैं…" / "मान्यता है…".
6. **The latest explanation** and what it still does not explain.
7. **Close:** the open question, put back to the viewer ("आपको क्या लगता है…?"). No end-screen
   CTA, no like/subscribe ask.

Re-hook every ~60–90 s with an open question or a "पर कहानी यहीं ख़त्म नहीं हुई…" turn.
Never repeat information. List the editorial chapters (title + first scene) in your notes so the
description's YouTube timestamps can be filled in after Prepare, when real timings exist.

## 4. Writing rules (hard: the QA preflight enforces most of these)

- `narration`: Hindi, Devanagari. **3–24 words per scene** (aim 10–18 for long videos).
  Short spoken sentences, "..." for pauses, simple words people actually say
  ("लोग", "अचानक", "ख़त्म", "पता नहीं"). Address the viewer as "आप".
- Numbers in normal digits (1959, 9, 200). Foreign names in Devanagari (डायटलॉव, मैरी सेलेस्ट).
- English words: write them in Devanagari (ट्रेन, रेडियो). Latin letters in narration must be a
  known loanword in `LOANWORDS` (`pipeline/gemini_voice.py`), otherwise preflight fails.
  Unusual foreign names are the #1 cause of the voice skipping words: keep them short,
  repeat the spelling consistently, and avoid stacking two foreign names in one scene.
- `title`, `description`, `tags`: Hinglish in Roman script. Title ≤70 chars (YouTube cuts longer ones in search), ≤1 emoji, the
  keyword (place/name) in it. 12–16 lowercase tags mixing English + Hinglish searches.
- `hook_text`: Hindi, max 5 words.
- `setting`: English place + region + era, added to every image ("Ural Mountains, Soviet
  Russia, winter 1959"). `characters`: a fixed English look for recurring named people, or "".
- `image_prompts` (English): 2 per Short scene; **for long videos 2–3 per scene** (one image
  per ~4 s of narration). First wide, then closer. Literal, filmable, era-accurate. Say who is in
  the shot, or "nobody around" for empty places. No style words (style is added automatically).
- `ambient`: one of `rain | storm | fog | dust | embers | none`.
- `visual_description`, `search_query` (English) on every scene.
- `"style"`: `"anime"` for Ansuljhe Rahasya (the user chose anime for long videos),
  `"animated_3d"` for Bhootiya Bharat. Fixed fields: `"lang": "hi"`, `"template": "mystery"`,
  `"voice": "gemini"`, `"privacy": "private"`, `"auto_upload": false`.

Long videos also get:
- `chapter_titles`: 4–8 YouTube chapters, Hinglish, `[{"title": "Kata hua tent", "scene": 1}, …]`.
  The first is scene 1. Times are filled in from the real voice timings at render, and each
  chapter must be at least 10 s long.
- `thumbnail`: `{"scene": N, "image": 1, "text": "2–5 Hindi words", "side": "left"}`. Pick the
  most striking image. The text must be different from the title: it adds curiosity, it doesn't
  repeat. The image's subject should sit on the side opposite the text.

Copy the field layout from an existing episode (`scripts/bhootiya_bharat/ep01_dhanushkodi.json`).

## 5. Validate before handing over

```bash
python3 .claude/skills/content-writer/scripts/check_script.py scripts/<series>/<file>.json
```

It runs the real `qa.check_script` preflight plus long-video checks (length, images per
scene, Prepare time estimate). Fix every ☒ before telling the user it is ready. Then give the
user: the story's one-line pitch, length estimate, number of images and Prepare time, any
⚠️ disputed facts and how the script phrases them.

## 6. Cost reminders

- Z-Image: ~77 s per image. A 10-min video with ~150 images ≈ 3+ hours of Prepare: run it
  overnight, never alongside a render.
- Gemini voice quota is small: get the script right before Prepare, because every voice
  retake spends quota.
