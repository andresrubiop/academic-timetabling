# ADR-0002 — Isolated local demo networking

The public source edition uses a dedicated Compose project. Only nginx publishes a host port, bound to `127.0.0.1`; backend and frontend communicate through the edge network, while PostgreSQL and Redis remain on the internal network. The default web port is 5070 and can be changed in the private local `.env`.

Do not assume a port is available or reuse an operational database. CI and release tests use disposable services with explicit test URLs. Production TLS, remote access and host-wide routing are deployment-specific work outside this local demonstration.

This public note replaces operational host inventory from the original working repository. It does not describe or alter an existing shared server.
