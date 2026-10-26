# Architecture

The backend package retains its original name, `academic_ai`. It separates HTTP (`api`), workflows (`application`), vocabulary and scheduling (`domain`), persistence/import/queue adapters (`infrastructure`), solver integration (`optimization`) and future model contracts (`ai`). Security checks are shared rather than reimplemented independently in each UI action.

The domain layer is framework-free. Scheduling facts and constraints are converted into CP-SAT variables and adapters at the solver boundary. Large Neighborhood Search changes neighborhoods; CP-SAT remains the feasibility authority. Objective tiers are lexicographic rather than a weighted scalar that could trade a higher-priority conflict for several lower-priority preferences.

PostgreSQL is required for the schema's partial indexes, exclusion constraints, trigger-enforced invariants and audit behavior. The tests use a disposable real PostgreSQL instance; Redis queue tests use a disposable Redis instance. Never point them at an operational database.

The frontend is a Next.js/React TypeScript interface. A dedicated nginx service routes the UI and API. The public Compose template exposes only a loopback web port and keeps PostgreSQL/Redis on its internal network. The optimizer worker has explicit CPU/memory limits.

AI contracts and registry code are included but no trained model is active. Human approvals, publication and model activation are distinct actions. See `backend/academic_ai/application/apply.py` for the explicit versioned apply path and `backend/academic_ai/security/jurisdiction.py` for the scope model. Scope coverage is incomplete; see `SECURITY.md`.
