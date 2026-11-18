"""Live smoke test for cal_api.py against the real Cal.com account in .env.

Creates ONE throwaway booking (attendee = $TEST_ATTENDEE_EMAIL), reschedules
it, then cancels it. Cleanup runs even if a step fails.

Run:  TEST_ATTENDEE_EMAIL=you+caltest@example.com .venv/bin/python tests/live_test_cal_api.py
"""

import os
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from cal_api import CalAPI, CalAPIError  # noqa: E402

TZ = os.environ.get("DEFAULT_TIMEZONE") or "UTC"
# Cal.com rejects undeliverable addresses (e.g. example.com), so a real one is required.
ATTENDEE_EMAIL = os.environ.get("TEST_ATTENDEE_EMAIL")
if not ATTENDEE_EMAIL:
    sys.exit("Set TEST_ATTENDEE_EMAIL to a real address you control (Cal.com rejects fake ones).")
results: list[tuple[str, bool, str]] = []


def check(name, fn):
    try:
        out = fn()
        results.append((name, True, ""))
        print(f"PASS  {name}")
        return out
    except Exception as e:  # noqa: BLE001
        results.append((name, False, f"{type(e).__name__}: {e}"))
        print(f"FAIL  {name}: {type(e).__name__}: {e}")
        if not isinstance(e, (CalAPIError, AssertionError)):
            traceback.print_exc()
        return None


def iso(dt_str: str) -> str:
    """Normalize a slot start to UTC 'Z' form for the booking endpoints."""
    return datetime.fromisoformat(dt_str.replace("Z", "+00:00")).astimezone(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def main() -> int:
    api = CalAPI()
    created_uids: list[str] = []

    # 1. Event types
    ets = check("list_event_types", api.list_event_types)
    assert ets, "No event types; cannot continue."
    et = next((e for e in ets if e["slug"] == "15min"), ets[0])
    print(f"      using event type {et['id']} '{et['title']}' ({et['lengthInMinutes']} min)")

    def _get_et():
        got = api.get_event_type(et["id"])
        assert got["id"] == et["id"], got
        return got

    check("get_event_type", _get_et)

    # 2. Slots (look 2-5 days ahead so we don't hit minimum-notice limits)
    now = datetime.now(timezone.utc)
    start_day = (now + timedelta(days=2)).strftime("%Y-%m-%d")
    end_day = (now + timedelta(days=5)).strftime("%Y-%m-%d")

    def _slots():
        s = api.get_available_slots(et["id"], start_day, end_day, time_zone=TZ)
        flat = [slot for day in sorted(s) for slot in s[day]]
        assert len(flat) >= 2, f"need >=2 free slots, got {len(flat)}"
        assert "start" in flat[0] and "end" in flat[0], flat[0]
        return flat

    slots = check("get_available_slots", _slots)
    if not slots:
        return summary()
    first_start, new_start = iso(slots[0]["start"]), iso(slots[-1]["start"])

    try:
        # 3. Create
        def _create():
            b = api.create_booking(
                event_type_id=et["id"],
                start=first_start,
                attendee_name="API Test Attendee",
                attendee_email=ATTENDEE_EMAIL,
                attendee_time_zone=TZ,
                metadata={"source": "live_test_cal_api"},
            )
            assert b.get("uid"), b
            created_uids.append(b["uid"])
            assert b["start"].startswith(first_start[:16]), (b["start"], first_start)
            print(f"      created {b['uid']} at {b['start']} status={b['status']}")
            return b

        booking = check("create_booking", _create)
        if not booking:
            return summary()

        # 4. Get (single + list with time filter)
        def _get_one():
            g = api.get_booking(booking["uid"])
            assert g["uid"] == booking["uid"], g
            return g

        check("get_booking", _get_one)

        def _get_list():
            day_start = first_start[:10] + "T00:00:00Z"
            day_end = (datetime.fromisoformat(first_start[:10]) + timedelta(days=1)).strftime(
                "%Y-%m-%dT00:00:00Z"
            )
            lst = api.get_bookings(status="upcoming", after_start=day_start, before_end=day_end)
            uids = [x["uid"] for x in lst]
            assert booking["uid"] in uids, f"{booking['uid']} not in {uids}"
            return lst

        check("get_bookings (status + time filter)", _get_list)

        def _slots_resched():
            s = api.get_available_slots(
                et["id"], start_day, end_day, time_zone=TZ,
                booking_uid_to_reschedule=booking["uid"],
            )
            assert isinstance(s, dict), s
            return s

        check("get_available_slots (booking_uid_to_reschedule)", _slots_resched)

        # 5. Reschedule
        def _resched():
            r = api.reschedule_booking(booking["uid"], new_start, reason="live API test")
            assert r.get("uid"), r
            if r["uid"] != booking["uid"]:
                created_uids.append(r["uid"])
            assert r["start"].startswith(new_start[:16]), (r["start"], new_start)
            print(f"      rescheduled -> {r['uid']} at {r['start']} status={r['status']}")
            return r

        new_booking = check("reschedule_booking", _resched)

        # 6. Cancel
        target = new_booking["uid"] if new_booking else booking["uid"]

        def _cancel():
            c = api.cancel_booking(target, reason="live API test cleanup")
            assert c.get("status") == "cancelled", c.get("status")
            return c

        if check("cancel_booking", _cancel):
            created_uids.remove(target) if target in created_uids else None

        def _verify_cancelled():
            g = api.get_booking(target)
            assert g["status"] == "cancelled", g["status"]

        check("get_booking after cancel (status=cancelled)", _verify_cancelled)

    finally:
        # Cleanup anything still active
        for uid in created_uids:
            try:
                st = api.get_booking(uid).get("status")
                if st not in ("cancelled", "rejected"):
                    api.cancel_booking(uid, reason="live API test cleanup")
                    print(f"      cleanup: cancelled {uid}")
            except CalAPIError as e:
                print(f"      cleanup: {uid}: {e}")

    return summary()


def summary() -> int:
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    for name, _, err in failed:
        print(f"  FAILED {name}: {err}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
