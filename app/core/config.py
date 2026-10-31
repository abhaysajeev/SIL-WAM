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
    META_CONFIG_ID: str = ""
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

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
