"""Runtime configuration for Aphrodite."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


@dataclass(frozen=True)
class Settings:
    service_name: str = "aphrodite"
    env: str = "development"
    db_path: str = "data/aphrodite.db"
    media_root: str = "media"
    max_upload_bytes: int = 15_000_000
    api_token: str | None = None
    worker_token: str | None = None
    host: str = "127.0.0.1"
    port: int = 8020
    reload: bool = False
    alert_webhook_url: str | None = None
    alert_webhook_token: str | None = None
    alert_timeout_seconds: float = 10.0
    alert_retry_base_seconds: int = 300
    alert_retry_max_seconds: int = 3600

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            env=os.getenv("APHRODITE_ENV", cls.env),
            db_path=os.getenv("APHRODITE_DB_PATH", cls.db_path),
            media_root=os.getenv("APHRODITE_MEDIA_ROOT", cls.media_root),
            max_upload_bytes=_env_int("APHRODITE_MAX_UPLOAD_BYTES", cls.max_upload_bytes),
            api_token=os.getenv("APHRODITE_API_TOKEN") or None,
            worker_token=os.getenv("APHRODITE_WORKER_TOKEN") or None,
            host=os.getenv("APHRODITE_HOST", cls.host),
            port=_env_int("APHRODITE_PORT", cls.port),
            reload=_env_bool("APHRODITE_RELOAD", cls.reload),
            alert_webhook_url=os.getenv("APHRODITE_ALERT_WEBHOOK_URL") or None,
            alert_webhook_token=os.getenv("APHRODITE_ALERT_WEBHOOK_TOKEN") or None,
            alert_timeout_seconds=_env_float(
                "APHRODITE_ALERT_TIMEOUT_SECONDS",
                cls.alert_timeout_seconds,
            ),
            alert_retry_base_seconds=_env_int(
                "APHRODITE_ALERT_RETRY_BASE_SECONDS",
                cls.alert_retry_base_seconds,
            ),
            alert_retry_max_seconds=_env_int(
                "APHRODITE_ALERT_RETRY_MAX_SECONDS",
                cls.alert_retry_max_seconds,
            ),
        )
