# Client API — Service Ingest

The first request a client system (SFA, ERPNext, Lizo, …) sends us. It creates a
service flow and puts a WhatsApp template message on its way to the customer.

Source: `app/api/client_services_api.py` · `app/schemas/service.py`

---

## Endpoint

```
POST https://silwhatsapp.softlandindia.net/client-api/v1/services
```

| | |
|---|---|
| Auth | `X-API-Key: <key>` header |
| Content-Type | `application/json` |
| Success | `201 Created` |

The API key identifies the company. There is no user login, no role check — the key
scopes everything. Keys are issued per company from the admin panel and can carry
their own `notify_url` for callbacks.

---

## Request body

```json
{
  "service_id": "SFA-2026-0043177",
  "template_name": "order_confirm_sa",
  "template_expiry_hours": 24,
  "template_params": ["Rahul", "10234", "4,500.00"],
  "cta_urls": { "0": "https://sfa.example.com/invoice/10234" },
  "data": {
    "customer_mobile": "919876543210",
    "customer_name": "Rahul",
    "order": { "no": "10234", "amount": "4,500.00" },
    "questions": [
      { "field_key": "delivered", "question": "Did you receive your order?", "answer_type": 1 },
      { "field_key": "rating",    "question": "Rate your experience",        "answer_type": 2 },
      { "field_key": "comments",  "question": "Any comments?",               "answer_type": 3 }
    ]
  }
}
```

### Top-level fields

| Field | Type | Required | Notes |
|---|---|---|---|
| `service_id` | string | **yes** | The client's own reference. Must be unique **per company** — a repeat is rejected `409`. Echoed back on every callback so the client can match without storing our ids. |
| `template_name` | string | **yes** | Template name exactly as it appears in Meta. Must already be **APPROVED** for this company. |
| `template_expiry_hours` | int | no (24) | How long the flow stays open before it expires. |
| `template_params` | string[] | no | Ordered values for `{{1}}`, `{{2}}`, … If omitted, they are resolved from the template's saved mapping — see below. |
| `cta_urls` | object | no | Dynamic URL button values, keyed by **0-indexed** button position: `{"0": "https://…"}`. If omitted, resolved from the template's saved mapping. |
| `data` | object | **yes** | The client's own payload. Stored as-is and never interpreted, except for the two reserved keys below. |

### Reserved keys inside `data`

Everything else in `data` is opaque — nest it however the client's system already
shapes it. Only these two keys mean something to us:

**`customer_mobile`** — required, always. 7–15 digits, optional leading `+`, no
spaces or dashes. We strip the `+` and store digits only, because Meta's inbound
webhook never sends one and the numbers must match for replies to route back.

**`questions`** — optional. The follow-up questions asked over WhatsApp after the
template lands. Each entry:

| Key | Meaning |
|---|---|
| `field_key` | Client's identifier for this answer. Must be unique within the service. |
| `question` | The text shown to the customer. |
| `answer_type` | `1` = yes/no · `2` = rating · `3` = free text |

Omit `questions` entirely for a send-only flow — the service completes as soon as
the template is delivered.

---

## Template parameter mapping (the alternative to sending params)

Rather than sending `template_params` on every request, a template can store a
mapping once in the admin panel, and we pull the values out of `data`:

```json
{ "1": "data.customer_name", "2": "data.order.no", "3": "data.order.amount" }
```

Four mappings exist, all using dot-paths: `param_mapping`, `cta_mapping`
(0-indexed button positions), `mobile_mapping` (single path to the phone number), and
`header_mapping` (single path to the media URL, for templates with an IMAGE, DOCUMENT
or VIDEO header).

`header_mapping` is required whenever the template has a media header — the request is
rejected `422` if it is unset, or if the path it points at is missing or blank. Meta
fetches that URL itself at send time, so it must be publicly reachable over HTTPS with
no auth and no redirects.

**Paths are resolved against the whole request envelope, not against `data`.** So a
path must be written `data.customer_name` — a bare `customer_name` resolves to an
empty string and sends a message with a hole in it. `service_id` is also reachable
at the top level.

If `template_params` is supplied on the request, it wins and the mapping is ignored.

> `mobile_mapping` does **not** make `data.customer_mobile` optional. The request is
> validated before any mapping runs, so a payload without `data.customer_mobile` is
> rejected `422` even when `mobile_mapping` points somewhere else.

---

## Response

```json
{
  "service_id":   "SFA-2026-0043177",
  "reference_id": "a0d58a7f-a18f-4c0d-9af6-5ecea6e03b9d",
  "status":       "in_progress"
}
```

| Field | Notes |
|---|---|
| `service_id` | Echo of what was sent. |
| `reference_id` | Our internal id. Needed for the retry endpoint and for support lookups — worth storing. |
| `status` | `in_progress` normally. `failed` if the service could not be activated at all. |

`in_progress` means accepted and queued — **not** delivered. The Meta send happens
asynchronously on a scheduler, and delivery is reported later via the callback.

---

## Errors

| Code | When |
|---|---|
| `401` | API key missing, unknown, or deactivated |
| `409` | `service_id` already used by this company |
| `404` | Template not found, not approved, or belongs to another company |
| `503` | No WhatsApp account configured for this company |
| `400` | Invalid `customer_mobile`; duplicate `field_key`; `answer_type` not 1/2/3 |
| `422` | Missing or malformed required fields (`data.customer_mobile` absent, wrong types) |
| `500` | Internal error — safe to retry |

Errors return FastAPI's standard shape: `{"detail": "<message>"}`.

---

## What happens next

1. We create (or reuse) the conversation for that number and record the service.
2. The send scheduler pushes the template to Meta. Failures retry twice — after 30s
   and 2 minutes — before becoming terminal. An invalid WhatsApp number never
   retries; use the retry endpoint with a corrected number instead.
3. Delivery receipts and each customer answer are posted back to the API key's
   `notify_url`.
4. The client can also poll `GET /client-api/v1/services/{service_id}` at any point.

Related endpoints, same base and same header:

```
GET  /client-api/v1/services/{service_id}          poll status and collected answers
POST /client-api/v1/services/{reference_id}/retry  resend to a corrected number
```

Note the retry endpoint takes the **`reference_id`** from this response, not the
client's own `service_id`.
