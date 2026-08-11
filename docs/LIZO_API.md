# Liso Order API

Send an order to a customer on WhatsApp. The customer receives one message — the
order receipt as an image, the totals as text, and a single **Confirm Order** button.

Internal reference: `app/lizo/api.py` · `app/lizo/schemas.py` · `app/lizo/validation.py`

---

## Endpoint

```
POST https://silwhatsapp.softlandindia.net/client-api/v1/lizo/orders
```

| | |
|---|---|
| Auth | `X-API-Key: <your key>` header |
| Content-Type | `application/json` |
| Success | `201 Created` |

---

## Request

```json
{
  "service_id": "26OS02LC00007",
  "template_name": "liso_with_image",
  "template_expiry_hours": 24,
  "customer_mobile": "917025985366",
  "data": {
    "customer_name": "GIANT BAZAAR, Pattom",
    "store_name":    "Liso",
    "order_no":      "26OS02LC00007",
    "order_date":    "17/07/2026",
    "ImageURL":      "http://your-server/FSSLisoFileServer/Sfa/Orders/2026/08/26OS02LC00029.jpg",
    "items": [
      { "item": "Almond Spread 190gm", "qty": 1.000 },
      { "item": "Liso Pebbles 2 Pcs Colour Sachet", "qty": 1.000 }
    ],
    "subtotal":   "211.230",
    "discount":   "0.000",
    "gst":        "10.561",
    "net_amount": "222.000"
  }
}
```

### Top-level fields

| Field | Type | Required | Notes |
|---|---|---|---|
| `service_id` | string | **yes** | Your own order reference. Must be unique — a repeat is rejected `409`, which returns the original `reference_id`. |
| `template_name` | string | **yes** | Must match an approved template **exactly**, including spelling. Currently `liso_with_image`. |
| `template_expiry_hours` | int | no (24) | Accepted but has no effect on this flow — the button stays tappable. |
| `customer_mobile` | string | **yes** | **Country code required.** 11–15 digits, optional leading `+`, no spaces or dashes. `917025985366` or `+917025985366` both work; a bare 10-digit `7025985366` is rejected `422`. |
| `data` | object | **yes** | The order itself. Any keys, any nesting — see below. |

### `data`

`data` is open-ended: add fields freely, nothing breaks and no code change is needed on
our side. Which field fills which slot in the message is configured per template in the
admin panel, not in code.

For `liso_with_image` these eight fields fill the message body, **and every one of them
is required**:

| Field | Fills |
|---|---|
| `customer_name` | `Dear {{1}},` |
| `store_name` | `Thank you for your order with {{2}}.` |
| `order_no` | `Order No: {{3}}` |
| `order_date` | `Order Date: {{4}}` |
| `subtotal` | `Subtotal: ₹{{5}}` |
| `discount` | `Discount: ₹{{6}}` |
| `gst` | `GST: ₹{{7}}` |
| `net_amount` | `Net Amount: ₹{{8}}` |

Plus `ImageURL`, which supplies the header image — see below.

> **Every one of the eight is mandatory, and a blank counts as missing.** Meta rejects
> the whole message if any parameter is empty, so a payload with a missing or blank
> field is rejected `422` up front, naming each offender and its field. Nothing is sent
> and no order is created.

`items` is stored with the order but **is not rendered anywhere** — the itemised list
now lives inside the header image. Send it or don't; it changes nothing in the message.

### One rule that will bite you

**Amounts and dates must be strings — and nothing will tell you if they aren't.**
`data` is accepted as-is with no type checking, so `"subtotal": 211.23` returns `201`
and then reaches the customer as `₹211.23` with whatever precision Python chooses.
There is no error to catch. Send every amount and date pre-formatted as a string,
exactly as it should appear.

### `ImageURL` — the order receipt as the header image

The itemised order is rendered by you as a JPEG and sent as the message's header image.
It is not fixed at approval time: you send a different URL for every order, exactly like
a body variable.

```json
"data": {
  "ImageURL": "http://your-server/FSSLisoFileServer/Sfa/Orders/2026/08/26OS02LC00029.jpg"
}
```

The key name is case-sensitive and must be exactly `ImageURL` — it is mapped to the
header once in the admin panel, and `data.imageurl` would resolve to nothing.

| Rule | Why |
|---|---|
| Publicly reachable, no auth, no redirects | **Meta's servers fetch the URL, not ours.** Anything behind a firewall, requiring a token, or answering with a redirect is unreachable to them. |
| Unique filename per order | Meta caches a fetched URL for about 10 minutes. Reusing a URL with new content inside that window can deliver the previous customer's receipt. |
| JPEG or PNG, max 5 MB | Meta's limit. Larger files and other formats are rejected. |
| Keep it reachable | The fetch happens at send time, and again on every retry. |

> **On `http://` vs `https://`.** Meta documents the `link` parameter as accepting
> HTTP/HTTPS, and plain HTTP appears to work, but almost all third-party guidance
> recommends HTTPS and Meta has never committed to supporting HTTP in writing. Plain
> HTTP also means a customer's order — names and amounts — travels unencrypted.
> Treat HTTPS as the safe choice.

If the mapped field is missing or blank the request is rejected `422` naming the exact
field, rather than accepting the order and failing the delivery silently minutes later.

If Meta cannot download the image, the send is retried three times and then reported
with `failed_reason: "media_error"` — distinct from `send_error`, so a broken URL is
immediately distinguishable from a problem on our side.

**Three key names are reserved inside `data`** — sending any of them is rejected
`422` naming the offender:

| Key | Why |
|---|---|
| `customer_mobile` | It is a top-level field. Two sources that could disagree is ambiguous. |
| `questions` | Turns the order into a questionnaire flow, which would never complete. |
| `_flow` | Our internal routing marker. |

Everything else in `data` passes through untouched.

---

## Response

**Every response has the same four keys** — success and failure alike, on every status
code. You never branch on the shape of the body.

```json
// 201 — accepted
{
  "service_id":   "26OS02LC00007",
  "reference_id": "89c89586-03ce-4c7c-ada8-a96fd07034ea",
  "status":       "in_progress",
  "message":      null
}
```

```json
// any error — same four keys
{
  "service_id":   "26OS02LC00007",
  "reference_id": null,
  "status":       "missing_parameter",
  "message":      "Template 'liso_with_image' needs a value for every parameter, but {{2}} ← 'data.store_name' resolved to nothing."
}
```

| Field | Type | Notes |
|---|---|---|
| `service_id` | string \| null | Echo of what you sent. `null` only if the body was unreadable (malformed JSON). |
| `reference_id` | uuid \| null | Our internal id. Store it — support lookups use it. `null` on errors, except `409`. |
| `status` | string | **The code to branch on.** One of the ten values below, always — never a sentence. |
| `message` | string \| null | `null` on success. Human-readable detail on failure. Treat its wording as liable to change. |

`in_progress` means **accepted and queued**, not delivered. The WhatsApp send happens
moments later, asynchronously, so nothing in this response reflects what Meta thought
of the message.

---

## Status codes

| HTTP | `status` | Meaning | Whose fix |
|---|---|---|---|
| `201` | `in_progress` | Accepted and queued | — |
| `401` | `invalid_api_key` | Key missing, unknown, or deactivated | Yours |
| `404` | `template_not_found` | `template_name` unknown or not approved | Yours |
| `409` | `duplicate_service_id` | `service_id` already used — see below | Yours |
| `422` | `validation_error` | Malformed payload: bad `customer_mobile`, a reserved key in `data`, a missing required field | Yours |
| `422` | `missing_parameter` | A required body field was missing or blank | Yours |
| `422` | `missing_media_url` | `ImageURL` missing or blank | Yours |
| `422` | `template_not_configured` | The template is not fully set up on our side | **Ours** |
| `503` | `whatsapp_not_configured` | No WhatsApp account for your company | **Ours** |
| `500` | `internal_error` | Unexpected failure on our side | **Ours** |

Branch on `status`, not on `message`. The last three are ours to fix — raise them with
us rather than checking your payload.

**Safe to retry:** `whatsapp_not_configured` and `internal_error`. Everything else needs
a corrected request first; retrying unchanged will fail identically.

When several fields fail validation at once they are joined into one line separated by
`; `, so nothing is lost:

```json
{"service_id": null, "reference_id": null, "status": "validation_error",
 "message": "service_id: Field required; customer_mobile: customer_mobile '12' is not a valid phone number (must be 7–15 digits, optional leading +, no spaces or dashes)"}
```

### `409` returns the original `reference_id`

A duplicate is the one error that still carries a `reference_id` — the one belonging to
the order already on file:

```json
{
  "service_id":   "LIZO-ORD-10235",
  "reference_id": "a0d58a7f-a18f-4c0d-9af6-5ecea6e03b9d",
  "status":       "duplicate_service_id",
  "message":      "service_id 'LIZO-ORD-10235' already exists for this company"
}
```

This exists for the case that actually happens: **your POST succeeded but the response
never reached you** — a timeout, a reset, a restart. Send the same request again and the
`409` hands back the reference of the original, so you can reconcile rather than being
left with an order you cannot identify. Nothing is sent twice; the duplicate is rejected.

---

## What the customer sees

**One message**, arriving immediately:

```
   ┌────────────────────────────────┐
   │   [ the receipt image you      │
   │     supplied in ImageURL ]     │
   └────────────────────────────────┘

Dear GIANT BAZAAR, Pattom,

Thank you for your order with Liso.

Order No: 26OS02LC00007
Order Date: 17/07/2026

Subtotal: ₹211.230
Discount: ₹0.000
GST: ₹10.561
Net Amount: ₹222.000

Please verify the above details. If everything looks
correct, tap Confirm Order below.
If you notice any discrepancy, please contact us
before confirming.

          [ Confirm Order ]
```

The line items appear **only inside the image** — WhatsApp does not allow line breaks
inside template parameters, so a variable-length list cannot go in the text.

**Tapping Confirm Order** records the confirmation. Only the first tap counts; later
taps are ignored. The button stays tappable indefinitely.

> There used to be a second **View Order** button that sent the itemised list as a
> follow-up text message. It has been removed — the image carries that content now.

---

## Confirmation callback — not built yet

The POST back to your system on **Confirm Order** is still to be implemented.
Proposed shape:

```json
{
  "service_id":      "LIZO-ORD-10235",
  "reference_id":    "a0d58a7f-a18f-4c0d-9af6-5ecea6e03b9d",
  "status":          "confirmed",
  "customer_mobile": "918111888008",
  "confirmed_at":    "2026-08-06T05:22:27Z"
}
```

Delivery would retry 8 times with backoff up to an hour, treating any 2xx as
success. To enable it you need to give us a receiving URL — none is configured on
the Lizo API key today.
