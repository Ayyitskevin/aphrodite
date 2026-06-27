# Retiring Aphrodite

Aphrodite is the **products render worker** for the Mise Solo Studio OS: a
stateless, contract-true worker that turns a source product photo into rendered
image variants and reports the **real per-render cost** so Mise can enforce its
spend cap. Mise — not Aphrodite — owns the authoritative spend cap, the
approval/consent gate, and the export step.

This document exists so the worker can be decommissioned, replaced, or folded
into Mise without losing anything that matters. It inventories what state
Aphrodite holds, marks what is genuinely worker-local versus what is
authoritative business state that ultimately belongs to Mise, and gives a
concrete teardown path.

## Design intent: stateless render worker

The render path is stateless and reproducible:

- A render is a pure function of `(source asset, output spec, render request)`.
- Each completion is stamped with a `render_request_id`; a duplicated delivery
  is an idempotent no-op, so retries never double-charge or revoke approval.
- The worker reports `cost_usd`, `cost_ticks`, `model`, and `latency_ms` on
  every output; Mise sums real cost against its hard cap.
- The worker never publishes. Output enters an explicit-commit review state;
  a human approves AND (when the consent policy is active) confirms
  rights/consent before any export. Exports are operator-pull only.

Everything else Aphrodite persists is either a worker-local cache that can be
rebuilt, or business state that should migrate to Mise on retirement.

## State inventory

### Worker-local (safe to discard or rebuild)

| State | Where | Notes |
| --- | --- | --- |
| Rendered output files | `media/outputs/{job_id}/...` | Regenerable by re-running the render; the durable artifact for export. |
| Source uploads | `media/originals/...` | Mirror of assets Mise already owns; re-uploadable. |
| Job claim state | `jobs.claimed_by/claim_token/claimed_at/claim_expires_at` | Lease bookkeeping; meaningless once workers stop. |
| Render cache | `job_outputs` rows | Cache of produced artifacts + their cost/provenance; rebuildable by re-render. |
| Local xAI cost ledger | `media/.aphrodite-xai-costs.jsonl` | **Secondary** spend rail (reserve→reconcile) subordinate to Mise's cap. Not authoritative. |

### Authoritative business state (must migrate to Mise before retiring)

| State | Where | Belongs to |
| --- | --- | --- |
| Client / project ownership | `clients`, `projects` | Mise owns asset & client ownership. |
| Approval / review decisions | `job_outputs.review_status/review_note/reviewed_at` | Mise owns the approval gate. |
| Rights / consent confirmations | `job_outputs.rights_confirmed_at/_by`, `license_note` | Mise owns the consent gate. |
| Spend records | `media/.aphrodite-xai-costs.jsonl`, `job_outputs.cost_usd` | Mise owns the authoritative ledger and cap. |
| Batch / alert lifecycle | `project_job_batches`, `project_job_batch_alerts` | Operational workflow state; export before teardown. |

> The render worker function (claim → render → report cost) is stateless and
> disposable. The rows in the second table are the only things a retirement must
> not drop on the floor.

## Activation gates (owner decisions, not code)

Aphrodite ships the *mechanism* but stays dormant until the owner decides:

1. **Budget number** — Mise's authoritative hard spend cap. The worker only
   *reports* real cost; it does not own the number. The local
   `APHRODITE_XAI_*` budgets are a secondary backstop, not the cap.
2. **Consent / licensing policy** — set `APHRODITE_REQUIRE_RIGHTS_CONFIRMATION=true`
   and define what rights/consent must be confirmed before export. Off by default.
3. **Render backend** — `local_stub` (free, deterministic) by default; enable
   `xai` (real spend, needs an API key) only deliberately.

## Decommission checklist

1. **Stop intake.** Stop creating jobs; let in-flight claims drain or expire
   (claims are short-lived and self-recover).
2. **Stop workers.** Halt all `aphrodite-worker` processes. No new spend can
   occur once backends are idle.
3. **Export authoritative state to Mise:**
   - Clients/projects: `GET /v1/clients`, `GET /v1/projects`.
   - Per-job renders + real cost + review + consent state:
     `GET /v1/jobs/{job_id}/renders` (the Mise-facing envelope) and
     `GET /v1/jobs/{job_id}`.
   - Spend: `GET /admin/spend.json` and per-batch reports.
   - Approved/consented media: the export ZIP routes.
4. **Reconcile spend** in Mise's ledger from the exported cost data; the local
   xAI ledger is the source of record only until Mise absorbs it.
5. **Archive media** (`media/originals`, `media/outputs`) if the artifacts are
   still needed; otherwise discard — they are regenerable.
6. **Retire the service.** The SQLite DB (`data/aphrodite.db`) and media root
   can be deleted once steps 3–5 are confirmed. Nothing here is the system of
   record once Mise has the exported state.

## What "retired" looks like

Mise holds ownership, the authoritative spend ledger and cap, and the
approval/consent decisions. Aphrodite (or its successor) is reduced to a
stateless render worker that can be re-instantiated from scratch, points at
Mise for the cap and the gate, and keeps only a regenerable render cache and
output files.
