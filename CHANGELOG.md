# Changelog

All notable changes to this project. Newest first.

## 2026-11-17 — Web UI (Gradio)

### Added
- `app/web.py`: Gradio `ChatInterface` web UI (`.venv/bin/python app/web.py` → http://127.0.0.1:7860).
  - One `SchedulingAgent` per browser session via `gr.State`, so history and pending clarifications are per user.
  - Example prompts from `NOTES.md`; host/port configurable with `WEB_HOST` / `WEB_PORT`.
  - Chosen over Chainlit and Streamlit by user choice.
- `requirements.txt`: added `gradio`.

### Verified
- Page serves HTTP 200. A 3-turn clarification conversation through the Gradio API keeps context and ends with slot options. A new session starts with fresh state.

## 2026-11-17 — Clarification loop + interactive terminal

### Changed
- `app/agent.py`:
  - **Required details per intention.** The agent asks until they are all given:
    - booking: duration, start time (exact or a window), person joining
    - get_all_events: a specific day or time window (no more default of the next 7 days)
    - reschedule: the meeting's current start time and the new time; duration defaults to the meeting's current length
    - cancel: the meeting's start time and which meeting it is (event type or duration), then a yes/no confirmation
  - **Pending request.** A partial request is kept in `SchedulingAgent.pending`, and each follow-up answer is merged into it.
  - **Past times are rejected.** A booking or reschedule time in the past is reported and the user is asked for a future time.
  - **Owner email fallback.** With no attendee email, the booking uses `OWNER_EMAIL` as the contact. Cal.com rejects bookings with neither email nor phone (400 "Attendee must have at least one contact method").
  - **Interactive terminal chat** (`.venv/bin/python app/agent.py`), with `quit`/`exit`.
- `.env`: added `OWNER_EMAIL` (not tracked).

### Added
- `tests/test_samples.md` (user's test cases).
- `tests/run_samples.py`: runs the samples, multi-turn clarification flows, and a reschedule + cancel conversation on a throwaway meeting, with cleanup.

### Verified
- All 9 samples behave as intended. Sample 6 asks for the duration (rule 1). Sample 9 reports that no 3pm meeting exists, because there is none.
- Multi-turn flows complete after clarification (booking, get, reschedule).
- Live reschedule (picked from offered options) and cancel (after confirmation) on a real meeting worked. Nothing left on the calendar.

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
