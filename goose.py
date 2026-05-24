import json
import os
import re
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

import anthropic
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from icalendar import Calendar

BASE_DIR = Path(__file__).parent.resolve()

# Env vars are injected by the LaunchAgent plist (EnvironmentVariables key).
# For manual runs, export them in your shell first or copy .env to your session.

CANVAS_ICAL_URL = os.environ["CANVAS_ICAL_URL"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

BRIEFING_FILE = BASE_DIR / "briefing.json"
BRIEFING_TMP  = BASE_DIR / "briefing.json.tmp"
STATIC_DIR    = BASE_DIR / "static"
TOKEN_PATH    = BASE_DIR / "token.json"


def compute_urgency(days_until_due: float) -> float:
    return round(1 / max(days_until_due, 0.5), 4)


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
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH))

        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            TOKEN_PATH.write_text(creds.to_json())

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


def generate_headline(assignments: list[dict], free_blocks: list[dict]) -> dict | None:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    prompt = f"""You are Goose, a personal co-pilot. Generate a scheduling recommendation based on today's data.

Top assignments (most urgent first):
{json.dumps(assignments[:5], indent=2)}

Free time blocks today:
{json.dumps(free_blocks, indent=2)}

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


def fetch_and_write_briefing() -> None:
    try:
        assignments = fetch_canvas_assignments()
        events = fetch_calendar_events()
        free_blocks = compute_free_blocks(events)
        headline = generate_headline(assignments, free_blocks)

        briefing = {
            "status": "ok",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "headline": headline,
            "assignments": assignments[:10],
            "events": events,
            "free_blocks": free_blocks,
        }
    except Exception as e:
        briefing = {
            "status": "error",
            "error": str(e),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    BRIEFING_TMP.write_text(json.dumps(briefing, indent=2))
    os.replace(BRIEFING_TMP, BRIEFING_FILE)


@asynccontextmanager
async def lifespan(app: FastAPI):
    STATIC_DIR.mkdir(exist_ok=True)

    seed = {"status": "loading", "generated_at": datetime.now(timezone.utc).isoformat()}
    BRIEFING_TMP.write_text(json.dumps(seed))
    os.replace(BRIEFING_TMP, BRIEFING_FILE)

    scheduler = BackgroundScheduler()
    scheduler.add_job(
        fetch_and_write_briefing,
        IntervalTrigger(minutes=15),
        next_run_time=datetime.now(),
    )
    scheduler.start()

    yield

    scheduler.shutdown(wait=False)


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def index():
    # Read synchronously to avoid anyio EDEADLK under macOS LaunchAgent
    html = (STATIC_DIR / "index.html").read_bytes()
    return HTMLResponse(content=html)


@app.get("/api/data")
async def get_data():
    if not BRIEFING_FILE.exists():
        return JSONResponse({"status": "loading"})
    try:
        return JSONResponse(json.loads(BRIEFING_FILE.read_text()))
    except (json.JSONDecodeError, OSError):
        return JSONResponse({"status": "loading"})
