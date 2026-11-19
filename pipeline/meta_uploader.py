"""
pipeline/meta_uploader.py: post videos to the Facebook Page and the linked Instagram account
through Meta's official Graph API (no browser automation).

  Facebook  · Shorts → Page Reels (video_reels: start → upload to rupload → finish)
            · long videos → Page videos (resumable: start → transfer chunks → finish)
  Instagram · Reels (resumable upload straight from this Mac → wait for processing → publish)

Credentials live in .env (never committed): META_PAGE_ID, META_PAGE_TOKEN (never expires),
META_IG_USER_ID, META_ACCESS_TOKEN (long-lived user token, a fallback for Instagram).
Posting is pinned to META_PAGE_ID ("The Faceless Hindi"); the account manages other Pages too.

Results are kept per job in <job>/meta_result.json, so nothing is ever posted twice.
"""
import json
import os
import re
import time

import requests

GRAPH = "https://graph.facebook.com/v21.0"
GRAPH_VIDEO = "https://graph-video.facebook.com/v21.0"
CHUNK = 8 * 1024 * 1024
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _env():
    try:
        from dotenv import dotenv_values
        env = dotenv_values(os.path.join(ROOT, ".env"))
    except ImportError:
        env = {}
    env = {**env, **{k: v for k, v in os.environ.items() if k.startswith("META_")}}
    need = ["META_PAGE_ID", "META_PAGE_TOKEN", "META_IG_USER_ID"]
    missing = [k for k in need if not env.get(k)]
    if missing:
        raise RuntimeError(f"Meta credentials missing in .env: {', '.join(missing)}")
    return env


def _check(r):
    try:
        d = r.json()
    except ValueError:
        r.raise_for_status()
        return {}
    if r.status_code >= 400 or (isinstance(d, dict) and "error" in d):
        err = d.get("error", d) if isinstance(d, dict) else d
        raise RuntimeError(f"Meta API {r.status_code}: {json.dumps(err, ensure_ascii=False)[:400]}")
    return d


# ─── Captions ─────────────────────────────────────────────────────────────────

def _hashtags(text):
    return re.findall(r"#\w+", text or "")


def _credit(desc):
    """The music licence lines (CC-BY needs the attribution on every platform)."""
    m = re.search(r"Music:.*", desc or "", re.S)
    return m.group(0).strip() if m else ""


def build_caption(meta, platform, youtube_url=None):
    title = (meta.get("title") or "").strip()
    desc = (meta.get("description") or "").strip()
    body = re.split(r"\n\s*(?:#|Music:|Chapters|⏱|00:00)", desc)[0].strip()   # the story lines only
    series = meta.get("series") or ""
    base_tags = _hashtags(desc) + ["#TheFacelessHindi", "#HindiStories", "#Rahasya", "#Mystery"]
    if "bhoot" in series.lower() or "#BhootiyaBharat" in desc:
        base_tags += ["#BhootiyaBharat", "#HauntedIndia", "#HauntedPlaces", "#IndianMystery"]
    else:
        base_tags += ["#AnsuljheRahasya", "#UnsolvedMystery", "#Mysteries"]
    tags, seen = [], set()
    for t in base_tags:
        if t.lower() not in seen and t.lower() != "#shorts":
            seen.add(t.lower())
            tags.append(t)
    credit = _credit(desc)
    if platform == "instagram":
        tags = tags[:15]                      # Instagram allows 30; fewer, focused tags read better
        lines = [title, "", body, "", "▶️ Poori kahani YouTube pe: link bio mein 🔗", "", " ".join(tags)]
    else:
        lines = [title, "", body]
        if youtube_url:
            lines += ["", f"▶️ YouTube pe dekhein: {youtube_url}"]
        lines += ["", " ".join(tags)]
    if credit:
        lines += ["", credit]
    return "\n".join(lines).strip()


# ─── Facebook ─────────────────────────────────────────────────────────────────

def fb_reel(path, description, log=print):
    """Publish a vertical video (≤ 90 s) as a Reel on the Page. Returns {id, url}."""
    env = _env()
    page, tok = env["META_PAGE_ID"], env["META_PAGE_TOKEN"]
    start = _check(requests.post(f"{GRAPH}/{page}/video_reels",
                                 data={"upload_phase": "start", "access_token": tok}, timeout=60))
    vid = start["video_id"]
    size = os.path.getsize(path)
    log(f"  📘 Facebook Reel: uploading {size / 1e6:.1f} MB...")
    with open(path, "rb") as f:
        _check(requests.post(start["upload_url"], data=f, timeout=600, headers={
            "Authorization": f"OAuth {tok}", "offset": "0", "file_size": str(size)}))
    _check(requests.post(f"{GRAPH}/{page}/video_reels", timeout=60, data={
        "upload_phase": "finish", "video_id": vid, "video_state": "PUBLISHED",
        "description": description, "access_token": tok}))
    url = _wait_fb_video(vid, tok, log)
    return {"id": vid, "url": url}


def fb_video(path, title, description, log=print):
    """Publish a long (landscape) video on the Page with a resumable chunked upload."""
    env = _env()
    page, tok = env["META_PAGE_ID"], env["META_PAGE_TOKEN"]
    size = os.path.getsize(path)
    s = _check(requests.post(f"{GRAPH_VIDEO}/{page}/videos", timeout=60, data={
        "upload_phase": "start", "file_size": size, "access_token": tok}))
    sess, vid = s["upload_session_id"], s["video_id"]
    a, b = int(s["start_offset"]), int(s["end_offset"])
    with open(path, "rb") as f:
        while a < b:
            f.seek(a)
            chunk = f.read(b - a)
            log(f"  📘 Facebook video: {a / 1e6:.0f}/{size / 1e6:.0f} MB")
            r = _check(requests.post(f"{GRAPH_VIDEO}/{page}/videos", timeout=600, data={
                "upload_phase": "transfer", "upload_session_id": sess, "start_offset": a,
                "access_token": tok}, files={"video_file_chunk": ("chunk", chunk)}))
            a, b = int(r["start_offset"]), int(r["end_offset"])
    _check(requests.post(f"{GRAPH_VIDEO}/{page}/videos", timeout=120, data={
        "upload_phase": "finish", "upload_session_id": sess, "title": title,
        "description": description, "published": "true", "access_token": tok}))
    url = _wait_fb_video(vid, tok, log)
    return {"id": vid, "url": url}


def _wait_fb_video(vid, tok, log, limit=900):
    t0 = time.time()
    while time.time() - t0 < limit:
        d = _check(requests.get(f"{GRAPH}/{vid}", timeout=60,
                                params={"fields": "status,permalink_url", "access_token": tok}))
        st = (d.get("status") or {}).get("video_status")
        if st == "ready":
            link = d.get("permalink_url") or ""
            return ("https://www.facebook.com" + link) if link.startswith("/") else link
        if st == "error":
            raise RuntimeError(f"Facebook could not process the video: {d.get('status')}")
        time.sleep(10)
    log("  ⏳ Facebook still processing — it will appear on the Page when done")
    return f"https://www.facebook.com/{vid}"


# ─── Instagram ────────────────────────────────────────────────────────────────

def ig_reel(path, caption, cover_offset_ms=None, log=print):
    """Publish a Reel on the linked Instagram account. Returns {id, url}."""
    env = _env()
    ig = env["META_IG_USER_ID"]
    last = None
    for tok in [t for t in (env.get("META_PAGE_TOKEN"), env.get("META_ACCESS_TOKEN")) if t]:
        try:
            return _ig_reel(ig, tok, path, caption, cover_offset_ms, log)
        except RuntimeError as e:
            if "OAuthException" not in str(e) and "permission" not in str(e).lower():
                raise
            last = e                          # this token can't do it → try the user token
    raise last


def _ig_reel(ig, tok, path, caption, cover_offset_ms, log):
    data = {"media_type": "REELS", "upload_type": "resumable", "caption": caption,
            "share_to_feed": "true", "access_token": tok}
    if cover_offset_ms is not None:
        data["thumb_offset"] = int(cover_offset_ms)
    c = _check(requests.post(f"{GRAPH}/{ig}/media", data=data, timeout=60))
    cid = c["id"]
    size = os.path.getsize(path)
    log(f"  📸 Instagram Reel: uploading {size / 1e6:.1f} MB...")
    with open(path, "rb") as f:
        _check(requests.post(c.get("uri") or f"https://rupload.facebook.com/ig-api-upload/v21.0/{cid}",
                             data=f, timeout=600, headers={
                                 "Authorization": f"OAuth {tok}", "offset": "0", "file_size": str(size)}))
    t0 = time.time()
    while True:
        d = _check(requests.get(f"{GRAPH}/{cid}", timeout=60,
                                params={"fields": "status_code,status", "access_token": tok}))
        if d.get("status_code") == "FINISHED":
            break
        if d.get("status_code") in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram could not process the video: {d.get('status')}")
        if time.time() - t0 > 900:
            raise RuntimeError("Instagram processing timed out")
        time.sleep(8)
    m = _check(requests.post(f"{GRAPH}/{ig}/media_publish", timeout=60,
                             data={"creation_id": cid, "access_token": tok}))
    link = _check(requests.get(f"{GRAPH}/{m['id']}", timeout=60,
                               params={"fields": "permalink", "access_token": tok})).get("permalink")
    return {"id": m["id"], "url": link}


# ─── One job → both platforms ────────────────────────────────────────────────

def post_job(job_dir, platforms=("facebook", "instagram"), video=None, log=print):
    """Post a finished job (YouTube metadata in upload_meta.json) to Facebook and Instagram.
    Skips platforms already done (meta_result.json). `video` overrides the file to post."""
    meta = json.load(open(os.path.join(job_dir, "upload_meta.json"), encoding="utf-8"))
    yt = {}
    if os.path.exists(os.path.join(job_dir, "upload_result.json")):
        yt = json.load(open(os.path.join(job_dir, "upload_result.json")))
    yt_url = yt.get("video_url") or (f"https://www.youtube.com/watch?v={yt['video_id']}" if yt.get("video_id") else None)
    res_path = os.path.join(job_dir, "meta_result.json")
    res = json.load(open(res_path)) if os.path.exists(res_path) else {}
    if not video:
        src = os.path.join(job_dir, "social_source.mp4")
        video = src if os.path.exists(src) else next(
            os.path.join(job_dir, f) for f in sorted(os.listdir(job_dir))
            if f.endswith(".mp4") and not f.startswith("_"))
    short = meta.get("video_type", "short") == "short"
    for p in platforms:
        if res.get(p, {}).get("id"):
            log(f"  ⏭️  {p}: already posted → {res[p].get('url')}")
            continue
        cap = build_caption(meta, p, yt_url)
        if p == "facebook":
            out = fb_reel(video, cap, log) if short else fb_video(video, meta.get("title", ""), cap, log)
        else:
            out = ig_reel(video, cap, log=log)
        out["time"] = time.strftime("%Y-%m-%d %H:%M")
        res[p] = out
        json.dump(res, open(res_path, "w"), ensure_ascii=False, indent=1)
        log(f"  ✅ {p}: {out['url']}")
    return res


# ─── Daily queue (one post a day, 7 PM IST) ──────────────────────────────────
# static/outputs/.social_queue.json:
#   {"time": "19:00", "items": [{"job": "<job folder>", "platforms": [...], "video": optional}],
#    "last_day": "YYYY-MM-DD", "log": [...]}
# The server's dispatcher calls run_due() every few seconds; it posts at most one item a day.

QUEUE = os.path.join(ROOT, "static", "outputs", ".social_queue.json")
_busy = [False]


def _load_queue():
    try:
        return json.load(open(QUEUE, encoding="utf-8"))
    except Exception:
        return {"time": "19:00", "items": [], "log": []}


def _save_queue(q):
    json.dump(q, open(QUEUE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def enqueue(job, platforms=("facebook", "instagram"), video=None):
    q = _load_queue()
    if not any(i["job"] == job and i.get("video") == video for i in q["items"]):
        q["items"].append({"job": job, "platforms": list(platforms), **({"video": video} if video else {})})
        _save_queue(q)
    return q


def run_due(log=print):
    """Post the next queued item if it's past today's posting time (local clock = IST) and
    nothing was posted today. Runs the upload in a thread; returns True if one started."""
    if _busy[0]:
        return False
    q = _load_queue()
    if not q.get("items"):
        return False
    today = time.strftime("%Y-%m-%d")
    if q.get("last_day") == today or time.strftime("%H:%M") < q.get("time", "19:00"):
        return False
    item = q["items"][0]
    _busy[0] = True
    q["last_day"] = today                      # claim today before posting: never two in a day
    _save_queue(q)

    def work():
        try:
            job_dir = os.path.join(ROOT, "static", "outputs", item["job"])
            video = os.path.join(job_dir, item["video"]) if item.get("video") else None
            res = post_job(job_dir, item.get("platforms") or ("facebook", "instagram"), video, log)
            entry = {"day": today, "job": item["job"], "ok": True,
                     "urls": {k: v.get("url") for k, v in res.items() if isinstance(v, dict)}}
        except Exception as e:
            entry = {"day": today, "job": item["job"], "ok": False, "error": str(e)[:300]}
            log(f"  ❌ Social post failed for {item['job']}: {e}")
        q2 = _load_queue()
        if entry["ok"]:
            q2["items"] = [i for i in q2["items"] if i != item]
        q2.setdefault("log", []).append(entry)
        _save_queue(q2)
        _busy[0] = False

    import threading
    threading.Thread(target=work, daemon=True).start()
    return True


if __name__ == "__main__":
    import sys
    print(json.dumps(post_job(sys.argv[1], sys.argv[2:] or ("facebook", "instagram")),
                     ensure_ascii=False, indent=1))
