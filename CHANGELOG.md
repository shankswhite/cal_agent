# Changelog

All notable changes to this project. Newest first.

## 2026-11-17 — Agent

### Added
- `app/agent.py`: `SchedulingAgent` with `async respond(user_input) -> str`.
  - Intention detection with the Pydantic model `UserIntent`: `booking`, `get_all_events`, `reschedule`, `cancel`, plus `unsupported` as the fallback. It uses Claude structured outputs (`client.beta.messages.parse`, model from `LLM_MODEL`) with server-side refusal fallback (`fallbacks="default"`).
  - Dispatches to `cal_api.CalAPI`, run in a thread so `respond()` stays async. Replies are built only from Cal.com data.
  - Booking: resolves the event type by duration or name, asks for missing attendee name/email/time, checks the requested time against free slots, and offers alternatives.
  - Get: lists active meetings in the requested period (default: next 7 days).
  - Reschedule: finds the meeting by time/attendee/type, checks the new time (or offers free times in a window), then reschedules.
  - Cancel: finds the meeting, asks for yes/no confirmation, then cancels.
  - Keeps the last 20 messages so follow-ups like "2pm works" and "yes" carry context.
  - `python app/agent.py` starts a terminal chat loop for manual testing.
- `requirements.txt`: added `anthropic`, `pydantic`.

### Verified
- Intention classification is correct for all three `NOTES.md` examples, plus a cancel request and an off-topic message (`unsupported`).
- End-to-end read-only turns work: listing tomorrow's meetings, and a booking request that asks for missing details and then offers Thursday-afternoon slots.
- Not yet tested end-to-end (they change real data): confirming a booking, reschedule, cancel through the agent.

## 2026-11-17 — Live API test

### Added
- `tests/live_test_cal_api.py`: end-to-end live test of every `CalAPI` method. It creates a throwaway booking, gets it, reschedules it, cancels it, and always cleans up. The attendee comes from `TEST_ATTENDEE_EMAIL`, which must be a real address.

### Changed
- `cal_api.py` moved to `app/cal_api.py` (moved by user, contents unchanged).

### Verified
- 10/10 live checks passed: list/get event types, slots (including `booking_uid_to_reschedule`), create, get one, list with filters, reschedule, cancel, and status after cancel. No test bookings left active.
- Finding: Cal.com rejects undeliverable attendee emails (e.g. `test@example.com`) with 400 "This email address cannot receive mail".

## 2026-11-17 — Cal.com API client

### Added
- `cal_api.py`: `CalAPI` client for the design figure's intentions:
  - Booking: `create_booking`, plus `list_event_types` / `get_event_type` / `get_available_slots` to resolve event type, duration and free start times.
  - Get: `get_bookings` (filters plus cursor pagination), `get_booking`.
  - Reschedule: `reschedule_booking`.
  - Cancel: `cancel_booking`.
  - `CalAPIError` for non-2xx / `status: "error"` responses. Each endpoint pins its own `cal-api-version`.
- `requirements.txt`: `requests`, `python-dotenv`.

### Verified
- Read-only calls tested live: list/get event types, slots, list bookings, get booking (including the 404 error path).
- Not yet tested live (they change real data): create, reschedule, cancel.

## 2026-11-17 — Project setup

### Added
- `api_notes.md`: Cal.com API v2 reference (auth, slots, bookings), sourced from the official docs.
- Python 3.13 virtual environment in `.venv/` (not tracked by git).
- `.gitignore`: excludes `.env`, `.venv/`, Python caches, and OS/editor files.
- `requirements.txt` (empty for now).
- `CHANGELOG.md`: this file.
