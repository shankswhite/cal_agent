"""Thin client for the Cal.com API v2, covering the agent's four intentions.

Design (see design figure):
    Booking    -> CalAPI.create_booking        (+ list_event_types / get_available_slots to resolve inputs)
    Get        -> CalAPI.get_bookings / get_booking
    Reschedule -> CalAPI.reschedule_booking    (+ get_available_slots with booking_uid_to_reschedule)
    Cancel     -> CalAPI.cancel_booking

Endpoints, headers and fields follow api_notes.md / https://cal.com/docs/api-reference/v2.
Every endpoint pins its own `cal-api-version`, as the docs require.
This module only wraps HTTP calls: no intent parsing, no date math, no caching.
"""

from __future__ import annotations

import os
from typing import Any

import requests

BASE_URL = "https://api.cal.com/v2"

# cal-api-version per endpoint group, as stated in the Cal.com docs.
VERSION_EVENT_TYPES = "2026-06-12"
VERSION_SLOTS = "2024-09-04"
VERSION_BOOKINGS = "2026-02-25"       # create / get one / reschedule / cancel
VERSION_BOOKINGS_LIST = "2026-05-01"  # GET /v2/bookings

BOOKING_STATUSES = ("upcoming", "recurring", "past", "cancelled", "unconfirmed")


class CalAPIError(Exception):
    """Raised when Cal.com returns a non-2xx response or `status: "error"`."""

    def __init__(self, status_code: int, message: str, payload: Any = None):
        super().__init__(f"Cal.com API error {status_code}: {message}")
        self.status_code = status_code
        self.message = message
        self.payload = payload


class CalAPI:
    def __init__(self, api_key: str | None = None, base_url: str = BASE_URL, timeout: float = 30.0):
        self.api_key = api_key or os.environ.get("CAL_API_KEY")
        if not self.api_key:
            raise ValueError("Cal.com API key missing: pass api_key or set CAL_API_KEY.")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    # ------------------------------------------------------------------ #
    # HTTP
    # ------------------------------------------------------------------ #
    def _request(
        self,
        method: str,
        path: str,
        version: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "cal-api-version": version,
        }
        if params:
            params = {k: v for k, v in params.items() if v is not None}
        resp = self.session.request(
            method, f"{self.base_url}{path}", headers=headers,
            params=params, json=json, timeout=self.timeout,
        )
        try:
            body = resp.json()
        except ValueError:
            body = None

        if not resp.ok or (isinstance(body, dict) and body.get("status") == "error"):
            message = resp.reason
            if isinstance(body, dict):
                err = body.get("error")
                if isinstance(err, dict):
                    message = err.get("message") or err.get("code") or message
                elif err:
                    message = str(err)
            raise CalAPIError(resp.status_code, message, body)
        return body

    # ------------------------------------------------------------------ #
    # Event types: resolve "Event Type" / "Duration" for Booking
    # GET /v2/event-types
    # ------------------------------------------------------------------ #
    def list_event_types(self) -> list[dict[str, Any]]:
        """Event types of the authenticated user (includes hidden ones).

        Useful fields per item: id, title, slug, lengthInMinutes,
        lengthInMinutesOptions, description, hidden, bookingFields.
        """
        return self._request("GET", "/event-types", VERSION_EVENT_TYPES)["data"]

    def get_event_type(self, event_type_id: int | str) -> dict[str, Any]:
        """GET /v2/event-types/{eventTypeId}."""
        return self._request("GET", f"/event-types/{event_type_id}", VERSION_EVENT_TYPES)["data"]

    # ------------------------------------------------------------------ #
    # Slots: availability for Booking / Reschedule
    # GET /v2/slots
    # ------------------------------------------------------------------ #
    def get_available_slots(
        self,
        event_type_id: int,
        start: str,
        end: str,
        time_zone: str | None = None,
        duration: int | None = None,
        booking_uid_to_reschedule: str | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Free slots for an event type between `start` and `end`.

        start / end: UTC ISO 8601 (`2026-11-19` or `2026-11-19T13:00:00Z`).
        time_zone:   IANA zone for returned times (Cal.com default is UTC).
        duration:    only for multi-duration event types.
        booking_uid_to_reschedule: shows that booking's own slot as free.

        Returns {"YYYY-MM-DD": [{"start": ..., "end": ...}, ...]}  (format=range).
        """
        params = {
            "eventTypeId": event_type_id,
            "start": start,
            "end": end,
            "timeZone": time_zone,
            "duration": duration,
            "bookingUidToReschedule": booking_uid_to_reschedule,
            "format": "range",
        }
        return self._request("GET", "/slots", VERSION_SLOTS, params=params)["data"]

    # ------------------------------------------------------------------ #
    # Booking
    # POST /v2/bookings
    # ------------------------------------------------------------------ #
    def create_booking(
        self,
        event_type_id: int,
        start: str,
        attendee_name: str,
        attendee_time_zone: str,
        attendee_email: str | None = None,
        length_in_minutes: int | None = None,
        guests: list[str] | None = None,
        location: dict[str, Any] | None = None,
        booking_fields_responses: dict[str, Any] | None = None,
        metadata: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Create a booking.

        start:             UTC ISO 8601, e.g. "2026-11-19T14:00:00Z".
        length_in_minutes: only if the event type allows multiple lengths.
        location:          e.g. {"type": "integration", "integration": "cal-video"}.
        booking_fields_responses: custom booking-field slug -> value.

        Note: the API has no free-text "title" field; the booking title comes
        from the event type.
        """
        attendee: dict[str, Any] = {"name": attendee_name, "timeZone": attendee_time_zone}
        if attendee_email:
            attendee["email"] = attendee_email

        body: dict[str, Any] = {"eventTypeId": event_type_id, "start": start, "attendee": attendee}
        if length_in_minutes is not None:
            body["lengthInMinutes"] = length_in_minutes
        if guests:
            body["guests"] = guests
        if location:
            body["location"] = location
        if booking_fields_responses:
            body["bookingFieldsResponses"] = booking_fields_responses
        if metadata:
            body["metadata"] = metadata
        return self._request("POST", "/bookings", VERSION_BOOKINGS, json=body)["data"]

    # ------------------------------------------------------------------ #
    # Get
    # GET /v2/bookings, GET /v2/bookings/{uid}
    # ------------------------------------------------------------------ #
    def get_bookings(
        self,
        status: str | None = None,
        after_start: str | None = None,
        before_end: str | None = None,
        event_type_id: int | None = None,
        attendee_email: str | None = None,
        attendee_name: str | None = None,
        sort_start: str | None = "asc",
        limit: int = 50,
        max_results: int | None = None,
    ) -> list[dict[str, Any]]:
        """List bookings, following cursor pagination.

        status:      one of BOOKING_STATUSES (one per request, per the docs).
        after_start: bookings starting after this ISO datetime.
        before_end:  bookings ending before this ISO datetime.
        max_results: stop after this many items (None = all pages).

        Each booking has uid, title, status, start, end, duration,
        eventType {id, slug}, attendees, hosts, location, ...
        """
        if status is not None and status not in BOOKING_STATUSES:
            raise ValueError(f"status must be one of {BOOKING_STATUSES}")

        params: dict[str, Any] = {
            "status": status,
            "afterStart": after_start,
            "beforeEnd": before_end,
            "eventTypeId": event_type_id,
            "attendeeEmail": attendee_email,
            "attendeeName": attendee_name,
            "sortStart": sort_start,
            "limit": limit,
        }
        results: list[dict[str, Any]] = []
        while True:
            body = self._request("GET", "/bookings", VERSION_BOOKINGS_LIST, params=params)
            results.extend(body.get("data") or [])
            if max_results is not None and len(results) >= max_results:
                return results[:max_results]
            pagination = body.get("pagination") or {}
            if not pagination.get("hasMore") or not pagination.get("nextCursor"):
                return results
            params["cursor"] = pagination["nextCursor"]

    def get_booking(self, booking_uid: str) -> dict[str, Any] | list[dict[str, Any]]:
        """One booking by uid (a recurring-booking uid returns a list)."""
        return self._request("GET", f"/bookings/{booking_uid}", VERSION_BOOKINGS)["data"]

    # ------------------------------------------------------------------ #
    # Reschedule
    # POST /v2/bookings/{uid}/reschedule
    # ------------------------------------------------------------------ #
    def reschedule_booking(
        self,
        booking_uid: str,
        new_start: str,
        length_in_minutes: int | None = None,
        rescheduled_by: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Move a booking to `new_start` (UTC ISO 8601).

        Returns the NEW booking (it has a new uid; the old one keeps
        rescheduledToUid). Only `accepted` / `pending` bookings can be
        rescheduled; others return 400. The event type cannot be changed.
        length_in_minutes: only if the event type allows multiple lengths.
        rescheduled_by: owner email auto-confirms when confirmation is required.
        """
        body: dict[str, Any] = {"start": new_start}
        if length_in_minutes is not None:
            body["lengthInMinutes"] = length_in_minutes
        if rescheduled_by:
            body["rescheduledBy"] = rescheduled_by
        if reason:
            body["reschedulingReason"] = reason
        return self._request(
            "POST", f"/bookings/{booking_uid}/reschedule", VERSION_BOOKINGS, json=body
        )["data"]

    # ------------------------------------------------------------------ #
    # Cancel
    # POST /v2/bookings/{uid}/cancel
    # ------------------------------------------------------------------ #
    def cancel_booking(
        self,
        booking_uid: str,
        reason: str | None = None,
        cancel_subsequent_bookings: bool | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """Cancel a booking (a recurring-booking uid cancels all occurrences).

        cancel_subsequent_bookings: recurring only; also cancels later occurrences.
        """
        body: dict[str, Any] = {}
        if reason:
            body["cancellationReason"] = reason
        if cancel_subsequent_bookings is not None:
            body["cancelSubsequentBookings"] = cancel_subsequent_bookings
        return self._request(
            "POST", f"/bookings/{booking_uid}/cancel", VERSION_BOOKINGS, json=body
        )["data"]
