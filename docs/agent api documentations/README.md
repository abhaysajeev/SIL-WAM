# Agent App API — index

**Base URL:** `https://<host>/agent-api/v1`
**Status:** complete for v1. Auth, templates, phonebooks, the chat window, 1:1 sending
and agent-started broadcasts are all built and tested.

**New to the app? Read [`DEVICE_INTEGRATION_GUIDE.md`](./DEVICE_INTEGRATION_GUIDE.md) first**:
every endpoint, field and error code in one place, with the call order, the sync algorithm
and a QA checklist. The files below go deeper per screen.

Start here, then read the file for the screen you are building.

| Document | Covers | Endpoints |
|---|---|---|
| [`AGENT_API_AUTH.md`](./AGENT_API_AUTH.md) | Sign in, session, sign out, device binding | `POST /auth/login`, `GET /auth/session`, `POST /auth/logout` |
| [`AGENT_API_TEMPLATES.md`](./AGENT_API_TEMPLATES.md) | The approved templates this agent may send | `GET /templates` |
| [`AGENT_API_PHONEBOOKS.md`](./AGENT_API_PHONEBOOKS.md) | The contacts this agent may work from; create, edit, Excel/CSV import | `GET /phonebooks`, `POST /phonebooks`, `GET /phonebooks/sample`, `POST /phonebooks/{id}/import` |
| [`AGENT_API_THREADS.md`](./AGENT_API_THREADS.md) | Inbox list, chat window, scrolling and sync | `GET /threads`, `GET /threads/{id}/messages`, `GET /threads/by-mobile/{mobile}` |
| [`AGENT_API_REPLIES.md`](./AGENT_API_REPLIES.md) | **Replying**: text, template or file, with retry key and teammate check | `POST /threads/{id}/messages` |
| [`AGENT_API_PUSH.md`](./AGENT_API_PUSH.md) | **Push notifications, live updates, and the sync contract** | `PUT /devices/push-token`, `GET /events` |
| [`AGENT_API_MESSAGES.md`](./AGENT_API_MESSAGES.md) | Older free-text send by mobile number (kept for old builds) | `POST /messages` |
| [`AGENT_API_BROADCASTS.md`](./AGENT_API_BROADCASTS.md) | Sending a template to one or more phonebooks, incl. media templates | `POST /broadcasts` |
| [`AGENT_API_MEDIA.md`](./AGENT_API_MEDIA.md) | Uploading a broadcast image/video/PDF; downloading chat files | `POST /media`, `GET /media/{id}` |
| [`AGENT_API_LIMITS.md`](./AGENT_API_LIMITS.md) | Every ceiling, to show before the agent hits it | `GET /limits` |

---

## Every request

```
Authorization: Bearer <session token>
X-Device-Id:   <the device this session was issued to>
```

**Both headers, on every authenticated call.** Omitting `X-Device-Id` is a `401`, not a
`400` — the token alone is not a session. See `AGENT_API_AUTH.md` for why the device is
part of the credential.

## Every error

One shape, everywhere:

```json
{ "detail": { "code": "window_closed", "message": "You can only send a free message…" } }
```

**Branch on `code`. Show `message`.** The messages are written to be read by the person
holding the phone, so they can usually be displayed verbatim. Some codes carry extra
fields alongside — those are listed with the endpoint that raises them.

### Every code in the API

| `code` | HTTP | Meaning | What the app should do |
|---|---|---|---|
| `invalid_credentials` | 401 | Unknown username **or** wrong password — deliberately indistinguishable | Show the error on the login form |
| `account_locked` | 403 | Too many failed attempts (`retry_after_minutes`) | Show the countdown; disable the button |
| `account_disabled` | 403 | The user was switched off | "Contact your administrator" |
| `not_an_agent` | 403 | Valid user, no agent configuration | "Contact your administrator" |
| `licence_mismatch` | 403 | `customer_id` is not the one registered | Check the entered customer ID |
| `device_mismatch` | 403 | Account bound to another handset (`registered_device_id`) | "Ask your administrator to release it" |
| `device_already_registered` | 403 | This handset belongs to another agent | "Contact your administrator" |
| `invalid_session` | 401 | Expired, revoked, unknown token, or wrong/missing device header | **Clear the token, show login. Do not retry** |
| `window_closed` | 409 | Outside the 24-hour service window (`last_inbound_at`) | Offer a template instead of free text |
| `recipient_opted_out` | 403 | This person unsubscribed | Disable the composer for this contact |
| `recipient_cap_exceeded` | 403 | Broadcast too large (`recipient_count`, `limit`) | Show the limit; suggest a smaller list |
| `daily_limit_reached` | 403 | Agent's rolling 24h quota spent (`sent_today`, `limit`) | Show when it frees up (gradually) |
| `param_count_mismatch` | 400 | Wrong number of template values (`expected`, `supplied`) | **Re-fetch templates and retry** |
| `nothing_to_send` | 409 | Every contact screened out | "Everyone on this list has unsubscribed" |
| `no_whatsapp_account` | 409 | The company has no WhatsApp number configured | "Contact your administrator" |
| `not_found` | 404 | Thread, phonebook or template not found *for this agent* — includes a chat with a customer you have not contacted in 30 days | Refresh the list it came from |
| `teammate_replied` | 409 | A teammate replied since `last_seen_rev` (`messages`) | Show it; resend with `force` if still wanted |
| `idempotency_mismatch` | 409 | `client_msg_id` reused for a different message | App bug: new UUID per message |
| `template_unavailable` | 409 | Template paused/disabled by WhatsApp | Refresh templates |
| `invalid_param` | 400 | A template value is empty or has a line break/tab/5+ spaces | Fix the value |
| `account_unavailable` | 503 | Company's WhatsApp account has a problem (`reason`) | "Contact your administrator" |
| `media_*`, `header_media_*`, `file_*` | 4xx | See `AGENT_API_MEDIA.md`, `AGENT_API_PHONEBOOKS.md` | Show `message` |
| `build_failed` | 400 | The recipient list could not be assembled | Show `message` |
| `send_failed` | 502 | WhatsApp rejected the send | Offer retry; the message row still exists |

> **`401` always means the same thing**: drop the stored token and show the login
> screen. There is no refresh token to attempt, so retrying is never the answer.

## Timestamps

Every timestamp is **UTC, ISO-8601, with a `Z` suffix**. Convert for display; never
compare a server timestamp against a device clock without accounting for skew — see the
`server_time` field, explained in `AGENT_API_THREADS.md`.

## Parse leniently

Fields will be **added** without a version bump. Ignore what you do not recognise rather
than failing to parse.
