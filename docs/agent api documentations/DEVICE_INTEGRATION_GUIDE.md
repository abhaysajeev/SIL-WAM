# SIL-WAM Agent App — Device Integration Guide

**For:** the mobile (Android) team building the agent app.
**Covers:** every endpoint, every field, every error code, the order to call things in,
and the edge cases that will otherwise show up in QA.
**API version:** `/agent-api/v1` · **Document date:** 13 Nov 2026

> Everything here is built and tested on the server. Sections marked **🆕 New** are
> part of the release that adds media broadcasts, the reply inbox and push. Until that
> release is deployed to your environment, those endpoints return `404`. Ask the
> backend team which environment has them.

---

## Contents

1. [What the app does](#1-what-the-app-does)
2. [Ground rules: base URL, headers, formats, errors](#2-ground-rules)
3. [App lifecycle: the order of calls](#3-app-lifecycle)
4. [Sign in, session, sign out](#4-sign-in-session-sign-out)
5. [Limits 🆕](#5-limits-)
6. [Templates](#6-templates)
7. [Phonebooks: list, create, edit, import 🆕](#7-phonebooks)
8. [Media upload and download 🆕](#8-media-)
9. [Broadcasts (incl. media templates) 🆕](#9-broadcasts)
10. [Inbox and chat window](#10-inbox-and-chat-window)
11. [Replying to a customer 🆕](#11-replying-to-a-customer-)
12. [Staying in sync: push, live events, `rev` 🆕](#12-staying-in-sync-)
13. [Data reference: every object](#13-data-reference)
14. [Error code reference](#14-error-code-reference)
15. [Screen-by-screen recipes](#15-screen-by-screen-recipes)
16. [Edge cases and QA checklist](#16-edge-cases-and-qa-checklist)
17. [Not supported yet](#17-not-supported-yet)

---

## 1. What the app does

An **agent** belongs to one company. In the app they:

1. **Sign in.** The phone becomes bound to that agent.
2. **Pick contacts** from phonebooks an admin assigned, or build their own phonebooks
   (typed in, or imported from Excel/CSV).
3. **Send a broadcast**: one approved WhatsApp template to everyone in one or more
   phonebooks, optionally with an image, video or PDF.
4. **Get the replies.** When a customer answers, the reply appears **only** on the
   devices of agents who contacted that customer in the last 30 days.
5. **Reply back**:
   - free text or a file within 24 hours of the customer's last message
   - a template at any time
6. Repeat.

The server enforces every rule: who can see what, limits, opt-outs, the 24-hour window and
template status. The app shows state and should check limits early, but the server always
has the final say.

---

## 2. Ground rules

### Base URL

```
https://<host>/agent-api/v1
```

### Headers on every authenticated call

```
Authorization: Bearer <token from login>
X-Device-Id:   <the same device_id you logged in with>
```

**Send both, every time.** If `X-Device-Id` is missing or different, the response is `401`.
The token alone is not a session.

JSON bodies: `Content-Type: application/json`. File uploads: `multipart/form-data` with
the field name `file`.

### Formats

| Thing | Format | Example |
|---|---|---|
| IDs | UUID strings | `"3f88c2a1-77de-4b30-9c11-0a4e6b2f9d55"` |
| Timestamps | UTC, ISO-8601 | `"2026-11-13T09:41:07.123456Z"` |
| Phone numbers (from the server) | Digits only, country code included, **no `+`** | `"918075571893"` |
| Phone numbers (to the server) | Anything a person would type | `8075571893`, `+91 80755 71893`, `080755-71893` |

**Phone numbers:** the server converts whatever you send into the canonical form
(`918075571893`). A number with no country code is read in the company's **default
country** (`GET /limits` → `default_country`, e.g. `IN`), so `8075571893` becomes
`918075571893`. Always **display and match on the server's form**.

**Parse leniently.** New fields can appear in any response without a version bump.
Ignore fields you don't recognise; never fail on them.

### Errors: one shape everywhere

```json
{ "detail": { "code": "window_closed", "message": "You can only send a free message…", "last_inbound_at": "…" } }
```

- **Branch on `code`. Show `message`.** Messages are written for the person holding the
  phone.
- Some codes carry extra fields, listed with each endpoint. All codes are in
  [§14](#14-error-code-reference).
- A `422` without a `code` is a request-format problem caught before any logic ran (a
  wrong type or a missing field). Its `detail` is a **list**:
  `[{"loc": ["body","text"], "msg": "…", "type": "…"}]`. That's a bug in the app. Log it.

### The `401` rule

**Any `401` means the session is over.** Delete the stored token and go to the login
screen. There is no refresh token, so **never retry a `401`**.

### `server_time` and clocks

Most responses include `server_time`. Phone clocks drift, so whenever you compute a
countdown (for example "window closes in 2h 10m"), use the **server's** clock:

```
offset = server_time - device_now        // store whenever a response has server_time
server_now = device_now + offset
```

---

## 3. App lifecycle

```
App start
 ├─ token stored?  no ──────────────────────────────► Login screen
 └─ yes → GET /auth/session
          ├─ 401 → delete token → Login screen
          └─ 200 → Home
                   ├─ GET /limits             (ceilings, account status)
                   ├─ GET /templates          (cache; refresh on pull-to-refresh)
                   ├─ GET /phonebooks         (cache; refresh after any phonebook edit)
                   ├─ GET /threads            (inbox)
                   ├─ PUT /devices/push-token (FCM token; also in onNewToken)
                   └─ open GET /events        (while in the foreground)

On push / live event / foreground / reconnect:
   GET /threads/{id}/messages?changed_after_rev=<highest rev held for that thread>
   and refresh GET /threads
```

---

## 4. Sign in, session, sign out

### 4.1 `POST /auth/login`

No auth headers.

```json
{
  "username":    "ravi.k",
  "password":    "secret",
  "customer_id": "FM-2291",
  "device_id":   "9774d56d682e549c",
  "app_version": "1.0.3",
  "os_version":  "Android 14"
}
```

| Field | Required | Notes |
|---|---|---|
| `username` | yes | 1–100 chars |
| `password` | yes | 1–200 chars |
| `customer_id` | yes | Licence key; must match what the admin set for this agent |
| `device_id` | yes | Use `Settings.Secure.ANDROID_ID`. Keep it stable for the install, and send the same value in `X-Device-Id` on every later call |
| `app_version`, `os_version` | no | Shown to admins; please send them |

**`200 OK`**

```json
{
  "token": "vzGBuM1cNdw2BiUV…",
  "expires_at": null,
  "server_time": "2026-11-13T09:41:07Z",
  "agent": {
    "user_id": "db3bb70e-…", "username": "ravi.k", "full_name": "Ravi Kumar",
    "agent_id": "AG01", "company_id": "9a2b1c3d-…", "company_name": "Softland India Ltd"
  },
  "device": { "device_id": "9774d56d682e549c", "bound_at": "…", "newly_bound": true }
}
```

- Store `token` in encrypted storage.
- `expires_at` is always `null`: a session lasts until it's revoked, or until it goes
  unused for 90 days.
- `newly_bound: true` happens once, the first time this phone is claimed. Show a one-off
  confirmation.
- **Device binding:** one agent maps to one phone. Logging in from another phone gives
  `device_mismatch` until an admin releases the old one.

| Error `code` | HTTP | Show |
|---|---|---|
| `invalid_credentials` | 401 | "Username or password is incorrect" (never say which) |
| `account_locked` | 403 | Countdown from `retry_after_minutes` |
| `account_disabled` / `not_an_agent` | 403 | "Contact your administrator" |
| `licence_mismatch` | 403 | Check the customer ID |
| `device_mismatch` | 403 | "This account is registered to another phone. Ask your administrator to release it" (`registered_device_id`) |
| `device_already_registered` | 403 | "This phone belongs to another agent" |

### 4.2 `GET /auth/session`

Use it on cold start to check the stored token. Same response shape as login, but with
`token: null`. A `401` means sign out.

### 4.3 `POST /auth/logout`

Returns **`204`**. This signs out this session only: the phone stays bound, and pushes
to it stop.

---

## 5. Limits 🆕

### `GET /limits`

Call it on startup and before showing the broadcast confirmation screen.

```json
{
  "max_recipients_per_send": 100,
  "daily_message_limit": 0,
  "sent_last_24h": 12,
  "max_phonebooks_per_broadcast": 20,
  "max_contacts_per_phonebook": 2000,
  "import_max_file_bytes": 5242880,
  "import_formats": ["xlsx", "csv"],
  "media": [
    {"format": "IMAGE",    "mimes": ["image/jpeg", "image/png"], "max_bytes": 5242880},
    {"format": "DOCUMENT", "mimes": ["application/pdf"],          "max_bytes": 104857600},
    {"format": "VIDEO",    "mimes": ["video/mp4"],                "max_bytes": 16777216}
  ],
  "default_country": "IN",
  "account": { "state": "ok", "reason": null, "messaging_limit_tier": "TIER_1K" },
  "server_time": "2026-11-13T09:41:07Z"
}
```

| Field | Use it for |
|---|---|
| `max_recipients_per_send` | The broadcast confirmation: "This will reach 240 people; your limit is 100". `0` = no limit |
| `daily_message_limit`, `sent_last_24h` | Your own replies in a rolling 24 hours. `0` = no limit. Broadcasts don't count here |
| `max_phonebooks_per_broadcast` | The phonebook multi-select cap |
| `max_contacts_per_phonebook` | The size cap for phonebooks the agent creates or imports into |
| `import_max_file_bytes`, `import_formats` | The file picker for imports |
| `media` | Check a file before uploading it |
| `default_country` | Hint text such as "numbers without a country code are read as India (+91)" |
| `account.state` | `ok`, `warning` (a problem was detected; sending still works), `blocked` (sending is refused, so show `reason`), or `not_connected` |

An admin can change these values at any time. Re-read them; don't hard-code.

---

## 6. Templates

### `GET /templates`

Returns the **approved** templates this agent is allowed to use, sorted by name. A template
that WhatsApp pauses or disables disappears from this list.

```json
[
  {
    "id": "50cf9403-741f-48b8-8baf-cf8f89fec74d",
    "name": "diwali_offer",
    "language": "en_US",
    "category": "MARKETING",
    "body_text": "Hi {{1}}, our Diwali offer on {{2}} is live!",
    "param_count": 2,
    "parameters": [
      {"index": 1, "label": "Param 1", "example": "Ravi"},
      {"index": 2, "label": "Param 2", "example": "LED TVs"}
    ],
    "header": {"format": "IMAGE", "text": null},
    "footer_text": "Reply STOP to unsubscribe",
    "buttons": [{"type": "QUICK_REPLY", "text": "Interested"}]
  }
]
```

How to use it:
- **`param_count`**: show exactly this many inputs, filling `{{1}}`, `{{2}}`, … in order.
  Use `example` as the placeholder text.
- **`header.format`**:

  | Value | What the app does |
  |---|---|
  | `null` | No header |
  | `TEXT` | Show `header.text`. Nothing to supply |
  | `IMAGE`, `VIDEO`, `DOCUMENT` | **A file is required.** Upload it with `POST /media` and send its `media_id` ([§8](#8-media-)) |

- **Preview:** substitute the values into `body_text` locally. The server renders its own
  copy and that is what the customer receives.
- **Value rules (the server rejects them otherwise):**
  - no empty values
  - no line breaks or tabs
  - no more than 4 spaces in a row
- Fetch the list again just before sending. An admin may have changed it, or WhatsApp may
  have paused a template.

---

## 7. Phonebooks

### 7.1 `GET /phonebooks`

Every phonebook this agent can use, **with all contacts nested**, in one call. Cache it
for offline browsing and search.

```json
[
  {
    "id": "3f88c2a1-…",
    "name": "Kerala retail",
    "editable": false,
    "contact_count": 2,
    "updated_at": "2026-11-01T09:14:22Z",
    "contacts": [
      {"id": "70a7a280-…", "mobile_no": "917025985366", "customer_name": "Abhay",
       "email": null, "agent_id": null, "opted_out": true},
      {"id": "88429605-…", "mobile_no": "918075571893", "customer_name": "Ravi",
       "email": "ravi@example.com", "agent_id": "AG01", "opted_out": false}
    ]
  }
]
```

| Field | Meaning |
|---|---|
| `editable` | `true` = **this agent created it in the app**, so they can rename it and add, edit or remove contacts. `false` = an admin assigned it, so it's read-only |
| `contacts[].opted_out` | **`true` = this person unsubscribed.** Grey them out. They're skipped from broadcasts automatically, and replies to them are refused |
| `contacts[].agent_id` | A label imported with the contact. **It isn't necessarily you**, and nothing routes on it. Show it or ignore it |

### 7.2 `POST /phonebooks`: create a phonebook

```json
{ "name": "My shop customers",
  "contacts": [ {"mobile_no": "8075571893", "customer_name": "Ravi", "email": null} ] }
```

- `name` is required, at most 150 characters, and unique within the company.
- `contacts` is optional, with at most **500 per call**. Use `/contacts` or `/import` to
  add more.
- The new phonebook is assigned to this agent and comes back with `editable: true`.

**`201`** returns the phonebook object plus `skipped`: `[{"row": 1, "value": "12", "reason": "…"}]`.
`row` is the 1-based position in your `contacts` array.

### 7.3 `PATCH /phonebooks/{id}`: rename

Send `{"name": "New name"}`. Returns the phonebook object. Only works on editable
phonebooks.

### 7.4 `POST /phonebooks/{id}/contacts`: add contacts

Send `{"contacts": [ {...}, … ]}` with 1–500 contacts.

**`201`** returns `{"added": 148, "skipped": [{"row": 7, "value": "…", "reason": "Already in this phonebook"}]}`.
Bad rows are skipped and the rest are added.

### 7.5 `PATCH /phonebooks/{id}/contacts/{contact_id}`: edit a contact

Send any of `{"mobile_no", "customer_name", "email"}`. Returns the saved contact, with the
number in canonical form.

### 7.6 `DELETE /phonebooks/{id}/contacts/{contact_id}`

Returns **`204`**.

### 7.7 Excel / CSV import 🆕

**Step 1: give the agent the sample.**
`GET /phonebooks/sample?format=xlsx` (or `format=csv`) returns the file
(`Content-Disposition: attachment`). Save it, then open or share it.

- The headings are `WhatsApp Number`, `Customer Name` and `Email`.
- In the `.xlsx`, the number column is pre-formatted as **Text**, so Excel can't round
  long numbers to `9.18E+11`.
- An *Instructions* sheet explains each column.

**Step 2: upload the filled file.**

```
POST /phonebooks/{id}/import
Content-Type: multipart/form-data
file=<the .xlsx or .csv>
```

- This only works on an **editable** phonebook. The usual flow is to create an empty
  phonebook (§7.2), then import into it.
- The file can be at most 5 MB. Only the first sheet is read, and row 1 must be the
  headings.
- Columns are matched by heading. The sample's headings always match, and so do common
  variants like "Mobile", "Phone" and "Name".

**`201`**

```json
{
  "added": 148,
  "skipped": [
    {"row": 7,  "value": "9.18E+11", "reason": "'9.18E+11' was changed by Excel into a rounded number — format the WhatsApp Number column as Text and type the number again"},
    {"row": 12, "value": "918075571893", "reason": "Already in this phonebook"}
  ],
  "total_in_book": 148,
  "columns": {"mobile_no": "WhatsApp Number", "customer_name": "Customer Name", "email": "Email"}
}
```

- `row` is the **spreadsheet row**, so show it as "Row 7: …" and the agent can find it.
- Afterwards, call `GET /phonebooks` again to pick up the contacts.

Whole-file errors (nothing is added):

| `code` | HTTP | Meaning |
|---|---|---|
| `file_unreadable` | 400 | The file is damaged |
| `unsupported_file_type` | 415 | An old `.xls` or not a spreadsheet: "Save as .xlsx or .csv" |
| `file_too_large` | 413 | Over 5 MB |
| `empty_file` | 400 | No rows |
| `missing_number_column` | 400 | No WhatsApp number column was found |
| `phonebook_full` | 422 | Would exceed `max_contacts_per_phonebook` (`contact_count`, `adding`, `limit`) |
| `phonebook_read_only` | 403 | Not an editable phonebook |

### 7.8 Phonebook errors

| `code` | HTTP | When |
|---|---|---|
| `not_found` | 404 | Unknown phonebook or contact, or not yours |
| `phonebook_read_only` | 403 | Changing a phonebook an admin assigned |
| `phonebook_name_taken` | 409 | That name already exists in the company |
| `phonebook_full` | 422 | Over the size limit |
| `contact_exists` | 409 | An edit would duplicate a number already in the phonebook |
| `invalid_contact` | 422 | Bad number, missing name, and so on (`message` says what) |

---

## 8. Media 🆕

Use this for a broadcast's header file, or for a file sent in a reply.

### 8.1 `POST /media`: upload

```
POST /media
Content-Type: multipart/form-data
file=<image/video/pdf>
```

**`201`**

```json
{
  "media_id": "0b9d6e1c-5a4f-4a43-9b0e-2f1d0c7b8a11",
  "format": "IMAGE",
  "mime": "image/jpeg",
  "size_bytes": 184233,
  "filename": "diwali.jpg",
  "expires_at": "2026-12-13T09:41:07Z"
}
```

- Upload **as soon as the agent picks the file**, so a bad file is refused before they
  fill in the rest of the form.
- One `media_id` can be reused across any number of broadcasts and replies until
  `expires_at` (30 days).
- It's uploaded to WhatsApp **once** here, and that one upload is sent to every recipient.
- **The type is read from the file's contents**, not its name, so a renamed file is
  refused.

| Format | Types | Max |
|---|---|---|
| `IMAGE` | JPEG, PNG | 5 MB |
| `VIDEO` | MP4 | 16 MB |
| `DOCUMENT` | PDF | 100 MB |

Tip: compress photos on the device (to about 1600 px, JPEG quality 80) before uploading.
It's faster and keeps you under 5 MB.

| `code` | HTTP | Meaning |
|---|---|---|
| `unsupported_media_type` | 415 | Not JPEG, PNG, MP4 or PDF |
| `media_too_large` | 413 | Over the limit (`max_bytes`) |
| `file_unreadable` | 400 | Empty file |
| `media_upload_failed` | 502 | WhatsApp refused the file. Show `message` |
| `no_whatsapp_account` | 409 | The company has no WhatsApp number connected |

### 8.2 `GET /media/{media_id}`: download

This returns the **file bytes** (not JSON), with the right `Content-Type`.
- Images and video are `inline`; PDFs are `attachment`.
- You can download files you uploaded, and files shown in a chat you can open.
- The chat message's `media.asset_id` is the id to use here.
- Cache downloads on the device (`Cache-Control: private`).

| HTTP | `code` | Show |
|---|---|---|
| 404 | `media_not_found` | Not found, or not visible to you |
| 410 | `media_expired` | "This file is no longer available" (files are deleted after 30 days) |

---

## 9. Broadcasts

### `POST /broadcasts`

```json
{
  "phonebook_ids": ["3f88c2a1-…", "a1b2c3d4-…"],
  "contact_ids":   ["7932ad5b-…", "5d616872-…"],
  "template_id":   "50cf9403-…",
  "params":        ["Ravi", "LED TVs"],
  "header_media":  { "media_id": "0b9d6e1c-…" },
  "rendered":      "Hi Ravi, our Diwali offer on LED TVs is live!",
  "name":          "Diwali — Kerala"
}
```

| Field | Required | Notes |
|---|---|---|
| `phonebook_ids` | yes | 1–20 of **this agent's** phonebooks. A number that appears in several of them, in any spelling, is messaged **once** |
| `phonebook_id` | — | Old single-phonebook field, still accepted. Use `phonebook_ids` |
| `contact_ids` | no | **Omit the field to send to everyone** in the chosen phonebooks. Send only the ids the agent ticked otherwise. **Never send `[]`**: it's refused (`nothing_selected`) so that an empty selection can't become "everyone" |
| `template_id` | yes | From `GET /templates` |
| `params` | yes | Exactly `param_count` values, the same for every recipient. Send `[]` for a template with no values |
| `header_media` | when needed | **Required** if `header.format` is IMAGE, VIDEO or DOCUMENT; **refused** otherwise |
| `rendered` | no | Your preview text. The server logs any mismatch with its own rendering |
| `name` | no | Shown in the web panel. Defaults to "first phonebook +N — template" |

**`201`**

```json
{ "campaign_id": "9fc97936-…", "name": "Kerala retail +1 — diwali_offer",
  "total": 240, "skipped": 3, "sendable": 237, "status": "sending",
  "server_time": "…" }
```

- `total`: unique recipients after duplicates across phonebooks are removed.
- `skipped`: opted-out recipients and others that won't be sent.
- `sendable`: queued now.

Sending happens in the background, and the request returns straight away. Each recipient's
message then shows up in that customer's chat (`kind: "broadcast"`), and the ticks update
through sync.

**Before calling:** show a confirmation with the recipient count and the
`max_recipients_per_send` limit from `/limits`.

| `code` | HTTP | Meaning |
|---|---|---|
| `not_found` | 404 | A phonebook or template isn't one of yours. **Nothing is sent** if any one of them fails |
| `too_many_phonebooks` | 400 | More than 20 phonebooks |
| `nothing_selected` | 400 | `contact_ids: []` |
| `unknown_contacts` | 400 | Some `contact_ids` are no longer in those phonebooks (`contact_ids`, `count`). Re-fetch phonebooks |
| `param_count_mismatch` | 400 | `expected`, `supplied`. Re-fetch templates |
| `header_media_required` | 400 | The template needs a file (`format`) |
| `header_media_not_allowed` | 400 | The template takes no file |
| `header_media_mismatch` | 400 | The wrong kind of file (`expected`, `supplied`) |
| `media_not_found` / `media_expired` | 404 / 410 | Upload again |
| `recipient_cap_exceeded` | 403 | `recipient_count` is over `limit` |
| `nothing_to_send` | 409 | Everyone has opted out |
| `no_whatsapp_account` | 409 | The company has no WhatsApp number connected |
| `account_unavailable` | 503 | The company's WhatsApp account has a problem (`reason`, e.g. billing) |
| `build_failed` | 400 | Show `message` |

---

## 10. Inbox and chat window

### Who sees a conversation

Only agents who **contacted the customer** (a broadcast or a reply from the app) in the
**last 30 days**. Teammates who both contacted the same customer both see the chat and
each other's replies. Everyone else gets **`404`** for that chat on every endpoint. If a
customer replies after 30 days, the reply appears in the admin web panel, not in the app.
Contacting them again brings the chat back.

### 10.1 `GET /threads`: inbox list

| Query | Default | Notes |
|---|---|---|
| `limit` | 30 | 1–100 |
| `before` | — | Pass the previous page's `next_cursor` |
| `include_silent` | `false` | `true` also lists customers who never replied |

```json
{
  "threads": [
    {
      "id": "b4760a72-…",
      "mobile_no": "918075571893",
      "contact_name": "Ravi",
      "last_message_at": "2026-11-13T09:30:00Z",
      "message_seq": 42,
      "rev": 57,
      "last_inbound_at": "2026-11-13T09:30:00Z",
      "window_expires_at": "2026-11-14T09:30:00Z",
      "window_state": "open",
      "opted_out": false,
      "last_message": "Is it available in 55 inch?"
    }
  ],
  "next_cursor": null,
  "server_time": "…"
}
```

- The list is sorted by `last_message_at`, newest first. Stop paging when `next_cursor`
  is `null`.
- **`rev` larger than the value you hold** means that thread has changes. Sync it ([§12](#12-staying-in-sync-)).
- `mobile_no` can be `null` for a customer who uses a WhatsApp username. See [§17](#17-not-supported-yet).

### 10.2 `GET /threads/{thread_id}/messages`: the chat

| Query | Use |
|---|---|
| *(none)* | Open the chat: the newest `limit` messages, newest first |
| `before_seq=N` | Scroll up: the `limit` messages just older than position N. Stop when `has_more` is false |
| `changed_after_rev=N` | **Sync**: everything new **or changed** since change N, oldest change first. If `has_more` is true, call again with the highest `rev` returned |
| `after_seq=N` | Older sync that returns only new messages, never status changes. Use `changed_after_rev` instead |
| `limit` | 1–100 (default 30) |

Response: `{ "thread": {…}, "messages": [ Message, … ], "has_more": bool, "server_time": "…" }`
(see [Message](#message)).

**How to apply a sync result:** for each message, if you already hold its `id`, **replace**
it (the status, reactions or media changed). Otherwise **insert** it at its `seq`. Then
store `max(rev)` as the thread's synced rev.

**Order the chat by `seq`, never by time.**

### 10.3 `GET /threads/by-mobile/{mobile_no}`: open a contact's chat

Use this when the agent taps a contact in a phonebook. Any spelling of the number works.
It returns the same response as §10.2. It returns `404 not_found` if you've never
contacted them, or not within 30 days.

### 10.4 The 24-hour window

WhatsApp allows **free text or files** only within 24 hours of the **customer's** last
message. Outside that, only **templates** can be sent. Sending a template **doesn't reopen**
the window; only the customer writing does.

| `window_state` | Composer |
|---|---|
| `open` | Text, files and templates |
| `closing_soon` (less than 1 hour left) | Same, with a countdown to `window_expires_at` (use [server time](#server_time-and-clocks)) |
| `closed` | **Templates only.** Hide the text box and show "Send a template" |

The server checks the window again at send time and is the only authority.

---

## 11. Replying to a customer 🆕

### `POST /threads/{thread_id}/messages`

Send **exactly one** of `text`, `media` or `template`:

```json
{ "client_msg_id": "1f6c2c3e-8a0e-4d0e-9a51-6a3d2b7f9c10",
  "last_seen_rev": 117,
  "text": "Yes, the 55 inch is in stock." }
```

```json
{ "client_msg_id": "…", "last_seen_rev": 117,
  "media": { "media_id": "<from POST /media>", "caption": "Price list" } }
```

```json
{ "client_msg_id": "…", "last_seen_rev": 117,
  "template": { "template_id": "…", "params": ["Ravi", "LED TVs"],
                "header_media_id": "<only for IMAGE/VIDEO/DOCUMENT templates>" } }
```

| Field | Required | Notes |
|---|---|---|
| `client_msg_id` | **yes** | A **new UUID per message**, created when Send is tapped and **saved with the draft**. Reuse it on every retry of that message |
| `last_seen_rev` | recommended | The highest `rev` on screen. Lets the server warn you that a teammate just replied |
| `force` | no | `true` sends even though a teammate replied |
| `text` | one of | 1–4096 chars. Window must be open |
| `media` | one of | Window must be open. `caption` ≤ 1024 |
| `template` | one of | **Works whether or not the window is open** |

**`202`**

```json
{
  "message": { "id": "…", "seq": 43, "rev": 120, "direction": "outbound",
               "kind": "agent_reply", "body": "Yes, the 55 inch is in stock.",
               "status": "sent", "failure_category": null, "wamid": "wamid.HBg…",
               "sent_by_agent_id": "…", "created_at": "…", "…": "…" },
  "thread_rev": 120,
  "replayed": false,
  "server_time": "…"
}
```

**When WhatsApp refuses the message**, you still get `202`, with `status: "failed"` and a
`failure_category`. The message exists as a red bubble in the chat; it isn't an error
response. See [failure categories](#failure_category).

### Retries (important)

**Optimistic UI flow:**
1. Show the bubble immediately as *queued*.
2. POST the message.
3. On a timeout or network error, POST again with **the same body and the same
   `client_msg_id`**.

If the first attempt already reached the server, the response is **`200`** with
`"replayed": true` and the original message. **The customer isn't messaged twice.**

If you reuse the same id with a different body, you get `409 idempotency_mismatch`. That's
an app bug: create a new UUID for each new message.

### Teammate collision

When two agents share a customer, both could answer at once. Send `last_seen_rev`. If a
teammate replied after it, you get:

```json
{ "detail": { "code": "teammate_replied",
              "message": "A teammate has just replied to this customer…",
              "messages": [ { …Message: the teammate's reply… } ] } }
```

Insert those messages, show them, and let the agent decide. To send anyway, repeat the
request with `"force": true` (same `client_msg_id`).

### Reply errors

| `code` | HTTP | What to do |
|---|---|---|
| `not_found` | 404 | Not your conversation. Remove it from the list |
| `window_closed` | 409 | Switch the composer to templates (`last_inbound_at`) |
| `teammate_replied` | 409 | See above |
| `idempotency_mismatch` | 409 | App bug |
| `recipient_opted_out` | 403 | Disable the composer: "This customer unsubscribed" |
| `template_unavailable` | 409 | WhatsApp paused or disabled it. Re-fetch templates (`template_status`) |
| `param_count_mismatch` | 400 | `expected`, `supplied` |
| `invalid_param` | 400 | A value is empty or has a line break, tab or 5+ spaces (`index`) |
| `header_media_required` / `header_media_mismatch` / `header_media_not_allowed` | 400 | As for broadcasts |
| `media_not_found` / `media_expired` | 404 / 410 | Upload again |
| `daily_limit_reached` | 403 | `sent_today`, `limit` |
| `no_whatsapp_account` | 409 | Contact your administrator |
| `account_unavailable` | 503 | Contact your administrator (`reason`) |
| `no_phone_number` | 409 | Customer known only by username. See §17 |

---

## 12. Staying in sync 🆕

### The one rule

> **Pushes and live events only say "thread X changed". The content always comes from
> `GET /threads/{id}/messages?changed_after_rev=<what you hold>`.**

So a lost push only delays things, and never loses a message.

### What `seq` and `rev` mean

| | `seq` | `rev` |
|---|---|---|
| What it is | The message's **position** in the chat (1, 2, 3, …) | The thread's **change counter** |
| When it increases | A new message | **Any** change: a new message, ✓ → ✓✓ → blue ticks, a reaction, an attachment becoming ready, a failure |
| Use it for | Sorting and scrolling up (`before_seq`) | Syncing (`changed_after_rev`) |

Both have **no gaps** within a thread, so "everything after N" never misses anything.

### Sync algorithm (per thread)

```
held = localDb.maxRev(threadId)            // 0 if never synced
loop:
    page = GET /threads/{threadId}/messages?changed_after_rev={held}&limit=100
    for m in page.messages:
        if localDb.has(m.id): localDb.replace(m) else: localDb.insert(m)
    held = max(held, max(m.rev for m in page.messages))
    localDb.setMaxRev(threadId, held)
    if not page.has_more: break
```

Run it on:
- app start
- return to the foreground
- every push
- every live event
- network reconnect
- opening a chat

### 12.1 Push notifications (Android, FCM)

**Register:** `PUT /devices/push-token` with `{"token": "<FCM token>", "platform": "android"}`.
Call it after login and in `FirebaseMessagingService.onNewToken`. Response:
`{"registered": true, "server_time": "…"}`.
**Unregister:** `DELETE /devices/push-token`, for example when the agent turns notifications
off. Logging out also stops pushes.

**What arrives:** a **data-only** message. It never contains the customer's text.

```json
{ "type": "sync", "thread_id": "b4760a72-…", "rev": "57", "new_message": "1" }
```

All values are strings, as FCM delivers them.

| `new_message` | Priority | The app must |
|---|---|---|
| `"1"` | high | Sync the thread, **then show a notification** with the new message (contact name and preview). Android demotes high-priority pushes that don't show a notification, so always show one |
| `"0"` | normal, collapsed per thread | Status-only change. Sync silently; no notification |

Pushes are sent **only to agents who contacted the customer** (§10), and are batched: a
burst of changes in one thread becomes one push.

### 12.2 Live events while the app is open: `GET /events`

A Server-Sent Events stream using the usual two headers. Open it when the app comes to
the foreground and close it when it goes to the background, where push takes over.

```
retry: 5000

event: sync
data: {"type":"sync","thread_id":"b4760a72-…","rev":57,"new_message":true}

: ping
```

- `: ping` arrives every 25 s. Ignore it; it keeps the connection open.
- On disconnect, reconnect with backoff, then **sync**: missed events aren't replayed.
- On Android, use OkHttp's `EventSource` (okhttp-sse), or read the stream line by line.

### 12.3 Customer attachments

When a customer sends a photo or PDF, the message first arrives with `media` like
`{"type": "image", "media_id": "<meta id>", "mime_type": "image/jpeg"}`, and no
`asset_id` yet. Show a placeholder. A few seconds later the server has copied the file,
sets `media.asset_id`, and increases that message's `rev`. Your next sync gets it, and you
download it with `GET /media/{asset_id}`.

If the copy fails repeatedly, `media.fetch_error` is set. Show "Attachment unavailable".

### 12.4 Reactions

A customer's reaction **doesn't add a message**. It updates the message it reacts to:
`reactions: {"👍": ["customer"]}` and a new `rev`. An empty reaction removes it.

---

## 13. Data reference

### Message

| Field | Type | Meaning |
|---|---|---|
| `id` | uuid | Stable. The key for replacing a message on sync |
| `seq` | int | Position in the chat. Sort by this |
| `rev` | int | Change number of this message's latest change |
| `direction` | string | `inbound` (from the customer) or `outbound` |
| `kind` | string | `reply` (customer message), `broadcast`, `agent_reply` (text or file from an agent), `template` (a template sent by an agent), `transactional` (sent by the company's systems) |
| `body` | string or null | The text the customer sees or wrote. Templates are already rendered. A caption for files. A placeholder for types the app can't show, e.g. `"Unsupported message (poll) — view it on WhatsApp"` or `"📍 Location: Lulu Mall"` |
| `template` | object or null | `{template_id, name, language, params}` for a template sent by an agent; `{name, language, params}` for broadcasts |
| `media` | object or null | See below |
| `status` | string | See below |
| `error_message` | string or null | Why it failed |
| `failure_category` | string or null | See below |
| `wamid` | string or null | WhatsApp's id. Ignore it in the UI |
| `replies_to` | string or null | The `wamid` of the message the customer quoted. Find it with `wamid` and show a quote bubble |
| `sent_by_agent_id` | uuid or null | The agent who sent it (`null` for the customer and for web-panel sends). **Label teammates' messages** with this; your own id is in the session (`agent`) |
| `reactions` | object or null | `{"❤️": ["customer"]}` |
| `created_at`, `delivered_at`, `read_at` | datetime or null | Timestamps |

### `media` on a message

| Key | Meaning |
|---|---|
| `type` / `format` | `image`, `video`, `document`, `audio` or `sticker` |
| `asset_id` | **Download with `GET /media/{asset_id}`.** Missing on a customer attachment until it has been copied (§12.3) |
| `filename`, `mime_type`, `caption`, `size_bytes` | When known |
| `media_id` | WhatsApp's id. Not usable by the app |
| `fetch_error` | The attachment couldn't be retrieved |

### `status`

| Value | Bubble |
|---|---|
| `queued` | 🕓 not handed over yet |
| `sending` | 🕓 being handed over |
| `sent` | ✓ WhatsApp accepted it (not yet on the customer's phone) |
| `delivered` | ✓✓ on the customer's phone |
| `read` | ✓✓ blue |
| `failed` | ❗ plus the text for its `failure_category` |
| `unconfirmed` | ⚠ "May not have been delivered": the connection dropped mid-send. It's never re-sent automatically. Let the agent resend it as a **new** message if they want |
| `received` | A customer's message (inbound) |

Statuses only move forward. A late "delivered" never overwrites "read".

### `failure_category`

| Value | Show the agent |
|---|---|
| `blocked` | "This customer can't receive messages from you" (not on WhatsApp, blocked you, or stopped marketing messages) |
| `outside_window` | "More than 24 h since they wrote. Send a template" |
| `billing` | "Your company's WhatsApp account has a billing problem. Contact your administrator" |
| `rate_limited` | "Sending too fast. Try again shortly" |
| `template` | "The template was rejected" |
| `other` | `error_message` |

---

## 14. Error code reference

| `code` | HTTP | Where | Meaning |
|---|---|---|---|
| `invalid_credentials` | 401 | login | Wrong username or password |
| `invalid_session` | 401 | any | **Sign out.** Never retry |
| `account_locked` | 403 | login | `retry_after_minutes` |
| `account_disabled` | 403 | login | User switched off |
| `not_an_agent` | 403 | any | No agent access |
| `licence_mismatch` | 403 | login | Wrong `customer_id` |
| `device_mismatch` | 403 | login | Bound to another phone (`registered_device_id`) |
| `device_already_registered` | 403 | login | This phone belongs to another agent |
| `no_company` | 403 | phonebooks | Agent has no company |
| `not_found` | 404 | many | Not found **for this agent**, including chats you can't see |
| `window_closed` | 409 | replies | Outside 24 h (`last_inbound_at`) |
| `recipient_opted_out` | 403 | replies | Unsubscribed |
| `recipient_cap_exceeded` | 403 | broadcasts | `recipient_count`, `limit` |
| `daily_limit_reached` | 403 | replies | `sent_today`, `limit` |
| `param_count_mismatch` | 400 | broadcasts, replies | `expected`, `supplied` |
| `invalid_param` | 400 | replies | A bad template value (`index`) |
| `nothing_selected` | 400 | broadcasts | `contact_ids: []` |
| `unknown_contacts` | 400 | broadcasts | `contact_ids`, `count` |
| `too_many_phonebooks` | 400 | broadcasts | `limit` |
| `nothing_to_send` | 409 | broadcasts | Everyone opted out |
| `build_failed` | 400 | broadcasts | Show `message` |
| `no_whatsapp_account` | 409 | sends, media | No WhatsApp number connected |
| `account_unavailable` | 503 | sends | Account problem (`reason`) |
| `send_failed` | 502 | sends | Couldn't be handed over. Retry later |
| `teammate_replied` | 409 | replies | `messages` |
| `idempotency_mismatch` | 409 | replies | Key reused for a different message |
| `template_unavailable` | 409 | replies | Paused or disabled (`template_status`) |
| `no_phone_number` | 409 | replies | Username-only customer |
| `header_media_required` | 400 | broadcasts, replies | `format` |
| `header_media_not_allowed` | 400 | broadcasts, replies | — |
| `header_media_mismatch` | 400 | broadcasts, replies | `expected`, `supplied` |
| `unsupported_media_type` | 415 | media | — |
| `media_too_large` | 413 | media | `max_bytes` |
| `media_upload_failed` | 502 | media | WhatsApp refused the file |
| `media_not_found` | 404 | media, sends | — |
| `media_expired` | 410 | media, sends | Older than 30 days |
| `file_unreadable` | 400 | media, import | Damaged or empty |
| `unsupported_file_type` | 415 | import | Not .xlsx or .csv |
| `file_too_large` | 413 | import | Over 5 MB |
| `empty_file` | 400 | import | No rows |
| `missing_number_column` | 400 | import | No number column |
| `phonebook_read_only` | 403 | phonebooks | Admin-assigned phonebook |
| `phonebook_name_taken` | 409 | phonebooks | Name exists |
| `phonebook_full` | 422 | phonebooks, import | `contact_count`, `adding`, `limit` |
| `contact_exists` | 409 | phonebooks | Duplicate number |
| `invalid_contact` | 422 | phonebooks | Show `message` |
| *(no code)* | 422 | any | Request-format error (FastAPI list). App bug |

---

## 15. Screen-by-screen recipes

### Login
1. `POST /auth/login`, then store `token`, `agent`, `device_id` and the server-time offset.
2. Then follow the [lifecycle](#3-app-lifecycle).

### Phonebooks screen
- Show the list from cached `GET /phonebooks`, with a lock icon on read-only phonebooks
  (`editable: false`).
- "New phonebook" → `POST /phonebooks` with `{name}` → "Add contacts" or "Import file".
- Import: "Download sample (Excel)" → `GET /phonebooks/sample?format=xlsx` and share it to
  Excel or Sheets. Then "Upload" → `POST /phonebooks/{id}/import` → show `added` and the
  `skipped` rows → refresh `GET /phonebooks`.

### New broadcast (wizard)
1. **Phonebooks:** multi-select up to `max_phonebooks_per_broadcast`, with optional
   per-contact ticks. Leave opted-out contacts unticked and greyed.
2. **Template:** pick from `GET /templates`, then fill in `param_count` values with a
   live preview.
3. **File** (only if `header.format` is IMAGE, VIDEO or DOCUMENT): pick, compress, then
   `POST /media` straight away. Show a thumbnail; keep the `media_id`.
4. **Confirm:** show the recipient count (selected contacts, de-duplicated by
   `mobile_no`, minus opted-out ones) against `max_recipients_per_send`.
5. `POST /broadcasts`. Show `sendable` and `skipped`. On `unknown_contacts` or
   `param_count_mismatch`, re-fetch and retry.

### Inbox
- `GET /threads`, paging by `next_cursor`. Show a badge where `rev` is greater than the
  locally synced rev.
- Pull to refresh: `GET /threads` plus a sync of the changed threads.

### Chat
- Open it: sync from the stored rev. On the first open, call without parameters, then use
  `before_seq` to scroll up.
- Composer state from `window_state`: text and files when `open` or `closing_soon`,
  templates only when `closed`.
- Send: create the `client_msg_id`, insert a *queued* bubble locally, POST, then replace
  the bubble with the returned message. Retry with the same id.
- Teammate messages: label them with the sender (`sent_by_agent_id` is not you).

---

## 16. Edge cases and QA checklist

| Scenario | Expected behaviour |
|---|---|
| Airplane mode mid-send, then retry | One message reaches the customer; the retry returns `replayed: true` |
| Agent double-taps Send | Same `client_msg_id`, one message |
| Two agents reply at the same moment | The second gets `teammate_replied` and sees the first reply |
| Customer replies 25 h after the agent's last broadcast | `window_state: open` (the reply opens it). The agent can text back |
| Agent tries text 25 h after the customer's last message | `window_closed`. Offer templates |
| Template paused by WhatsApp while the agent filled it in | `template_unavailable`. Re-fetch the list |
| Customer number typed as `8075571893` in an import | Stored and shown as `918075571893` |
| Excel file with numbers shown as `9.18E+11` | Those rows are skipped with a reason; the sample prevents it |
| Same customer in two selected phonebooks | Messaged once (`total` counts them once) |
| Phone off for 3 days | On open, the sync catches up everything; no push needed |
| Push token rotated | `onNewToken` → `PUT /devices/push-token` |
| App uninstalled and reinstalled | Same `ANDROID_ID`. Log in again; the phone is still bound to the agent |
| Agent moves to a new phone | Admin releases the old device, the agent logs in on the new one, and the chats are still there |
| Agent deactivated | Next call returns `401`; their chats disappear for them |
| Customer sends a location, poll or other unsupported type | A bubble with placeholder text, never blank |
| Customer reacts 👍 | The reacted-to bubble shows 👍; no new bubble |
| Customer photo older than 30 days | `GET /media` returns `410`. Show "no longer available" |
| Company billing problem | `/limits` `account.state` is `warning` or `blocked`; sends return `account_unavailable`; failed bubbles say `billing` |
| Device clock wrong by 2 hours | Window countdowns are still correct (server-time offset) |

---

## 17. Not supported yet

| Item | Status |
|---|---|
| **Personalized broadcasts** (different values per recipient) from the app | Planned. Web panel only for now |
| A different image per recipient | Not planned for the app |
| **iOS** push | Not planned. Android (FCM) only |
| Replying to a customer known only by WhatsApp username (no number) | `no_phone_number`. Coming once Meta's username sending is enabled for us |
| Typing indicators, read receipts *from* the agent | Not available |
| Broadcast progress screen in the app | Use the per-customer ticks in each chat. A campaign summary endpoint can be added on request |

---

*Questions or something that behaves differently from this guide? Report the request
(method, path, body, and the full response including `detail.code`) to the backend team.*
