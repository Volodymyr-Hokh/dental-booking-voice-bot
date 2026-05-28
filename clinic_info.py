"""Placeholder clinic data — edit these constants to match a real clinic."""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

CLINIC_NAME = "Bright Smile Dental"
ADDRESS = "123 Elm Street, Suite 4, Springfield"
PHONE_DISPLAY = "+1 (555) 010-2025"

# Mon=0 .. Sun=6. Missing keys = closed.
WORKING_HOURS: dict[int, tuple[time, time]] = {
    0: (time(9, 0), time(18, 0)),
    1: (time(9, 0), time(18, 0)),
    2: (time(9, 0), time(18, 0)),
    3: (time(9, 0), time(18, 0)),
    4: (time(9, 0), time(18, 0)),
}

SLOT_MINUTES = 30
TIMEZONE = "Europe/Kyiv"
TIMEZONE_LABEL = "Kyiv time"
CLINIC_TZ = ZoneInfo(TIMEZONE)

SERVICES: list[dict] = [
    {
        "name": "Consultation",
        "duration_min": 30,
        "price_usd": 50,
        "description": "Initial check-up and treatment-plan discussion.",
    },
    {
        "name": "Cleaning",
        "duration_min": 30,
        "price_usd": 90,
        "description": "Professional teeth cleaning and polishing.",
    },
    {
        "name": "Filling",
        "duration_min": 30,
        "price_usd": 150,
        "description": "Composite filling for a single tooth.",
    },
    {
        "name": "Whitening",
        "duration_min": 30,
        "price_usd": 250,
        "description": "In-office teeth whitening session.",
    },
    {
        "name": "Extraction",
        "duration_min": 30,
        "price_usd": 200,
        "description": "Simple tooth extraction.",
    },
]


def format_services_for_prompt() -> str:
    lines = []
    for s in SERVICES:
        lines.append(
            f"- {s['name']}: ${s['price_usd']} "
            f"({s['duration_min']} min) — {s['description']}"
        )
    return "\n".join(lines)


def format_hours_for_prompt() -> str:
    days = ["Monday", "Tuesday", "Wednesday", "Thursday",
            "Friday", "Saturday", "Sunday"]
    lines = []
    for i, d in enumerate(days):
        if i in WORKING_HOURS:
            start, end = WORKING_HOURS[i]
            lines.append(f"- {d}: {start.strftime('%H:%M')}–{end.strftime('%H:%M')} {TIMEZONE_LABEL}")
        else:
            lines.append(f"- {d}: closed")
    return "\n".join(lines)


def format_date_reference(days: int = 14) -> str:
    """Upcoming dates with weekday + open/closed status.

    Lets the model map weekday names ("Monday") and relative days ("tomorrow")
    to an exact YYYY-MM-DD by lookup instead of doing weekday arithmetic, which
    small models get wrong.
    """
    today = datetime.now(CLINIC_TZ).date()
    lines = []
    for i in range(days):
        d = today + timedelta(days=i)
        status = "open" if d.weekday() in WORKING_HOURS else "closed"
        label = "today" if i == 0 else ("tomorrow" if i == 1 else "")
        suffix = f" ({label})" if label else ""
        lines.append(f"- {d.isoformat()} {d.strftime('%A')} — {status}{suffix}")
    return "\n".join(lines)


def get_info(topic: str) -> str:
    topic = topic.lower().strip()
    if topic == "hours":
        return f"Working hours ({TIMEZONE_LABEL}):\n{format_hours_for_prompt()}"
    if topic == "address":
        return f"{CLINIC_NAME} is located at {ADDRESS}."
    if topic in ("services", "prices"):
        return f"Services and prices:\n{format_services_for_prompt()}"
    if topic == "general":
        return (
            f"{CLINIC_NAME} — {ADDRESS}. Phone: {PHONE_DISPLAY}. "
            f"All times in {TIMEZONE_LABEL}."
        )
    return f"No information for topic '{topic}'."
