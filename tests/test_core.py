import pytest

from insights import db, instagram, report
from insights.config import AuthError
from insights.db import FetchResult, Post, Snapshot
from insights.youtube import parse_iso_duration


def test_iso_duration():
    assert parse_iso_duration("PT1M5S") == 65
    assert parse_iso_duration("PT45S") == 45
    assert parse_iso_duration("PT1H2M") == 3720
    assert parse_iso_duration(None) is None


def _seed(conn, n, platform="youtube"):
    """n posts; post i has retention 10*i and shares i per 1000 views, all 100h old."""
    res = FetchResult()
    for i in range(1, n + 1):
        cid = f"v{i}"
        res.posts.append(Post(platform, cid, "2026-11-01T00:00:00+00:00", title=f"t{i}", format="short"))
        res.snapshots.append(Snapshot(platform, cid, "2026-11-05T04:00:00+00:00", 100, views=1000,
                                      shares=i, avg_view_pct=10.0 * i, avg_watch_s=float(i)))
    db.save(conn, platform, res)


def test_too_few_posts_gives_early_not_push():
    conn = db.connect(":memory:")
    _seed(conn, 2)
    rows = report.build_rows(conn)
    report.assign_verdicts(rows, min_posts=8, min_age_h=72)
    assert all(r.verdict == "EARLY" for r in rows)


def test_quadrants():
    conn = db.connect(":memory:")
    _seed(conn, 8)
    rows = {r.content_id: r for r in report.build_rows(conn)}
    rows["v1"].share_1k = 99  # low retention, high shares
    rows["v8"].share_1k = 0   # high retention, low shares
    report.assign_verdicts(list(rows.values()), min_posts=8, min_age_h=72)
    assert rows["v7"].verdict == "PUSH"
    assert rows["v2"].verdict == "HOLD"
    assert rows["v1"].verdict == "EXPERIMENT" and "edit loses" in rows["v1"].hint
    assert rows["v8"].verdict == "EXPERIMENT" and "not shared" in rows["v8"].hint


def test_young_post_waits():
    conn = db.connect(":memory:")
    _seed(conn, 8)
    res = FetchResult(posts=[Post("youtube", "new", "2026-11-05T00:00:00+00:00")],
                      snapshots=[Snapshot("youtube", "new", "2026-11-05T10:00:00+00:00", 10, views=50, shares=5, avg_view_pct=90)])
    db.save(conn, "youtube", res)
    rows = {r.content_id: r for r in report.build_rows(conn)}
    report.assign_verdicts(list(rows.values()), 8, 72)
    assert rows["new"].verdict == "WAIT"


def test_views_at_fixed_age():
    conn = db.connect(":memory:")
    res = FetchResult(posts=[Post("youtube", "a", "2026-11-01T00:00:00+00:00")], snapshots=[
        Snapshot("youtube", "a", "2026-11-01T23:00:00+00:00", 23, views=100),
        Snapshot("youtube", "a", "2026-11-08T01:00:00+00:00", 169, views=900),
    ])
    db.save(conn, "youtube", res)
    r = report.build_rows(conn)[0]
    assert (r.views_24h, r.views_7d) == (100, 900)


def test_owner_labels_survive_refetch():
    conn = db.connect(":memory:")
    _seed(conn, 1)
    conn.execute("UPDATE posts SET topic='studio-tour', decision='push' WHERE content_id='v1'")
    _seed(conn, 1)
    p = conn.execute("SELECT topic, decision FROM posts WHERE content_id='v1'").fetchone()
    assert (p["topic"], p["decision"]) == ("studio-tour", "push")


def test_report_renders_and_flags_questions():
    conn = db.connect(":memory:")
    _seed(conn, 3)
    db.save(conn, "youtube", FetchResult(comments=[
        db.Comment("youtube", "c1", "v1", "bhai part 2 kab aayega"),
        db.Comment("youtube", "c2", "v1", "nice"),
    ]))
    text = report.render(conn, min_posts=8, min_age_h=72)
    assert "part 2 kab" in text and "'nice'" not in text


# --- Instagram: failure paths ---------------------------------------------------------

class FakeResp:
    def __init__(self, body):
        self._b = body
        self.content = b"x"

    def json(self):
        return self._b

    def raise_for_status(self):
        pass


class FakeSession:
    def __init__(self, handler):
        self.handler = handler

    def get(self, url, params, timeout):
        return FakeResp(self.handler(url, params))


def test_ig_expired_token_fails_fast():
    s = FakeSession(lambda u, p: {"error": {"code": 190, "message": "expired"}})
    with pytest.raises(AuthError):
        instagram.fetch(client=instagram.Client("t", session=s))


def test_ig_invalid_metric_falls_back_per_metric():
    def handler(url, p):
        m = p.get("metric", "")
        if "," in m or m == "reels_skip_rate":
            return {"error": {"code": 100, "message": "invalid metric"}}
        return {"data": [{"name": m, "values": [{"value": 7}]}]}

    c = instagram.Client("t", session=FakeSession(handler))
    got, missing = instagram.media_insights(c, "123", ["views", "reach", "reels_skip_rate"])
    assert got == {"views": 7, "reach": 7} and missing == ["reels_skip_rate"]


def test_ig_full_fetch_with_small_account():
    def handler(url, p):
        if url.endswith("/me"):
            return {"user_id": "1", "username": "x", "account_type": "MEDIA_CREATOR", "followers_count": 40}
        if url.endswith("/me/media"):
            return {"data": [{"id": "m1", "caption": "Studio tour\n#tags", "media_type": "VIDEO",
                              "media_product_type": "REELS", "timestamp": "2026-11-23T10:00:00+0000",
                              "permalink": "https://instagram.com/reel/x", "comments_count": 0}]}
        if url.endswith("/m1/insights"):
            return {"data": [{"name": "views", "values": [{"value": 500}]},
                             {"name": "reach", "values": [{"value": 400}]},
                             {"name": "shares", "values": [{"value": 4}]},
                             {"name": "ig_reels_avg_watch_time", "values": [{"value": 6500}]},
                             {"name": "reels_skip_rate", "values": [{"value": 41.0}]}]}
        if url.endswith("/me/insights"):
            return {"error": {"code": 10, "message": "Not enough followers"}}
        raise AssertionError(url)

    res = instagram.fetch(client=instagram.Client("t", session=FakeSession(handler)))
    s = res.snapshots[0]
    assert (s.views, s.reach, s.shares, s.avg_watch_s, s.skip_rate) == (500, 400, 4, 6.5, 41.0)
    assert res.posts[0].title == "Studio tour" and res.posts[0].format == "reel"
    assert any("100+ followers" in n for n in res.notes)
