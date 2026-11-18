# Changelog

All notable changes to this project. Newest first.

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
