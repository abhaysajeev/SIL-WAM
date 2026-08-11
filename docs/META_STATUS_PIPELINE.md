# How Meta tells us a message was delivered

Everything that happens between "we sent a WhatsApp message" and "we know it arrived".
Written as the groundwork for Liso's status callback, but it describes the existing
pipeline, which is unchanged and shared by every client.

Source files: `app/api/meta_webhook.py` · `app/services/conversation_engine.py` ·
`app/services/wa_sender.py` · `app/services/queue_manager.py`

---

## 1. The short version

```
    we send  ──────────────► Meta   ── returns ──►  wamid
                                                     │
                                          stored on messages.wamid
                                                     │
    Meta delivers to the phone                       │
                                                     ▼
    Meta POSTs a status receipt ──► /webhook/meta ──► look up that wamid
       {"id": "<the same wamid>",                    ──► update the Message row
        "status": "delivered"}                       ──► find its Service
```

**The wamid is the only thing linking a delivery receipt back to an order.** Everything
else in this document is plumbing around that one identifier.

---

## 2. The wamid: what it is and where it comes from

When we POST a message to Meta's Graph API, the response body is:

```json
{
  "messaging_product": "whatsapp",
  "contacts": [{"input": "917025985366", "wa_id": "917025985366"}],
  "messages": [{"id": "wamid.HBgMOTE3MDI1OTg1MzY2FQIAERgSMjcxRUQ2RUIyNzRBQkZDNEZEAA=="}]
}
```

That `messages[0].id` is the **wamid** — WhatsApp Message ID. It is Meta's permanent
handle for this one message to this one recipient. It is opaque, base64-ish, and about
60–70 characters.

`wa_sender` pulls it out:

```python
# app/services/wa_sender.py:80-83
if res.status_code == 200 and data.get("messages"):
    wamid = data["messages"][0].get("id")
    return SendResult(True, wamid, None)
```

> **A 200 here means Meta accepted the message, not that anyone received it.** For a
> template with a media header Meta has not even fetched the image yet. Everything about
> actual delivery arrives later, through the webhook.

`queue_manager` writes it to the database immediately:

```python
# app/services/queue_manager.py:145-155
db.add(Message(
    conversation_id = service.conversation_id,
    service_id      = service.id,          # ← the link back to the order
    wamid           = result.meta_message_id,
    direction       = "outbound",
    message_type    = "template",
    status          = "sent",
    sent_at         = now,
))
```

The `messages` table is where the mapping lives:

| Column | Purpose |
|---|---|
| `wamid` | Meta's id. **`UNIQUE`** (`uq_messages_wamid`) — one row per real message |
| `service_id` | FK to `services`. This is how a receipt reaches an order |
| `conversation_id` | FK to `conversations` (the customer) |
| `status` | `sent` → `delivered` → `read`, or `failed` |
| `sent_at` / `delivered_at` / `read_at` | Timestamps, filled in as receipts arrive |

If the row does not exist, a receipt for that wamid cannot be attributed to anything.
That matters — see §7.

---

## 3. Meta's status receipt

Minutes or seconds later, Meta POSTs to our webhook. A delivery receipt looks like this:

```json
{
  "object": "whatsapp_business_account",
  "entry": [{
    "id": "995511569886523",
    "changes": [{
      "field": "messages",
      "value": {
        "messaging_product": "whatsapp",
        "metadata": {
          "display_phone_number": "...",
          "phone_number_id": "1156947457499664"
        },
        "statuses": [{
          "id": "wamid.HBgMOTE3MDI1OTg1MzY2FQIAERgSMjcxRUQ2RUIyNzRBQkZDNEZEAA==",
          "status": "delivered",
          "timestamp": "1786500123",
          "recipient_id": "917025985366",
          "conversation": {"id": "...", "origin": {"type": "utility"}},
          "pricing": {"billable": true, "category": "utility"}
        }]
      }
    }]
  }]
}
```

The fields we actually read:

| Field | Meaning |
|---|---|
| `statuses[].id` | **The wamid.** The same string Meta gave us at send time |
| `statuses[].status` | `sent` · `delivered` · `read` · `failed` |
| `statuses[].timestamp` | Unix seconds, as a **string** |
| `statuses[].errors[]` | Present only when `status == "failed"` — carries `code` |
| `metadata.phone_number_id` | Which of our WhatsApp numbers this concerns |

A `failed` receipt carries the reason:

```json
"statuses": [{
  "id": "wamid...",
  "status": "failed",
  "errors": [{
    "code": 131053,
    "title": "Media upload error",
    "error_data": {"details": "Unable to download the media from the provided URL"}
  }]
}]
```

**One message produces several receipts**, arriving separately and minutes apart:
`sent` → `delivered` → `read`. `read` only arrives if the customer opens the chat, so
most messages stop at `delivered`.

---

## 4. Arrival: `POST /webhook/meta`

`app/api/meta_webhook.py`

### 4.1 Signature check

```python
# :77-84
expected = "sha256=" + hmac.new(META_APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
return hmac.compare_digest(expected, header)
```

Meta signs the **raw request body** with the app secret and sends it as
`X-Hub-Signature-256`. A mismatch returns `403` and nothing is processed — this is what
stops anyone POSTing fake delivery receipts at us.

> If `META_APP_SECRET` is unset the check is skipped and a warning is logged. That is a
> dev convenience and must never be the case in production.

### 4.2 Answer immediately, work later

```python
# :69-72
background_tasks.add_task(_process_payload_bg, body)
return JSONResponse({"status": "ok"})       # always 200
```

Meta retries aggressively on any non-200 or slow response, and a retry storm would
multiply the work. So we return `200` before doing anything, and process in a
`BackgroundTask` with **its own database session** (`SessionLocal()` in
`_process_payload_bg`, closed in a `finally`).

Consequence worth internalising: **nothing downstream of this point can report failure
to Meta.** If processing throws, Meta already believes we succeeded. That is why the
next part exists.

### 4.3 Failures are captured, not lost

```python
# :101-116
db.add(FailedWebhook(source="meta", raw_payload=body,
                     error_type=type(exc).__name__, traceback=..., replayed=False))
```

Any exception during processing stores the entire raw payload in `failed_webhooks` so it
can be inspected and replayed later. Nothing is silently dropped.

### 4.4 Unwrapping and routing

```python
# :119-156
for entry in body["entry"]:
    for change in entry["changes"]:
        if change["field"] != "messages": continue        # ignore other subscriptions
        value    = change["value"]
        statuses = value.get("statuses", [])              # delivery receipts
        messages = value.get("messages", [])              # customer replies, button taps

        account = db.query(WhatsAppAccount).filter(
            WhatsAppAccount.phone_number_id == value["metadata"]["phone_number_id"]
        ).first()
        if not account:  continue                         # not one of our numbers

        for status in statuses:  conversation_engine.handle_status(db, status, account)
        for msg    in messages:  conversation_engine.handle_inbound(db, account, msg)
```

Three things to note:

- The payload is **batched and nested**. One POST can carry several entries, several
  changes, and several statuses. Always loop.
- `phone_number_id` → `WhatsAppAccount` is the **multi-tenant hop**. One webhook URL
  serves every company on the app; the account lookup decides whose message this is. An
  unknown `phone_number_id` is skipped silently.
- `statuses` and `messages` are different arrays with different handlers. Delivery
  receipts and customer replies never mix.

---

## 5. Processing: `conversation_engine.handle_status`

`app/services/conversation_engine.py:246`

### Step 1 — read the receipt

```python
wamid     = status.get("id")
state     = status.get("status")        # sent | delivered | read | failed
timestamp = status.get("timestamp")
if not wamid or not state: return
```

### Step 2 — find the message

```python
msg = db.query(Message).filter(Message.wamid == wamid).first()
```

**This is the join.** Unique index on `wamid`, so it is a single-row lookup.

### Step 3 — no row? try broadcasts

```python
if not msg:
    if broadcast_status.handle(db, wamid, state, status):
        db.commit(); return
    logger.debug("Status receipt for unknown wamid=%s", wamid)
    return
```

Broadcast sends deliberately write **no `Message` rows** — at 1,000 recipients per
campaign that would flood the table — so their receipts are matched against
`broadcast_recipients.wamid` instead. A transactional send always has a `Message` row and
never reaches this branch.

### Step 4 — update the message

```python
msg.status = state
ts = datetime.fromtimestamp(int(timestamp), tz=timezone.utc) if timestamp else None
if   state == "delivered" and ts: msg.delivered_at = ts
elif state == "read"      and ts: msg.read_at      = ts
```

Meta's timestamp is a **string of unix seconds** and is converted to an aware UTC
datetime. A malformed one degrades to `None` rather than raising.

### Step 5 — reach the order

```python
if msg.service_id:
    service = db.query(Service).filter(Service.id == msg.service_id).first()
```

`Message.service_id` → `Service` is the hop from *"a WhatsApp message was delivered"* to
*"Liso order 26OS02LC00007 was delivered"*.

### Step 6 — async failures

```python
if state == "failed" and service.status == "in_progress":
    error_codes   = {e.get("code") for e in status.get("errors", [])}
    failed_reason = ("whatsapp_number_invalid"
                     if error_codes & {131026} else "send_error")
    service.status        = "failed"
    service.failed_reason = failed_reason
    queue_manager._mark_queue_completed(db, service)
```

This branch exists because **Meta can accept a send and reject it moments later.** The
synchronous call returned `ok=True` with a wamid, so `queue_manager` saw success; the
real outcome only arrives here. A bad media URL (`131053`) and an unreachable number
(`131026`) both land in this branch.

> **Note the guard: `service.status == "in_progress"`.** A Liso order is already
> `completed` — it has no questions, so `queue_manager` completes it the moment the
> template send returns. **This branch therefore never fires for Liso**, and a failed
> receipt currently leaves the service looking successful. This is one of the two things
> the callback work has to solve.

### Step 7 — notify the client

```python
notify_queue.enqueue_notification(db, service, state, message=msg, note=note)
```

The last step, and the one Liso cannot use — see §6.

### Step 8 — commit

One `db.commit()` at the end, wrapped so a failure rolls back and is logged rather than
escaping into the BackgroundTask.

---

## 6. Why Liso needs its own notifier

`notify_queue.enqueue_notification` has two gates:

```python
# notify_queue.py:88 — a terminal service ignores non-terminal events
if service.status in ("completed","expired","failed") and event_status not in (
        "completed","expired","failed"): return

# notify_queue.py:95 — status may only move forward
_STATUS_RANK = {"sent":1,"delivered":2,"read":3,"responded":4,"answered":5,
                "completed":6,"expired":6,"failed":6}
if _STATUS_RANK.get(event_status,0) <= _max_notified_rank(db, service.id, attempt): return
```

Both are correct for Shirin Asal, whose services run a long questionnaire and where a
late receipt must not drag the dashboard backwards. Both are wrong for Liso:

| Event | What happens today |
|---|---|
| Template sent → `completed` fires | ✅ the client is told, immediately |
| `sent` / `delivered` / `read` arrive | ❌ dropped by gate 1 — service is already terminal |
| `failed` arrives (bad image URL) | ❌ dropped by gate 2 — rank 6 ≤ rank 6 |

**Net effect: Liso receives exactly one callback, saying the order succeeded, sent before
Meta had even fetched the image.** A broken receipt image would be reported as a success.

The way out: `notify_scheduler` is completely payload-agnostic —

```python
# notify_scheduler.py:97
resp = client.post(notif.notify_url, json=notif.payload)
```

It POSTs whatever JSONB is in the row to whatever URL is in the row, retrying **8 times**
with exponential backoff capped at an hour. So Liso's notifier can insert
`OutboundNotification` rows **directly**, with its own payload shape, and inherit all of
that delivery machinery while skipping the suppression entirely.

---

## 7. Things that will bite you

**A `200` from the send API is not delivery.** It means the request was well-formed.
Media is fetched afterwards; failures arrive as receipts.

**Receipts can arrive out of order or repeat.** Meta gives at-least-once delivery. The
`wamid` unique constraint means repeats update the same row, but a consumer must treat
status as monotonic rather than assuming a strict sequence.

**`read` may never arrive.** It requires the customer to open the chat, and depends on
their privacy settings. Never wait on it.

**One webhook URL serves every company.** It is configured at the Meta *app* level, so
changing it — for example to an ngrok tunnel for testing — redirects **every client's**
traffic, production included.

**The webhook returns 200 before processing.** Meta can never learn that we failed to
handle something. `failed_webhooks` is the only safety net.

**No `Message` row means no attribution.** Broadcasts have their own path; anything else
without a row is logged at debug and dropped.

---

## 8. Where the Liso callback plugs in

```
handle_status  (§5)
    step 5   service resolved
    step 6   async failure detected      ← needs a Liso-aware guard (see §5 note)
    step 7   notify_queue.enqueue_notification(...)      ← Shirin
             if lizo_notify.handles(service):            ← Liso, new
                 lizo_notify.emit(service, state, reason)
```

`lizo_notify.handles(service)` is the same discriminator already used for button taps —
`service.data["_flow"] == "lizo_order"`, stamped at ingest by `app/lizo/schemas.py` and
carried by no other client. Shirin evaluates it as false and runs exactly as before.

`emit()` builds the SFA envelope and inserts an `OutboundNotification` row. Delivery,
retries and backoff are the existing scheduler's job.

New code lives in **`app/lizo/notify.py`**. Shared files take one guarded line each.
