# Agent App API — Templates

**Status:** built and running. Every example below is a real response, captured from the
running server on 2026-09-02.
**Endpoint:** `GET /agent-api/v1/templates`
**Companion:** [`AGENT_API_AUTH.md`](./AGENT_API_AUTH.md) — how to obtain the token and
device header this endpoint requires.

---

## What this is for

WhatsApp allows an agent to reply in **free text only within 24 hours** of the customer's
last message. Outside that window it accepts **approved templates only**.

So this endpoint fills the picker the app shows when the composer is disabled. It returns
the templates an administrator has assigned to this specific agent — not everything the
company owns.

---

## Request

```
GET /agent-api/v1/templates
Authorization: Bearer <session token>
X-Device-Id:   <the device this session was issued to>
```

No body, no query parameters. Both headers are required, as on every authenticated call.

---

## Response — `200 OK`

An array, ordered by template name. This is a verbatim response showing all three header
shapes an app must handle:

```json
[
  {
    "id": "b56dcb71-f399-4341-8a73-c028490a26be",
    "name": "invoice_ready",
    "language": "en_US",
    "category": "UTILITY",
    "body_text": "Hi {{1}}, invoice {{2}} is ready.",
    "param_count": 2,
    "parameters": [
      { "index": 1, "label": "Param 1", "example": "Ravi" },
      { "index": 2, "label": "Param 2", "example": "INV-88" }
    ],
    "header": { "format": "DOCUMENT", "text": null },
    "footer_text": null,
    "buttons": []
  },
  {
    "id": "50cf9403-741f-48b8-8baf-cf8f89fec74d",
    "name": "order_update",
    "language": "en_US",
    "category": "UTILITY",
    "body_text": "Hello {{1}}, your order {{2}} ships on {{3}}.",
    "param_count": 3,
    "parameters": [
      { "index": 1, "label": "Param 1", "example": "Ravi" },
      { "index": 2, "label": "Param 2", "example": "SO-1201" },
      { "index": 3, "label": "Param 3", "example": "5 Sep" }
    ],
    "header": { "format": "TEXT", "text": "Order update" },
    "footer_text": "Softland India",
    "buttons": [ { "type": "QUICK_REPLY", "text": "Track order" } ]
  },
  {
    "id": "6740726b-51f9-473e-b0f4-935dbe9342e4",
    "name": "reminder_simple",
    "language": "en_US",
    "category": "UTILITY",
    "body_text": "This is a reminder from Softland India.",
    "param_count": 0,
    "parameters": [],
    "header": { "format": null, "text": null },
    "footer_text": null,
    "buttons": []
  }
]
```

### Fields

| Field | Type | Notes |
|---|---|---|
| `id` | uuid | Our identifier. Stable — use it as the list key |
| `name` | string | **Part of the send address.** WhatsApp identifies a template by name + language, never by `id` |
| `language` | string | The other half of the send address, e.g. `en_US` |
| `category` | string | `UTILITY`, `MARKETING` or `AUTHENTICATION`. Affects billing, not behaviour |
| `body_text` | string | The approved text with `{{n}}` placeholders **left in**. Render it as a live preview |
| `param_count` | int | Exactly this many values must be supplied, in order. `0` means nothing to fill in |
| `parameters[]` | array | One entry per placeholder, ascending. Empty when `param_count` is `0` |
| `parameters[].index` | int | The placeholder number — `1` fills `{{1}}` |
| `parameters[].label` | string | A display label. **Often just `"Param 1"`** — see below |
| `parameters[].example` | string | Meta's own sample value. Good placeholder text for the input |
| `header.format` | string¦null | `null`, `TEXT`, `IMAGE`, `DOCUMENT` or `VIDEO` |
| `header.text` | string¦null | Filled **only** when `format` is `TEXT`. A media header carries no text |
| `footer_text` | string¦null | Small print under the message |
| `buttons[]` | array | Empty if the template has none |
| `buttons[].type` | string | `QUICK_REPLY` returns an inbound message when tapped; `URL` just opens a link |
| `buttons[].text` | string | The button's label |

---

## Building the compose screen

**Show `body_text` with the placeholder highlighted, above the inputs.** This is the part
that matters most for usability, and it is why `body_text` is returned with `{{n}}` intact
rather than stripped.

```
┌──────────────────────────────────────────────┐
│  Order update                    ← header    │
│                                              │
│  Hello ▮Ravi▮, your order SO-1201            │
│  ships on ▮▮▮▮.               ← live preview │
│                                              │
│  Softland India                  ← footer    │
│  [ Track order ]                 ← button    │
└──────────────────────────────────────────────┘

  Param 1   [ Ravi          ]
  Param 2   [ SO-1201       ]
  Param 3   [               ]   ← currently focused
```

> **On `label`.** It falls back to `"Param 1"`, `"Param 2"` unless an administrator has
> separately mapped that template's parameters. That is not a bug and it is not worth
> waiting for — **the sentence is what tells the agent what belongs in the box.** Show the
> label *and* highlight the matching placeholder in the preview as they type, and the form
> is clear regardless of the label text.

**A `param_count` of `0` still needs the preview**, just with no inputs — the agent should
see what they are about to send before sending it.

**Media headers cannot be filled from the app.** If `header.format` is `IMAGE`,
`DOCUMENT` or `VIDEO`, the file is supplied server-side at send time. Show the template
normally; do not offer a file picker.

---

## Three behaviours to build around

### An empty array is normal, not an error

```json
[]
```

It means an administrator has not assigned any templates to this agent yet. Say so
plainly — *"No templates have been assigned to you. Contact your administrator."*

It deliberately does **not** fall back to "all of the company's templates". A template
sent outside the 24-hour window is a billable message, so an agent gets exactly what
somebody chose to give them and nothing by default.

### The list can change between calls — never cache it

Template structure is read live on every request. When a template is edited or
re-approved at Meta, the change reaches you on your **next call** — including a changed
`param_count`.

> ⚠️ **Do not persist `param_count` across sessions.** Read it from the same response you
> are about to send with. A cached count that has since gone from 3 to 4 produces a send
> WhatsApp rejects for a parameter mismatch, and nothing in the UI would hint at why.

### A template can disappear

One paused, rejected or deleted at Meta is dropped from this list, because sending it
would fail. Handle a template you were showing being absent after a refresh — the list
does not only grow. An administrator can see the paused template, flagged, in the web
panel, so the agent's answer is always *"contact your administrator"*.

---

## Errors

| HTTP | Body | Meaning |
|---|---|---|
| `200` | `[]` | No templates assigned. **Not an error** |
| `401` | `{"detail":{"code":"invalid_session",…}}` | Token missing, unknown, revoked, expired, or `X-Device-Id` missing/wrong |

There are no other failure modes. A `401` means the same thing here as everywhere else in
this API: **clear the stored token and show the login screen.** Do not retry.

---

## How an agent gets templates

For context — this is done once by an administrator in the web panel, not by the app:

1. Open the agent's user record → **Agent Config** tab.
2. Under **Templates this agent can send**, pick from the dropdown. It lists only
   *approved* templates belonging to that agent's own company.
3. Each selection appears as a chip with an **×** to remove it.
4. Save.

Saving stores the full set — what is on screen is what the agent gets. Different agents in
the same company can have completely different lists.

---

## Worked example

```bash
BASE=https://<host>/agent-api/v1

TOKEN=$(curl -s -X POST $BASE/auth/login -H 'Content-Type: application/json' -d '{
  "username":"ravi.k","password":"•••","customer_id":"FM-2291",
  "device_id":"9774d56d682e549c"}' | jq -r .token)

curl -s $BASE/templates \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Device-Id: 9774d56d682e549c"
```

```bash
# Missing the device header — 401, as on every endpoint
curl -s -o /dev/null -w '%{http_code}\n' $BASE/templates \
  -H "Authorization: Bearer $TOKEN"
# → 401
```

---

## Client checklist

- [ ] Send **both** `Authorization` and `X-Device-Id`
- [ ] Treat `[]` as "none assigned", never as a failure
- [ ] Re-read before composing; **never cache `param_count`**
- [ ] Render `body_text` with the active placeholder highlighted
- [ ] Send using `name` + `language`, not `id`
- [ ] Handle a previously-shown template being gone after a refresh
- [ ] No file picker for `IMAGE`/`DOCUMENT`/`VIDEO` headers
- [ ] Any `401` → clear token, show login, do not retry
- [ ] Parse leniently — fields will be added without a version bump

---

## Related

| Document | Covers |
|---|---|
| [`AGENT_API_AUTH.md`](./AGENT_API_AUTH.md) | Login, session, sign out, device binding |
| [`AGENT_API_PHONEBOOKS.md`](./AGENT_API_PHONEBOOKS.md) | The contacts an agent may work from |
| `../AGENT_INBOX_DESIGN.md` | The wider system design, including the 24-hour window |
| `app/agent/templates.py` | The implementation |
| `tests/test_agent_templates.py` | 20 tests covering this endpoint and the mapping behind it |
