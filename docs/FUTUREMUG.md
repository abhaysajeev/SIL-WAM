# Future Mug — send and receive pipeline

**Status:** analysis only. Nothing built, nothing decided. Four questions in §10 block planning.
**Date:** 2026-09-02
**Scope agreed with the client:** we provide **the sending pipeline and the receiving
webhook, and nothing else**. Their templates, their scheduling logic, their business
workflow all stay on their side.

## Source documents

Supplied by the client, kept in this repo:

| File | What it holds |
|---|---|
| [`docs/Gupshup_WhatsApp_Integration.docx`](./Gupshup_WhatsApp_Integration.docx) | The full template catalog **and** §5, the two Gupshup bot journeys — read §5 first, it is the part that is not obvious |
| [`docs/Gupshup template reference.docx`](./Gupshup%20template%20reference.docx) | The same 24 templates with rendered screenshots (23 images) |

Absolute paths as shared:

```
/home/silpc-068/Desktop/2026/SIL Whatsapp/SIL-WAM/docs/Gupshup_WhatsApp_Integration.docx
/home/silpc-068/Desktop/2026/SIL Whatsapp/SIL-WAM/docs/Gupshup template reference.docx
```

**Companion docs in this repo:** [`BROADCAST_DESIGN.md`](./BROADCAST_DESIGN.md) §8 and §12
(why broadcast-class traffic stays out of the transactional tables, and the scheduler
architecture), [`LIZO_API.md`](./LIZO_API.md) + `app/lizo/` (the precedent this client
should follow — a separate contract layer over shared domain logic),
[`CLIENT_API_INGEST.md`](./CLIENT_API_INGEST.md) (the existing `/client-api/v1`, and why
it is *not* the right fit here — see §6).

---

## 1. Who they are

A technical-interview-as-a-service company. Three parties in every message:

- **Candidate** — applies, picks a slot, attends
- **Interviewer / panel** — confirms availability, runs the interview, submits a verdict
- **Employer / client** — the company hiring, kept informed at every step

Every one of their 24 templates is transactional notification around a single lifecycle:

```
invite → candidate picks slot → interviewer confirms → interview → verdict → report
```

They are currently on **Gupshup** as their BSP and are moving off it.

---

## 2. What they are asking us for

A dumb pipe, in both directions:

- **Send** — they call us, we put a WhatsApp message out
- **Receive** — a customer replies, we push it to their webhook

They own everything else. We do not model interviews, candidates, slots or panels, and
we hold no state about their business.

---

## 3. The template catalog — 24 templates

Grouped by who receives them. Parameter names are the client's own, from the doc.

### To the employer / client (10)

| Purpose | Params |
|---|---|
| Interview scheduled | company_name, candidate_name, date_time, job_role |
| Interview completed | candidate_name, candidate_phone, date_time, job_role |
| Candidate not responding — with reason | + reason |
| Candidate not responding — no reason | candidate_name, candidate_phone, date_time, job_role |
| No-show — with reason | + reason |
| No-show — no reason | candidate_name, candidate_phone, date_time, job_role |
| Reschedule requested — with reason | + reason |
| Reschedule requested — no reason | candidate_name, candidate_phone, date_time, job_role |
| Interview cancelled — with reason | + reason |
| Interview cancelled — no reason | candidate_name, candidate_phone, date_time, job_role |
| Candidate not interested — with / without reason | candidate_name, candidate_phone, job_role (+ reason) |
| Candidate dropped out — with / without reason | candidate_name, candidate_phone, job_role (+ reason) |
| Panel verdict submitted | company, job_role, verdict, candidate_name, interview_date |

### To the candidate (5)

| Purpose | Params |
|---|---|
| **Interview invite — has a button** | Candidate_name, Position, Client_name |
| Interview confirmed | Client_Name, Position_Name, Date, Time |
| 15-minutes-before reminder (cron-driven) | candidate_name, formatted_date, formatted_time |
| We've been trying to reach you | candidate_name, job_role, company_name |

### To the interviewer / panel (3)

| Purpose | Params |
|---|---|
| **Confirm availability — has buttons** | interviewer_name, candidate_name, position, date, time |
| Interview confirmed | candidate_name, Position_Name, Date, Time |
| Onboarding credentials | name, "login details", dashboard, URL, email, password, manager, contact |

### Internal / admin (2)

| Purpose | Params |
|---|---|
| New job description added | job_role, candidate_name, date_time |
| Fill the interview report — reminder | *(none)* |

### An observation worth carrying forward

**Seven templates exist twice** — once "with reason" and once "without". That is the
standard workaround for Meta rejecting an empty parameter: a blank `{{5}}` cannot be
sent, so a second template is approved without the placeholder. It doubles the approval
work in §7 and doubles their mapping table.

It is their call, not ours. But if they are re-approving everything anyway (§7), this is
the moment to collapse each pair into one template with the reason folded into an
existing parameter — 24 templates becomes 17.

---

## 4. The two Gupshup journeys — the part that is not just templates

**Read §5 of the integration doc.** Two of the 24 templates carry quick-reply buttons,
and the button taps drive **conversational state machines that currently run inside
Gupshup**, not inside Future Mug's application.

### 4.1 Interviewer journey (`d28437c4-…`)

Asks an interviewer to confirm availability. Three buttons, and the branches call *their*
APIs:

| Reply | What happens |
|---|---|
| **I will be available** | Resolves the original message by its Gupshup id, calls their `Schedule_interview` API, branches on the returned status: **1** → send the confirmation template to the candidate · **2** → "you have already committed to another schedule" · **3** → "accepted by someone else or the time has passed" · anything else → generic failure |
| **I'm not available** | No backend call. A thank-you reply only |
| **Cancel Interview** | Calls their *Clear Interviewer* API, re-fetches the matching interviewer pool, and **re-broadcasts the same open slot to all of them** via `POST /api/notifyinterviewer` |

### 4.2 Candidate journey (`1d3db787-…`)

Invites a candidate to arrange their interview:

1. Button tap → *"Please select your available time slot(s)"*
2. **Interactive list** of the next 5 calendar days, **computed at runtime, Sundays excluded**
3. **Interactive list** of times available for the chosen date, loaded dynamically
4. Calls their `Notify_all_Interviewer` API, which fans the interviewer template out to the pool
5. `Reschedule` → Yes/No confirmation → cancels the booking, notifies the previously-assigned interviewer, re-enters the slot picker
6. `Unsubscribe` → replies "Unsubscribed"
7. If the original message context cannot be resolved → *"we are unable to find your records"*

### 4.3 Why this matters to us

> This logic does not move to us, and it does not disappear. It has to be rebuilt inside
> Future Mug's own backend.

That is arguably where it belongs — every branch calls their APIs and reads their
database. But **it must be said explicitly to the client**, because it is a real chunk of
work on their side and they may be assuming it comes along with the migration.

Our obligation is narrower but absolute: **give them enough in the inbound webhook to
rebuild it.**

> 🔴 **The linchpin is the context pointer.** Both journeys begin by resolving the
> original message by its Gupshup `gsId` to recover *which interview this reply is
> about*. Our inbound webhook must carry the equivalent — the id of the message we sent
> that this reply answers. Without it neither journey can be rebuilt at all, and the
> client discovers this after integrating.

### 4.4 What the journeys imply for the send API

The date and time pickers are built at runtime from *their* availability data. So the
send side needs **interactive list and reply-button messages as first-class message
types**, not only template sends. See §6.

---

## 5. What already exists in this codebase

More than expected. The shape — an external system posting sends and receiving callbacks
— is exactly `/client-api/v1` and Lizo.

| Need | Where it already lives |
|---|---|
| `X-API-Key` auth, per-company keys | `core/deps.py::get_api_key_and_company`, `models/api_key.py` |
| Send a template with parameters | `services/wa_sender.py::send_template` |
| Plain text send | `wa_sender.send_text` |
| **Interactive reply buttons** (max 3) | `wa_sender.send_interactive_buttons` |
| **Interactive list picker** (10 per section) | `wa_sender.send_list_message` |
| Document send | `wa_sender.send_document` |
| Media header on a template | `wa_sender.send_template(header_media=…)`, `services/meta_media.py` |
| Durable outbound callbacks with retry + backoff | `services/notify_queue.py` → `services/notify_scheduler.py`, `models/outbound_notification.py` |
| Inbound receipt, dedup by `wamid` | `api/meta_webhook.py` → `services/conversation_engine.py::handle_inbound` |
| Delivery / read / failed receipts | `conversation_engine.handle_status` + `services/broadcast_status.py` |
| Opt-out (`Unsubscribe`) | `models/messaging.py::MessagingOptOut` |
| A separate contract layer per client | `app/lizo/` — 11 modules, ~1,600 lines |

**Both interactive primitives the journeys need are already written.** That was not
expected and it materially reduces the work.

---

## 6. What is genuinely new

### 6.1 A dumb-pipe contract layer

`/client-api/v1` is **not** the right fit. It is built around the Service state machine —
question flows, `mobile_queue`, expiry scheduling, answer routing, `ServiceResponse`
rows. Future Mug wants none of that: send this, tell me what came back.

So this is a new layer, `app/futuremug/`, over the same `wa_sender` — following the Lizo
precedent exactly: **separate route and contract layers over shared domain logic, never
URL-prefix branching inside shared handlers** (see the memory note on Lizo/Shirin).

### 6.2 An outbound webhook for inbound messages

Today an inbound message reaches `handle_inbound`, finds no service, and stops. Future
Mug needs every reply pushed to them:

- plain text
- **button tap** — with the button id *and* title, since the journeys branch on the
  literal strings "I will be available", "I'm not available", "Cancel Interview"
- **list selection** — the chosen row id and title
- the **context pointer** (§4.3)

This attaches at the same `service is None` branch the agent inbox design identified as
its attachment point, and reuses the `outbound_notifications` delivery machinery.

### 6.3 Message records with no Service

Their sends are neither services nor broadcasts. A light table is needed so an inbound
reply can be matched back to the message it answers — the same reasoning that put `wamid`
on `broadcast_recipients` (BROADCAST_DESIGN §8).

---

## 7. Three things that will bite

### 7.1 Every template must be re-created and re-approved

Every id in their documents — `1d3db787-…`, `d28437c4-…` — is a **Gupshup** id. It means
nothing to Meta and nothing to us. Templates are per-WABA.

So either Future Mug brings their WABA to us (embedded signup), or **all 24 templates are
resubmitted for approval** under the hosting WABA. That is a Meta review cycle with real
lead time and a real rejection risk, and it should be scheduled, not discovered.

Their code also addresses templates by Gupshup UUID. Against us it is **name +
language**. That mapping table is their change.

### 7.2 The 24-hour window, and its new price

Templates go out cold — fine. But every free-text reply in the journeys — *"Interview has
been cancelled successfully."*, *"Unsubscribed"*, *"Sorry, we are unable to find your
records"* — is a **service message**, legal only inside 24 hours of the customer's last
inbound. In practice each follows a button tap, so the window is open. It still has to be
enforced server-side rather than assumed.

> 💰 **Meta begins charging for service messages on 1 October 2026** — four weeks out.
> Their per-message cost changes inside this quarter. Flag it before they budget.

### 7.3 Interactive sends are not templates

The date and time pickers are assembled at runtime from their availability data, so the
API needs list and button message types as first-class citizens. The primitives exist
(§5); the contract does not.

---

## 8. Proposed API surface

```
POST /fm-api/v1/messages/template    template by name + language, params, optional media
POST /fm-api/v1/messages/text        free text, window-enforced
POST /fm-api/v1/messages/buttons     up to 3 reply buttons
POST /fm-api/v1/messages/list        the date and time pickers
GET  /fm-api/v1/messages/{id}        status poll, if they want one alongside the webhook

→ webhook to their URL: inbound reply (text | button | list) + context pointer
→ webhook to their URL: delivery status (sent | delivered | read | failed)
```

Auth: `X-API-Key`, as `/client-api/v1` and Lizo already do.

**Rough estimate: about a week**, most of it the inbound webhook and the contract layer,
since the send primitives are done. Not a commitment — §10 has to be answered first.

---

## 9. Architecture

Follow Lizo. A new package `app/futuremug/` with its own router, schemas, response
envelope and webhook emitter, calling `wa_sender` and the existing notify machinery.
Nothing in the shared handlers learns that Future Mug exists.

Two decisions to make deliberately, not by default:

- **Inline send or queued?** Their traffic is transactional and latency matters
  (an interviewer tapping a button expects a reply). The agent-inbox pattern — commit
  first, attempt inline, fall back to a retry job — fits better than the broadcast
  scheduler. Depends on §10.4.
- **Conversation/Message rows or not?** If they want only the pipe, skip them. That keeps
  their volume out of the transactional tables for the same reason broadcast does
  (BROADCAST_DESIGN §8), and out of the Conversations list, which is ordered by activity.

---

## 10. Open questions — these block planning

1. **Whose WhatsApp number?** Does Future Mug bring their own WABA, or do we host them on
   a number we manage? This decides the onboarding path and who owns template approval
   (§7.1).
2. **Do they need the message history UI, or purely the pipe?** If purely the pipe we can
   skip `Conversation`/`Message` rows entirely (§9).
3. **Are they rebuilding the two journeys themselves?** Assumed yes (§4.3). If they expect
   *us* to host that conversational logic, this is a substantially larger job and a
   different commercial conversation.
4. **Volume and latency.** Reminders are cron-driven and clustered; "notify all
   interviewers" fans out to a pool. Pool size and daily peak decide inline versus queued
   (§9).

---

## 11. Decisions log

| Decision | Rationale |
|---|---|
| A new `app/futuremug/` contract layer, not an extension of `/client-api/v1` | That API is built around the Service state machine, which this client explicitly does not want |
| Follow the Lizo precedent | Separate route and contract layers over shared domain logic; no URL-prefix branching in shared handlers |
| The inbound webhook must carry a context pointer to the original message | Both Gupshup journeys resolve context by `gsId` first; without the equivalent, neither can be rebuilt (§4.3) |
| Button **id and title** both go in the webhook | The journeys branch on the literal button strings, not on ids |
| Journey logic stays on the client's side | Every branch calls their APIs and reads their database. Must be stated explicitly, not assumed (§4.3) |
