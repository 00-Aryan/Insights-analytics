"""CLI.

  python -m insights check                       verify setup step by step
  python -m insights yt-login                    one-time YouTube browser login
  python -m insights fetch all|youtube|instagram take a snapshot (run 1-2x a day)
  python -m insights report                      write reports/report-YYYY-MM-DD.md
  python -m insights list                        show post ids
  python -m insights tag <platform> <id> --topic studio-tour --hook question
  python -m insights decide <platform> <id> push|hold|experiment [--note "..."]
  python -m insights ig-refresh                  extend Instagram token another ~60 days
"""
import argparse
import sys
from datetime import date

from . import config, db
from .config import AuthError, QuotaError


def cmd_fetch(args) -> int:
    from . import instagram, youtube

    fetchers = {"youtube": youtube.fetch, "instagram": instagram.fetch}
    targets = list(fetchers) if args.platform == "all" else [args.platform]
    conn = db.connect(config.DB_PATH)
    failed = 0
    for platform in targets:
        run = db.start_run(conn, platform)
        try:
            result = fetchers[platform]()
        except (AuthError, QuotaError) as e:
            db.finish_run(conn, run, ok=False, error=str(e))
            print(f"[{platform}] STOPPED: {e}", file=sys.stderr)
            failed += 1
            continue
        except Exception as e:  # unexpected: record it, keep the other platform going
            db.finish_run(conn, run, ok=False, error=f"{type(e).__name__}: {e}")
            print(f"[{platform}] FAILED: {type(e).__name__}: {e}", file=sys.stderr)
            failed += 1
            continue
        db.save(conn, platform, result)
        db.finish_run(conn, run, ok=True, api_calls=result.api_calls, posts=len(result.posts))
        print(f"[{platform}] {len(result.posts)} posts, {len(result.comments)} comments, {result.api_calls} API calls")
        for n in result.notes:
            print(f"[{platform}]   note: {n}")
    return 1 if failed else 0


def cmd_check(args) -> int:
    """Walk through setup in order and say exactly what is missing."""
    problems = 0

    def say(good: bool, msg: str, fix: str = "") -> bool:
        nonlocal problems
        problems += not good
        print(f"[{'OK' if good else 'MISSING'}] {msg}" + ("" if good else f"\n          fix: {fix}"))
        return good

    say((config.ROOT / ".env").exists(), ".env file", "copy .env.example to .env")

    if say(config.YT_CLIENT_SECRET.exists(), f"YouTube OAuth client ({config.YT_CLIENT_SECRET.name})",
           "README > YouTube steps 1-4"):
        if say(config.YT_TOKEN.exists(), "YouTube login token", "run: python -m insights yt-login"):
            try:
                from googleapiclient.discovery import build
                from . import youtube
                yt = build("youtube", "v3", credentials=youtube._credentials(), cache_discovery=False)
                items = yt.channels().list(part="snippet,statistics", mine=True).execute().get("items", [])
                say(bool(items), f"YouTube channel: {items[0]['snippet']['title'] if items else 'none'}",
                    "log in with the Google account that owns the channel")
            except Exception as e:
                say(False, f"YouTube API call ({type(e).__name__}: {e})",
                    "check both APIs are enabled and you are a test user on the consent screen")

    try:
        from . import instagram
        me = instagram.Client(instagram.load_token()).get("me", fields="username,account_type,followers_count")
        say(True, f"Instagram @{me.get('username')} ({me.get('account_type')}, {me.get('followers_count')} followers)")
    except Exception as e:
        say(False, f"Instagram ({e})", "README > Instagram steps; paste token into .env as IG_ACCESS_TOKEN")

    print("\nAll set. Run: python -m insights fetch all" if not problems else f"\n{problems} step(s) left.")
    return 1 if problems else 0


def cmd_report(args) -> int:
    from . import report

    conn = db.connect(config.DB_PATH)
    text = report.render(conn, min_posts=args.min_posts, min_age_h=args.min_age)
    config.REPORTS_DIR.mkdir(exist_ok=True)
    path = config.REPORTS_DIR / f"report-{date.today().isoformat()}.md"
    path.write_text(text)
    print(text)
    print(f"Saved {path}")
    return 0


def cmd_list(args) -> int:
    conn = db.connect(config.DB_PATH)
    for p in conn.execute("SELECT platform, content_id, format, posted_at, topic, hook, title FROM posts ORDER BY posted_at DESC"):
        print(f"{p['platform']:9} {p['content_id']:20} {p['format'] or '':8} {p['posted_at'][:10]} "
              f"topic={p['topic'] or '-'} hook={p['hook'] or '-'}  {(p['title'] or '')[:50]}")
    return 0


def _update_post(platform, content_id, **fields) -> int:
    conn = db.connect(config.DB_PATH)
    fields = {k: v for k, v in fields.items() if v is not None}
    sets = ", ".join(f"{k}=?" for k in fields)
    with conn:
        cur = conn.execute(f"UPDATE posts SET {sets} WHERE platform=? AND content_id=?",
                           (*fields.values(), platform, content_id))
    if cur.rowcount == 0:
        print(f"No post {platform}/{content_id}. Run `python -m insights list`.", file=sys.stderr)
        return 1
    print(f"Updated {platform}/{content_id}: {fields}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="insights", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    sub.add_parser("yt-login")
    sub.add_parser("ig-refresh")
    f = sub.add_parser("fetch")
    f.add_argument("platform", choices=["all", "youtube", "instagram"])
    r = sub.add_parser("report")
    r.add_argument("--min-posts", type=int, default=config.MIN_POSTS_FOR_VERDICT)
    r.add_argument("--min-age", type=float, default=config.MIN_AGE_HOURS)
    sub.add_parser("list")
    t = sub.add_parser("tag")
    t.add_argument("platform", choices=["youtube", "instagram"])
    t.add_argument("content_id")
    t.add_argument("--topic")
    t.add_argument("--hook")
    d = sub.add_parser("decide")
    d.add_argument("platform", choices=["youtube", "instagram"])
    d.add_argument("content_id")
    d.add_argument("decision", choices=["push", "hold", "experiment"])
    d.add_argument("--note")
    args = ap.parse_args(argv)

    try:
        if args.cmd == "yt-login":
            from . import youtube
            youtube.login()
            return 0
        if args.cmd == "ig-refresh":
            from . import instagram
            instagram.refresh_token()
            return 0
    except AuthError as e:
        print(f"STOPPED: {e}", file=sys.stderr)
        return 1
    if args.cmd == "check":
        return cmd_check(args)
    if args.cmd == "fetch":
        return cmd_fetch(args)
    if args.cmd == "report":
        return cmd_report(args)
    if args.cmd == "list":
        return cmd_list(args)
    if args.cmd == "tag":
        if not (args.topic or args.hook):
            ap.error("give --topic and/or --hook")
        return _update_post(args.platform, args.content_id, topic=args.topic, hook=args.hook)
    if args.cmd == "decide":
        return _update_post(args.platform, args.content_id, decision=args.decision,
                            decision_note=args.note, decided_at=db.utcnow())
    return 1


if __name__ == "__main__":
    sys.exit(main())
