from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql://postgres:password@localhost:5432/sil_wam"
    SECRET_KEY: str = "change-me-in-production"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30
    MAX_LOGIN_ATTEMPTS: int = 5      # failed attempts before lockout
    LOCKOUT_MINUTES: int = 15        # how long the account stays locked
    APP_TITLE: str = "SIL WhatsApp Manager"
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
    # Set True in production (HTTPS) so the session cookie gets the Secure flag
    HTTPS_ONLY: bool = False

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
