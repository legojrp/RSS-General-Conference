import os
import json
import logging
from datetime import datetime

import requests
from flask import Flask, Response
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
import random
from feedgen.feed import FeedGenerator

API_BASE = "https://www.openscriptureapi.org/api/conference/v1/lds/en"
DATA_FILE = os.path.join(os.path.dirname(__file__), "gc_data.json")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("gc-rss")

app = Flask(__name__)


def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            log.exception("Failed to load data file")
    return {"talk_ids": [], "index": 0, "current": None}


def save_data(d):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def build_talk_index():
    """Build a list of talk IDs from conferences within the past 10 years."""
    talk_ids = []
    limit = 100
    offset = 0
    current_year = datetime.utcnow().year
    start_year = current_year - 10
    while True:
        params = {"limit": limit, "offset": offset, "start_year": start_year}
        url = f"{API_BASE}/conferences"
        log.info("Fetching conferences: %s", params)
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        j = r.json()
        confs = j.get("conferences", [])
        if not confs:
            break
        for c in confs:
            for s in c.get("sessions", []):
                for tid in s.get("talkIds", []):
                    talk_ids.append(tid)
        if len(confs) < limit:
            break
        offset += limit
    # deduplicate while preserving order
    seen = set()
    deduped = []
    for t in talk_ids:
        if t not in seen:
            seen.add(t)
            deduped.append(t)
    log.info("Built talk index with %d ids (past 10 years)", len(deduped))
    return deduped


def fetch_talk(talk_id):
    url = f"{API_BASE}/talk/{talk_id}"
    log.info("Fetching talk %s", talk_id)
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    return r.json()


def select_next_talk():
    try:
        data = load_data()
        if not data.get("talk_ids"):
            data["talk_ids"] = build_talk_index()
        if not data.get("talk_ids"):
            log.warning("No talk ids available to select")
            return
        talk_id = random.choice(data["talk_ids"])
        talk = fetch_talk(talk_id)
        data["current"] = {
            "id": talk.get("_id"),
            "title": talk.get("title"),
            "speaker": talk.get("speaker"),
            "role": talk.get("role"),
            "conferenceId": talk.get("conferenceId"),
            "content": talk.get("content", {}),
        }
        data["last_updated"] = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
        save_data(data)
        log.info("Selected talk %s", talk_id)
    except Exception:
        log.exception("Error selecting next talk")


def make_rss(item):
    fg = FeedGenerator()
    fg.title("General Conference Daily")
    fg.link(href="https://www.openscriptureapi.org", rel="alternate")
    fg.description("A daily General Conference article")
    fg.language("en")

    fe = fg.add_entry()
    fe.id(item["id"]) if item.get("id") else None
    title = item.get("title") or "General Conference Talk"
    speaker = item.get("speaker")
    if speaker:
        fe.title(f"{title} — {speaker}")
    else:
        fe.title(title)

    link = f"https://www.openscriptureapi.org/conference/{item.get('conferenceId', '')}/talk/{item.get('id', '')}"
    fe.link(href=link)

    paragraphs = item.get("content", {}).get("paragraphs", [])
    if paragraphs:
        html = "".join(f"<p>{p.get('text','')}</p>" for p in paragraphs)
    else:
        html = ""
    fe.content(html, type="CDATA")

    pubdate = item.get("last_updated")
    if pubdate:
        try:
            dt = datetime.fromisoformat(pubdate.replace("Z", "+00:00"))
            fe.pubDate(dt)
        except Exception:
            pass

    return fg.rss_str(pretty=True)


@app.route("/feed.xml")
def feed():
    data = load_data()
    item = data.get("current")
    # attach last_updated to item for pubDate
    if item and data.get("last_updated"):
        item = dict(item)
        item["last_updated"] = data.get("last_updated")
    if not item:
        return Response("", status=204)
    rss = make_rss(item)
    return Response(rss, mimetype="application/rss+xml; charset=utf-8")


def start_scheduler():
    scheduler = BackgroundScheduler()
    # Schedule at 8:00 AM local time every day
    scheduler.add_job(select_next_talk, CronTrigger(hour=8, minute=0, timezone=pytz.timezone(os.environ.get("TZ", "UTC"))))
    scheduler.start()
    log.info("Scheduler started")
    return scheduler


if __name__ == "__main__":
    # Ensure talk index exists and pick initial talk
    try:
        data = load_data()
        if not data.get("talk_ids"):
            data["talk_ids"] = build_talk_index()
            save_data(data)
        # select today's talk immediately on startup
        select_next_talk()
    except Exception:
        log.exception("Startup initialization failed")

    start_scheduler()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
