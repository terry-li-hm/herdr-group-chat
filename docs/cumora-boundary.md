# Cumora boundary

Herdr Group Chat remains a local relay for visible native agents. It does not use Cumora as its backend.

## Why

The two systems own different loops.

- Herdr Group Chat prompts already-running Herdr sessions. The relay alone appends replies, so ordinary turns cannot race at the transcript boundary.
- Cumora owns network rooms, durable server state, wake scheduling, presence, and separate BYOA engine sessions.

Putting Cumora below the current relay would leave both systems responsible for wakeups, cursors, cancellation, and agent lifecycle. It would also add authentication, Postgres, Redis, and network availability to a local plugin whose useful constraint is that it works with Herdr and local files alone.

## Reuse rule

Borrow a Cumora mechanism only after Herdr Group Chat exhibits the failure that mechanism prevents and a focused assay reproduces it.

- Add wake debounce or coalescing only if messages can arrive while a turn is active and duplicate engine turns are observed.
- Add quota-lane pacing only if two participants are shown to share one provider limit and concurrent review calls cause throttling.
- Add a freshness or `HELD` gate only if participants gain a direct or concurrent transcript-write path. The current relay is the sole writer, so this gate would duplicate an invariant already enforced by structure.

Port the smallest mechanism that closes the measured gap. Do not copy Cumora's server, scheduler, persistence layer, or standing prompt as a bundle.

## Pivot rule

A requirement for mobile access, cross-host rooms, shared server persistence, or asynchronous team presence would change the product boundary. At that point, make Herdr Group Chat a thin Cumora terminal client and let Cumora own messages, wakeups, presence, claims, and coordination.

That pivot must replace local backend ownership for the Cumora-backed mode. Do not dual-write transcripts or run both Herdr and Cumora schedulers against the same participant identity.
