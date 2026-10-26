# ADR-0011: Server-Sent Events for optimization progress

Date: 2026-08-18
Status: Accepted
Session: SESSION 9

## Context

An optimization run takes minutes. The playbook (SESSION 9) requires progress
to be reported through eight phases — `VALIDATING`, `BUILDING_MODEL`,
`SEARCHING_FEASIBLE`, `FEASIBLE_FOUND`, `OPTIMIZING`, `EVALUATING`,
`COMPLETED`, `FAILED` — and requires the choice of transport to be documented.

The realistic options are polling, Server-Sent Events, and WebSockets.

What the traffic actually looks like matters more than the general merits of
each:

- **One direction.** The server reports; the browser does not stream anything
  back. The one action a user takes mid-run is cancellation, which is a single
  `POST` and does not need a persistent channel.
- **Low frequency.** A phase change every few seconds at most. A CP-SAT solve
  spends most of its time in one phase.
- **Small messages.** A phase name, a percentage, a sentence.
- **Losing a message is survivable.** The database row is authoritative. The
  stream is a convenience over polling it.

Constraints from this deployment:

- nginx is the only published entrypoint (ADR-0002) and terminates every
  connection.
- The platform is served over plain HTTP by IP today; TLS arrives later
  (SECURITY.md §7).
- The host is shared, and idle connections cost memory that other tenants also
  need.

## Decision

**Server-Sent Events**, at `GET /api/v1/optimization/runs/{id}/events`.

Progress is written to the `optimization_run` row by the worker and streamed
by the API process, which polls the row. The stream is a *view* of the row,
never the source of truth.

Every response also carries the current state, so a client that never opens
the stream still works by polling `GET /runs/{id}`.

## Consequences

**It is ordinary HTTP.** No protocol upgrade, so nginx needs no `Upgrade`
handling on the API path, no `proxy_read_timeout` tuning beyond what long
optimizer requests already need, and no separate treatment when TLS is added.
A WebSocket would need all three, on a proxy configuration that is currently
simple enough to read in one sitting.

**Reconnection is built in.** The browser's `EventSource` reconnects on its
own and sends `Last-Event-ID`. With WebSockets that is application code, and
application code that only runs when the network is already misbehaving is
application code that is never tested.

**It survives a worker moving.** The API streams from the database row, not
from the worker process. Any API replica can serve the stream for a run
executing on any worker, and a worker that dies leaves the row visible with a
stale heartbeat rather than a dropped socket the client must interpret.

**One connection per watching client, held open.** Bounded by making the
stream terminate itself: it closes on a terminal status, and on an idle
timeout. An unbounded stream on a shared host is a slow memory leak with a
sympathetic explanation.

**The browser six-connection limit applies per origin on HTTP/1.1.** A
coordinator watching six runs at once would stall the seventh request. This
is acceptable now — watching more than one or two runs is not a real workflow
— and disappears with HTTP/2, which arrives with TLS.

**Polling remains supported and is not deprecated.** It is what the SSE
endpoint itself does internally, and any client that finds SSE inconvenient
loses nothing but latency.

## Alternatives considered

**WebSockets.** Rejected: bidirectional, which this traffic is not. It buys a
capability that would go unused in exchange for proxy configuration,
hand-written reconnection, and a second protocol to secure when TLS arrives.
Worth revisiting if a genuinely interactive feature appears — the SESSION 10
drag-and-drop editor, if it grows collaborative editing, is the plausible
candidate, and it would justify its own ADR.

**Polling only.** Rejected as the *sole* mechanism: a two-second poll from
every open tab is more requests than one held connection, and progress that
lags by up to the poll interval reads as a frozen UI on a run that changes
phase quickly. Kept as a supported fallback rather than removed.

**Redis pub/sub into the stream.** Rejected for now: it removes the database
poll but adds a delivery path that can succeed while the write fails, so a
client could be told a run finished before the row says so. The row is the
authority; the poll is what keeps that true. Worth revisiting only if the poll
becomes a measured problem.
