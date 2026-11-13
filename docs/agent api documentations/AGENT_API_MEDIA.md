# Agent App API — Media

Upload a file to use as a broadcast's header (image, video or document), and download
a file shown in a chat.

---

## `POST /agent-api/v1/media` — upload

```
POST /agent-api/v1/media
Authorization: Bearer <session token>
X-Device-Id:   <device>
Content-Type:  multipart/form-data; boundary=…

file=<the file>
```

### Response — `201 Created`

```json
{
  "media_id":   "0b9d6e1c-5a4f-4a43-9b0e-2f1d0c7b8a11",
  "format":     "IMAGE",
  "mime":       "image/jpeg",
  "size_bytes": 184233,
  "filename":   "diwali.jpg",
  "expires_at": "2026-12-13T09:41:07Z"
}
```

Use `media_id` in `POST /broadcasts` → `header_media`. One upload can be used by any
number of broadcasts until `expires_at` (30 days); after that, upload again.

### What is accepted

| `format` | Types | Max size |
|---|---|---|
| `IMAGE` | JPEG, PNG | 5 MB |
| `VIDEO` | MP4 | 16 MB |
| `DOCUMENT` | PDF | 100 MB |

The same rules are in `GET /limits` → `media`. **The type is read from the file's
content**, not its name or the Content-Type you send — a renamed file is refused.

| `code` | HTTP | Meaning |
|---|---|---|
| `unsupported_media_type` | 415 | Not JPEG/PNG/MP4/PDF |
| `media_too_large` | 413 | Over the limit for its type (`max_bytes`) |
| `file_unreadable` | 400 | Empty file |
| `media_upload_failed` | 502 | WhatsApp refused the file — show `message`, let the agent try another |
| `no_whatsapp_account` | 409 | The company has no WhatsApp number connected |

---

## `GET /agent-api/v1/media/{media_id}` — download

Returns the file itself (`Content-Type` is the file's type). Images and videos are
`inline`, documents `attachment`. Cache it on the device — it is `Cache-Control: private`.

You can download a file you uploaded, or a file shown in a chat you can open (a
broadcast bubble carries it as `media.asset_id`).

| HTTP | `code` | Meaning |
|---|---|---|
| 404 | `media_not_found` | Unknown, another company's, or not visible to you |
| 410 | `media_expired` | Deleted after 30 days — show "no longer available" |

Files are kept **30 days**, then deleted. The message itself stays in the chat.
