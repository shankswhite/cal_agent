"""Scheduling agent: user text in -> intention (Pydantic) -> Cal.com call -> reply text out.

Flow (design figure):
    User Query -> Agent -> User Intention -> Booking | Get | Reschedule | Cancel -> Cal.com data

- Intention + details are extracted by Claude into the `UserIntent` Pydantic model
  (structured outputs via `client.beta.messages.parse`).
- Cal.com calls go through `cal_api.CalAPI` (sync), run in a thread so `respond()` stays async.
- Replies are built from real API data only; nothing is invented.

Usage:
    agent = SchedulingAgent()
    reply = await agent.respond("what's on my calendar tomorrow?")
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

import anthropic
from pydantic import BaseModel, Field

from cal_api import CalAPI, CalAPIError

ACTIVE_STATUSES = {"accepted", "pending", "awaiting_host"}
MAX_OPTIONS = 5
MAX_HISTORY = 20  # messages kept for multi-turn context


# ---------------------------------------------------------------------- #
# Intention schema (Pydantic)
# ---------------------------------------------------------------------- #
class Intention(str, Enum):
    BOOKING = "booking"
    GET_ALL_EVENTS = "get_all_events"
    RESCHEDULE = "reschedule"
    CANCEL = "cancel"
    UNSUPPORTED = "unsupported"  # anything outside the four above


class UserIntent(BaseModel):
    """What the user wants, extracted from the conversation.

    All datetimes are ISO 8601 WITH a UTC offset, in the user's time zone,
    e.g. "2026-11-19T14:00:00-08:00".
    """

    intention: Intention = Field(description="The user's intention for the latest message, using the whole conversation as context.")
    event_type: str | None = Field(None, description="Words naming the kind of meeting, e.g. 'intro', '30 min meeting'.")
    duration_minutes: int | None = Field(None, description="Meeting length in minutes if stated, e.g. 30.")
    attendee_name: str | None = Field(None, description="Name of the person to meet (booking), or used to identify an existing meeting.")
    attendee_email: str | None = Field(None, description="Email of the person to meet, only if the user gave it.")

    start_time: str | None = Field(
        None,
        description="An exact time. booking: the chosen start of the new meeting. "
        "reschedule/cancel: the start of the EXISTING meeting (e.g. 'my 3pm' -> today 15:00).",
    )
    range_start: str | None = Field(
        None,
        description="Start of a time range. booking: window to search for free slots (e.g. Thursday afternoon -> Thu 12:00). "
        "get_all_events: start of the period to list. reschedule/cancel: window containing the existing meeting when no exact time is given.",
    )
    range_end: str | None = Field(None, description="End of the time range described by range_start.")

    new_start_time: str | None = Field(None, description="reschedule only: exact new start time if the user chose one.")
    new_range_start: str | None = Field(None, description="reschedule only: start of window for the new time (e.g. 'later today' -> now).")
    new_range_end: str | None = Field(None, description="reschedule only: end of window for the new time (e.g. 'later today' -> today 23:59).")

    confirmed: bool = Field(
        False,
        description="cancel only: true ONLY if the assistant already asked to confirm cancelling a specific meeting "
        "and the user's latest message clearly says yes.",
    )


SYSTEM_PROMPT = """You extract scheduling intentions for a busy founder's Cal.com assistant.
Supported intentions: booking (create a new meeting), get_all_events (list meetings in a period),
reschedule (move an existing meeting), cancel (cancel an existing meeting). Anything else is "unsupported".

Rules:
- Use the whole conversation: a short reply like "2pm works" or "yes" continues the previous request.
  Carry over details already given (attendee, event type, duration, the meeting being moved/cancelled).
- Resolve relative dates ("tomorrow", "Thursday", "later today") using the current time given in the message.
- Output every datetime as ISO 8601 with the user's UTC offset. "Afternoon" = 12:00-17:00, "morning" = 08:00-12:00,
  "evening" = 17:00-21:00. A whole day = 00:00 to 23:59.
- Only fill a field when the user actually said it (or it is clearly implied). Never invent emails or names.
"""


# ---------------------------------------------------------------------- #
# Agent
# ---------------------------------------------------------------------- #
class SchedulingAgent:
    def __init__(
        self,
        cal: CalAPI | None = None,
        client: anthropic.AsyncAnthropic | None = None,
        model: str | None = None,
        user_timezone: str | None = None,
    ):
        self.cal = cal or CalAPI()
        self.client = client or anthropic.AsyncAnthropic()
        self.model = model or os.environ.get("LLM_MODEL", "claude-sonnet-5-5")
        self.tz = ZoneInfo(user_timezone or os.environ.get("DEFAULT_TIMEZONE") or "UTC")
        self.history: list[dict[str, str]] = []
        self._event_types: list[dict[str, Any]] | None = None

    # ---------------- public entry point ---------------- #
    async def respond(self, user_input: str) -> str:
        """Take the user's raw text, return the assistant's reply text."""
        try:
            intent = await self.identify_intent(user_input)
            if intent is None:
                reply = "Sorry, I couldn't process that request. Could you rephrase it?"
            else:
                reply = await self._dispatch(intent)
        except CalAPIError as e:
            reply = f"Cal.com returned an error: {e.message}"
        except anthropic.APIError as e:
            reply = f"The language model request failed: {e.__class__.__name__}. Please try again."

        self.history += [
            {"role": "user", "content": user_input},
            {"role": "assistant", "content": reply},
        ]
        self.history = self.history[-MAX_HISTORY:]
        return reply

    async def identify_intent(self, user_input: str) -> UserIntent | None:
        """Classify the latest message into a `UserIntent` (None on refusal / no parse)."""
        now = datetime.now(self.tz)
        content = (
            f"Current time: {now.isoformat(timespec='minutes')} ({now:%A}), "
            f"time zone {self.tz.key}.\n\nUser: {user_input}"
        )
        response = await self.client.beta.messages.parse(
            model=self.model,
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            messages=[*self.history, {"role": "user", "content": content}],
            output_format=UserIntent,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            return None
        return response.parsed_output

    # ---------------- routing ---------------- #
    async def _dispatch(self, intent: UserIntent) -> str:
        handlers = {
            Intention.BOOKING: self._handle_booking,
            Intention.GET_ALL_EVENTS: self._handle_get,
            Intention.RESCHEDULE: self._handle_reschedule,
            Intention.CANCEL: self._handle_cancel,
        }
        handler = handlers.get(intent.intention)
        if handler is None:
            return (
                "I can book a new meeting, show your scheduled meetings, "
                "reschedule a meeting, or cancel one. What would you like to do?"
            )
        return await handler(intent)

    # ---------------- Booking ---------------- #
    async def _handle_booking(self, intent: UserIntent) -> str:
        event_type, question = await self._resolve_event_type(intent)
        if question:
            return question

        missing = []
        if not intent.attendee_name:
            missing.append("who the meeting is with")
        if not intent.attendee_email:
            missing.append("their email (Cal.com sends them the invite)")
        if not (intent.start_time or intent.range_start):
            missing.append("when you'd like it")
        if missing:
            return f"To book the {event_type['title']}, I need {' and '.join(missing)}."

        duration = intent.duration_minutes if intent.duration_minutes in (event_type.get("lengthInMinutesOptions") or []) else None

        if intent.start_time:
            start = self._parse_dt(intent.start_time)
            slots = await self._slots(event_type["id"], start.replace(hour=0, minute=0), start.replace(hour=23, minute=59), duration)
            if not any(s == start for s, _ in slots):
                return self._no_exact_slot_reply(start, slots, f"for the {event_type['title']}")
            booking = await asyncio.to_thread(
                self.cal.create_booking,
                event_type_id=event_type["id"],
                start=self._utc(start),
                attendee_name=intent.attendee_name,
                attendee_email=intent.attendee_email,
                attendee_time_zone=self.tz.key,
                length_in_minutes=duration,
            )
            return (
                f"Booked: {booking['title']} on {self._fmt(booking['start'])} "
                f"({booking['duration']} min) with {intent.attendee_name}. Status: {booking['status']}."
            )

        start, end = self._range(intent.range_start, intent.range_end)
        slots = await self._slots(event_type["id"], start, end, duration)
        if not slots:
            return f"No free slots for the {event_type['title']} between {self._fmt(start)} and {self._fmt(end)}."
        return (
            f"Free times for a {event_type['title']} with {intent.attendee_name}:\n"
            + self._options(slots)
            + "\nWhich one should I book?"
        )

    # ---------------- Get ---------------- #
    async def _handle_get(self, intent: UserIntent) -> str:
        if intent.range_start:
            start, end = self._range(intent.range_start, intent.range_end)
        else:
            start = datetime.now(self.tz)
            end = start + timedelta(days=7)
        bookings = await self._active_bookings(start, end)
        period = f"{self._fmt(start)} – {self._fmt(end)}"
        if not bookings:
            return f"You have no meetings scheduled for {period}."
        lines = [f"Your meetings ({period}):"] + [f"- {self._describe(b)}" for b in bookings]
        return "\n".join(lines)

    # ---------------- Reschedule ---------------- #
    async def _handle_reschedule(self, intent: UserIntent) -> str:
        booking, question = await self._find_booking(intent, action="move")
        if question:
            return question

        event_type_id = booking["eventType"]["id"]
        if intent.new_start_time:
            new_start = self._parse_dt(intent.new_start_time)
            day_start, day_end = new_start.replace(hour=0, minute=0), new_start.replace(hour=23, minute=59)
            slots = await self._slots(event_type_id, day_start, day_end, None, booking["uid"])
            if not any(s == new_start for s, _ in slots):
                return self._no_exact_slot_reply(new_start, slots, f"to move \"{booking['title']}\" to")
            new = await asyncio.to_thread(self.cal.reschedule_booking, booking["uid"], self._utc(new_start))
            return f"Moved \"{new['title']}\" from {self._fmt(booking['start'])} to {self._fmt(new['start'])}."

        if intent.new_range_start:
            start, end = self._range(intent.new_range_start, intent.new_range_end)
            slots = await self._slots(event_type_id, start, end, None, booking["uid"])
            original = self._parse_dt(booking["start"])
            slots = [s for s in slots if s[0] != original]
            if not slots:
                return f"No free times to move \"{booking['title']}\" between {self._fmt(start)} and {self._fmt(end)}."
            return (
                f"Free times to move \"{booking['title']}\" (now {self._fmt(booking['start'])}):\n"
                + self._options(slots)
                + "\nWhich one works?"
            )

        return f"When would you like to move \"{booking['title']}\" ({self._fmt(booking['start'])}) to?"

    # ---------------- Cancel ---------------- #
    async def _handle_cancel(self, intent: UserIntent) -> str:
        booking, question = await self._find_booking(intent, action="cancel")
        if question:
            return question
        if not intent.confirmed:
            return f"Cancel {self._describe(booking)}? (yes/no)"
        await asyncio.to_thread(self.cal.cancel_booking, booking["uid"], "Cancelled via scheduling assistant")
        return f"Cancelled \"{booking['title']}\" on {self._fmt(booking['start'])}."

    # ---------------------------------------------------------------- #
    # Helpers: Cal.com lookups
    # ---------------------------------------------------------------- #
    async def _resolve_event_type(self, intent: UserIntent) -> tuple[dict[str, Any] | None, str | None]:
        """Pick the Cal.com event type matching the request, or return a question."""
        if self._event_types is None:
            self._event_types = await asyncio.to_thread(self.cal.list_event_types)
        types = [e for e in self._event_types if not e.get("hidden")] or self._event_types
        if not types:
            return None, "Your Cal.com account has no event types, so I can't book a meeting."

        candidates = types
        if intent.duration_minutes:
            candidates = [
                e for e in types
                if e["lengthInMinutes"] == intent.duration_minutes
                or intent.duration_minutes in (e.get("lengthInMinutesOptions") or [])
            ]
            if not candidates:
                available = ", ".join(f"{e['title']} ({e['lengthInMinutes']} min)" for e in types)
                return None, f"There's no {intent.duration_minutes}-minute event type. Available: {available}."
        if len(candidates) > 1 and intent.event_type:
            words = intent.event_type.lower()
            named = [e for e in candidates if e["slug"].lower() in words or e["title"].lower() in words]
            candidates = named or candidates
        if len(candidates) == 1:
            return candidates[0], None
        available = ", ".join(f"{e['title']} ({e['lengthInMinutes']} min)" for e in candidates)
        return None, f"Which type of meeting? Options: {available}."

    async def _slots(
        self, event_type_id: int, start: datetime, end: datetime,
        duration: int | None = None, reschedule_uid: str | None = None,
    ) -> list[tuple[datetime, datetime]]:
        data = await asyncio.to_thread(
            self.cal.get_available_slots,
            event_type_id, self._utc(start), self._utc(end),
            time_zone=self.tz.key, duration=duration, booking_uid_to_reschedule=reschedule_uid,
        )
        out = []
        for day in sorted(data):
            for slot in data[day]:
                s = self._parse_dt(slot["start"] if isinstance(slot, dict) else slot)
                e = self._parse_dt(slot["end"]) if isinstance(slot, dict) and slot.get("end") else s
                if start <= s <= end:
                    out.append((s, e))
        return out

    async def _active_bookings(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        bookings = await asyncio.to_thread(
            self.cal.get_bookings, after_start=self._utc(start), before_end=self._utc(end), sort_start="asc",
        )
        active = [b for b in bookings if b.get("status") in ACTIVE_STATUSES]
        return sorted(active, key=lambda b: b["start"])

    async def _find_booking(self, intent: UserIntent, action: str) -> tuple[dict[str, Any] | None, str | None]:
        """Locate the single existing booking the user means, or return a question."""
        if intent.start_time:
            target = self._parse_dt(intent.start_time)
            bookings = await self._active_bookings(target - timedelta(minutes=1), target + timedelta(days=1))
            bookings = [b for b in bookings if self._parse_dt(b["start"]) == target]
            where = f"at {self._fmt(target)}"
        elif intent.range_start:
            start, end = self._range(intent.range_start, intent.range_end)
            bookings = await self._active_bookings(start, end)
            where = f"between {self._fmt(start)} and {self._fmt(end)}"
        else:
            return None, f"Which meeting should I {action}? Tell me its day and time."

        if intent.attendee_name:
            name = intent.attendee_name.lower()
            by_name = [b for b in bookings if any(name in (a.get("name") or "").lower() for a in b.get("attendees", []))]
            bookings = by_name or bookings
        if intent.event_type and len(bookings) > 1:
            words = intent.event_type.lower()
            by_type = [b for b in bookings if b["eventType"]["slug"] in words or words in b["title"].lower()]
            bookings = by_type or bookings

        if not bookings:
            return None, f"I couldn't find an active meeting {where}."
        if len(bookings) > 1:
            lines = "\n".join(f"- {self._describe(b)}" for b in bookings[:MAX_OPTIONS])
            return None, f"I found several meetings {where}:\n{lines}\nWhich one should I {action}?"
        return bookings[0], None

    # ---------------------------------------------------------------- #
    # Helpers: time + formatting
    # ---------------------------------------------------------------- #
    def _parse_dt(self, value: str) -> datetime:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=self.tz)
        return dt.astimezone(self.tz)

    def _range(self, start: str, end: str | None) -> tuple[datetime, datetime]:
        s = self._parse_dt(start)
        e = self._parse_dt(end) if end else s.replace(hour=23, minute=59)
        return s, e

    @staticmethod
    def _utc(dt: datetime) -> str:
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _fmt(self, value: str | datetime) -> str:
        dt = self._parse_dt(value) if isinstance(value, str) else value.astimezone(self.tz)
        return dt.strftime("%a %b %-d, %-I:%M %p")

    def _options(self, slots: list[tuple[datetime, datetime]]) -> str:
        shown = slots[:MAX_OPTIONS]
        lines = [f"- {self._fmt(s)}" for s, _ in shown]
        if len(slots) > len(shown):
            lines.append(f"(+{len(slots) - len(shown)} more)")
        return "\n".join(lines)

    def _no_exact_slot_reply(self, wanted: datetime, slots: list[tuple[datetime, datetime]], what: str) -> str:
        if not slots:
            return f"{self._fmt(wanted)} isn't available {what}, and that day has no free slots."
        nearest = sorted(slots, key=lambda s: abs((s[0] - wanted).total_seconds()))[:MAX_OPTIONS]
        nearest.sort()
        return f"{self._fmt(wanted)} isn't available {what}. Closest free times:\n" + self._options(nearest)

    def _describe(self, b: dict[str, Any]) -> str:
        who = ", ".join(a.get("name") or a.get("email", "") for a in b.get("attendees", [])) or "no attendees"
        status = "" if b.get("status") == "accepted" else f" [{b.get('status')}]"
        return f"{self._fmt(b['start'])}: {b['title']} ({b['duration']} min) with {who}{status}"


# ---------------------------------------------------------------------- #
# Manual chat loop: python app/agent.py
# ---------------------------------------------------------------------- #
async def _main() -> None:
    agent = SchedulingAgent()
    print("Scheduling assistant (Ctrl+C to quit)")
    while True:
        try:
            text = await asyncio.to_thread(input, "> ")
        except (EOFError, KeyboardInterrupt):
            break
        if text.strip():
            print(await agent.respond(text))


if __name__ == "__main__":
    from pathlib import Path

    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    asyncio.run(_main())
