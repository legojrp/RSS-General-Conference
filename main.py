import os
import json
import logging
import random
from datetime import datetime

import requests
from flask import Flask, Response, render_template
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
from feedgen.feed import FeedGenerator

API_BASE = "https://www.openscriptureapi.org/api/conference/v1/lds/en"
BASE_DATA_FILE = os.path.join(os.path.dirname(__file__), "gc_data.json")
RUNTIME_DATA_FILE = os.environ.get("GC_DATA_FILE", os.path.join("/tmp", "gc_data.json"))
DEFAULT_TIMEZONE = "America/New_York"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("gc-rss")

app = Flask(__name__)


def get_data_file():
    if os.path.exists(RUNTIME_DATA_FILE):
        return RUNTIME_DATA_FILE
    if os.access(os.path.dirname(BASE_DATA_FILE) or ".", os.W_OK):
        return BASE_DATA_FILE
    return RUNTIME_DATA_FILE


def load_data():
    for path in (get_data_file(), BASE_DATA_FILE):
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                log.exception("Failed to load data file: %s", path)
    return {"talk_ids": [], "index": 0, "current": None}


def save_data(d):
    data_file = get_data_file()
    os.makedirs(os.path.dirname(data_file), exist_ok=True)
    with open(data_file, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def get_local_timezone():
    tz_name = os.environ.get("TZ", DEFAULT_TIMEZONE).lstrip(":") or DEFAULT_TIMEZONE
    try:
        return pytz.timezone(tz_name)
    except Exception:
        log.warning("Invalid TZ %s, falling back to %s", tz_name, DEFAULT_TIMEZONE)
        return pytz.timezone(DEFAULT_TIMEZONE)


def should_refresh_today(data):
    item = data.get("current")
    last_updated = data.get("last_updated")
    talk_ids = data.get("talk_ids") or []
    if not item or not last_updated or not talk_ids:
        return True

    try:
        local_tz = get_local_timezone()
        now = datetime.now(local_tz)
        refresh_time = now.replace(hour=8, minute=0, second=0, microsecond=0)
        updated_at = datetime.fromisoformat(last_updated.replace("Z", "+00:00")).astimezone(local_tz)
        return now >= refresh_time and updated_at < refresh_time
    except Exception:
        log.exception("Failed to evaluate refresh state")
        return True


def log_feed_state(prefix, data):
    current = data.get("current") or {}
    log.info(
        "%s current_id=%s session=%s speaker=%s last_updated=%s index=%s talk_count=%s",
        prefix,
        current.get("id"),
        current.get("session"),
        current.get("speaker"),
        data.get("last_updated"),
        data.get("index"),
        len(data.get("talk_ids") or []),
    )


def is_feed_ready(data):
    item = data.get("current")
    if not item:
        return False

    required_fields = ("id", "title", "speaker", "session", "api_url")
    if any(not item.get(field) for field in required_fields):
        return False

    last_updated = data.get("last_updated")
    if not last_updated:
        return False

    try:
        local_tz = get_local_timezone()
        now = datetime.now(local_tz)
        refresh_time = now.replace(hour=8, minute=0, second=0, microsecond=0)
        updated_at = datetime.fromisoformat(last_updated.replace("Z", "+00:00")).astimezone(local_tz)
        return now < refresh_time or updated_at >= refresh_time
    except Exception:
        log.exception("Failed to verify feed readiness")
        return False


def ensure_feed_current(trigger="request"):
    data = load_data()
    log_feed_state(f"Feed check ({trigger}) before refresh", data)
    if should_refresh_today(data):
        log.info("Feed check (%s) requires refresh", trigger)
        select_next_talk()
        data = load_data()
        log_feed_state(f"Feed check ({trigger}) after refresh", data)
    else:
        log.info("Feed check (%s) is already current", trigger)
    if not is_feed_ready(data):
        log.error("Feed state is not ready after refresh")
        return None
    local_tz = get_local_timezone()
    now = datetime.now(local_tz)
    refresh_time = now.replace(hour=8, minute=0, second=0, microsecond=0)
    updated_at = datetime.fromisoformat(data["last_updated"].replace("Z", "+00:00")).astimezone(local_tz)
    log.info(
        "Feed ready (%s): now=%s refresh_time=%s updated_at=%s on_time=%s",
        trigger,
        now.isoformat(),
        refresh_time.isoformat(),
        updated_at.isoformat(),
        updated_at >= refresh_time,
    )
    return data


def build_talk_index():
    """Build a list of talk IDs from conferences within the past 10 years."""
    log.info("Building talk index")
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
        log_feed_state("Selecting next talk before update", data)
        if not data.get("talk_ids"):
            log.info("Talk index missing, building before selection")
            data["talk_ids"] = build_talk_index()
        if not data.get("talk_ids"):
            log.warning("No talk ids available to select")
            return
        talk_id = random.choice(data["talk_ids"])
        index = data["talk_ids"].index(talk_id)
        log.info("Randomly selected talk candidate %s at index %d of %d", talk_id, index, len(data["talk_ids"]))
        talk = fetch_talk(talk_id)
        api_url = f"{API_BASE}/talk/{talk_id}"
        data["current"] = {
            "id": talk.get("_id"),
            "title": talk.get("title"),
            "speaker": talk.get("speaker"),
            "role": talk.get("role"),
            "conferenceId": talk.get("conferenceId"),
            "session": talk.get("session"),
            "api_url": api_url,
            "content": talk.get("content", {}),
        }
        data["index"] = (data.get("index", 0) + 1) % len(data["talk_ids"])
        data["last_updated"] = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
        save_data(data)
        log_feed_state("Selected talk saved", data)
    except Exception:
        log.exception("Error selecting next talk")


def make_rss(item):
    fg = FeedGenerator()
    fg.title("General Conference Daily")
    fg.link(href="https://ldsrss.patchindustries.com", rel="alternate")
    fg.description("A daily General Conference article")
    fg.language("en")

    fe = fg.add_entry()
    api_url = item.get("api_url") or f"{API_BASE}/talk/{item.get('id', '')}"
    if item.get("id"):
        fe.id(api_url)
    title = item.get("title") or "General Conference Talk"
    session = item.get("session")
    speaker = item.get("speaker")
    if session and speaker:
        fe.title(f"{session}: {title} — {speaker}")
    elif session:
        fe.title(f"{session}: {title}")
    elif speaker:
        fe.title(f"{title} — {speaker}")
    else:
        fe.title(title)

    fe.link(href=api_url)

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
    log.info("/feed.xml requested")
    data = ensure_feed_current(trigger="request")
    if not data:
        log.error("/feed.xml request could not produce a current feed")
        return Response("Feed is temporarily unavailable", status=503)
    item = data.get("current")
    if item and item.get("id") and (not item.get("session") or not item.get("api_url")):
        try:
            log.info("Hydrating stale feed metadata for talk %s", item["id"])
            live_talk = fetch_talk(item["id"])
            item = dict(item)
            item["session"] = live_talk.get("session", item.get("session"))
            item["speaker"] = live_talk.get("speaker", item.get("speaker"))
            item["api_url"] = f"{API_BASE}/talk/{item['id']}"
        except Exception:
            log.exception("Failed to hydrate current talk metadata")
            return Response("Feed is temporarily unavailable", status=503)
    # attach last_updated to item for pubDate
    if item and data.get("last_updated"):
        item = dict(item)
        item["last_updated"] = data.get("last_updated")
    if not item:
        log.error("/feed.xml request had no item after refresh")
        return Response("", status=204)
    log.info(
        "/feed.xml serving talk id=%s session=%s speaker=%s api_url=%s",
        item.get("id"),
        item.get("session"),
        item.get("speaker"),
        item.get("api_url"),
    )
    rss = make_rss(item)
    return Response(rss, mimetype="application/rss+xml; charset=utf-8")


@app.route("/")
def index():
    github_url = os.environ.get("GITHUB_URL", "https://github.com/legojrp/RSS-General-Conference")
    feed_url = os.environ.get("FEED_URL", "https://ldsrss.patchindustries.com/feed.xml")
    openscripture_url = "https://www.openscriptureapi.org/docs/general-conference"
    return render_template(
        "index.html",
        feed_url=feed_url,
        github_url=github_url,
        openscripture_url=openscripture_url,
    )


def start_scheduler():
    scheduler = BackgroundScheduler()
    # Schedule at 8:00 AM local time every day
    scheduler.add_job(select_next_talk, CronTrigger(hour=8, minute=0, timezone=get_local_timezone()))
    scheduler.start()
    log.info("Scheduler started for 8:00 AM local time")
    return scheduler


if __name__ == "__main__":
    # Ensure talk index exists and pick initial talk
    try:
        log.info("Starting General Conference RSS server")
        data = load_data()
        if not data.get("talk_ids"):
            log.info("No cached talk index found at startup")
            data["talk_ids"] = build_talk_index()
            save_data(data)
        # select today's talk immediately on startup
        log.info("Selecting initial talk at startup")
        select_next_talk()
    except Exception:
        log.exception("Startup initialization failed")

    start_scheduler()
    log.info("Serving on 0.0.0.0:%s", os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
