# Broadcast Campaign Messaging — Research & Design Notes

**Status:** research / design discussion. Nothing implemented.
**Probed:** 2026-07-30, live Meta Graph API `v22.0`, tokens from the connected accounts.
**Docs checked:** 2026-07-30 against Meta for Developers primary documentation
(Messaging Limits · Per-user marketing template message limits · Upcoming messaging
limits changes). Quoted wording is Meta's own.

### How to read this document

| Marker | Meaning |
|---|---|
| ✅ *verified — Meta docs* | Taken from Meta's official documentation, quoted where possible |
| 📊 *measured* | Read from the live Graph API against our own accounts |
| ⚠️ *unverified* | Third-party reporting only — **not** found in Meta's docs |
| ⚠️ **Correction** | An earlier draft of this document was wrong; the correction follows |

Third-party blogs disagreed with Meta's documentation on several points during this
research. Where they conflict, **Meta's docs win** and the blog claim is marked unverified.

> **Meta changes these rules regularly** — several changed in October 2025 alone. The
> *model* (unique recipients, moving window, tiers) is stable; the *numbers and field
> names* are not. Re-verify before hard-coding any threshold.

---

## 1. Live account facts  📊 *measured*

Read from the live Graph API, not assumed — via `GET /{phone_number_id}` and `GET /{waba_id}`.

| | **Softland India Ltd** (ours) | **Shirin Asal** (client) |
|---|---|---|
| Number | +91 99950 38305 | +971 52 306 3549 |
| `phone_number_id` | `1156947457499664` | `1230384960155351` |
| `waba_id` | `995511569886523` | `2217150472159129` |
| **Business Portfolio** | **`3294019020636466`** | **`279169304106222`** |
| **Messaging tier** | **`TIER_10K`** (10,000 / 24h) | **`TIER_250`** (250 / 24h) |
| Quality rating | GREEN | GREEN |
| Business verification | **verified** | **not_verified** |
| Account review | APPROVED | APPROVED |
| Ownership | SELF | CLIENT_OWNED |
| Throughput | STANDARD (~80 msg/s) | STANDARD |
| Status / mode | CONNECTED / LIVE | CONNECTED / LIVE |
| Templates | 6 approved: **5 UTILITY, 1 MARKETING** | — |

**The portfolios are different.** Shirin's 250 cap and Softland's 10K are independent
pools; neither can consume the other's budget.

Shirin Asal sits at the 250 default, with `business_verification_status: not_verified`.
Verification is one route off 250 — and since her WABA is `CLIENT_OWNED`, only she can
complete it — but it is **not the only route**: the volume path (2,000 delivered messages
to unique numbers over a 30-day moving period) also works. See §2 "How you climb".

### Known non-issues
- `code_verification_status: EXPIRED` on both numbers — both are CONNECTED and LIVE.
- The health probe warns *"app is not subscribed to the message webhook"*. Confirmed to
  be a **local/dev artifact only** — webhooks are subscribed in production.

---

## 2. How messaging limits actually work

### The counting rule ✅ *verified — Meta docs*

The limit counts **how many different people you start a conversation with in 24 hours.**
Not messages — people.

- 1 message to 500 people → **500** against the limit
- 20 messages to 1 person → **1**

| Situation | Counts? |
|---|---|
| We message a customer first (template) | ✅ uses a slot |
| Customer messages us first, we reply | ❌ free |

A customer messaging us opens a 24-hour window where replies are free. This is why the
existing transactional flow is cheap: one template to start (1 slot), then the whole
Q&A runs inside that window at no further cost.

### Rolling, not daily — and it replenishes continuously ✅ *verified — Meta docs*

**There is no global reset and no "wait 24 hours to send again".** Each send consumes one
slot, and that slot returns **24 hours after that individual send**. Capacity comes back
gradually, mirroring the shape of how it was spent.

| Time | Action | Used | Available (of 10,000) |
|---|---|---|---|
| Mon 09:00 | send 4,000 | 4,000 | 6,000 |
| Mon 13:00 | send 6,000 | 10,000 | **0 — blocked** |
| Tue 09:00 | Mon-09:00 batch ages out | 6,000 | **4,000** |
| Tue 13:00 | Mon-13:00 batch ages out | 0 | **10,000** |

So a limit drained gradually from 09:00–17:00 starts refilling gradually from 09:00 the
next morning. You are only fully blocked for a long stretch if the whole allowance was
spent in one burst.

**Design consequences:**

- The budget check must be a **sliding** query — `WHERE created_at > now() - interval '24 hours'`.
  Never `WHERE created_at::date = CURRENT_DATE`: that models a midnight reset which does
  not exist, and would both over-block (refusing sends when slots have already freed) and
  under-block (letting a burst through just after midnight).
- **A paused campaign can auto-resume.** The worker re-checks budget each batch and
  continues as slots age out. No manual restart needed.

> **Caveat — sources differ slightly.** Bloomreach describes the window as resetting
> "from the first message sent in that period", which reads like a fixed window anchored
> to the first send; Meta's docs say "moving 24-hour period" and others describe pure
> continuous replenishment. Implementing as a **sliding count over the last 24h** is
> correct under the continuous reading and conservative under the anchored one — safe
> either way.

### The tier ladder ✅ *verified — Meta docs*

| Tier | Unique recipients / 24h |
|---|---|
| Default | 250 |
| — | **2,000** |
| — | 10,000 ← **Softland is here** |
| — | 100,000 |
| — | Unlimited |

> ⚠️ **Correction:** an earlier draft of this document listed 1,000 as the second rung.
> Meta's documentation states **2,000**. There is no 1,000 tier.

### How you climb ✅ *verified — Meta docs*

**Getting to 2,000 — three alternative paths, any one suffices:**

1. Verify your business, **or**
2. Have your **partner** verify your business (if onboarded by one), **or**
3. *"Send 2,000 delivered messages outside of customer service windows to unique WhatsApp
   user phone numbers within a 30-day moving period, **using templates with a high quality
   rating**"*

> ⚠️ **Correction:** an earlier draft said business verification was the *only* way off
> 250. It is not — the volume path (#3) works without it. At 250/day the ceiling over 30
> days is ~7,500, so 2,000 delivered is achievable. **This matters for Shirin Asal:** she
> can reach 2,000 on normal transactional volume alone. Note the quality qualifier —
> low-quality templates do not count toward the 2,000.

Completing a path is not automatic approval: Meta then analyses message quality and either
**approves** or **denies** eligibility for automatic scaling.

**On approval** — limit jumps to 2,000 immediately, plus email + developer alert, plus a
**`business_capability_update`** webhook carrying the new limit:

| Webhook version | Field |
|---|---|
| v24.0 and newer | `max_daily_conversations_per_business` |
| v23.0 and older | `max_daily_conversation_per_phone` — **removed February 2026** |

**On denial** — limit unchanged, plus an **`account_alerts`** webhook whose `alert_type`
states the remedy:

| `alert_type` | Remedy |
|---|---|
| `INCREASED_CAPABILITIES_ELIGIBILITY_DEFERRED` | Send 2,000 delivered messages to unique numbers in 30 days using high-quality templates |
| `INCREASED_CAPABILITIES_ELIGIBILITY_FAILED` | Same as above |
| `INCREASED_CAPABILITIES_ELIGIBILITY_NEED_MORE_INFO` | Verify identity, **or** the 2,000-message path |

**Beyond 2,000 — automatic scaling.** Both criteria must hold:

1. High-quality messages **across all business phone numbers and templates** in the portfolio
2. In the last 7 days, at least **half** the current limit was used

→ limit increases **one level within 6 hours**. No request, no button.

> **Note for us:** criterion 2 means an unused limit never grows. Softland sits at 10,000;
> reaching 100,000 requires sustaining **5,000+ unique recipients per day**. Worth knowing
> before assuming headroom will appear on its own.

---

## 3. What changed on 2025-10-07 ✅ *verified*

Five changes, all effective 2025-10-07 and already live. Older guides — and the first
draft of these notes — are wrong on several.

**a. Limits are per Business Portfolio, not per phone number.** ✅ *verified*
Meta: *"Messaging limits are calculated and set at the business portfolio level and are
shared by all business phone numbers within a portfolio."* Effective 2025-10-07. Adding phone numbers to a portfolio does **not** add capacity —
they share one pool. Existing portfolios inherited the highest limit among their numbers.

**b. Quality drops no longer downgrade your tier.** ✅ *verified — stated explicitly*
Meta: messaging limits *"will not be downgraded"* for quality rating drops, and the old
*"Flagged"* quality state is *"no longer possible."* Quality now only gates whether you
can *climb*. Blocks and reports still hurt deliverability and reputation — this is not a
licence to spam.

**c. `messaging_limit_tier` is deprecated.** ✅ *verified — confirmed against our own accounts*
Use **`whatsapp_business_manager_messaging_limit`**. This is exactly why Softland
returned no tier at first — a deprecated field, not an account quirk. Shirin still
returns the old field, so read the new one and fall back to the old.

**d. Quality is assessed across the whole portfolio.** ✅ *verified*
Automatic scaling now requires *"sending high-quality messages across all of your business
phone numbers and templates"* — not per-number quality ratings. **A portfolio is a
shared-fate pool.** See §3.1 below.

**e. Newly registered numbers inherit the portfolio limit.** ✅ *verified*
Meta: newly registered numbers *"share the messaging limit set on the business portfolio"*
— they no longer start at 250. Existing portfolios were set at the Oct-7 cutover to
*"the highest limit of any phone number within their portfolio"* (almost certainly how
Softland arrived at `TIER_10K`).

### 3.1 Portfolio placement is a business decision, not just config

Because a new number inherits the portfolio's limit **and** quality is judged across the
whole portfolio, where a client's number lives has real consequences:

| | Client inside **Softland's** portfolio | Client in **their own** portfolio |
|---|---|---|
| Starting limit | Inherits **10,000/day** immediately | Starts at **250**, must climb |
| Onboarding friction | None | Verification, partner verification, or 2,000-in-30-days |
| Risk | One bad sender degrades scaling for **every** number in the portfolio | Fully isolated |
| Current example | — | Shirin Asal (`CLIENT_OWNED`, portfolio `279169304106222`) |

Shirin sits at 250 because her WABA is client-owned and in her own portfolio. Inside
Softland's portfolio she would have started at 10,000 — but her sending quality would then
have counted against Softland's scaling, and vice versa.

No recommendation here — it is a commercial/risk call, not a technical one. But it should
be a **deliberate** choice at onboarding rather than a side effect of how the WABA was
created.

### 3.2 Throughput ceiling ✅ *verified*

Above `STANDARD` there is a **1,000 messages/second** throughput tier. Eligibility requires
messaging *"100K or more unique WhatsApp user phone numbers, outside of a customer service
window, within a moving 24-hour period"*, with automatic upgrade within 12 hours. Far
beyond current scale — noted only so the ceiling is known.

### Per-user marketing limits ✅ *verified — Meta docs*

Separate from, and **on top of**, the portfolio limit. This caps how many marketing
templates *one person* receives from **any** business — not just from us.

- **There is no fixed number.** Meta: *"The per-user marketing limit adapts automatically
  over time based on a person's recent engagement levels."* Inputs include *"a dynamic
  view of an individual's recent marketing message read rate and how many messages they
  currently have in their inbox from friends, family, and businesses."*
- *"Each marketing template message delivered counts towards the per-user marketing limit."*
- If the user **replies**, that opens a 24-hour customer service window, and marketing
  messages sent **inside that window do not count** toward the limit.
- Meta's framing: this may mean *"delivering fewer messages to some WhatsApp users during
  periods of lower marketing read rates"*, but *"your ability to reach people when they are
  most engaged does not change."*

> ⚠️ **Correction:** an earlier draft stated a fixed cap of "2 marketing templates per
> user per 24h". That came from third-party blogs and is **not** in Meta's documentation.
> The real limit is dynamic and engagement-based.

#### 🚩 Geographic restrictions — check this before building any list

**United States numbers are blocked entirely for marketing.**
Meta: *"WhatsApp does not currently deliver marketing template messages to WhatsApp users
with United States phone numbers (numbers composed of a +1 dialing code and a US area
code). Attempting to send a template message to a WhatsApp user with a US phone number
after this date will result in an error."*

→ **Filter `+1` US numbers out of marketing campaigns at list-import time.** Sending them
is guaranteed failure, wastes budget, and pollutes delivery reporting.

**Countries where per-user limits do NOT apply:** EEA, UK, Japan, South Korea — for a
business phone number *in* those regions, or to a user *in* those regions.

| Our number | Country | Per-user limits apply? |
|---|---|---|
| Softland `+91 99950 38305` | India | **Yes** |
| Shirin Asal `+971 52 306 3549` | UAE | **Yes** |

Neither of our numbers is in an excluded region, so per-user throttling is live for both.

#### Retry rules — getting this wrong causes a 24h suspension

- Wait **at least 24 hours** before resending to a user who hit their limit.
- Meta: resending earlier *"will likely result in additional error responses and can
  reduce the accuracy of campaign delivery reporting."*
- Repeated retries inside 24h → *"further delivery attempts to these users may be
  unavailable for up to 24 hours"* with error `131049`.
- Crucially: a suspension is **per-user, not account-wide** — *"This does not affect your
  ability to send marketing messages to other users."*

#### Error `131049` — arrives via the messages webhook

Meta: the messages webhook is triggered with **status `failed`** and **error code
`131049`**, both when the per-user limit blocks delivery and when retries have been
excessive.

**This lands in code we already have.** It arrives through the same status webhook path as
every other delivery failure — `meta_webhook.py` → `conversation_engine.handle_status()`,
the function extended on 2026-07-30 to read `status.errors[]`. Broadcast needs to branch
on `131049` there rather than build a separate error path.

**Design consequences:**

1. We **cannot pre-compute** whether a recipient is over their personal limit — only Meta
   knows. It must be handled reactively.
2. Treat `131049` as its own recipient outcome (`deferred`), **never** as a generic send
   failure. Generic retry logic would trigger the 24h suspension.
3. Store a `retry_not_before` timestamp of now + 24h on that recipient.
4. Exclude `131049` from campaign failure-rate calculations — it is a throttle, not a bad
   number, and counting it would falsely trip an auto-pause.

### ⚠️ Unverified — treat with caution

**"Portfolio Pacing"** — several third-party articles describe Meta automatically
splitting broadcasts into batches, observing block/report rates, and releasing subsequent
batches only if clean. **This is not stated in the Meta documentation reviewed.** The
batch-and-observe design below is still worth doing on its own merits (it limits damage
from a bad list), but do not assume Meta is also pacing on our behalf.

**"2K and 10K tiers being removed in 2026, verified businesses jumping straight to 100K"**
— reported by third parties. Meta's current messaging-limits documentation still lists
2,000 and 10,000 as live tiers, Softland measures as `TIER_10K`, and Meta's *Upcoming
messaging limits changes* page covers only the 2025-10-07 changes with no mention of tier
removal. **Unconfirmed.** It may be a staged rollout that has not reached this portfolio,
or it may be wrong.

---

## 4. API reference

### Tier, quality, health (per phone number)

```
GET https://graph.facebook.com/v22.0/{phone_number_id}
    ?fields=whatsapp_business_manager_messaging_limit,quality_rating,health_status,
            throughput,status,account_mode,display_phone_number,verified_name,
            code_verification_status,name_status,is_official_business_account
    &access_token={token}
```

- `whatsapp_business_manager_messaging_limit` → `TIER_250` | `TIER_2K` | `TIER_10K` | `TIER_100K` | `TIER_UNLIMITED`
  (Meta's own example response returns `"TIER_250"`. Note the ladder has **no `TIER_1K`**.)
- `quality_rating` → `GREEN` | `YELLOW` | `RED`
- `health_status` → per-entity `can_send_message` for PHONE_NUMBER / WABA / BUSINESS / APP,
  with actionable `errors[]`. **The most useful single field** — it reports why sending
  is blocked, and exposes the Business Portfolio id.

### Account verification state (per WABA)

```
GET https://graph.facebook.com/v22.0/{waba_id}
    ?fields=id,name,country,currency,timezone_id,account_review_status,
            business_verification_status,ownership_type,message_template_namespace,health_status
    &access_token={token}
```

### All numbers on a WABA

```
GET https://graph.facebook.com/v22.0/{waba_id}/phone_numbers
    ?fields=id,display_phone_number,verified_name,quality_rating,status,throughput
    &access_token={token}
```

### Template inventory

```
GET https://graph.facebook.com/v22.0/{waba_id}/message_templates
    ?fields=id,name,status,category,language&limit=200
    &access_token={token}
```

### Change notifications (instead of polling) ✅ *verified*

Three distinct webhook fields matter. Subscription machinery already exists in
`app/services/meta_graph_client.py`.

| Webhook field | Fires when | Carries |
|---|---|---|
| **`business_capability_update`** | Messaging limit **increases** | `max_daily_conversations_per_business` (v24.0+) · `max_daily_conversation_per_phone` (v23.0−, **removed Feb 2026**) |
| **`account_alerts`** | Scaling eligibility **denied** | `alert_type`, `alert_description` — see §2 for the remedy table |
| **`account_update`** | Quality rating changes | `PHONE_NUMBER_QUALITY_UPDATE` |
| **`messages`** | Delivery status, incl. per-user limit hits | `status: failed`, `errors[].code` — e.g. **`131049`** |

> ⚠️ **Correction:** an earlier draft said `account_update` carries limit changes. It does
> not — it carries **quality** changes. Limit increases arrive on
> `business_capability_update`, denials on `account_alerts`.

**Deprecation to diarise:** `max_daily_conversation_per_phone` is removed in
**February 2026**. Anything reading it must move to
`max_daily_conversations_per_business` on webhooks v24.0+.

### Checking the limit without the API

WhatsApp Manager → **Account tools** → **Messaging limits**.
Useful as a sanity check when the API and our own counter disagree.

### API version note

Meta's current examples use **`v25.0`**. This codebase mixes **`v21.0`**
(`wa_sender.py`, `meta_graph_client.py`) and **`v22.0`** (`whatsapp_api.py`,
`erpnext_client.py`). Worth standardising before adding a fourth consumer — and note that
the `business_capability_update` payload field depends on the **webhook** version (v24.0+
vs v23.0−), which is versioned separately from the Graph calls.

### ⚠️ There is no remaining-quota endpoint

Meta does **not** expose "slots left today". You discover the limit by getting a send
error. **We must count it ourselves.** A business-initiated conversation in our schema is
an outbound template message, so:

```sql
SELECT COUNT(DISTINCT m.conversation_id)
FROM messages m
JOIN conversations c ON c.id = m.conversation_id
WHERE m.direction    = 'outbound'
  AND m.message_type = 'template'
  AND m.created_at   > now() - interval '24 hours'
  AND c.company_id   = :company_id;
```

`/{waba_id}/conversation_analytics` exists but is delayed historical/billing data, not a
live balance.

### Probe scripts

Written during this research, kept in the session scratchpad (not committed):
`wa_health.py` (both accounts) and `softland_probe.py` (deep, field-by-field).
Both read-only; they decrypt the stored token in memory and never print it.
Run with `PYTHONPATH="$PWD" venv/bin/python <script>`.

---

## 5. Constraints that shape the design

### The real bottleneck is templates, not capacity

Softland has **one approved MARKETING template**. Broadcasts are marketing-category and
each distinct campaign message needs its own approved template. First campaign is blocked
on Meta approval turnaround, not on send throughput.

**Recommendation:** submit 2–3 marketing templates now, so approval overlaps development
rather than following it.

### The existing send path would starve

`send_scheduler` polls one shared pool — every `Service` with `template_sent=False`,
25 per tick, every 5s. That is a ceiling of **5 msg/s = 18,000/hour**, and a broadcast
queued into it would sit in front of live transactional orders.

A 10,000-recipient campaign would take ~33 minutes on that path *and* delay every real
order placed during it. Broadcast needs its own pipeline.

### Shared resources that do *not* isolate

Even with separate code:

1. **The Meta quota.** Per portfolio, not per pipeline. Broadcast and transactional spend
   from the same 10,000/24h. Isolation must be *cooperative* — a reserved budget — not
   architectural.
2. **The DB pool.** `create_engine(DATABASE_URL, pool_pre_ping=True)` uses SQLAlchemy
   defaults: **pool_size 5, max_overflow 10 = 15 connections**, shared by uvicorn and all
   three schedulers. A concurrent worker needs its own engine.
3. **The process.** `docker-compose.prod.yml` has two services (`db`, `app`); one container
   runs uvicorn plus three APScheduler *threads*. A high-throughput worker belongs in its
   own container.

### On "async FastAPI"

- CLAUDE.md forbids `async def` handlers touching `get_db()` — the stack is sync
  SQLAlchemy, and `wa_sender.py` documents *"Sync httpx only; no async."*
- The win is **concurrent HTTP to Meta**, not async DB. 10k sends sequentially at ~200ms
  ≈ 33 min; at 15–20 in flight ≈ 2 min.
- Achievable without asyncifying the app: a worker using `httpx.AsyncClient` + semaphore
  for Meta calls, sync SQLAlchemy for batched DB work. A thread pool with sync httpx would
  also suffice at these volumes.

---

## 6. Proposed architecture (not built)

### Tables

**`broadcast_campaigns`** — `company_id`, `name`, `template_id`, `status`, `scheduled_at`,
`created_by_id`, denormalised counters (`total`/`sent`/`delivered`/`read`/`failed`/`skipped`).

**`broadcast_recipients`** — `campaign_id`, `mobile_no`, `conversation_id` (nullable until
sent), `status`, `wamid`, `error_code`, `error_message`, `sent_at`. Index `(campaign_id, status)`.

**`messaging_optouts`** — `company_id`, `mobile_no`, `opted_out_at`, `source`;
unique `(company_id, mobile_no)`.

**Deliberately not reusing `Service`.** A recipient has no questionnaire, no expiry, no
queue position, and no client-supplied `service_id`. Forcing it in would mean synthesising
IDs and inheriting the starvation problem.

**Still write `Message` rows on send** — so `handle_status` receipts, delivery/read
tracking and conversation transcripts keep working unchanged.

### Worker loop — batch and observe

```
claim batch (FOR UPDATE SKIP LOCKED, ~100)
  ├─ skip: opted out
  ├─ skip: already had 2 marketing msgs in 24h
  ├─ pause: portfolio budget exhausted / transactional reserve hit
  ├─ send concurrently (semaphore ~15)
  ├─ write Message rows
  └─ PAUSE and observe:
       batch failure rate above threshold? → auto-pause campaign
       quality rating dropped?             → auto-pause campaign
       else                                → next batch
```

The gap between batches is the safety feature — a bad list is caught after 100 sends,
not 10,000. It also aligns with Meta's own Portfolio Pacing rather than fighting it.

### Deployment — a third service inside this repo

Same repo, same `docker-compose.prod.yml`, **same image**, different command:

```yaml
services:
  db:        # postgres:16
  app:       # uvicorn — web + send/expiry/notify schedulers
  worker:    # same image, own entrypoint — broadcast loop only
```

Same repo because the worker needs `WhatsAppAccount`, `Message`, `wa_sender`,
`whatsapp_crypto` and `config`. A separate project would mean duplicating the model layer
or extracting a shared package — large overhead, no benefit. One Alembic history, one
image, one deploy.

Separate *container* (not just a thread inside `app`) because:

| | Reason |
|---|---|
| CPU | `app` is one Python process running uvicorn + 3 APScheduler threads — one GIL. A busy send loop steals cycles from request handling. |
| DB pool | `pool_size=5, max_overflow=10` = **15 connections shared**. A concurrent worker would starve web requests. Own container → own engine and pool. |
| Blast radius | Worker crash or OOM mid-campaign leaves the admin panel up. |
| Resource caps | CPU/memory limits can be set per service, so a runaway campaign can't take the site down. |
| Scaling | A second worker can be added later; `FOR UPDATE SKIP LOCKED` already makes that safe. |

> **Gotcha:** `main.py`'s lifespan starts `send_scheduler`, `expiry_scheduler` and
> `notify_scheduler`. The worker must **not** boot through `main.py` or all three would
> run twice. Give it its own entrypoint script.

### Two counters, not one

- **Portfolio daily budget** — distinct recipients initiated in rolling 24h, with a
  reserved slice for transactional.
- **Per-recipient marketing counter** — enforce Meta's 2-per-user-per-24h cap before
  spending budget on a message that will be dropped.

### Safety features worth building

- Auto-pause on quality degradation (via `account_update` webhook)
- Auto-pause on batch failure-rate threshold
- Opt-out (`STOP`/`UNSUBSCRIBE`) detection in `conversation_engine`, enforced at claim time
- Store `whatsapp_business_manager_messaging_limit` + `quality_rating` on `WhatsAppAccount`
  so a tier change is visible in the dashboard the day it happens

---

## 7. Open decisions

1. **Reservation split.** At 10K/day, what is the transactional floor? Suggested start:
   broadcast capped at 60% (6,000/day), tuned once real transactional volume is known.
2. **Recipient source.** CSV upload vs. selecting from existing `conversations`. The
   latter is far safer for consent — those people already messaged us.
3. **Who can send.** New `campaigns` resource in `resources.py`, or super-admin only to
   begin with. A bad campaign is hard to unsend.
4. **Client visibility.** Softland-internal only, or do client companies see campaigns on
   their own number? Decides whether `company_filter` scoping and a client API are needed.

---

## 8. Build checklist — what the rules force us to implement

Every row below is a direct consequence of a ✅ verified rule above, not a preference.

| # | Requirement | Why | Rule |
|---|---|---|---|
| 1 | **Strip `+1` US numbers** at list import | Marketing to US numbers is never delivered | Per-user limits § US |
| 2 | **Sliding 24h budget query**, never `::date` | Limit is a moving window, not a daily reset | Messaging limits |
| 3 | **Count at portfolio level**, not per number | *"shared by all business phone numbers within a portfolio"* | Messaging limits |
| 4 | **Reserve headroom for transactional** | *"one number can consume all of the portfolio's messaging capability"* | Messaging limits |
| 5 | **Treat `131049` as `deferred`, not `failed`** | Retrying inside 24h causes a per-user suspension | Per-user limits § retry |
| 6 | **`retry_not_before = now + 24h`** on `131049` | Meta's explicit retry guidance | Per-user limits § retry |
| 7 | **Exclude `131049` from failure-rate auto-pause** | It is a throttle, not a bad number — would false-trip | Derived from 5 |
| 8 | **Branch on `131049` inside `handle_status`** | It arrives on the `messages` webhook we already consume | Per-user limits § error code |
| 9 | **Subscribe `business_capability_update`** | Only source of limit-increase events | Messaging limits § approvals |
| 10 | **Subscribe `account_alerts`** | Only source of denial reasons | Messaging limits § denials |
| 11 | **Use high-quality templates on the volume path** | Low-quality sends do not count toward 2,000 | Scaling paths |
| 12 | **Read `whatsapp_business_manager_messaging_limit`**, fall back to `messaging_limit_tier` | New field; old one deprecated but still returned for some accounts | Messaging limits § warning |
| 13 | **Migrate off `max_daily_conversation_per_phone`** | Removed February 2026 | Messaging limits § approvals |

### Softland-specific implications 📊

- **`TIER_10K`, GREEN, verified, own portfolio** — a workable broadcast account today.
- **Growth to 100K needs ~5,000 unique recipients/day sustained**, since automatic scaling
  requires using half the current limit over 7 days. It will not grow on its own.
- **Per-user limits apply** (India, not an excluded region) — so `131049` handling is
  mandatory, not optional.
- **One approved MARKETING template.** This, not capacity, is the binding constraint on
  the first campaign.

---

## 9. Quick reference — safe sending

- Speed is not what gets you restricted; **blocks and reports are**.
- Stay under ~80% of the daily tier; leave headroom for transactional.
- 10–20 msg/s is ample. The 24h cap binds long before the ~80/s throughput cap.
- Warm up gradually; do not jump from low volume to a full-tier blast.
- Pre-validate numbers — a high failure rate is itself a negative signal.
- Only send to people who opted in. This is the actual ban vector.
- Subscribe to quality webhooks and auto-pause. Cheapest high-value safety feature.

---

## Sources

**Primary (Meta official — authoritative):**
- [Messaging limits](https://developers.facebook.com/docs/whatsapp/messaging-limits/)
- [Messaging limits (business-messaging)](https://developers.facebook.com/documentation/business-messaging/whatsapp/messaging-limits)
- [Per-user marketing template message limits](https://developers.facebook.com/documentation/business-messaging/whatsapp/templates/marketing-templates/per-user-limits)
- [Upcoming messaging limits changes (2025-10-07)](https://developers.facebook.com/documentation/business-messaging/whatsapp/upcoming-messaging-limits-changes)
- [Business phone number API (`whatsapp_business_manager_messaging_limit`)](https://developers.facebook.com/documentation/business-messaging/whatsapp/reference/whatsapp-business-phone-number/whatsapp-business-account-phone-number-api#get-version-phone-number-id)
- [`business_capability_update` webhook](https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/reference/business_capability_update)
- [`account_alerts` webhook](https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/reference/account_alerts)
- [Template quality rating](https://developers.facebook.com/documentation/business-messaging/whatsapp/templates/template-quality)
- [Customer service windows](https://developers.facebook.com/documentation/business-messaging/whatsapp/messages/send-messages#customer-service-windows)

**Secondary (third-party — used only where Meta is silent, and marked unverified):**
- [Gallabox — portfolio-based limits, Oct 2025](https://gallabox.com/whatsapp-business-pricing-october-2025-update)
- [Woztell — 2026 portfolio pacing & 100K limits](https://woztell.com/whatsapp-api-2026-updates-pacing-limits-usernames/)
- [Chatarmin — WhatsApp messaging limits 2026](https://chatarmin.com/en/blog/whats-app-messaging-limits)
- [Uptail — broadcast caps & tier progression](https://www.uptail.ai/blog/whatsapp-business-message-limits-2026-broadcast-caps-tier-progression-what-happens-when-you-hit-the-ceiling)
- [Bloomreach — WhatsApp messaging limits (rolling window)](https://documentation.bloomreach.com/engagement/docs/whatsapp-messaging-limits)

Live values in §1 came from the API on 2026-07-30, not from these articles.
