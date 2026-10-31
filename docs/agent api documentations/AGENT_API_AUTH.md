# Agent App API — Authentication

**Status:** built and running. Verified end to end against a live server on 2026-09-02.
**Base URL:** `https://<host>/agent-api/v1`
**Covers:** login, session check, sign out, and device binding.
**See also:** [`README.md`](./README.md) for the index — threads, messages and
broadcasts are now built and documented alongside this file. Media upload is not.

> ⚠️ **If you were given an earlier `AGENT_API.md`, discard it.** That was a contract
> proposal written before implementation. It described a JWT access token with a rotating
> refresh token obtained from `/api/auth/login`; **none of that is what was built.** This
> API uses an opaque session token bound to a device, and it is documented here from the
> code. The endpoints that document once described as "planned" are now built and have
> their own files — see [`README.md`](./README.md).

---

## What is different from a normal login, and why

Three things, each with a reason worth knowing before you write the client.

**One opaque token. No refresh token, no rotation, no expiry.**
The web panel uses a 15-minute JWT plus a rotating refresh token. That rotation is
single-use with no grace window: the server revokes the old token and returns the
successor in one statement. If that *response* is lost in transit — which is ordinary on
patchy mobile data — the token is already spent and the agent is logged out for good. No
amount of client-side care fixes a lost response, so the agent app does not use that
mechanism at all.

**The session is bound to one device, checked on every request.**
An agent is licensed to one handset. The token alone is not enough: every authenticated
request must also carry `X-Device-Id`, and it must match the device the session was
issued to. A token copied off the phone is inert anywhere else.

**A `401` has exactly one meaning.**
There is nothing to refresh, so any `401` means *the session is over*. Clear the stored
token and show the login screen. Do not retry, do not attempt recovery.

---

## Before an agent can log in

An administrator does this once, in the web panel:

1. Create a user with role **Agent**.
2. Open that user, go to the **Agent Config** tab, and set:
   - **Agent ID** — their identifier in your agent system, e.g. `AG01`. Unique per company.
   - **Customer ID** — the licence key the app will send. It must match exactly.
   - **Templates** — which approved templates this agent may send. With none selected,
     `GET /templates` returns an empty list.
3. Save.

The device is **not** set here. It is claimed automatically by the first successful login
(see [Device binding](#device-binding)).

An Agent-role user **cannot sign into the web panel**. Attempting it returns `403` with
*"This account is for the agent app. Sign in from the app instead."*

---

## `POST /agent-api/v1/auth/login`

### Request

```json
{
  "username":    "ravi.k",
  "password":    "•••••••••",
  "customer_id": "FM-2291",
  "device_id":   "9774d56d682e549c",
  "app_version": "1.0.3",
  "os_version":  "Android 14"
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `username` | string | **yes** | 1–100 chars. The same username the admin created |
| `password` | string | **yes** | 1–200 chars |
| `customer_id` | string | **yes** | The licence key. Matched against the value stored for this agent — we never call your licensing server |
| `device_id` | string | **yes** | This handset's identifier. Use `Settings.Secure.ANDROID_ID` — see [Device binding](#device-binding) |
| `app_version` | string | no | Shown to admins in the sessions list. Send it; it makes support possible |
| `os_version` | string | no | Same |

### Response — `200 OK`

```json
{
  "token":       "vzGBuM1cNdw2BiUVegnSQe7f3c1e9b204a7f3c1e9b204a7f3c1e9b204a7f3c1e",
  "expires_at":  null,
  "server_time": "2026-09-02T05:45:58.754228Z",

  "agent": {
    "user_id":      "db3bb70e-dd98-480a-b8b5-19d01cd710b1",
    "username":     "ravi.k",
    "full_name":    "Ravi Kumar",
    "agent_id":     "AG01",
    "company_id":   "9a2b1c3d-0000-0000-0000-000000000001",
    "company_name": "Softland India Ltd"
  },

  "device": {
    "device_id":   "9774d56d682e549c",
    "bound_at":    "2026-09-02T05:45:58.721114Z",
    "newly_bound": true
  }
}
```

| Field | Meaning |
|---|---|
| `token` | The session token. Send it as `Authorization: Bearer <token>` on every request |
| `expires_at` | **Always `null`.** The session lives until it is revoked — see [Session lifetime](#session-lifetime). Show "signed in", not a date |
| `server_time` | Our clock, right now. **Store the offset** — see [server_time](#server_time) |
| `agent` | Who this is. Complete, so the app needs no second call on launch |
| `device.newly_bound` | `true` only on the login that claimed this handset. Show a one-time confirmation |

### Errors

Every error body is:

```json
{ "detail": { "code": "device_mismatch", "message": "…" } }
```

Branch on `code`. `message` is written for the person holding the phone and can be shown
as-is.

| HTTP | `code` | When | What the app should do |
|---|---|---|---|
| `401` | `invalid_credentials` | Wrong password **or** unknown username | Show the message. Do not say which was wrong — the server deliberately does not tell you |
| `403` | `account_locked` | 5 failed passwords. Includes `retry_after_minutes` | Show the countdown; the account unlocks on its own |
| `403` | `account_disabled` | The user account is switched off | Direct them to their administrator |
| `403` | `not_an_agent` | No Agent Config, or app access switched off | Direct them to their administrator |
| `403` | `licence_mismatch` | `customer_id` does not match | Usually a misconfigured build |
| `403` | `device_mismatch` | This agent is registered to a **different** handset. Includes `registered_device_id` | An admin must release the old device |
| `403` | `device_already_registered` | This handset belongs to **another agent** | An admin must release it from them |
| `422` | — | Malformed body. FastAPI's validation array | A client bug |

> **`invalid_credentials` is identical for an unknown username and a wrong password** —
> same body, and the server spends the same time on both. Anything else would let someone
> discover which usernames exist. Do not try to distinguish them in the UI.

---

## `GET /agent-api/v1/auth/session`

The cold-start check: *is my stored token still good?* No body.

**Headers:** `Authorization: Bearer <token>` and `X-Device-Id: <device_id>`.

### Response — `200 OK`

The same shape as login, with `token: null` — you already hold it.

```json
{
  "token":       null,
  "expires_at":  null,
  "server_time": "2026-09-02T06:12:44.019284Z",
  "agent":  { "…": "as above" },
  "device": { "device_id": "9774d56d682e549c",
              "bound_at": "2026-09-02T05:45:58.721114Z",
              "newly_bound": false }
}
```

`401` means the session is over. Clear the token and show login.

Call this on app start, before rendering anything that depends on being signed in. It is
also the cheapest way to re-sample `server_time`.

---

## `POST /agent-api/v1/auth/logout`

Signs out **this session only**. No body. Same two headers.

**`204 No Content`** on success.

The device stays registered — this is a sign-out, not a release. The same agent can sign
in again from the same handset immediately.

---

## `GET /agent-api/v1/templates`

The templates this agent may send. No body; the usual two headers.

Only relevant when a conversation's 24-hour window has closed — inside it an agent
replies in free text, outside it WhatsApp accepts approved templates only. So this is
what fills the picker when the composer is disabled.

### Response — `200 OK`

```json
[
  {
    "id":          "7c2e1f04-3a55-4b90-9e21-8f0c4d6b1122",
    "name":        "order_update",
    "language":    "en_US",
    "category":    "UTILITY",
    "body_text":   "Hello {{1}}, your order {{2}} ships on {{3}}.",
    "param_count": 3,
    "parameters": [
      { "index": 1, "label": "Param 1", "example": "Ravi" },
      { "index": 2, "label": "Param 2", "example": "SO-1201" },
      { "index": 3, "label": "Param 3", "example": "5 Sep" }
    ],
    "header":      { "format": "TEXT", "text": "Order update" },
    "footer_text": "Softland India",
    "buttons":     [ { "type": "QUICK_REPLY", "text": "Track" } ]
  }
]
```

| Field | Notes |
|---|---|
| `name` + `language` | **The send address.** WhatsApp identifies a template by these, not by `id` |
| `body_text` | The approved text with `{{n}}` left in. Render it as a live preview, substituting what the agent types |
| `param_count` | Exactly this many values must be supplied, in order |
| `parameters[].example` | Meta's own sample value. Useful as placeholder text |
| `parameters[].label` | Often just `"Param 1"` — see the note below |
| `header.format` | `null`, `TEXT`, `IMAGE`, `DOCUMENT` or `VIDEO`. `text` is filled only for `TEXT` |
| `buttons[].type` | `QUICK_REPLY` comes back as an inbound message when tapped; `URL` just opens a link |

### Three behaviours to build around

**An empty list is normal, not an error.** It means an administrator has not assigned any
templates yet. Say so plainly — *"No templates have been assigned to you"* — and point at
the administrator. It is deliberately not "all of the company's templates": a template
send outside the window is a billable message.

**The list can change without warning.** Templates are resolved live, so an edit or a
re-approval at Meta reaches you on the next call — including a changed parameter count.
**Never cache `param_count` across sessions**; read it from the response you are about to
send with.

**A template can disappear.** One paused or rejected at Meta is dropped from this list,
because sending it would fail. If the agent had it open, handle its absence on refresh
rather than assuming the list only grows.

> **On `label`:** it falls back to `"Param 1"`, `"Param 2"` unless an administrator has
> mapped that template's parameters for the ingest API. Show the label *and* the body
> text, highlighting the placeholder being filled — the surrounding sentence is what
> actually tells the agent what belongs in the box.

---

## Every authenticated request

```
Authorization: Bearer vzGBuM1cNdw2BiUVegnSQe…
X-Device-Id:   9774d56d682e549c
```

**Both, every time.** A request with a valid token and a missing or wrong `X-Device-Id`
returns `401`, exactly as an unknown token does.

### The `401` contract

A single error covers every session failure — expired, revoked, unknown token, wrong
device, agent switched off, user disabled, account locked:

```json
{ "detail": { "code": "invalid_session",
              "message": "Your session has ended. Please sign in again." } }
```

One code on purpose. There is nothing to refresh, so the app cannot act differently on
the different reasons. **Clear the stored token, return to login.** Do not retry.

---

## Device binding

An agent is licensed to **one handset**, and that handset serves **one agent**.

### What to send as `device_id`

`Settings.Secure.ANDROID_ID`. The alternatives do not work:

- IMEI and serial number need privileged permissions and are unavailable to normal apps
  since Android 10.
- A UUID you generate and store resets whenever app data is cleared.

`ANDROID_ID` survives a reinstall. **It does not survive a factory reset** — which matters,
see below.

### How the binding happens

Trust on first use. The admin does not type a device id anywhere.

| Situation | Result |
|---|---|
| Agent has no device yet, and this handset is free | **Bound.** `newly_bound: true` |
| This handset is already this agent's | Normal login, `newly_bound: false` |
| Agent is bound to a different handset | `device_mismatch` |
| This handset belongs to another agent | `device_already_registered` |

Show the agent a one-time message when `newly_bound` is `true` — something like *"This
phone is now registered to your account."* It is much better that they learn the
one-device rule then, rather than the first time they try a second phone.

### When an agent changes phone — or factory resets

They are locked out until an administrator opens their **Agent Config** tab and presses
**Release device**. That clears the binding, signs out any live session, and the next
login registers the new handset.

**This includes a factory reset of the same physical phone**, because `ANDROID_ID`
changes. From the agent's point of view nothing changed and they cannot get in, so the
app's `device_mismatch` message should point at the administrator explicitly.

---

## Session lifetime

**There is no absolute expiry.** `expires_at` is always `null`. A session ends only when
something ends it:

| Ends the session | Takes effect |
|---|---|
| The agent signs out | Immediately |
| The agent signs in again (anywhere) | The previous session is superseded immediately |
| An admin revokes the session | Next request |
| An admin releases the device | Next request |
| An admin switches off app access, or disables the user | Next request |
| Unused for **90 days** | On the next attempt |

A timed expiry was considered and rejected: every control above already takes effect on
the next request, so a timer would only sign someone out mid-shift with nothing having
happened.

**Only one session per agent is live at a time.** Signing in again — after a reinstall,
say — retires the previous token. If a request suddenly returns `401`, the agent has
signed in somewhere else.

---

## `server_time`

Every response carries it. **Use it, not the device clock.**

```kotlin
val offset = serverTime.toEpochMilli() - System.currentTimeMillis()
// later, for anything time-sensitive:
val trueNow = System.currentTimeMillis() + offset
```

Android clocks drift, and users set them by hand. This matters little for login, but the
messaging API that comes next has a **24-hour reply window** with a live countdown, and a
phone forty minutes fast would show an agent forty minutes of time they do not have — or
show time remaining on a window that has already closed.

Re-sample the offset on every response. It costs nothing and it self-corrects.

---

## Storing the token

The token does not expire, so treat it as a long-lived credential:

- **`EncryptedSharedPreferences`**, backed by the Android Keystore. Not plain
  `SharedPreferences`.
- **`android:allowBackup="false"`** in the manifest, or it ends up in a Google cloud backup.
- Never log it, never put it in a URL, never send it to a crash reporter.

---

## Client checklist

- [ ] Send `Authorization` **and** `X-Device-Id` on every request
- [ ] Treat any `401` as "session over" — clear the token, show login. **Never retry**
- [ ] Never distinguish unknown-username from wrong-password in the UI
- [ ] Show a one-time confirmation when `newly_bound` is `true`
- [ ] On `device_mismatch` / `device_already_registered`, tell them to contact their administrator
- [ ] Store the `server_time` offset; recompute it on every response
- [ ] `EncryptedSharedPreferences` + `allowBackup="false"`
- [ ] Send `app_version` and `os_version` at login — support depends on them
- [ ] Re-read `GET /templates` rather than caching it — `param_count` can change
- [ ] Handle an empty template list as "none assigned", not as an error
- [ ] Parse leniently: fields will be **added** to these responses without a version bump

---

## Worked example

```bash
BASE=https://<host>/agent-api/v1

# 1. First login — claims this handset
curl -s -X POST $BASE/auth/login -H 'Content-Type: application/json' -d '{
  "username":"ravi.k","password":"•••","customer_id":"FM-2291",
  "device_id":"9774d56d682e549c","app_version":"1.0.3","os_version":"Android 14"}'
# → 200, "newly_bound": true

# 2. Check the session on next app start
curl -s $BASE/auth/session \
  -H "Authorization: Bearer $TOKEN" -H "X-Device-Id: 9774d56d682e549c"
# → 200

# 3. Same token, wrong handset
curl -s $BASE/auth/session \
  -H "Authorization: Bearer $TOKEN" -H "X-Device-Id: some-other-phone"
# → 401  invalid_session

# 4. Sign out
curl -s -X POST $BASE/auth/logout \
  -H "Authorization: Bearer $TOKEN" -H "X-Device-Id: 9774d56d682e549c"
# → 204
```

---

## Related

| Document | What it covers |
|---|---|
| [`AGENT_INBOX_DESIGN.md`](../AGENT_INBOX_DESIGN.md) | The full system design — threads, routing, the 24-hour window, the sync cursor |
| [`README.md`](./README.md) | Index, shared headers, and every error code in the API |
| [`AGENT_API_THREADS.md`](./AGENT_API_THREADS.md) | Inbox, chat window, paging, the 24-hour window |
| [`AGENT_API_MESSAGES.md`](./AGENT_API_MESSAGES.md) | Sending one free-text message |
| [`AGENT_API_BROADCASTS.md`](./AGENT_API_BROADCASTS.md) | Sending a template to a phonebook |
| `AGENT_API.md` | An earlier contract proposal, no longer in the repository. **Fully superseded** by the files above — it described a JWT + refresh design that was never built |
| `app/agent/` | The implementation — `auth.py`, `deps.py`, `errors.py`, `schemas.py` |
| `tests/test_agent_auth.py` | 36 tests covering every auth path documented here |
| `tests/test_agent_templates.py` | 20 tests covering the template mapping and this endpoint |
