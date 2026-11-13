"""
pipeline/script_generator.py
Generates a structured video script using Google Gemini API.
"""

import json
import re
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

try:
    from google import genai
    GENAI_SDK = "new"
except ImportError:
    try:
        import google.generativeai as genai_legacy
        GENAI_SDK = "legacy"
    except ImportError:
        GENAI_SDK = None

try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

from config import GEMINI_API_KEY, NVIDIA_API_KEY, NVIDIA_TEXT_MODEL, GEMINI_TEXT_MODEL


def generate_script(topic: str, video_type: str = "long", style: str = "animated_3d") -> dict:
    """
    Generate a structured video script for the given topic.

    Args:
        topic: The video topic (e.g. "History of Ancient Rome")
        video_type: "long" (5-8 min) or "short" (60 sec)

    Returns:
        dict with keys: title, description, tags, scenes
        Each scene has: narration, visual_description, duration_hint
    """

    if video_type == "short":
        prompt = f"""
You write viral YouTube Shorts / Instagram Reels that get millions of views.
Write a script about:

TOPIC: {topic}

VIRAL STRUCTURE (follow it exactly):
1. HOOK (scene 1, first 2 seconds): a shocking claim, question or "you won't believe" statement. No greeting, no intro, no "aaj hum baat karenge".
2. CONTEXT (1 scene): the minimum the viewer needs, fast.
3. ESCALATION (3-4 scenes): each scene reveals something MORE surprising than the last. Use open loops ("लेकिन असली राज़ अभी बाकी है...").
4. TWIST / PAYOFF (1 scene): the most surprising fact.
5. LOOP ENDING (last scene): a line that makes people rewatch or comment (a question to the viewer, or a sentence that flows back into the hook).

HINDI NARRATION — how it must sound:
- Spoken, everyday Hindi, like a gripping storyteller talking to a friend at night. Not a news reader, not a textbook.
- Short sentences (max ~10 words each). Use "..." for dramatic pauses and rhetorical questions ("सोचिए...", "पर क्यों?").
- Simple words people actually say: "लोग" not "जनता", "अचानक" not "अकस्मात", "ख़त्म" not "समाप्त", "पता नहीं" not "अज्ञात".
  Common English words are fine IN DEVANAGARI (ट्रेन, साइक्लोन, रेडियो) — never in Latin letters.
- Write years/numbers with normal digits (1964, 100), never Devanagari digits.
- Address the viewer as "आप". Build tension scene to scene; never repeat information.
- Only state real, verifiable facts as facts; legends must be framed as "कहते हैं..." / "मान्यता है...".

LANGUAGE:
- `narration` and `hook_text` MUST be in Hindi (Devanagari script).
- `visual_description`, `search_query`, `image_prompts` MUST be in English.
- AUDIENCE IS HINGLISH (Indians who read Hindi in Roman letters). So `title`, `description`
  and `tags` are in HINGLISH, Roman script — never Devanagari:
  title e.g. "Raaton-Raat Gayab Hua Pura Shehar 😱 | Dhanushkodi"
  tags mix what people actually TYPE in search: English + Hinglish
  (e.g. "dhanushkodi mystery", "bhutiya shahar", "haunted places in india", "rahasya")

Return ONLY valid JSON (no markdown, no code blocks) in this exact structure:

{{
  "title": "Hinglish curiosity-gap title in Roman script, max 60 chars, max 1 emoji, put the keyword (place/name) in it",
  "description": "2 short Hinglish lines (Roman script) that tease the story without spoiling it, then 3-5 relevant hashtags",
  "tags": ["12-15 search terms, English + Hinglish, lowercase, each under 30 chars"],
  "hook_text": "On-screen hook banner in Hindi, MAX 5 words (e.g. 'यहाँ जहाज़ गायब होते हैं!')",
  "setting": "Short English place + region + era added to EVERY image (e.g. 'Dhanushkodi, Tamil Nadu, South India, 1960s'). Be specific about the country/region so people and buildings look right.",
  "characters": "ONLY if the story is about a specific real person or a named protagonist: an English visual description reused in every image so they look identical. Otherwise an EMPTY string — never invent a narrator/guide character.",
  "scenes": [
    {{
      "scene_number": 1,
      "narration": "Punchy Hindi narration, 8-14 words",
      "image_prompts": ["2 English shot descriptions for an ANIMATED MOVIE frame that literally show what THIS narration line is about (the place, object or event — not a random person): first a wide establishing shot, then a closer dramatic shot. Show people only when the line is about people, and describe them to match the region and era (e.g. for Tamil Nadu: South Indian Tamil people, dark brown skin, white dhoti, lungi, saris; thatched huts, palm trees). Describe real landmarks accurately. No style words (style is added automatically)."],
      "ambient": "one of: rain | storm | fog | dust | embers | none — moving weather/particles over this shot",
      "visual_description": "One concrete, literal, filmable shot in English: subject, setting, action (e.g. 'huge storm waves crashing over a small cargo ship at night'). No camera jargon.",
      "search_query": "2-3 plain English words that find this on a stock-footage site (e.g. 'stormy ocean ship')",
      "duration_hint": 4
    }}
  ]
}}

Rules:
- 7-9 scenes, each narration 8-14 words; total ~35-50 seconds spoken
- Scene 1 narration IS the hook
- Every scene must show something visually DIFFERENT; visual_description and search_query must describe real, photographable things (places, objects, people, nature), never abstract ideas
"""
    else:
        prompt = f"""
You are an expert YouTube scriptwriter. Create a highly engaging script for a standard YouTube video (3-5 minutes) about:

TOPIC: {topic}

LANGUAGE INSTRUCTIONS:
- The `narration` MUST be in Hindi (written in devanagari script, e.g., 'प्राचीन मिस्र...').
- `visual_description` MUST be in English. `title`, `description` and `tags` are HINGLISH in
  Roman script (audience reads Hindi in Roman letters), tags mixing English + Hinglish search terms.

Return ONLY valid JSON (no markdown, no code blocks) in this exact structure:

{{
  "title": "Compelling YouTube video title (max 70 chars)",
  "description": "Full YouTube description with keywords and hashtags (400-500 chars)",
  "tags": ["12-15 search terms, English + Hinglish, lowercase"],
  "hook": "Opening hook (first 10 seconds — must grab attention)",
  "scenes": [
    {{
      "scene_number": 1,
      "narration": "The full narration text for this scene (2-4 sentences)",
      "visual_description": "One concrete, literal, filmable shot in English: the subject, setting and action. No camera jargon.",
      "search_query": "2-3 plain English words that would find this on a stock-footage site",
      "duration_hint": 30
    }}
  ]
}}

Rules:
- 8-12 scenes
- Each scene narration: 2-4 sentences (30-45 seconds when spoken)
- Total video duration: 5-8 minutes
- Include intro, main content (facts, stories, key points), and conclusion/CTA
- End with a call-to-action to like, subscribe, and comment
- Make it educational, engaging, and authoritative
- Every scene must show something DIFFERENT; visual_description and search_query must describe real, photographable things, never abstract ideas
"""

    raw = ""
    errors = []
    # 1) Google Gemini — fast and reliable on the free tier
    if GENAI_SDK == "new" and GEMINI_API_KEY and not GEMINI_API_KEY.startswith("YOUR_"):
        try:
            print(f"  🎬 Using Google Gemini ({GEMINI_TEXT_MODEL}) for script generation...")
            client = genai.Client(api_key=GEMINI_API_KEY)
            response = client.models.generate_content(model=GEMINI_TEXT_MODEL, contents=prompt)
            raw = (response.text or "").strip()
        except Exception as e:
            errors.append(f"Gemini ({GEMINI_TEXT_MODEL}): {str(e)[:200]}")
            print(f"  ⚠️  Gemini script generation failed, trying NVIDIA: {str(e)[:150]}")
    # 2) NVIDIA NIM fallback (some of its models are slow reasoning models — capped at 90 s)
    if not raw and NVIDIA_API_KEY and NVIDIA_API_KEY != "YOUR_NVIDIA_API_KEY_HERE" and OPENAI_AVAILABLE:
        try:
            print(f"  🎬 Using NVIDIA NIM ({NVIDIA_TEXT_MODEL}) for script generation...")
            client = OpenAI(base_url="https://integrate.api.nvidia.com/v1", api_key=NVIDIA_API_KEY,
                            timeout=90, max_retries=0)
            response = client.chat.completions.create(
                model=NVIDIA_TEXT_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.8,
                max_tokens=4000,
            )
            raw = (response.choices[0].message.content or "").strip()
        except Exception as e:
            errors.append(f"NVIDIA ({NVIDIA_TEXT_MODEL}): {str(e)[:200]}")
    if not raw and GENAI_SDK == "legacy":
        genai_legacy.configure(api_key=GEMINI_API_KEY)
        raw = genai_legacy.GenerativeModel("gemini-1.5-flash").generate_content(prompt).text.strip()
    if not raw:
        raise RuntimeError("Script generation failed — " + " | ".join(errors or ["no AI SDK installed"]))

    # Keep only the JSON object (models sometimes add prose around it)
    if "{" in raw and "}" in raw:
        raw = raw[raw.index("{"): raw.rindex("}") + 1]

    # Strip markdown code fences if present
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"\s*```$", "", raw, flags=re.MULTILINE)

    script = json.loads(raw)
    script["video_type"] = video_type
    script["topic"] = topic
    return script


if __name__ == "__main__":
    # Quick test
    result = generate_script("The Fall of the Roman Empire", "long")
    print(json.dumps(result, indent=2))
