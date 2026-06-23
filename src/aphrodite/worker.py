"""Aphrodite renderer worker CLI."""

from __future__ import annotations

import argparse
import json
import os
import socket
import time
from dataclasses import dataclass
from typing import Any, Protocol
from urllib import error, request

from pydantic import ValidationError

from aphrodite.domain import JobRecord, OutputVariant, WorkerJobClaim
from aphrodite.renderers import RendererBackend, RendererError, get_renderer_backend


class WorkerApiError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class WorkerConfig:
    api_url: str = "http://127.0.0.1:8020"
    worker_id: str = f"aphrodite-worker-{socket.gethostname()}"
    backend: str = "local_stub"
    poll_seconds: float = 5.0
    claim_ttl_seconds: int = 300
    once: bool = False

    @classmethod
    def from_env(cls) -> WorkerConfig:
        return cls(
            api_url=os.getenv("APHRODITE_WORKER_API_URL", cls.api_url),
            worker_id=os.getenv("APHRODITE_WORKER_ID", cls.worker_id),
            backend=os.getenv("APHRODITE_WORKER_BACKEND", cls.backend),
            poll_seconds=_env_float("APHRODITE_WORKER_POLL_SECONDS", cls.poll_seconds),
            claim_ttl_seconds=_env_int(
                "APHRODITE_WORKER_CLAIM_TTL_SECONDS",
                cls.claim_ttl_seconds,
            ),
            once=_env_bool("APHRODITE_WORKER_ONCE", cls.once),
        )


class WorkerApi(Protocol):
    def claim_next_job(self, *, worker_id: str, claim_ttl_seconds: int) -> WorkerJobClaim | None:
        ...

    def heartbeat(
        self,
        *,
        job_id: str,
        claim_token: str,
        claim_ttl_seconds: int,
    ) -> WorkerJobClaim:
        ...

    def complete_output(
        self,
        *,
        job_id: str,
        output_payload: dict[str, str | int],
    ) -> dict[str, Any]:
        ...

    def fail_job(self, *, job_id: str, claim_token: str, error_message: str) -> JobRecord:
        ...


class HttpWorkerApiClient:
    def __init__(self, api_url: str, *, timeout: float = 30.0) -> None:
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout

    def claim_next_job(
        self,
        *,
        worker_id: str,
        claim_ttl_seconds: int,
    ) -> WorkerJobClaim | None:
        payload = self._post_json(
            "/v1/worker/jobs/claim",
            {"worker_id": worker_id, "claim_ttl_seconds": claim_ttl_seconds},
        )
        if payload is None:
            return None
        try:
            return WorkerJobClaim.model_validate(payload)
        except ValidationError as exc:
            raise WorkerApiError("claim response did not match worker contract") from exc

    def heartbeat(
        self,
        *,
        job_id: str,
        claim_token: str,
        claim_ttl_seconds: int,
    ) -> WorkerJobClaim:
        payload = self._post_json(
            f"/v1/worker/jobs/{job_id}/heartbeat",
            {"claim_token": claim_token, "claim_ttl_seconds": claim_ttl_seconds},
        )
        try:
            return WorkerJobClaim.model_validate(payload)
        except ValidationError as exc:
            raise WorkerApiError("heartbeat response did not match worker contract") from exc

    def complete_output(
        self,
        *,
        job_id: str,
        output_payload: dict[str, str | int],
    ) -> dict[str, Any]:
        payload = self._post_json(f"/v1/worker/jobs/{job_id}/outputs", output_payload)
        if not isinstance(payload, dict):
            raise WorkerApiError("output response did not match worker contract")
        return payload

    def fail_job(self, *, job_id: str, claim_token: str, error_message: str) -> JobRecord:
        payload = self._post_json(
            f"/v1/worker/jobs/{job_id}/fail",
            {"claim_token": claim_token, "error": error_message},
        )
        try:
            return JobRecord.model_validate(payload)
        except ValidationError as exc:
            raise WorkerApiError("failure response did not match worker contract") from exc

    def _post_json(self, path: str, payload: dict[str, Any]) -> Any:
        data = json.dumps(payload).encode("utf-8")
        req = request.Request(
            f"{self.api_url}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                body = response.read()
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise WorkerApiError(
                f"worker API rejected {path}: {detail}",
                status_code=exc.code,
            ) from exc
        except OSError as exc:
            raise WorkerApiError(f"worker API request failed for {path}: {exc}") from exc

        if not body:
            return None
        return json.loads(body.decode("utf-8"))


def process_next_job(
    *,
    client: WorkerApi,
    backend: RendererBackend,
    worker_id: str,
    claim_ttl_seconds: int,
) -> bool:
    claim = client.claim_next_job(
        worker_id=worker_id,
        claim_ttl_seconds=claim_ttl_seconds,
    )
    if claim is None:
        return False

    job = claim.job
    pending_variants = _pending_variants(job)
    try:
        for variant in pending_variants:
            claim = client.heartbeat(
                job_id=job.id,
                claim_token=claim.claim_token,
                claim_ttl_seconds=claim_ttl_seconds,
            )
            rendered = backend.render(job=job, variant=variant)
            client.complete_output(
                job_id=job.id,
                output_payload=rendered.as_worker_payload(claim_token=claim.claim_token),
            )
        return True
    except Exception as exc:
        error_message = f"{type(exc).__name__}: {exc}"
        try:
            client.fail_job(
                job_id=job.id,
                claim_token=claim.claim_token,
                error_message=error_message,
            )
        except Exception as fail_exc:
            raise WorkerApiError(
                f"renderer failed and job failure update also failed: {fail_exc}"
            ) from exc
        return True


def run_worker(
    *,
    config: WorkerConfig,
    client: WorkerApi | None = None,
    backend: RendererBackend | None = None,
) -> int:
    client = client or HttpWorkerApiClient(config.api_url)
    backend = backend or get_renderer_backend(config.backend)

    while True:
        processed = process_next_job(
            client=client,
            backend=backend,
            worker_id=config.worker_id,
            claim_ttl_seconds=config.claim_ttl_seconds,
        )
        if config.once:
            return 0
        if not processed:
            time.sleep(config.poll_seconds)


def main(argv: list[str] | None = None) -> int:
    env_config = WorkerConfig.from_env()
    parser = argparse.ArgumentParser(description="Run an Aphrodite renderer worker.")
    parser.add_argument("--api-url", default=env_config.api_url)
    parser.add_argument("--worker-id", default=env_config.worker_id)
    parser.add_argument("--backend", default=env_config.backend)
    parser.add_argument("--poll-seconds", type=float, default=env_config.poll_seconds)
    parser.add_argument(
        "--claim-ttl-seconds",
        type=int,
        default=env_config.claim_ttl_seconds,
    )
    parser.add_argument("--once", action="store_true", default=env_config.once)
    args = parser.parse_args(argv)

    config = WorkerConfig(
        api_url=args.api_url,
        worker_id=args.worker_id,
        backend=args.backend,
        poll_seconds=args.poll_seconds,
        claim_ttl_seconds=args.claim_ttl_seconds,
        once=args.once,
    )
    try:
        return run_worker(config=config)
    except KeyboardInterrupt:
        return 0
    except (RendererError, WorkerApiError) as exc:
        print(f"aphrodite-worker: {exc}")
        return 1


def _pending_variants(job: JobRecord) -> list[OutputVariant]:
    completed = {
        output.variant_id
        for output in job.outputs
        if output.status == "completed"
    }
    return [variant for variant in job.output_plan if variant.id not in completed]


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a float") from exc


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
