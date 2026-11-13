"""
pipeline/youtube_uploader.py
Uploads the final video to YouTube using the YouTube Data API v3.
Handles OAuth 2.0 authentication with token caching.
"""

import os
import sys
import json
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import CLIENT_SECRETS_FILE, YOUTUBE_SCOPES, OAUTH_TOKEN_FILE, CHANNEL_HASHTAGS, CHANNEL_TAGS

try:
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    GOOGLE_API_AVAILABLE = True
except ImportError:
    GOOGLE_API_AVAILABLE = False
    print("⚠️  Google API libraries not installed. Run: pip install google-api-python-client google-auth-oauthlib")


def get_authenticated_service():
    """
    Authenticate with YouTube Data API v3 using OAuth 2.0.
    Caches token in token.json for subsequent runs.

    Returns:
        Authenticated YouTube API service object
    """
    if not GOOGLE_API_AVAILABLE:
        raise RuntimeError("Google API libraries not available.")

    creds = None

    # Load cached token if available
    if os.path.exists(OAUTH_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(OAUTH_TOKEN_FILE, YOUTUBE_SCOPES)

    # A token saved before new scopes were added can't do the new things (e.g. playlists):
    # ask for consent again instead of failing later with "insufficient permissions".
    # (Read the GRANTED scopes from the token file — creds.scopes just echoes what we asked for.)
    if creds:
        try:
            granted = set(json.load(open(OAUTH_TOKEN_FILE)).get("scopes") or [])
        except Exception:
            granted = set()
        if not set(YOUTUBE_SCOPES) <= granted:
            print("  🔑 YouTube needs extra permission (playlists) — opening Google consent in the browser...")
            creds = None

    # Refresh or re-authenticate if needed
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CLIENT_SECRETS_FILE):
                raise FileNotFoundError(
                    f"client_secrets.json not found at '{CLIENT_SECRETS_FILE}'.\n"
                    "Please download it from Google Cloud Console:\n"
                    "  APIs & Services → Credentials → OAuth 2.0 Client IDs → Download JSON\n"
                    "Rename to 'client_secrets.json' and place in the youtube-automation/ directory."
                )
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS_FILE, YOUTUBE_SCOPES)
            creds = flow.run_local_server(port=0)

        # Save token for future use
        with open(OAUTH_TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return build("youtube", "v3", credentials=creds)


def upload_video(video_path: str, script: dict,
                  thumbnail_path: str = None,
                  privacy_status: str = "public",
                  category_id: str = "27",
                  progress_callback=None) -> dict:
    """
    Upload a video to YouTube.

    Args:
        video_path: Local path to the .mp4 file
        script: Script dict (for title, description, tags)
        thumbnail_path: Optional path to thumbnail image
        privacy_status: "public", "private", or "unlisted"
        category_id: YouTube category ID
                     27 = Education, 25 = News & Politics, 22 = People & Blogs

    Returns:
        dict with video_id, video_url, and upload status
    """
    if not GOOGLE_API_AVAILABLE:
        raise RuntimeError("Google API libraries not available.")

    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Video file not found: {video_path}")

    youtube = get_authenticated_service()

    title = script.get("title", "Untitled Video")[:100]  # YouTube limit: 100 chars
    description = script.get("description", "")
    video_type = script.get("video_type", "long")

    # Channel branding hashtags (YouTube shows the first 3 hashtags above the title)
    missing = [h for h in CHANNEL_HASHTAGS if h.lower() not in description.lower()
               and (h != "#Shorts" or video_type == "short")]
    if missing:
        description = description.rstrip() + "\n\n" + " ".join(missing)
    description = description[:5000]                       # YouTube limit: 5000 chars

    # Tags: de-duplicate, then fit YouTube's 500-CHARACTER total (multi-word tags count
    # their surrounding quotes, plus a comma between tags)
    tags, seen, used = [], set(), 0
    for t in list(script.get("tags", [])) + CHANNEL_TAGS:
        t = str(t).strip().lstrip("#")
        if not t or t.lower() in seen:
            continue
        cost = len(t) + (2 if " " in t else 0) + (1 if tags else 0)
        if used + cost > 500:
            break
        tags.append(t)
        seen.add(t.lower())
        used += cost

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": category_id,
            "defaultLanguage": "hi",          # Hindi/Hinglish channel → recommended to Indian viewers
            "defaultAudioLanguage": "hi",
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": False,
        },
    }

    print(f"  📤 Starting upload: {os.path.basename(video_path)}")
    print(f"  📋 Title: {title}")
    print(f"  🔒 Privacy: {privacy_status}")

    media = MediaFileUpload(
        video_path,
        mimetype="video/mp4",
        resumable=True,
        chunksize=1024 * 1024 * 2,   # 2 MB chunks → smooth progress updates
    )

    request = youtube.videos().insert(
        part=",".join(body.keys()),
        body=body,
        media_body=media,
    )

    video_id = None
    response = None

    # Resumable upload with progress; transient network/server errors retry the chunk
    total = os.path.getsize(video_path)
    if progress_callback:
        progress_callback(0, 0, total)
    retries = 0
    while response is None:
        try:
            status, response = request.next_chunk()
            retries = 0
        except Exception as e:
            code = getattr(getattr(e, "resp", None), "status", None)
            transient = code in (500, 502, 503, 504) or isinstance(e, (ConnectionError, TimeoutError, OSError))
            if not transient or retries >= 5:
                raise
            retries += 1
            wait = min(2 ** retries, 30)
            print(f"  ⚠️  Upload chunk failed ({e}); retry {retries}/5 in {wait}s")
            time.sleep(wait)
            continue
        if status:
            pct = int(status.progress() * 100)
            print(f"  ⬆️  Upload progress: {pct}%")
            if progress_callback:
                progress_callback(pct, status.resumable_progress, total)
    if progress_callback:
        progress_callback(100, total, total)

    video_id = response.get("id")
    video_url = f"https://www.youtube.com/watch?v={video_id}"
    print(f"  ✅ Upload complete: {video_url}")

    # Set thumbnail if provided
    if thumbnail_path and os.path.exists(thumbnail_path):
        try:
            youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(thumbnail_path)
            ).execute()
            print(f"  🖼️  Thumbnail set from: {thumbnail_path}")
        except Exception as e:
            print(f"  ⚠️  Thumbnail upload failed: {e}")

    return {
        "video_id": video_id,
        "video_url": video_url,
        "title": title,
        "privacy_status": privacy_status,
        "status": "uploaded",
    }


def find_or_create_playlist(title: str, description: str = "", privacy: str = "public",
                            youtube=None) -> str:
    """Return the id of the channel's playlist with this exact title, creating it if needed."""
    youtube = youtube or get_authenticated_service()
    token = None
    while True:
        resp = youtube.playlists().list(part="snippet", mine=True, maxResults=50, pageToken=token).execute()
        for pl in resp.get("items", []):
            if pl["snippet"]["title"].strip() == title.strip():
                return pl["id"]
        token = resp.get("nextPageToken")
        if not token:
            break
    pl = youtube.playlists().insert(part="snippet,status", body={
        "snippet": {"title": title[:150], "description": description[:5000], "defaultLanguage": "hi"},
        "status": {"privacyStatus": privacy},
    }).execute()
    print(f"  📃 Created playlist: {title} ({pl['id']})")
    return pl["id"]


def _episode_no(text: str):
    import re
    m = re.search(r"\bEp(?:isode)?\s*(\d+)", text or "", re.I)
    return int(m.group(1)) if m else None


def add_to_playlist(playlist_id: str, video_id: str, episode: int = None, youtube=None) -> bool:
    """
    Add a video to a playlist, kept in episode order (titles contain "Ep N").
    Returns False if it was already there.
    """
    youtube = youtube or get_authenticated_service()
    items, token = [], None
    while True:
        resp = youtube.playlistItems().list(part="snippet", playlistId=playlist_id,
                                            maxResults=50, pageToken=token).execute()
        items += resp.get("items", [])
        token = resp.get("nextPageToken")
        if not token:
            break
    if any(it["snippet"]["resourceId"].get("videoId") == video_id for it in items):
        return False
    body = {"snippet": {"playlistId": playlist_id,
                        "resourceId": {"kind": "youtube#video", "videoId": video_id}}}
    if episode is not None:
        # insert after every item whose episode number is lower
        body["snippet"]["position"] = sum(1 for it in items
                                          if (_episode_no(it["snippet"].get("title")) or 0) < episode)
    youtube.playlistItems().insert(part="snippet", body=body).execute()
    return True


def get_channel_stats(channel_id: str) -> dict:
    """
    Fetch channel statistics from YouTube Data API.

    Args:
        channel_id: YouTube channel ID

    Returns:
        dict with subscriber count, view count, video count, recent videos
    """
    if not GOOGLE_API_AVAILABLE:
        return {"error": "Google API libraries not available"}

    try:
        youtube = get_authenticated_service()

        # Channel stats
        channel_resp = youtube.channels().list(
            part="statistics,snippet",
            id=channel_id
        ).execute()

        channel_data = channel_resp.get("items", [{}])[0]
        stats = channel_data.get("statistics", {})
        snippet = channel_data.get("snippet", {})

        # Recent videos
        search_resp = youtube.search().list(
            part="snippet",
            channelId=channel_id,
            order="date",
            maxResults=5,
            type="video",
        ).execute()

        recent_videos = []
        for item in search_resp.get("items", []):
            s = item.get("snippet", {})
            recent_videos.append({
                "video_id": item["id"]["videoId"],
                "title": s.get("title", ""),
                "published_at": s.get("publishedAt", ""),
                "thumbnail": s.get("thumbnails", {}).get("medium", {}).get("url", ""),
            })

        return {
            "channel_name": snippet.get("title", ""),
            "subscriber_count": int(stats.get("subscriberCount", 0)),
            "view_count": int(stats.get("viewCount", 0)),
            "video_count": int(stats.get("videoCount", 0)),
            "recent_videos": recent_videos,
        }

    except Exception as e:
        return {"error": str(e)}
