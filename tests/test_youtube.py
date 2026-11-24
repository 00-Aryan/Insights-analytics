"""YouTube fetch against a fake Google client: happy path plus the failure paths."""
import json

import httplib2
import pytest
from googleapiclient.errors import HttpError

from insights import youtube
from insights.config import AuthError, QuotaError


def http_error(status, reason):
    body = json.dumps({"error": {"code": status, "message": reason, "errors": [{"reason": reason}]}}).encode()
    return HttpError(httplib2.Response({"status": status}), body)


class Call:
    def __init__(self, result):
        self.result = result

    def execute(self):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class Resource:
    def __init__(self, fn):
        self.fn = fn

    def list(self, **kw):
        return Call(self.fn(**kw))

    def query(self, **kw):
        return Call(self.fn(**kw))


VIDEO = {
    "id": "vid1",
    "snippet": {"publishedAt": "2026-11-23T08:00:00Z", "title": "Studio tour"},
    "statistics": {"viewCount": "420", "likeCount": "30", "commentCount": "2"},
    "contentDetails": {"duration": "PT45S"},
}


class FakeYT:
    def __init__(self, comments=None):
        self._comments = comments

    def channels(self):
        return Resource(lambda **k: {"items": [{"statistics": {"subscriberCount": "12"},
                                               "contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]})

    def playlistItems(self):
        return Resource(lambda **k: {"items": [{"contentDetails": {"videoId": "vid1"}}]})

    def videos(self):
        return Resource(lambda **k: {"items": [VIDEO]})

    def commentThreads(self):
        default = {"items": [{"id": "c1", "snippet": {"topLevelComment": {"snippet": {
            "textDisplay": "camera konsa hai?", "publishedAt": "2026-11-23T09:00:00Z", "likeCount": 1}}}}]}
        return Resource(lambda **k: default if self._comments is None else self._comments)


class FakeYA:
    def __init__(self, analytics_ready=True, error=None):
        self.ready, self.error = analytics_ready, error

    def reports(self):
        def q(**k):
            if self.error:
                return self.error
            if not self.ready:
                return {"columnHeaders": [], "rows": []}
            if k.get("dimensions") == "country":
                return {"columnHeaders": [{"name": "country"}, {"name": "views"}], "rows": [["IN", 300], ["US", 20]]}
            names = k["metrics"].split(",")
            return {"columnHeaders": [{"name": n} for n in names], "rows": [[400, 62.5, 28, 5, 3]]}
        return Resource(q)


@pytest.fixture
def patch(monkeypatch):
    def apply(yt=None, ya=None):
        yt, ya = yt or FakeYT(), ya or FakeYA()
        monkeypatch.setattr(youtube, "_credentials", lambda: object())
        monkeypatch.setattr("googleapiclient.discovery.build",
                            lambda name, ver, **kw: yt if name == "youtube" else ya)
    return apply


def test_happy_path(patch):
    patch()
    r = youtube.fetch()
    s = r.snapshots[0]
    assert r.followers == 12 and r.posts[0].format == "short" and r.posts[0].duration_s == 45
    assert (s.views, s.avg_view_pct, s.shares, s.follows) == (420, 62.5, 5, 3)
    assert ("vid1", "country", "IN", 300.0) in r.geo
    assert r.comments[0].text == "camera konsa hai?"


def test_analytics_lag_is_a_note_not_a_crash(patch):
    patch(ya=FakeYA(analytics_ready=False))
    r = youtube.fetch()
    assert r.snapshots[0].views == 420 and r.snapshots[0].avg_view_pct is None
    assert any("analytics not ready" in n for n in r.notes)


def test_comments_disabled_is_skipped(patch):
    patch(yt=FakeYT(comments=http_error(403, "commentsDisabled")))
    r = youtube.fetch()
    assert r.comments == [] and len(r.snapshots) == 1


def test_quota_fails_fast(patch):
    patch(ya=FakeYA(error=http_error(403, "quotaExceeded")))
    with pytest.raises(QuotaError):
        youtube.fetch()


def test_revoked_login_fails_fast(patch):
    patch(ya=FakeYA(error=http_error(401, "authError")))
    with pytest.raises(AuthError):
        youtube.fetch()
