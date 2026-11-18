"""Run tests/test_samples.md cases plus multi-turn clarification flows against the live agent.

Each single-turn sample uses a fresh agent. Any booking created during the run
is cancelled at the end, so the calendar is left as it was.

Run:  .venv/bin/python tests/run_samples.py
"""

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from agent import SchedulingAgent  # noqa: E402
from cal_api import CalAPI  # noqa: E402

SAMPLES = [
    ("help me", "unclear"),
    ("book a 3pm event", "unclear"),
    ("book a event", "unclear"),
    ("talk with Steven", "unclear"),
    ("Booking a intro event with Steven at 5 PM today.", "past time -> reclarify"),
    ("Booking a intro event with Steven at 10 PM today.", "result (asks duration per rule 1)"),
    ("book a 30-min intro with a candidate Thursday afternoon", "result"),
    ("what's on my calendar tomorrow?", "result"),
    ("move my 3pm to later today", "result"),
]

CONVERSATIONS = [
    ["book a event", "30 minutes with Steven", "tomorrow afternoon"],
    ["what's on my calendar?", "tomorrow"],
    ["cancel my meeting", "the one at 3pm today, the 30 min meeting"],
    ["reschedule my meeting", "the 3pm today"],
]


def active_uids(cal: CalAPI) -> set[str]:
    now = datetime.now(timezone.utc)
    bookings = cal.get_bookings(
        after_start=(now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        before_end=(now + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    return {b["uid"] for b in bookings if b["status"] in ("accepted", "pending")}


async def main() -> None:
    cal = CalAPI()
    before = active_uids(cal)
    try:
        print("=== Single-turn samples ===")
        for i, (text, expected) in enumerate(SAMPLES, 1):
            reply = await SchedulingAgent(cal=cal).respond(text)
            print(f"\n[{i}] You: {text}\n    expected: {expected}\n    Assistant: {reply.replace(chr(10), chr(10) + '      ')}")

        print("\n\n=== Multi-turn clarification ===")
        for convo in CONVERSATIONS:
            agent = SchedulingAgent(cal=cal)
            print()
            for text in convo:
                reply = await agent.respond(text)
                print(f"You: {text}\nAssistant: {reply}")
            print("-" * 40)
        print("\n\n=== Reschedule + cancel on a real (throwaway) meeting ===")
        agent = SchedulingAgent(cal=cal)
        et = next(e for e in cal.list_event_types() if e["slug"] == "15min")
        day = (datetime.now(agent.tz) + timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
        slots = await agent._slots(et["id"], day, day.replace(hour=23, minute=59))
        first = slots[0][0]
        fixture = cal.create_booking(
            event_type_id=et["id"], start=agent._utc(first), attendee_name="Steven",
            attendee_email=agent.owner_email, attendee_time_zone=agent.tz.key,
        )
        print(f"(fixture: {fixture['title']} at {agent._fmt(fixture['start'])} with Steven)")
        when = f"{first:%A} at {first.strftime('%-I:%M %p').lower()}"
        convo = [
            f"move my meeting on {when} to later that day",
            "the second option",
            f"what's on my calendar {first:%A}?",
            "cancel it, the 15 min meeting with Steven",
            "yes",
        ]
        for text in convo:
            reply = await agent.respond(text)
            print(f"You: {text}\nAssistant: {reply}")
    finally:
        created = active_uids(cal) - before
        for uid in created:
            cal.cancel_booking(uid, reason="run_samples cleanup")
            print(f"cleanup: cancelled {uid}")


if __name__ == "__main__":
    asyncio.run(main())
