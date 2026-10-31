# Agent App API — Phonebooks

**Status:** built and running. Every example below is a real response, captured from the
running server on 2026-09-03.
**Endpoint:** `GET /agent-api/v1/phonebooks`
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
| `name` | string | As named in the web panel |
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
