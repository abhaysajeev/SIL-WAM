# Lizo — client integration

Status as of **2026-08-10**: the flow has changed shape. The order receipt is now
an **image supplied by the client** and sent as the template's media header, so the
**View Order button and the free-form summary have been removed** — `app/lizo/summary.py`
is deleted. One button remains, *Confirm Order*, still a logging stub.
**Nothing is committed or deployed** — all Liso work is in the working tree.

> Sections below written before 2026-08-10 describe the two-button design and are
> kept as the record of why it existed. Where they conflict with this header, this
> header wins.

---

## 1. What Lizo is

Lizo (referred to as **Client X**, a .NET system) sends order data; we send a
WhatsApp order-confirmation template, then respond to two buttons on it:

| Button | Behaviour |
|---|---|
| **Confirm Order** | Record the confirmation, POST back to the client (**stub**) |

*(Removed 2026-08-10: a **View Order** button that sent the order summary as a
free-form message. The image header replaced it. A tap from an older message now
falls to the unknown-button branch and does nothing.)*

Unlike SFA/Shirin Asal, **Lizo has no questionnaire**. That single fact drives
most of the design — a service with no questions is completed by
`queue_manager.py:157` the moment its template sends.

## 2. The contract

`POST /client-api/v1/lizo/orders`, header `X-API-Key: <lizo's key>`.

Four fixed top-level fields, plus a `data` dictionary of **arbitrary shape**.
snake_case throughout (confirmed with the client). `template_expiry_hours` is in
hours — though see §7, it is a no-op for Lizo.

```json
{
  "service_id": "LIZO-ORD-10234",
  "template_name": "lizo_test",
  "template_expiry_hours": 24,
  "customer_mobile": "919876543210",
  "data": {
    "customer_name": "Ravi Kumar",
    "store_name": "Lizo Store",
    "order_no": "10234",
    "order_date": "30/07/2026",
    "items": {
      "item_1": {"item": "A", "qty": 2},
      "item_2": {"item": "B", "qty": 1}
    },
    "summary": {
      "subtotal": "1499.00", "discount": "150.00",
      "gst": "45.00", "net_amount": "1394.00"
    }
  }
}
```

`data` is deliberately opaque — any keys, any nesting. Which field fills which
template placeholder is configured per template through the mapping UI, not in
code.

**Reserved keys** — rejected with a 422 naming the offending key:

| Key | Why |
|---|---|
| `questions` | The shared ingest lifts it out of `data` and turns the service into a questionnaire, so the order never auto-completes. A non-list value crashes it (500). |
| `customer_mobile` | Sent as a top-level field. Two disagreeing sources of truth is ambiguous. |
| `_flow` | Our routing marker (§5). A client must not be able to spoof it. |

**Money and dates must be strings, but this is _not_ enforced.** `data` is typed
`dict[str, Any]`, so pydantic never inspects its contents — `"subtotal": 1499.00`
returns 201 and renders to the customer as `₹1499.0` (verified 2026-08-06 against
the live endpoint; an earlier note here claiming a 422 was wrong). `_get_nested`
does `str(val)` and `summary.render` interpolates directly, so a float degrades
silently in both the template params and the summary. Client X controls formatting
and there is no safety net.

## 3. The template — `lizo_test`

UTILITY · en_US · APPROVED. **8 parameters**, two Quick Reply buttons
(*View Order*, *Confirm Order*).

```
Dear {{1}},
Thank you for your order with {{2}}.

Order No: {{3}}
Order Date: {{4}}

*Subtotal:* ₹{{5}}
*Discount:* ₹{{6}}
*GST:* ₹{{7}}
*Net Amount:* ₹{{8}}

Please verify the above details.
If everything looks correct, tap *Confirm Order* below.
If you notice any discrepancy, please contact us before confirming.
```

Working `param_mapping` (set on the dev DB; **must be set again wherever this is
deployed**, it is per-template config, not code):

```json
{"1":"data.customer_name",      "2":"data.store_name",
 "3":"data.order_no",           "4":"data.order_date",
 "5":"data.subtotal",           "6":"data.discount",
 "7":"data.gst",                "8":"data.net_amount"}
# 2026-08-10: totals moved from data.summary.* to the top level of data,
# and header_mapping = "data.ImageURL" was added.
```

Dot-paths resolve against `{"data": …, "service_id": …}`, hence the `data.`
prefix on every one.

## 4. Three platform constraints that shaped this

**Template parameters cannot contain newlines.** Meta returns HTTP 400 —
*"Param text cannot have new-line/tab characters or more than 4 consecutive
spaces"*. So a multi-line organised summary **cannot** be a template placeholder.
This is why `lizo_test` has no summary slot and the summary is a separate message.

**Free-form messages need an open 24-hour window, and only the customer can open
it.** A Quick Reply tap is an inbound message, so it opens the window — which is
exactly why the two-button design works. Sending the summary unprompted right
after the template would be rejected.

**Arrays cannot be mapped to placeholders.** `_get_nested` ends with
`return str(val)`, and there is no index syntax:

```
data.items          -> "{'item_1': {'item': 'A', 'qty': 2}, …}"   Python repr
data.items.0.item   -> ''
data.items[0].item  -> ''
```

Client X moved `items` from an array to a dict (`item_1`, `item_2`) which makes
every *scalar* addressable — but a variable-length list still cannot be joined
into one fixed placeholder. Resolved by putting the summary in a free-form
message instead.

## 5. Architecture — and the isolation constraint

**Hard rule: the live SFA/Shirin Asal pipeline must not change.**

Every inbound webhook funnels through `conversation_engine.handle_inbound`, and
the codebase has no hook or dispatch mechanism (`_is_download_invoice` is the only
inline branch, and it is global). So one guarded branch was unavoidable.

```
POST /client-api/v1/lizo/orders
  → app/lizo/api.py  (reshape only)
  → ingest_service()  ← shared: auth, dedup, template check, param resolution
  → Service(questions=[], data={…, "_flow": "lizo_order"})
  → send_scheduler → template sent → status = "completed"

customer taps a button
  → /webhook/meta → handle_inbound → step 7 (msg_type == "button")
  → guard: lizo_inbound.handles(service)
       Confirm Order → set data["lizo_confirmed_at"] → _post_confirmation() STUB
       anything else → logged, no action (incl. View Order on older messages)
```

**The discriminator** is `Service.data["_flow"] == "lizo_order"`, stamped by
`app/lizo/schemas.py`. No pre-existing row carries it, so for Shirin the guard is
false and the following lines execute exactly as before. Chosen over a new column
because `Service.data` is already the per-row behaviour switch the engine reads
(`completion_message`, `invoice_no`, `pdf_sent`), it needs no migration, and it is
never echoed to clients — `notify_queue` builds payloads from Service columns.

**A tap resolves to its Service** via `context.id` → `Message.wamid` →
`Message.service_id` (`_resolve_service_from_context`). There is **no status
filter** on that path, which is why an already-`completed` Lizo service is still
found.

### Files

| File | Lines | Role |
|---|---|---|
| `app/lizo/api.py` | ~75 | Route. Dedup + param pre-check, then delegates to `ingest_service` |
| `app/lizo/schemas.py` | 96 | Payload contract, reserved keys, `_flow` marker |
| `app/lizo/responses.py` | ~110 | The four-key response envelope, Liso's alone |
| `app/lizo/route.py` | ~85 | `LizoRoute` — renders every outcome in that envelope |
| `app/lizo/validation.py` | ~80 | Rejects blank template params before the send |
| `app/lizo/inbound.py` | ~105 | `handles()` + tap dispatch + confirm idempotency |
| `app/services/conversation_engine.py` | **+11** | The only shared-code change |
| `tests/test_lizo.py` | 277 | Ingest — 24 tests |
| `tests/test_lizo_inbound.py` | 247 | Taps + renderer — 21 tests, incl. the Shirin control |

Nothing now reads `data["items"]` by name — with the summary gone, the itemised list
exists only inside the image the client renders. `items` is stored and never used.

**Idempotency:** buttons stay tappable forever and every tap carries a fresh
wamid, so `handle_inbound`'s wamid dedup does not stop repeats. Confirm Order is
guarded by
`data["lizo_confirmed_at"]` + `flag_modified(service, "data")` — the JSONB
mutation is invisible to SQLAlchemy without it.

## 6. Verification

- **348 passed, 1 failed** — the failure is the pre-existing
  `test_expiry_scheduler::test_past_deadline_with_answered_question_not_expired`.
  Run the suite alone; concurrent runs share `sil_wam_test` and corrupt each other.
- `git diff app/services/conversation_engine.py` → **11 insertions, 0 deletions**.
- Shirin regression suites green untouched: `test_conversation_engine_notify.py`,
  `test_concurrent_services.py`, `test_notify_queue.py`, `test_send_retry.py`,
  `test_retry_notifications.py`.
- `TestIsolation::test_shirin_tap_still_fires_responded_and_next_question` drives a
  Shirin-shaped service through the same entry point and asserts `handle_tap` is
  never called and the old behaviour is intact.
- `alembic revision --autogenerate` emits an empty `upgrade()` — no schema change.

### Live test performed 2026-08-03

Real template delivered to **917025985366** (`service_id LIZO-TEST-172956`), all 8
placeholders resolved, then the summary delivered after a View Order tap:

```
*Order Summary*

Order No: 10234
Date: 03/08/2026

1. A × 2
2. B × 1

Subtotal: ₹1499.00
Discount: ₹150.00
GST: ₹45.00
*Net Amount: ₹1394.00*
```

### Testing taps without touching production

Meta allows **one callback URL per Meta app**, and it serves every tenant. Pointing
it at a tunnel to a laptop sends **Shirin Asal's live inbound there instead**, and
Meta does not re-deliver it afterwards. Do not repoint the webhook outside a
maintenance window.

Instead, POST a signed webhook straight at a local server — this exercises
signature verification, service resolution, the guard and the send:

```python
import hashlib, hmac, json, time, urllib.request
from app.core.config import settings

payload = {"object": "whatsapp_business_account", "entry": [{"id": "0", "changes": [
  {"field": "messages", "value": {
    "messaging_product": "whatsapp",
    "metadata": {"phone_number_id": "<the account's phone_number_id>"},
    "messages": [{
      "context": {"id": "<wamid of the outbound template>"},
      "from": "917025985366",
      "id": f"wamid.SIMTAP{int(time.time())}",
      "timestamp": str(int(time.time())),
      "type": "button",
      "button": {"text": "View Order", "payload": "View Order"},
    }]}}]}]}

raw = json.dumps(payload).encode()
sig = "sha256=" + hmac.new(settings.META_APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
urllib.request.urlopen(urllib.request.Request(
    "http://127.0.0.1:8050/webhook/meta", data=raw,
    headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig}))
```

The service must exist in the **local** DB — a service created locally cannot be
resolved by production, and vice versa.

## 7. Open items

**`{{2}}` (store name) has no source in Client X's payload.** Currently it must
arrive as an extra `data.store_name` key. Either they add it, or hardcode it in
`to_ingest_request()`.

**Confirm Order POST is a stub.** `_post_confirmation()` only logs. The real one
needs a channel decision — reuse `CompanyApiKey.notify_url` or a dedicated Lizo
endpoint — plus retry semantics. It cannot go through `notify_queue` as-is:
`notify_queue.py:88` suppresses non-terminal events once a service is terminal,
and a Lizo service is `completed` from the moment its template sends. That
suppression is also why the built-in `responded` notification never fires for Lizo.

**`template_expiry_hours` is accepted but inert.** `expiry_scheduler.py:101` skips
services with no questions, and Lizo services complete immediately. Keep the field
for contract compatibility; it will do nothing unless Lizo gains a questionnaire.

**Not deployed.** `app/lizo/` is untracked and `conversation_engine.py` is
modified but uncommitted. Production has neither.

## 8. Platform bugs found along the way — not Lizo-specific

**`wa_sender.send_template` does not sanitise parameters.** Values go straight to
Meta:

```python
"parameters": [{"type": "text", "text": v} for v in body_params],
```

Any mapped field containing a newline — a multi-line delivery address is the
obvious one — produces a Meta 400. `_fail_or_schedule_retry` then retries it with
backoff and fails identically every time before landing on `send_error`, with
nothing connecting the failure to a line break. **This affects SFA/Shirin today.**

**Silent blanks on any mapping typo.** `_get_nested` returns `''` for a missing
path, so `data.custmer_name` sends a message reading *"Dear ,"* — no exception, no
log, no failed status, and the client is told the service completed. Suggested
mitigation: log a warning when a *mapped* path resolves empty.

**No path discovery in the mapping UI.** `companies/detail.html:2384` is a bare
free-text input with an empty placeholder — the admin must already know
`data.summary.net_amount` and type it exactly. Combined with silent blanks, a
wrong guess produces no feedback at all. Highest-leverage fix would be storing the
last-seen payload per template so the UI can offer real paths.

## 9. Related

- `docs/BROADCAST_TIPS.md` — messaging tiers, the 24-hour window, verified Meta
  platform rules
- `docs/BROADCAST_DESIGN.md` — broadcast module design
- Webhook app config: production inbound runs on the Softland parent app
  `981233374512490` (multi-tenant again as of 2026-08-03). One global
  `META_APP_SECRET`, so onboarding a client on a *different* Meta app would need
  multi-secret verification.
