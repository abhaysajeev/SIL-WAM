# Agent App API — Limits

## `GET /agent-api/v1/limits`

Every ceiling that applies to this agent, so the app can show it **before** the agent
hits it (e.g. on the broadcast confirm screen). All of them are also enforced by the
server.

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
    {"format": "IMAGE", "mimes": ["image/jpeg", "image/png"], "max_bytes": 5242880},
    {"format": "DOCUMENT", "mimes": ["application/pdf"], "max_bytes": 104857600},
    {"format": "VIDEO", "mimes": ["video/mp4"], "max_bytes": 16777216}
  ],
  "default_country": "IN",
  "server_time": "2026-11-13T09:41:07Z"
}
```

| Field | Meaning |
|---|---|
| `max_recipients_per_send` | People one broadcast may reach, after duplicates and opt-outs. `0` = no limit |
| `daily_message_limit` / `sent_last_24h` | Rolling 24 hours across everything this agent sends. `0` = no limit |
| `max_contacts_per_phonebook` | Size of a phonebook the agent builds (create, add, import) |
| `default_country` | Numbers typed without a country code are read as numbers in this country |

The values are set per company by an administrator (web panel → Broadcast settings).
Re-read on app start and before a broadcast; they can change.
