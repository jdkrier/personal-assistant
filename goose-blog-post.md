# Goose: Building My AI-Powered Personal Co-Pilot

*May 2026 | Jackson Krier*

---

## The Idea

Every Sunday night I'd find myself doing the same thing — opening five different tabs, checking Canvas for upcoming assignments, pulling up Google Calendar, scrolling through GroupMe to see what I missed, checking if any flights were overhead, and trying to piece together what my week actually looked like. It was tedious, and it felt like exactly the kind of thing a computer should be doing for me.

So I built Goose.

Named after the RIO from *Top Gun* (the 1986 original — not Maverick), Goose is my personal AI co-pilot. Every morning it compiles everything I need to know — assignments, calendar events, group chat summaries, live flight radar, current weather, and whatever else I throw at it — runs it through Claude, and delivers me a single, clean daily briefing. It runs 24/7 on a server in my basement and I can pull it up from anywhere.

This is the story of how I built it, what I learned along the way, and where it's going.

---

## The Stack

At its core, Goose is a Python web app built with **FastAPI** and served via **uvicorn**. It uses **APScheduler** to run background jobs on a schedule — fetching data, generating briefings, polling APIs — and exposes a simple REST API that the frontend consumes.

The frontend is a single `index.html` file styled to match the aesthetic of a 1986 naval aviator's HUD. Warm amber and orange tones, monospace fonts, the kind of thing you'd expect to see in an F-14 Tomcat cockpit. No frameworks, no build system — just clean HTML, CSS, and vanilla JavaScript.

The whole thing is containerized with **Docker** and lives on a Red Hat Linux server in my basement, managed with `docker compose`.

---

## The APIs: A Six-Way Handshake

The thing that makes Goose actually useful is how many data sources it pulls together simultaneously. Each one required its own authentication setup, its own quirks, and its own debugging session.

### 1. Anthropic (Claude)

The brain of the operation. Every morning, Goose feeds all the raw data — calendar events, assignments, messages, weather, flights — into Claude via the Anthropic API. Claude reads everything and writes a coherent, human-readable briefing. Not a dump of raw data, but an actual summary with context, priorities called out, and a tone that feels like someone who knows my schedule. This is what makes the whole thing more than just a dashboard.

### 2. Google Calendar

OAuth 2.0 flow, refresh tokens, the full thing. Goose authenticates with my Google account, pulls my upcoming events, and feeds them into the briefing. Getting the initial `credentials.json` and `token.json` set up took some patience — Google's OAuth flow isn't exactly beginner-friendly — but once it's running it just works. The token auto-refreshes so I never have to re-authenticate.

### 3. Canvas (ASU)

Canvas exposes a personal iCal feed URL — a long, unique URL that streams all your assignments and due dates as calendar events. No API key required, just the URL. Goose fetches this on a schedule, parses the `.ics` file, and extracts upcoming assignments so they make it into the morning briefing. If something is due this week, I know about it before I even open my laptop.

### 4. GroupMe

This one was interesting to set up. GroupMe has a REST API that lets you read messages from group chats using an access token. Goose polls the group chat on a schedule, tracks which messages it's already seen with a `since_id` cursor, and keeps a running summary of recent activity. There's a "SITREP" panel in the UI that shows what's been going on.

One hard rule I built in from the start: **Goose is read-only in GroupMe.** It can observe, summarize, and report — but it will never send a message, react, or modify anything. Observer only.

### 5. Spotify

Goose has a persistent bottom bar that shows whatever is currently playing on Spotify — album art, track name, artist, a live progress bar, and playback controls (previous, play/pause, next). This required setting up a full OAuth 2.0 Authorization Code flow with Spotify's Web API. The tricky part was getting the redirect URI right — Spotify's dashboard blocks certain localhost formats, so I had to switch from `localhost` to `127.0.0.1` before the callback would work. Once authenticated, the token auto-refreshes and the bar updates every five seconds.

### 6. FlightRadar / Aviation APIs

Because I thought it would be cool, and it is — Goose can show live flight data. Planes overhead, flight numbers, altitudes. It ended up being one of the more fun features to build.

---

## The Debugging Gauntlet: EDEADLK

No project goes smoothly, and Goose had one bug that took a genuine deep-dive to understand.

After running for a while under macOS as a LaunchAgent (a background process that starts on login), the app would intermittently fail to read its own JSON files. The error was `errno 11: EDEADLK` — a deadlock error that you almost never see in normal Python code.

The root cause turned out to be **iCloud Desktop sync**. My project lived on the Desktop, which iCloud syncs. When files aren't accessed for a while, iCloud evicts them — marks them as "dataless" and stores only the metadata locally, with the actual content in the cloud. When the LaunchAgent tried to read one of these evicted files, it would IPC-block with the CloudKit daemon waiting for the download to complete. Under the macOS security model that LaunchAgent processes operate in, this manifested as EDEADLK.

The fix had two parts:

1. **Move all runtime data** to `~/.goose/data/` — a directory that's never iCloud-synced. No more evictions.
2. **Add retry logic** to every file read/write operation — eight attempts with exponential backoff, so even if a transient lock occurs, the app recovers gracefully.

Along the way I also learned about `brctl download` — a macOS command that forces iCloud to hydrate a dataless file on demand — and `com.apple.provenance`, a system-managed extended attribute that macOS uses to track file origin and that was contributing to the locking behavior.

When I later moved Goose to Docker on Linux, I had to remember to guard the `brctl` call — it doesn't exist on Linux, and an unguarded `FileNotFoundError` was aborting the entire startup sequence, preventing the UI from loading. Small bug, easy fix once you know what to look for.

---

## Learning gstack

One of the unexpected wins of this project was getting deep into **gstack** — a toolkit for working with Claude Code (Anthropic's AI coding assistant). gstack adds a layer of structured workflows on top of Claude: skills like `/investigate` for systematic debugging, `/plan-eng-review` for architecture reviews, `/ship` for deploying changes, and `/office-hours` for brainstorming.

Working on Goose taught me how to lean into these tools properly. Instead of just asking Claude to "fix the bug," `/investigate` walks through a four-phase process: investigate, analyze, hypothesize, implement — with the explicit rule that no fix gets written until the root cause is understood. That discipline is what cracked the EDEADLK issue.

The workflow that emerged: work locally on my Mac, use Claude Code with gstack to build and debug, commit to GitHub, then `git pull && docker compose up -d --build` on the server. Clean, repeatable, and fast.

---

## Moving to the Server

Getting Goose off my Mac and onto the basement server was its own project.

The containerization with Docker was straightforward in concept but had a few sharp edges:

- **`DATA_DIR` as an environment variable**: Rather than hardcoding paths, all runtime data paths (`/data` in Docker, `~/.goose/data` on macOS) are controlled by a single `DATA_DIR` env var. The same codebase runs in both environments without modification.
- **Named Docker volumes**: All the JSON data files, tokens, and cached content live in a Docker named volume (`goose_data:/data`) that persists across container restarts and rebuilds. `git pull && docker compose up --build` updates the code without touching any runtime state.
- **Secrets via `.env`**: API keys and tokens are kept out of the codebase entirely. They live in a `.env` file that gets `scp`'d to the server and is never committed to git.
- **`credentials.json` as a read-only volume mount**: The Google OAuth credentials file is mounted into the container at startup but can't be modified by the app.

The update workflow is now two commands on the server:

```bash
git pull
docker compose up -d --build
```

---

## What the UI Looks Like

The frontend is intentionally minimal and atmospheric. The color palette is warm amber and orange — the kind of glow you'd associate with a 1986 cockpit display, not a modern touchscreen. Monospace type, panel-based layout, the occasional visual nod to aviation instruments.

There's a main briefing panel where Claude's daily summary appears, a GroupMe SITREP panel with a manual refresh button for when I know I have unread messages, a weather section, a flight radar section, and the Spotify bar fixed to the bottom of the screen — always showing what's playing with live progress.

The design philosophy was: information-dense but readable. I want to open it in the morning, absorb what I need in sixty seconds, and close it.

---

## What Goose Does For Me Every Week

In practice, here's how Goose fits into my week:

**Every morning**: A fresh briefing is generated. I know what's on my calendar, what assignments are coming up, what the weather looks like, and a summary of anything important that happened in my group chats overnight.

**Throughout the day**: If I want to know what's been going on in GroupMe without actually scrolling through it, I hit the refresh button and get a current summary. If a plane is flying overhead, I can see it. Whatever I'm listening to is always visible at the bottom.

**Over time**: This is the part that makes Goose different from a static dashboard. Goose keeps a running log of my days — what was on my schedule, what I actually did, how my week played out. Over time, it builds a picture of my patterns: when I'm most productive, how I tend to handle busy weeks, what a typical Tuesday looks like versus a Thursday. That context feeds back into the briefings. The longer Goose runs, the more it sounds like it actually knows me — because it does. It's not just reading today's calendar, it's reading today's calendar through the lens of everything it's learned about how I operate.

**Every week**: I actually feel on top of things. That sounds small, but the problem Goose was solving — the friction of checking five different things just to know what my day looks like — is genuinely gone.

---

## What's Next

A few things on the roadmap:

- **Neutral news brief** — a morning summary of headlines from Reuters, AP, and BBC, summarized by Claude
- **Public access** — once the firewall is configured, Goose will be accessible from anywhere, not just my local network
- **Spotify redirect URI update** — tied to the above, the OAuth callback needs to point to the public address once it's live

The basement server becoming public is an interesting milestone. It means Goose stops being a local tool and starts being something I can pull up on my phone from campus, check between classes, and rely on from anywhere.

The bones are solid. The architecture is clean. Now it's just about adding the right features.

---

---

## The End Goal

The vision for Goose was never just a dashboard on a laptop. From the beginning, the mental model was Tony Stark's JARVIS — an ambient, always-on assistant that knows your context, responds to your voice, and actually does things in the world.

The roadmap to get there runs through a Raspberry Pi. The plan is to mount a touchscreen monitor on my wall, wire up a microphone and speakers, and turn Goose from a web app you open into a presence in the room. Wake word detection, voice synthesis, the works. At that point, the interaction changes completely:

> *"Goose, what's on the agenda today?"*
>
> *"Goose, lock up my room — I'm heading out."*
>
> *"Goose, turn the lights on at 8:00 so I actually get up."*

The smart home layer is already part of the plan — a smart lock on the door, light control, eventually anything that can be automated. The web app is the foundation. The wall-mounted panel is the interface. The voice is what makes it feel real.

For a college student, a working JARVIS isn't science fiction anymore. It's a Raspberry Pi, a few APIs, and enough free weekends to build it right.

---

*Built with FastAPI, Claude, and too much coffee. Named after the best wingman in cinema history.*
