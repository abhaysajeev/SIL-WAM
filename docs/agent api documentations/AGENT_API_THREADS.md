# Agent App API — Threads and the chat window

**Status:** built and tested. Every response below is real output from the application,
captured on 2026-09-07 against the test database.
**Endpoints:** `GET /threads`, `GET /threads/{thread_id}/messages`,
`GET /threads/by-mobile/{mobile_no}`
**Companion:** [`README.md`](./README.md) — headers and the shared error shape.

---

## What this is for

A **thread** is one customer's whole history with one company: broadcasts we sent them,
their replies, and anything an agent sent by hand — all in one ordered list. Before this
existed those lived in different tables and nothing could put them side by side.

Two screens come out of these endpoints:

- **The inbox** — `GET /threads`, people who have written to us, most recent first.
- **The chat window** — `GET /threads/{id}/messages`, one conversation, paged as you scroll.

---

## `GET /threads` — the inbox

```
GET /agent-api/v1/threads?limit=30
```

| Query | Default | Notes |
|---|---|---|
| `limit` | 30 | Max 100 |
| `before` | — | Cursor: pass `next_cursor` from the previous page |
| `include_silent` | `false` | Also return contacts who have **never** replied |

```json
{
  "threads": [
    {
      "id": "b4760a72-5cef-49d6-a6d1-d740b84eaa3a",
      "mobile_no": "917025985366",
      "contact_name": "Abhay",
      "last_message_at": "2026-09-07T05:03:10.368684Z",
      "message_seq": 2,
      "last_inbound_at": "2026-09-07T05:03:10.368684Z",
      "window_expires_at": "2026-09-08T05:03:10.368684Z",
      "window_state": "open",
      "opted_out": false,
      "last_message": "Yes, please send details"
    }
  ],
  "next_cursor": null,
  "server_time": "2026-09-07T05:03:10.402772Z"
}
```

### Fields

| Field | Type | Notes |
|---|---|---|
| `id` | uuid | Use as the list key and to fetch messages |
| `mobile_no` | string | Digits, country code, **no leading `+`** |
| `contact_name` | string¦null | Best known name. Often null for a number we only ever broadcast to |
| `last_message_at` | datetime¦null | Either direction. The sort key |
| `message_seq` | int | Highest `seq` in the thread — see **Sync** below |
| `last_inbound_at` | datetime¦null | When they last wrote. **null = never** |
| `window_expires_at` | datetime¦null | `last_inbound_at` + 24h, or null |
| `window_state` | string | `open` ¦ `closing_soon` ¦ `closed` |
| `opted_out` | bool | **true = do not message.** Disable the composer |
| `last_message` | string¦null | Preview line for the list |
| `next_cursor` | string¦null | Pass as `before` for the next page. **null = end of list** |
| `server_time` | datetime | The server's clock — see **Clock skew** |

### Why the inbox is not every contact

**By default only threads where the customer has actually written back.** A campaign to
2,000 people creates 2,000 threads; listing all of them would bury the handful of people
waiting for a reply under everyone who ignored a marketing message.

The silent ones are not lost — open them by number (below), or pass
`include_silent=true` to see them all.

### Paging

```
GET /threads?limit=30                       → first page
GET /threads?limit=30&before=<next_cursor>  → the next page
```

**Stop when `next_cursor` is null.** A short page is the end of the list; the cursor is
only issued when a full page came back, so you never fetch an empty page to discover
you are done.

---

## `GET /threads/{thread_id}/messages` — the chat window

```
GET /agent-api/v1/threads/b4760a72-…/messages?limit=2
```

| Query | Default | Use |
|---|---|---|
| `limit` | 30 | Max 100 |
| `before_seq` | — | **Scroll up**: the messages immediately older than this `seq` |
| `after_seq` | — | **Sync**: everything newer than this `seq` |

```json
{
  "thread": {
    "id": "b4760a72-5cef-49d6-a6d1-d740b84eaa3a",
    "mobile_no": "917025985366",
    "contact_name": "Abhay",
    "last_message_at": "2026-09-07T05:03:10.481126Z",
    "message_seq": 3,
    "last_inbound_at": "2026-09-07T05:03:10.368684Z",
    "window_expires_at": "2026-09-08T05:03:10.368684Z",
    "window_state": "open",
    "opted_out": false,
    "last_message": "Sure — sending them now."
  },
  "messages": [
    {
      "id": "47ab7a0d-9950-4dc0-8334-76cc2c0842c3",
      "seq": 3,
      "direction": "outbound",
      "kind": "agent_reply",
      "body": "Sure — sending them now.",
      "template": null,
      "media": null,
      "status": "sent",
      "error_message": null,
      "wamid": "wamid.HBgMOTE3MDI1OTg1MzY2FQIAERgSN0Y2",
      "replies_to": null,
      "created_at": "2026-09-07T05:03:10.379163Z",
      "delivered_at": null,
      "read_at": null
    },
    {
      "id": "3d307c53-2ad4-4c17-b2b7-cf9d58b35b89",
      "seq": 2,
      "direction": "inbound",
      "kind": "reply",
      "body": "Yes, please send details",
      "template": null,
      "media": null,
      "status": "received",
      "error_message": null,
      "wamid": "wamid.HBgMOTE3MDI1OTg1MzY2FQIAEhgUM0E5",
      "replies_to": null,
      "created_at": "2026-09-07T05:03:10.224636Z",
      "delivered_at": null,
      "read_at": null
    }
  ],
  "has_more": true,
  "server_time": "2026-09-07T05:03:10.568090Z"
}
```

### Message fields

| Field | Type | Notes |
|---|---|---|
| `seq` | int | **Position in the thread, from 1.** Never reused, never skipped |
| `direction` | string | `inbound` (from the customer) ¦ `outbound` (from us) |
| `kind` | string | `broadcast` ¦ `agent_reply` ¦ `reply` ¦ `template` ¦ `transactional` |
| `body` | string¦null | **The text as the customer sees it**, placeholders already filled |
| `template` | object¦null | `{name, language, params}` when this was a template send |
| `media` | object¦null | Attachment or image header |
| `status` | string | `queued` ¦ `sent` ¦ `delivered` ¦ `read` ¦ `failed` ¦ `received` |
| `error_message` | string¦null | Set when `status` is `failed` |
| `wamid` | string¦null | WhatsApp's id |
| `replies_to` | string¦null | The `wamid` this message replies to, if the customer used WhatsApp's reply gesture |
| `has_more` | bool | Older messages exist above what was returned |

> **`body` is already rendered.** A broadcast bubble reads
> *"Hi Abhay, our Diwali offer on LED TVs is live."*, not `"Hi {{1}}, our Diwali offer
> on {{2}} is live."`. The `template` object carries the structured form beside it if
> you want to show which campaign it was.

### `status` on an outbound message

`sent` means **WhatsApp accepted it** — it is not proof it reached the phone. Only
`delivered` is that. Use the same tick treatment WhatsApp itself does:

```
queued     …   not yet handed over
sent       ✓   accepted by WhatsApp
delivered  ✓✓  on the customer's phone
read       ✓✓  (blue)
failed     !   with error_message
```

---

## Scrolling and syncing

The two directions are different parameters, on purpose.

**Scroll up** — the reader is going back through history:

```
GET …/messages?limit=30                    → newest 30 (seq 100…71)
GET …/messages?limit=30&before_seq=71      → the next 30 older (seq 70…41)
```

Stop when `has_more` is `false`.

**Sync** — catching up on what arrived while you were away:

```
GET …/messages?after_seq=<highest seq you hold>
```

Returned **oldest first**, so you can append them in order.

> **Why `seq` and not a timestamp.** The sequence is allocated per thread under a
> database row lock, so it is gap-free: if you hold up to seq 40 and ask for
> `after_seq=40`, you are guaranteed nothing between 40 and what comes back was
> missed. Timestamps cannot promise that — under concurrency a message can be written
> with an *earlier* timestamp than one already delivered to you, and a timestamp cursor
> would skip it silently. Never page this API by time.

**Never use an offset.** A message arriving mid-scroll would shift every later page by
one, and a customer's question would vanish between two screens.

---

## `GET /threads/by-mobile/{mobile_no}` — open a contact

Same response as `/messages`. Use it when the agent taps a contact in a phonebook
rather than a row in the inbox, because at that point you have a number and not a
thread id.

```json
{
  "thread": {
    "mobile_no": "917025985366",
    "contact_name": "Abhay",
    "last_inbound_at": null,
    "window_expires_at": null,
    "window_state": "closed",
    "opted_out": false
  },
  "messages": [
    {
      "seq": 1,
      "direction": "outbound",
      "kind": "broadcast",
      "body": "Hi Abhay, our Diwali offer on LED TVs is live.",
      "template": { "name": "diwali_offer", "language": "en_US",
                    "params": ["Abhay", "LED TVs"] },
      "status": "sent",
      "wamid": "wamid.HBgMOTE3MDI1OTg1MzY2FQIAERgSNzND"
    }
  ],
  "has_more": false
}
```

That is a contact who was sent a campaign and never replied: history is visible,
`last_inbound_at` is null, and `window_state` is `closed`.

**`404` means nothing has ever been sent to that number either.** An empty chat window
for somebody we have never contacted is a different thing from a conversation, so the
API does not invent one.

---

## The 24-hour window

WhatsApp allows a **free-text** message only within 24 hours of the customer's last
inbound message. Outside it, approved templates only.

Every thread carries the answer, so you never have to compute it:

```jsonc
"last_inbound_at":   "2026-09-07T05:03:10Z",  // null = they have never written
"window_expires_at": "2026-09-08T05:03:10Z",  // null when last_inbound_at is null
"window_state":      "open"                   // open | closing_soon | closed
```

| `window_state` | Meaning | Composer |
|---|---|---|
| `open` | More than an hour left | Enabled |
| `closing_soon` | Under an hour left | Enabled, with a warning |
| `closed` | Expired, or they never wrote | **Disabled** — offer a template |

> ⚠️ **Sending never opens the window. Only the customer writing to us does**, and it
> restarts on every message they send. A contact you broadcast to an hour ago has
> `window_state: "closed"` — as in the by-mobile example above. An app that starts a
> 24-hour timer when a *send* succeeds will show an enabled composer and every send
> will be refused.

### Clock skew — what `server_time` is for

Every response carries `server_time`. Compute your countdown as:

```
remaining = window_expires_at − (server_time + time_elapsed_locally_since_response)
```

**Not** `window_expires_at − Date.now()`. A handset clock can be minutes or hours out,
and a countdown built on it will disagree with the server about whether a send is
allowed.

`server_time` is a **skew correction, not the start of the timer.** The clock runs from
`last_inbound_at` and nothing else.

The countdown is a courtesy that stops an agent typing a paragraph they cannot send.
**The server re-checks at send time and is the only authority** — see
[`AGENT_API_MESSAGES.md`](./AGENT_API_MESSAGES.md).

---

## Errors

| HTTP | `code` | Meaning |
|---|---|---|
| `200` | — | `threads: []` or `messages: []` is normal, not a failure |
| `401` | `invalid_session` | Token or `X-Device-Id` missing/wrong/expired → clear token, show login |
| `404` | `not_found` | No such thread **for this agent's company** — also what another tenant's id returns |

---

## Client checklist

- [ ] Send **both** `Authorization` and `X-Device-Id`
- [ ] Inbox: page with `before` = `next_cursor`; stop when it is null
- [ ] Chat: page up with `before_seq`; stop when `has_more` is false
- [ ] Sync with `after_seq`, never with a timestamp
- [ ] Never use an offset/page-number for messages
- [ ] Render `body` as-is; it is already substituted
- [ ] Treat `sent` as "accepted", not "delivered"
- [ ] Disable the composer when `window_state` is `closed` **or** `opted_out` is true
- [ ] Build the countdown on `server_time`, not the device clock
- [ ] Any `401` → clear token, show login, do not retry
- [ ] Parse leniently — fields will be added without a version bump

---

## Related

| Document | Covers |
|---|---|
| [`AGENT_API_MESSAGES.md`](./AGENT_API_MESSAGES.md) | Sending into an open window |
| [`AGENT_API_BROADCASTS.md`](./AGENT_API_BROADCASTS.md) | Reaching a whole phonebook |
| [`AGENT_API_PHONEBOOKS.md`](./AGENT_API_PHONEBOOKS.md) | Where `by-mobile` numbers come from |
| `app/agent/threads.py` | The implementation |
| `tests/test_agent_send.py` | Tests covering these endpoints |
