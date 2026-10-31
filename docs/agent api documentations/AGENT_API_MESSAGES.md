# Agent App API — Sending a message

**Status:** built and tested. Every response below is real output from the application,
captured on 2026-09-07 against the test database.
**Endpoint:** `POST /agent-api/v1/messages`
**Companion:** [`AGENT_API_THREADS.md`](./AGENT_API_THREADS.md) — where `window_state`
comes from, and how to build the countdown.

---

## What this is for

One free-text message, from an agent, to one customer — the reply an agent types in the
chat window.

It is **not** how you send a template. WhatsApp only permits free text inside the
24-hour service window; outside it you need an approved template, which for a single
contact today means a broadcast to a phonebook containing them
([`AGENT_API_BROADCASTS.md`](./AGENT_API_BROADCASTS.md)).

---

## Request

```
POST /agent-api/v1/messages
Authorization: Bearer <session token>
X-Device-Id:   <the device this session was issued to>
Content-Type:  application/json
```

```json
{
  "mobile_no": "917025985366",
  "text": "Sure — sending them now."
}
```

| Field | Type | Notes |
|---|---|---|
| `mobile_no` | string | Digits with country code, **no leading `+`**. As returned by `/phonebooks` or `/threads` |
| `text` | string | 1–4096 characters |

---

## Response — `201 Created`

```json
{
  "id": "47ab7a0d-9950-4dc0-8334-76cc2c0842c3",
  "thread_id": "b4760a72-5cef-49d6-a6d1-d740b84eaa3a",
  "seq": 3,
  "status": "sent",
  "wamid": "wamid.HBgMOTE3MDI1OTg1MzY2FQIAERgSN0Y2",
  "created_at": "2026-09-07T05:03:10.379163Z",
  "server_time": "2026-09-07T05:03:10.509407Z"
}
```

| Field | Notes |
|---|---|
| `id` | The message row. Same id `/threads/{id}/messages` returns |
| `thread_id` | Useful when you sent by number and do not yet hold the thread |
| `seq` | **Its position in the thread.** Insert the bubble at this position — no refetch needed |
| `status` | `sent` — accepted by WhatsApp. **Not** proof of delivery |
| `wamid` | WhatsApp's id. Delivery receipts arrive against it later |

**`status: "sent"` is not "delivered".** Show one tick. It becomes `delivered`, and then
`read`, on a later poll of the thread — see the status table in
[`AGENT_API_THREADS.md`](./AGENT_API_THREADS.md).

---

## The 24-hour window

The single most common legitimate failure. **Read this before building the composer.**

```json
{
  "detail": {
    "code": "window_closed",
    "message": "You can only send a free message within 24 hours of the customer's last message. Send an approved template instead.",
    "last_inbound_at": null
  }
}
```

`409 Conflict`. `last_inbound_at` tells you when the clock last started, or `null` if
that customer has never written to you at all.

> ⚠️ **Only an inbound message opens the window.** Sending does not — not a broadcast,
> not a template, not a previous reply. A contact you messaged an hour ago who has not
> answered is **closed**.

**Check `window_state` before enabling the composer** rather than discovering it here.
A `409` after the agent has typed a paragraph is a bad experience and an avoidable one.

**But do not rely on your check alone.** The server re-checks on every send and is the
only authority: a device clock can be hours out, and WhatsApp rejects a late send with
error 131047 whatever the handset displayed. Treat this `409` as expected, not as a bug —
show the message, offer a template, keep the typed text so it is not lost.

---

## Errors

| HTTP | `code` | Meaning | What to do |
|---|---|---|---|
| `409` | `window_closed` | Outside the 24-hour window, or they never wrote | Offer a template; keep the draft |
| `403` | `recipient_opted_out` | This person unsubscribed | Disable the composer permanently for them |
| `403` | `daily_limit_reached` | Your rolling 24h quota is spent (`sent_today`, `limit`) | Show when it frees up — it recovers gradually, not at midnight |
| `409` | `no_whatsapp_account` | The company has no WhatsApp number configured | "Contact your administrator" |
| `404` | `not_found` | `mobile_no` is not a usable number | Check the number |
| `502` | `send_failed` | WhatsApp rejected it | Offer retry — **the message row still exists**, see below |
| `401` | `invalid_session` | Token or device header missing/wrong | Clear the token, show login |
| `422` | — | `text` empty or over 4096 chars | Validate before sending |

Real responses:

```json
{"detail": {"code": "recipient_opted_out",
            "message": "This contact has unsubscribed and cannot be messaged."}}
```

```json
{"detail": {"code": "invalid_session",
            "message": "Your session has ended. Please sign in again."}}
```

### `opted_out` is a hard block, not a warning

Messaging someone who unsubscribed is a compliance breach, and it lowers the WhatsApp
quality rating of the **business number every agent shares** — degrading delivery for
colleagues who did nothing wrong. `/phonebooks` and `/threads` both return
`opted_out`; use it to disable the composer before anyone types.

---

## What happens if the send fails

**The message is saved before it is sent.** On a `502` the row still exists in the
thread with `status: "failed"` and an `error_message`, so:

- The agent's typed text is never silently lost.
- The failure is visible in the chat window rather than only in a toast that disappears.
- A retry is a new send; it does not resurrect the failed row.

Show the failed bubble with a retry affordance, the way WhatsApp itself does.

---

## Worked example

```bash
BASE=https://<host>/agent-api/v1
DEV=9774d56d682e549c

TOKEN=$(curl -s -X POST $BASE/auth/login -H 'Content-Type: application/json' -d '{
  "username":"ravi.k","password":"•••","customer_id":"FM-2291",
  "device_id":"'$DEV'"}' | jq -r .token)

# Is the window open?
curl -s "$BASE/threads" -H "Authorization: Bearer $TOKEN" -H "X-Device-Id: $DEV" \
  | jq '.threads[] | {mobile_no, window_state, window_expires_at}'

# Send
curl -s -X POST $BASE/messages \
  -H "Authorization: Bearer $TOKEN" -H "X-Device-Id: $DEV" \
  -H 'Content-Type: application/json' \
  -d '{"mobile_no":"917025985366","text":"Sure — sending them now."}'
```

---

## Client checklist

- [ ] Send **both** `Authorization` and `X-Device-Id`
- [ ] Disable the composer when `window_state` is `closed` **or** `opted_out` is true
- [ ] Still handle `409 window_closed` — your check can be wrong, the server's cannot
- [ ] Keep the typed text when a send is refused
- [ ] Use the returned `seq` to place the bubble without refetching
- [ ] Show `status: "sent"` as one tick, not two
- [ ] Render a `failed` message in the thread with a retry action
- [ ] Any `401` → clear token, show login, do not retry
- [ ] Parse leniently — fields will be added without a version bump

---

## Related

| Document | Covers |
|---|---|
| [`AGENT_API_THREADS.md`](./AGENT_API_THREADS.md) | The chat window, `window_state`, clock skew |
| [`AGENT_API_BROADCASTS.md`](./AGENT_API_BROADCASTS.md) | Sending a template outside the window |
| [`README.md`](./README.md) | Shared headers and the full error table |
| `app/agent/messages.py` | The implementation |
