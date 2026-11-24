"""Instagram Creator account via "Instagram API with Instagram Login" (graph.instagram.com).

Needs permissions: instagram_business_basic, instagram_business_manage_insights,
instagram_business_manage_comments. Audience country/city needs 100+ followers.
"""
import json
import os
import time

import requests

from . import config
from .config import AuthError, QuotaError
from .db import Comment, FetchResult, Post, Snapshot, hours_between, utcnow

REEL_METRICS = ["views", "reach", "saved", "shares", "likes", "comments",
                "ig_reels_avg_watch_time", "reels_skip_rate"]
FEED_METRICS = ["views", "reach", "saved", "shares", "likes", "comments", "follows"]

AUTH_CODES = {190, 102}  # token invalid/expired; permission errors (10, 200) are handled per call
RATE_CODES = {4, 17, 32, 613}


class IGError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(f"Instagram API error {code}: {message}")
        self.code = code


def load_token() -> str:
    if config.IG_TOKEN_FILE.exists():
        return json.loads(config.IG_TOKEN_FILE.read_text())["access_token"]
    token = os.getenv("IG_ACCESS_TOKEN", "").strip()
    if not token:
        raise AuthError("No Instagram token. Put IG_ACCESS_TOKEN in .env (see README).")
    return token


class Client:
    def __init__(self, token: str, session=None):
        self.token = token
        self.base = f"https://graph.instagram.com/{config.IG_API_VERSION}"
        self.http = session or requests.Session()
        self.calls = 0

    def get(self, path: str, **params) -> dict:
        params["access_token"] = self.token
        url = path if path.startswith("http") else f"{self.base}/{path.lstrip('/')}"
        self.calls += 1
        r = self.http.get(url, params=params, timeout=30)
        body = r.json() if r.content else {}
        if "error" in body:
            err = body["error"]
            code = err.get("code")
            if code in AUTH_CODES:
                raise AuthError(f"Instagram token rejected ({code}): {err.get('message')}. Get a new token.")
            if code in RATE_CODES:
                raise QuotaError(f"Instagram rate limit ({code}): {err.get('message')}")
            raise IGError(code, err.get("message"))
        r.raise_for_status()
        return body


def parse_insights(data: list[dict]) -> dict:
    """Insights come back as either values[0].value or total_value.value."""
    out = {}
    for m in data:
        if "total_value" in m:
            out[m["name"]] = m["total_value"].get("value")
        elif m.get("values"):
            out[m["name"]] = m["values"][0].get("value")
    return out


def media_insights(client: Client, media_id: str, metrics: list[str]) -> tuple[dict, list[str]]:
    """Ask for all metrics at once; if Meta rejects one metric (code 100), fall back to one by one."""
    try:
        return parse_insights(client.get(f"{media_id}/insights", metric=",".join(metrics))["data"]), []
    except IGError as e:
        if e.code != 100:
            raise
    got, missing = {}, []
    for m in metrics:
        try:
            got.update(parse_insights(client.get(f"{media_id}/insights", metric=m)["data"]))
        except IGError:
            missing.append(m)
    return got, missing


def _format(media: dict) -> str:
    if media.get("media_product_type") == "REELS":
        return "reel"
    return {"CAROUSEL_ALBUM": "carousel", "IMAGE": "image", "VIDEO": "video"}.get(media.get("media_type"), "other")


def fetch(max_media: int = config.IG_MAX_MEDIA, client: Client | None = None) -> FetchResult:
    client = client or Client(load_token())
    out = FetchResult()
    now = utcnow()

    me = client.get("me", fields="user_id,username,account_type,followers_count,media_count")
    out.followers = me.get("followers_count")
    if me.get("account_type") not in (None, "BUSINESS", "MEDIA_CREATOR", "CREATOR"):
        out.notes.append(f"Account type is {me.get('account_type')}; insights need a Creator or Business account.")

    media, page = [], client.get(
        "me/media", fields="id,caption,media_type,media_product_type,timestamp,permalink,like_count,comments_count",
        limit=min(max_media, 50),
    )
    while True:
        media += page.get("data", [])
        nxt = page.get("paging", {}).get("next")
        if len(media) >= max_media or not nxt:
            break
        page = client.get(nxt)
    media = media[:max_media]

    for m in media:
        if m.get("media_product_type") == "STORY":
            continue
        fmt = _format(m)
        out.posts.append(Post(
            platform="instagram", content_id=m["id"], posted_at=m["timestamp"].replace("+0000", "+00:00"),
            title=(m.get("caption") or "").split("\n")[0][:120] or None, url=m.get("permalink"), format=fmt,
        ))
        ins, missing = media_insights(client, m["id"], REEL_METRICS if fmt == "reel" else FEED_METRICS)
        if missing:
            out.notes.append(f"{m['id']}: Meta did not return {', '.join(missing)}")
        avg_ms = ins.get("ig_reels_avg_watch_time")
        out.snapshots.append(Snapshot(
            platform="instagram", content_id=m["id"], fetched_at=now,
            hours_since_post=hours_between(out.posts[-1].posted_at, now),
            views=ins.get("views"), reach=ins.get("reach"),
            likes=ins.get("likes", m.get("like_count")), comments=ins.get("comments", m.get("comments_count")),
            shares=ins.get("shares"), saves=ins.get("saved"), follows=ins.get("follows"),
            avg_watch_s=None if avg_ms is None else round(avg_ms / 1000, 2),
            skip_rate=ins.get("reels_skip_rate"),
        ))
        if (m.get("comments_count") or 0) > 0:
            try:
                resp = client.get(f"{m['id']}/comments", fields="id,text,timestamp,like_count", limit=50)
                for c in resp.get("data", []):
                    out.comments.append(Comment(
                        platform="instagram", comment_id=c["id"], content_id=m["id"], text=c.get("text", ""),
                        published_at=c.get("timestamp"), like_count=c.get("like_count"),
                    ))
            except IGError as e:
                out.notes.append(f"{m['id']}: comments unavailable ({e})")

    for breakdown in ("country", "city"):
        try:
            resp = client.get(
                "me/insights", metric="follower_demographics", period="lifetime",
                metric_type="total_value", breakdown=breakdown,
            )
            for metric in resp.get("data", []):
                for b in metric.get("total_value", {}).get("breakdowns", []):
                    for r in b.get("results", []):
                        out.geo.append(("_account", breakdown, r["dimension_values"][0], float(r["value"])))
        except IGError as e:
            out.notes.append(f"Audience {breakdown} not available yet ({e}). Usually needs 100+ followers.")

    out.api_calls = client.calls
    return out


def refresh_token() -> None:
    """Long-lived tokens last ~60 days; refreshing resets the clock. Token must be 24h+ old."""
    token = load_token()
    r = requests.get(
        "https://graph.instagram.com/refresh_access_token",
        params={"grant_type": "ig_refresh_token", "access_token": token}, timeout=30,
    )
    body = r.json()
    if "access_token" not in body:
        raise AuthError(f"Instagram token refresh failed: {body.get('error', body)}")
    config.SECRETS_DIR.mkdir(exist_ok=True)
    body["refreshed_at"] = int(time.time())
    config.IG_TOKEN_FILE.write_text(json.dumps(body))
    days = body.get("expires_in", 0) // 86400
    print(f"Instagram token refreshed; valid for ~{days} days. Saved to {config.IG_TOKEN_FILE}")
