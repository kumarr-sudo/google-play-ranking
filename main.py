#!/usr/bin/env python3
"""
Google Play + Apple App Store game-ranking -> webhook.

Fetches one or more "top charts" (any mix of Top Free / Top Grossing / Top Paid
x category) for a country from Google Play and/or the Apple App Store, and for each:
  * posts the top N with movement tags (up / down / new vs the previous run),
  * optionally reports the rank of specific apps you care about (TRACK_APPS /
    APPSTORE_TRACK_APPS),
  * appends the top HISTORY_DEPTH to a CSV log (with prev_rank + change).

Movement is computed by comparing each app to its rank in the previous run,
read back from the history CSV -- so history must be enabled for movement tags.

No API key, no paid service.
  * Google Play: Google's own web endpoint (the same one the
    `google-play-scraper` project uses) -- free but unofficial.
  * App Store: Apple's public iTunes RSS top-chart feed (max 100 per chart),
    plus the iTunes Lookup API for star ratings.
See README "If it stops working".

Run locally with no WEBHOOK_URL set to just print the charts (diagnostic mode).
"""

import csv
import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

# --------------------------------------------------------------------------
# Config (all via environment variables; sensible defaults for local testing)
# --------------------------------------------------------------------------
WEBHOOK_URL  = os.environ.get("WEBHOOK_URL", "").strip()
WEBHOOK_TYPE = os.environ.get("WEBHOOK_TYPE", "auto").strip().lower()  # slack|discord|generic|seatalk|auto


def _parse_charts(value):
    return [tuple(p.strip().upper() for p in c.split(":")) for c in value.split(",") if c.strip()]


# Charts to post, comma-separated "COLLECTION:CATEGORY" pairs. Each is its own section.
#   COLLECTION: TOP_FREE | TOP_PAID | GROSSING
#   CATEGORY:   GAME (all games), or a sub-category like GAME_ACTION
# Set to "" to skip that store entirely.
CHARTS          = _parse_charts(os.environ.get("CHARTS", "TOP_FREE:GAME,GROSSING:GAME"))
APPSTORE_CHARTS = _parse_charts(os.environ.get("APPSTORE_CHARTS", "TOP_FREE:GAME,GROSSING:GAME"))
COUNTRY      = os.environ.get("COUNTRY", "us").strip().lower()
LANG         = os.environ.get("LANG_", os.environ.get("LANG", "en")).strip().lower()
TOP_N        = int(os.environ.get("TOP_N", "10"))
# How deep to fetch when locating tracked apps (bigger = can find lower ranks).
RANK_DEPTH   = int(os.environ.get("RANK_DEPTH", "200"))
# Comma-separated package IDs to report the rank of, e.g. "com.dts.freefiremax,com.mobile.legends"
TRACK_APPS   = [p.strip() for p in os.environ.get("TRACK_APPS", "").split(",") if p.strip()]
# App Store apps to track: numeric App Store IDs or bundle IDs, e.g. "6449933265,com.garena.game.ffi"
APPSTORE_TRACK_APPS = [p.strip() for p in os.environ.get("APPSTORE_TRACK_APPS", "").split(",") if p.strip()]

# History log: appends top HISTORY_DEPTH apps per category to a CSV each run.
# Set HISTORY_FILE="" to disable. TZ_OFFSET/TZ_LABEL stamp the local time (India = +05:30 IST).
HISTORY_FILE  = os.environ.get("HISTORY_FILE", "history.csv").strip()
APPSTORE_HISTORY_FILE = os.environ.get("APPSTORE_HISTORY_FILE", "history_appstore.csv").strip()
HISTORY_DEPTH = int(os.environ.get("HISTORY_DEPTH", "25"))
TZ_OFFSET     = os.environ.get("TZ_OFFSET", "+05:30").strip()
TZ_LABEL      = os.environ.get("TZ_LABEL", "IST").strip()


def _local_now():
    """Current time in the configured fixed offset (India has no DST)."""
    sign = -1 if TZ_OFFSET.startswith("-") else 1
    hh, mm = (TZ_OFFSET.lstrip("+-") + ":0").split(":")[:2]
    tz = timezone(sign * timedelta(hours=int(hh), minutes=int(mm or 0)))
    return datetime.now(timezone.utc).astimezone(tz)

CLUSTER = {"TOP_FREE": "topselling_free", "TOP_PAID": "topselling_paid", "GROSSING": "topgrossing"}

# Apple iTunes RSS feed names + genre IDs (Games = 6014, sub-genres 7001..7019).
APPSTORE_FEED = {"TOP_FREE": "topfreeapplications", "TOP_PAID": "toppaidapplications",
                 "GROSSING": "topgrossingapplications"}
APPSTORE_GENRE = {
    "GAME": 6014, "GAME_ACTION": 7001, "GAME_ADVENTURE": 7002, "GAME_ARCADE": 7003,
    "GAME_BOARD": 7004, "GAME_CARD": 7005, "GAME_CASINO": 7006, "GAME_DICE": 7007,
    "GAME_EDUCATIONAL": 7008, "GAME_FAMILY": 7009, "GAME_MUSIC": 7011, "GAME_PUZZLE": 7012,
    "GAME_RACING": 7013, "GAME_ROLE_PLAYING": 7014, "GAME_SIMULATION": 7015,
    "GAME_SPORTS": 7016, "GAME_STRATEGY": 7017, "GAME_TRIVIA": 7018, "GAME_WORD": 7019,
}
APPSTORE_MAX = 100   # Apple's RSS feed caps every chart at 100 entries.

_HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(_HERE, "chart_payload.txt"), encoding="utf-8") as _f:
    _BODY_TEMPLATE = _f.read()


# --------------------------------------------------------------------------
# Fetch + parse the chart
# --------------------------------------------------------------------------
def get_chart(collection, category, num, lang, country):
    """Return a list of {rank, appId, title, developer, score, url}."""
    if collection not in CLUSTER:
        raise ValueError(f"COLLECTION must be one of {list(CLUSTER)}, got {collection!r}")

    body = (_BODY_TEMPLATE
            .replace("${num}", str(num))
            .replace("${collection}", CLUSTER[collection])
            .replace("${category}", category)).encode("utf-8")

    url = ("https://play.google.com/_/PlayStoreUi/data/batchexecute"
           "?rpcids=vyAe2&source-path=%2Fstore%2Fapps&f.sid=-4178618388443751758"
           "&bl=boq_playuiserver_20220612.08_p0&authuser=0&soc-app=121"
           "&soc-platform=1&soc-device=1&_reqid=82003&rt=c"
           f"&hl={lang}&gl={country}")

    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        text = resp.read().decode("utf-8")

    # Response is Google's ")]}'"-prefixed chunked JSON; the payload is on line 4.
    envelope = json.loads(text.split("\n")[3])
    data = json.loads(envelope[0][2])
    apps_node = data[0][1][0][28][0]

    out = []
    for i, a in enumerate(apps_node, 1):
        core = a[0]
        out.append({
            "rank": i,
            "appId": core[0][0],
            "title": core[3],
            "developer": core[14],
            "score": round(core[4][1], 2) if core[4] and core[4][1] is not None else None,
            "url": "https://play.google.com" + core[10][4][2],
        })
    return out


def _get_json(url, timeout=60):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_appstore_chart(collection, category, num, country):
    """Return a list of {rank, appId, bundleId, title, developer, score, price, url}."""
    if collection not in APPSTORE_FEED:
        raise ValueError(f"COLLECTION must be one of {list(APPSTORE_FEED)}, got {collection!r}")
    genre = int(category) if category.isdigit() else APPSTORE_GENRE.get(category)
    if genre is None:
        raise ValueError(f"Unknown App Store CATEGORY {category!r}; use one of "
                         f"{list(APPSTORE_GENRE)} or a numeric genre ID")

    num = min(num, APPSTORE_MAX)
    url = (f"https://itunes.apple.com/{country}/rss/{APPSTORE_FEED[collection]}"
           f"/limit={num}/genre={genre}/json")
    entries = _get_json(url).get("feed", {}).get("entry", [])
    if isinstance(entries, dict):          # a 1-entry feed comes back as an object
        entries = [entries]

    out = []
    for i, e in enumerate(entries, 1):
        ids = e["id"]["attributes"]
        out.append({
            "rank": i,
            "appId": ids["im:id"],
            "bundleId": ids.get("im:bundleId", ""),
            "title": e["im:name"]["label"],
            "developer": e["im:artist"]["label"],
            "score": None,
            "price": e.get("im:price", {}).get("label", ""),
            "url": e["id"]["label"].split("?")[0],
        })

    # The RSS feed has no ratings; fill them in from the Lookup API (best effort).
    if out:
        try:
            ids = ",".join(a["appId"] for a in out)
            looked = _get_json(f"https://itunes.apple.com/lookup?id={ids}&country={country}", 30)
            scores = {str(r.get("trackId")): r.get("averageUserRating")
                      for r in looked.get("results", [])}
            for a in out:
                s = scores.get(a["appId"])
                a["score"] = round(s, 2) if s is not None else None
        except (urllib.error.URLError, ValueError, TimeoutError) as exc:
            print(f"  (App Store ratings lookup failed: {exc}; continuing without ⭐)",
                  file=sys.stderr)
    return out


# Per-store settings, so the rest of the pipeline is store-agnostic.
STORES = {
    "PLAY": {
        "label": "Google Play", "charts": CHARTS, "track": TRACK_APPS,
        "history": HISTORY_FILE, "max_depth": None,
        "fetch": lambda col, cat, num: get_chart(col, cat, num, LANG, COUNTRY),
    },
    "APPSTORE": {
        "label": "App Store", "charts": APPSTORE_CHARTS, "track": APPSTORE_TRACK_APPS,
        "history": APPSTORE_HISTORY_FILE, "max_depth": APPSTORE_MAX,
        "fetch": lambda col, cat, num: get_appstore_chart(col, cat, num, COUNTRY),
    },
}


# --------------------------------------------------------------------------
# Message formatting
# --------------------------------------------------------------------------
def collection_label(collection):
    nice = {"TOP_FREE": "Top Free", "TOP_PAID": "Top Paid", "GROSSING": "Top Grossing"}
    return nice.get(collection, collection)


def category_label(category):
    # "GAME" -> "Games"; "GAME_ACTION" -> "Action"
    if category == "GAME":
        return "Games"
    return category.replace("GAME_", "").replace("_", " ").title()


def read_prev_ranks(history_file, collection, category):
    """{app_id: rank} from the most recent prior run of this chart (for movement)."""
    if not history_file or not os.path.isfile(history_file):
        return {}
    rows = []
    with open(history_file, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if (r.get("country") == COUNTRY.upper()
                    and r.get("collection") == collection
                    and r.get("category") == category):
                rows.append(r)
    if not rows:
        return {}
    last_run = max(r["run_at"] for r in rows)
    return {r["app_id"]: int(r["rank"]) for r in rows if r["run_at"] == last_run}


def movement(prev_map, app):
    """Return (tag_for_message, change_for_csv, prev_rank_for_csv)."""
    if not prev_map:                       # no baseline yet (first ever run)
        return "", "", ""
    prev = prev_map.get(app["appId"])
    if prev is None:                       # not in the previous run
        return "🆕", "NEW", ""
    d = prev - app["rank"]                 # positive = moved up
    if d > 0:
        return f"▲{d}", f"+{d}", prev
    if d < 0:
        return f"▼{-d}", f"{d}", prev
    return "=", "0", prev


def section_header(store, collection, category):
    # The message title already names the store, so sections stay short.
    return f"*{collection_label(collection)} {category_label(category)} — {COUNTRY.upper()}*"


def section_lines(store, collection, category, chart, prev_map):
    """Return the text lines for one chart section, with movement tags."""
    lines = [section_header(store, collection, category)]

    if store["track"]:
        by_id = {a["appId"]: a for a in chart}
        by_id.update({a["bundleId"]: a for a in chart if a.get("bundleId")})
        for pkg in store["track"]:
            a = by_id.get(pkg)
            if a:
                tag, _, _ = movement(prev_map, a)
                tag = f"  {tag}" if tag else ""
                lines.append(f"  • #{a['rank']}  {a['title']}  (⭐{a['score']}){tag}")
            else:
                lines.append(f"  • —   {pkg}  (not in top {len(chart)})")
        lines.append("  —")

    for a in chart[:TOP_N]:
        star = f" ⭐{a['score']}" if a["score"] is not None else ""
        price = f" ({a['price']})" if collection == "TOP_PAID" and a.get("price") else ""
        tag, _, _ = movement(prev_map, a)
        tag = f"   {tag}" if tag else ""
        lines.append(f"  {a['rank']:>2}. {a['title']}{price}{star}{tag}")

    return lines


def append_history(history_file, now, collection, category, chart, prev_map):
    """Append the top HISTORY_DEPTH apps of one chart to the CSV log, with movement."""
    if not history_file:
        return
    header = ["run_at", "date", "time", "country", "collection", "category",
              "rank", "prev_rank", "change", "app_id", "title", "score", "developer"]
    # App Store rows also carry the bundle ID (e.g. com.garena.game.ffi) for readability.
    has_bundle = "bundleId" in (chart[0] if chart else {})
    if has_bundle:
        header.append("bundle_id")
    exists = os.path.isfile(history_file)
    with open(history_file, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(header)
        for a in chart[:HISTORY_DEPTH]:
            _, change, prev_rank = movement(prev_map, a)
            row = [
                now.strftime("%Y-%m-%d %H:%M:%S ") + TZ_LABEL,
                now.strftime("%Y-%m-%d"), now.strftime("%H:%M"),
                COUNTRY.upper(), collection, category,
                a["rank"], prev_rank, change, a["appId"], a["title"], a["score"], a["developer"],
            ]
            if has_bundle:
                row.append(a["bundleId"])
            w.writerow(row)


def _discord_chunks(text, limit=1900):
    """Split on line boundaries so each Discord message stays under its 2000-char cap."""
    chunks, cur = [], ""
    for ln in text.split("\n"):
        if cur and len(cur) + len(ln) + 1 > limit:
            chunks.append(cur)
            cur = ""
        cur = f"{cur}\n{ln}" if cur else ln
    if cur:
        chunks.append(cur)
    return chunks


def _post_json(payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        WEBHOOK_URL, data=data, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def send_webhook(title, lines):
    text = title + "\n" + "\n".join(lines)

    wtype = WEBHOOK_TYPE
    if wtype == "auto":
        if "hooks.slack.com" in WEBHOOK_URL:
            wtype = "slack"
        elif "discord.com/api/webhooks" in WEBHOOK_URL or "discordapp.com/api/webhooks" in WEBHOOK_URL:
            wtype = "discord"
        elif "seatalk.io" in WEBHOOK_URL:
            wtype = "seatalk"
        else:
            wtype = "generic"

    if wtype == "discord":
        # Discord uses ** for bold, not *; with both stores the post can exceed
        # Discord's length cap, so send it as several messages if needed.
        status = None
        for chunk in _discord_chunks(text.replace("*", "**")):
            status, _ = _post_json({"content": chunk})
        return status

    if wtype == "slack":
        payload = {"text": text}
    elif wtype == "seatalk":
        # SeaTalk group bot: markdown message. Standard markdown uses ** for bold.
        payload = {"tag": "markdown", "markdown": {"content": text.replace("*", "**")}}
    else:  # generic: send structured JSON so a custom endpoint can render it itself
        payload = {"title": title, "lines": lines, "text": text}

    status, body = _post_json(payload)
    # SeaTalk returns HTTP 200 with {"code":0} on success, non-zero on error.
    if wtype == "seatalk":
        try:
            code = json.loads(body).get("code")
            if code not in (0, None):
                raise RuntimeError(f"SeaTalk rejected the message: {body}")
        except json.JSONDecodeError:
            pass
    return status


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    now = _local_now()
    active = [s for s in STORES.values() if s["charts"]]
    if not active:
        raise RuntimeError("No charts configured — set CHARTS and/or APPSTORE_CHARTS.")
    stamp = now.strftime("%Y-%m-%d %H:%M ") + TZ_LABEL

    failures = []
    # Each store is posted as its own webhook message.
    for store in active:
        title = f"📊 {store['label']} Rankings — {COUNTRY.upper()}"
        lines = [f"🕒 {stamp}   (vs previous run:  ▲ up · ▼ down · 🆕 new)", ""]
        depth = max(RANK_DEPTH if store["track"] else 0, TOP_N,
                    HISTORY_DEPTH if store["history"] else 0, 1)
        if store["max_depth"]:
            depth = min(depth, store["max_depth"])
        for i, (collection, category) in enumerate(store["charts"]):
            name = f"{store['label']} {collection_label(collection)} {category_label(category)}"
            print(f"Fetching {name} ({COUNTRY.upper()}, depth={depth}) ...", file=sys.stderr)
            if i:
                lines.append("")   # blank line between sections
            # Read the previous run's ranks BEFORE appending this run's rows.
            prev_map = read_prev_ranks(store["history"], collection, category)
            try:
                chart = store["fetch"](collection, category, depth)
                if not chart:
                    raise RuntimeError("chart came back empty — the store may have changed its format")
            except Exception as exc:   # one broken chart shouldn't block the others
                print(f"  ERROR fetching {name}: {exc}", file=sys.stderr)
                failures.append(name)
                lines.append(section_header(store, collection, category))
                lines.append("  ⚠️ could not fetch this chart today")
                continue
            lines.extend(section_lines(store, collection, category, chart, prev_map))
            append_history(store["history"], now, collection, category, chart, prev_map)

        # Always echo to console (diagnostic + GitHub Actions log).
        print(title)
        for ln in lines:
            print(ln)
        print()
        if store["history"]:
            print(f"[history: appended top {HISTORY_DEPTH}/chart to {store['history']}]",
                  file=sys.stderr)

        if not WEBHOOK_URL:
            print(f"[no WEBHOOK_URL set — diagnostic mode, {store['label']} message not sent]",
                  file=sys.stderr)
            continue
        try:
            status = send_webhook(title, lines)
            print(f"{store['label']} message: webhook responded HTTP {status}", file=sys.stderr)
        except Exception as exc:   # still try to send the other store's message
            print(f"  ERROR sending {store['label']} message: {exc}", file=sys.stderr)
            failures.append(f"{store['label']} webhook")

    if failures:
        # Fail the run (red in GitHub Actions) after posting whatever did work.
        sys.exit(f"Failed: {', '.join(failures)}")


if __name__ == "__main__":
    main()
