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


@dataclass(frozen=True)
class Settings:
    service_name: str = "aphrodite"
    env: str = "development"
    db_path: str = "data/aphrodite.db"
    host: str = "127.0.0.1"
    port: int = 8020
    reload: bool = False

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            env=os.getenv("APHRODITE_ENV", cls.env),
            db_path=os.getenv("APHRODITE_DB_PATH", cls.db_path),
            host=os.getenv("APHRODITE_HOST", cls.host),
            port=_env_int("APHRODITE_PORT", cls.port),
            reload=_env_bool("APHRODITE_RELOAD", cls.reload),
        )
