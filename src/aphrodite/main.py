"""Application entrypoint for uvicorn."""

from __future__ import annotations

import uvicorn

from aphrodite.api import create_app
from aphrodite.config import Settings

app = create_app()


def main() -> None:
    settings = Settings.from_env()
    uvicorn.run(
        "aphrodite.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.reload,
    )
