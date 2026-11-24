"""YouTube: Data API v3 (videos, live counts, comments) + Analytics API v2 (retention, shares, geography).

Analytics numbers lag 1-3 days behind the live view count; that's YouTube, not a bug.
"""
import re
from datetime import date

from . import config
from .config import AuthError, QuotaError
from .db import Comment, FetchResult, Post, Snapshot, hours_between, utcnow

SHORTS_MAX_S = 180  # Shorts can be up to 3 minutes


def parse_iso_duration(value: str | None) -> float | None:
    """'PT1M5S' -> 65.0"""
    if not value:
        return None
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value)
    if not m:
        return None
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return float(d * 86400 + h * 3600 + mi * 60 + s)


def _credentials():
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = None
    if config.YT_TOKEN.exists():
        creds = Credentials.from_authorized_user_file(str(config.YT_TOKEN), config.YT_SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError as e:
            raise AuthError(f"YouTube token refresh failed ({e}). Delete {config.YT_TOKEN} and run `python -m insights yt-login`.")
    else:
        raise AuthError("No YouTube login yet. Run `python -m insights yt-login` on your own computer (it opens a browser).")
    config.YT_TOKEN.write_text(creds.to_json())
    return creds


def login() -> None:
    """One-time browser login. Run on a machine with a browser."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not config.YT_CLIENT_SECRET.exists():
        raise AuthError(
            f"Missing {config.YT_CLIENT_SECRET}. Download the OAuth client (Desktop app) JSON from Google Cloud Console."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(config.YT_CLIENT_SECRET), config.YT_SCOPES)
    creds = flow.run_local_server(port=0)
    config.SECRETS_DIR.mkdir(exist_ok=True)
    config.YT_TOKEN.write_text(creds.to_json())
    print(f"Saved YouTube token to {config.YT_TOKEN}")


def _raise_for_http(e) -> None:
    """Map Google HttpErrors to fail-fast errors. Returns if the error is ignorable."""
    status = getattr(e.resp, "status", None)
    text = str(e)
    if status == 401:
        raise AuthError(f"YouTube auth rejected: {text}")
    if status == 403 and ("quota" in text.lower() or "rateLimit" in text):
        raise QuotaError(f"YouTube quota hit: {text}")
    if status == 403 and "commentsDisabled" in text:
        return
    if status == 403:
        raise AuthError(f"YouTube permission denied: {text}")
    raise e


def _rows_to_dicts(resp: dict) -> list[dict]:
    headers = [h["name"] for h in resp.get("columnHeaders", [])]
    return [dict(zip(headers, row)) for row in resp.get("rows", []) or []]


def fetch(max_videos: int = config.YT_MAX_VIDEOS) -> FetchResult:
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    creds = _credentials()
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    ya = build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False)
    out = FetchResult()
    now = utcnow()
    today = date.today().isoformat()

    try:
        ch = yt.channels().list(part="contentDetails,statistics", mine=True).execute()
        out.api_calls += 1
        if not ch.get("items"):
            raise AuthError("This Google account has no YouTube channel. Log in with the channel's account.")
        channel = ch["items"][0]
        out.followers = int(channel["statistics"].get("subscriberCount", 0))
        uploads = channel["contentDetails"]["relatedPlaylists"]["uploads"]

        video_ids, page = [], None
        while len(video_ids) < max_videos:
            resp = yt.playlistItems().list(
                part="contentDetails", playlistId=uploads, maxResults=50, pageToken=page
            ).execute()
            out.api_calls += 1
            video_ids += [i["contentDetails"]["videoId"] for i in resp.get("items", [])]
            page = resp.get("nextPageToken")
            if not page:
                break
        video_ids = video_ids[:max_videos]

        videos = []
        for i in range(0, len(video_ids), 50):
            resp = yt.videos().list(part="snippet,statistics,contentDetails", id=",".join(video_ids[i:i + 50])).execute()
            out.api_calls += 1
            videos += resp.get("items", [])

        for v in videos:
            vid = v["id"]
            sn, st = v["snippet"], v.get("statistics", {})
            posted_at = sn["publishedAt"]
            duration = parse_iso_duration(v["contentDetails"].get("duration"))
            out.posts.append(Post(
                platform="youtube", content_id=vid, posted_at=posted_at, title=sn.get("title"),
                url=f"https://youtu.be/{vid}",
                format="short" if duration is not None and duration <= SHORTS_MAX_S else "long",
                duration_s=duration,
            ))

            snap = Snapshot(
                platform="youtube", content_id=vid, fetched_at=now,
                hours_since_post=hours_between(posted_at, now),
                views=_int(st.get("viewCount")), likes=_int(st.get("likeCount")),
                comments=_int(st.get("commentCount")),
            )

            start = posted_at[:10]
            q = dict(ids="channel==MINE", startDate=start, endDate=today, filters=f"video=={vid}")
            resp = ya.reports().query(
                metrics="views,averageViewPercentage,averageViewDuration,shares,subscribersGained", **q
            ).execute()
            out.api_calls += 1
            rows = _rows_to_dicts(resp)
            if rows:
                r = rows[0]
                snap.avg_view_pct = r.get("averageViewPercentage")
                snap.avg_watch_s = r.get("averageViewDuration")
                snap.shares = _int(r.get("shares"))
                snap.follows = _int(r.get("subscribersGained"))
            else:
                out.notes.append(f"{vid}: analytics not ready yet (normal for the first 1-3 days)")
            out.snapshots.append(snap)

            resp = ya.reports().query(metrics="views", dimensions="country", **q).execute()
            out.api_calls += 1
            for r in _rows_to_dicts(resp):
                out.geo.append((vid, "country", r["country"], float(r["views"])))

            try:
                resp = yt.commentThreads().list(
                    part="snippet", videoId=vid, maxResults=100, textFormat="plainText", order="time"
                ).execute()
                out.api_calls += 1
                for t in resp.get("items", []):
                    c = t["snippet"]["topLevelComment"]["snippet"]
                    out.comments.append(Comment(
                        platform="youtube", comment_id=t["id"], content_id=vid,
                        text=c.get("textDisplay", ""), published_at=c.get("publishedAt"),
                        like_count=_int(c.get("likeCount")),
                    ))
            except HttpError as e:
                _raise_for_http(e)  # returns only when comments are disabled
    except HttpError as e:
        _raise_for_http(e)
        raise
    return out


def _int(v) -> int | None:
    return None if v is None else int(v)
