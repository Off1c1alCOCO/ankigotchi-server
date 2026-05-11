"""
Ankigotchi local development server.

Endpoints:
  GET  /                     — health check (JSON)
  GET  /app                  — serves the web Ankigotchi page (HTML)
  POST /reviews              — Anki add-on submits a single review-count event
  POST /reviews/backfill     — Bulk historical events (dev/testing only now)
  GET  /pet/<user_id>        — computed pet state for a user

Design notes:
  - Pet state is computed on demand from the raw event log (reviews.jsonl).
  - Events from before a user's `started_at` are filtered out.
  - Future reward features (XP, badges, prizes) should be derived from
    the same event log — don't add new "state" tables, add new queries.

Run locally:
    python server.py
On Railway (or any other host that sets PORT):
    Same command. The code below auto-detects environment via PORT.
"""

import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, request, jsonify, send_from_directory


app = Flask(__name__)

PROJECT_DIR = Path(__file__).parent
DATA_FILE = PROJECT_DIR / "reviews.jsonl"


# ---------------------------------------------------------------------------
# User table
# ---------------------------------------------------------------------------

USERS: dict[str, dict] = {
    "cameron": {
        "started_at": "2026-05-04",
        "daily_goal": 100,
    },
}


def get_user(user_id: str) -> dict | None:
    return USERS.get(user_id)


# ---------------------------------------------------------------------------
# Routes — write side
# ---------------------------------------------------------------------------

@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "service": "ankigotchi-dev-server",
        "status": "running",
        "events_recorded": count_events(),
        "users": list(USERS.keys()),
    })


@app.route("/reviews", methods=["POST"])
def receive_reviews():
    data = request.get_json(silent=True)
    if data is None:
        return jsonify({"error": "expected a JSON body"}), 400

    data["received_at"] = datetime.now(timezone.utc).isoformat()
    print(f"[+] received: {json.dumps(data)}")
    append_event(data)

    return jsonify({"status": "ok", "received": data}), 200


@app.route("/reviews/backfill", methods=["POST"])
def receive_backfill():
    body = request.get_json(silent=True)
    if body is None:
        return jsonify({"error": "expected a JSON body"}), 400

    user_id = body.get("user_id")
    events = body.get("events")

    if not user_id:
        return jsonify({"error": "missing 'user_id'"}), 400
    if not isinstance(events, list):
        return jsonify({"error": "'events' must be a list"}), 400

    received_at = datetime.now(timezone.utc).isoformat()
    stored = 0
    skipped = 0

    for event in events:
        if not isinstance(event, dict):
            skipped += 1
            continue
        if "date" not in event or "total_reviews" not in event:
            skipped += 1
            continue

        record = {
            "user_id": user_id,
            "date": event["date"],
            "total_reviews": event["total_reviews"],
            "unique_cards": event.get("unique_cards", 0),
            "received_at": received_at,
            "source": "backfill",
        }
        append_event(record)
        stored += 1

    print(f"[+] backfill from {user_id}: stored {stored}, skipped {skipped}")
    return jsonify({"status": "ok", "user_id": user_id, "stored": stored, "skipped": skipped}), 200


# ---------------------------------------------------------------------------
# Routes — read side
# ---------------------------------------------------------------------------

@app.route("/pet/<user_id>", methods=["GET"])
def get_pet_state(user_id: str):
    user = get_user(user_id)
    if user is None:
        return jsonify({"error": "user not found", "user_id": user_id}), 404
    return jsonify(compute_pet_state(user_id, user)), 200


# ---------------------------------------------------------------------------
# Routes — web app
# ---------------------------------------------------------------------------

@app.route("/app", methods=["GET"])
def serve_app():
    return send_from_directory(PROJECT_DIR, "index.html")


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

def append_event(event: dict) -> None:
    with DATA_FILE.open("a") as f:
        f.write(json.dumps(event) + "\n")


def count_events() -> int:
    if not DATA_FILE.exists():
        return 0
    with DATA_FILE.open() as f:
        return sum(1 for _ in f)


def load_user_events(user_id: str, started_at: str) -> dict[str, dict]:
    if not DATA_FILE.exists():
        return {}

    by_date: dict[str, dict] = {}
    with DATA_FILE.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("user_id") != user_id:
                continue

            event_date = event.get("date")
            if not event_date:
                continue
            if event_date < started_at:
                continue

            existing = by_date.get(event_date)
            if existing is None or event.get("received_at", "") > existing.get("received_at", ""):
                by_date[event_date] = event

    return by_date


# ---------------------------------------------------------------------------
# Stats computation
# ---------------------------------------------------------------------------

def current_streak(dates_with_reviews: set[str]) -> int:
    today = date.today()
    check_date = today if today.isoformat() in dates_with_reviews else today - timedelta(days=1)
    streak = 0
    while check_date.isoformat() in dates_with_reviews:
        streak += 1
        check_date -= timedelta(days=1)
    return streak


def longest_streak(dates_with_reviews: set[str]) -> int:
    if not dates_with_reviews:
        return 0
    sorted_dates = sorted(date.fromisoformat(d) for d in dates_with_reviews)
    longest = 1
    current = 1
    for i in range(1, len(sorted_dates)):
        if sorted_dates[i] - sorted_dates[i - 1] == timedelta(days=1):
            current += 1
            longest = max(longest, current)
        else:
            current = 1
    return longest


def goal_streak(events_by_date: dict[str, dict], daily_goal: int) -> int:
    today = date.today()
    today_event = events_by_date.get(today.isoformat())
    today_hit = bool(today_event and today_event.get("total_reviews", 0) >= daily_goal)

    check_date = today if today_hit else today - timedelta(days=1)
    streak = 0
    while True:
        ev = events_by_date.get(check_date.isoformat())
        if not ev or ev.get("total_reviews", 0) < daily_goal:
            break
        streak += 1
        check_date -= timedelta(days=1)
    return streak


def total_goal_hits(events_by_date: dict[str, dict], daily_goal: int) -> int:
    return sum(1 for ev in events_by_date.values() if ev.get("total_reviews", 0) >= daily_goal)


def last_n_days(events_by_date: dict[str, dict], n: int = 7) -> list[dict]:
    today = date.today()
    out = []
    for i in range(n - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        ev = events_by_date.get(d)
        out.append({
            "date": d,
            "total_reviews": (ev or {}).get("total_reviews", 0),
        })
    return out


def days_with_pet(started_at: str) -> int:
    start = date.fromisoformat(started_at)
    delta = (date.today() - start).days
    return max(delta + 1, 1)


def compute_pet_state(user_id: str, user: dict) -> dict:
    started_at = user["started_at"]
    daily_goal = user["daily_goal"]

    events_by_date = load_user_events(user_id, started_at)
    dates_with_reviews = {
        d for d, ev in events_by_date.items() if ev.get("total_reviews", 0) > 0
    }

    today_str = date.today().isoformat()
    today_event = events_by_date.get(today_str)
    today_total = (today_event or {}).get("total_reviews", 0)
    today_unique = (today_event or {}).get("unique_cards", 0)

    latest_date = max(events_by_date.keys()) if events_by_date else None
    latest_event = events_by_date.get(latest_date) if latest_date else None

    return {
        "user_id": user_id,
        "started_at": started_at,
        "days_with_pet": days_with_pet(started_at),
        "daily_goal": daily_goal,

        "total_reviews_today": today_total,
        "unique_cards_today": today_unique,
        "goal_hit_today": today_total >= daily_goal,
        "goal_progress_today": min(today_total / daily_goal, 1.0) if daily_goal > 0 else 0,

        "current_streak": current_streak(dates_with_reviews),
        "longest_streak": longest_streak(dates_with_reviews),
        "goal_streak": goal_streak(events_by_date, daily_goal),

        "lifetime_reviews": sum(ev.get("total_reviews", 0) for ev in events_by_date.values()),
        "days_with_reviews": len(dates_with_reviews),
        "total_goal_hits": total_goal_hits(events_by_date, daily_goal),

        "last_7_days": last_n_days(events_by_date, n=7),

        "last_synced_at": latest_event.get("received_at") if latest_event else None,
        "latest_data_date": latest_date,
    }


# ---------------------------------------------------------------------------
# Entry point
#
# Locally (no PORT env var set): listens on 127.0.0.1:8000 with debug=True.
# On Railway / other hosts: uses $PORT and 0.0.0.0, debug off.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    is_hosted = "PORT" in os.environ
    app.run(
        host="0.0.0.0" if is_hosted else "127.0.0.1",
        port=port,
        debug=not is_hosted,
    )