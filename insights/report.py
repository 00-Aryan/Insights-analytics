"""Turns snapshots into a Push / Experiment / Hold call per post, compared to YOUR OWN median.

Rates (retention, shares per 1k) are compared only on posts at least MIN_AGE_HOURS old, because
early numbers are noisy and YouTube analytics lag. View counts are compared at fixed ages (24h, 7d).
"""
import re
import sqlite3
from dataclasses import dataclass
from statistics import median

from . import config

QUESTION_RE = re.compile(
    r"\?|\b(how|kaise|kaisa|kab|kya|kyu|kyun|kahan|kidhar|link|part ?2|next|tutorial|price|kitne|kitna|"
    r"where|which|song|music|camera|mic|lens|app|editing|bata|batao|please make)\b",
    re.IGNORECASE,
)


@dataclass
class Row:
    platform: str
    content_id: str
    title: str
    format: str
    topic: str
    hook: str
    decision: str | None
    age_h: float
    views: int | None
    views_24h: int | None
    views_7d: int | None
    retention: float | None  # YouTube: avg % viewed. Instagram: avg seconds watched.
    skip_rate: float | None
    share_1k: float | None
    save_1k: float | None
    follow_1k: float | None
    verdict: str = ""
    hint: str = ""


def _per_1k(n, denom):
    if n is None or not denom:
        return None
    return round(n / denom * 1000, 1)


def _views_at(snaps: list[sqlite3.Row], target_h: float) -> int | None:
    """Views from the snapshot closest to target age, if one exists within 0.5x-1.5x of it."""
    near = [s for s in snaps if 0.5 * target_h <= s["hours_since_post"] <= 1.5 * target_h and s["views"] is not None]
    if not near:
        return None
    return min(near, key=lambda s: abs(s["hours_since_post"] - target_h))["views"]


def build_rows(conn: sqlite3.Connection) -> list[Row]:
    rows = []
    for p in conn.execute("SELECT * FROM posts ORDER BY posted_at DESC"):
        snaps = conn.execute(
            "SELECT * FROM snapshots WHERE platform=? AND content_id=? ORDER BY fetched_at",
            (p["platform"], p["content_id"]),
        ).fetchall()
        if not snaps:
            continue
        last = snaps[-1]
        denom = last["reach"] or last["views"]
        retention = last["avg_view_pct"] if p["platform"] == "youtube" else last["avg_watch_s"]
        rows.append(Row(
            platform=p["platform"], content_id=p["content_id"], title=(p["title"] or "")[:45],
            format=p["format"] or "", topic=p["topic"] or "(untagged)", hook=p["hook"] or "",
            decision=p["decision"], age_h=last["hours_since_post"], views=last["views"],
            views_24h=_views_at(snaps, 24), views_7d=_views_at(snaps, 168),
            retention=None if retention is None else round(retention, 1), skip_rate=last["skip_rate"],
            share_1k=_per_1k(last["shares"], denom), save_1k=_per_1k(last["saves"], denom),
            follow_1k=_per_1k(last["follows"], denom),
        ))
    return rows


def assign_verdicts(rows: list[Row], min_posts: int, min_age_h: float) -> dict[str, dict]:
    """Mutates rows with verdict + hint. Returns per-platform medians."""
    medians = {}
    for platform in {r.platform for r in rows}:
        mine = [r for r in rows if r.platform == platform]
        eligible = [r for r in mine if r.age_h >= min_age_h and r.retention is not None and r.share_1k is not None]
        for r in mine:
            if r not in eligible:
                r.verdict = "WAIT"
                r.hint = f"needs {min_age_h:.0f}h+ of data" if r.age_h < min_age_h else "metrics not returned yet"
        if not eligible:
            continue
        med_ret = median(r.retention for r in eligible)
        med_share = median(r.share_1k for r in eligible)
        skips = [r.skip_rate for r in eligible if r.skip_rate is not None]
        med_skip = median(skips) if skips else None
        medians[platform] = {"n": len(eligible), "retention": med_ret, "share_1k": med_share, "skip_rate": med_skip}

        for r in eligible:
            hold_ok, share_ok = r.retention >= med_ret, r.share_1k >= med_share
            if hold_ok and share_ok:
                call, hint = "PUSH", "make 2-3 variations: same topic, new hook"
            elif hold_ok:
                call, hint = "EXPERIMENT", "watched but not shared: sharper payoff, more relatable or useful to send"
            elif share_ok:
                call, hint = "EXPERIMENT", "idea spreads but edit loses people: cut the slow middle, fix first 2s"
            else:
                call, hint = "HOLD", "retry the topic once with a different hook before dropping it"
            if med_skip is not None and r.skip_rate is not None and r.skip_rate > med_skip:
                hint += "; skip rate above your median, so the first 3s is the problem"
            if len(eligible) < min_posts:
                r.verdict, r.hint = "EARLY", f"leaning {call} (only {len(eligible)} comparable posts; need {min_posts})"
            else:
                r.verdict, r.hint = call, hint
    return medians


def _fmt(v, suffix=""):
    return "-" if v is None else f"{v:g}{suffix}"


def _table(headers: list[str], rows: list[list]) -> list[str]:
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(str(c).replace("|", "/") for c in row) + " |" for row in rows]
    return out


def render(conn: sqlite3.Connection, min_posts=config.MIN_POSTS_FOR_VERDICT, min_age_h=config.MIN_AGE_HOURS) -> str:
    rows = build_rows(conn)
    medians = assign_verdicts(rows, min_posts, min_age_h)
    lines = ["# Content report", ""]
    if not rows:
        return "\n".join(lines + ["No data yet. Run `python -m insights fetch all` first."])

    for platform in sorted({r.platform for r in rows}):
        mine = [r for r in rows if r.platform == platform]
        ret_label = "avg % viewed" if platform == "youtube" else "avg watch s"
        lines += [f"## {platform.title()}", ""]
        f = conn.execute(
            "SELECT followers FROM account_snapshots WHERE platform=? ORDER BY fetched_at", (platform,)
        ).fetchall()
        if f:
            lines.append(f"Followers: **{f[-1]['followers']}** (first tracked: {f[0]['followers']})")
        m = medians.get(platform)
        if m:
            lines.append(
                f"Your medians over {m['n']} comparable posts: {ret_label} {m['retention']:g}, "
                f"shares/1k {m['share_1k']:g}" + (f", skip rate {m['skip_rate']:g}%" if m["skip_rate"] is not None else "")
            )
        lines.append("")
        headers = ["Verdict", "Title", "Topic", "Fmt", "Age h", "Views", "@24h", "@7d", ret_label]
        if platform == "instagram":
            headers += ["Skip %", "Saves/1k"]
        headers += ["Shares/1k", "Follows/1k", "Why / next", "id"]
        body = []
        for r in mine:
            row = [f"**{r.verdict}**", r.title, r.topic, r.format, f"{r.age_h:.0f}", _fmt(r.views),
                   _fmt(r.views_24h), _fmt(r.views_7d), _fmt(r.retention)]
            if platform == "instagram":
                row += [_fmt(r.skip_rate), _fmt(r.save_1k)]
            row += [_fmt(r.share_1k), _fmt(r.follow_1k), r.hint, r.content_id]
            body.append(row)
        lines += _table(headers, body) + [""]

        topics = {}
        for r in mine:
            if r.verdict not in ("WAIT",):
                topics.setdefault(r.topic, []).append(r)
        if topics:
            lines += ["**By topic** (tag posts with `python -m insights tag`)", ""]
            lines += _table(
                ["Topic", "Posts", f"Median {ret_label}", "Median shares/1k"],
                [[t, len(rs), _fmt(median(x.retention for x in rs)), _fmt(median(x.share_1k for x in rs))]
                 for t, rs in sorted(topics.items(), key=lambda kv: -len(kv[1]))],
            ) + [""]

        geo = conn.execute(
            """SELECT breakdown, key, SUM(value) v FROM geo WHERE platform=?
               GROUP BY breakdown, key ORDER BY breakdown, v DESC""", (platform,)
        ).fetchall()
        for breakdown in ("country", "city"):
            g = [x for x in geo if x["breakdown"] == breakdown]
            if g:
                total = sum(x["v"] for x in g) or 1
                label = "Viewers by country" if platform == "youtube" else f"Followers by {breakdown}"
                top = ", ".join(f"{x['key']} {x['v'] / total:.0%}" for x in g[:6])
                lines.append(f"**{label}:** {top}")
        lines.append("")

    qs = [c for c in conn.execute(
        "SELECT platform, content_id, text, like_count FROM comments WHERE replied=0 ORDER BY published_at DESC"
    ) if QUESTION_RE.search(c["text"] or "")]
    lines += ["## Comments to answer (questions and requests)", ""]
    if qs:
        lines += [f"- [{c['platform']}] {c['text'][:140]!r} (on {c['content_id']})" for c in qs[:15]]
        lines += ["", "Requests like \"part 2\" or \"how did you...\" are your next video ideas."]
    else:
        lines.append("None yet.")

    runs = conn.execute(
        """SELECT platform, finished_at, ok, api_calls, error FROM runs WHERE id IN
           (SELECT MAX(id) FROM runs GROUP BY platform)"""
    ).fetchall()
    if runs:
        lines += ["", "## Last fetch", ""]
        lines += [f"- {r['platform']}: {'ok' if r['ok'] else 'FAILED'} at {r['finished_at']}, {r['api_calls']} API calls"
                  + (f", error: {r['error']}" if r["error"] else "") for r in runs]
    return "\n".join(lines) + "\n"
