"""
pipeline/music.py
Background music per template.

Source: Kevin MacLeod / incompetech.com — licensed CC-BY 4.0, which allows commercial use
(monetized YouTube) as long as the credit line is in the description. The credit is
returned with the track and appended to the video description automatically.

Your own track at assets/music/<template>.mp3 (or .m4a/.wav) always takes priority.
"""

import os
import urllib.parse
import zlib

import requests

ASSETS = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "music")
LIBRARY = os.path.join(ASSETS, "library")
BASE_URL = "https://incompetech.com/music/royalty-free/mp3-royaltyfree/"

TRACKS = {
    "mystery": ["Dark Fog", "Ghost Story", "Echoes of Time v2", "Night Vigil",
                "Investigations", "Lost Time", "Long Note Four"],
    "story":   ["Inspired", "Heartwarming", "Discovery Hit", "Crossing the Chasm", "Wholesome"],
    "facts":   ["Funkorama", "Pixel Peeker Polka - faster", "Thinking Music",
                "Sneaky Snitch", "Volatile Reaction"],
}


def credit_for(title):
    return (f'Music: "{title}" Kevin MacLeod (incompetech.com)\n'
            f"Licensed under Creative Commons: By Attribution 4.0 License\n"
            f"http://creativecommons.org/licenses/by/4.0/")


def _download(title):
    os.makedirs(LIBRARY, exist_ok=True)
    path = os.path.join(LIBRARY, f"{title}.mp3")
    if os.path.exists(path) and os.path.getsize(path) > 100_000:
        return path
    r = requests.get(BASE_URL + urllib.parse.quote(title) + ".mp3", timeout=60)
    if r.status_code != 200 or len(r.content) < 100_000:
        raise RuntimeError(f"download failed ({r.status_code})")
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(r.content)
    os.replace(tmp, path)
    return path


def track_for(template: str, key: str = "", log=print):
    """Return (path, credit_or_None), or (None, None) if no music is available."""
    for ext in ("mp3", "m4a", "wav"):
        own = os.path.join(ASSETS, f"{template}.{ext}")
        if os.path.exists(own):
            return own, None
    titles = TRACKS.get(template) or TRACKS["mystery"]
    start = zlib.crc32(key.encode("utf-8")) % len(titles)    # stable choice per video
    for k in range(len(titles)):
        title = titles[(start + k) % len(titles)]
        try:
            path = _download(title)
            log(f"  🎵 Music: \"{title}\" — Kevin MacLeod (CC-BY 4.0)")
            return path, credit_for(title)
        except Exception as e:
            log(f"  ⚠️  Music track \"{title}\" unavailable: {e}")
    return None, None
