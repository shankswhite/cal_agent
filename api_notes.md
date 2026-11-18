# Cal.com API v2 — Notes for the Scheduler Chatbot

Sources (fetched 2026-11-17):
- Auth / intro: https://cal.com/docs/api-reference/v2/introduction
  (the link `cal.com/docs/enterprise-features/api/authentication` now returns **404**)
- Bookings: https://cal.com/docs/api-reference/v2/bookings/get-all-bookings (+ create / get / reschedule / cancel pages)
- Slots: https://cal.com/docs/api-reference/v2/slots/get-available-time-slots-for-an-event-type
  (the link `.../slots/find-out-when-is-an-event-type-ready-to-be-booked` now returns **404**)
- Docs index: https://cal.com/docs/llms.txt → https://cal.com/docs/_llms/api-v2-reference.txt

Legend: **[docs]** = stated in Cal.com docs. **[verified]** = checked against the live API.
Anything not in the docs is marked as such. Nothing here is guessed.

---

## 1. Basics

| Item | Value | Source |
|---|---|---|
| Base URL | `https://api.cal.com` + spec paths (which begin with `/v2`) → e.g. `https://api.cal.com/v2/bookings` | [verified] — the doc pages' OpenAPI `servers` is empty; confirmed by live requests |
| Transport | HTTPS only; plain HTTP calls fail | [docs] |
| Versioning | **Every endpoint needs a `cal-api-version` header**, and the value differs per endpoint (see each section). If it's missing, an older version of the endpoint is used. | [docs] |
| Response envelope | `{ "status": "success" \| "error", "data": ... }` | [docs] |
| Error shape | e.g. `{"status":"error","timestamp":"...","path":"/v2/bookings","error":{"code":"UnauthorizedException","message":"..."}}` | [verified] (live 401 response) |

### `cal-api-version` per endpoint (as written in docs)

| Endpoint | Version |
|---|---|
| `GET /v2/bookings` (list) | `2026-05-01` (description says this; schema example shows `2026-02-25`, which is inconsistent in the docs) |
| `POST /v2/bookings` (create) | `2026-02-25` |
| `GET /v2/bookings/{bookingUid}` | `2026-02-25` |
| `POST /v2/bookings/{bookingUid}/reschedule` | `2026-02-25` |
| `POST /v2/bookings/{bookingUid}/cancel` | `2026-02-25` |
| All `/v2/slots...` endpoints | `2024-09-04` |

---

## 2. Authentication [docs]

Three methods: **OAuth**, **API key**, **Platform (deprecated)**. Cal.com says API keys are easy to create, but recommends OAuth "almost always" for integrations and apps.

### API key (simplest for our bot)
- Create one under **Settings > Security** in Cal.com.
- Prefixes: test mode `cal_`, live mode `cal_live_`.
- Header: `Authorization: Bearer YOUR_API_KEY`
- Missing auth on a protected endpoint returns 401 `UnauthorizedException` [verified].

### OAuth
- You must be an official partner / App Store app. Request a verified OAuth client: https://cal.com/docs/api-reference/v2/oauth
- Header: `Authorization: Bearer <oauth_access_token>`
- Scopes: listing bookings with an OAuth token needs the `BOOKING_READ` scope.
- Token exchange endpoint: `oauth2/exchange-authorization-code-or-refresh-token-for-tokens` (its contents were not read for these notes).

### Platform (deprecated; no new signups as of 15 Dec 2025)
- OAuth client credentials (from https://app.cal.com/settings/platform), sent as headers:
  - `x-cal-client-id: <client id>`
  - `x-cal-secret-key: <client secret>`
- Managed-user access tokens: `Authorization: Bearer <access-token>`. Access tokens last **60 min** and refresh tokens last **1 year**. Lost tokens can be force-refreshed with the client credentials.

### Rate limits
- API key: **120 req/min** by default. It can be raised to about 200/min on request. 800/min is possible but may cost extra (contact support).
- Unauthenticated: 120 req/min by default.
- The docs give no limits for OAuth or platform tokens.

---

## 3. Slots (availability) — `cal-api-version: 2024-09-04`

### 3.1 Get available slots — `GET /v2/slots`
Auth: the docs list no Authorization header. A live call without auth returned 200 [verified].

Query params:

| Param | Type | Req | Notes |
|---|---|---|---|
| `start` | string | **yes** | UTC ISO 8601. A date alone (`2033-09-05`) means start of day. A time is allowed, e.g. `2024-08-13T09:00:00Z` |
| `end` | string | **yes** | Same format. A date alone means end of day |
| `eventTypeId` | number | * | Event type ID |
| `eventTypeSlug` | string | * | Requires `username` or `teamSlug` (plus `organizationSlug` if in an org) |
| `username` | string | * | Owner of an individual event type |
| `teamSlug` | string | * | Team that owns the event type |
| `organizationSlug` | string | * | Org of the user/team |
| `usernames` | string | * | Comma-separated, ≥2 usernames → dynamic event |
| `timeZone` | string | no | e.g. `Europe/Rome`. Default **UTC** |
| `duration` | number | no | Only for multi-duration or dynamic events (dynamic default is 30 min) |
| `format` | string | no | `range` → `{start,end}` per slot. `time` or omitted → `{start}` only |
| `bookingUidToReschedule` | string | no | The booking's current time shows as available (use this when rescheduling) |

Ways to identify the event type (\* above):
1. `eventTypeId`
2. `eventTypeSlug` + `username` (+ `organizationSlug` if in an org)
3. `eventTypeSlug` + `teamSlug` (+ `organizationSlug` if in an org)
4. `usernames` (dynamic group; also `organizationSlug`)

Managed event types: you can't query the parent. Use the child event type IDs.

Response 200: `data` maps a date (`YYYY-MM-DD`) to an array of slots. An empty result is `data: {}`. Seated slots also include `attendeesCount` and `bookingUid`.
```json
// format omitted / "time"  (shape per docs example)
{ "status": "success",
  "data": { "2033-09-05": [ { "start": "2033-09-05T09:00:00.000+02:00" } ] } }

// format=range
{ "status": "success",
  "data": { "2033-09-05": [
    { "start": "2033-09-05T09:00:00.000+02:00", "end": "2033-09-05T10:00:00.000+02:00" },
    { "start": "2033-09-05T10:00:00.000+02:00", "end": "2033-09-05T11:00:00.000+02:00" } ] } }
```
Note: the schema declares the items as strings, but the docs example uses objects with `start`. Code should handle both.

### 3.2 Reserve a slot — `POST /v2/slots/reservations`
Headers: optional `Authorization: Bearer <token>`, optional `x-cal-client-id`.
Body:

| Field | Type | Req | Notes |
|---|---|---|---|
| `eventTypeId` | number | **yes** | |
| `slotStart` | date-time | **yes** | UTC ISO 8601, e.g. `2024-09-04T09:00:00Z` |
| `slotDuration` | number | no | Minutes. Defaults to the event length. Needed for variable-length events |
| `reservationDuration` | number | no | Minutes to hold the slot. Default 5. Custom values only work with auth |

Response 201 `data`: `eventTypeId`, `slotStart`, `slotEnd`, `slotDuration`, `reservationUid`, `reservationDuration`, `reservationUntil`.

### 3.3 Other reservation endpoints
| Method | Path | Notes |
|---|---|---|
| GET | `/v2/slots/reservations/{uid}` | Returns `{status, data}`. The docs define no fields for `data` (nullable) |
| PATCH | `/v2/slots/reservations/{uid}` | Same body as reserve. Optional Bearer auth. Returns the same shape as reserve (200) |
| DELETE | `/v2/slots/reservations/{uid}` | No body. Returns `{"status":"success"}` |

---

## 4. Bookings

Common headers: `cal-api-version` (see table), `Authorization: Bearer <token>`. Platform customers can also send `x-cal-client-id` and `x-cal-secret-key`.

### 4.1 List bookings — `GET /v2/bookings` (`cal-api-version: 2026-05-01`)
Auth is **required** (API key `cal_...`, managed-user token, or OAuth token with `BOOKING_READ`).

Query params (all optional):

| Param | Type | Notes |
|---|---|---|
| `status` | enum | `upcoming` \| `recurring` \| `past` \| `cancelled` \| `unconfirmed`. One status per request; for several, make one call each and merge |
| `attendeeEmail`, `attendeeName` | string | Filters |
| `bookingUid` | string | |
| `eventTypeId` / `eventTypeIds` | string | Single / comma-separated (`100,200`) |
| `teamId` / `teamsIds` | string | Single / comma-separated. Param name is `teamsIds` (docs example text says `teamIds`) |
| `afterStart`, `beforeEnd` | string | ISO dates, e.g. `2025-03-07T10:00:00.000Z` |
| `afterCreatedAt`, `beforeCreatedAt`, `afterUpdatedAt`, `beforeUpdatedAt` | string | ISO dates |
| `sortStart`, `sortEnd`, `sortCreated`, `sortUpdatedAt` | enum | `asc` \| `desc` |
| `cursor` | string | Opaque. Pass the previous `pagination.nextCursor`; omit on the first page |
| `limit` | number | 1–100, default **50** |

Default ordering and first-page anchor by status:

| status | Order | Page-1 anchor |
|---|---|---|
| (omitted) | uuid DESC | from year 2100 backward |
| `upcoming` / `recurring` / `unconfirmed` | uuid ASC | from NOW() − 1h forward |
| `past` | uuid DESC | from NOW() backward |
| `cancelled` | uuid DESC | from year 2100 backward |

Response 200: `{ status, data: Booking[], pagination: { nextCursor: string|null, hasMore: boolean } }`.
Each `data` item is one of: Booking, Recurring Booking (`+recurringBookingUid`), Seated Booking, or Recurring Seated Booking.

**Booking object** (`*` = required):
`id*` number · `uid*` string · `title*` · `description*` · `hosts*` BookingHost[] · `status*` (`cancelled|accepted|rejected|pending|awaiting_host`) · `cancellationReason` · `cancelledByEmail` · `reschedulingReason` · `rescheduledByEmail` · `rescheduledFromUid` · `rescheduledToUid` · `start*` / `end*` date-time · `duration*` number · `eventTypeId*` (deprecated) · `eventType*` `{id, slug}` · `eventUuid` string|null · `meetingUrl` (deprecated) · `location*` string · `absentHost*` bool · `createdAt*` · `updatedAt*` (nullable) · `metadata` · `rating` · `icsUid` · `attendees*` BookingAttendee[] · `guests` string[] · `bookingFieldsResponses*` object.

- **BookingHost**: `id, uuid, name, email, displayEmail, username, timeZone` (all required).
- **BookingAttendee**: required `name, email, displayEmail, timeZone, absent`. Optional `language, phoneNumber`.
- **SeatedAttendee**: adds `seatUid, bookingFieldsResponses`, and optional `createdAt, metadata, status ("cancelled"), confirmationStatus (pending|accepted|rejected)`.

The docs page has no full response JSON example.

### 4.2 Create a booking — `POST /v2/bookings` (`cal-api-version: 2026-02-25`)
Auth is optional (creating a booking is public). The event type decides whether the booking is regular or recurring. An instant booking needs `"instant": true` (team events only).

Body (standard): required **`start`** (UTC ISO 8601) and **`attendee`**.

| Field | Type | Notes |
|---|---|---|
| `start` | date-time | Required (optional for instant; defaults to now) |
| `attendee` | object | `name*`, `timeZone*` (e.g. `America/New_York`), `email`, `phoneNumber` (international format; required if SMS reminders are on), `language` (default `en`) |
| `eventTypeId` | number | Required unless `eventTypeSlug` + `username`/`teamSlug` is given |
| `eventTypeSlug`, `username`, `teamSlug`, `organizationSlug` | string | Other ways to identify the event type |
| `usernames` | string[] | 2–20 users for a dynamic group booking. Can't be combined with the fields above. Default 30 min; `lengthInMinutes` ∈ {15,30,45,60,90} |
| `lengthInMinutes` | number | ≥1 |
| `guests` | string[] | Emails |
| `location` | object | `type` is one of: `address`, `attendeeAddress` (+`address`), `attendeeDefined` (+`location`), `attendeePhone` (+`phone`), `integration` (+`integration`: `cal-video`, `google-meet`, `zoom`, `whereby-video`, …), `link`, `phone`, `organizersDefaultApp` (team only) |
| `meetingUrl` | uri | Deprecated → use `location` |
| `bookingFieldsResponses` | object | Custom booking field slug → value |
| `metadata` | object | ≤50 keys, key ≤40 chars, value ≤500 chars |
| `routing` | object | Routing-form fields (`responseId`, `teamMemberIds`, …) |
| `emailVerificationCode` | string | |
| `leadEnrichmentOptOut` | bool | |
| `allowConflicts`, `allowBookingOutOfBounds`, `skipBookingLimits` | bool | Only for authenticated users with the right access |

Variants:
- **Instant**: `instant: true`, `instantMeetingExpiresInSeconds` (≥1; can only shorten the event type's setting).
- **Recurring**: `recurrenceCount` (1–32; can't exceed the event type's count).

Response **201**: `data` is a Booking, an instant booking (`+instantMeetingExpiresAt`, `instantMeetingExpiresInSeconds`), an array of recurring bookings, or a seated booking (`+seatUid`).

### 4.3 Get a booking — `GET /v2/bookings/{bookingUid}` (`2026-02-25`)
`bookingUid` can be a normal booking uid, a single recurrence uid, or a recurring booking uid (returns an array of all recurrences). Response 200: `{status, data}` with the Booking shapes above.

### 4.4 Reschedule — `POST /v2/bookings/{bookingUid}/reschedule` (`2026-02-25`)
- You can reschedule `accepted` bookings (they move to the new time) and `pending` bookings (they stay pending until the host confirms).
- `cancelled`, `rejected`, and `awaiting_host` bookings return **400**; create a new booking instead.

Body (standard/recurring): required **`start`**. Optional: `lengthInMinutes` (keeps the original if omitted), `rescheduledBy` (email; the owner's email auto-confirms when confirmation is required), `reschedulingReason`, `emailVerificationCode`, `rescheduleWithSameHost` (round robin), `allowConflicts`, `allowBookingOutOfBounds`, `skipBookingLimits`.
Body (seated): required `start` and `seatUid`, plus optional `rescheduledBy`, `emailVerificationCode`, and the allow/skip flags.

Response **201**: the new Booking (seated responses include `seatUid`).

### 4.5 Cancel — `POST /v2/bookings/{bookingUid}/cancel` (`2026-02-25`)
- Standard body (all optional): `cancellationReason`, `cancelSubsequentBookings` (recurring non-seated: cancels this recurrence and all later ones). Example: `{"cancellationReason": "Will travel"}`.
- Passing a recurring booking uid cancels all recurrences.
- Seated, as an attendee: `{"seatUid": "..."}` removes that seat. As the host, send `cancellationReason` with the owner's Bearer token to cancel all seats.
- Recurring seated: cancel each recurrence with its `bookingUid` + `seatUid`.

Response 200: `{status, data}` with the cancelled booking.

### 4.6 Other Bookings endpoints (found in the docs index, details not read yet)
Under `https://cal.com/docs/api-reference/v2/`:
- `bookings/confirm-a-booking`, `bookings/decline-a-booking`
- `bookings/request-to-reschedule-a-booking`
- `bookings/mark-a-booking-absence`
- `bookings/reassign-a-booking`, `bookings/reassign-a-booking-to-a-specific-host`
- `bookings/update-booking-location-for-an-existing-booking`
- `bookings/get-add-to-calendar-links-for-a-booking`, `bookings/get-booking-references`
- `bookings/get-a-booking-by-seat-uid`, `bookings/confirm-booking-seats`, `bookings/decline-booking-seats`
- Recordings / transcripts / video sessions (Cal Video)
- `bookings-attendees/*` (list/add/get attendee), `bookings-guests/add-guests-to-an-existing-booking`
- `bookings--email-verification/*` (check required / send code / verify)
- `bookings-routing-trace/get-the-routing-trace-for-a-booking`

---

## 5. Mapping to chatbot intents (from NOTES.md examples)

| User says | Calls |
|---|---|
| "book a 30-min intro with a candidate Thursday afternoon" | `GET /v2/slots` (event type, Thursday range, `timeZone`, `format=range`) → pick a slot → optionally `POST /v2/slots/reservations` to hold it → `POST /v2/bookings` with `start` + `attendee` |
| "what's on my calendar tomorrow?" | `GET /v2/bookings?status=upcoming&afterStart=<tomorrow 00:00Z>&beforeEnd=<tomorrow 23:59Z>&sortStart=asc` |
| "move my 3pm to later today" | `GET /v2/bookings` (find the 3pm booking `uid`) → `GET /v2/slots?bookingUidToReschedule=<uid>&start=..&end=..` → `POST /v2/bookings/{uid}/reschedule` with the new `start` |

Gotchas:
- Send times as **UTC ISO 8601**. Pass the user's `timeZone` to slots so results come back in local offsets.
- Set the correct `cal-api-version` **per endpoint** (slots `2024-09-04`, bookings `2026-02-25`, list `2026-05-01`).
- List bookings needs auth. Slots and create booking work without it.
- Pagination is cursor-based (`nextCursor` / `hasMore`), not page numbers.
