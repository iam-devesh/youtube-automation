"""
check_dialogue.py: validate the dialogue of a script JSON (see the dialogue-writer skill).

    python3 .claude/skills/dialogue-writer/scripts/check_dialogue.py scripts/<series>/<file>.json [cast.json]

cast.json defaults to the one next to the script.
"""
import json
import os
import re
import sys

VOICES = {"Charon", "Fenrir", "Orus", "Algenib", "Puck", "Iapetus", "Umbriel", "Kore", "Aoede",
          "Leda", "Despina", "Sulafat", "Zephyr", "Enceladus", "Achernar", "Gacrux", "Schedar",
          "Rasalgethi", "Alnilam", "Sadaltager", "Achird", "Zubenelgenubi", "Vindemiatrix",
          "Sadachbia", "Pulcherrima", "Laomedeia", "Callirrhoe", "Autonoe", "Erinome", "Algieba"}


def norm(s):
    return re.sub(r"[^\w\s]", " ", s).split()


def contains_in_order(hay, needle):
    i = 0
    for w in hay:
        if i < len(needle) and w == needle[i]:
            i += 1
    return i == len(needle)


def main(path, cast_path=None):
    script = json.load(open(path, encoding="utf-8"))
    cast_path = cast_path or os.path.join(os.path.dirname(path), "cast.json")
    fails, notes = [], []

    cast = {}
    if os.path.exists(cast_path):
        c = json.load(open(cast_path, encoding="utf-8"))
        cast = {ch["id"]: ch for ch in c.get("characters", [])}
        voices = [c.get("narrator", {}).get("voice", "Charon")] + [ch.get("voice") for ch in cast.values()]
        for ch in cast.values():
            for k in ("id", "name", "voice", "look", "speech_habit"):
                if not ch.get(k):
                    fails.append(f"cast: {ch.get('id')} missing '{k}'")
            if ch.get("voice") and ch["voice"] not in VOICES:
                fails.append(f"cast: {ch['id']} voice '{ch['voice']}' is not a known Gemini voice")
            n = len((ch.get("look") or "").split())
            if not 20 <= n <= 45:
                notes.append(f"cast: {ch['id']} look is {n} words (aim 25–40)")
        dup = {v for v in voices if voices.count(v) > 1}
        if dup:
            fails.append(f"cast: voices used twice: {', '.join(dup)}")
    else:
        notes.append(f"no cast file at {cast_path}: speaker ids not checked")

    dialogue_scenes = spoken = 0
    for s in script.get("scenes", []):
        sn, d = s.get("scene_number"), s.get("dialogue")
        if not d:
            continue
        dialogue_scenes += 1
        speakers = {l.get("speaker") for l in d}
        if len(speakers) > 2:
            fails.append(f"scene {sn}: {len(speakers)} voices (max 2, narrator counts)")
        for l in d:
            sp, text = l.get("speaker"), (l.get("text") or "").strip()
            if sp != "narrator":
                spoken += 1
                if cast and sp not in cast:
                    fails.append(f"scene {sn}: speaker '{sp}' not in cast")
            n = len(text.split())
            if not 1 <= n <= 14:
                fails.append(f"scene {sn}: line by {sp} is {n} words (1–14)")
            if re.search(r"[\(\[\{]", text):
                fails.append(f"scene {sn}: brackets in a line; put stage directions in 'direction'")
        if not contains_in_order(norm(s.get("narration") or ""), norm(" ".join(l.get("text", "") for l in d))):
            fails.append(f"scene {sn}: narration doesn't contain every dialogue line in order")

    total = len(script.get("scenes", []))
    print(f"{dialogue_scenes}/{total} scenes with dialogue ({dialogue_scenes / max(total, 1) * 100:.0f}%), "
          f"{spoken} character lines")
    for n in notes:
        print("  note:", n)
    for f in fails:
        print("  ☒", f)
    print("DIALOGUE OK ✅" if not fails else f"NOT READY ☒ ({len(fails)} problem(s))")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3]))
