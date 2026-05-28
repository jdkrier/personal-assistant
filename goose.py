import asyncio
import base64
import errno
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
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
from starlette.middleware.base import BaseHTTPMiddleware
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
ADSB_URL           = "https://api.adsb.lol/v2/lat/{lat}/lon/{lon}/dist/{nm}"
FLIGHTS_BOX_DEG    = 0.6   # display box: ±0.6° around centre (~42 miles)
FLIGHTS_RADIUS_NM  = 100   # search radius sent to adsb.lol (nautical miles)

# ── Behavioral Learning ──────────────────────────────────────────────────────
DAILY_LOG_FILE        = DATA_DIR / "daily_log.json"
PATTERNS_FILE         = DATA_DIR / "patterns.json"
CONTEXT_FILE          = DATA_DIR / "context.json"
WEEKLY_PLAN_FILE      = DATA_DIR / "weekly_plan.json"
WEEKLY_STATUS_FILE    = DATA_DIR / "weekly_status.json"
PREDICTION_THRESHOLD  = 7   # minimum log entries before predictions are generated

# ── GroupMe (read-only observer — NEVER posts or sends) ─────────────────────
GROUPME_ACCESS_TOKEN = os.environ.get("GROUPME_ACCESS_TOKEN", "")
GROUPME_GROUP_ID     = "30939626"   # "people" — fraternity group chat
GROUPME_STATE_FILE   = DATA_DIR / "groupme_state.json"
GROUPME_API_BASE     = "https://api.groupme.com/v3"

# ── Gmail (read-only) ────────────────────────────────────────────────────────
GMAIL_STATE_FILE = DATA_DIR / "gmail_state.json"

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

# ── Auth ─────────────────────────────────────────────────────────────────────
# APP_PASSWORD: plaintext password stored in .env — compared with constant-time
#   hmac.compare_digest to prevent timing attacks.
# SESSION_SECRET: random hex string used to sign session tokens.  Auto-generated
#   at startup if not set (tokens won't survive restarts in that case).
APP_PASSWORD   = os.environ.get("APP_PASSWORD", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
if not SESSION_SECRET:
    SESSION_SECRET = secrets.token_hex(32)
    logging.getLogger(__name__).warning(
        "SESSION_SECRET not set in environment — generated a throwaway secret. "
        "All sessions will be invalidated on restart. Set SESSION_SECRET in .env to persist sessions."
    )
SESSION_COOKIE = "goose_session"
_REMEMBER_AGE  = 24 * 3600   # 1 day in seconds


def _make_session_token() -> str:
    """Return a signed random session token."""
    payload = secrets.token_hex(16)
    sig = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def _verify_session_token(token: str) -> bool:
    """Return True if the token's HMAC signature is valid."""
    try:
        payload, sig = token.rsplit(".", 1)
        expected = hmac.new(
            SESSION_SECRET.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(sig, expected)
    except Exception:
        return False


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


def _get_google_creds() -> "Credentials | None":
    """Get valid Google credentials, refreshing the token if expired."""
    if not TOKEN_PATH.exists():
        return None
    token_data = _read_token_json()
    if not token_data:
        return None
    creds = Credentials.from_authorized_user_info(token_data)
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(GoogleAuthRequest())
            raw = creds.to_json().encode()
            fd = os.open(str(TOKEN_PATH), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, raw)
            finally:
                os.close(fd)
        except Exception as e:
            _log.warning("_get_google_creds refresh failed: %s", e)
            return None
    return creds if creds.valid else None


def fetch_calendar_events() -> list[dict]:
    """Fetch calendar events for today + the next 7 days."""
    creds = _get_google_creds()
    if not creds:
        return []

    try:
        service = build("calendar", "v3", credentials=creds)

        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_end    = today_start + timedelta(days=7)

        result = service.events().list(
            calendarId="primary",
            timeMin=today_start.isoformat(),
            timeMax=week_end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        ).execute()

        today_date = now.date()
        events = []
        for event in result.get("items", []):
            start = event["start"].get("dateTime", event["start"].get("date", ""))
            end   = event["end"].get("dateTime", event["end"].get("date", ""))

            # Determine if this is today or upcoming
            try:
                if "T" in start:
                    event_date = datetime.fromisoformat(start).date()
                else:
                    event_date = datetime.fromisoformat(start).date()
            except Exception:
                event_date = today_date

            is_today = (event_date == today_date)
            events.append({
                "title":    event.get("summary", "Busy"),
                "start":    start,
                "end":      end,
                "is_today": is_today,
                "date_label": "Today" if is_today else event_date.strftime("%a %b %-d"),
            })

        return events

    except Exception as e:
        _log.warning("fetch_calendar_events failed: %s", e)
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


def _gmail_read_state() -> dict:
    return _read_json(GMAIL_STATE_FILE, {
        "last_check": None,
        "processed_ids": [],
        "events": [],
        "events_added_to_calendar": [],
    })


def _gmail_write_state(state: dict) -> None:
    _write_json(GMAIL_STATE_FILE, state)


def add_events_to_calendar(events: list[dict], source: str = "Email") -> list[str]:
    """Add detected events to Google Calendar. Returns titles of added events."""
    if not events:
        return []

    creds = _get_google_creds()
    if not creds:
        return []

    added: list[str] = []
    try:
        service = build("calendar", "v3", credentials=creds)

        for event in events:
            try:
                date_str = event.get("date")
                if not date_str:
                    continue

                time_str  = event.get("time")
                duration  = float(event.get("duration_hours", 1))
                desc      = event.get("description", f"Detected from {source}")
                if event.get("source"):
                    desc += f"\nSource: {event['source']}"

                if time_str:
                    start_dt = datetime.fromisoformat(f"{date_str}T{time_str}:00")
                    end_dt   = start_dt + timedelta(hours=duration)
                    body = {
                        "summary":     event["title"],
                        "description": desc,
                        "start": {"dateTime": start_dt.isoformat(), "timeZone": "America/Phoenix"},
                        "end":   {"dateTime": end_dt.isoformat(),   "timeZone": "America/Phoenix"},
                    }
                else:
                    body = {
                        "summary":     event["title"],
                        "description": desc,
                        "start": {"date": date_str},
                        "end":   {"date": date_str},
                    }

                service.events().insert(calendarId="primary", body=body).execute()
                added.append(event["title"])
                _log.info("%s: added calendar event '%s'", source, event["title"])
            except Exception as e:
                _log.warning("%s: failed to add event '%s': %s", source, event.get("title"), e)

    except Exception as e:
        _log.warning("add_events_to_calendar auth failed: %s", e)

    return added


def fetch_gmail_messages(processed_ids: list[str]) -> list[dict]:
    """
    Fetch recent inbox emails not yet processed.
    Returns list of {id, subject, from, snippet} dicts.
    Read-only — never sends or modifies anything.
    """
    creds = _get_google_creds()
    if not creds:
        return []

    try:
        service = build("gmail", "v1", credentials=creds)

        # Search inbox for the last 3 days, excluding spam/trash
        cutoff = (datetime.now() - timedelta(days=3)).strftime("%Y/%m/%d")
        query  = f"in:inbox after:{cutoff} -in:spam -in:trash"

        result = service.users().messages().list(
            userId="me",
            q=query,
            maxResults=50,
        ).execute()

        messages     = result.get("messages", [])
        processed_set = set(processed_ids)

        new_messages: list[dict] = []
        for msg_meta in messages:
            msg_id = msg_meta["id"]
            if msg_id in processed_set:
                continue

            msg = service.users().messages().get(
                userId="me",
                id=msg_id,
                format="metadata",
                metadataHeaders=["Subject", "From", "Date"],
            ).execute()

            headers = {
                h["name"]: h["value"]
                for h in msg.get("payload", {}).get("headers", [])
            }
            new_messages.append({
                "id":      msg_id,
                "subject": headers.get("Subject", "(no subject)"),
                "from":    headers.get("From", ""),
                "snippet": msg.get("snippet", ""),
            })

        return new_messages

    except Exception as e:
        _log.warning("fetch_gmail_messages failed: %s", e)
        return []


def analyze_gmail_messages(messages: list[dict]) -> dict:
    """Use Claude Haiku to extract calendar events from email subjects + snippets."""
    if not messages:
        return {"events": []}

    today = datetime.now().strftime("%A, %B %d, %Y")

    email_lines = []
    for m in messages[:30]:   # cap at 30 emails for token safety
        email_lines.append(
            f"FROM: {m['from']}\n"
            f"SUBJECT: {m['subject']}\n"
            f"PREVIEW: {m['snippet']}\n---"
        )
    block = "\n".join(email_lines)

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""You are scanning emails to find personal events, meetings, or appointments that should be added to a calendar.
Today is {today}.

Emails to analyze:
{block}

Respond with ONLY valid JSON:
{{
  "events": [
    {{
      "title": "Event name",
      "date": "YYYY-MM-DD",
      "time": "HH:MM",
      "duration_hours": 1,
      "description": "brief description",
      "source": "sender or company name"
    }}
  ]
}}

STRICT inclusion rules — only add an event if ALL of these are true:
1. The recipient is personally expected to attend (invited, enrolled, registered, or scheduled)
2. It is NOT a marketing/promotional email advertising something to buy (tickets, courses, subscriptions)
3. It is NOT a mass newsletter blast sent to a mailing list
4. It has a specific date

EXCLUDE (do not add):
- Concert/festival advertisements and ticket promotions
- Online course or webinar advertisements (unless the user is already enrolled/registered)
- Promotional emails from retailers, apps, or brands
- Newsletters with dates that are just content — not personal invitations
- Any email where the primary intent is to sell something

INCLUDE (add these):
- Meeting invitations from real people or organizations
- Cohort kick-offs, onboarding sessions, interviews you've signed up for
- Appointments or confirmed bookings
- Events from programs the user is actively enrolled in (networking programs, mentorship, etc.)

Resolve relative dates like "this Thursday" relative to today ({today}).
Omit "time" if no specific time is mentioned.
Use 24-hour time (HH:MM).
Empty array if no qualifying events found."""

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    return parse_llm_json(message.content[0].text) or {"events": []}


def poll_gmail() -> None:
    """
    Scan Gmail inbox for event-like emails, extract events with Claude,
    and add them to Google Calendar. Runs every 30 minutes via APScheduler.
    Read-only Gmail access — never sends, modifies, or deletes anything.
    """
    if not TOKEN_PATH.exists():
        return

    state         = _gmail_read_state()
    processed_ids = state.get("processed_ids", [])

    messages = fetch_gmail_messages(processed_ids)

    state["last_check"] = datetime.now(timezone.utc).isoformat()

    if not messages:
        _gmail_write_state(state)
        return

    analysis     = analyze_gmail_messages(messages)
    added_events: list[str] = []
    if analysis.get("events"):
        added_events = add_events_to_calendar(analysis["events"], source="Gmail")

    # Cap processed_ids at 500 to avoid unbounded growth
    new_ids   = [m["id"] for m in messages]
    all_ids   = processed_ids + new_ids
    state["processed_ids"] = all_ids[-500:]

    # Merge detected events, prune anything more than 7 days old
    today  = datetime.now().date()
    cutoff = today - timedelta(days=7)

    existing = {(e.get("title", ""), e.get("date", "")): e for e in state.get("events", [])}
    for e in analysis.get("events", []):
        existing[(e.get("title", ""), e.get("date", ""))] = e

    def _keep(e: dict) -> bool:
        ds = e.get("date")
        if not ds:
            return True
        try:
            return datetime.fromisoformat(ds).date() >= cutoff
        except Exception:
            return True

    state["events"] = [e for e in existing.values() if _keep(e)]
    state["events_added_to_calendar"] = list(set(
        state.get("events_added_to_calendar", []) + added_events
    ))

    _gmail_write_state(state)
    _log.info(
        "Gmail: %d new emails scanned, %d events detected, %d added to calendar",
        len(messages), len(analysis.get("events", [])), len(added_events),
    )


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


def _week_start() -> str:
    """Return the ISO date of the most recent Monday (start of current week)."""
    today = datetime.now().date()
    return (today - timedelta(days=today.weekday())).isoformat()


def check_weekly_triggers() -> None:
    """
    Hourly job: set weekly banner state.
    - Sunday 6pm–11:59pm → 'plan' banner (if no plan saved for this week yet)
    - Friday 9pm–11:59pm → 'retro' banner (if plan exists but no retro yet)
    """
    now    = datetime.now()
    dow    = now.weekday()   # Mon=0, Tue=1, ..., Fri=4, ..., Sun=6
    hour   = now.hour

    week_of = _week_start()
    plan    = _read_json(WEEKLY_PLAN_FILE, {})
    status  = _read_json(WEEKLY_STATUS_FILE, {})

    new_banner_type: str | None = None

    # Sunday (6) 6pm–11:59pm: show planning banner if no plan saved yet this week
    if dow == 6 and 18 <= hour <= 23 and plan.get("week_of") != week_of:
        new_banner_type = "plan"

    # Friday (4) 9pm–11:59pm: show retro banner if plan exists but no retro yet
    if dow == 4 and 21 <= hour <= 23 and plan.get("week_of") == week_of and not plan.get("retrospective"):
        new_banner_type = "retro"

    current_type = status.get("banner_type") if status.get("banner_active") else None
    if new_banner_type and current_type != new_banner_type:
        _write_json(WEEKLY_STATUS_FILE, {
            "banner_active": True,
            "banner_type":   new_banner_type,
            "triggered_at":  datetime.now(timezone.utc).isoformat(),
            "session_done":  False,
        })
        _log.info("check_weekly_triggers: %s banner activated", new_banner_type)


def _extract_weekly_plan(raw_text: str) -> dict:
    """
    Use Claude Haiku to extract a structured weekly plan from free-form conversation text.
    Returns a dict with keys: work_sessions_targeted, social_plans, goals,
    blockers_anticipated, raw_summary. Returns {} on failure.
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""Extract a structured weekly plan from the conversation below.

Conversation:
{raw_text}

Respond with ONLY valid JSON:
{{
  "work_sessions_targeted": 3,
  "social_plans": ["Friday night out", "Sunday brunch"],
  "goals": ["finish project X", "gym 3x"],
  "blockers_anticipated": ["busy weekend"],
  "raw_summary": "One-sentence summary of the week plan."
}}

Rules:
- Use the actual words and numbers Jackson said
- Empty array [] if not mentioned
- work_sessions_targeted must be an integer (default 0 if not mentioned)"""

    try:
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        return parse_llm_json(msg.content[0].text) or {}
    except Exception as e:
        _log.warning("_extract_weekly_plan failed: %s", e)
        return {}


def analyze_patterns() -> None:
    """
    Daily job: use Claude to extract behavioral patterns from the log.
    Skips silently until there are at least 7 entries.
    """
    log = _read_json(DAILY_LOG_FILE, [])
    if len(log) < 7:
        _log.info("analyze_patterns: only %d entries — need 7 to run", len(log))
        return

    patterns     = _read_json(PATTERNS_FILE, {})
    feedback_log = patterns.get("feedback_log", [])
    weekly_plan  = _read_json(WEEKLY_PLAN_FILE, {})

    log_text      = json.dumps(log[-30:], indent=2)
    feedback_text = json.dumps(feedback_log[-20:], indent=2) if feedback_log else "None yet"

    # Include this week's plan + retrospective in the prompt if they exist
    weekly_text = ""
    if weekly_plan:
        weekly_text = f"""
Weekly plan data (plan/reality gap — highest-signal learning input):
{json.dumps(weekly_plan, indent=2)}
"""

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""You are analyzing behavioral data for a college student named Jackson.

Daily activity log (one entry per day — newer entries may include richer fields):
{log_text}
{weekly_text}
Scheduling feedback + corrections log:
{feedback_text}

Fields you may see in each log entry:
- plans_today: what Jackson intended to do that morning
- went_out / out_time: social activity
- work_session: when he got work done (morning/afternoon/evening/barely)
- focus_level: locked_in / decent / scattered
- mood_score: 1-5
- social_vs_solo: mostly_solo / mixed / mostly_social
- peak_energy_time: morning / afternoon / evening / night
- biggest_blocker: what pulled him away from focus
- actual_focus_window: the real time window he sat down to work

Weekly plan fields (if present):
- goals: what he planned to accomplish this week
- work_sessions_targeted: how many work sessions he planned
- social_plans: social events he anticipated
- blockers_anticipated: what he predicted might get in the way
- retrospective: his Friday reflection on how the week went vs. the plan
Use the plan/reality gap (goals vs. what actually happened in daily logs) as the
highest-signal input for plans_vs_reality and scheduling_notes.

Extract Jackson's behavioral patterns and respond with ONLY valid JSON:
{{
  "typical_out_days": ["Friday", "Saturday"],
  "avg_pregame_time": "21:00",
  "avg_out_frequency_per_week": 2.5,
  "preferred_work_times": ["14:00-17:00"],
  "peak_energy_pattern": "One sentence on when he has the most energy.",
  "mood_trend": "One sentence on mood patterns if data exists, else null.",
  "common_blockers": ["phone", "plans came up"],
  "plans_vs_reality": "One sentence on whether his morning plans match what he actually does, or null if not enough data.",
  "avoid_scheduling": ["Friday evenings"],
  "best_scheduling": ["Monday-Wednesday afternoons"],
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


def generate_prediction() -> dict:
    """
    Generate today's day prediction using patterns + log.
    Cached in patterns.json — only calls Claude once per day.
    Returns a prediction dict with keys: date, text, low_data_mode,
    days_remaining, generated_at, accuracy.
    """
    today    = datetime.now().date().isoformat()
    patterns = _read_json(PATTERNS_FILE, {})
    preds    = patterns.get("predictions", [])

    # Return cached prediction if already generated today
    existing = next((p for p in preds if p.get("date") == today), None)
    if existing:
        return existing

    log          = _read_json(DAILY_LOG_FILE, [])
    entry_count  = len(log)
    day_of_week  = datetime.now().strftime("%A")

    prediction: dict = {
        "date":         today,
        "day_of_week":  day_of_week,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "accuracy":     None,
    }

    if entry_count < PREDICTION_THRESHOLD:
        prediction["text"]           = None
        prediction["low_data_mode"]  = True
        prediction["days_remaining"] = PREDICTION_THRESHOLD - entry_count
    else:
        derived     = patterns.get("derived") or {}
        today_entry = next((e for e in log if e.get("date") == today), {})
        plans_today = today_entry.get("plans_today", "")

        prompt = f"""You are Goose, a personal co-pilot. Generate a short day prediction for Jackson.

Today: {day_of_week}
{"Jackson's plan for today: " + plans_today if plans_today else "No morning plan logged yet."}

Learned behavioral patterns:
{json.dumps(derived, indent=2)}

Recent log (last 14 days):
{json.dumps(log[-14:], indent=2)}

Write 2-3 short, direct sentences predicting:
1. Whether today looks like a focus day or social day (and why, based on patterns)
2. His likely peak energy window
3. One concrete suggestion

Rules: Use "you" not "Jackson". Be specific, not generic. No hedging phrases like "it seems" or "it appears". Short sentences. If today is a typical out night based on patterns, say so plainly."""

        try:
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            msg    = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}],
            )
            prediction["text"]          = msg.content[0].text.strip()
            prediction["low_data_mode"] = False
        except Exception as e:
            _log.warning("generate_prediction failed: %s", e)
            prediction["text"]           = None
            prediction["low_data_mode"]  = True
            prediction["days_remaining"] = 0

    # Store prediction, keep last 60 days
    preds = [p for p in preds if p.get("date") != today]
    preds.append(prediction)
    patterns["predictions"] = preds[-60:]
    _write_json(PATTERNS_FILE, patterns)
    return prediction


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


_HTML_CACHE:  bytes       = b""
_LOGIN_CACHE: bytes | None = None
# Initial load deferred to lifespan (after the 5-second provenance-check sleep).

def _sync_static() -> None:
    """Copy static files from the iCloud project dir to the local data dir.

    Forces iCloud to hydrate dataless files via brctl before reading,
    then writes with raw os.open so the destination stays provenance-free.
    Falls back gracefully if the source is unavailable — the existing
    destination copy is kept intact.
    """
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    for fname in ("index.html", "login.html", "profile.html"):
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
    global _HTML_CACHE, _LOGIN_CACHE
    _sync_static()
    try:
        _HTML_CACHE = _read_html_file(STATIC_DIR / "index.html")
        _log.info("lifespan: index.html loaded (%d bytes)", len(_HTML_CACHE))
    except OSError as e:
        _log.error("lifespan: index.html load failed: %s — will retry on first request", e)
    try:
        _LOGIN_CACHE = _read_html_file(STATIC_DIR / "login.html")
        _log.info("lifespan: login.html loaded (%d bytes)", len(_LOGIN_CACHE))
    except OSError as e:
        _log.error("lifespan: login.html load failed: %s — will serve fallback", e)

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
        poll_gmail,
        IntervalTrigger(minutes=30),
        next_run_time=datetime.now() + _startup_delay,
    )
    scheduler.add_job(
        analyze_patterns,
        IntervalTrigger(days=1),
    )
    scheduler.add_job(
        check_weekly_triggers,
        IntervalTrigger(hours=1),
        next_run_time=datetime.now() + _startup_delay,
    )
    scheduler.start()

    yield

    scheduler.shutdown(wait=False)


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ── Auth middleware ───────────────────────────────────────────────────────────
_PUBLIC_PATHS = {"/login", "/logout", "/health"}

class _AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # Always allow public paths and static assets
        if path in _PUBLIC_PATHS or path.startswith("/static"):
            return await call_next(request)
        # If APP_PASSWORD is unset, skip auth entirely (local dev)
        if not APP_PASSWORD:
            return await call_next(request)
        token = request.cookies.get(SESSION_COOKIE, "")
        if token and _verify_session_token(token):
            return await call_next(request)
        return RedirectResponse(url="/login", status_code=302)

app.add_middleware(_AuthMiddleware)


@app.get("/health")
async def health():
    """Unauthenticated health check — used by Docker HEALTHCHECK."""
    return JSONResponse({"ok": True})


@app.get("/login")
async def login_page(request: Request):
    global _LOGIN_CACHE
    # Already authenticated — send straight to the briefing
    token = request.cookies.get(SESSION_COOKIE, "")
    if token and _verify_session_token(token):
        return RedirectResponse(url="/", status_code=302)
    if not _LOGIN_CACHE:
        try:
            _LOGIN_CACHE = _read_html_file(STATIC_DIR / "login.html")
        except OSError:
            return HTMLResponse(content=b"<html><body>Login unavailable</body></html>",
                                status_code=503)
    return HTMLResponse(content=_LOGIN_CACHE)


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    password = str(form.get("password", ""))
    remember  = str(form.get("remember", "")) == "on"

    # Auth disabled (no APP_PASSWORD) — redirect straight through, no cookie needed
    if not APP_PASSWORD:
        return RedirectResponse(url="/", status_code=302)

    if not hmac.compare_digest(password, APP_PASSWORD):
        return RedirectResponse(url="/login?error=1", status_code=302)

    token    = _make_session_token()
    max_age  = _REMEMBER_AGE if remember else None
    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(
        SESSION_COOKIE, token,
        httponly=True, samesite="lax", max_age=max_age,
    )
    return response


@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(SESSION_COOKIE)
    return response


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
        def _fetch_weather_sync():
            with urllib.request.urlopen(url, timeout=10) as resp:
                return json.loads(resp.read())
        data = await asyncio.to_thread(_fetch_weather_sync)

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
    data = await asyncio.to_thread(_read_json, BRIEFING_FILE, {"status": "loading"})
    return JSONResponse(data)


@app.get("/api/checkin-status")
async def get_checkin_status():
    now        = datetime.now()
    today      = now.date().isoformat()
    log        = await asyncio.to_thread(_read_json, DAILY_LOG_FILE, [])
    patterns   = await asyncio.to_thread(_read_json, PATTERNS_FILE, {})

    today_entry = next((e for e in log if e.get("date") == today), None)
    morning_done = bool(today_entry and today_entry.get("plans_today"))
    done         = bool(today_entry and today_entry.get("work_session"))  # evening complete

    # Phase: morning (before 16:00) or evening (16:00+)
    phase = "morning" if now.hour < 16 else "evening"

    # Derive which questions Goose already knows the answer to with confidence.
    # A field is "settled" if the last 7 entries all gave the same answer.
    settled: dict = {}
    if len(log) >= 7:
        recent = log[-7:]
        for field in ("out_time", "work_session", "focus_level"):
            vals = [e.get(field) for e in recent if e.get(field)]
            if len(vals) == 7 and len(set(vals)) == 1:
                settled[field] = vals[0]

    # Prediction state for the evening accuracy question
    preds           = patterns.get("predictions", [])
    today_pred      = next((p for p in preds if p.get("date") == today), None)
    has_prediction  = bool(today_pred and today_pred.get("text"))
    pred_confirmed  = bool(today_pred and today_pred.get("accuracy") is not None)

    return JSONResponse({
        "done":              done,
        "morning_done":      morning_done,
        "phase":             phase,
        "today":             today,
        "day_of_week":       now.strftime("%A"),
        "entries":           len(log),
        "settled":           settled,
        "has_patterns":      bool(patterns.get("derived")),
        "has_prediction":    has_prediction,
        "pred_confirmed":    pred_confirmed,
    })


@app.post("/api/checkin")
async def post_checkin(request: Request):
    body  = await request.json()
    now   = datetime.now()
    today = now.date().isoformat()

    weather_snap = None
    try:
        w = await asyncio.to_thread(fetch_weather)
        weather_snap = {"temp_f": w["temp_f"], "condition": w["condition"]}
    except Exception:
        pass

    log = await asyncio.to_thread(_read_json, DAILY_LOG_FILE, [])
    # Merge into existing today entry so morning + evening submissions stack
    existing = next((e for e in log if e.get("date") == today), {})
    log = [e for e in log if e.get("date") != today]

    entry = {
        **existing,                                     # keep fields already set today
        "date":                today,
        "day_of_week":         now.strftime("%A"),
        "weather":             weather_snap or existing.get("weather"),
        "logged_at":           now.astimezone(timezone.utc).isoformat(),
    }

    # Morning fields
    if body.get("plans_today") is not None:
        entry["plans_today"] = body["plans_today"]

    # Evening fields (all optional — only overwrite if provided)
    for field in ("went_out", "out_time", "work_session", "focus_level",
                  "mood_score", "social_vs_solo", "peak_energy_time",
                  "biggest_blocker", "actual_focus_window"):
        if body.get(field) is not None:
            entry[field] = body[field]

    log.append(entry)
    await asyncio.to_thread(_write_json, DAILY_LOG_FILE, log)

    # Save prediction accuracy if provided
    if body.get("prediction_accurate") is not None:
        def _save_accuracy():
            pats  = _read_json(PATTERNS_FILE, {})
            preds = pats.get("predictions", [])
            for p in preds:
                if p.get("date") == today:
                    p["accuracy"] = body["prediction_accurate"]
                    break
            pats["predictions"] = preds
            _write_json(PATTERNS_FILE, pats)
        await asyncio.to_thread(_save_accuracy)

    return JSONResponse({"status": "ok", "total_entries": len(log)})


@app.post("/api/feedback")
async def post_feedback(request: Request):
    body     = await request.json()
    patterns = await asyncio.to_thread(_read_json, PATTERNS_FILE, {})
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
    await asyncio.to_thread(_write_json, PATTERNS_FILE, patterns)
    return JSONResponse({"status": "ok"})


@app.get("/api/patterns")
async def get_patterns():
    return JSONResponse(await asyncio.to_thread(_read_json, PATTERNS_FILE, {}))


@app.get("/api/prediction")
async def get_prediction():
    """Return today's prediction, generating it if not yet cached."""
    pred = await asyncio.to_thread(generate_prediction)
    return JSONResponse(pred)


@app.post("/api/profile-correction")
async def post_profile_correction(request: Request):
    """User corrects a learned inference from the profile page."""
    body     = await request.json()
    patterns = await asyncio.to_thread(_read_json, PATTERNS_FILE, {})
    feedback = patterns.get("feedback_log", [])
    feedback.append({
        "date":       datetime.now().date().isoformat(),
        "type":       "correction",
        "field":      body.get("field"),
        "old_value":  body.get("old_value"),
        "correction": body.get("correction"),
        "source":     "user",
        "logged_at":  datetime.now(timezone.utc).isoformat(),
    })
    patterns["feedback_log"] = feedback
    await asyncio.to_thread(_write_json, PATTERNS_FILE, patterns)
    return JSONResponse({"status": "ok"})


@app.get("/profile")
async def profile_page():
    try:
        content = await asyncio.to_thread(_read_html_file, STATIC_DIR / "profile.html")
        return HTMLResponse(content=content)
    except OSError:
        return HTMLResponse(content=b"<html><body>Profile page not found.</body></html>", status_code=404)


@app.get("/api/weekly-status")
async def get_weekly_status():
    """Return current weekly banner state and this week's plan (if any)."""
    status  = await asyncio.to_thread(_read_json, WEEKLY_STATUS_FILE, {})
    plan    = await asyncio.to_thread(_read_json, WEEKLY_PLAN_FILE, {})
    week_of = _week_start()

    this_week_plan = plan if plan.get("week_of") == week_of else None
    return JSONResponse({
        "banner_active": status.get("banner_active", False),
        "banner_type":   status.get("banner_type"),        # "plan" | "retro" | None
        "triggered_at":  status.get("triggered_at"),
        "session_done":  status.get("session_done", False),
        "has_plan":      this_week_plan is not None,
        "plan":          this_week_plan,
        "week_of":       week_of,
    })


@app.post("/api/weekly-session/save")
async def save_weekly_session(request: Request):
    """
    Save a weekly plan (type='plan') or retrospective (type='retro').
    Extracts structured data from raw conversation text, writes weekly_plan.json,
    and marks the banner as done.
    """
    body         = await request.json()
    session_type = body.get("type")  # "plan" | "retro"
    raw_text     = body.get("raw_text", "").strip()

    if not session_type or not raw_text:
        return JSONResponse({"error": "type and raw_text are required"}, status_code=400)

    week_of = _week_start()
    plan    = await asyncio.to_thread(_read_json, WEEKLY_PLAN_FILE, {})

    if session_type == "plan":
        structured = await asyncio.to_thread(_extract_weekly_plan, raw_text)
        plan = {
            "week_of":                week_of,
            "work_sessions_targeted": structured.get("work_sessions_targeted", 0),
            "social_plans":           structured.get("social_plans", []),
            "goals":                  structured.get("goals", []),
            "blockers_anticipated":   structured.get("blockers_anticipated", []),
            "raw_summary":            structured.get("raw_summary", raw_text[:200]),
            "raw_conversation":       raw_text,
            "structured":             bool(structured),
            "created_at":             datetime.now(timezone.utc).isoformat(),
            "retrospective":          None,
            "retrospective_at":       None,
        }
        await asyncio.to_thread(_write_json, WEEKLY_PLAN_FILE, plan)
        _log.info("save_weekly_session: plan saved for week %s", week_of)

    elif session_type == "retro":
        plan["retrospective"]    = raw_text
        plan["retrospective_at"] = datetime.now(timezone.utc).isoformat()
        await asyncio.to_thread(_write_json, WEEKLY_PLAN_FILE, plan)
        _log.info("save_weekly_session: retro saved for week %s", week_of)
    else:
        return JSONResponse({"error": "type must be 'plan' or 'retro'"}, status_code=400)

    # Mark banner done
    status = await asyncio.to_thread(_read_json, WEEKLY_STATUS_FILE, {})
    status["session_done"]  = True
    status["banner_active"] = False
    await asyncio.to_thread(_write_json, WEEKLY_STATUS_FILE, status)

    return JSONResponse({"ok": True, "week_of": week_of})


@app.get("/api/flights")
async def get_flights(lat: float = WEATHER_LAT, lon: float = WEATHER_LON):
    """Return airborne aircraft near the given coordinates via adsb.lol, 30-second cache."""
    cache     = await asyncio.to_thread(_read_json, FLIGHTS_CACHE_FILE, {})
    cache_age = time.time() - cache.get("fetched_at", 0)
    clat, clon = round(lat, 2), round(lon, 2)

    if cache_age < 30 and cache.get("clat") == clat and cache.get("clon") == clon:
        return JSONResponse(cache)

    aircraft: list[dict] = []
    try:
        url = ADSB_URL.format(lat=lat, lon=lon, nm=FLIGHTS_RADIUS_NM)
        def _fetch_flights_sync():
            req = urllib.request.Request(url, headers={"User-Agent": "Goose/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        data = await asyncio.to_thread(_fetch_flights_sync)

        for ac in (data.get("ac") or []):
            ac_lat = ac.get("lat")
            ac_lon = ac.get("lon")
            if ac_lat is None or ac_lon is None:
                continue
            # alt_baro is feet (int) or the string "ground" for surface traffic
            alt = ac.get("alt_baro")
            if alt == "ground" or alt is None:
                continue

            dist = ((ac_lat - lat) ** 2 + (ac_lon - lon) ** 2) ** 0.5
            aircraft.append({
                "icao":     ac.get("hex", ""),
                "callsign": (ac.get("flight") or ac.get("hex", "")).strip(),
                "lat":      ac_lat,
                "lon":      ac_lon,
                "alt_ft":   int(alt),
                "speed_kt": round(ac["gs"]) if ac.get("gs") else None,
                "heading":  ac.get("track") or 0,
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
    await asyncio.to_thread(_write_json, FLIGHTS_CACHE_FILE, result)
    return JSONResponse(result)


@app.get("/api/groupme")
async def get_groupme():
    """Return latest GroupMe summary, detected events, and poll metadata."""
    data = await asyncio.to_thread(_read_json, GROUPME_STATE_FILE, {"summary": None, "events": [], "last_check": None})
    return JSONResponse(data)


@app.post("/api/groupme/refresh")
async def groupme_refresh():
    """Trigger an immediate GroupMe poll in a background thread."""
    t = threading.Thread(target=poll_groupme, daemon=True)
    t.start()
    return JSONResponse({"ok": True, "message": "GroupMe poll started"})


@app.get("/api/gmail")
async def get_gmail():
    """Return latest Gmail scan state — detected events and last check time."""
    data = await asyncio.to_thread(
        _read_json, GMAIL_STATE_FILE,
        {"events": [], "events_added_to_calendar": [], "last_check": None}
    )
    # Don't expose processed_ids to the frontend — strip it
    safe = {k: v for k, v in data.items() if k != "processed_ids"}
    return JSONResponse(safe)


@app.post("/api/gmail/refresh")
async def gmail_refresh():
    """Trigger an immediate Gmail scan in a background thread."""
    t = threading.Thread(target=poll_gmail, daemon=True)
    t.start()
    return JSONResponse({"ok": True, "message": "Gmail scan started"})


@app.post("/api/chat")
async def chat(request: Request):
    body     = await request.json()
    messages = body.get("messages", [])
    mode     = body.get("mode", "normal")  # "normal" | "weekly_plan" | "weekly_retro"

    patterns: dict = await asyncio.to_thread(_read_json, PATTERNS_FILE, {})
    context:  dict = await asyncio.to_thread(_read_json, CONTEXT_FILE,  {})
    local_now = datetime.now().strftime("%A, %B %d, %Y %I:%M %p")

    if mode == "weekly_plan":
        derived_text = json.dumps(patterns.get("derived", {}), indent=2) if patterns.get("derived") else "Not enough data yet."
        system_prompt = f"""You are Goose, running a Sunday evening weekly planning session with Jackson. Today is {local_now}.

Your job: have a natural, brief conversation to understand what's on Jackson's plate for the week. One question at a time. Keep responses to 2–3 short sentences.

Ask about:
1. What he wants to accomplish this week (goals, projects, gym, etc.)
2. Any social plans or events coming up
3. Anything that might get in the way

When you have enough to summarize, say exactly: "Got it — I've got your plan for the week. Hit 'Save Plan' when you're ready to lock it in."

Rules:
- This will become voice-activated on a wall display — keep language natural and speakable
- No bullet lists, no headers, no formatting — plain spoken sentences only
- Don't ask more than one question at a time
- Use "you" not "Jackson"

Learned patterns for context: {derived_text}"""

    elif mode == "weekly_retro":
        plan    = await asyncio.to_thread(_read_json, WEEKLY_PLAN_FILE, {})
        log     = await asyncio.to_thread(_read_json, DAILY_LOG_FILE, [])
        week_of = _week_start()

        # Pull this week's daily log entries for the retro
        this_week_log = [e for e in log if e.get("date", "") >= week_of]
        plan_text     = json.dumps(plan, indent=2) if plan.get("week_of") == week_of else "No plan found for this week."
        log_text      = json.dumps(this_week_log, indent=2) if this_week_log else "No daily logs for this week yet."

        system_prompt = f"""You are Goose, running a Friday evening weekly retrospective with Jackson. Today is {local_now}.

Start by briefly summarizing how the week went based on the plan and daily log data below. Then let Jackson reflect.

This week's plan:
{plan_text}

This week's daily log:
{log_text}

Rules:
- Open with a 2-3 sentence summary of how the week actually went vs. what was planned
- Reference specific goals from the plan (hit or missed?)
- Then ask: "How do you feel about the week overall?"
- Keep each response to 2–3 short sentences — plain spoken sentences, no lists or formatting
- This will become voice-activated on a wall display — keep it natural and speakable
- When Jackson is done reflecting, say exactly: "Good debrief. Hit 'Save Reflection' to lock it in."
- Use "you" not "Jackson" """

    else:
        # Normal mode — original behavior
        briefing: dict = {}
        if BRIEFING_FILE.exists():
            try:
                briefing = json.loads(await asyncio.to_thread(BRIEFING_FILE.read_text))
            except Exception:
                pass

        groupme: dict = {}
        if GROUPME_STATE_FILE.exists():
            try:
                groupme = json.loads(await asyncio.to_thread(GROUPME_STATE_FILE.read_text))
            except Exception:
                pass

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
        return HTMLResponse(f"<h2>Spotify auth failed: {html.escape(error)}</h2>")

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
        def _exchange_token_sync():
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        data = await asyncio.to_thread(_exchange_token_sync)

        await asyncio.to_thread(_write_json, SPOTIFY_TOKEN_FILE, {
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
    return JSONResponse(await asyncio.to_thread(fetch_now_playing))


@app.post("/api/spotify/control")
async def spotify_control(request: Request):
    """Handle play/pause/next/prev commands."""
    body = await request.json()
    action = body.get("action", "")

    if action == "play_pause":
        state = await asyncio.to_thread(fetch_now_playing)
        if state.get("status") == "playing":
            ok = await asyncio.to_thread(_spotify_put_post, "PUT", "/me/player/pause")
        else:
            ok = await asyncio.to_thread(_spotify_put_post, "PUT", "/me/player/play")
    elif action == "next":
        ok = await asyncio.to_thread(_spotify_put_post, "POST", "/me/player/next")
    elif action == "prev":
        ok = await asyncio.to_thread(_spotify_put_post, "POST", "/me/player/previous")
    else:
        return JSONResponse({"error": "unknown action"}, status_code=400)

    # Slight delay so Spotify's state updates before the next poll
    await asyncio.sleep(0.4)
    return JSONResponse({"ok": ok, "now": await asyncio.to_thread(fetch_now_playing)})
