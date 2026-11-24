# Insights-analytics

Pulls your own YouTube and Instagram numbers through the official APIs. Each post gets a **PUSH / EXPERIMENT / HOLD** call, measured against **your own median**, not against other creators.

```
fetch (1-2x/day) -> SQLite snapshots -> report: verdict per post, topic table, top countries, comments to answer
```

## What the report decides on

| Signal | YouTube | Instagram |
|---|---|---|
| Hold (do people stay?) | avg % viewed | avg watch seconds, plus **skip rate** (share of viewers gone in the first 3s) |
| Spread (would they send it?) | shares per 1k views | shares per 1k reach, saves per 1k |
| Convert | subs gained per 1k | follows per 1k (feed posts only) |
| Velocity | views at 24h and 7d | views at 24h and 7d |

- **PUSH**: hold and spread both at or above your median. Make 2-3 variations.
- **EXPERIMENT**: one of the two is above median. The hint says which part to fix.
- **HOLD**: both below median. Retry the topic once with a new hook before dropping it.
- **EARLY**: fewer than 8 comparable posts. Shown as "leaning X". Don't act on it yet.
- **WAIT**: the post is under 72h old, or YouTube analytics haven't caught up (they lag 1-3 days).

Tag every post with a topic and a hook style. Topic-level results are what tell you what to make next.

## Setup (on your own computer)

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt   # Windows: .venv\Scripts\pip
cp .env.example .env
```

### YouTube (about 15 min, one time)
1. Go to [console.cloud.google.com](https://console.cloud.google.com) and create a project.
2. Under **APIs & Services → Library**, enable **YouTube Data API v3** and **YouTube Analytics API**.
3. On the **OAuth consent screen**, choose External and add your own Gmail as a test user.
4. Under **Credentials → Create credentials → OAuth client ID → Desktop app**, download the JSON and save it as `secrets/client_secret.json`.
5. Run `python -m insights yt-login`. A browser opens: log in with the account that owns the channel.

### Instagram Creator account (about 20 min, one time)
1. At [developers.facebook.com](https://developers.facebook.com), create an app and add the **Instagram** product. Pick **API setup with Instagram login**.
2. Add your Instagram account as an **Instagram tester**, then accept the invite in the Instagram app (Settings → Website permissions / Apps).
3. In the app's Instagram settings, generate an access token for your account with `instagram_business_basic`, `instagram_business_manage_insights` and `instagram_business_manage_comments`.
4. Paste the token into `.env` as `IG_ACCESS_TOKEN`. Tokens last about 60 days, so run `python -m insights ig-refresh` once a month.

Meta changes this dashboard often. If a step's name doesn't match, look for "Instagram API with Instagram Login" in their docs.

## Daily use

```bash
python -m insights fetch all          # schedule this 1-2x a day
python -m insights list               # see post ids
python -m insights tag youtube <id> --topic studio-tour --hook before-after
python -m insights report             # saved to reports/report-YYYY-MM-DD.md
python -m insights decide youtube <id> push --note "variation with faster intro"
```

To schedule it:
- **Linux/macOS:** run `crontab -e` and add `0 9,21 * * * cd /path/to/Insights-analytics && .venv/bin/python -m insights fetch all`.
- **Windows:** use Task Scheduler to run `.venv\Scripts\python.exe -m insights fetch all` with "Start in" set to the repo folder.

Every fetch adds new snapshots and never overwrites old ones, so views-at-24h and views-at-7d only exist if fetch was running at those ages. Start the schedule now.

## Not built yet, on purpose (tech-debt ledger)

| Item | Why not now | What would trigger building it |
|---|---|---|
| Facebook Page fetcher | Reels cross-posted from Instagram already show `crossposted_views`/`facebook_views` there; separate FB content is low priority | You post FB-only content, or FB becomes 20%+ of your views |
| LLM comment-intent classifier | Too few comments to justify it. A keyword rule (Hindi + English) flags questions and "part 2" requests | 50+ comments a week |
| Hosted runs (GitHub Actions) | Needs secrets stored in CI and a persistent DB | You miss scheduled fetches because your laptop is off |
| Long-form retention curves | Shorts/Reels first | You start posting 5+ minute videos |
| Instagram token auto-refresh | A monthly manual `ig-refresh` is enough | You forget it once |

Development: `pip install -r requirements-dev.txt && pytest`.
