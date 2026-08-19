"""
Application configuration.

All settings are read from environment variables (prefix ``VS_``) or the
project-root ``.env`` file.  Nothing secret is ever hard-coded here.

Tüm ayarlar ortam değişkenlerinden (``VS_`` öneki) veya proje kökündeki
``.env`` dosyasından okunur. Hiçbir gizli değer burada saklanmaz.
"""

from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ---------------------------------------------------------------------------
# Path anchors
# ---------------------------------------------------------------------------
# config.py -> core -> app -> backend -> <project root>
PROJECT_ROOT: Path = Path(__file__).resolve().parents[3]
BACKEND_ROOT: Path = Path(__file__).resolve().parents[2]
APP_ROOT: Path = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    """Typed application settings."""

    model_config = SettingsConfigDict(
        env_prefix="VS_",
        env_file=(PROJECT_ROOT / ".env", BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # -- Application --------------------------------------------------------
    app_name: str = "Akilli Sicak Satis Yonetim Sistemi"
    app_version: str = "1.0.0"
    env: Literal["development", "production", "test"] = "development"
    debug: bool = True
    host: str = "127.0.0.1"
    port: int = 8000
    default_language: Literal["tr", "en"] = "tr"
    default_currency: str = "TRY"
    timezone: str = "Europe/Istanbul"
    api_prefix: str = "/api/v1"

    # -- Security -----------------------------------------------------------
    secret_key: str = Field(default="")
    algorithm: str = "HS256"
    access_token_minutes: int = 120
    refresh_token_days: int = 14
    password_min_length: int = 8
    max_login_attempts: int = 5
    lockout_minutes: int = 15
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173,http://localhost:8000"
    rate_limit_per_minute: int = 300
    login_rate_limit_per_minute: int = 10

    # -- Database -----------------------------------------------------------
    database_url: str = "sqlite:///./data/van_sales.db"
    db_echo: bool = False
    db_pool_size: int = 10
    db_max_overflow: int = 20

    # -- Cache --------------------------------------------------------------
    redis_url: str = ""

    # -- AI: LM Studio ------------------------------------------------------
    lmstudio_enabled: bool = True
    lmstudio_base_url: str = "http://localhost:1234/v1"
    lmstudio_model: str = "google/gemma-4-12b-qat"
    lmstudio_embedding_model: str = "text-embedding-nomic-embed-text-v1.5"
    lmstudio_timeout: int = 180
    lmstudio_max_tokens: int = 2048
    lmstudio_temperature: float = 0.3

    # -- AI: NVIDIA ---------------------------------------------------------
    nvidia_enabled: bool = True
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    nvidia_api_key: str = ""
    nvidia_model: str = "meta/llama-3.3-70b-instruct"
    nvidia_timeout: int = 120
    nvidia_max_tokens: int = 2048
    nvidia_temperature: float = 0.3

    # -- AI: Claude ---------------------------------------------------------
    claude_enabled: bool = False
    claude_base_url: str = "https://api.anthropic.com/v1"
    claude_api_key: str = ""
    claude_model: str = "claude-sonnet-5"
    claude_timeout: int = 120
    claude_max_tokens: int = 4096
    claude_temperature: float = 0.3

    # -- AI: Router ---------------------------------------------------------
    ai_failover_order: str = "lmstudio,nvidia,claude"
    ai_monthly_budget_usd: float = 25.0
    ai_budget_warn_pct: int = 80
    ai_request_log_enabled: bool = True

    # -- Backup -------------------------------------------------------------
    backup_dir: str = "./backups"
    backup_retention_days: int = 30
    backup_auto_enabled: bool = True
    backup_auto_cron: Literal["daily", "weekly", "monthly", "off"] = "daily"

    # -- Logging ------------------------------------------------------------
    log_dir: str = "./logs"
    log_level: str = "INFO"
    log_max_bytes: int = 10 * 1024 * 1024
    log_backup_count: int = 10

    # -- Business defaults --------------------------------------------------
    default_vat_rate: float = 20.0
    stock_allocation_strategy: Literal["FEFO", "FIFO"] = "FEFO"
    expiry_warning_days: int = 30
    low_stock_ratio: float = 0.2

    # ------------------------------------------------------------------ #
    # Validators
    # ------------------------------------------------------------------ #
    @field_validator("secret_key", mode="after")
    @classmethod
    def _ensure_secret(cls, v: str) -> str:
        """Never ship an empty or placeholder secret key."""
        if not v or v.startswith("CHANGE_ME"):
            # Ephemeral key: fine for dev/tests, sessions reset on restart.
            return secrets.token_urlsafe(64)
        return v

    # ------------------------------------------------------------------ #
    # Derived helpers
    # ------------------------------------------------------------------ #
    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def failover_chain(self) -> list[str]:
        return [p.strip().lower() for p in self.ai_failover_order.split(",") if p.strip()]

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgres")

    def _resolve(self, raw: str) -> Path:
        p = Path(raw)
        return p if p.is_absolute() else (PROJECT_ROOT / p).resolve()

    @property
    def log_path(self) -> Path:
        p = self._resolve(self.log_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def backup_path(self) -> Path:
        p = self._resolve(self.backup_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def data_path(self) -> Path:
        p = PROJECT_ROOT / "data"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def sqlite_file(self) -> Path | None:
        """Absolute path of the SQLite database file, or None for other engines."""
        if not self.is_sqlite:
            return None
        raw = self.database_url.split("///", 1)[-1]
        if raw in ("", ":memory:"):
            return None
        return self._resolve(raw)

    @property
    def effective_database_url(self) -> str:
        """Database URL with relative SQLite paths made absolute and directories created."""
        if self.is_sqlite:
            f = self.sqlite_file
            if f is None:
                return self.database_url
            f.parent.mkdir(parents=True, exist_ok=True)
            return f"sqlite:///{f.as_posix()}"
        return self.database_url

    def has_provider_credentials(self, provider: str) -> bool:
        return {
            "lmstudio": True,  # local, no key required
            "nvidia": bool(self.nvidia_api_key),
            "claude": bool(self.claude_api_key),
        }.get(provider.lower(), False)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton."""
    return Settings()


settings: Settings = get_settings()
