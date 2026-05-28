"""Central configuration, loaded and validated once at import time.

All environment / `.env` settings live here so the rest of the codebase reads
typed attributes off a single `settings` object instead of scattered
`os.environ` lookups. Missing required keys (API keys) raise a clear
`ValidationError` at startup rather than a buried `KeyError` once a caller
connects.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

# Default ElevenLabs voice: "Rachel" — warm, professional female voice.
DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"

# STUN server used for WebRTC ICE on both the server and the browser client.
STUN_SERVER = "stun:stun.l.google.com:19302"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ── ElevenLabs ───────────────────────────────────────────────────────────
    elevenlabs_api_key: str
    elevenlabs_voice_id: str = DEFAULT_VOICE_ID

    # ── OpenAI ───────────────────────────────────────────────────────────────
    openai_api_key: str
    openai_model: str = "gpt-4o-mini"

    # ── Google Calendar ──────────────────────────────────────────────────────
    google_calendar_id: str = "primary"
    google_service_account_json: str = "credentials/service_account.json"

    # ── Server ───────────────────────────────────────────────────────────────
    host: str = "0.0.0.0"
    port: int = 7860

    # ── Logging ──────────────────────────────────────────────────────────────
    log_level: str = "INFO"
    log_file: str = "logs/bot.log"
    log_rotation: str = "10 MB"
    log_retention: str = "7 days"
    # Dump the full conversation context to logs/transcripts/ when a call ends.
    transcript_log: bool = True

    @property
    def debug(self) -> bool:
        """True when verbose diagnostics (audio levels, frame logging) are on."""
        return self.log_level.upper() == "DEBUG"


settings = Settings()
