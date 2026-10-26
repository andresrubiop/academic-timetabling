# Public source provenance

Prepared on 2026-10-26 from source commit `b3e663fcc8a0663e7a11746d920c722e0f384614` of the Academic AI planning platform. The package remains `academic-ai` / `academic_ai`; the public repository name emphasizes the implemented timetabling work rather than implying a trained AI model.

Included: backend, frontend, migrations, synthetic tests, demo generator, generic deployment files and architectural decisions. Public documentation was rewritten to distinguish current implementation and limits. The Compose entry point binds to loopback, and the environment template uses local addresses. Runtime application code is preserved.

Excluded: private history and working instructions, operational status/journal files, credentials, backups, real institutional FET data and other institution/participant records. Some source comments refer to historical session numbers or document names; those are development provenance, not claims that private files are distributed.

The project used AI coding assistants. Assertions about working behavior belong to executable tests or explicitly reported manual checks. No learned model was trained during this public-release preparation.
