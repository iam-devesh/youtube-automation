"""Views per video on YouTube, Facebook and Instagram.

Finds every job with an upload_result.json (YouTube) or meta_result.json (Facebook/Instagram)
and asks each platform for its numbers. Results are cached for CACHE_MIN minutes in
static/outputs/.views_cache.json so the app never waits on three APIs every page load.

Facebook Reel plays and Instagram views need two extra Meta permissions (read_insights,
instagram_manage_insights). Without them we still show what the Page token can read
(Facebook's `views` field, likes and comments) and report the missing permission.
"""
import json
import os
import time
from datetime import datetime

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "static", "outputs")
CACHE = os.path.join(OUT, ".views_cache.json")
CACHE_MIN = 15


def _load(p):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return None


def _videos():
    """[{job, title, youtube:{id,url}, facebook:{id,url}, instagram:{id,url}, posted}]"""
    out = []
    for job in sorted(os.listdir(OUT)):
        d = os.path.join(OUT, job)
        if not os.path.isdir(d):
            continue
        yt = _load(os.path.join(d, "upload_result.json")) or {}
        mr = _load(os.path.join(d, "meta_result.json")) or {}
        if not yt.get("video_id") and not mr:
            continue
        meta = _load(os.path.join(d, "upload_meta.json")) or {}
        stamp = os.path.join(d, "upload_result.json")
        out.append({
            "job": job,
            "title": yt.get("title") or meta.get("title") or job,
            "video_type": meta.get("video_type", "short"),
            "posted": datetime.fromtimestamp(os.path.getmtime(stamp)).isoformat() if os.path.exists(stamp) else None,
            "youtube": {"id": yt["video_id"], "url": yt.get("video_url")} if yt.get("video_id") else None,
            "facebook": {"id": mr["facebook"]["id"], "url": mr["facebook"].get("url")} if mr.get("facebook") else None,
            "instagram": {"id": mr["instagram"]["id"], "url": mr["instagram"].get("url")} if mr.get("instagram") else None,
        })
    return out


def _youtube(ids):
    """{video_id: {views, likes, comments}} in batches of 50."""
    from pipeline.youtube_uploader import get_authenticated_service
    yt = get_authenticated_service()
    res = {}
    for i in range(0, len(ids), 50):
        r = yt.videos().list(part="statistics", id=",".join(ids[i:i + 50])).execute()
        for it in r.get("items", []):
            s = it.get("statistics", {})
            res[it["id"]] = {"views": int(s.get("viewCount", 0)), "likes": int(s.get("likeCount", 0)),
                             "comments": int(s.get("commentCount", 0))}
    return res


def _get(url, **params):
    r = requests.get(url, params=params, timeout=20)
    try:
        d = r.json()
    except ValueError:
        d = {}
    return r.status_code, d


def _facebook(vid, tok, graph, missing):
    st, d = _get(f"{graph}/{vid}", access_token=tok,
                 fields="views,likes.summary(true).limit(0),comments.summary(true).limit(0)")
    if st >= 400:
        raise RuntimeError((d.get("error") or {}).get("message", f"HTTP {st}"))
    out = {"views": d.get("views", 0),
           "likes": ((d.get("likes") or {}).get("summary") or {}).get("total_count", 0),
           "comments": ((d.get("comments") or {}).get("summary") or {}).get("total_count", 0)}
    # Reel plays (the number Facebook shows on the reel) need read_insights
    st, d = _get(f"{graph}/{vid}/video_insights", access_token=tok,
                 metric="blue_reels_play_count,fb_reels_total_plays,total_video_views")
    if st < 400:
        vals = {m["name"]: (m.get("values") or [{}])[0].get("value", 0) for m in d.get("data", [])}
        plays = max([v for v in vals.values() if isinstance(v, (int, float))] or [0])
        out["views"] = max(out["views"], int(plays))
    else:
        missing.add("read_insights")
        out["partial"] = True
        if not out["views"]:
            out["views"] = None          # the basic field doesn't count Reel plays: 0 would be misleading
    return out


def _instagram(mid, tok, graph, missing):
    st, d = _get(f"{graph}/{mid}", access_token=tok, fields="like_count,comments_count")
    if st >= 400:
        raise RuntimeError((d.get("error") or {}).get("message", f"HTTP {st}"))
    out = {"views": None, "likes": d.get("like_count", 0), "comments": d.get("comments_count", 0)}
    for metric in ("views", "plays", "ig_reels_aggregated_all_plays_count"):
        st, d = _get(f"{graph}/{mid}/insights", access_token=tok, metric=metric)
        if st < 400:
            vals = d.get("data") or [{}]
            out["views"] = int(((vals[0].get("values") or [{}])[0]).get("value", 0) or vals[0].get("total_value", {}).get("value", 0))
            return out
        code = (d.get("error") or {}).get("code")
        if code in (10, 200, 190):           # permission problem: other metrics won't help
            break
    missing.add("instagram_manage_insights")
    out["partial"] = True
    return out


def collect(log=print):
    vids = _videos()
    errors, missing = [], set()
    ids = [v["youtube"]["id"] for v in vids if v["youtube"]]
    yts = {}
    if ids:
        try:
            yts = _youtube(ids)
        except Exception as e:
            errors.append(f"YouTube: {e}")
    tok = graph = None
    if any(v["facebook"] or v["instagram"] for v in vids):
        try:
            from pipeline.meta_uploader import _env, GRAPH
            tok, graph = _env()["META_PAGE_TOKEN"], GRAPH
        except Exception as e:
            errors.append(f"Meta: {e}")
    for v in vids:
        if v["youtube"]:
            v["youtube"].update(yts.get(v["youtube"]["id"], {}))
        for name, fn in (("facebook", _facebook), ("instagram", _instagram)):
            if v[name] and tok:
                try:
                    v[name].update(fn(v[name]["id"], tok, graph, missing))
                except Exception as e:
                    v[name]["error"] = str(e)[:200]
                    errors.append(f"{name} {v['job']}: {str(e)[:120]}")
        v["total"] = sum((v[p] or {}).get("views") or 0 for p in ("youtube", "facebook", "instagram"))
    vids.sort(key=lambda v: v["posted"] or "", reverse=True)
    totals = {p: sum((v[p] or {}).get("views") or 0 for v in vids) for p in ("youtube", "facebook", "instagram")}
    totals["all"] = sum(totals.values())
    data = {"updated": datetime.now().isoformat(timespec="seconds"), "videos": vids, "totals": totals,
            "missing_permissions": sorted(missing), "errors": errors}
    try:
        json.dump(data, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    except Exception:
        pass
    return data


def get(force=False):
    if not force and os.path.exists(CACHE) and time.time() - os.path.getmtime(CACHE) < CACHE_MIN * 60:
        d = _load(CACHE)
        if d:
            return d
    return collect()


if __name__ == "__main__":
    print(json.dumps(collect(), ensure_ascii=False, indent=1))
