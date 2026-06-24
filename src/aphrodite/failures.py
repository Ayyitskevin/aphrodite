"""Renderer failure classification helpers."""

from __future__ import annotations

from aphrodite.domain import JobFailureCategory


def classify_failure(error: str | None) -> JobFailureCategory:
    text = (error or "").strip().lower()
    if not text:
        return JobFailureCategory.UNKNOWN

    if any(token in text for token in ["timeout", "timed out", "read timed out"]):
        return JobFailureCategory.TIMEOUT
    if any(token in text for token in ["budget", "quota", "cost guard", "daily limit"]):
        return JobFailureCategory.BUDGET_EXCEEDED
    if any(
        token in text
        for token in [
            "source asset",
            "source image",
            "asset not found",
            "file not found",
            "unidentifiedimageerror",
            "invalid image",
            "cannot identify image",
        ]
    ):
        return JobFailureCategory.SOURCE_ASSET_ERROR
    if any(
        token in text
        for token in [
            "http ",
            "status ",
            "429",
            "500",
            "502",
            "503",
            "504",
            "rate limit",
            "provider",
            "xai image request",
            "api request",
        ]
    ):
        return JobFailureCategory.PROVIDER_ERROR
    if any(token in text for token in ["workerapierror", "claim", "output variant"]):
        return JobFailureCategory.WORKER_ERROR
    if any(token in text for token in ["renderererror", "runtimeerror", "render"]):
        return JobFailureCategory.RENDERER_ERROR
    return JobFailureCategory.UNKNOWN
