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
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from google.auth.transport.requests import Request as GoogleAuthRequest
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
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH))

        if creds.expired and creds.refresh_token:
            creds.refresh(GoogleAuthRequest())
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


def generate_headline(
    assignments: list[dict],
    free_blocks: list[dict],
    weather: dict | None,
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

    prompt = f"""You are Goose, a personal co-pilot. Generate a scheduling recommendation based on today's data.

Top assignments (most urgent first):
{json.dumps(assignments[:5], indent=2)}

Free time blocks today:
{json.dumps(free_blocks, indent=2)}
{weather_line}

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
        assignments  = fetch_canvas_assignments()
        events       = fetch_calendar_events()
        free_blocks  = compute_free_blocks(events)
        weather      = fetch_weather()
        headline     = generate_headline(assignments, free_blocks, weather)

        briefing = {
            "status": "ok",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "headline": headline,
            "assignments": assignments[:10],
            "events": events,
            "free_blocks": free_blocks,
            "weather": weather,
        }
    except Exception as e:
        briefing = {
            "status": "error",
            "error": str(e),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    BRIEFING_TMP.write_text(json.dumps(briefing, indent=2))
    os.replace(BRIEFING_TMP, BRIEFING_FILE)


# index.html is cached in memory.  Under macOS LaunchAgent context the
# com.apple.provenance xattr on editor-written files can cause Python's
# buffered IO to raise EDEADLK.  We try multiple reading strategies and
# log which one (if any) succeeds so we can diagnose failures.
import logging as _logging
_log = _logging.getLogger(__name__)


def _read_html_file(path: Path) -> bytes:
    """Try every available I/O method to read a provenance-tagged file."""
    p = str(path)

    # Strategy 1: raw os.open + os.read (bypasses buffered IO)
    try:
        fd = os.open(p, os.O_RDONLY)
        try:
            data = os.read(fd, os.fstat(fd).st_size)
            _log.info("_read_html: strategy 1 (os.read) succeeded")
            return data
        finally:
            os.close(fd)
    except OSError as e:
        _log.warning("_read_html: strategy 1 failed: %s", e)

    # Strategy 2: subprocess cat (different process, may have different perms)
    import subprocess
    try:
        result = subprocess.run(["/bin/cat", p], capture_output=True, timeout=5)
        if result.returncode == 0 and result.stdout:
            _log.info("_read_html: strategy 2 (cat) succeeded")
            return result.stdout
        _log.warning("_read_html: strategy 2 failed: rc=%s stderr=%s",
                     result.returncode, result.stderr[:200])
    except Exception as e:
        _log.warning("_read_html: strategy 2 failed: %s", e)

    raise OSError(f"All read strategies failed for {path}")


_HTML_CACHE: bytes = b""
try:
    _HTML_CACHE = _read_html_file(STATIC_DIR / "index.html")
except OSError as e:
    _log.error("_read_html startup load failed: %s — will retry on first request", e)


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
    if not BRIEFING_FILE.exists():
        return JSONResponse({"status": "loading"})
    try:
        return JSONResponse(json.loads(BRIEFING_FILE.read_text()))
    except (json.JSONDecodeError, OSError):
        return JSONResponse({"status": "loading"})


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

    local_now = datetime.now().strftime("%A, %B %d, %Y %I:%M %p")

    system_prompt = f"""You are Goose, a personal AI co-pilot. Today is {local_now}.

Live briefing data:
{json.dumps(briefing, indent=2)}

Your job: give sharp, tactical answers. Reference specific assignments, times, \
and free blocks from the briefing when relevant. Keep replies under 120 words \
unless the user asks for more detail. No fluff."""

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
