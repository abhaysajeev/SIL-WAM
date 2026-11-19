from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql://postgres:password@localhost:5432/sil_wam"
    SECRET_KEY: str = "change-me-in-production"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30
    MAX_LOGIN_ATTEMPTS: int = 5      # failed attempts before lockout
    LOCKOUT_MINUTES: int = 15        # how long the account stays locked

    # ── Agent app sessions ────────────────────────────────────────────────────
    # Agent tokens have no absolute expiry — they live until revoked. This is the
    # idle cutoff instead: a session unused for this long stops working. It is
    # housekeeping for handsets replaced or wiped without anyone saying so, not a
    # security control; disabling a user or revoking a session is already instant.
    AGENT_SESSION_IDLE_DAYS: int = 90
    # last_used_at is only rewritten when it is older than this. Touching it on
    # every request would mean a row UPDATE per read and needless contention, and
    # since the cutoff above is measured in days the imprecision is irrelevant.
    AGENT_SESSION_TOUCH_MINUTES: int = 5
    APP_TITLE: str = "SIL WhatsApp Manager"

    # The timezone the ADMIN PANEL renders timestamps in. Storage is always UTC —
    # Postgres runs on Etc/UTC and every column is timezone-aware — because the
    # message pipeline compares instants across Meta webhooks, schedulers and the
    # agent app, and a localised database makes those comparisons quietly wrong.
    # This setting only decides what a person reads on screen.
    # Any IANA name: "Asia/Kolkata", "Asia/Dubai", "UTC".
    DISPLAY_TIMEZONE: str = "Asia/Kolkata"
    DEBUG: bool = True
    FB_APP_ID: str = ""
    META_APP_SECRET: str = ""
    # Extra Meta app secrets accepted on the webhook signature, comma-separated. Meta
    # signs each delivery with the secret of whichever app the WABA is subscribed to, so
    # a second app (or a rotation in progress) needs its secret here or every one of its
    # events is rejected with 403. META_APP_SECRET is always accepted as well.
    META_APP_SECRETS: str = ""
    # off    — old path only: process in a BackgroundTask, nothing stored first.
    # shadow — store every webhook in webhook_inbox AND run the old path; the inbox rows
    #          are not processed. For comparing counts in production before switching.
    # on     — store first, process from webhook_inbox (immediately + scheduler sweep).
    WEBHOOK_INBOX_MODE: str = "shadow"
    META_CONFIG_ID: str = ""

    # ── Media files (broadcast images, chat attachments) ──────────────────────
    # "s3" in production (MinIO in docker-compose.prod.yml, any S3-compatible store
    # works); "local" writes under MEDIA_LOCAL_DIR for development and tests.
    MEDIA_STORE: str = "local"
    MEDIA_LOCAL_DIR: str = "var/media"
    MEDIA_S3_ENDPOINT: str = ""          # e.g. http://minio:9000
    MEDIA_S3_BUCKET: str = "wam-media"
    MEDIA_S3_ACCESS_KEY: str = ""
    MEDIA_S3_SECRET_KEY: str = ""
    MEDIA_S3_REGION: str = "us-east-1"
    # Every stored file is deleted this many days after it was stored. Meta's own
    # copy of an uploaded file lasts 30 days, so keeping ours longer would show a
    # bubble whose file can no longer be re-sent anyway.
    MEDIA_RETENTION_DAYS: int = 30

    # ── Push notifications to the agent app (Firebase Cloud Messaging, Android) ──
    # Path to the Firebase service-account JSON (role: Firebase Cloud Messaging API
    # Admin). Empty = pushes are off; the app still syncs on open / via /events.
    FCM_SERVICE_ACCOUNT_FILE: str = ""

    # What happens when a company's sends start failing with an account-level error
    # (billing, account locked, policy block):
    #   alert   — record it on the account and in the error log, keep sending
    #   enforce — also pause the campaign and refuse new agent sends (503) until a
    #             message is delivered again
    ACCOUNT_BREAKER_MODE: str = "alert"
    # Meta inbound webhook — ISV-level, not per-company
    META_WEBHOOK_VERIFY_TOKEN: str = ""
    # Phase 4 — lazy conversation expiry threshold (NEW_PLAN §6.7)
    CONVERSATION_TIMEOUT_HOURS: int = 72
    # ERPNext integration — global default method name (per-company override on ERPNextConfig)
    ERPNEXT_PDF_METHOD: str = "sil.services.print_download.get_invoice_pdf"
    # Optional Redis for wamid dedup fast path — leave empty to rely on DB unique index only
    REDIS_URL: str = ""
    # Lizo → SFA order approval, fired when the customer taps Confirm Order. A single
    # global URL rather than a per-key column: every Lizo company points at the same
    # SFA host, and the status callback's own URL already lives on CompanyApiKey.
    # Empty disables the call — the hook stays inert, like a missing notify_url.
    LIZO_APPROVE_ORDER_URL: str = ""
    # The Confirm Order / Cancel template sent when the customer taps Confirm on the
    # order message. A setting rather than a constant because it is also the
    # discriminator: both templates carry a button labelled "Confirm Order", and the
    # tap is routed by comparing the tapped template's name against this.
    LIZO_CONFIRM_TEMPLATE: str = "order_confirm_liso"
    # Set True in production (HTTPS) so the session cookie gets the Secure flag
    HTTPS_ONLY: bool = False
    # Central log server (the log-ingestion project). /client-api, /agent-api and
    # /webhook calls are copied there from a background thread -- see app/monitoring/.
    # Off unless both URL and key are set; the key is the project's ingest key there.
    LOG_INGEST_URL: str = ""              # e.g. https://logs.softlandindia.net/ingest/batch
    LOG_INGEST_KEY: str = ""
    LOG_ENVIRONMENT: str = ""             # shown on the dashboard, e.g. "live" or "local"
    LOG_INGEST_QUEUE_SIZE: int = 5000     # logs waiting to be sent before new ones are dropped
    LOG_INGEST_TIMEOUT: int = 5           # seconds per POST

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
