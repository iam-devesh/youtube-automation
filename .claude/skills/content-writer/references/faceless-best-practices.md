# Faceless video best practices (learned 2026-11-19)

Sources studied: Krea "Faceless Video" skill (krea.ai/skills/faceless-video), Higgsfield
"faceless" skill (higgsfield.ai/skills/faceless) and its public SKILL.md "higgsfield-video-explainer",
plus a community Seedance "faceless channel" skill. Both services render with paid models
(Seedance 2.5, Gemini Omni Flash video), so we take their method, not their tools (free only).

## Their workflow, and where we stand

| Their step | What it means | Us |
|---|---|---|
| Lock the brief | topic, runtime, aspect, audience, narration path decided before any generation | ✅ script JSON + series.json |
| Style key first | ONE reusable style image/descriptor, attached to every scene | ⚠️ we add a style string; no style key image yet |
| Ordered blocks | story → timed blocks, one narration line ↔ one clip | ✅ scenes, voice parts |
| Retry only the failed block | passed blocks stay fixed | ✅ voice patch takes; ❌ no per-image style check yet |
| Real timestamps for captions | never guess word timings | ✅ Whisper alignment |
| Verify before delivery | pairing, counts, final inspection | ✅ qa checklist + every-word check |

## Rules adopted into SKILL.md

1. **Spell numbers out in narration** (Higgsfield rule). Digits caused two bugs: "17वीं सदी के भूकंप"
   was read as "सत्रवीं अर्थक्वेक", and "800" (spoken "आठ सौ") produced a misplaced cut.
   Write "आठ सौ साल", "बारह सौ तेरह में"; years can be words too. Captions show the words, which is fine.
2. **One clear action per scene.** Each scene shows one thing happening; the narration line is
   about that one thing.
3. **Image prompt fields**: SUBJECT (who/what, or "nobody around") · CAMERA (wide / close-up /
   low angle / aerial) · LIGHT (dawn, torchlight, overcast…) · ACTION (one visible thing happening).
   First prompt of a scene wide, the second closer.
4. **Shared style + negatives**: the same style descriptor in every image (done automatically
   by `ai_image.style_prompt`), and no realism / no text / no logos / no watermark.
5. **The screen never rests**: motion every second, layered depth (foreground faster than
   background). Ours: depth parallax + `ambient` weather + dissolves. Pick an `ambient` for
   every scene where it fits.
6. **Hook → payoff**: line 1 is the most surprising fact; the last line answers or reopens it.
   Never "is video mein…".

## Ideas not built yet (need a decision)

- **Style drift check per image**: score each generated image against the episode's style
  (local CLIP, or Gemini vision on the free tier) and regenerate only the off-style ones.
- **Style key image**: generate one reference image per series and condition every image on it
  (needs image-to-image / reference support in our local image model; to test).
- **New style presets** to test with Z-Image: Watercolor chronicle, Paper diorama, Editorial
  motion graphics, Pastel flat 2D, Fairy tale & myth (Mandir Ke Rahasya could use Paper diorama
  or Watercolor to stand out among 3D temple Shorts).
