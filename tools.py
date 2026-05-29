"""LLM function-call schemas and handlers for the receptionist bot."""

from __future__ import annotations

from datetime import date, datetime, time

from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.services.llm_service import FunctionCallParams

import clinic_info
from calendar_service import CalendarService
from clinic_info import CLINIC_TZ, TIMEZONE

# Maximum slots to return per check_availability call — keeps spoken responses short.
MAX_SLOTS_RETURNED = 6


CHECK_AVAILABILITY_SCHEMA = FunctionSchema(
    name="check_availability",
    description=(
        f"Check available {clinic_info.SLOT_MINUTES}-minute appointment slots for a given date. "
        "Use this whenever the caller asks about openings or proposes a time."
    ),
    properties={
        "date": {
            "type": "string",
            "description": "Date in YYYY-MM-DD format (Kyiv time).",
        },
        "preferred_time": {
            # Nullable: Groq strictly validates generated tool calls, and models
            # (e.g. gpt-oss) emit `null` for this optional field. A bare "string"
            # type rejects null → the LLM call errors and the bot goes silent.
            "type": ["string", "null"],
            "description": (
                "Optional preferred time HH:MM (24-hour, Kyiv time). "
                "If provided, results are sorted by proximity to this time."
            ),
        },
    },
    required=["date"],
)

BOOK_APPOINTMENT_SCHEMA = FunctionSchema(
    name="book_appointment",
    description=(
        "Book an appointment after the caller has confirmed name, reason, "
        "date, and time. Only call this once the caller has explicitly agreed."
    ),
    properties={
        "date": {"type": "string", "description": "Date in YYYY-MM-DD format (Kyiv time)."},
        "time": {"type": "string", "description": "Time in HH:MM (24-hour, Kyiv time)."},
        "patient_name": {"type": "string", "description": "Patient's full name."},
        "reason": {
            "type": "string",
            "description": "Short reason for the visit (e.g. cleaning, consultation).",
        },
    },
    required=["date", "time", "patient_name", "reason"],
)

GET_CLINIC_INFO_SCHEMA = FunctionSchema(
    name="get_clinic_info",
    description=(
        "Look up factual information about the clinic. "
        "Use for current hours, address, full service list, or prices."
    ),
    properties={
        "topic": {
            "type": "string",
            "enum": ["hours", "address", "services", "prices", "general"],
            "description": "Which piece of info to retrieve.",
        }
    },
    required=["topic"],
)


TOOLS_SCHEMA = ToolsSchema(
    standard_tools=[
        CHECK_AVAILABILITY_SCHEMA,
        BOOK_APPOINTMENT_SCHEMA,
        GET_CLINIC_INFO_SCHEMA,
    ]
)


def _parse_date(s: str) -> date | None:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _parse_time(s: str) -> time | None:
    try:
        return datetime.strptime(s, "%H:%M").time()
    except (ValueError, TypeError):
        return None


def make_handlers(calendar: CalendarService) -> dict:
    """Build async handlers bound to the given CalendarService."""

    async def check_availability(params: FunctionCallParams) -> None:
        args = params.arguments
        d = _parse_date(args.get("date", ""))
        if d is None:
            await params.result_callback(
                {"error": "Invalid date format. Use YYYY-MM-DD."}
            )
            return

        logger.debug("check_availability date={} preferred_time={}", args.get("date"), args.get("preferred_time"))

        try:
            slots = calendar.find_free_slots(d)
        except Exception as e:
            logger.exception("find_free_slots failed")
            await params.result_callback({"error": f"Calendar lookup failed: {e}"})
            return

        if not slots:
            await params.result_callback(
                {"date": d.isoformat(), "weekday": d.strftime("%A"), "free_slots": [], "note": "No free slots — clinic closed or fully booked."}
            )
            return

        preferred = _parse_time(args.get("preferred_time", "") or "")
        if preferred is not None:
            slots.sort(key=lambda s: abs(
                (s.hour * 60 + s.minute) - (preferred.hour * 60 + preferred.minute)
            ))

        formatted = [s.strftime("%H:%M") for s in slots[:MAX_SLOTS_RETURNED]]
        await params.result_callback(
            {"date": d.isoformat(), "weekday": d.strftime("%A"), "timezone": TIMEZONE, "free_slots": formatted}
        )

    async def book_appointment(params: FunctionCallParams) -> None:
        args = params.arguments
        d = _parse_date(args.get("date", ""))
        t = _parse_time(args.get("time", ""))
        name = (args.get("patient_name") or "").strip()
        reason = (args.get("reason") or "").strip()

        if d is None or t is None or not name or not reason:
            await params.result_callback(
                {"error": "Missing or invalid required fields."}
            )
            return

        start_dt = datetime.combine(d, t, tzinfo=CLINIC_TZ)
        duration = clinic_info.SLOT_MINUTES

        invalid_reason = calendar.validate_slot(start_dt, duration)
        if invalid_reason is not None:
            await params.result_callback({"status": "rejected", "message": invalid_reason})
            return

        try:
            if not calendar.is_slot_free(start_dt, duration):
                # Offer alternatives the same day.
                alternatives = [
                    s.strftime("%H:%M") for s in calendar.find_free_slots(d)[:MAX_SLOTS_RETURNED]
                ]
                await params.result_callback({
                    "status": "conflict",
                    "message": "That slot is no longer available.",
                    "alternatives": alternatives,
                })
                return
            created = calendar.create_event(start_dt, duration, name, reason)
        except Exception as e:
            logger.exception("book_appointment failed")
            await params.result_callback({"status": "error", "error": str(e)})
            return

        logger.info(
            "Booked appointment {} for {} ({}) at {}",
            created["event_id"], name, reason, start_dt.isoformat(),
        )
        await params.result_callback({
            "status": "booked",
            "confirmation_id": created["event_id"],
            "weekday": d.strftime("%A"),
            "start": created["start"],
            "end": created["end"],
            "patient_name": name,
            "reason": reason,
        })

    async def get_clinic_info(params: FunctionCallParams) -> None:
        topic = (params.arguments.get("topic") or "general").strip()
        await params.result_callback({"topic": topic, "info": clinic_info.get_info(topic)})

    return {
        "check_availability": check_availability,
        "book_appointment": book_appointment,
        "get_clinic_info": get_clinic_info,
    }
