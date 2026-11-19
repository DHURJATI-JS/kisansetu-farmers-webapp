"""Application configuration (env-driven, validated by pydantic-settings)."""
from __future__ import annotations

from typing import Any

import base64
import json
import os
import secrets
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent
APP_DIR = ROOT_DIR / "app"
VAR_DIR = ROOT_DIR / "var"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT_DIR / ".env", env_file_encoding="utf-8", extra="ignore")

    APP_NAME: str = "KisanSetu"
    ENV: str = "development"            # development | production
    TEST_MODE: bool = False             # auto-approve farms / GSTIN / insurance, simulate SMS & email, show OTPs
    DEBUG: bool = False
    BASE_URL: str = "http://localhost:8088"
    HOST: str = "0.0.0.0"
    PORT: int = 8088
    WEB_CONCURRENCY: int = 1            # uvicorn worker processes; sessions, limits, bans, caches and sockets are shared via Redis
    METRICS_ALLOW: str = "127.0.0.1/32,::1/128"   # networks that may read /internal/metrics (never via a forwarded request)
    ALLOWED_HOSTS: str = "*"            # comma separated
    FORCE_HTTPS: bool = False
    TRUSTED_PROXIES: str = "127.0.0.1"

    # --- secrets (auto-generated into var/dev_secrets.json in development) ---
    SECRET_KEY: str = ""
    PASSWORD_PEPPER: str = ""
    FIELD_ENCRYPTION_KEYS: str = ""     # "k2:<b64 32 bytes>,k1:<b64>"  first = active key
    BLIND_INDEX_KEY: str = ""

    # --- database ---
    DATABASE_URL: str = "postgresql+asyncpg://kisan:kisan@127.0.0.1:5444/kisansetu"
    DB_POOL_SIZE: int = 10
    DB_ECHO: bool = False
    AUTO_CREATE_TABLES: bool = True      # dev convenience; production uses `alembic upgrade head`

    # --- redis (sessions, rate limits, socket.io fan-out, job locks, caches) ---
    REDIS_URL: str = "redis://:kisan-redis@127.0.0.1:6384/0"
    REDIS_REQUIRED: bool = False         # forced on in production
    SOCKETIO_REDIS: bool = True          # fan-out Socket.IO events across workers via Redis pub/sub
    SESSION_TTL_DAYS: int = 7            # absolute lifetime
    KEEP_ALIVE_SECONDS: int = 75         # idle HTTP keep-alive; keep it above any proxy / load-balancer idle timeout
    SESSION_IDLE_HOURS: int = 72         # sliding idle timeout

    # --- AI providers (primary = OpenAI-compatible gateway) ---
    AI_GATEWAY_BASE_URL: str = ""      # OpenAI-compatible endpoint, e.g. http://localhost:8000/v1 (empty = not used)
    AI_GATEWAY_API_KEY: str = ""
    AI_GATEWAY_MODEL: str = "gemma"
    VLLM_BASE_URL: str = ""             # e.g. http://localhost:8001/v1
    VLLM_API_KEY: str = "EMPTY"
    VLLM_MODEL: str = "google/gemma-3-4b-it"
    GEMMA3_BASE_URL: str = ""           # failsafe Gemma 3 (Ollama http://localhost:11434/v1 or Google AI Studio OpenAI endpoint)
    GEMMA3_API_KEY: str = "ollama"
    GEMMA3_MODEL: str = "gemma3:4b"
    ANTHROPIC_API_KEY: str = ""
    CLAUDE_MODEL: str = "claude-opus-5-5"
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-5-mini"
    AI_TIMEOUT_SECONDS: float = 90
    AI_CONCURRENCY: int = 4
    AI_LOW_CONFIDENCE: float = 0.6
    AI_EMBEDDINGS_MODEL: str = ""       # set if the gateway serves /v1/embeddings
    EMBEDDING_DIM: int = 384
    TRANSLATION_MINUTES: float = 10     # AI back-fill: minutes of each 15-minute slot (steps aside for people)

    # --- OCR ---
    TESSERACT_CMD: str = ""             # e.g. C:\Program Files\Tesseract-OCR\tesseract.exe
    OCR_ENGINES: str = "tesseract,rapidocr"

    # --- GSTIN verification API (skipped in TEST_MODE) ---
    GSTIN_API_URL: str = ""             # e.g. https://sheet.gstincheck.co.in/check/{key}/{gstin}
    GSTIN_API_KEY: str = ""

    # --- messaging ---
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = "KisanSetu <no-reply@example.com>"
    SMTP_STARTTLS: bool = True
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_SMS_FROM: str = ""
    TWILIO_WHATSAPP_FROM: str = ""      # e.g. whatsapp:+14155238886

    # --- external data ---
    GOOGLE_CSE_API_KEY: str = ""
    GOOGLE_CSE_CX: str = ""
    JOB_HUNT_MAX_QUERIES_PER_DAY: int = 40
    NOMINATIM_URL: str = "https://nominatim.openstreetmap.org"
    GEOCODER_USER_AGENT: str = "KisanSetu-StudentProject/1.0"

    # --- superadmin bootstrap ---
    SUPERADMIN_PHONE: str = ""
    SUPERADMIN_EMAIL: str = ""
    SUPERADMIN_PASSWORD: str = ""
    SUPERADMIN_NAME: str = "Super Admin"

    # --- optional antivirus scanning of uploads (clamd) ---
    CLAMAV_HOST: str = ""
    CLAMAV_PORT: int = 3310
    CLAMAV_REQUIRED: bool = True        # refuse an upload when it cannot be scanned (clamd down / error) — never store unscanned files

    # --- storage / limits ---
    UPLOAD_DIR: str = str(VAR_DIR / "uploads")
    EXPORT_DIR: str = str(VAR_DIR / "exports")
    MAX_UPLOAD_MB: int = 8
    MAX_BODY_MB: int = 12

    # --- security ---
    ACCESS_TOKEN_DAYS: int = 7
    LOGIN_MAX_ATTEMPTS: int = 5
    LOGIN_LOCK_MINUTES: int = 15
    REQUIRE_ADMIN_2FA: bool = True
    SCHEDULER_ENABLED: bool = True
    RUN_STARTUP_SCAN: bool = False

    # ------------------------------------------------------------------
    @property
    def is_production(self) -> bool:
        return self.ENV.lower() == "production"

    @property
    def https(self) -> bool:
        return self.FORCE_HTTPS or self.BASE_URL.lower().startswith("https://")

    @property
    def allowed_hosts(self) -> list[str]:
        return [h.strip() for h in self.ALLOWED_HOSTS.split(",") if h.strip()] or ["*"]

    @property
    def socketio_transports(self) -> list[str]:
        """Several worker processes without a sticky load balancer: Engine.IO long-polling would hop between workers,
        so browsers use WebSocket only (one connection = one worker)."""
        return ["websocket"] if self.WEB_CONCURRENCY > 1 else ["websocket", "polling"]

    @property
    def base_origin(self) -> str:
        u = urlparse(self.BASE_URL)
        return f"{u.scheme}://{u.netloc}"

    @property
    def ocr_engines(self) -> list[str]:
        return [e.strip().lower() for e in self.OCR_ENGINES.split(",") if e.strip()]

    @model_validator(mode="before")
    @classmethod
    def _comment_only_values_are_unset(cls, data: Any) -> Any:
        """`TESSERACT_CMD=   # e.g. C:\\...` arrives as the comment text through docker compose env_file — treat it as unset."""
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if not (isinstance(v, str) and v.strip().startswith("#"))}
        return data

    def model_post_init(self, __context) -> None:  # noqa: D401
        if self.is_production and self.TEST_MODE:
            # test mode returns OTP codes in API responses and auto-approves farms: never on a real deployment
            raise RuntimeError("TEST_MODE=true is not allowed with ENV=production")
        missing = [k for k in ("SECRET_KEY", "PASSWORD_PEPPER", "FIELD_ENCRYPTION_KEYS", "BLIND_INDEX_KEY") if not getattr(self, k)]
        if not missing:
            return
        if self.is_production:
            raise RuntimeError(f"Missing required secrets in production: {', '.join(missing)}. Run scripts/generate_keys.py")
        # development convenience: persist generated secrets so restarts keep sessions & decryptability
        VAR_DIR.mkdir(parents=True, exist_ok=True)
        path = VAR_DIR / "dev_secrets.json"
        data: dict[str, str] = {}
        if path.exists():
            data = json.loads(path.read_text("utf-8"))
        changed = False
        defaults = {
            "SECRET_KEY": lambda: secrets.token_urlsafe(48),
            "PASSWORD_PEPPER": lambda: secrets.token_urlsafe(32),
            "FIELD_ENCRYPTION_KEYS": lambda: "k1:" + base64.urlsafe_b64encode(os.urandom(32)).decode(),
            "BLIND_INDEX_KEY": lambda: secrets.token_urlsafe(32),
        }
        for k in missing:
            if k not in data:
                data[k] = defaults[k]()
                changed = True
            object.__setattr__(self, k, data[k])
        if changed:
            path.write_text(json.dumps(data, indent=2), "utf-8")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
