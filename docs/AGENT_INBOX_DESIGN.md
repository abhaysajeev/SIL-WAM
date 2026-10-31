# Agent Inbox — System Design

**Status:** design proposed, nothing implemented.
**Date:** 2026-08-21, revised 2026-09-02.

> **2026-09-02 — the manager hierarchy is dropped from v1.** Every agent sees exactly their
> own threads. §4 previously specified a reporting tree stored as a materialised path
> (`users.manager_id` + `users.agent_path`), subtree visibility, manager replies and
> subtree-scoped reassignment. All of it is removed below, not merely deferred: an
> unmaintained materialised path is worse than none, because it drifts and someone
> eventually trusts it.
>
> The hierarchy was the only part of this design not forced by the problem. The 24-hour
> window, the per-thread cursor and the routing rules exist because WhatsApp or mobile
> networks make them necessary; the tree existed because someone might want oversight,
> which is a guess about the org rather than a constraint. §4.6 records what to preserve so
> it can come back without breaking a shipped app.

**Companion docs:** [`BROADCAST_DESIGN.md`](./BROADCAST_DESIGN.md) (§8 status tracking, §11 agents),
[`BROADCAST_TIPS.md`](./BROADCAST_TIPS.md) (Meta platform rules). Read §8 and §11 of the
broadcast design first — this document is the deferred half of §11 and assumes its decisions.

---

## 1. Scope

A customer receives a broadcast, replies on WhatsApp, and a human agent answers from an
Android app. The agent sees only their own customers, with full history and timestamps.

**In scope:** agent identity, inbound reply routing, an isolated thread/message store built
for the app, the agent reply send path, the mobile sync protocol, the 24-hour window
countdown, and push wake-ups.

**Deliberately deferred:** the manager hierarchy (see the note above and §4.6), agent-to-agent
transfer, canned replies, supervisor dashboards, media *sending* from the app (receiving is in
scope), typing indicators.

### The constraint that shapes everything

> ⚠️ **Free-form replies are legal only inside a 24-hour window** opened by the customer's
> last inbound message. Outside it, Meta accepts **approved templates only**. There is
> currently no window handling anywhere in this codebase — `grep` for "24h" finds one
> unrelated comment in `broadcast_screening.py`.
>
> An agent inbox without window enforcement looks fine in testing (you always reply within
> seconds) and fails in production the first time an agent picks up yesterday's message.
> Section 7 handles this; it is not optional.

> 💰 **From 1 October 2026** — six weeks out — Meta begins charging for service messages,
> which is exactly what every agent reply is. They are free today. Budget for it now; the
> feature's per-message cost goes from zero to non-zero inside this quarter.

---

## 2. What already works, and where this attaches

Tracing an actual broadcast reply through today's code:

| Step | Where | What happens now |
|---|---|---|
| Customer replies | `POST /webhook/meta` | Received on the main app |
| Dedup | `conversation_engine.handle_inbound` step 1 | Redis, then `Message.wamid` unique index |
| Normalise | step 2 | `_resolve_stored_mobile` → canonical stored form |
| Conversation | step 2 | `_get_or_create_conversation` — the §8 "accepted asymmetry" |
| Service lookup | step 3 | `context.id` → service, or awaiting-free-text. **A broadcast reply matches neither** |
| Store | step 4 | `Message` row, `service_id=None`, `is_flow_message=False` |
| **Step 6** | **`if not service:`** | **Commits and returns. The reply dies here.** |

**Step 6 is the attachment point** — one new branch, nothing above it changes. Dedup,
normalisation and the transactional service flow are untouched, which is what "without
affecting the existing pipeline" requires.

---

## 3. Data model — five new tables, fully isolated

Following §8's precedent: broadcast stays out of `conversations` / `messages`, and the
agent inbox gets purpose-built tables the app can read without joining the transactional
schema.

> **Built as of 2026-09-07 (migration `a7c3e1f5b982`): two of the five.**
> `agent_threads` and `agent_messages` exist and are being written by all three sources —
> broadcast sends, inbound replies, and delivery receipts. They shipped **leaner than
> specified below**, because the assignment model in §4 has no consumer until the agent
> app can actually send. The tables as built are in §3.0; the columns described in §3.1
> and §3.2 that are not in that list are **designed, not present**.
>
> `agent_thread_reads` (§3.5), `agent_thread_assignments` (§3.6) and the device table
> (§3.3 — superseded by `agent_sessions`, already built) are not created.

### 3.0 What actually exists

```
agent_threads
  id
  company_id FK→companies CASCADE
  mobile_no                     canonical stored form, via _resolve_stored_mobile
  contact_name                  nullable; never overwritten with NULL once known
  last_inbound_at               THE 24-hour clock. NULL = they have never written
  last_message_at               either direction; the inbox sort key
  message_seq                   per-thread cursor, allocated under the row lock (§8)
  created_at, updated_at
  UNIQUE (company_id, mobile_no)
  INDEX  (company_id, last_message_at)

agent_messages
  id
  thread_id FK→agent_threads CASCADE
  seq                           per-thread, gap-free
  direction                     inbound | outbound
  kind                          broadcast | agent_reply | template | transactional | reply
  body_rendered                 what the customer sees, placeholders substituted
  template_meta   JSONB         { name, language, params }
  media           JSONB         { type, media_id | link, mime_type, filename }
  wamid                         indexed, not unique
  status                        queued | sent | delivered | read | failed | received
  error_code, error_message
  campaign_id       FK→broadcast_campaigns SET NULL
  sent_by_agent_id  FK→agents SET NULL
  replies_to_wamid              Meta's context.id
  created_at, sent_at, delivered_at, read_at
  UNIQUE (thread_id, seq)
  INDEX  (thread_id, seq), (wamid)
```

**A thread existing is not a thread belonging in an inbox.** A 2,000-recipient campaign
creates 2,000 threads. The inbox filters on `last_inbound_at IS NOT NULL` — people who
actually wrote back — while opening a contact from a phonebook shows the thread
regardless, so prior broadcasts are visible even when nobody replied. One table, two
filters.

**Writes never commit and never raise.** Every function in `app/services/agent_timeline.py`
joins the caller's transaction, so a bubble and the send that produced it either both land
or neither does. Each is wrapped in a **SAVEPOINT**, not a plain try/except: a bare
`db.rollback()` on a thread-creation race would discard everything the caller had staged,
which for `handle_inbound` is the customer's actual `Message` row. A timeline failure must
cost a chat bubble, never a customer's message.

**`wamid` is indexed but not unique**, unlike `Message.wamid`. `Message` is the dedup
authority for inbound; this table is a view, and a uniqueness violation here would turn a
cosmetic duplicate into a failed webhook.

### 3.1 Threads (as designed — assignment columns not yet built)

```
agent_threads
  id
  company_id FK→companies CASCADE
  mobile_no             normalised via _validate_mobile      ← §7, no exceptions
  customer_name
  assigned_agent_id     String(100), nullable, indexed       ← free-text routing key, §4
  assigned_user_id      FK→users SET NULL, nullable          ← resolved owner, §4.2
  origin_campaign_id    FK→broadcast_campaigns SET NULL      ← which campaign opened it
  status                open | closed
  last_inbound_at                                            ← drives the 24h window
  last_message_at                                            ← thread-list sort key
  message_seq           Integer, default 0                   ← per-thread cursor, see §8
  created_at, updated_at
  UNIQUE (company_id, mobile_no)
  INDEX  (company_id, assigned_user_id, last_message_at DESC)
```

**Two ownership columns, deliberately.** `assigned_agent_id` is the raw key that arrives from
a phonebook or a CSV — it may name an agent who has not been provisioned yet, or who no longer
exists. `assigned_user_id` is that key *resolved* to a real user row (§4.2). Keeping them separate
means an unresolvable agent id is a visible `assigned_user_id IS NULL` — the unassigned pool —
rather than a thread that silently belongs to nobody.

**No `unread_count` column.** Unread is per viewer — see `agent_thread_reads` in §3.5. With
one agent per thread the distinction looks academic today; it stops being academic the moment
a second person can open the same thread, and by then the counter is load-bearing in the app.

**One thread per (company, mobile) — not per campaign.** A customer in three campaigns is
one conversation to the human answering them. Fragmenting by campaign would show the agent
three half-threads of the same person. `origin_campaign_id` records where it started; it
does not partition the thread.

### 3.2 Messages (as designed — see §3.0 for what shipped)

```
agent_messages
  id
  thread_id FK→agent_threads CASCADE
  seq                Integer, NOT NULL          ← per-thread, gap-free. §8 explains why
  direction          inbound | outbound
  message_type       text | image | document | audio | video | template
  body               Text, nullable
  media_id           String, nullable           ← Meta media id, resolved lazily
  mime_type          String, nullable
  wamid              String, nullable UNIQUE
  client_msg_id      UUID, nullable UNIQUE      ← mobile retry idempotency, §6
  sender_agent_id    String(100), nullable      ← display id of the sender (outbound)
  sender_user_id     FK→users SET NULL          ← who actually typed it, §4.6
  status             queued | sent | delivered | read | failed
  error_code, error_message
  reply_to_wamid     String, nullable           ← Meta context.id, threads the reply
  created_at, sent_at, delivered_at, read_at
  UNIQUE (thread_id, seq)
  INDEX  (thread_id, seq)
  INDEX  (wamid)
```

**Self-contained, not a join onto `messages`.** Outbound agent replies write here only —
mirroring §8's decision to keep broadcast traffic out of the transactional tables. The app
reads one table, ordered by one column.

Inbound is the exception: `handle_inbound` still writes its `Message` row exactly as it does
today (that row *is* the dedup authority — step 1 checks `Message.wamid`), and the agent
message is written alongside it **in the same transaction**, so the two cannot drift.

**As built, the timeline row is written at step 5b — before the service branching, not
after it.** The obvious place looked like step 6, where an out-of-flow message falls
through and a broadcast reply currently dies. That would have been wrong: a reply that
*does* match a service would never be recorded, and the chat window would show a
transcript with the customer's answers missing. Every inbound is recorded, whatever
happens to it afterwards.

### 3.3 Devices

```
agent_devices
  id, user_id FK→users CASCADE
  fcm_token         String UNIQUE
  platform          android | ios
  app_version
  last_seen_at, is_active
  INDEX (user_id, is_active)
```

FCM tokens rotate. Registration is upsert-by-token, and a `404 UNREGISTERED` from FCM
deactivates the row rather than retrying it forever.

### 3.4 Columns on an existing table

```
users
  agent_id    String(100), nullable
  UNIQUE (company_id, agent_id)
```

One column. `manager_id` and `agent_path` are **not** added — see the note at the top.

> ⚠️ **`agent_id` must also be added to `deps._load_user_row`**, which hand-writes its SELECT
> and is what every guard in the application reads. Add the column to the table but not to
> that query and `user.agent_id` raises `AttributeError` inside the agent guard — at request
> time, not at import. `auth.me` runs its own second SELECT and needs the same edit.

`agent_id` is deliberately free text, matching `phonebook_contacts.agent_id`, whose comment
already says: *"Free-text identifier from the external agent app — deliberately not a FK.
There is no agents table yet, and the id originates in another system."* That reasoning still
holds — the id is minted elsewhere. The unique constraint is per company so two companies can
both have an `AG01`.

### 3.5 Per-viewer read state

```
agent_thread_reads
  user_id   FK→users CASCADE      ┐ composite PK
  thread_id FK→agent_threads CASCADE ┘
  last_read_seq  Integer, default 0
  updated_at
```

Unread for any viewer is `thread.message_seq − COALESCE(last_read_seq, 0)` — computed, never
stored, and it reuses the per-thread `seq` from §8 rather than inventing a second ordering.

Keyed by user even though only one person reads a thread today. The row costs nothing, and it
is what lets a second viewer hold independent unread state later without the app's badge logic
changing at all.

### 3.6 Assignment history

```
agent_thread_assignments
  id, thread_id FK→agent_threads CASCADE
  from_user_id, to_user_id   FK→users SET NULL
  changed_by_user_id         FK→users SET NULL
  reason        String, nullable
  created_at
  INDEX (thread_id, created_at DESC)
```

Append-only. Reassignment still exists without a hierarchy — a thread lands in the unassigned
pool (§5 rule 4) and someone claims it, or an agent leaves and their threads move — and "who
moved this customer, and when" is precisely the question asked after something goes wrong, and
precisely the one that cannot be reconstructed later if it was never written down. The table
is tiny and never read on the hot path.

---

## 4. Agent identity

A new seeded role `agent`, plus `users.agent_id`. An agent is an ordinary company-scoped
user with `role_name = "agent"` and an `agent_id` — no new auth system.

The linkage chain, all pre-existing except the last hop:

```
phonebook_contacts.agent_id          (set at import)
        ↓ snapshotted at send — BROADCAST_DESIGN §11
broadcast_recipients.agent_id
        ↓ resolved when the customer replies — §5
agent_threads.assigned_agent_id
        ↓ matched at request time
users.agent_id   +   users.company_id
```

**Guard on every agent endpoint**, and it is two conditions, never one:

```python
thread.company_id == user.company_id and thread.assigned_agent_id == user.agent_id
```

Company scoping alone is not enough — that would let one agent read a colleague's threads.
Agent matching alone is not enough — `AG01` exists in more than one company.

### 4.1 Visibility — flat

Every agent sees exactly their own threads. The guard above **is** the visibility rule; there
is no second query and no scope parameter.

```sql
SELECT t.* FROM agent_threads t
WHERE t.company_id = :company_id
  AND t.assigned_agent_id = :my_agent_id
```

> ⚠️ **The §3.1 index is on `assigned_user_id`, but the query above filters on
> `assigned_agent_id`.** It will not be used. Either index `(company_id, assigned_agent_id,
> last_message_at DESC)` instead, or have the guard resolve the caller to their `users.id`
> once and filter on `assigned_user_id` — which is the better of the two, since it also makes
> threads whose agent id no longer resolves fall out of every agent's inbox rather than
> reappearing for whoever inherits that id later.

### 4.2 Two ownership columns, still

`assigned_agent_id` is the raw key that arrives from a phonebook or a CSV — it may name an
agent who has not been provisioned yet, or who no longer exists. `assigned_user_id` is that key
*resolved* to a real user row.

Keeping both matters even flat, because it is what makes the two failure modes
distinguishable:

| State | Means | Fix |
|---|---|---|
| `assigned_agent_id IS NULL` | **Nobody was ever named** | Needs a human decision |
| `assigned_agent_id` set, `assigned_user_id IS NULL` | **`AG07` was named, but no such active agent exists** | Usually a CSV typo, or an agent who left — fixed by creating the user |

Collapse them into one column and both become an indistinguishable NULL.

### 4.3 The unassigned pool

A reply from a number with no agent, or naming an `agent_id` that matches no active user,
leaves `assigned_user_id` NULL. Those threads are **not in the app**. They are visible
company-wide in the web panel to whoever holds `agent_inbox.create`, who assigns them to an
agent; from then on the normal rule applies.

Silently dropping such a reply means a customer talking to nobody, which is worse than a
thread the UI admits it could not route.

### 4.4 What v1 does not have

No manager view, no `?scope=team`, no reassignment from the app, no cross-agent visibility of
any kind. A supervisor who needs to see everything uses the web panel, where company-wide
access already exists.

### 4.5 One deliberate omission

There is **no** `users.manager_id` and **no** `users.agent_path`. Adding an unmaintained
materialised path "ready for later" is worse than adding nothing: nothing writes it, it drifts
from reality, and eventually something trusts it. When a hierarchy is wanted, both columns
arrive together with the code that maintains them.

### 4.6 Keeping the door open — three rules for the API contract

Cheap now, expensive after an app ships. None of these cost anything in v1.

1. **`GET /threads` returns "the threads visible to you", never "your threads".** If the
   contract promises the list can only contain the caller's own, a client will hardcode that
   assumption, and widening it later becomes a breaking change. Phrased loosely, adding
   `?scope=team` is purely additive.

2. **`sender_agent_id` ships on every message from day one.** In v1 it is always the thread's
   own agent on outbound and null on inbound, so it looks redundant. Document it as *who sent
   this*, have the app render it rather than assume, and the day a second person can reply
   into a thread nothing on the client changes.

3. **Per-viewer read state stays keyed by user** (§3.5), not folded into a counter on the
   thread.

Together these mean a hierarchy can be added as a server-side change plus one optional query
parameter, rather than a client rewrite.

---

## 5. Inbound routing — resolving which agent owns a reply

Inserted at `handle_inbound` step 6, where `service is None`.

```
inbound with no service
  ├─ 1. context.id matches broadcast_recipients.wamid?   → that recipient's agent   (exact)
  ├─ 2. existing open agent_thread for (company, mobile)? → its assigned_agent_id    (sticky)
  ├─ 3. most recent broadcast_recipient for (company, mobile)
  │       sent within N days?                             → that recipient's agent   (inferred)
  └─ 4. otherwise                                         → unassigned pool
```

**Why this order.** Meta attaches `context.id` when the customer uses WhatsApp's reply
affordance, which resolves the §11 ambiguity exactly — a number in two phonebooks with two
agents is disambiguated by *which message they actually replied to*. Most customers,
though, just open the chat and type, so no context arrives. Rule 2 keeps an ongoing
conversation with the same agent even after the window turns over. Rule 3 is the cold-start
inference; `N` defaults to 30 days.

Each rule yields an `agent_id`, which is then **resolved to a user**:

```python
owner = db.query(User).filter(
    User.company_id == account.company_id,
    User.agent_id   == resolved_agent_id,
    User.is_active.is_(True),
).first()
thread.assigned_agent_id = resolved_agent_id      # kept even if unresolvable
thread.assigned_user_id  = owner.id if owner else None
```

**Rule 4 must exist and must be visible.** A reply from a number with no agent, or an
`agent_id` matching no active user, leaves `assigned_user_id` NULL — the unassigned pool of
§4.3. Silently dropping it means a customer talking to nobody, which is worse than a message
the UI admits it could not route.

Note the two failure modes are distinguishable, and the UI should distinguish them:
`assigned_agent_id IS NULL` means *nobody was ever named*, while a non-null
`assigned_agent_id` with a null `assigned_user_id` means *`AG07` was named but no such active
agent exists* — usually a typo in a CSV, or an agent who left. The second is fixable by
creating the user; the first needs a human decision.

**Reassignment is allowed.** `assigned_agent_id` is mutable and the thread keeps its
history. §11's "the agent who sent it owns the reply" decides the *initial* owner, not a
permanent one — the agent who sent it may have left the company.

---

## 6. Agent reply — the send path

```
POST /agent-api/v1/threads/{id}/messages
  { client_msg_id: <uuid>, type: "text", body: "..." }
```

**Order of operations, and the order matters:**

1. Guard the thread (company + agent).
2. **Check the 24h window** (§7). Reject before writing anything if it has closed.
3. Insert `agent_messages` with `status = queued`, under the thread row lock (§8).
4. Commit. **The message is now durable, before any network call.**
5. Attempt `wa_sender.send_text` inline; on success record `wamid` + `status = sent`.
6. On failure leave it `queued` — the retry job (§10) picks it up.

**Inline send with a durable fallback**, matching the pattern `campaigns_api.py:393` already
uses (kick dispatch from the request, scheduler as the safety net). The agent is a human
waiting on a reply, so latency matters; but a Meta hiccup must not lose a typed message.

**`client_msg_id` is required, not optional.** Mobile networks retry. Without a
client-generated idempotency key, one flaky POST sends the customer the same message twice.
The unique index makes the retry a no-op that returns the original row.

---

## 7. The 24-hour window

```
window_open  ==  now < thread.last_inbound_at + 24h
```

Enforced in **three** places, and the middle one is the one that gets skipped:

1. **In the thread list and thread detail responses** — return `window_expires_at` so the app
   can show a countdown and disable the composer before the agent types a paragraph they
   cannot send.
2. **⚠️ At send time, server-side** — the app's check is a courtesy, not a guarantee. A phone
   with a stale thread list, a clock skew, or simply a slow thumb will POST after expiry.
   Reject with a distinct code (`window_closed`) the app can render as a specific state.
3. **On the status webhook** — Meta may still reject with `131047` (re-engagement required).
   Mark the message `failed` with that code so the thread shows what happened rather than a
   message that silently never arrived.

**When the window is closed**, the only legal move is an approved template. Offer the agent a
short list of the company's approved templates, send it as a template message, and note that
this **re-opens nothing** — only a customer reply re-opens the window.

### 7.1 The countdown contract with the app

The app renders a live timer, so every thread response carries three fields, not one:

```jsonc
{
  "last_inbound_at":    "2026-08-21T09:14:22Z",   // when the clock started
  "window_expires_at":  "2026-08-22T09:14:22Z",   // last_inbound_at + 24h
  "window_state":       "open",                   // open | closing_soon | closed
  "server_time":        "2026-08-21T14:02:07Z"    // ← see below
}
```

> ⚠️ **`server_time` is why the timer is correct.** The obvious implementation is
> `window_expires_at − System.currentTimeMillis()`, and it is wrong: Android device clocks
> drift, and users change them by hand. A phone forty minutes fast shows an agent forty
> minutes less than they really have — or worse, shows time remaining on a window that
> actually closed.
>
> Every response carries `server_time`. The app computes `offset = server_time − deviceNow`
> once per response and renders `window_expires_at − (deviceNow + offset)`. The countdown then
> ticks against *our* clock, on a device we do not control.

`window_state` is computed server-side rather than left to the app to derive from the
timestamps. Two reasons: the `closing_soon` threshold is a business rule that will get tuned
(suggest 2h), and every client that re-derives a rule eventually derives it differently.

**Timer behaviour:**

| State | App |
|---|---|
| `open` | Composer live, countdown shown quietly |
| `closing_soon` | Countdown emphasised, warn on entering the thread |
| `closed` | Composer disabled, template picker offered instead |

**The timer is advisory, always.** §7 point 2 stands: the server re-checks at send time and
returns `window_closed`. A phone can hold a stale thread list, sleep through the expiry, or
have been offline for the entire window. The countdown is there to stop an agent typing a
paragraph they cannot send — it is not the enforcement.

**The clock restarts on every inbound message**, not just the first. Any customer reply pushes
`last_inbound_at` forward and reopens a closed window, so the app should re-read these fields
on every sync rather than caching an expiry it computed earlier.

---

## 8. The sync protocol — why per-thread sequences

The app needs "what changed since I last looked", resumable, gap-free, over a mobile network
that drops.

### The trap: a global auto-increment cursor

The obvious design is a global `BIGSERIAL seq` on `agent_messages` with the client storing
the highest value it has seen. **It loses messages.** Sequence values are assigned at INSERT
but become visible at COMMIT, so a transaction holding `seq=100` can commit *after* one
holding `seq=101`. A client that syncs in between records `cursor=101` and never sees 100.
It is rare, it is silent, and it is a lost customer message.

### The fix: sequence per thread, assigned under the thread's row lock

Every message write already has to touch its thread row — `last_message_at`, `unread_count`,
`last_inbound_at`. Take that row lock, increment `agent_threads.message_seq`, and stamp it on
the message:

```python
thread = db.query(AgentThread).filter(...).with_for_update().first()
thread.message_seq += 1
msg.seq = thread.message_seq
```

The lock serialises writers **to the same thread**, so within a thread sequences are gap-free
and commit-ordered. Different threads never contend. The serialisation costs nothing because
the update was already required.

### The protocol

```
GET  /agent-api/v1/threads
     → [{ id, customer_name, mobile_no, last_message_at, message_seq,
           unread_count, window_expires_at }]

GET  /agent-api/v1/threads/{id}/messages?after_seq=<n>&limit=100
     → messages with seq > n, ascending
```

The app compares each thread's server `message_seq` against its local high-water mark and
pulls deltas only for threads that moved. An agent has a handful of customers, so the thread
list is a few rows — cheap enough to fetch on every wake-up.

**Backfill** uses the same endpoint with `before_seq` for scroll-back, so history and live
sync share one code path.

---

## 9. Push — the ping is disposable, the state is durable

Your instinct is right: **a data-less ping, and the app pulls.** Never put message content in
the push (payload limits, ordering, and privacy on a lock screen all argue against it).

But the offline design should be simpler than proposed. **Do not build a ping outbox.**

Tracking "ping not received, re-ping when they return" cannot actually be built: FCM does not
reliably tell you whether a device received a data message, so the outbox would be guessing.
And it is unnecessary — FCM already queues for offline devices, and more importantly:

> **Correctness never depends on the push.** The cursor is the contract. A push is a *hint*
> to sync sooner. Drop every push and the app still converges to the correct state.

The app therefore syncs on **four** triggers:

| Trigger | Covers |
|---|---|
| FCM data message | The fast path — seconds |
| App foreground | Every push missed while backgrounded or offline |
| Pull-to-refresh | The agent's own impatience |
| Periodic heartbeat (~15 min, foreground only) | A silently dropped FCM socket |

Foreground sync is what actually makes offline work. It needs no server-side state at all,
because the server already knows the truth and the cursor already knows the gap.

**Sending the push:**

```
inbound reply committed
  └─ thread.assigned_user_id → that user's active agent_devices rows
       └─ FCM data-only message, high priority
            collapse_key = thread_id     ← 10 rapid replies wake the app once, not ten times
```

**Only the assigned agent is pushed.** With one agent per thread that is the whole rule. It
is also the rule that must survive a hierarchy arriving later: fanning a push out to a
reporting chain scales in exactly the wrong direction — with a 500-recipient campaign running
at ~60 replies an hour, whoever sits at the top would be paged for every customer reply in the
company.

An unassigned thread pushes nobody. It is visible in the pool (§4.3), and that is the signal.

> Escalation — pushing someone else when a thread stays unanswered past a threshold — needs
> both a timer job and a policy for what counts as "answered", and it needs somewhere to
> escalate *to*. With no hierarchy there is no such destination, so it is out of scope until
> one exists.

FCM failures are logged, not retried into a queue. A `404 UNREGISTERED` deactivates the
device row. Anything else is covered by foreground sync.

**Ordering guarantee:** none is needed. The push carries no data, so pushes arriving out of
order, twice, or not at all are all equivalent — every one of them means "call sync".

---

## 10. Where this runs

**No new scheduler, no new container.** Two jobs added to the existing broadcast
`BackgroundScheduler` instance:

| Job | Interval | Purpose |
|---|---|---|
| `agent_outbound_retry` | 15s | Re-attempt `agent_messages` stuck in `queued` |
| existing `broadcast_send` / `broadcast_reclaim` | 3s / 60s | unchanged |

APScheduler runs distinct jobs concurrently within a scheduler's pool, so a long campaign
does not delay agent retries — and this avoids a fifth thread pool. §12 of the broadcast
design argued against a separate container on grounds that apply here unchanged: inbound
webhooks return to the main app regardless.

Retries use the same `SKIP LOCKED` claim and the same `claimed_at` staleness pattern the
broadcast scheduler now uses, so multi-instance stays a deployment change.

---

## 11. API surface

A separate router, `/agent-api/v1/*`, distinct from `/api/*` (web session/JWT) and
`/client-api/v1/*` (ISV clients). This follows the Lizo/Shirin precedent: **separate route
and contract layers over shared domain logic**, never URL-prefix branching inside shared
handlers.

```
POST /agent-api/v1/auth/login              → JWT, long-lived refresh for mobile
POST /agent-api/v1/auth/refresh
POST /agent-api/v1/devices                 → register/upsert FCM token
DELETE /agent-api/v1/devices/{token}       → logout

GET  /agent-api/v1/threads                 → the caller's threads + seqs + window state
GET  /agent-api/v1/threads/{id}/messages   → after_seq / before_seq paging
POST /agent-api/v1/threads/{id}/messages   → reply (client_msg_id required)
POST /agent-api/v1/threads/{id}/read       → advance last_read_seq for THIS user
GET  /agent-api/v1/media/{media_id}        → proxied download, never a raw Meta URL
```

Two endpoints are gone with the hierarchy: `GET /team` and `POST /threads/{id}/assign`, along
with the `?scope=` parameter. Claiming from the unassigned pool happens in the web panel
(§4.3).

Router-level guard `require("agent_inbox", "read")` plus the per-thread check from §4. Add
`agent_inbox` to `RESOURCES` so it appears in the permission matrix — `read` to use the inbox
at all, `write` to reply, `create` to claim from the unassigned pool in the web panel.

**Phrase the thread list as "threads visible to you"** — see §4.6 rule 1. It is the difference
between a hierarchy later being a server change and being a client rewrite.

**Media is proxied, never redirected.** Meta media URLs are short-lived and require the
access token; handing one to the app either leaks the token or breaks on expiry. `/media/{id}`
streams it after checking the caller owns the thread it belongs to.

> ⚠️ **This has to be built — `meta_media.py` is upload-only.** It has `upload_media`,
> `upload_resumable` and `validate_upload`; there is no download anywhere in the codebase.
> Inbound media is a two-step fetch: `GET /{media_id}` returns a short-lived URL, then that
> URL is fetched with the bearer token. Budget for it, and decide whether to cache the bytes
> or re-fetch per view — Meta expires the URL in minutes, so re-fetching means two round
> trips every time an agent scrolls past an image.

---

## 12. Changes to existing code — the complete list

Everything is additive. No transactional behaviour changes.

| File | Change | Risk |
|---|---|---|
| `conversation_engine.handle_inbound` | New branch at step 6 (§5) | Low — only the `service is None` path |
| `conversation_engine.handle_status` | Third fallback → `agent_messages.wamid` | Low — mirrors the §8 broadcast fallback |
| `column_mapper.campaign_fields()` | Optional `agent_id` column (§13) | Low — additive `FieldSpec` |
| `campaigns_api` personalised import | Persist mapped `agent_id` onto the recipient | Low |
| `models/user.py` | `agent_id` + `UNIQUE (company_id, agent_id)` | Migration |
| `core/deps.py::_load_user_row` | Add `agent_id` to the hand-written SELECT | Low — but silent if missed, see §3.4 |
| `api/auth.py::me` | Add `agent_id` to its own SELECT and to `MeResponse` | Low |
| `core/resources.py` | `agent_inbox` resource | None |
| `services/meta_media.py` | **New** download path — currently upload-only | Medium — new Meta call |
| `main.py` | Include the agent router | None |

`handle_status` gaining a third branch is worth a note: it currently tries `Message.wamid`,
then `broadcast_status.handle`. Agent replies are a third kind of outbound with a `wamid` and
no `Message` row, so they need the same treatment. Keep the chain ordered cheapest-first and
return on the first match.

---

## 13. Personalised broadcasts

Standard campaigns get `agent_id` from `phonebook_contacts` — already built, already
snapshotted at `campaigns_api.py:275`.

Personalised campaigns have no phonebook: the CSV *is* the list. So `campaign_fields()` gains
one more target:

```python
FieldSpec(
    "agent_id", "Agent ID", False,
    ["agent", "agent id", "agentid", "assigned to", "owner", "rep", "executive"],
    "Optional. Links each customer to the agent who will handle their replies.",
)
```

Optional, not required — a campaign nobody will reply to should not be blocked on it. Rows
without one route by §5 rule 3 or land unassigned.

Two validations at import, both reported in the existing skipped-rows report rather than
failing the file:

- `agent_id` matching no active agent user in this company → **warn, import anyway**. Agents
  get provisioned late; blocking the import helps nobody.
- The generated sample CSV includes the column, so nobody has to guess the header.

---

## 14. Build order

Sequenced so nothing can message a customer until step 6, matching the broadcast doc's
approach.

| # | Step | Sends? |
|---|---|---|
| 1 | Tables + `users.agent_id` + `agent` role + `agent_inbox` resource | ❌ |
| 2 | Inbound routing at step 6 → threads and messages appear | ❌ |
| 3 | Read APIs: threads, messages, paging | ❌ |
| 3b | Media download + proxy endpoint (new — see §11) | ❌ |
| 4 | Mobile auth + device registration | ❌ |
| 5 | 24h window computation, surfaced read-only | ❌ |
| 6 | Reply send path + retry job | ⚠️ **first real traffic** |
| 7 | FCM push + collapse keys | ⚠️ |
| 8 | Personalised CSV `agent_id` column | ⚠️ |
| 9 | Unassigned pool claiming in the web panel + assignment history (§4.3) | ❌ |

Steps 1–5 are read-only and testable against real replies to a real campaign without any risk
of messaging anyone. **A walking skeleton after step 5** — one campaign to your own number,
reply, watch the thread appear in the API — flushes out the routing rules while the blast
radius is one message.

Note that steps 1–3 deliver value on their own: replies stop vanishing at step 6 and become
visible, even before an app exists.

---

## 15. Open questions

1. **Can an agent start a conversation?** Not without a customer reply — the window is closed,
   so it would be a template send, billed as marketing/utility. Assumed **no** for v1.
2. **Unassigned pool ownership** — settled in principle (§4.3: holders of
   `agent_inbox.create`), but *which* existing role gets that action is still open. Suggest the
   company admin tier rather than a new `supervisor` role, unless one is wanted anyway.
3. **Inference window `N`** for §5 rule 3 — 30 days assumed. Longer risks routing an unrelated
   inbound to a stale agent.
4. **Media sending from the app** — deferred, but if agents will send images, `meta_media.py`
   needs an upload path and the app needs a size/type policy.
5. **Read receipts to Meta** — sending them shows the customer blue ticks. Nice, but it also
   commits you to the expectation that someone is reading. Assumed **on**, easily flipped.
6. **Thread closure** — manual, automatic on window expiry, or never? Assumed manual, since an
   auto-closed thread reopens on the next reply anyway.
7. **Do agents log in with a username?** `/api/auth/login` takes `username` only. Field staff
   on a phone typically expect a mobile number or an email. Changing it is a resolver at login
   rather than a schema change, but it is a contract decision the app is built around.
8. **Does the app implement a change-password screen?** `users.must_change_password` defaults
   to `True` and login succeeds while it is set — nothing blocks. Without that screen, every
   agent provisioned by an admin lands in the inbox still holding the temporary password
   somebody typed into a form.

---

## 16. Decisions log

| Decision | Rationale |
|---|---|
| Separate `agent_threads` / `agent_messages` tables | §8's precedent — keeps 500-recipient reply volume out of `conversations`/`messages` |
| One thread per (company, mobile), not per campaign | A customer is one conversation to the human answering them |
| Inbound writes both `Message` and `agent_messages`, one transaction | `Message.wamid` is the existing dedup authority and must keep working |
| Outbound agent replies write only `agent_messages` | Mirrors §8 — broadcast-class traffic stays out of transactional tables |
| `agent_id` free text, not a FK | Matches `phonebook_contacts.agent_id`; the id is minted in another system |
| Per-thread `seq` under the thread row lock | A global BIGSERIAL cursor silently loses messages committed out of order |
| Push is a hint; cursor is the contract | Correctness cannot depend on FCM delivery, which is unverifiable |
| No ping outbox | Cannot be built reliably, and foreground sync makes it unnecessary |
| 24h window enforced server-side at send | The client check is a courtesy; stale thread lists are normal |
| `client_msg_id` required on reply | Mobile networks retry; without it the customer gets doubles |
| Inline send + durable queue fallback | An agent is a human waiting; a Meta hiccup must not lose typed text |
| Two jobs on the existing broadcast scheduler | Avoids a fifth thread pool; §12's container reasoning is unchanged |
| Media proxied through us | Meta media URLs need the access token |
| Separate `/agent-api/v1/*` router | Lizo/Shirin precedent — separate contract layers, shared domain logic |
| `assigned_user_id` alongside `assigned_agent_id` | Keeps *nobody was named* distinguishable from *a name that resolves to no active agent* — collapsing them makes both an identical NULL |
| Unread per viewer, not per thread | Costs nothing now; the moment two people can open one thread a shared counter is wrong, and by then the app depends on it |
| Push only the assigned agent | The whole rule while threads have one owner, and the rule that must survive a hierarchy arriving |
| Assignment history append-only | "Who moved this customer" is unanswerable later if never written |
| **No hierarchy in v1** (2026-09-02) | The only part of the design not forced by the problem — a guess about the org, not a constraint |
| **No `agent_path` / `manager_id` columns at all** | An unmaintained materialised path drifts and is eventually trusted; worse than not having one |
| Thread list documented as "visible to you", not "yours" | Keeps a later hierarchy a server change plus one query parameter, not a client rewrite |
| `sender_agent_id` on every message from day one | Redundant in v1, free now, and the client needs no change when a second person can reply |
| `server_time` in every window response | Android clocks drift; a device-clock countdown misreports the window |
