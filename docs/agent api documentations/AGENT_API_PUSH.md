# Agent App API — Push notifications, live updates, and the sync contract

## The rule

**Pushes and live events only say "something changed in thread X". The content always
comes from** `GET /threads/{id}/messages?changed_after_rev=<highest rev you hold>`.
A lost push therefore costs seconds, never a message.

Sync a thread on: app start · return to foreground · a push · a live event · reconnect.
After any of these, also refresh the inbox (`GET /threads`) — `thread.rev` larger than
what you hold means that thread has news.

## Who is notified

Every agent who contacted the customer (broadcast or reply) in the last **30 days**.
Others never see the chat. A reply that arrives after 30 days reaches nobody's device
(it is in the web panel).

## `PUT /agent-api/v1/devices/push-token`

```json
{ "token": "<FCM registration token>", "platform": "android" }
```

Call after login and in `onNewToken`. `DELETE /agent-api/v1/devices/push-token` turns
pushes off for this device. Logging out also stops them.

### The push (FCM, data-only)

```json
{ "type": "sync", "thread_id": "b4760a72-…", "rev": "57", "new_message": "1" }
```

* `new_message = "1"` — a customer wrote. Sent at **high priority**. **You must show a
  notification** (fetch the message, then notify): Android demotes high-priority pushes
  that show nothing.
* `new_message = "0"` — only statuses changed (✓✓, read). Normal priority, collapsed per
  thread. Sync silently; no notification.

The push never contains message text.

## `GET /agent-api/v1/events` — while the app is open

Server-Sent Events with the usual two headers:

```
event: sync
data: {"type": "sync", "thread_id": "…", "rev": 57, "new_message": true}

: ping
```

Open on foreground, close on background. Reconnect with backoff if it drops, then do a
normal sync — events missed while disconnected are not replayed.

## Inbound attachments

A customer's photo/document arrives first as a message whose `media` has no `asset_id`
yet. Within seconds the server copies it, sets `media.asset_id`, and bumps the message's
`rev` — your next sync picks it up. Download with `GET /media/{asset_id}`. Files are
kept 30 days.

## Reactions

A customer's reaction does not add a message. It changes the reacted-to message's
`reactions` (e.g. `{"👍": ["customer"]}`) and its `rev`.
