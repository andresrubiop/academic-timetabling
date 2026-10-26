# ADR-0001: Target architecture and layering

Date: 2026-08-07
Status: Accepted
Session: SESSION 0

## Context

The platform combines a conventional academic-administration CRUD surface
with a mathematically nontrivial core (CP-SAT timetabling + LNS + an
evolving AI layer that must never override hard feasibility). The
implementation plan (`docs/CLAUDE_IMPLEMENTATION_PLAYBOOK.md`) spans 20+
sessions across many months; the architecture must stay legible and stable
across long gaps between sessions and across model/effort switches
(Opus/Sonnet/Haiku).

## Decision

- Frontend: Next.js + React + TypeScript.
- Backend: FastAPI (Python), layered as API / Application / Domain /
  Infrastructure / Optimization / AI. Domain stays framework-free.
- Database: PostgreSQL + SQLAlchemy + Alembic.
- Jobs: Redis-backed queue with a worker abstraction, not a direct
  synchronous call from the API — optimization must never block HTTP
  (confirmed again in SESSION 9).
- Optimizer: Google OR-Tools CP-SAT for feasibility/optimality, Large
  Neighborhood Search for scaling; CP-SAT is the permanent feasibility
  authority — no ML component may override a HARD constraint.
- AI: contract-first (`NeighborhoodPolicy`, `RecommendationProvider`,
  `ConflictPriorityProvider`), versioned through an explicit
  `AIModel`/`AIModelVersion` registry with manual activation only.
- Deployment: Docker Compose on the target server (`127.0.0.1`), with
  the optimizer worker's execution boundary designed so a local worker,
  remote worker, HPC/Slurm, Kubernetes, or cloud executor can be swapped
  in by configuration.

## Consequences

- Early sessions (1–7) are model/architecture-heavy and justify Opus;
  later CRUD/UX/docs sessions (14+) are routine and should run on Sonnet
  — see the model/effort table in the playbook.
- Domain and optimizer code must not import FastAPI/SQLAlchemy directly,
  to keep the optimizer runnable outside the web process (including on
  HPC/Slurm later) without a rewrite.
- No source code is created in SESSION 0; this ADR fixes the shape that
  SESSION 1 (domain) and SESSION 8/9 (infra/backend) must follow.

## Alternatives considered

- Django monolith (rejected: weaker fit for a long-lived async
  optimization job model and a decoupled optimizer worker).
- Putting the optimizer inside the request/response cycle (rejected:
  CP-SAT/LNS runs can take from seconds to minutes; SESSION 9 explicitly
  requires it be async with progress reporting).
