# Google Play Ranking → Webhook

Fetches a Google Play **top chart** (Top Free / Top Grossing / Top Paid) for a
category + country every day and posts it to a webhook (Slack, Discord, or any
custom endpoint). Can also report the **daily rank of specific games** you care
about.

- **Free.** No API key, no paid service.
- **Serverless.** Runs on **GitHub Actions** — Google runs the schedule for you;
  your PC doesn't need to be on.
- **No dependencies.** `main.py` uses only the Python standard library.

> Data source: Google Play's own (unofficial) web endpoint — the same one the
> `google-play-scraper` project uses. See [If it stops working](#if-it-stops-working).

---

## The five planning questions

| Question | Answer here |
|---|---|
| Where's the data? | Google Play top-chart endpoint |
| What counts as "the ranking"? | Position in a chart (Top Free / Grossing / Paid) for a category + country |
| What do we send? | Top N of the chart, and/or the rank of specific package IDs |
| Where does it go? | A webhook URL (Slack / Discord / generic JSON) |
| What runs it, and is it guaranteed on? | GitHub Actions cron — serverless, always available |

---

## Quick start

### 1. Try it locally first (diagnostic mode)
With **no** `WEBHOOK_URL` set, it just prints the chart — nothing is sent.

```bash
python main.py
```

Change what you fetch with environment variables:

```bash
TOP_N=5 COLLECTION=GROSSING COUNTRY=id TRACK_APPS="com.dts.freefireth,com.mobile.legends" python main.py
```

### 2. Get a webhook URL
- **Slack:** create an *Incoming Webhook* → `https://hooks.slack.com/services/...`
- **Discord:** channel → Edit → Integrations → Webhooks → New → Copy URL
- **Custom:** any endpoint that accepts a `POST` with a JSON body

### 3. Put it on GitHub (free daily run)
1. Create a repo and push these files.
2. Repo **Settings → Secrets and variables → Actions → New repository secret**
   - Name: `WEBHOOK_URL`  ·  Value: your webhook URL
3. Edit the config block in [`.github/workflows/daily.yml`](.github/workflows/daily.yml)
   (chart type, country, tracked apps, schedule time).
4. **Actions** tab → run **“Daily Google Play ranking”** once via *Run workflow*
   to confirm it posts. After that it runs on the cron schedule automatically.

---

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `WEBHOOK_URL` | *(none)* | Where to POST. Unset = print only (diagnostic). |
| `WEBHOOK_TYPE` | `auto` | `slack` \| `discord` \| `generic` \| `seatalk` \| `auto` (detects from URL). |
| `CHARTS` | `TOP_FREE:GAME,GROSSING:GAME` | Comma-separated `COLLECTION:CATEGORY` pairs; each posts as its own section. COLLECTION = `TOP_FREE`\|`TOP_PAID`\|`GROSSING`; CATEGORY = `GAME` (all) or e.g. `GAME_ACTION`. |
| `COUNTRY` | `us` | Store country code: `us`, `id`, `in`, `br`, … |
| `LANG_` | `en` | Language code. (Named `LANG_` so it doesn't clash with the shell's `LANG`.) |
| `TOP_N` | `10` | How many chart entries to post per section. |
| `RANK_DEPTH` | `200` | How deep to fetch when locating `TRACK_APPS`. |
| `TRACK_APPS` | *(none)* | Comma-separated package IDs to report the rank of. |
| `HISTORY_FILE` | `history.csv` | CSV log file; `""` disables history. |
| `HISTORY_DEPTH` | `25` | Top N per chart written to the CSV each run. |
| `TZ_OFFSET` | `+05:30` | Fixed UTC offset for timestamps (India = IST, no DST). |
| `TZ_LABEL` | `IST` | Label shown next to the timestamp. |

**Finding a package ID:** open the game on Google Play; the URL is
`play.google.com/store/apps/details?id=<THIS>` — e.g. `com.dts.freefireth`.

### Payload shapes
- **Slack:** `{"text": "..."}`
- **Discord:** `{"content": "..."}`
- **Generic:** `{"title": "...", "lines": [...], "text": "..."}`

---

## Output example

```
📊 Google Play Rankings — IN
🕒 2026-07-24 17:00 IST   (vs previous run:  ▲ up · ▼ down · 🆕 new)

*Top Free Games — IN*
   1. Arrow Puzzle: Tap Puzzle Games ⭐4.57   =
   2. Ludo King® ⭐3.98   ▲1
   3. Free Fire MAX ⭐4.34   ▼1
   4. Carrom Pool: Disc Game ⭐4.58   🆕
   ...

*Top Grossing Games — IN*
   1. Free Fire MAX ⭐4.34   =
   2. BGMI: FPS Battle Royale ⭐4.36   ▲2
   ...
```

**Movement** (`▲` up / `▼` down / `🆕` new / `=` unchanged) is computed by
comparing each app to its rank in the **previous run**, read from `history.csv`.
The first ever run has no baseline, so it shows no movement tags.
(Tracked-app lines appear only if you set `TRACK_APPS`.)

---

## History log (Excel-friendly record)

Every run appends the **top 25 of each chart** to `history.csv`, and the
GitHub Action commits the file back to the repo. Over time you get a full daily
record you can open directly in Excel / Google Sheets.

Columns:

| Column | Example | Notes |
|---|---|---|
| `run_at` | `2026-07-24 17:00:03 IST` | |
| `date` / `time` | `2026-07-24` / `17:00` | |
| `country` | `IN` | |
| `collection` | `TOP_FREE` / `GROSSING` | |
| `category` | `GAME` | |
| `rank` | `3` | position this run |
| `prev_rank` | `5` | position last run (blank if new) |
| `change` | `+2` / `-1` / `0` / `NEW` | movement vs last run |
| `app_id` | `com.dts.freefiremax` | |
| `title` | `Free Fire MAX` | |
| `score` | `4.34` | |
| `developer` | `GARENA INTERNATIONAL I` | |

**Track one game over time:** open `history.csv`, filter `app_id` = your game,
and you have its daily rank history (with `change` showing day-over-day movement).
A pivot on `date` × `rank` gives you a trend line. Disable logging by setting
`HISTORY_FILE` to `""`.

> **Note:** the CSV now includes `prev_rank` and `change` columns. If you have an
> old `history.csv` from a previous version (without these columns), delete it so
> the tool writes a fresh file with the new header.

> The workflow needs `permissions: contents: write` (already set) so it can push
> the updated CSV. Nothing else to configure.

## If it stops working

The chart comes from an **unofficial** Google endpoint. If Google changes its
format, the fetch may return empty and the run will fail (you'll see it in the
Actions log). The request template lives in `chart_payload.txt`, mirrored from
the [`google-play-scraper`](https://github.com/facundoolano/google-play-scraper)
project. To refresh it:

```bash
npm install google-play-scraper
# copy the body template out of node_modules/google-play-scraper/lib/list.js
# (the big `f.req=...` string) into chart_payload.txt
```

The parse paths in `main.py` (`get_chart`) mirror that library's `list.js`; if
they drift, update them to match.

---

## Files
- `main.py` — fetch, format, send. Stdlib only.
- `chart_payload.txt` — the (large) request body template for the chart endpoint.
- `.github/workflows/daily.yml` — the daily schedule + config.
