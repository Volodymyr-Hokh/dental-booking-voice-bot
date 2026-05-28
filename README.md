# Dental Booking Voice Bot

AI voice receptionist for a dental clinic, built on **Pipecat** + **ElevenLabs** (STT & TTS) + **OpenAI** (LLM) + **Google Calendar**. Includes a tiny browser test page so you can talk to the bot through your mic.

## What it does

- Greets callers and answers FAQs (hours, prices, services, address).
- Checks availability in Google Calendar (Mon–Fri, 30-min slots, working hours from [clinic_info.py](clinic_info.py)).
- Books appointments by creating Calendar events.
- Refuses medical advice; defers to dentist.

Phone integration (Twilio) is **not** wired up yet — for now the bot is reachable through a browser page only.

## Quick start (Docker, recommended on Windows)

### 1. Configure `.env`

```
ELEVENLABS_API_KEY=...
ELEVENLABS_VOICE_ID=21m00Tcm4TlvDq8ikWAM   # Rachel by default
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-4o-mini                    # optional, this is the default
GOOGLE_CALENDAR_ID=primary                  # or a specific calendar ID
GOOGLE_SERVICE_ACCOUNT_JSON=credentials/service_account.json
HOST=0.0.0.0
PORT=7860
LOG_LEVEL=INFO                              # set to DEBUG for verbose audio/VAD diagnostics
```

### 2. Set up Google Calendar

1. Create a Google Cloud project, enable Calendar API.
2. Create a **service account**, download its JSON key → save to `credentials/service_account.json`.
3. Open the calendar you want bookings in → **Settings and sharing** → **Share with specific people** → add the service account's `client_email` (from the JSON) → grant **Make changes to events**.
4. Copy that calendar's ID (looks like `abc...@group.calendar.google.com`) into `GOOGLE_CALENDAR_ID`. Use `primary` only if you've shared the service account's own primary calendar (rare).

> If `GOOGLE_CALENDAR_ID=primary` and the service account doesn't have its own visible primary calendar, you'll get 404s on first booking — use a real calendar ID.

### 3. Enable Docker Desktop host networking (Windows / macOS)

WebRTC needs to negotiate UDP candidates the browser can actually reach. With normal Docker port mapping, ICE candidates point to the container's internal IP, which the browser can't connect to. The cleanest fix is host networking, supported in **Docker Desktop 4.34+**:

**Docker Desktop → Settings → Resources → Network → enable “Host networking”.**

Apply, restart Docker Desktop if prompted.

### 4. Run

```powershell
docker compose up --build
```

Open <http://localhost:7860> → click **Connect** → allow mic → talk to the bot.

Stop with `Ctrl+C`, then `docker compose down`.

#### Fallback if host networking isn't available

```powershell
docker compose -f docker-compose.yml -f docker-compose.ports.yml up --build
```

This switches to bridge networking + port mapping. Signaling (the HTTP API and static page) will work, but WebRTC media may fail to connect — if so, enable host networking instead.

#### Iterating on code

The compose file bind-mounts the project directory into the container at `/app`. Most edits (prompt, clinic data, tool handlers) take effect on container restart:

```powershell
docker compose restart
```

Logs:

```powershell
docker compose logs -f
```

## Quick start (without Docker)

Python 3.10+ recommended.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

Open <http://localhost:7860>.

## Things to try

- "What are your hours?"
- "How much is a cleaning?"
- "Do you have anything tomorrow morning?"
- "Book me at 10:00 tomorrow, name John Doe, reason cleaning."
- "Should I get a root canal?" → should defer to the dentist.

## Project layout

| Path | What's in it |
|------|-------------|
| [main.py](main.py) | FastAPI app + `/api/offer` WebRTC signaling |
| [bot.py](bot.py) | Pipecat pipeline (STT → LLM → TTS) |
| [prompts.py](prompts.py) | Receptionist system prompt |
| [clinic_info.py](clinic_info.py) | Hardcoded clinic data — **edit this** for a real clinic |
| [calendar_service.py](calendar_service.py) | Google Calendar wrapper |
| [tools.py](tools.py) | LLM function-call schemas and handlers |
| [config.py](config.py) | Central settings (pydantic `BaseSettings`) loaded from env / `.env` |
| [test_calendar.py](test_calendar.py) | Standalone diagnostic for Calendar read/write access |
| [static/](static/) | Browser test page (vanilla WebRTC) |
| [Dockerfile](Dockerfile) | Container image |
| [docker-compose.yml](docker-compose.yml) | `host` and `ports` profiles |

## Tuning notes

- **Voice**: change `ELEVENLABS_VOICE_ID` (browse at <https://elevenlabs.io/voice-library>).
- **Working hours / services / prices**: edit constants in [clinic_info.py](clinic_info.py).
- **Slot length**: `SLOT_MINUTES` in [clinic_info.py](clinic_info.py) — the single source of truth. Availability, booking duration, and the spoken/tool wording all derive from it.
- **Diagnostics**: set `LOG_LEVEL=DEBUG` to enable per-frame audio-level logging and VAD/transcription frame tracing (off by default to keep demo logs clean).
- **Timezone**: currently `Europe/Kyiv`. All working hours, availability, and bookings are interpreted in this zone (DST handled automatically via `zoneinfo`). To switch, update `TIMEZONE` / `TIMEZONE_LABEL` in [clinic_info.py](clinic_info.py); everything else reads from `CLINIC_TZ`.

## Next steps (not in this version)

- Twilio Media Streams transport for real phone calls.
- Cancellation / reschedule flows.
- Multi-language support.
- Auth on the test page.
