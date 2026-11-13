# Agent App API — Replying to a customer

## `POST /agent-api/v1/threads/{thread_id}/messages`

One endpoint for every reply. Send **exactly one** of `text`, `media`, `template`.

```json
{
  "client_msg_id": "1f6c2c3e-8a0e-4d0e-9a51-6a3d2b7f9c10",
  "last_seen_rev": 117,
  "text": "Yes, the offer is valid till Sunday."
}
```

| Field | Required | Notes |
|---|---|---|
| `client_msg_id` | yes | A **new UUID per message**, made when the agent taps Send and **stored with the draft**. Reuse it on every retry of that message — see **Retries** |
| `last_seen_rev` | recommended | The highest `rev` on screen. If a teammate replied after it you get `409 teammate_replied` instead of sending |
| `force` | no | `true` to send anyway after seeing `teammate_replied` |
| `text` | one of | 1–4096 characters. **Only inside the 24-hour window** |
| `media` | one of | `{"media_id": "<POST /media id>", "caption": "…"}`. Only inside the window |
| `template` | one of | `{"template_id", "params": [...], "header_media_id"}`. **Allowed any time** — the only way to reach a customer outside the window |

### Response — `202 Accepted`

```json
{
  "message": { "id": "…", "seq": 42, "rev": 120, "direction": "outbound",
               "kind": "agent_reply", "body": "Yes, the offer is valid till Sunday.",
               "status": "sent", "failure_category": null, "wamid": "wamid.HBg…",
               "sent_by_agent_id": "…", "created_at": "…" },
  "thread_rev": 120,
  "replayed": false,
  "server_time": "…"
}
```

`status` is usually `sent`. If WhatsApp refused it, the response is still `202` with
`"status": "failed"` and a `failure_category` — the message exists in the chat as a
failed bubble; it is not an exception.

### Retries

Network drop? Send the **same body with the same `client_msg_id`**. If the first attempt
reached us, you get that message back (`200`, `"replayed": true`) and the customer is
**not** messaged twice. A key reused for a *different* message → `409 idempotency_mismatch`
(a bug in the app: make a new UUID per message).

### Teammates

Every agent who contacted the customer sees the chat, and replies from each other
(`sent_by_agent_id` tells you who; label it). To avoid two answers crossing, send
`last_seen_rev`: if a teammate replied after it, you get:

```json
{ "detail": { "code": "teammate_replied", "message": "…",
              "messages": [ { …the teammate's reply… } ] } }
```

Show their reply; let the agent send again (`"force": true`) if they still want to.

### Errors

| `code` | HTTP | Meaning |
|---|---|---|
| `not_found` | 404 | Not your conversation (you never contacted this customer, or > 30 days ago) |
| `window_closed` | 409 | Text/media outside 24 h — offer templates (`last_inbound_at`) |
| `teammate_replied` | 409 | See above |
| `idempotency_mismatch` | 409 | Same `client_msg_id`, different message |
| `recipient_opted_out` | 403 | Customer unsubscribed |
| `template_unavailable` | 409 | Template paused/disabled by WhatsApp **right now** — refresh templates |
| `param_count_mismatch` / `invalid_param` | 400 | Wrong number of values / a value with a line break, tab, 5+ spaces, or empty |
| `header_media_required` / `header_media_mismatch` / `media_expired` | 400/410 | As for broadcasts |
| `daily_limit_reached` | 403 | Agent's 24-hour limit (`sent_today`, `limit`) |
| `account_unavailable` | 503 | The company's WhatsApp account has a problem (e.g. billing) |
| `no_phone_number` | 409 | Customer is known only by WhatsApp username — not supported from the app yet |

### Failed messages — `failure_category`

| Value | Show the agent |
|---|---|
| `blocked` | "This customer can't receive messages from you" (blocked / not on WhatsApp / opted out of marketing) |
| `outside_window` | "More than 24 h since they wrote — send a template" |
| `billing` | "Your company's WhatsApp account has a billing problem" |
| `rate_limited` | "Sending too fast — try again shortly" |
| `template` | "The template was rejected" |
| `other` | `error_message` |

`unconfirmed` (status): the connection dropped mid-send; it may or may not have arrived.
It is never re-sent automatically.

`POST /agent-api/v1/messages` (free text by mobile number) still works for older builds;
new builds should use this endpoint.
