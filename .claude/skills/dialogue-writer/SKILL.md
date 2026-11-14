---
name: dialogue-writer
description: Write Hindi character dialogue for The Faceless Studio's animated series and long videos: character bible, voices, dramatized scenes with narrator + characters, and image prompts that keep characters looking the same. Use when the user wants characters to speak, a dramatized re-enactment, a cast for a series, or to turn a researched story (from content-writer) into scenes with dialogue.
---

# Dialogue writer: The Faceless Studio

Turns a researched story into dramatized scenes where characters speak, without breaking
the pipeline. Works together with **content-writer**: that skill owns research, facts and the
overall structure; this one owns the cast, the voices and the spoken lines.

## Pipeline reality (read first)

- **Today the pipeline voices one narrator** (Gemini, Charon) from each scene's `narration`.
  Dialogue is written in a `dialogue` field next to it. Until multi-voice support is built, the
  narrator reads the scene, so every scene must still work when read by one voice:
  `narration` = the lines joined, each with an attribution ("डायटलॉव ने कहा…").
- **Multi-voice plan:** Gemini TTS can voice **at most 2 speakers per request**. So every
  scene may contain at most **2 voices, and the narrator counts as one**. A scene is
  narrator + 1 character, or 2 characters with no narrator. Write to this limit now so the
  scripts don't need rewriting later.
- Captions show `narration`, word for word. Never let `dialogue` and `narration` disagree
  in content.

## 1. Character bible (once per series)

Keep it in `scripts/<series>/cast.json` (format in `references/cast-template.json`). For each
character:

- `name` (Devanagari, as spoken) and `id` (Latin, used in `dialogue`).
- `role`, `age`, `want` (what they're trying to do), `fear`, and one **speech habit**
  (e.g. a gruff army man in short orders; a student who asks questions; an old village woman
  who speaks in proverbs). The habit is what makes them recognisable by ear.
- `voice`: a Gemini prebuilt voice, distinct from the narrator (Charon) and from each other.
  Pick from: male deeper (Fenrir, Orus, Algenib); male lighter (Puck, Iapetus, Umbriel);
  female (Kore, Aoede, Leda, Despina, Sulafat). Note a `voice_direction` (tone, pace, accent).
- `look`: a fixed English visual description of 25–40 words (age, build, skin tone, face,
  hair, outfit with colours, one signature item). It is pasted unchanged into every image
  prompt the character appears in. Real people: era-accurate, respectful, no caricature.

Keep the cast small: 2–4 speaking characters in an episode. Viewers must recognise them
from the image and the voice alone.

## 2. When to dramatize (and when not)

- Dramatize **moments**: the decision, the discovery, the argument, the last words. Keep
  the narrator for context, facts, dates, and theories.
- About 25–40% of a long video in dialogue is the sweet spot. More than that and it stops
  being a documentary and the facts get lost.
- **Real people:** only put words in their mouths that are documented (diary, letters,
  testimony, logbook), or are clearly a reconstruction. The narrator says so first
  ("डायरी में लिखा था…", "शायद उन्होंने सोचा होगा…"). Never invent a confession, an accusation,
  or a quote that blames a real person.
- Fictional or legend characters (village elder, ghost of a folk tale) can speak freely, framed as
  "कहते हैं…".

## 3. Writing the lines

- Hindi in Devanagari, spoken, everyday. Each character uses their speech habit.
- **One line = 3–14 words.** One thought per line; people interrupt and don't speak in
  paragraphs.
- Subtext beats exposition: "कल तक तो यहीं था…" not "मेरा भाई जो कल यहाँ था अब गायब है".
- Every exchange must **move the story or raise tension**. Cut greetings and filler.
- Emotion goes in the line and in `direction` (e.g. "whispering, terrified"), never as a
  bracketed stage direction inside the text. The voice would read it out loud.
- Foreign names in Devanagari; the same spelling every time. Latin English words follow the
  content-writer `LOANWORDS` rule.
- The scene limits still apply to the whole scene: **narration ≤24 words**, ≥3 words.
  Dialogue-heavy moments become several short scenes, which also gives more images.

## 4. Scene format

```json
{
  "scene_number": 12,
  "narration": "रात के तीन बजे… डायटलॉव ने टेंट में कहा: बाहर कुछ है… सब चुप रहो।",
  "dialogue": [
    {"speaker": "narrator", "text": "रात के तीन बजे…"},
    {"speaker": "dyatlov", "text": "बाहर कुछ है… सब चुप रहो।",
     "direction": "whispering, tense, holding his breath"}
  ],
  "image_prompts": [
    "inside a dark canvas tent at night, <dyatlov look>, raising a hand for silence, other hikers in sweaters frozen, lantern light",
    "close-up of <dyatlov look>, eyes wide, listening, frost on the tent wall behind him"
  ],
  "ambient": "storm",
  "visual_description": "hikers frozen in silence inside a tent at night",
  "search_query": "tent night snow"
}
```

- `narration` must contain every dialogue line word for word, in order (the attribution words
  around them are allowed).
- `speaker` is `narrator` or a cast `id`; ≤2 distinct speakers per scene.
- Image prompts: replace `<id look>` with the character's `look` text, word for word. The
  script-level `characters` field goes into every image with people in it, so put only the
  1–2 leads there and write the others inline.
- Show the speaker in the image while they speak: a close-up for an emotional line, a two-shot
  for an exchange.

## 5. Validate

```bash
python3 .claude/skills/dialogue-writer/scripts/check_dialogue.py scripts/<series>/<file>.json
```

It checks the cast, ≤2 voices per scene, line length, that narration contains every line, and
that no stage directions are inside the text. Then run the content-writer checker too (QA
preflight).
