"""Scheduling agent: user text in -> intention (Pydantic) -> Cal.com call -> reply text out.

Flow (design figure):
    User Query -> Agent -> User Intention -> Booking | Get | Reschedule | Cancel -> Cal.com data

- Intention + details are extracted by Claude into the `UserIntent` Pydantic model
  (structured outputs via `client.beta.messages.parse`).
- If required details are missing, the agent asks for them and keeps the partial
  request (`pending`), merging each follow-up answer until it is complete:
    booking         -> duration, start time, person joining
    get_all_events  -> a specific day or time window
    reschedule      -> the meeting's current start time, the new time
                       (duration defaults to the meeting's current length)
    cancel          -> the meeting's start time and its event type
- Times in the past are rejected for booking / reschedule targets.
- Cal.com calls go through `cal_api.CalAPI` (sync), run in a thread so `respond()` stays async.
- Replies are built from real API data only; nothing is invented.

Usage:
    agent = SchedulingAgent()
    reply = await agent.respond("what's on my calendar tomorrow?")

Terminal:  .venv/bin/python app/agent.py
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
    UNSUPPORTED = "unsupported"  # unclear, or anything outside the four above


class UserIntent(BaseModel):
    """What the user wants, extracted from the conversation.

    All datetimes are ISO 8601 WITH a UTC offset, in the user's time zone,
    e.g. "2026-11-19T14:00:00-08:00".
    """

    intention: Intention = Field(description="The user's intention for the latest message, using the whole conversation as context.")
    event_type: str | None = Field(None, description="Words naming the kind of meeting, e.g. 'intro', '30 min meeting'.")
    duration_minutes: int | None = Field(None, description="Meeting length in minutes if stated, e.g. 30.")
    attendee_name: str | None = Field(
        None, description="The person joining. A name ('Steven') or a description if unnamed ('a candidate' -> 'Candidate')."
    )
    attendee_email: str | None = Field(None, description="Email of the person joining, only if the user gave it.")

    start_time: str | None = Field(
        None,
        description="An exact time. booking: the chosen start of the new meeting. "
        "reschedule/cancel: the start of the EXISTING meeting (e.g. 'my 3pm' -> today 15:00).",
    )
    range_start: str | None = Field(
        None,
        description="Start of a time range. booking: window to search for free slots (e.g. Thursday afternoon -> Thu 12:00). "
        "get_all_events: start of the period to list. reschedule: window containing the existing meeting when no exact time is given.",
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

    def merged_with(self, newer: "UserIntent") -> "UserIntent":
        """Fill this (pending) intent with any details the newer message added."""
        data = self.model_dump()
        for key, value in newer.model_dump(exclude={"intention"}).items():
            if value not in (None, False):
                data[key] = value
        return UserIntent(**data)


SYSTEM_PROMPT = """You extract scheduling intentions for a busy founder's Cal.com assistant.
Supported intentions: booking (create a new meeting), get_all_events (list meetings in a period),
reschedule (move an existing meeting), cancel (cancel an existing meeting).
Use "unsupported" when the message is about something else or too vague to tell (e.g. "help me").
A message like "talk with Steven" or "meet Anna" is a booking.

Rules:
- Use the whole conversation: a short reply like "2pm works", "30 minutes" or "yes" continues the
  previous request, so keep its intention and carry over details already given.
- Resolve relative dates ("tomorrow", "Thursday", "later today") using the current time given in the message.
- Output every datetime as ISO 8601 with the user's UTC offset. "Afternoon" = 12:00-17:00, "morning" = 08:00-12:00,
  "evening" = 17:00-21:00. A whole day = 00:00 to 23:59. A time with no day means today.
- Only fill a field when the user actually said it (or it is clearly implied). Never invent emails, names,
  durations or times. Leave fields empty rather than guessing.
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
        owner_email: str | None = None,
    ):
        self.cal = cal or CalAPI()
        self.client = client or anthropic.AsyncAnthropic()
        self.model = model or os.environ.get("LLM_MODEL", "claude-sonnet-5-5")
        self.tz = ZoneInfo(user_timezone or os.environ.get("DEFAULT_TIMEZONE") or "UTC")
        # Cal.com requires an attendee contact; used when the user gives no email.
        self.owner_email = owner_email or os.environ.get("OWNER_EMAIL")
        self.history: list[dict[str, str]] = []
        self.pending: UserIntent | None = None  # request still waiting for details
        self._event_types: list[dict[str, Any]] | None = None

    # ---------------- public entry point ---------------- #
    async def respond(self, user_input: str) -> str:
        """Take the user's raw text, return the assistant's reply text."""
        try:
            intent = await self.identify_intent(user_input)
            if intent is None:
                reply = "Sorry, I couldn't process that request. Could you rephrase it?"
            else:
                reply = await self._dispatch(self._with_pending(intent))
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
        now = self._now()
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

    def _with_pending(self, intent: UserIntent) -> UserIntent:
        """Merge a follow-up answer into the request that is waiting for details."""
        if self.pending is None:
            return intent
        if intent.intention in (self.pending.intention, Intention.UNSUPPORTED):
            return self.pending.merged_with(intent)
        return intent  # user switched to a different request

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
            self.pending = None
            return (
                "I didn't catch what you'd like to do. I can:\n"
                "- book a meeting (tell me who, how long, and when)\n"
                "- show your meetings (tell me the day or time window)\n"
                "- reschedule a meeting (tell me its current time and the new time)\n"
                "- cancel a meeting (tell me its time and which meeting)"
            )
        self.pending = None
        return await handler(intent)

    def _ask(self, intent: UserIntent, question: str) -> str:
        """Keep the partial request and ask the user to clarify."""
        self.pending = intent
        return question

    # ---------------- Booking ---------------- #
    async def _handle_booking(self, intent: UserIntent) -> str:
        # A time in the past is treated as missing, and reported first.
        past_note = self._drop_past_booking_time(intent)

        missing: list[str] = []
        event_type, options = await self._resolve_event_type(intent)
        if event_type is None:
            missing.append(options if options.startswith("a length") else f"how long it should be ({options})")
        if not intent.attendee_name:
            missing.append("who is joining")
        if not (intent.start_time or intent.range_start):
            missing.append("a future time" if past_note else "when (day and time)")
        if missing:
            return self._ask(intent, f"{past_note}To book this meeting, please tell me {self._join(missing)}.")

        if intent.start_time:
            start = self._parse_dt(intent.start_time)
        else:
            start, end = self._range(intent.range_start, intent.range_end)

        email = intent.attendee_email or self.owner_email
        if not email:
            return self._ask(intent, f"What is {intent.attendee_name}'s email? Cal.com needs it to send the invite.")

        duration = intent.duration_minutes if intent.duration_minutes in (event_type.get("lengthInMinutesOptions") or []) else None

        if intent.start_time:
            slots = await self._slots(event_type["id"], start.replace(hour=0, minute=0), start.replace(hour=23, minute=59), duration)
            if not any(s == start for s, _ in slots):
                intent.start_time = None
                return self._ask(intent, self._no_exact_slot_reply(start, slots, f"for a {event_type['title']}"))
            booking = await asyncio.to_thread(
                self.cal.create_booking,
                event_type_id=event_type["id"],
                start=self._utc(start),
                attendee_name=intent.attendee_name,
                attendee_email=email,
                attendee_time_zone=self.tz.key,
                length_in_minutes=duration,
            )
            note = "" if intent.attendee_email else f" (no email given for {intent.attendee_name}; the invite went to {email})"
            return (
                f"Booked: {booking['title']} on {self._fmt(booking['start'])} "
                f"({booking['duration']} min) with {intent.attendee_name}. Status: {booking['status']}.{note}"
            )

        start = max(start, self._now())
        slots = await self._slots(event_type["id"], start, end, duration)
        intent.range_start = intent.range_end = None
        if not slots:
            return self._ask(
                intent,
                f"No free slots for a {event_type['title']} between {self._fmt(start)} and {self._fmt(end)}. "
                "Which other day or time works?",
            )
        return self._ask(
            intent,
            f"Free times for a {event_type['title']} with {intent.attendee_name}:\n"
            + self._options(slots)
            + "\nWhich one should I book?",
        )

    # ---------------- Get ---------------- #
    async def _handle_get(self, intent: UserIntent) -> str:
        if not intent.range_start:
            return self._ask(intent, "Which day or time window should I check? (e.g. \"tomorrow\", \"this week\")")
        start, end = self._range(intent.range_start, intent.range_end)
        bookings = await self._active_bookings(start, end)
        period = f"{self._fmt(start)} – {self._fmt(end)}"
        if not bookings:
            return f"You have no meetings scheduled for {period}."
        return "\n".join([f"Your meetings ({period}):"] + [f"- {self._describe(b)}" for b in bookings])

    # ---------------- Reschedule ---------------- #
    async def _handle_reschedule(self, intent: UserIntent) -> str:
        missing = []
        if not (intent.start_time or intent.range_start):
            missing.append("the meeting's current start time")
        if not (intent.new_start_time or intent.new_range_start):
            missing.append("the new time")
        if missing:
            return self._ask(intent, f"To reschedule, please tell me {self._join(missing)}.")

        new_start = self._parse_dt(intent.new_start_time) if intent.new_start_time else None
        if new_start and new_start <= self._now():
            intent.new_start_time = None
            return self._ask(intent, self._past_time_msg(new_start))

        booking, question = await self._find_booking(intent, action="move")
        if question:
            return self._ask(intent, question)

        # Duration: keep the meeting's length unless the user asked for another one.
        length = None
        if intent.duration_minutes and intent.duration_minutes != booking["duration"]:
            options = await self._length_options(booking["eventType"]["id"])
            if intent.duration_minutes not in options:
                intent.duration_minutes = None
                return self._ask(
                    intent,
                    f"\"{booking['title']}\" is fixed at {booking['duration']} min; Cal.com can't change its length "
                    "when rescheduling. Should I move it keeping the same length?",
                )
            length = intent.duration_minutes

        event_type_id = booking["eventType"]["id"]
        original = self._parse_dt(booking["start"])
        if new_start:
            day_start, day_end = new_start.replace(hour=0, minute=0), new_start.replace(hour=23, minute=59)
            slots = await self._slots(event_type_id, day_start, day_end, length, booking["uid"])
            if not any(s == new_start for s, _ in slots):
                intent.new_start_time = None
                return self._ask(intent, self._no_exact_slot_reply(new_start, slots, f"to move \"{booking['title']}\" to"))
            new = await asyncio.to_thread(self.cal.reschedule_booking, booking["uid"], self._utc(new_start), length)
            return f"Moved \"{new['title']}\" from {self._fmt(original)} to {self._fmt(new['start'])} ({new['duration']} min)."

        start, end = self._range(intent.new_range_start, intent.new_range_end)
        start = max(start, self._now())
        intent.new_range_start = intent.new_range_end = None
        if end <= start:
            return self._ask(intent, self._past_time_msg(end))
        slots = [s for s in await self._slots(event_type_id, start, end, length, booking["uid"]) if s[0] != original]
        if not slots:
            return self._ask(
                intent,
                f"No free times to move \"{booking['title']}\" between {self._fmt(start)} and {self._fmt(end)}. "
                "Which other time works?",
            )
        return self._ask(
            intent,
            f"Free times to move \"{booking['title']}\" (now {self._fmt(original)}):\n" + self._options(slots) + "\nWhich one works?",
        )

    # ---------------- Cancel ---------------- #
    async def _handle_cancel(self, intent: UserIntent) -> str:
        missing = []
        if not intent.start_time:
            missing.append("the meeting's start time (day and time)")
        if not (intent.event_type or intent.duration_minutes):
            missing.append("which meeting it is (e.g. \"the 30 min meeting\")")
        if missing:
            return self._ask(intent, f"To cancel, please tell me {self._join(missing)}.")

        booking, question = await self._find_booking(intent, action="cancel")
        if question:
            return self._ask(intent, question)
        if not intent.confirmed:
            return self._ask(intent, f"Cancel {self._describe(booking)}? (yes/no)")
        await asyncio.to_thread(self.cal.cancel_booking, booking["uid"], "Cancelled via scheduling assistant")
        return f"Cancelled \"{booking['title']}\" on {self._fmt(booking['start'])}."

    # ---------------------------------------------------------------- #
    # Helpers: Cal.com lookups
    # ---------------------------------------------------------------- #
    async def _event_type_list(self) -> list[dict[str, Any]]:
        if self._event_types is None:
            self._event_types = await asyncio.to_thread(self.cal.list_event_types)
        return [e for e in self._event_types if not e.get("hidden")] or self._event_types

    async def _resolve_event_type(self, intent: UserIntent) -> tuple[dict[str, Any] | None, str]:
        """Event type fixed by the stated duration, or by naming a specific event type.

        Returns (event_type, "") or (None, "<available lengths>") when the length is unknown.
        """
        types = await self._event_type_list()
        lengths = sorted({e["lengthInMinutes"] for e in types} | {m for e in types for m in (e.get("lengthInMinutesOptions") or [])})
        options = " or ".join(f"{m} min" for m in lengths) or "no event types found in Cal.com"

        candidates: list[dict[str, Any]] = []
        if intent.duration_minutes:
            candidates = [
                e for e in types
                if e["lengthInMinutes"] == intent.duration_minutes
                or intent.duration_minutes in (e.get("lengthInMinutesOptions") or [])
            ]
            if not candidates:
                asked = intent.duration_minutes
                intent.duration_minutes = None
                return None, f"a length you have an event type for — there's no {asked} min option, only {options}"
        if intent.event_type:
            words = intent.event_type.lower()
            named = [e for e in (candidates or types) if e["slug"].lower() in words or e["title"].lower() in words]
            if named:
                candidates = named
        if not candidates:
            return None, options
        # Several event types share the length: prefer the plain "<N> min" one.
        return sorted(candidates, key=lambda e: (e["lengthInMinutes"] != (intent.duration_minutes or e["lengthInMinutes"]), e["id"]))[0], ""

    async def _length_options(self, event_type_id: int) -> list[int]:
        for e in await self._event_type_list():
            if e["id"] == event_type_id:
                return e.get("lengthInMinutesOptions") or [e["lengthInMinutes"]]
        return []

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
        else:
            start, end = self._range(intent.range_start, intent.range_end)
            bookings = await self._active_bookings(start, end)
            where = f"between {self._fmt(start)} and {self._fmt(end)}"

        if intent.attendee_name:
            name = intent.attendee_name.lower()
            by_name = [b for b in bookings if any(name in (a.get("name") or "").lower() for a in b.get("attendees", []))]
            bookings = by_name or bookings
        if len(bookings) > 1 and (intent.event_type or intent.duration_minutes):
            words = (intent.event_type or "").lower()
            by_type = [
                b for b in bookings
                if (words and (b["eventType"]["slug"] in words or words in b["title"].lower()))
                or b["duration"] == intent.duration_minutes
            ]
            bookings = by_type or bookings

        if not bookings:
            intent.start_time = intent.range_start = intent.range_end = None
            return None, f"I couldn't find an active meeting {where}. What time is the meeting?"
        if len(bookings) > 1:
            lines = "\n".join(f"- {self._describe(b)}" for b in bookings[:MAX_OPTIONS])
            return None, f"I found several meetings {where}:\n{lines}\nWhich one should I {action}?"
        return bookings[0], None

    # ---------------------------------------------------------------- #
    # Helpers: time + formatting
    # ---------------------------------------------------------------- #
    def _now(self) -> datetime:
        return datetime.now(self.tz)

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

    @staticmethod
    def _join(items: list[str]) -> str:
        return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]

    def _fmt(self, value: str | datetime) -> str:
        dt = self._parse_dt(value) if isinstance(value, str) else value.astimezone(self.tz)
        return dt.strftime("%a %b %-d, %-I:%M %p")

    def _drop_past_booking_time(self, intent: UserIntent) -> str:
        """Clear a booking time that has already passed; return a note for the user ("" if fine)."""
        now = self._now()
        if intent.start_time and self._parse_dt(intent.start_time) <= now:
            when = self._parse_dt(intent.start_time)
            intent.start_time = None
            return self._past_time_msg(when).replace(" Please give a future time.", " ")
        if intent.range_start and self._range(intent.range_start, intent.range_end)[1] <= now:
            when = self._range(intent.range_start, intent.range_end)[1]
            intent.range_start = intent.range_end = None
            return self._past_time_msg(when).replace(" Please give a future time.", " ")
        return ""

    def _past_time_msg(self, when: datetime) -> str:
        return f"{self._fmt(when)} is in the past (it's now {self._fmt(self._now())}). Please give a future time."

    def _options(self, slots: list[tuple[datetime, datetime]]) -> str:
        shown = slots[:MAX_OPTIONS]
        lines = [f"- {self._fmt(s)}" for s, _ in shown]
        if len(slots) > len(shown):
            lines.append(f"(+{len(slots) - len(shown)} more)")
        return "\n".join(lines)

    def _no_exact_slot_reply(self, wanted: datetime, slots: list[tuple[datetime, datetime]], what: str) -> str:
        if not slots:
            return f"{self._fmt(wanted)} isn't available {what}, and that day has no free slots. Which other day works?"
        nearest = sorted(slots, key=lambda s: abs((s[0] - wanted).total_seconds()))[:MAX_OPTIONS]
        nearest.sort()
        return f"{self._fmt(wanted)} isn't available {what}. Closest free times:\n" + self._options(nearest) + "\nWhich one works?"

    def _describe(self, b: dict[str, Any]) -> str:
        who = ", ".join(a.get("name") or a.get("email", "") for a in b.get("attendees", [])) or "no attendees"
        status = "" if b.get("status") == "accepted" else f" [{b.get('status')}]"
        return f"{self._fmt(b['start'])}: {b['title']} ({b['duration']} min) with {who}{status}"


# ---------------------------------------------------------------------- #
# Interactive terminal chat:  .venv/bin/python app/agent.py
# ---------------------------------------------------------------------- #
async def _main() -> None:
    agent = SchedulingAgent()
    print("Scheduling assistant — book, show, reschedule or cancel meetings. Type 'quit' to exit.")
    while True:
        try:
            text = (await asyncio.to_thread(input, "\nYou: ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text.lower() in {"quit", "exit", "q"}:
            break
        print("…", end="\r", flush=True)
        print(f"Assistant: {await agent.respond(text)}")


if __name__ == "__main__":
    from pathlib import Path

    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    asyncio.run(_main())
