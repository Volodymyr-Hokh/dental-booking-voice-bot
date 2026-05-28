"""Quick diagnostic: test Google Calendar read + write access.

Run inside Docker:
  docker compose run --rm bot python test_calendar.py
"""

from __future__ import annotations

from datetime import datetime

from clinic_info import CLINIC_TZ
from config import settings

calendar_id = settings.google_calendar_id
sa_path = settings.google_service_account_json

print(f"Calendar ID : {calendar_id}")
print(f"Service acct: {sa_path}")
print()

from calendar_service import CalendarService  # noqa: E402

try:
    svc = CalendarService(calendar_id=calendar_id, service_account_path=sa_path)
    print("✅  CalendarService created OK")
except Exception as e:
    print(f"❌  CalendarService init failed: {e}")
    raise SystemExit(1)

# ── 1. Free/busy read ────────────────────────────────────────────────────────
from datetime import date, timedelta  # noqa: E402

tomorrow = date.today() + timedelta(days=1)
try:
    slots = svc.find_free_slots(tomorrow)
    print(f"✅  find_free_slots for {tomorrow}: {[s.strftime('%H:%M') for s in slots[:3]]} ...")
except Exception as e:
    print(f"❌  find_free_slots failed: {e}")
    raise SystemExit(1)

# ── 2. Write (create + immediately delete a test event) ─────────────────────
test_start = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 23, 0, tzinfo=CLINIC_TZ)
try:
    created = svc.create_event(
        start_dt=test_start,
        duration_min=30,
        patient_name="TEST PATIENT - PLEASE DELETE",
        reason="calendar write access diagnostic",
    )
    print(f"✅  create_event OK — id={created['event_id']}")

    # Clean up the test event immediately.
    svc._svc.events().delete(calendarId=svc.calendar_id, eventId=created["event_id"]).execute()
    print("✅  Test event deleted (cleanup OK)")
except Exception as e:
    print(f"❌  create_event FAILED: {e}")
    print()
    print("Fix: Share your Google Calendar with the service account email")
    print("and grant 'Make changes to events' permission.")
    raise SystemExit(1)

print()
print("All checks passed — calendar read/write is working correctly.")
