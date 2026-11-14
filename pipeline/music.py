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


PLAYLIST_FROM = 150          # videos longer than this (s) get several tracks instead of a loop
CROSSFADE = 4.0


def playlist_for(template: str, key: str, total: float, out_dir: str, log=print):
    """Long videos: chain different tracks of the template's mood with crossfades until the
    video is covered, so 10 minutes don't loop one 2-minute piece. Returns (path, credits) like
    track_for; short videos (or your own track in assets/music/) get track_for unchanged."""
    if total <= PLAYLIST_FROM or any(os.path.exists(os.path.join(ASSETS, f"{template}.{e}"))
                                     for e in ("mp3", "m4a", "wav")):
        return track_for(template, key, log)
    import subprocess
    from mutagen.mp3 import MP3
    try:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        ffmpeg = "ffmpeg"
    titles = TRACKS.get(template) or TRACKS["mystery"]
    start = zlib.crc32(key.encode("utf-8")) % len(titles)
    chosen, length = [], 0.0
    for k in range(len(titles)):
        title = titles[(start + k) % len(titles)]
        try:
            path = _download(title)
            d = MP3(path).info.length
        except Exception as e:
            log(f"  ⚠️  Music track \"{title}\" unavailable: {e}")
            continue
        if d < 30:
            continue
        chosen.append((title, path))
        length += d - (CROSSFADE if len(chosen) > 1 else 0)
        if length >= total + 5:
            break
    if len(chosen) < 2:
        return track_for(template, key, log)
    out = os.path.join(out_dir, "_music_playlist.mp3")
    n = len(chosen)
    args = [ffmpeg, "-y", "-v", "error"]
    for _, p in chosen:
        args += ["-i", p]
    chain, last = [], "[0:a]"
    for i in range(1, n):
        tag = f"[x{i}]" if i < n - 1 else "[out]"
        chain.append(f"{last}[{i}:a]acrossfade=d={CROSSFADE}:c1=tri:c2=tri{tag}")
        last = tag
    args += ["-filter_complex", ";".join(chain), "-map", "[out]", "-ar", "48000", "-b:a", "192k", out]
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode:
        log(f"  ⚠️  Music playlist failed ({r.stderr[-200:]}); using one track")
        return track_for(template, key, log)
    log(f"  🎵 Music: {n} tracks crossfaded ({', '.join(t for t, _ in chosen)}), "
        f"{length / 60:.1f} min{' (loops)' if length < total else ''} — Kevin MacLeod (CC-BY 4.0)")
    return out, "\n\n".join(credit_for(t) for t, _ in chosen)
