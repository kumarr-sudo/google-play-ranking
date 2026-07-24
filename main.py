#!/usr/bin/env python3
"""
Google Play game-ranking -> webhook.

Fetches one or more Google Play "top charts" (any mix of Top Free / Top Grossing
/ Top Paid x category) for a country, and for each:
  * posts the top N with movement tags (up / down / new vs the previous run),
  * optionally reports the rank of specific apps you care about (TRACK_APPS),
  * appends the top HISTORY_DEPTH to a CSV log (with prev_rank + change).

Movement is computed by comparing each app to its rank in the previous run,
read back from the history CSV -- so history must be enabled for movement tags.

No API key, no paid service. Data comes from Google Play's own web endpoint
(the same one the `google-play-scraper` project uses), so it is free but
unofficial -- see README "If it stops working".

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
# Charts to post, comma-separated "COLLECTION:CATEGORY" pairs. Each is its own section.
#   COLLECTION: TOP_FREE | TOP_PAID | GROSSING
#   CATEGORY:   GAME (all games), or a sub-category like GAME_ACTION
CHARTS       = [tuple(p.strip().upper() for p in c.split(":"))
                for c in os.environ.get("CHARTS", "TOP_FREE:GAME,GROSSING:GAME").split(",") if c.strip()]
COUNTRY      = os.environ.get("COUNTRY", "us").strip().lower()
LANG         = os.environ.get("LANG_", os.environ.get("LANG", "en")).strip().lower()
TOP_N        = int(os.environ.get("TOP_N", "10"))
# How deep to fetch when locating tracked apps (bigger = can find lower ranks).
RANK_DEPTH   = int(os.environ.get("RANK_DEPTH", "200"))
# Comma-separated package IDs to report the rank of, e.g. "com.dts.freefiremax,com.mobile.legends"
TRACK_APPS   = [p.strip() for p in os.environ.get("TRACK_APPS", "").split(",") if p.strip()]

# History log: appends top HISTORY_DEPTH apps per category to a CSV each run.
# Set HISTORY_FILE="" to disable. TZ_OFFSET/TZ_LABEL stamp the local time (India = +05:30 IST).
HISTORY_FILE  = os.environ.get("HISTORY_FILE", "history.csv").strip()
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


def read_prev_ranks(collection, category):
    """{app_id: rank} from the most recent prior run of this chart (for movement)."""
    if not HISTORY_FILE or not os.path.isfile(HISTORY_FILE):
        return {}
    rows = []
    with open(HISTORY_FILE, newline="", encoding="utf-8") as f:
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


def section_lines(collection, category, chart, prev_map):
    """Return the text lines for one chart section, with movement tags."""
    lines = [f"*{collection_label(collection)} {category_label(category)} — {COUNTRY.upper()}*"]

    if TRACK_APPS:
        by_id = {a["appId"]: a for a in chart}
        for pkg in TRACK_APPS:
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
        tag, _, _ = movement(prev_map, a)
        tag = f"   {tag}" if tag else ""
        lines.append(f"  {a['rank']:>2}. {a['title']}{star}{tag}")

    return lines


def append_history(now, collection, category, chart, prev_map):
    """Append the top HISTORY_DEPTH apps of one chart to the CSV log, with movement."""
    if not HISTORY_FILE:
        return
    header = ["run_at", "date", "time", "country", "collection", "category",
              "rank", "prev_rank", "change", "app_id", "title", "score", "developer"]
    exists = os.path.isfile(HISTORY_FILE)
    with open(HISTORY_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(header)
        for a in chart[:HISTORY_DEPTH]:
            _, change, prev_rank = movement(prev_map, a)
            w.writerow([
                now.strftime("%Y-%m-%d %H:%M:%S ") + TZ_LABEL,
                now.strftime("%Y-%m-%d"), now.strftime("%H:%M"),
                COUNTRY.upper(), collection, category,
                a["rank"], prev_rank, change, a["appId"], a["title"], a["score"], a["developer"],
            ])


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

    if wtype == "slack":
        payload = {"text": text}
    elif wtype == "discord":
        # Discord uses ** for bold, not *; keep it simple and readable.
        payload = {"content": text.replace("*", "**")[:1900]}
    elif wtype == "seatalk":
        # SeaTalk group bot: markdown message. Standard markdown uses ** for bold.
        payload = {"tag": "markdown", "markdown": {"content": text.replace("*", "**")}}
    else:  # generic: send structured JSON so a custom endpoint can render it itself
        payload = {"title": title, "lines": lines, "text": text}

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        WEBHOOK_URL, data=data, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8", "replace")
    # SeaTalk returns HTTP 200 with {"code":0} on success, non-zero on error.
    if wtype == "seatalk":
        try:
            code = json.loads(body).get("code")
            if code not in (0, None):
                raise RuntimeError(f"SeaTalk rejected the message: {body}")
        except json.JSONDecodeError:
            pass
    return resp.status


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    now = _local_now()
    depth = max(RANK_DEPTH if TRACK_APPS else 0, TOP_N, HISTORY_DEPTH if HISTORY_FILE else 0, 1)
    title = f"📊 Google Play Rankings — {COUNTRY.upper()}"
    stamp = now.strftime("%Y-%m-%d %H:%M ") + TZ_LABEL

    lines = [f"🕒 {stamp}   (vs previous run:  ▲ up · ▼ down · 🆕 new)", ""]
    for i, (collection, category) in enumerate(CHARTS):
        print(f"Fetching {collection_label(collection)} {category_label(category)} "
              f"({COUNTRY.upper()}, depth={depth}) ...", file=sys.stderr)
        # Read the previous run's ranks BEFORE appending this run's rows.
        prev_map = read_prev_ranks(collection, category)
        chart = get_chart(collection, category, depth, LANG, COUNTRY)
        if not chart:
            raise RuntimeError(f"Chart {collection}:{category} came back empty — "
                               "Google may have changed their format.")
        if i:
            lines.append("")   # blank line between sections
        lines.extend(section_lines(collection, category, chart, prev_map))
        append_history(now, collection, category, chart, prev_map)

    # Always echo to console (diagnostic + GitHub Actions log).
    print(title)
    for ln in lines:
        print(ln)
    if HISTORY_FILE:
        print(f"\n[history: appended top {HISTORY_DEPTH}/chart to {HISTORY_FILE}]",
              file=sys.stderr)

    if not WEBHOOK_URL:
        print("\n[no WEBHOOK_URL set — diagnostic mode, nothing sent]", file=sys.stderr)
        return

    status = send_webhook(title, lines)
    print(f"\nWebhook responded HTTP {status}", file=sys.stderr)


if __name__ == "__main__":
    main()
