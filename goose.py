import asyncio
import base64
import errno
import json
import logging
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

import anthropic
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from icalendar import Calendar

BASE_DIR = Path(__file__).parent.resolve()

# DATA_DIR: where all runtime JSON and static files live.
# - macOS LaunchAgent: defaults to ~/.goose/data/ (outside iCloud Desktop)
# - Docker:            set DATA_DIR=/data in the container environment
DATA_DIR = Path(os.environ.get("DATA_DIR", str(Path.home() / ".goose" / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Env vars are injected by the LaunchAgent plist or Docker env_file.
# For manual runs, export them in your shell first or copy .env to your session.

CANVAS_ICAL_URL = os.environ["CANVAS_ICAL_URL"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

BRIEFING_FILE  = DATA_DIR / "briefing.json"
# Serve static files from the data dir (never iCloud-evicted).
# The source lives in BASE_DIR/static/ and is copied to DATA_DIR/static/
# at every startup so VS Code edits flow through automatically.
STATIC_SRC_DIR = BASE_DIR / "static"
STATIC_DIR     = DATA_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)   # must exist before StaticFiles mounts
TOKEN_PATH     = DATA_DIR / "token.json"

# ── Live Flight Radar ────────────────────────────────────────────────────────
FLIGHTS_CACHE_FILE = DATA_DIR / "flights_cache.json"
OPENSKY_URL        = "https://opensky-network.org/api/states/all"
FLIGHTS_BOX_DEG    = 1.2   # ~84 miles radius around center point

# ── Behavioral Learning ──────────────────────────────────────────────────────
DAILY_LOG_FILE = DATA_DIR / "daily_log.json"
PATTERNS_FILE  = DATA_DIR / "patterns.json"
CONTEXT_FILE   = DATA_DIR / "context.json"

# ── GroupMe (read-only observer — NEVER posts or sends) ─────────────────────
GROUPME_ACCESS_TOKEN = os.environ.get("GROUPME_ACCESS_TOKEN", "")
GROUPME_GROUP_ID     = "30939626"   # "people" — fraternity group chat
GROUPME_STATE_FILE   = DATA_DIR / "groupme_state.json"
GROUPME_API_BASE     = "https://api.groupme.com/v3"

# ── Spotify ──────────────────────────────────────────────────────────────────
SPOTIFY_CLIENT_ID     = os.environ.get("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
SPOTIFY_REDIRECT_URI  = "http://127.0.0.1:8080/auth/spotify/callback"
SPOTIFY_SCOPES        = (
    "user-read-currently-playing "
    "user-read-playback-state "
    "user-modify-playback-state"
)
SPOTIFY_TOKEN_FILE    = DATA_DIR / "spotify_token.json"
SPOTIFY_NOW_FILE      = DATA_DIR / "spotify_now.json"   # 5-s server-side cache


def compute_urgency(days_until_due: float) -> float:
    return round(1 / max(days_until_due, 0.5), 4)


# ── Weather (Open-Meteo, no API key required) ──────────────────────────────
WEATHER_LAT =  33.4255   # ASU Tempe, AZ
WEATHER_LON = -111.9400

_WMO_LABEL: dict[int, str] = {
    0: "CLEAR SKY",
    1: "MAINLY CLEAR", 2: "PARTLY CLOUDY", 3: "OVERCAST",
    45: "FOG", 48: "RIME FOG",
    51: "LIGHT DRIZZLE", 53: "DRIZZLE", 55: "HEAVY DRIZZLE",
    61: "LIGHT RAIN", 63: "RAIN", 65: "HEAVY RAIN",
    71: "LIGHT SNOW", 73: "SNOW", 75: "HEAVY SNOW",
    77: "SNOW GRAINS",
    80: "RAIN SHOWERS", 81: "SHOWERS", 82: "HEAVY SHOWERS",
    85: "SNOW SHOWERS", 86: "HEAVY SNOW SHOWERS",
    95: "THUNDERSTORM", 96: "THUNDERSTORM + HAIL", 99: "HEAVY THUNDERSTORM",
}

_WIND_DIRS = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
              "S","SSW","SW","WSW","W","WNW","NW","NNW"]


def _wind_label(deg: float) -> str:
    return _WIND_DIRS[round(deg / 22.5) % 16]


def fetch_weather() -> dict:
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={WEATHER_LAT}&longitude={WEATHER_LON}"
        "&current=temperature_2m,apparent_temperature,precipitation,"
        "weather_code,wind_speed_10m,wind_direction_10m"
        "&daily=sunrise,sunset,uv_index_max,precipitation_sum"
        "&temperature_unit=fahrenheit&wind_speed_unit=mph"
        "&precipitation_unit=inch&timezone=America%2FPhoenix&forecast_days=1"
    )
    with urllib.request.urlopen(url, timeout=10) as resp:
        data = json.loads(resp.read())

    cur   = data["current"]
    daily = data["daily"]

    return {
        "temp_f":        round(cur["temperature_2m"]),
        "feels_like_f":  round(cur["apparent_temperature"]),
        "precip_in":     cur["precipitation"],
        "weather_code":  cur["weather_code"],
        "condition":     _WMO_LABEL.get(cur["weather_code"], "UNKNOWN"),
        "wind_mph":      round(cur["wind_speed_10m"]),
        "wind_dir":      _wind_label(cur["wind_direction_10m"]),
        "uv_index":      daily["uv_index_max"][0],
        "precip_today_in": daily["precipitation_sum"][0],
        "sunrise":       daily["sunrise"][0].split("T")[1],   # "HH:MM"
        "sunset":        daily["sunset"][0].split("T")[1],
    }


def fetch_canvas_assignments() -> list[dict]:
    with urllib.request.urlopen(CANVAS_ICAL_URL, timeout=10) as response:
        cal = Calendar.from_ical(response.read())

    today = datetime.now(timezone.utc).date()
    assignments = []

    for component in cal.walk():
        if component.name != "VEVENT":
            continue

        summary = str(component.get("SUMMARY", ""))
        dtstart = component.get("DTSTART")
        if dtstart is None:
            continue

        due = dtstart.dt
        if hasattr(due, "date"):
            due = due.date()

        days_until_due = (due - today).days
        if days_until_due < -1:
            continue

        assignments.append({
            "name": summary,
            "due_date": due.isoformat(),
            "days_until_due": days_until_due,
            "urgency": compute_urgency(days_until_due),
        })

    return sorted(assignments, key=lambda a: a["urgency"], reverse=True)


def fetch_calendar_events() -> list[dict]:
    if not TOKEN_PATH.exists():
        return []

    try:
        token_data = _read_token_json()
        if not token_data:
            return []
        creds = Credentials.from_authorized_user_info(token_data)

        if creds.expired and creds.refresh_token:
            creds.refresh(GoogleAuthRequest())
            raw = creds.to_json().encode()
            fd = os.open(str(TOKEN_PATH), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, raw)
            finally:
                os.close(fd)

        if not creds.valid:
            return []

        service = build("calendar", "v3", credentials=creds)

        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_end = today_start + timedelta(days=1)

        result = service.events().list(
            calendarId="primary",
            timeMin=today_start.isoformat(),
            timeMax=today_end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        ).execute()

        events = []
        for event in result.get("items", []):
            start = event["start"].get("dateTime", event["start"].get("date", ""))
            end = event["end"].get("dateTime", event["end"].get("date", ""))
            events.append({
                "title": event.get("summary", "Busy"),
                "start": start,
                "end": end,
            })

        return events

    except Exception:
        return []


def compute_free_blocks(events: list[dict]) -> list[dict]:
    WORK_START = 8.0
    WORK_END = 22.0

    def to_decimal(dt_str: str) -> float | None:
        if "T" not in dt_str:
            return None
        try:
            dt = datetime.fromisoformat(dt_str)
            return dt.hour + dt.minute / 60
        except ValueError:
            return None

    busy = []
    for event in events:
        s = to_decimal(event["start"])
        e = to_decimal(event["end"])
        if s is not None and e is not None:
            busy.append((s, e))

    busy.sort()

    merged: list[list[float]] = []
    for s, e in busy:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])

    def fmt(h: float) -> str:
        hour = int(h)
        minute = int(round((h % 1) * 60))
        period = "am" if hour < 12 else "pm"
        display = hour if hour <= 12 else hour - 12
        if display == 0:
            display = 12
        return f"{display}:{minute:02d}{period}"

    free = []
    cursor = WORK_START
    for b_start, b_end in merged:
        if cursor < b_start - 0.01:
            duration = b_start - cursor
            if duration >= 0.5:
                free.append({"start": fmt(cursor), "end": fmt(b_start), "hours": round(duration, 1)})
        cursor = max(cursor, b_end)

    if cursor < WORK_END - 0.01:
        duration = WORK_END - cursor
        if duration >= 0.5:
            free.append({"start": fmt(cursor), "end": fmt(WORK_END), "hours": round(duration, 1)})

    return free


def parse_llm_json(text: str) -> dict | None:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    return None


def generate_headline(
    assignments: list[dict],
    free_blocks: list[dict],
    weather: dict | None,
    patterns: dict | None = None,
) -> dict | None:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    weather_line = ""
    if weather:
        weather_line = (
            f"\nCurrent weather: {weather['temp_f']}°F, "
            f"feels {weather['feels_like_f']}°F, {weather['condition']}, "
            f"wind {weather['wind_mph']} mph {weather['wind_dir']}, "
            f"UV index {weather['uv_index']}."
        )

    context      = _read_json(CONTEXT_FILE, {})
    context_line = ""
    if context:
        sched = context.get("schedule", {})
        notes = context.get("notes", [])
        context_line = (
            f"\n\nJackson's current life context:"
            f"\n- Schedule: {sched.get('type','')}, {sched.get('weekday_hours','')} Mon–Thu, {sched.get('friday_hours','')} Fri"
            f"\n- Work mode: {sched.get('work_mode','')} — {sched.get('wfh_days_per_week','')} days WFH per week"
            f"\n- Valid through: {sched.get('valid_through','')}"
            f"\n- Rules: {'; '.join(notes)}"
        )

    patterns_line = ""
    if patterns and isinstance(patterns.get("derived"), dict):
        d = patterns["derived"]
        parts = []
        if d.get("scheduling_notes"):
            parts.append(f"Scheduling rules: {d['scheduling_notes']}")
        if d.get("weather_insight"):
            parts.append(f"Weather pattern: {d['weather_insight']}")
        if d.get("avoid_scheduling"):
            parts.append(f"Avoid: {', '.join(d['avoid_scheduling'])}")
        if d.get("best_scheduling"):
            parts.append(f"Best slots: {', '.join(d['best_scheduling'])}")
        if parts:
            patterns_line = "\n\nLearned behavioral patterns (use these to personalise the recommendation):\n" + "\n".join(parts)

    prompt = f"""You are Goose, a personal co-pilot. Generate a scheduling recommendation based on today's data.

Top assignments (most urgent first):
{json.dumps(assignments[:5], indent=2)}

Free time blocks today:
{json.dumps(free_blocks, indent=2)}
{weather_line}{context_line}{patterns_line}

Respond with ONLY valid JSON, no other text:
{{
  "headline": "Do the 320 problem set between 2–4pm. Essay can wait until tonight.",
  "priority_task": "exact assignment name",
  "suggested_block": "2:00pm–4:00pm"
}}"""

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )

    return parse_llm_json(message.content[0].text)


def _groupme_read_state() -> dict:
    if GROUPME_STATE_FILE.exists():
        try:
            return json.loads(GROUPME_STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_message_id": None, "last_check": None, "summary": None, "events": []}


def _groupme_write_state(state: dict) -> None:
    tmp = GROUPME_STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, GROUPME_STATE_FILE)


def fetch_groupme_messages(since_id: str | None = None) -> list[dict]:
    """Fetch messages from the group chat. Read-only — never posts anything."""
    if not GROUPME_ACCESS_TOKEN:
        return []

    url = (
        f"{GROUPME_API_BASE}/groups/{GROUPME_GROUP_ID}/messages"
        f"?token={GROUPME_ACCESS_TOKEN}&limit=100"
    )
    if since_id:
        url += f"&since_id={since_id}"

    try:
        req = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        return data.get("response", {}).get("messages", [])
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return []   # not modified — no new messages, not an error
        _log.warning("fetch_groupme_messages HTTP error: %s", e)
        return []
    except Exception as e:
        _log.warning("fetch_groupme_messages failed: %s", e)
        return []


def analyze_groupme_messages(messages: list[dict]) -> dict:
    """Use Claude to extract events and summarize new messages."""
    if not messages:
        return {"summary": None, "events": []}

    # Reverse so messages read oldest → newest
    msgs_lines = []
    for m in reversed(messages):
        ts   = datetime.fromtimestamp(m["created_at"]).strftime("%m/%d %I:%M%p")
        name = m.get("name", "Unknown")
        text = (m.get("text") or "").strip()
        if text:
            msgs_lines.append(f"[{ts}] {name}: {text}")

    if not msgs_lines:
        return {"summary": None, "events": []}

    today = datetime.now().strftime("%A, %B %d, %Y")
    block = "\n".join(msgs_lines[-100:])   # cap for token safety

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""You are reading messages from a college fraternity group chat (200 members).
Today is {today}.

Messages (oldest → newest):
{block}

Respond with ONLY valid JSON:
{{
  "summary": "A detailed, conversational summary written like a friend catching you up. Cover the main topics, who said what (by first name if mentioned), any drama or important decisions, upcoming plans, and the general vibe of the chat. Write naturally — no bullet points, no military language, just plain casual English. Aim for 5-8 sentences.",
  "events": [
    {{
      "title": "Event name",
      "date": "YYYY-MM-DD",
      "time": "HH:MM",
      "duration_hours": 2,
      "description": "brief description"
    }}
  ]
}}

Rules:
- summary: write it like you're texting a friend to catch them up. Conversational, specific, detailed.
- events: ONLY include things with a clear date (meeting, party, social, chapter, etc). Empty array if none.
- Resolve relative dates like "Friday" or "this weekend" relative to today ({today}).
- Omit "time" field if no time is mentioned for an event."""

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    return parse_llm_json(message.content[0].text) or {"summary": None, "events": []}


def _read_token_json() -> dict | None:
    """Read token.json using os.open with EDEADLK retry (same pattern as _read_json)."""
    for attempt in range(8):
        try:
            fd = os.open(str(TOKEN_PATH), os.O_RDONLY)
            try:
                return json.loads(os.read(fd, os.fstat(fd).st_size))
            finally:
                os.close(fd)
        except OSError as e:
            if e.errno == errno.EDEADLK and attempt < 7:
                time.sleep(min(2.0, 0.5 * (attempt + 1)))
                continue
            _log.warning("_read_token_json failed after %d attempt(s): %s", attempt + 1, e)
            return None
        except Exception as e:
            _log.warning("_read_token_json failed: %s", e)
            return None
    return None


def add_groupme_events_to_calendar(events: list[dict]) -> list[str]:
    """Add detected GroupMe events to Google Calendar. Returns titles of added events."""
    if not events or not TOKEN_PATH.exists():
        return []

    added: list[str] = []
    try:
        token_data = _read_token_json()
        if token_data is None:
            return []
        creds = Credentials.from_authorized_user_info(token_data)
        if creds.expired and creds.refresh_token:
            creds.refresh(GoogleAuthRequest())
            raw = creds.to_json().encode()
            fd = os.open(str(TOKEN_PATH), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, raw)
            finally:
                os.close(fd)
        if not creds.valid:
            return []

        service = build("calendar", "v3", credentials=creds)

        for event in events:
            try:
                date_str = event.get("date")
                if not date_str:
                    continue

                time_str     = event.get("time")
                duration     = float(event.get("duration_hours", 2))

                if time_str:
                    start_dt = datetime.fromisoformat(f"{date_str}T{time_str}:00")
                    end_dt   = start_dt + timedelta(hours=duration)
                    body = {
                        "summary":     event["title"],
                        "description": event.get("description", "Detected from GroupMe"),
                        "start": {"dateTime": start_dt.isoformat(), "timeZone": "America/Phoenix"},
                        "end":   {"dateTime": end_dt.isoformat(),   "timeZone": "America/Phoenix"},
                    }
                else:
                    body = {
                        "summary":     event["title"],
                        "description": event.get("description", "Detected from GroupMe"),
                        "start": {"date": date_str},
                        "end":   {"date": date_str},
                    }

                service.events().insert(calendarId="primary", body=body).execute()
                added.append(event["title"])
                _log.info("GroupMe: added calendar event '%s'", event["title"])
            except Exception as e:
                _log.warning("GroupMe: failed to add event '%s': %s", event.get("title"), e)

    except Exception as e:
        _log.warning("GroupMe: calendar auth failed: %s", e)

    return added


def fetch_groupme_pinned() -> list[dict]:
    """Fetch currently pinned messages. Read-only — never posts anything."""
    if not GROUPME_ACCESS_TOKEN:
        return []

    url = (
        f"{GROUPME_API_BASE}/groups/{GROUPME_GROUP_ID}/messages"
        f"?token={GROUPME_ACCESS_TOKEN}&pinned=true&limit=20"
    )
    try:
        req = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        messages = data.get("response", {}).get("messages", [])
        # Only include messages that are actually pinned (pinned_at is set)
        return [
            {
                "id":        m["id"],
                "name":      m.get("name", "Unknown"),
                "text":      (m.get("text") or "").strip(),
                "pinned_at": m.get("pinned_at"),
                "created_at": m.get("created_at"),
            }
            for m in messages
            if m.get("pinned_at")
        ]
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return []
        _log.warning("fetch_groupme_pinned HTTP error: %s", e)
        return []
    except Exception as e:
        _log.warning("fetch_groupme_pinned failed: %s", e)
        return []


def poll_groupme() -> None:
    """
    Read-only GroupMe poll — runs every 20 minutes via APScheduler.
    Fetches new messages, summarizes, detects events, adds to Google Calendar.
    NEVER posts, sends, reacts, or modifies anything in GroupMe.
    """
    if not GROUPME_ACCESS_TOKEN:
        return

    state   = _groupme_read_state()
    last_id = state.get("last_message_id")

    messages = fetch_groupme_messages(since_id=last_id)

    state["last_check"] = datetime.now(timezone.utc).isoformat()
    state["pinned"] = fetch_groupme_pinned()

    if not messages:
        state["caught_up"] = True
        state["new_message_count"] = 0
        # Still prune events older than 7 days even when nothing new came in
        today  = datetime.now().date()
        cutoff = today - timedelta(days=7)
        state["events"] = [
            e for e in state.get("events", [])
            if not e.get("date") or datetime.fromisoformat(e["date"]).date() >= cutoff
        ]
        _groupme_write_state(state)
        return

    # New messages — analyze with Claude
    state["caught_up"] = False
    analysis     = analyze_groupme_messages(messages)
    added_events = []
    if analysis.get("events"):
        added_events = add_groupme_events_to_calendar(analysis["events"])

    # Merge new events with existing ones — persist until 7 days overdue
    today   = datetime.now().date()
    cutoff  = today - timedelta(days=7)

    existing = {(e.get("title",""), e.get("date","")): e for e in state.get("events", [])}
    for e in analysis.get("events", []):
        existing[(e.get("title",""), e.get("date",""))] = e  # new wins on collision

    def _keep(e: dict) -> bool:
        ds = e.get("date")
        if not ds:
            return True
        try:
            return datetime.fromisoformat(ds).date() >= cutoff
        except Exception:
            return True

    merged_events = [e for e in existing.values() if _keep(e)]

    # Merge calendar-added list too
    all_added = list(set(state.get("events_added_to_calendar", []) + added_events))

    # GroupMe returns newest first; messages[0] is the most recent
    state["last_message_id"]           = messages[0]["id"]
    state["summary"]                   = analysis.get("summary")
    state["events"]                    = merged_events
    state["events_added_to_calendar"]  = all_added
    state["new_message_count"]         = len(messages)

    _groupme_write_state(state)
    _log.info(
        "GroupMe: %d new messages, %d events detected, %d added to calendar",
        len(messages), len(analysis.get("events", [])), len(added_events),
    )


def _read_json(path: Path, default):
    """Read a JSON file using os.open with EDEADLK retry.

    Under macOS LaunchAgent, files with com.apple.provenance xattr cause the
    security daemon to serialize I/O — triggering EDEADLK (errno 11) on nearby
    concurrent reads even on clean files.  Retrying after a short sleep
    consistently succeeds once the daemon finishes its check.
    """
    if not path.exists():
        return default
    for attempt in range(8):          # initial try + up to 7 retries (~14 s total)
        try:
            fd = os.open(str(path), os.O_RDONLY)
            try:
                return json.loads(os.read(fd, os.fstat(fd).st_size))
            finally:
                os.close(fd)
        except OSError as e:
            if e.errno == errno.EDEADLK and attempt < 7:
                time.sleep(min(2.0, 0.5 * (attempt + 1)))  # 0.5, 1, 1.5, 2, 2, 2, 2 s
                continue
            _log.warning("_read_json(%s) failed after %d attempt(s): %s",
                         path.name, attempt + 1, e)
            return default
        except Exception as e:
            _log.warning("_read_json(%s) failed: %s", path.name, e)
            return default
    return default                    # unreachable, but satisfies type checker


def _write_json(path: Path, data) -> None:
    """Atomic write using raw os.open with EDEADLK retry.

    Using os.open (not write_text) avoids macOS tagging the file with
    com.apple.provenance, which would cause EDEADLK on future LaunchAgent reads.
    """
    tmp = path.with_suffix(".tmp")
    raw = json.dumps(data, indent=2).encode()
    for attempt in range(8):
        try:
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            try:
                os.write(fd, raw)
            finally:
                os.close(fd)
            os.replace(tmp, path)
            return
        except OSError as e:
            if e.errno == errno.EDEADLK and attempt < 7:
                time.sleep(min(2.0, 0.5 * (attempt + 1)))
                continue
            _log.warning("_write_json(%s) failed after %d attempt(s): %s",
                         path.name, attempt + 1, e)
            raise


_SEMESTER_KEYWORDS = {
    "class", "lecture", "lab", "seminar", "orientation",
    "move in", "move-in", "first day", "syllabus", "asu",
    "registration", "tuition", "schedule", "transcript",
}


def detect_schedule_change() -> str | None:
    """
    Scan Canvas iCal and Google Calendar for signs of a new semester starting.
    Returns a human-readable note if a change is detected, else None.
    Updates context.json automatically.
    """
    context = _read_json(CONTEXT_FILE, {})
    # Only run while the schedule type is still 'internship'
    if context.get("schedule", {}).get("type") != "internship":
        return None
    # Don't re-detect if already flagged
    if context.get("schedule", {}).get("semester_detected"):
        return None

    detected_event: str | None = None
    detected_date:  str | None = None

    # ── 1. Canvas iCal — look for assignments with Aug+ due dates ──────────
    try:
        with urllib.request.urlopen(CANVAS_ICAL_URL, timeout=10) as resp:
            cal = Calendar.from_ical(resp.read())
        today = datetime.now(timezone.utc).date()
        aug1  = today.replace(month=8, day=1) if today.month < 8 else \
                today.replace(year=today.year + 1, month=8, day=1)

        for component in cal.walk():
            if component.name != "VEVENT":
                continue
            dtstart = component.get("DTSTART")
            if dtstart is None:
                continue
            due = dtstart.dt
            if hasattr(due, "date"):
                due = due.date()
            if due >= aug1:
                summary = str(component.get("SUMMARY", ""))
                detected_event = f"Canvas assignment: \"{summary}\" due {due.isoformat()}"
                detected_date  = due.isoformat()
                break
    except Exception as e:
        _log.debug("detect_schedule_change Canvas check: %s", e)

    # ── 2. Google Calendar — look for semester-related events ──────────────
    if not detected_event and TOKEN_PATH.exists():
        try:
            token_data = _read_token_json()
            if token_data:
                creds = Credentials.from_authorized_user_info(token_data)
                if creds.expired and creds.refresh_token:
                    creds.refresh(GoogleAuthRequest())
                if creds.valid:
                    service = build("calendar", "v3", credentials=creds)
                    now    = datetime.now(timezone.utc)
                    future = now + timedelta(days=120)
                    result = service.events().list(
                        calendarId="primary",
                        timeMin=now.isoformat(),
                        timeMax=future.isoformat(),
                        singleEvents=True,
                        orderBy="startTime",
                    ).execute()
                    for event in result.get("items", []):
                        title = event.get("summary", "").lower()
                        if any(kw in title for kw in _SEMESTER_KEYWORDS):
                            start = event["start"].get("dateTime", event["start"].get("date", ""))
                            detected_event = f"Calendar event: \"{event.get('summary')}\" on {start[:10]}"
                            detected_date  = start[:10]
                            break
        except Exception as e:
            _log.debug("detect_schedule_change Calendar check: %s", e)

    if not detected_event:
        return None

    # ── Detected — update context.json ────────────────────────────────────
    context.setdefault("schedule", {})["semester_detected"]    = detected_date
    context.setdefault("schedule", {})["semester_detected_via"] = detected_event
    context.setdefault("notes", []).append(
        f"AUTO-DETECTED: Semester likely starts around {detected_date}. "
        f"Triggered by: {detected_event}. Schedule rules will need updating."
    )
    _write_json(CONTEXT_FILE, context)
    _log.info("detect_schedule_change: semester detected around %s via %s", detected_date, detected_event)
    return (
        f"Heads up — Goose detected your fall semester may be starting around "
        f"{detected_date} ({detected_event}). Schedule recommendations will adjust. "
        f"Let me know when you're back in Tempe and I'll update your context."
    )


def analyze_patterns() -> None:
    """
    Daily job: use Claude to extract behavioral patterns from the log.
    Skips silently until there are at least 7 entries.
    """
    log = _read_json(DAILY_LOG_FILE, [])
    if len(log) < 7:
        _log.info("analyze_patterns: only %d entries — need 7 to run", len(log))
        return

    patterns = _read_json(PATTERNS_FILE, {})
    feedback_log = patterns.get("feedback_log", [])

    log_text      = json.dumps(log[-30:], indent=2)
    feedback_text = json.dumps(feedback_log[-20:], indent=2) if feedback_log else "None yet"

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""You are analyzing behavioral data for a college student named Jackson at ASU in Tempe, Arizona.

Daily activity log (one entry per day, Jackson filled these in himself):
{log_text}

Scheduling feedback log (times Jackson rejected Goose's suggestion and said why):
{feedback_text}

Extract Jackson's behavioral patterns and respond with ONLY valid JSON:
{{
  "typical_out_days": ["Friday", "Saturday"],
  "avg_pregame_time": "21:00",
  "avg_out_frequency_per_week": 2.5,
  "preferred_work_times": ["14:00-17:00"],
  "avoid_scheduling": ["Friday evenings", "nice weather under 90F"],
  "best_scheduling": ["Monday-Wednesday afternoons", "hot days over 100F"],
  "weather_insight": "One sentence about how weather affects his productivity.",
  "scheduling_notes": "Specific rules Goose should follow when suggesting work times.",
  "summary": "2-3 sentences describing Jackson's patterns in plain English."
}}

Use null for any field where there isn't enough data yet. Be specific and concrete."""

    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=768,
        messages=[{"role": "user", "content": prompt}],
    )

    derived = parse_llm_json(msg.content[0].text)
    if derived:
        patterns["derived"]             = derived
        patterns["observations_count"]  = len(log)
        patterns["feedback_log"]        = feedback_log
        patterns["last_analyzed"]       = datetime.now(timezone.utc).isoformat()
        _write_json(PATTERNS_FILE, patterns)
        _log.info("analyze_patterns: updated from %d observations", len(log))


# ── Spotify helpers ───────────────────────────────────────────────────────────

def _spotify_token() -> dict | None:
    """Return a valid Spotify access token dict, refreshing if needed."""
    tok = _read_json(SPOTIFY_TOKEN_FILE, None)
    if not tok:
        return None
    # Refresh if expired (expires_at is unix timestamp we store)
    if time.time() >= tok.get("expires_at", 0) - 30:
        try:
            creds_b64 = base64.b64encode(
                f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()
            ).decode()
            req = urllib.request.Request(
                "https://accounts.spotify.com/api/token",
                data=urllib.parse.urlencode({
                    "grant_type":    "refresh_token",
                    "refresh_token": tok["refresh_token"],
                }).encode(),
                headers={
                    "Authorization": f"Basic {creds_b64}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            tok["access_token"] = data["access_token"]
            tok["expires_at"]   = time.time() + data["expires_in"]
            if "refresh_token" in data:
                tok["refresh_token"] = data["refresh_token"]
            _write_json(SPOTIFY_TOKEN_FILE, tok)
        except Exception as e:
            _log.warning("Spotify token refresh failed: %s", e)
            return None
    return tok


def _spotify_get(path: str) -> dict | None:
    """Make an authenticated GET to the Spotify Web API."""
    tok = _spotify_token()
    if not tok:
        return None
    try:
        req = urllib.request.Request(
            f"https://api.spotify.com/v1{path}",
            headers={"Authorization": f"Bearer {tok['access_token']}"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            if resp.status == 204:   # No Content (nothing playing)
                return {}
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 204:
            return {}
        _log.warning("Spotify GET %s failed: %s", path, e)
        return None
    except Exception as e:
        _log.warning("Spotify GET %s failed: %s", path, e)
        return None


def _spotify_put_post(method: str, path: str, body: dict | None = None) -> bool:
    """Make an authenticated PUT or POST to the Spotify Web API."""
    tok = _spotify_token()
    if not tok:
        return False
    try:
        data = json.dumps(body).encode() if body else b""
        req = urllib.request.Request(
            f"https://api.spotify.com/v1{path}",
            data=data,
            headers={
                "Authorization": f"Bearer {tok['access_token']}",
                "Content-Type": "application/json",
            },
            method=method,
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.status in (200, 204)
    except Exception as e:
        _log.warning("Spotify %s %s failed: %s", method, path, e)
        return False


def fetch_now_playing() -> dict:
    """Fetch the current Spotify track and cache it. Returns a clean dict."""
    cache = _read_json(SPOTIFY_NOW_FILE, {})
    if time.time() - cache.get("fetched_at", 0) < 5:
        return cache           # serve the 5-second cache

    raw = _spotify_get("/me/player/currently-playing")
    if raw is None:
        # Token missing / not authed yet
        result = {"status": "not_authed", "fetched_at": time.time()}
    elif raw == {}:
        # 204 — nothing currently playing
        result = {"status": "idle", "fetched_at": time.time()}
    else:
        item = raw.get("item") or {}
        album = item.get("album", {})
        artists = ", ".join(a["name"] for a in item.get("artists", []))
        images = album.get("images", [])
        art_url = images[0]["url"] if images else None
        duration_ms  = item.get("duration_ms", 0)
        progress_ms  = raw.get("progress_ms", 0)
        result = {
            "status":      "playing" if raw.get("is_playing") else "paused",
            "track":       item.get("name", "Unknown"),
            "artist":      artists,
            "album":       album.get("name", ""),
            "art_url":     art_url,
            "duration_ms": duration_ms,
            "progress_ms": progress_ms,
            "fetched_at":  time.time(),
        }
    _write_json(SPOTIFY_NOW_FILE, result)
    return result


def fetch_and_write_briefing() -> None:
    try:
        assignments       = fetch_canvas_assignments()
        events            = fetch_calendar_events()
        free_blocks       = compute_free_blocks(events)
        weather           = fetch_weather()
        patterns          = _read_json(PATTERNS_FILE, {})
        headline          = generate_headline(assignments, free_blocks, weather, patterns)
        schedule_alert    = detect_schedule_change()

        briefing = {
            "status": "ok",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "headline": headline,
            "assignments": assignments[:10],
            "events": events,
            "free_blocks": free_blocks,
            "weather": weather,
            "schedule_alert": schedule_alert,
        }
    except Exception as e:
        briefing = {
            "status": "error",
            "error": str(e),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    _write_json(BRIEFING_FILE, briefing)


# index.html is cached in memory.  Under macOS LaunchAgent context the
# com.apple.provenance xattr on editor-written files can cause Python's
# buffered IO to raise EDEADLK.  We try multiple reading strategies and
# log which one (if any) succeeds so we can diagnose failures.
_log = logging.getLogger(__name__)


def _read_html_file(path: Path) -> bytes:
    """Read index.html with EDEADLK retry (same pattern as _read_json)."""
    p = str(path)
    for attempt in range(8):
        try:
            fd = os.open(p, os.O_RDONLY)
            try:
                data = os.read(fd, os.fstat(fd).st_size)
                return data
            finally:
                os.close(fd)
        except OSError as e:
            if e.errno == errno.EDEADLK and attempt < 7:
                time.sleep(min(2.0, 0.5 * (attempt + 1)))
                continue
            _log.warning("_read_html_file failed after %d attempt(s): %s", attempt + 1, e)
            raise
    raise OSError(f"All retries failed for {path}")


_HTML_CACHE: bytes = b""
# Initial load deferred to lifespan (after the 5-second provenance-check sleep).

def _sync_static() -> None:
    """Copy static files from the iCloud project dir to the local data dir.

    Forces iCloud to hydrate dataless files via brctl before reading,
    then writes with raw os.open so the destination stays provenance-free.
    Falls back gracefully if the source is unavailable — the existing
    destination copy is kept intact.
    """
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    for fname in ("index.html",):
        src = STATIC_SRC_DIR / fname
        dst = STATIC_DIR / fname
        if not src.exists():
            continue
        try:
            # Ask iCloud to download the file if it's been evicted (dataless).
            # brctl is macOS-only; the call is silently skipped on Linux/Docker.
            try:
                subprocess.run(["brctl", "download", str(src)],
                        capture_output=True, timeout=30)
            except Exception:
                pass
            raw = _read_html_file(src)
            fd = os.open(str(dst), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            try:
                os.write(fd, raw)
            finally:
                os.close(fd)
            _log.info("_sync_static: copied %s (%d bytes)", fname, len(raw))
        except Exception as e:
            _log.warning("_sync_static: failed to copy %s: %s — keeping existing copy", fname, e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    STATIC_DIR.mkdir(parents=True, exist_ok=True)

    # Under macOS LaunchAgent, the provenance security daemon runs checks on the
    # process immediately after launch, serializing all file I/O and causing
    # EDEADLK on os.open calls for ~15-25 s.  Waiting here lets those checks
    # finish before we touch any files.
    await asyncio.sleep(5)

    # Copy index.html from the project source dir to the local data dir.
    # This keeps VS Code edits flowing through while serving from a non-iCloud path.
    global _HTML_CACHE
    _sync_static()
    try:
        _HTML_CACHE = _read_html_file(STATIC_DIR / "index.html")
        _log.info("lifespan: index.html loaded (%d bytes)", len(_HTML_CACHE))
    except OSError as e:
        _log.error("lifespan: index.html load failed: %s — will retry on first request", e)

    seed = {"status": "loading", "generated_at": datetime.now(timezone.utc).isoformat()}
    try:
        _write_json(BRIEFING_FILE, seed)
    except OSError:
        # If the seed write fails (rare EDEADLK on cold start), keep the old
        # briefing.json in place — the scheduler job will overwrite it shortly.
        pass

    # Delay initial scheduler jobs by 25 s on top of the 5 s above = ~30 s
    # total before the first file-heavy job runs.
    _startup_delay = timedelta(seconds=25)
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        fetch_and_write_briefing,
        IntervalTrigger(minutes=15),
        next_run_time=datetime.now() + _startup_delay,
    )
    scheduler.add_job(
        poll_groupme,
        IntervalTrigger(minutes=20),
        next_run_time=datetime.now() + _startup_delay,
    )
    scheduler.add_job(
        analyze_patterns,
        IntervalTrigger(days=1),
    )
    scheduler.start()

    yield

    scheduler.shutdown(wait=False)


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index():
    global _HTML_CACHE
    if not _HTML_CACHE:
        try:
            _HTML_CACHE = _read_html_file(STATIC_DIR / "index.html")
        except OSError:
            return HTMLResponse(
                content=b"<html><body style='background:#06080c;color:#00d4ff;"
                        b"font-family:monospace;padding:2rem'>"
                        b"<p>GOOSE STARTING UP \xe2\x80\x94 please refresh in a moment.</p>"
                        b"</body></html>",
                status_code=503,
            )
    return HTMLResponse(content=_HTML_CACHE)


@app.get("/api/weather")
async def get_weather(lat: float, lon: float):
    """Return live weather for the given coordinates (browser geolocation)."""
    try:
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            "&current=temperature_2m,apparent_temperature,precipitation,"
            "weather_code,wind_speed_10m,wind_direction_10m"
            "&daily=sunrise,sunset,uv_index_max,precipitation_sum"
            "&temperature_unit=fahrenheit&wind_speed_unit=mph"
            "&precipitation_unit=inch&timezone=auto&forecast_days=1"
        )
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())

        cur   = data["current"]
        daily = data["daily"]

        result = {
            "temp_f":          round(cur["temperature_2m"]),
            "feels_like_f":    round(cur["apparent_temperature"]),
            "precip_in":       cur["precipitation"],
            "weather_code":    cur["weather_code"],
            "condition":       _WMO_LABEL.get(cur["weather_code"], "UNKNOWN"),
            "wind_mph":        round(cur["wind_speed_10m"]),
            "wind_dir":        _wind_label(cur["wind_direction_10m"]),
            "uv_index":        daily["uv_index_max"][0],
            "precip_today_in": daily["precipitation_sum"][0],
            "sunrise":         daily["sunrise"][0].split("T")[1],
            "sunset":          daily["sunset"][0].split("T")[1],
        }
        return JSONResponse(result)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.get("/api/data")
async def get_data():
    data = _read_json(BRIEFING_FILE, {"status": "loading"})
    return JSONResponse(data)


@app.get("/api/checkin-status")
async def get_checkin_status():
    today      = datetime.now().date().isoformat()
    log        = _read_json(DAILY_LOG_FILE, [])
    done       = any(e.get("date") == today for e in log)
    patterns   = _read_json(PATTERNS_FILE, {})

    # Derive which questions Goose already knows the answer to with confidence.
    # A field is "settled" if the last 7 entries all gave the same answer.
    settled: dict = {}
    if len(log) >= 7:
        recent = log[-7:]
        for field in ("out_time", "work_session", "focus_level"):
            vals = [e.get(field) for e in recent if e.get(field)]
            if len(vals) == 7 and len(set(vals)) == 1:
                settled[field] = vals[0]

    return JSONResponse({
        "done":        done,
        "today":       today,
        "day_of_week": datetime.now().strftime("%A"),
        "entries":     len(log),
        "settled":     settled,       # fields Goose already knows — frontend skips these
        "has_patterns": bool(patterns.get("derived")),
    })


@app.post("/api/checkin")
async def post_checkin(request: Request):
    body  = await request.json()
    today = datetime.now().date().isoformat()

    weather_snap = None
    try:
        w = fetch_weather()
        weather_snap = {"temp_f": w["temp_f"], "condition": w["condition"]}
    except Exception:
        pass

    log = _read_json(DAILY_LOG_FILE, [])
    log = [e for e in log if e.get("date") != today]   # remove duplicate if re-submitted
    log.append({
        "date":         today,
        "day_of_week":  datetime.now().strftime("%A"),
        "went_out":     body.get("went_out"),
        "out_time":     body.get("out_time"),
        "work_session": body.get("work_session"),
        "focus_level":  body.get("focus_level"),
        "weather":      weather_snap,
        "logged_at":    datetime.now(timezone.utc).isoformat(),
    })
    _write_json(DAILY_LOG_FILE, log)
    return JSONResponse({"status": "ok", "total_entries": len(log)})


@app.post("/api/feedback")
async def post_feedback(request: Request):
    body     = await request.json()
    patterns = _read_json(PATTERNS_FILE, {})
    feedback = patterns.get("feedback_log", [])
    feedback.append({
        "date":            datetime.now().date().isoformat(),
        "day_of_week":     datetime.now().strftime("%A"),
        "suggested_block": body.get("suggested_block"),
        "accepted":        body.get("accepted", True),
        "reason":          body.get("reason"),
        "weather_temp":    body.get("weather_temp"),
        "logged_at":       datetime.now(timezone.utc).isoformat(),
    })
    patterns["feedback_log"] = feedback
    _write_json(PATTERNS_FILE, patterns)
    return JSONResponse({"status": "ok"})


@app.get("/api/patterns")
async def get_patterns():
    return JSONResponse(_read_json(PATTERNS_FILE, {}))


@app.get("/api/flights")
async def get_flights(lat: float = WEATHER_LAT, lon: float = WEATHER_LON):
    """Return airborne aircraft near the given coordinates, with 30-second server-side cache."""
    cache     = _read_json(FLIGHTS_CACHE_FILE, {})
    cache_age = time.time() - cache.get("fetched_at", 0)
    clat, clon = round(lat, 2), round(lon, 2)

    if cache_age < 30 and cache.get("clat") == clat and cache.get("clon") == clon:
        return JSONResponse(cache)

    lamin = lat - FLIGHTS_BOX_DEG
    lamax = lat + FLIGHTS_BOX_DEG
    lomin = lon - FLIGHTS_BOX_DEG
    lomax = lon + FLIGHTS_BOX_DEG

    aircraft: list[dict] = []
    try:
        url = f"{OPENSKY_URL}?lamin={lamin}&lomin={lomin}&lamax={lamax}&lomax={lomax}"
        req = urllib.request.Request(url, headers={"User-Agent": "Goose/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())

        for sv in (data.get("states") or []):
            # sv indices: 0=icao, 1=callsign, 5=lon, 6=lat,
            #             7=baro_alt(m), 8=on_ground, 9=vel(m/s), 10=heading
            if sv[8]:                        continue   # skip ground vehicles
            if sv[5] is None or sv[6] is None: continue  # no position

            dist = ((sv[6] - lat) ** 2 + (sv[5] - lon) ** 2) ** 0.5
            aircraft.append({
                "icao":     sv[0],
                "callsign": (sv[1] or "").strip() or sv[0].upper(),
                "lat":      sv[6],
                "lon":      sv[5],
                "alt_ft":   round(sv[7] * 3.28084) if sv[7] else None,
                "speed_kt": round(sv[9] * 1.94384) if sv[9] else None,
                "heading":  sv[10] or 0,
                "dist_deg": round(dist, 4),
            })

        # Sort by distance, cap at 40 aircraft
        aircraft.sort(key=lambda a: a["dist_deg"])
        aircraft = aircraft[:40]

    except urllib.error.HTTPError as e:
        _log.warning("get_flights HTTP error: %s", e)
    except Exception as e:
        _log.warning("get_flights failed: %s", e)

    result = {
        "aircraft":   aircraft,
        "count":      len(aircraft),
        "clat":       clat,
        "clon":       clon,
        "box_deg":    FLIGHTS_BOX_DEG,
        "fetched_at": time.time(),
    }
    _write_json(FLIGHTS_CACHE_FILE, result)
    return JSONResponse(result)


@app.get("/api/groupme")
async def get_groupme():
    """Return latest GroupMe summary, detected events, and poll metadata."""
    data = _read_json(GROUPME_STATE_FILE, {"summary": None, "events": [], "last_check": None})
    return JSONResponse(data)


@app.post("/api/groupme/refresh")
async def groupme_refresh():
    """Trigger an immediate GroupMe poll in a background thread."""
    t = threading.Thread(target=poll_groupme, daemon=True)
    t.start()
    return JSONResponse({"ok": True, "message": "GroupMe poll started"})


@app.post("/api/chat")
async def chat(request: Request):
    body = await request.json()
    messages = body.get("messages", [])

    briefing: dict = {}
    if BRIEFING_FILE.exists():
        try:
            briefing = json.loads(BRIEFING_FILE.read_text())
        except Exception:
            pass

    groupme: dict = {}
    if GROUPME_STATE_FILE.exists():
        try:
            groupme = json.loads(GROUPME_STATE_FILE.read_text())
        except Exception:
            pass

    patterns: dict = _read_json(PATTERNS_FILE, {})
    context:  dict = _read_json(CONTEXT_FILE,  {})

    local_now = datetime.now().strftime("%A, %B %d, %Y %I:%M %p")

    system_prompt = f"""You are Goose, a personal AI co-pilot for Jackson. Today is {local_now}.

You have access to everything currently displayed on Jackson's dashboard:

--- ACADEMIC BRIEFING ---
{json.dumps(briefing, indent=2)}

--- FRATERNITY GROUP CHAT (GroupMe · read-only observer) ---
{json.dumps(groupme, indent=2)}

--- CURRENT LIFE CONTEXT ---
{json.dumps(context, indent=2)}

--- LEARNED BEHAVIORAL PATTERNS ---
{json.dumps(patterns.get("derived", "Not enough data yet — check-in more days to unlock"), indent=2)}

Your job: answer conversationally and specifically. Reference real names, times, \
assignments, and events from the data above when relevant. Keep replies concise \
(under 150 words) unless asked for more detail. When asked about the group chat, \
draw from the summary and detected events. Speak like a person, not a robot — \
this will become voice-activated so keep responses natural and speakable."""

    def generate():
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        try:
            with client.messages.stream(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                system=system_prompt,
                messages=messages,
            ) as stream:
                for text in stream.text_stream:
                    yield f"data: {json.dumps({'text': text})}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Spotify OAuth + API routes ────────────────────────────────────────────────

@app.get("/auth/spotify")
async def spotify_auth():
    """Redirect the browser to Spotify's authorization page."""
    params = urllib.parse.urlencode({
        "client_id":     SPOTIFY_CLIENT_ID,
        "response_type": "code",
        "redirect_uri":  SPOTIFY_REDIRECT_URI,
        "scope":         SPOTIFY_SCOPES,
        "show_dialog":   "false",
    })
    return RedirectResponse(f"https://accounts.spotify.com/authorize?{params}")


@app.get("/auth/spotify/callback")
async def spotify_callback(code: str = "", error: str = ""):
    """Exchange the auth code for access + refresh tokens."""
    if error or not code:
        return HTMLResponse(f"<h2>Spotify auth failed: {error}</h2>")

    creds_b64 = base64.b64encode(
        f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()
    ).decode()
    try:
        req = urllib.request.Request(
            "https://accounts.spotify.com/api/token",
            data=urllib.parse.urlencode({
                "grant_type":   "authorization_code",
                "code":         code,
                "redirect_uri": SPOTIFY_REDIRECT_URI,
            }).encode(),
            headers={
                "Authorization": f"Basic {creds_b64}",
                "Content-Type":  "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())

        _write_json(SPOTIFY_TOKEN_FILE, {
            "access_token":  data["access_token"],
            "refresh_token": data["refresh_token"],
            "expires_at":    time.time() + data["expires_in"],
        })
        return HTMLResponse("""
        <html><body style="font-family:monospace;background:#111;color:#1DB954;
                           display:flex;align-items:center;justify-content:center;
                           height:100vh;margin:0;font-size:1.4rem;">
          <div>✅ Spotify connected — you can close this tab.</div>
        </body></html>
        """)
    except Exception as e:
        return HTMLResponse(f"<h2>Token exchange failed: {e}</h2>")


@app.get("/api/spotify")
async def get_spotify():
    """Return current Spotify playback state (5-second server-side cache)."""
    return JSONResponse(fetch_now_playing())


@app.post("/api/spotify/control")
async def spotify_control(request: Request):
    """Handle play/pause/next/prev commands."""
    body = await request.json()
    action = body.get("action", "")

    if action == "play_pause":
        state = fetch_now_playing()
        if state.get("status") == "playing":
            ok = _spotify_put_post("PUT", "/me/player/pause")
        else:
            ok = _spotify_put_post("PUT", "/me/player/play")
    elif action == "next":
        ok = _spotify_put_post("POST", "/me/player/next")
    elif action == "prev":
        ok = _spotify_put_post("POST", "/me/player/previous")
    else:
        return JSONResponse({"error": "unknown action"}, status_code=400)

    # Slight delay so Spotify's state updates before the next poll
    await asyncio.sleep(0.4)
    return JSONResponse({"ok": ok, "now": fetch_now_playing()})
