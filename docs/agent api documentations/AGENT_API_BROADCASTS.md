# Agent App API — Broadcasts

**Status:** built and tested. Every response below is real output from the application,
captured on 2026-09-07 against the test database.
**Endpoint:** `POST /agent-api/v1/broadcasts`
**Companions:** [`AGENT_API_TEMPLATES.md`](./AGENT_API_TEMPLATES.md) (what to send),
[`AGENT_API_PHONEBOOKS.md`](./AGENT_API_PHONEBOOKS.md) (who to send it to).

---

## What this is for

Sending one approved template to everyone in a phonebook.

Unlike a free-text message this works **outside the 24-hour window** — that is what
templates are for. It is also how you reach a single customer whose window has closed:
a phonebook containing just them.

Under the hood this creates an ordinary campaign, identical to one an administrator
builds in the web panel. Same queue, same opt-out screening, same delivery receipts —
so a broadcast an agent starts appears in the panel's campaign list and reports progress
the same way.

---

## Request

```
POST /agent-api/v1/broadcasts
Authorization: Bearer <session token>
X-Device-Id:   <the device this session was issued to>
Content-Type:  application/json
```

```json
{
  "phonebook_ids": ["3f88c2a1-77de-4b30-9c11-0a4e6b2f9d55",
                    "a1b2c3d4-0000-4b30-9c11-0a4e6b2f9d55"],
  "contact_ids":   ["7932ad5b-a365-46e6-a939-30f43f083d02",
                    "5d616872-05e9-4a0e-bb0b-9f5fbd2d9a9e"],
  "template_id":   "50cf9403-741f-48b8-8baf-cf8f89fec74d",
  "params":        ["Abhay", "LED TVs"],
  "header_media":  { "media_id": "0b9d6e1c-5a4f-4a43-9b0e-2f1d0c7b8a11" },
  "rendered":      "Hi Abhay, our Diwali offer on LED TVs is live.",
  "name":          "Diwali — Kerala retail"
}
```

| Field | Required | Notes |
|---|---|---|
| `phonebook_ids` | yes | One or more of **this agent's** phonebooks, from `GET /phonebooks` (max 20). A number that is in two of them — in any spelling — is messaged **once** |
| `phonebook_id` | — | Older single-book form, still accepted. Prefer `phonebook_ids` |
| `contact_ids` | no | Send to only these contacts (from any of the chosen books). **Omit for every contact** — see **Sending to some contacts** below |
| `template_id` | yes | Must be one of **this agent's** templates, from `GET /templates` |
| `params` | yes | Ordered values for `{{1}}`, `{{2}}`, … Exactly `param_count` of them |
| `header_media` | when the template has a media header | `{ "media_id": … }` from `POST /media`. **Required** when the template's `header.format` is `IMAGE`, `VIDEO` or `DOCUMENT`; **refused** otherwise. See **Media templates** below |
| `rendered` | no | Your preview text. See **Rendering** below |
| `name` | no | Campaign name shown in the web panel. Defaults to `"<first book> +N — <template>"` |

### Media templates

1. `POST /agent-api/v1/media` with the image (multipart field `file`) → `{ "media_id", "format", "expires_at", … }`.
2. Send the broadcast with `"header_media": { "media_id": "<that id>" }`.

The file is uploaded to WhatsApp **once**, at step 1, and that single upload is sent to
every recipient. Upload as soon as the agent picks the file — a wrong type or a file
that is too large is refused there, before the agent fills in the rest. See
`AGENT_API_MEDIA.md` for the size and type rules.

| `code` | HTTP | When |
|---|---|---|
| `header_media_required` | 400 | Media template, no `header_media`. `format` says which kind is needed |
| `header_media_not_allowed` | 400 | `header_media` sent for a template without a media header |
| `header_media_mismatch` | 400 | Wrong kind of file (`expected`, `supplied`) — e.g. a PDF for an IMAGE template |
| `media_expired` | 410 | The file is older than 30 days — upload it again |
| `not_found` / `media_not_found` | 404 | Unknown phonebook / file |
| `too_many_phonebooks` | 400 | More than 20 phonebooks |

> **`params` are the same for every recipient.** Per-recipient values come from a CSV
> import in the web panel; the app sends one message to a list.

---

## Response — `201 Created`

```json
{
  "campaign_id": "9fc97936-0d51-4ed0-b946-53ca939ae0ad",
  "name": "Kerala retail — diwali_offer",
  "total": 3,
  "skipped": 0,
  "sendable": 3,
  "status": "sending",
  "server_time": "2026-09-07T05:03:09.936374Z"
}
```

| Field | Notes |
|---|---|
| `total` | Unique contacts found in the phonebook, after removing duplicate numbers |
| `skipped` | Excluded before sending — unsubscribed or unreachable |
| `sendable` | **Actually being sent.** `total − skipped` |
| `status` | `sending`. Messages go out in the background; this call does not wait |

**Show `sendable` and `skipped` separately.** "Sending to 47 — 3 unsubscribed" is a
useful, honest confirmation; "success" hides the fact that three people were dropped.

Nothing has been delivered when this returns. Progress appears in the individual chat
threads as receipts arrive — see [`AGENT_API_THREADS.md`](./AGENT_API_THREADS.md).

---

## Sending to some contacts, not all

`contact_ids` narrows a send to the rows the agent ticked. The ids are the `id` field of
each contact in `GET /phonebooks` — the app already holds them, so filtering (by name,
by `agent_id`, by anything your UI offers) happens locally and only the chosen ids are
sent.

| `contact_ids` | Meaning |
|---|---|
| **omitted** | Everyone in the phonebook — the original behaviour, unchanged |
| `null` | Same as omitted |
| `["…", "…"]` | Only these contacts |
| `[]` | **Refused** — `400 nothing_selected` |

> ⚠️ **An empty array is never "send to everyone".** If it were, a bug in the selection
> screen that serialised an empty list would broadcast to the entire phonebook — the one
> mistake here that cannot be taken back. Omit the field to mean *all*; send ids to mean
> *these*.

Every id must belong to the phonebook named in `phonebook_id`. That keeps the mapping
guarantee intact: an agent can only reach contacts in books an administrator gave them.

### Stale ids

If any id is no longer in that phonebook — usually because an administrator edited the
book after the app last synced — the whole request is refused and the offending ids are
named:

```json
{
  "detail": {
    "code": "unknown_contacts",
    "message": "1 selected contact(s) are no longer in this phonebook. Refresh your phonebooks and try again.",
    "contact_ids": ["00000000-0000-0000-0000-000000000999"],
    "count": 1
  }
}
```

**Nothing is sent and no campaign is created.** The alternative — dropping the stale ones
and sending to the rest — would tell the agent they had messaged fifty people when they
had messaged forty-seven, with nothing on screen ever saying otherwise. Re-fetch
`GET /phonebooks` and rebuild the selection.

A malformed (non-UUID) id is reported the same way rather than as a server error.

### What selection does **not** change

- **Opt-out screening still runs.** Ticking a row is not consent, and someone can
  unsubscribe after your last sync. A selected contact who has opted out lands in
  `skipped`, not `sendable`.
- **Duplicates still collapse.** The same id twice, or two contacts sharing a number, is
  one recipient. `total` can be lower than `len(contact_ids)` — **show the server's
  counts, not your own.**
- **The cap still applies** — but to the selection rather than the book, which is the
  point. A 250-contact phonebook under a 100 cap is unusable in a whole-book send; select
  80 and it goes.

---

## Rendering — the `rendered` field

The server always renders the message itself, from the approved template, and that
rendering is what goes to WhatsApp.

Send `rendered` anyway if it is convenient — your composer preview — and the server
compares the two. **They will only differ if the template changed at Meta after your app
cached it.** When that happens:

- The customer receives **Meta's** current wording, not yours.
- The mismatch is logged for the administrators.
- The send is **not** rejected.

So `rendered` is a drift detector, not the source of truth. The field is optional and
the endpoint behaves identically without it.

The same caution applies to `param_count`: re-read `GET /templates` immediately before
sending rather than trusting a cached count. If it has changed you get a clear error
(below) instead of a silent rejection from WhatsApp.

---

## Limits

An agent may not broadcast without bound. An administrator sets a per-send ceiling —
100 by default — in **Broadcast Settings** in the web panel.

```json
{
  "detail": {
    "code": "recipient_cap_exceeded",
    "message": "This would message 250 people, above the 100 you may send to at once. Choose a smaller phonebook or ask your administrator to raise the limit.",
    "recipient_count": 250,
    "limit": 100
  }
}
```

`403`. Nothing is created — no half-built campaign is left behind. Use `recipient_count`
and `limit` to say exactly what happened.

**Check before sending**: `GET /phonebooks` returns `contact_count` per book, so the app
can warn while the agent is choosing rather than after they commit.

There may also be a rolling 24-hour message quota per agent (`daily_limit_reached`),
off by default.

---

## Errors

| HTTP | `code` | Meaning | What to do |
|---|---|---|---|
| `400` | `param_count_mismatch` | Wrong number of values (`expected`, `supplied`) | **Re-fetch templates**, rebuild the form, retry |
| `400` | `nothing_selected` | `contact_ids` was an empty array | Select at least one, or omit the field for the whole book |
| `400` | `unknown_contacts` | Selected ids not in this phonebook (`contact_ids`, `count`) | **Re-fetch phonebooks**, rebuild the selection, retry |
| `403` | `recipient_cap_exceeded` | List too large (`recipient_count`, `limit`) | Show the limit; suggest a smaller book |
| `403` | `daily_limit_reached` | Rolling 24h quota spent (`sent_today`, `limit`) | Show when it frees up |
| `404` | `not_found` | Phonebook or template not assigned to this agent | Refresh both lists |
| `409` | `nothing_to_send` | Every contact was screened out | "Everyone on this list has unsubscribed" |
| `409` | `no_whatsapp_account` | Company has no WhatsApp number | "Contact your administrator" |
| `401` | `invalid_session` | Token or device header missing/wrong | Clear token, show login |

Real responses:

```json
{"detail": {"code": "param_count_mismatch",
            "message": "This template needs 2 value(s) and 1 were supplied. Refresh your templates and try again.",
            "expected": 2, "supplied": 1}}
```

```json
{"detail": {"code": "not_found", "message": "That phonebook was not found."}}
```

### `404` also means "not yours"

A phonebook belonging to your company but **not assigned to you** returns `404`, not
`403`. That is deliberate: an agent has no business learning which lists exist that they
were not given. Treat it as "refresh your lists" — an administrator may have unassigned
it since your last sync.

### `nothing_to_send` is an answer, not a fault

Every contact in the book has unsubscribed or is unreachable. Nothing is created. Say so
plainly — it is information about the list, not an error in the app.

---

## Screening — what happens to `skipped`

Before anything is sent, every recipient is checked and some are excluded:

| Reason | Why |
|---|---|
| Unsubscribed | Compliance. Also protects the number's quality rating for every agent |
| US numbers | Not supported on this platform |
| Duplicates | The same number in one book twice is one recipient — WhatsApp bills unique recipients |

These become the `skipped` count. They are not failures and no message is attempted.

---

## Worked example

```bash
BASE=https://<host>/agent-api/v1
DEV=9774d56d682e549c
AUTH=(-H "Authorization: Bearer $TOKEN" -H "X-Device-Id: $DEV")

# 1. What may this agent send, and to whom?
curl -s $BASE/templates  "${AUTH[@]}" | jq '.[] | {id, name, param_count}'
curl -s $BASE/phonebooks "${AUTH[@]}" | jq '.[] | {id, name, contact_count}'

# 2a. Send to the whole phonebook — omit contact_ids
curl -s -X POST $BASE/broadcasts "${AUTH[@]}" -H 'Content-Type: application/json' -d '{
  "phonebook_id": "3f88c2a1-77de-4b30-9c11-0a4e6b2f9d55",
  "template_id":  "50cf9403-741f-48b8-8baf-cf8f89fec74d",
  "params": ["Abhay", "LED TVs"]
}'

# 2b. Or send to just the contacts the agent ticked
curl -s -X POST $BASE/broadcasts "${AUTH[@]}" -H 'Content-Type: application/json' -d '{
  "phonebook_id": "3f88c2a1-77de-4b30-9c11-0a4e6b2f9d55",
  "contact_ids": ["7932ad5b-a365-46e6-a939-30f43f083d02"],
  "template_id":  "50cf9403-741f-48b8-8baf-cf8f89fec74d",
  "params": ["Abhay", "LED TVs"]
}'

# 3. Watch it land, per contact
curl -s "$BASE/threads?include_silent=true" "${AUTH[@]}" \
  | jq '.threads[] | {mobile_no, last_message}'
```

---

## Client checklist

- [ ] Send **both** `Authorization` and `X-Device-Id`
- [ ] Re-read `GET /templates` immediately before sending; **never cache `param_count`**
- [ ] Warn from `contact_count` before the agent commits to a large list
- [ ] **Omit `contact_ids` for a whole-book send** — never send an empty array
- [ ] Handle `unknown_contacts` by re-syncing phonebooks, not by retrying the same ids
- [ ] Do not assume `len(contact_ids)` equals what gets sent — dedup and screening apply
- [ ] Show `sendable` and `skipped` separately in the confirmation
- [ ] Handle `404` as "refresh your lists", not as a bug
- [ ] Treat `nothing_to_send` as information, not a failure
- [ ] Do not treat `201` as delivery — it means queued
- [ ] Any `401` → clear token, show login, do not retry
- [ ] Parse leniently — fields will be added without a version bump

---

## Related

| Document | Covers |
|---|---|
| [`AGENT_API_TEMPLATES.md`](./AGENT_API_TEMPLATES.md) | Templates, `param_count`, building the compose form |
| [`AGENT_API_PHONEBOOKS.md`](./AGENT_API_PHONEBOOKS.md) | Phonebooks, `contact_count`, `opted_out` |
| [`AGENT_API_THREADS.md`](./AGENT_API_THREADS.md) | Watching a broadcast land, per contact |
| `app/agent/broadcasts.py` | The implementation |
