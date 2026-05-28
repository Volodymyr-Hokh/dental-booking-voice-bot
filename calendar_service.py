"""Google Calendar wrapper: free-slot lookup and event creation."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from clinic_info import CLINIC_TZ, SLOT_MINUTES, TIMEZONE, WORKING_HOURS

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar"]


class CalendarService:
    def __init__(self, calendar_id: str, service_account_path: str) -> None:
        self.calendar_id = calendar_id
        creds = Credentials.from_service_account_file(
            service_account_path, scopes=SCOPES
        )
        self._svc = build("calendar", "v3", credentials=creds, cache_discovery=False)

    def _working_window(self, d: date) -> tuple[datetime, datetime] | None:
        hours = WORKING_HOURS.get(d.weekday())
        if hours is None:
            return None
        start_t, end_t = hours
        start = datetime.combine(d, start_t, tzinfo=CLINIC_TZ)
        end = datetime.combine(d, end_t, tzinfo=CLINIC_TZ)
        return start, end

    def find_free_slots(
        self, d: date, slot_minutes: int = SLOT_MINUTES
    ) -> list[datetime]:
        window = self._working_window(d)
        if window is None:
            return []
        start, end = window

        # Don't propose slots in the past.
        now = datetime.now(CLINIC_TZ)
        if start < now:
            # Round up to next slot boundary.
            delta = (now - start).total_seconds()
            steps = int(delta // (slot_minutes * 60)) + 1
            start = start + timedelta(minutes=steps * slot_minutes)
        if start >= end:
            return []

        body = {
            "timeMin": start.isoformat(),
            "timeMax": end.isoformat(),
            "items": [{"id": self.calendar_id}],
        }
        resp = self._svc.freebusy().query(body=body).execute()
        busy = resp["calendars"][self.calendar_id].get("busy", [])
        busy_ranges = [
            (
                datetime.fromisoformat(b["start"].replace("Z", "+00:00")),
                datetime.fromisoformat(b["end"].replace("Z", "+00:00")),
            )
            for b in busy
        ]

        slot = start
        free: list[datetime] = []
        delta = timedelta(minutes=slot_minutes)
        while slot + delta <= end:
            slot_end = slot + delta
            if not any(b_start < slot_end and b_end > slot for b_start, b_end in busy_ranges):
                free.append(slot)
            slot = slot_end
        return free

    def is_slot_free(self, start_dt: datetime, duration_min: int) -> bool:
        end_dt = start_dt + timedelta(minutes=duration_min)
        body = {
            "timeMin": start_dt.isoformat(),
            "timeMax": end_dt.isoformat(),
            "items": [{"id": self.calendar_id}],
        }
        resp = self._svc.freebusy().query(body=body).execute()
        busy = resp["calendars"][self.calendar_id].get("busy", [])
        return len(busy) == 0

    def create_event(
        self,
        start_dt: datetime,
        duration_min: int,
        patient_name: str,
        reason: str,
    ) -> dict:
        end_dt = start_dt + timedelta(minutes=duration_min)
        event = {
            "summary": f"Appointment: {patient_name}",
            "description": f"Reason: {reason}",
            "start": {"dateTime": start_dt.isoformat(), "timeZone": TIMEZONE},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": TIMEZONE},
        }
        created = self._svc.events().insert(
            calendarId=self.calendar_id, body=event
        ).execute()
        logger.info("Created event %s for %s at %s", created["id"], patient_name, start_dt)
        return {
            "event_id": created["id"],
            "html_link": created.get("htmlLink"),
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
        }
