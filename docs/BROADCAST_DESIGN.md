# Broadcast Campaign Messaging — System Design

**Status:** design agreed, nothing implemented.
**Date:** 2026-07-31
**Companion doc:** [`BROADCAST_TIPS.md`](./BROADCAST_TIPS.md) — Meta platform rules, limits,
tiers, per-user marketing limits and the verified API reference. **Read that first**; this
document assumes its constraints and does not repeat them.

---

## 1. Scope

Send an approved WhatsApp **marketing template** to many recipients at once, from a
company's own WhatsApp number, with delivery tracking and per-campaign insights.

**In scope now:** phonebooks, CSV import, campaign creation (two parameter modes),
pre-send screening, batched sending, delivery-status tracking, opt-out, invalid-number
flagging, campaign insights.

**Deliberately deferred:** agent routing of replies (see §11 — one decision cannot be
deferred), RBAC permissions, broadcast settings beyond defaults.

### Agreed scale

| | |
|---|---|
| PhoneBook size | ~1,000 today, **2,000 ceiling** |
| PhoneBooks per campaign | multiple |
| Realistic max campaign | ~5,000–10,000 recipients |
| Softland's daily tier | `TIER_10K` (10,000 unique / rolling 24h) |

Throughput is **not** a constraint at this scale: 2,000 sends at ~15 concurrent is roughly
30 seconds. Design pressure comes from compliance, correctness and shared budget — not speed.

---

## 2. Navigation

New sidebar entry **Broadcast campaign** → `/broadcast`, a card landing page:

| Card | Purpose |
|---|---|
| **PhoneBook** | Manage contact lists, manual add, CSV import |
| **New broadcast** | Create and send a campaign |
| **Broadcast settings** | Tier limits, defaults — deferred |
| **Agents** | Reply routing — deferred, see §11 |

---

## 3. Data model

### 3.1 PhoneBook

```
phonebooks
  id, company_id FK→companies CASCADE
  name
  created_by_id, created_at, updated_at
  UNIQUE (company_id, name)

phonebook_contacts
  id, phonebook_id FK→phonebooks CASCADE
  mobile_no        normalised, digits only        ← see §7
  customer_name
  email            nullable
  agent_id         nullable                        ← see §11
  created_at
  UNIQUE (phonebook_id, mobile_no)
```

One company → many lists. **The same number may appear in multiple lists**
(agreed) — hence uniqueness is per phonebook, not per company.

### 3.2 Campaign

```
broadcast_campaigns
  id, company_id FK→companies CASCADE
  name
  template_id FK→whatsapp_templates SET NULL
  param_mode              same | per_row          ← Case 1 / Case 2
  status                  draft | screening | sending | dispatched | settled | paused | cancelled
  shared_params           JSONB, Case 1 only
  total / sent / delivered / read / failed / skipped   denormalised counters
  dispatched_at, settled_at
  created_by_id, created_at
```

```
broadcast_recipients
  id, campaign_id FK→broadcast_campaigns CASCADE
  mobile_no        normalised
  customer_name
  agent_id         nullable, snapshotted at send  ← see §11
  params           JSONB — resolved, ready to send
  status           draft | pending | sending | sent | delivered | read
                   | failed | deferred | skipped
  skip_reason      opted_out | invalid_flagged | us_number | duplicate | validation
  wamid            nullable — Meta message id     ← see §8, load-bearing
  error_code, error_message
  retry_not_before nullable — set on 131049       ← see BROADCAST_TIPS §per-user limits
  sent_at, delivered_at, read_at
  INDEX (campaign_id, status)
  INDEX (wamid)
```

### 3.3 Opt-out and invalid numbers — two separate tables

```
messaging_optouts
  company_id FK→companies CASCADE
  mobile_no        normalised
  opted_out_at
  source           stop_keyword | optout_button | manual | api
  UNIQUE (company_id, mobile_no)

invalid_numbers
  company_id FK→companies CASCADE
  mobile_no        normalised
  error_code       e.g. 131026
  first_seen_at, last_seen_at, occurrences
  UNIQUE (company_id, mobile_no)
```

**Kept separate on purpose** — they behave differently:

| | Opt-out | Invalid number |
|---|---|---|
| Meaning | User chose to stop | Technical delivery failure |
| Lifecycle | Permanent until explicit opt-in | May become valid later |
| Screening | 🔴 **hard block — never send** | 🟠 warn, allow override |
| Source | STOP / opt-out button / manual | Meta error `131026` |

One table with a `type` column would tempt someone to treat them alike. They must not be:
opt-out is compliance, invalid is deliverability.

**Both are company-scoped, not global.** Consent belongs to a business — unsubscribing
from Softland must not unsubscribe you from Shirin Asal.

---

## 4. PhoneBook flow

### Manual add
Straightforward CRUD against `phonebook_contacts`.

### CSV import — ERPNext/Frappe-style column mapper

1. User uploads a CSV.
2. Dialog shows **our fields on the left**, a dropdown of **CSV column headers on the right**.
3. Backend pre-fills the mapping by fuzzy-matching header text to field names —
   normalise both sides (lowercase, strip spaces/underscores/punctuation), exact match
   first, then closest match. Python's stdlib `difflib.get_close_matches` is sufficient;
   no external dependency needed.
4. User corrects any mismatch and confirms.
5. Import runs with a progress bar, then reports results.

**Import can be synchronous at this scale** — 2,000 rows parse in well under a second.
Do not build in a way that assumes that forever.

### Import validation

| Rule | Behaviour |
|---|---|
| Phone format | `_validate_mobile` — **7–15 digits**, strips `+` (§7) |
| Duplicate within the CSV | **Skip the row, report it** (agreed) |
| Duplicate already in this phonebook | **Skip the row, report it** (agreed) |
| Missing required field | Skip the row, report it |

Skipped rows appear in a final import report with the row number and reason. Nothing is
silently dropped.

---

## 5. Campaign creation

Select **company** → select **template** (filtered to that company, `status = APPROVED`)
→ preview the template with its parameters.

### Case 1 — same parameters for everyone (`param_mode = same`)

Select one or more phonebooks, type the parameter values once. Every recipient gets them.

### Case 2 — different parameters per recipient (`param_mode = per_row`)

No phonebook — **the CSV is the list**. Offer a **downloadable sample CSV generated from
the selected template**, with one column per parameter, so the user cannot guess wrong.

Same mapper dialog as §4. Offer *"also save these contacts to a phonebook"* as an option,
so a good list is not lost after the campaign.

### Both modes converge on one data model

Case 1 is Case 2 with identical values. Both write `broadcast_recipients` rows with
**resolved** `params`. The send worker never knows which mode created them.

> Two UI paths, one pipeline. Building two send paths would mean two sets of bugs, two
> validation paths, two things to keep in sync.

### The preview table *is* the recipient table

Import writes rows with `status = draft`. The verification table the user reviews is a
paginated query over those same rows. Confirming flips `draft → pending`.

No temp structure, no in-memory parse, no handoff. Benefits: pagination is plain SQL, the
user can leave and return, per-row validation errors persist, and the worker reads exactly
what the user approved.

### Rules at campaign build

- **Template is campaign-level, never per row.** Two templates = two campaigns.
  Per-row templates would break the preview, param-count validation and the sample CSV.
- **Deduplicate across phonebooks.** A number in phonebook A *and* B is sent **once** —
  Meta counts unique recipients, and sending twice wastes budget and irritates the customer.
- **Validate parameter count** against the template at import time, not send time.
- **Campaign becomes immutable once dispatched.**
- **Double-click protection on Send.**

---

## 6. Pre-send screening

Runs before the confirmation dialog. Every check below is backed by a verified source —
see `BROADCAST_TIPS.md` §4 for the APIs.

| Check | Source | Action |
|---|---|---|
| Messaging tier + remaining budget | `whatsapp_business_manager_messaging_limit` + our sliding 24h count | Block if campaign exceeds remaining |
| Quality rating | `quality_rating` | Warn on YELLOW, strongly warn on RED |
| Can we send at all | `health_status.can_send_message` | Block if not `AVAILABLE` |
| **US numbers** (`+1`) | recipient list | 🚩 **Hard exclude** — Meta never delivers marketing to US numbers |
| Opted out | `messaging_optouts` | 🔴 **Hard skip** |
| Previously invalid | `invalid_numbers` | 🟠 Warn, allow override |
| Duplicates across phonebooks | recipient list | Auto-deduped, reported |

Present as a summary: *"4,812 will be sent. 142 unsubscribed, 37 previously invalid,
9 US numbers excluded. You have 6,200 of 10,000 remaining today."*

---

## 7. Phone number normalisation — one rule everywhere

**Always `_validate_mobile` from `app/schemas/service.py`.**

```python
_E164_RE = re.compile(r"^\+?[1-9]\d{6,14}$")     # 7–15 digits
# validates, then strips leading '+'
```

> ⚠️ **There is no 12-digit limit at Meta.** An earlier draft of this requirement capped
> numbers at 12 digits. E.164 allows up to 15; UK mobiles are 12–13 with country code.
> A 12-digit cap would reject valid numbers *and* accept 15-digit garbage.

Applies to **every** table storing a number: `phonebook_contacts`, `broadcast_recipients`,
`messaging_optouts`, `invalid_numbers`.

> 🚩 **This is the bug class that cost service `FACO050010`.** `+918136995390` and
> `918136995390` were treated as different numbers and inbound replies routed to the wrong
> conversation. If an opt-out is stored with a `+` and the phonebook without, the anti-join
> misses and **we send to someone who unsubscribed.** Same function, same rule, no exceptions.

---

## 8. Delivery status tracking 🔴 *load-bearing*

**Decision: broadcast does NOT create `Conversation` or `Message` rows** (agreed — keeps
2k campaigns out of the transactional tables).

**Consequence that must be handled:** `conversation_engine.handle_status()` currently finds
its target by `Message.wamid`:

```python
msg = db.query(Message).filter(Message.wamid == wamid).first()
if not msg:
    return          # ← silently dropped
```

`Message.conversation_id` is NOT NULL, so no conversation means no Message — which means
**every delivery receipt for a broadcast would be discarded**: no delivered counts, no read
counts, and no invalid-number detection.

**Fix:** store `wamid` on `broadcast_recipients`, and give `handle_status` a fallback —
if no `Message` matches, look up `broadcast_recipients` and update that instead.

This keeps broadcast fully out of the conversation tables *and* captures every status.

**Accepted asymmetry:** if a recipient *replies*, that inbound message goes through
`handle_inbound` and **will** create a Conversation, as all inbound does today. Outbound
stays out; inbound does not. That is fine now and is what agent routing will need later.

### Dispatch is not settlement

A campaign has **two** completion states:

| State | Meaning | Timing |
|---|---|---|
| `dispatched` | Every send attempted and accepted by Meta | Seconds–minutes |
| `settled` | All status receipts in, or the deadline passed | Minutes–hours |

Meta frequently accepts a send and reports failure **later** via an async status webhook —
this is exactly how `FACSRP050002` behaved: `result.ok = True`, then `failed` + `131026`
two seconds later.

**So success/failure numbers keep moving after the progress bar reaches 100%.**

- The progress bar tracks **dispatch** — that is what it honestly measures.
- Below it, a live results panel labelled *"Delivery results still arriving"* until settled.
- A **settle deadline** (24h suggested); un-receipted recipients then become `unknown`
  rather than counted as success.
- Use the existing SSE infrastructure (`app/api/sse_api.py`, `StreamingResponse`) for live
  updates rather than polling.

---

## 9. Opt-out

### Two mechanisms, use both

1. **Meta's native Marketing opt-out button** — added as a quick-reply when creating the
   marketing template. Tapping it delivers a normal inbound `button` message to our webhook.
   This is the recommended, most visible path.
2. **`STOP` / `UNSUBSCRIBE` / `CANCEL` keyword** — not Meta-native; every BSP implements
   this themselves.

Both arrive through `handle_inbound`, so enforcement is one check at the top of it:

```
inbound message
  ├─ opt-out button payload?              → upsert messaging_optouts, stop
  ├─ text in {STOP, UNSUBSCRIBE, CANCEL}? → upsert messaging_optouts, stop
  └─ otherwise                            → existing service routing (unchanged)
```

### Three enforcement points — the third is the one that gets missed

1. **On inbound** — record the opt-out.
2. **At pre-screening** — anti-join, so the user sees the count before confirming.
3. **⚠️ At claim time in the worker** — **re-check immediately before each send.**

Screening is a snapshot. A campaign runs for minutes; someone can text STOP during it. Only
checking at screening means sending to a person who unsubscribed sixty seconds earlier —
precisely the case a customer or regulator notices. The worker already claims batches, so
this is one extra condition on the claim query.

### Behaviour

- **Opted-out contacts are kept and marked**, never auto-removed (agreed). Removing loses
  the record they existed and they would be re-added by the next import of the same list.
- PhoneBook UI shows opt-out status as a **join at display time**, never a denormalised
  column — that reintroduces the staleness problem this design exists to avoid.
- **Replying is not re-subscription.** Opting back in requires explicit action. Provide a
  manual "remove opt-out" in the UI, recorded with `source = manual`.
- Meta requires stopping **immediately** on opt-out; failure risks spam reports, which
  damage quality rating.

---

## 10. Sending

### Worker loop — batch and observe

```
claim batch (FOR UPDATE SKIP LOCKED, ~100)
  ├─ re-check opt-out                → skipped (opted_out)
  ├─ check portfolio budget          → pause campaign if exhausted
  ├─ send concurrently (semaphore ~15)
  ├─ store wamid on each recipient
  └─ PAUSE and observe:
       batch failure rate over threshold?  → auto-pause
       quality rating dropped?             → auto-pause
       else                                → next batch
```

**Batch even at 2k.** Not for throughput — for the observation gap. A bad list is caught
after 100 sends instead of 2,000.

### Error handling

| Outcome | Recipient status | Then |
|---|---|---|
| Sent OK | `sent` + `wamid` | Await status receipts |
| `131026` invalid number | `failed` | Record in `invalid_numbers` |
| **`131049`** per-user marketing limit | **`deferred`** | `retry_not_before = now + 24h`. **Never retry sooner** — repeated attempts trigger a per-user delivery suspension. **Exclude from failure-rate auto-pause** — it is a throttle, not a bad number, and would false-trip the pause |
| Other error | `failed` | Record code + message |

### Budget exhaustion

Pause the campaign and resume automatically as capacity ages back in. The messaging limit
is a **moving 24-hour window**, not a daily reset — capacity returns gradually, mirroring
how it was spent, so a paused campaign self-resumes. See `BROADCAST_TIPS.md` §2.

---

## 11. Agents — deferred, but one decision cannot be

Planned: replies to broadcasts are routed to an agent using a mobile app. The app holds no
database — it only issues GET/POST against our backend, so all state lives here.

**The decision that must be made now:** a number can be in **two phonebooks with different
`agent_id`s**. When they reply, which agent owns it?

**Snapshot `agent_id` onto `broadcast_recipients` at send time.** The agent who sent it owns
the reply. Unambiguous, and it survives the contact being edited afterwards. Looking the
agent up at reply time is ambiguous by construction.

Costs one column now. Retrofitting later means backfilling from data that no longer has a
single right answer.

---

## 12. Architecture

### Decision (2026-08-04): in-process fourth scheduler, not a separate container

An earlier draft of this section recommended a separate `worker` container. **Reversed**
after checking the code rather than reasoning from the usual assumptions. The two arguments
that normally justify a worker container do not hold here, and the container would not
isolate the thing that actually contends.

**Starvation is already handled.** Each scheduler is its own `BackgroundScheduler` instance:

```
expiry_scheduler.py:32   scheduler = BackgroundScheduler(timezone="UTC")
notify_scheduler.py:25   scheduler = BackgroundScheduler(timezone="UTC")
send_scheduler.py:30     scheduler = BackgroundScheduler(timezone="UTC")
```

Separate instances mean separate thread pools. A broadcast scheduler is a fourth
independent one, so a long-running campaign job **cannot** block `send_scheduler` from
dispatching a transactional order confirmation. This was the main concern and the existing
architecture already answers it.

**Throughput was never the constraint** — see "Agreed scale" in §1: 2,000 sends at ~15
concurrent is about 30 seconds.

**The container would not isolate the contention point.** Broadcast status receipts — every
`delivered`, `read`, and the async `131026` that drives invalid-number flagging — arrive at
`POST /webhook/meta` on the **main app**, because Meta allows one callback URL per app. A
worker container splits *sending* while *receiving* stays on the main app's pool. The
contention point is unchanged, in exchange for a second deploy unit, a second log stream and
a second thing to monitor.

### The real constraint — the DB pool, and it is one line

Measured at runtime:

```
pool class    : QueuePool
pool_size     : 5
max_overflow  : 10
total ceiling : 15
```

`app/core/database.py` calls `create_engine(settings.DATABASE_URL, pool_pre_ping=True)` with
no sizing, so those are SQLAlchemy defaults. Fifteen connections are shared by uvicorn, three
schedulers, and every webhook `BackgroundTask` (each opens its own `SessionLocal()`).

**Fix this regardless of the container decision — it helps the existing pipeline too.** Set
`pool_size=20, max_overflow=10`; Postgres defaults to 100 connections. Then bound broadcast's
own send concurrency with a semaphore so a misbehaving campaign cannot consume the pool.

### Requirement that keeps the container option open

Write the claim loop **as if it were multi-instance from day one**, using the pattern
`send_scheduler.py:96` already uses:

```python
.with_for_update(skip_locked=True)
```

Get this right and moving broadcast into its own container later is a *deployment* change —
the same module, a different entrypoint, safely claiming rows alongside anything else.
Get it wrong and the split becomes a refactor under pressure.

**Gotcha for whenever the split does happen:** `main.py`'s lifespan starts `send_scheduler`,
`expiry_scheduler` and `notify_scheduler`. A worker must **not** boot through `main.py`, or
all three would run twice. Give it its own entrypoint.

### Revisit the container when

- Campaigns consistently exceed ~10k recipients
- Pool contention or connection timeouts show up in the logs
- Broadcast needs to be deployed or restarted without touching the live transactional path —
  the most likely trigger, and a legitimate operational reason on its own

### What no architecture choice isolates

- **The Meta quota** — per business portfolio, shared with transactional. Isolation must be
  cooperative (a reserved budget), not architectural. See §6 and §14 q2.
- **Inbound webhooks** — as above, they always return to the main app.

---

## 13. Build order

Sequenced so that **nothing can message a real customer until step 5**.

| # | Step | Sends? |
|---|---|---|
| 1 | PhoneBook — model, CRUD, manual add | ❌ |
| 2 | CSV import + mapper dialog + validation | ❌ |
| 3 | Campaign model + Case 1 + preview table | ❌ |
| 4 | Opt-out table + inbound STOP/button handling | ❌ |
| 5 | Pre-send screening | ❌ |
| 6 | Worker + send loop | ⚠️ **first real traffic** |
| 7 | Status handling — `wamid` fallback, invalid flagging | ⚠️ |
| 8 | Case 2 — per-row CSV (reuses 2 and 3) | ⚠️ |
| 9 | Campaign insights | ❌ |

Steps 1–5 are roughly half the work. By the time step 6 runs, opt-out, budget guards and
the US filter are already in place and tested.

A **walking skeleton** after step 5 is worth it — one campaign, one recipient, your own
number, end to end — to flush out integration problems while the blast radius is one message.

---

## 14. Open questions

1. **Does the opt-out button require recreating the template?** Softland has only **one**
   approved MARKETING template. If the button must be part of the template, that is another
   Meta approval cycle — check before planning timelines around it.
2. **Reserved transactional budget** — what share of the 10,000/day is off-limits to
   broadcast? Suggested start: broadcast capped at 60%, tuned on observed volume.
3. **Settle deadline** — 24h suggested. After it, un-receipted recipients become `unknown`.
4. **Invalid-number flag expiry** — should a `131026` from six months ago still warn?
   Suggest showing age rather than expiring silently.
5. **RBAC** — deferred, but decide before launch whether campaigns are super-admin only.

---

## 15. Decisions log

| Decision | Rationale |
|---|---|
| PhoneBook contacts unique per phonebook, not per company | A number may legitimately live in several lists |
| Duplicates skipped and reported, not merged | Agreed; keeps import predictable |
| One campaign may target several phonebooks | Agreed; deduplicated at build |
| Template campaign-level, never per row | Per-row breaks preview, validation and sample CSV |
| Case 1 and Case 2 share one data model | Two pipelines would mean two sets of bugs |
| Preview table *is* the recipient table | Pagination, persistence, no handoff |
| No Conversation/Message rows for broadcast | Keeps transactional tables clean — **requires the `wamid` fallback in §8** |
| Opt-out in its own company-scoped table | Survives multi-phonebook membership and Case 2 CSVs |
| Opted-out contacts kept and marked | Removing loses history and they return on re-import |
| Opt-out and invalid kept as separate tables | Compliance vs deliverability — must not be treated alike |
| `agent_id` snapshotted onto the recipient | A number in two phonebooks has two agents; sender wins |
| `_validate_mobile` everywhere, no 12-digit cap | No such Meta limit; E.164 is 7–15 digits |
| ~~Separate worker container~~ → **in-process fourth scheduler** (2026-08-04) | Schedulers already have separate thread pools, so no starvation; a container would not isolate inbound status receipts, which always return to the main app. §12 |
| Raise the DB pool from the default 15 | The actual contention point, and it constrains the existing pipeline too — independent of the container decision |
| Claim rows with `SKIP LOCKED` from day one | Makes a later container split a deployment change rather than a rewrite |
