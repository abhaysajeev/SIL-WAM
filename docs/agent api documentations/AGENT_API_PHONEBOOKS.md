# Agent App API — Phonebooks

**Status:** built and running. Every example below is a real response, captured from the
running server on 2026-09-03.
**Endpoints:** `GET /agent-api/v1/phonebooks` (read), plus create / rename / contact
writes for books the agent creates — see [Creating and editing phonebooks](#creating-and-editing-phonebooks-from-the-app).
**Companion:** [`AGENT_API_AUTH.md`](./AGENT_API_AUTH.md) — how to obtain the token and
device header this endpoint requires.

---

## What this is for

The contacts an agent is allowed to work with. An administrator assigns phonebooks to the
agent in the web panel; this returns those books **with every contact nested**, in one
call, so the app can browse and search offline.

---

## Request

```
GET /agent-api/v1/phonebooks
Authorization: Bearer <session token>
X-Device-Id:   <the device this session was issued to>
```

No body, no query parameters. Both headers required, as on every authenticated call.

---

## Response — `200 OK`

```json
[
  {
    "id": "3f88c2a1-77de-4b30-9c11-0a4e6b2f9d55",
    "name": "Test",
    "editable": false,
    "contact_count": 31,
    "updated_at": "2026-09-01T09:14:22Z",
    "contacts": [
      {
        "id": "70a7a280-0b28-42cd-8bc1-139cb149061b",
        "mobile_no": "917025985366",
        "customer_name": "Abhay",
        "email": null,
        "agent_id": null,
        "opted_out": true
      },
      {
        "id": "88429605-2c00-4ac2-8c7a-83cfbdb05d4e",
        "mobile_no": "910000000001",
        "customer_name": "Customer 1",
        "email": "customer1@example.com",
        "agent_id": "AGENT-001",
        "opted_out": false
      }
    ]
  }
]
```

### Phonebook

| Field | Type | Notes |
|---|---|---|
| `id` | uuid | Stable — use it as the list key |
| `name` | string | As named in the web panel, or by the agent |
| `editable` | bool | `true` for a book **this agent created from the app** — they may rename it and add, edit or remove contacts. `false` for books an administrator assigned: read-only |
| `contact_count` | int | Always equals `contacts.length`; it is derived from the same rows |
| `updated_at` | datetime¦null | When the phonebook itself last changed |
| `contacts` | array | Every contact. **`[]` for an empty book — never omitted** |

### Contact

| Field | Type | Notes |
|---|---|---|
| `id` | uuid | Stable per contact |
| `mobile_no` | string | Digits only, country code, **no leading `+`** — e.g. `917025985366` |
| `customer_name` | string | |
| `email` | string¦null | Often null; phonebooks are imported for messaging, not email |
| `agent_id` | string¦null | **This contact's own agent — may not be you.** See below |
| `opted_out` | bool | **`true` means do not message this person.** See below |

Contacts are ordered by `customer_name`, then `mobile_no`.

---

## `opted_out` — the one field you must not ignore

`true` means this person has unsubscribed from this company: they texted STOP, tapped an
opt-out button, or an administrator marked them.

> ⚠️ **The app must not offer to message a contact with `opted_out: true`.** Messaging
> someone who unsubscribed is a compliance breach, and it drives down the WhatsApp
> quality rating for the whole business number — which degrades delivery for every other
> agent too.

**They are returned, not hidden, on purpose.** The agent can see the person exists and
why they cannot be reached, instead of wondering where a contact went and re-adding them.
Show them greyed out with the compose action disabled.

The flag is computed fresh on every request, so someone who opts out after your last sync
shows as blocked on your next one. It is also **company-scoped** — unsubscribing from one
business does not unsubscribe you from another — so never cache it against a bare phone
number across companies.

---

## `agent_id` on a contact — probably not you

Each contact carries the agent id it was imported with. **It may name a different agent
than the one logged in**, or be `null`.

This is deliberate: the endpoint returns the whole book, not a filtered slice. Treat it as
data about the contact, not as a permission. A real response commonly holds several:

```
distinct agent_ids in one book: ["AGENT-001", "AGENT-002", "AGENT-003", "AGENT-004", null]
```

It is not the same field as the `agent_id` in your login response — that one identifies
*you*. Comparing them is a future feature, not something to act on today.

---

## Size, and how often to call

Everything nested means one round trip and a set the app can search offline. The cost is
payload size:

| Contacts | Approximate payload |
|---|---|
| 31 (measured) | 5.6 KB |
| 500 | ~90 KB |
| 2,000 (design ceiling for one book) | ~350 KB |

**Sync on app start and on explicit refresh — not on every screen.** Cache the result
locally and re-read when the agent asks, or after a long gap.

---

## Behaviours to build around

**An empty array is normal.** It means no phonebooks have been assigned yet — not a
failure. Say *"No phonebooks have been assigned to you. Contact your administrator."*
It deliberately does not fall back to all of the company's books.

**An empty book still appears**, with `"contacts": []`. The agent should see the book
exists and is empty rather than have it vanish.

**Books can disappear.** Unmapping a phonebook, or deleting it, removes it from this
response. Handle a book you were showing being gone after a refresh.

---

## Errors

| HTTP | Body | Meaning |
|---|---|---|
| `200` | `[]` | No phonebooks assigned. **Not an error** |
| `401` | `{"detail":{"code":"invalid_session",…}}` | Token missing, unknown, revoked, expired, or `X-Device-Id` missing/wrong |

As everywhere else in this API, a `401` means one thing: **clear the stored token and
show the login screen.** Do not retry.

---

## Creating and editing phonebooks from the app

An agent can build their own phonebooks. Each one belongs to **the agent's company**
(taken from their account — any `company_id` sent is ignored), is **assigned to that agent
automatically**, and shows up for administrators in the web panel like any company
phonebook (who can assign it to other agents too).

The agent may change **only books they created** (`editable: true`). Books an administrator
assigned stay read-only, and **deleting a whole phonebook is not possible from the app** —
ask an administrator.

All calls take the same two headers as every other call (`Authorization`, `X-Device-Id`).

| Call | Body | Success |
|---|---|---|
| `POST /phonebooks` | `{"name": "...", "contacts": [ ... ]}` — `contacts` optional, up to 500 | `201`, the new book (same shape as one item of `GET /phonebooks`) plus `skipped` |
| `PATCH /phonebooks/{id}` | `{"name": "..."}` | `200`, the book |
| `POST /phonebooks/{id}/contacts` | `{"contacts": [ ... ]}` — 1 to 500 | `201`, `{"added": n, "skipped": [ ... ]}` |
| `PATCH /phonebooks/{id}/contacts/{contact_id}` | any of `mobile_no`, `customer_name`, `email` | `200`, the contact |
| `DELETE /phonebooks/{id}/contacts/{contact_id}` | — | `204` |

A contact row is `{"mobile_no": "971589177070", "customer_name": "Ravi", "email": null}`.
Numbers follow the same rule as everywhere else: **7–15 digits with the country code, no
spaces or dashes**; a leading `+` is accepted and removed. The contact's `agent_id` is set
to the agent who added it.

**Bad rows don't fail the request.** A row with an invalid number, a blank name, or a
number already in the book is left out and reported, and the good rows are saved:

```json
{
  "added": 2,
  "skipped": [
    {"row": 3, "value": "12", "reason": "'12' is not a valid WhatsApp number — use 7 to 15 digits including the country code, with no spaces or dashes"},
    {"row": 4, "value": "971589177070", "reason": "Already in this phonebook"}
  ]
}
```
`row` is the 1-based position in the `contacts` array you sent. Show these to the agent.

A book holds at most **2,000 contacts**. A number that has unsubscribed can be added, but it
still comes back with `opted_out: true` and still cannot be messaged.

### Errors for these calls

| HTTP | `code` | Meaning |
|---|---|---|
| `403` | `phonebook_read_only` | The book was assigned by an administrator; it can't be changed from the app |
| `403` | `no_company` | The agent's account has no company, so it can't create phonebooks |
| `404` | `not_found` | No such phonebook or contact **for this agent** (another agent's or company's book looks the same as a missing one) |
| `409` | `phonebook_name_taken` | The company already has a phonebook with that name |
| `409` | `contact_exists` | Editing a contact to a number already in the book |
| `422` | `invalid_contact` | An edit with an invalid number or a blank name; `message` says which |
| `422` | `phonebook_full` | Adding these would take the book over 2,000 contacts; includes `contact_count`, `adding`, `limit` |
| `422` | *(FastAPI validation)* | Missing/blank `name`, or more than 500 contacts in one call |

```bash
# create with two contacts
curl -s -X POST $BASE/phonebooks -H "Authorization: Bearer $TOKEN" \
  -H "X-Device-Id: 9774d56d682e549c" -H 'Content-Type: application/json' -d '{
  "name":"My shop customers",
  "contacts":[{"mobile_no":"971589177070","customer_name":"Ravi"},
              {"mobile_no":"971500000001","customer_name":"Anil"}]}'
```

---

## How an agent gets phonebooks

Done once by an administrator, not by the app:

1. Open the agent's user record → **Agent Config** tab.
2. Under **Phonebooks this agent can use**, pick from the dropdown — it lists only
   phonebooks belonging to that agent's own company, with contact counts.
3. Each selection appears as a chip with an **×** to remove it.
4. Save.

Saving stores the full set. Different agents in the same company can hold entirely
different books.

---

## Worked example

```bash
BASE=https://<host>/agent-api/v1

TOKEN=$(curl -s -X POST $BASE/auth/login -H 'Content-Type: application/json' -d '{
  "username":"ravi.k","password":"•••","customer_id":"FM-2291",
  "device_id":"9774d56d682e549c"}' | jq -r .token)

curl -s $BASE/phonebooks \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Device-Id: 9774d56d682e549c"

# how many contacts can actually be messaged
curl -s $BASE/phonebooks -H "Authorization: Bearer $TOKEN" \
     -H "X-Device-Id: 9774d56d682e549c" \
  | jq '[.[].contacts[] | select(.opted_out == false)] | length'
```

---

## Client checklist

- [ ] Send **both** `Authorization` and `X-Device-Id`
- [ ] **Disable compose for any contact with `opted_out: true`** — show them, greyed
- [ ] Treat `[]` as "none assigned", never as a failure
- [ ] Render an empty book rather than hiding it
- [ ] Do not assume `contact.agent_id` is the logged-in agent — it usually is not
- [ ] Sync on start and on refresh, not per screen; cache locally
- [ ] Never cache `opted_out` against a phone number across companies
- [ ] Any `401` → clear token, show login, do not retry
- [ ] Offer edit actions only on books with `editable: true`
- [ ] After a create or contact upload, show the `skipped` rows with their reasons
- [ ] Parse leniently — fields will be added without a version bump

---

## Related

| Document | Covers |
|---|---|
| [`AGENT_API_AUTH.md`](./AGENT_API_AUTH.md) | Login, session, sign out, device binding |
| [`AGENT_API_TEMPLATES.md`](./AGENT_API_TEMPLATES.md) | The templates an agent may send |
| `../BROADCAST_DESIGN.md` §9 | Why opt-out is a hard block and is never denormalised |
| `app/agent/phonebooks.py` | The implementation |
| `tests/test_agent_phonebooks.py` | 25 tests covering this endpoint and the mapping |


---

## Import from Excel or CSV

### `GET /agent-api/v1/phonebooks/sample?format=xlsx|csv`

A blank file to fill in: headings `WhatsApp Number`, `Customer Name`, `Email`. In the
Excel file the number column is formatted as **Text** — keep it that way, or Excel
rounds long numbers (`9.18E+11`) and the digits are lost. An "Instructions" sheet
explains each column.

### `POST /agent-api/v1/phonebooks/{phonebook_id}/import`

Multipart field `file`: an `.xlsx` or `.csv` (max 5 MB). Only for a phonebook **this
agent created** (`editable: true`). Columns are matched by heading — the sample's
headings always match; common variants ("Mobile", "Phone", "Name") do too.

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

`row` is the **spreadsheet's row number** (row 1 = headings), so the agent can find it.
Bad rows are skipped, the rest are added. Numbers are stored in WhatsApp's form
(`8075571893` → `918075571893`, using the company's default country).

| `code` | HTTP | Meaning — nothing was added |
|---|---|---|
| `file_unreadable` | 400 | Damaged file |
| `unsupported_file_type` | 415 | Old `.xls` or not a spreadsheet — save as `.xlsx` or `.csv` |
| `file_too_large` | 413 | Over 5 MB |
| `empty_file` | 400 | No rows |
| `missing_number_column` | 400 | No WhatsApp number column found |
| `phonebook_full` | 422 | Would exceed `max_contacts_per_phonebook` (see `GET /limits`) |
