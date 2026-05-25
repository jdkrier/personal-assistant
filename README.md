# Goose

Your personal co-pilot. Opens on login, already knows your day.

Named after Goose from Top Gun (1986). Your wingman for daily life — briefing room aesthetic, amber cockpit theme, F-14 Tomcats on the radar.

---

## What it does

Goose runs as a macOS LaunchAgent and auto-opens in the browser on login. Every 15 minutes it pulls live data from all your sources and builds a daily briefing.

**Briefing**
- Canvas iCal — upcoming assignments sorted by urgency
- Google Calendar — today's events and free time blocks
- Claude-generated headline: one sentence telling you exactly what to focus on and when
- Live weather (Open-Meteo, no API key needed)

**SITREP — GroupMe Intel**
- Reads your fraternity group chat every 20 minutes (read-only observer — never posts)
- Summarizes recent messages with Claude
- Detects and extracts upcoming events mentioned in chat
- Highlights pinned messages as priority transmissions (red pulsing panel)
- Manual refresh button to re-poll on demand

**Live Flight Radar**
- Real aircraft near your location via OpenSky Network (free, no key required)
- Rendered on an animated canvas alongside F-14 Tomcats
- Radar sweep, callsign labels, altitude readout

**Spotify Now-Playing Bar**
- Persistent bottom bar: album art, track, artist, progress bar
- Play/pause, previous, next controls
- 5-second server-side cache, auto token refresh

**Behavioral Learning**
- Daily check-in card: wake time, energy, out time, tomorrow plan
- Feedback on headline suggestions (accept/reject + reason)
- Pattern analysis after 7+ days — adapts suggestions to your habits
- Questions collapse once Goose has learned your consistent answers

**Life Context**
- Knows your schedule: 9–5 internship, Friday 1pm cutoff, hybrid WFH 2–3 days/week
- Auto-detects semester change from Canvas iCal and Google Calendar
- Shows a banner alert when August class start is approaching

**Goose Chat**
- Persistent chat panel with full context: briefing, GroupMe state, patterns, schedule
- Streaming responses via Claude Haiku
- Written to be voice-activatable (conversational, speakable)

---

## Stack

| Layer | Tech |
|---|---|
| Backend | Python 3.12 + FastAPI + APScheduler |
| Frontend | Vanilla HTML/CSS/JS (no framework) |
| AI | Anthropic Claude (Haiku for speed, streaming SSE) |
| Data | Canvas iCal · Google Calendar API · Open-Meteo · OpenSky Network · GroupMe REST API · Spotify Web API |
| Runtime | macOS LaunchAgent (auto-start on login, KeepAlive) |

---

## Data files

All runtime data lives in `~/.goose/data/` — outside the iCloud-synced Desktop folder — to prevent macOS from evicting files and causing EDEADLK under LaunchAgent context.

| File | Purpose |
|---|---|
| `briefing.json` | Latest generated briefing (refreshed every 15 min) |
| `token.json` | Google OAuth tokens |
| `spotify_token.json` | Spotify OAuth tokens |
| `daily_log.json` | Behavioral check-in history |
| `patterns.json` | Derived behavioral patterns |
| `context.json` | Life schedule context (internship, WFH, semester) |
| `groupme_state.json` | Latest GroupMe summary + detected events |
| `flights_cache.json` | 30-second OpenSky cache |

---

## Setup

### 1. Clone and install dependencies

```bash
cd ~/Desktop/Personal\ Projects/Personal\ assistant
pip install -r requirements.txt
```

### 2. Credentials

**Canvas** — grab your iCal feed URL from Canvas → Calendar → Calendar Feed

**Google Calendar** — create a project at console.cloud.google.com, enable the Calendar API, download `credentials.json` to `~/.goose/data/credentials.json`

**Spotify** — create an app at developer.spotify.com/dashboard, set redirect URI to `http://127.0.0.1:8080/auth/spotify/callback`

### 3. LaunchAgent environment variables

Edit `~/Library/LaunchAgents/com.jdkrier.goose.plist`:

```xml
<key>CANVAS_ICAL_URL</key>      <string>your_canvas_ical_url</string>
<key>ANTHROPIC_API_KEY</key>    <string>your_anthropic_key</string>
<key>GOOGLE_CREDENTIALS_PATH</key> <string>/Users/you/.goose/data/credentials.json</string>
<key>GROUPME_ACCESS_TOKEN</key> <string>your_groupme_token</string>
<key>SPOTIFY_CLIENT_ID</key>    <string>your_spotify_client_id</string>
<key>SPOTIFY_CLIENT_SECRET</key><string>your_spotify_client_secret</string>
```

### 4. Install and start

```bash
launchctl load ~/Library/LaunchAgents/com.jdkrier.goose.plist
```

Open `http://localhost:8080` — Goose will be running.

### 5. Authorize services

- **Google Calendar**: run `python3 setup.py` once to complete OAuth
- **Spotify**: visit `http://127.0.0.1:8080/auth/spotify` once to connect

---

## Architecture notes

- **EDEADLK resilience** — macOS LaunchAgent processes hit deadlocks when opening files with `com.apple.provenance` xattr or iCloud-evicted (dataless) files. All file I/O uses raw `os.open`/`os.read`/`os.write` with 8-attempt exponential backoff, and all runtime data lives in `~/.goose/data/` (never iCloud-synced). Static files are copied from the project source to `~/.goose/data/static/` at every startup.
- **Atomic writes** — all JSON files are written via a `.tmp` + `os.replace()` pattern to prevent partial reads.
- **Scheduler** — APScheduler BackgroundScheduler with a 30-second startup delay to let macOS security checks settle before the first file-heavy job runs.
- **GroupMe is read-only** — the token only has read permissions. Goose never posts, reacts, or modifies anything in any group chat.
