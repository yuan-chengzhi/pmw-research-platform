# Architecture v0.1

Status: implementation baseline

## One world, replaceable sessions

The canonical mathematical state is a linear, content-addressed PMW world.
Every cohort freezes one base snapshot for orientation. Sessions may observe
new records through an explicit delta view; the next cohort simply attaches to
the then-current head. No predecessor wrapper or `M0i -> M0i+1` import exists.

The platform uses a stable host capability to perform PMW admissions. The
outer broker authenticates each session and embeds `cohort_id`, `session_id`,
the base snapshot, parents, and provenance in every generic research record.
The PMW core continues to enforce exact snapshots, visibility, idempotency,
linear Git CAS, and independent audit.

## Source authority

| Concern | Authority |
|---|---|
| Problem statements and verifier contracts | `agent-math-frontier` |
| Durable identity, provenance, admission and snapshots | `persistent-mathematical-worlds` |
| Sessions, concurrency, runtime safety and receipts | this repository |
| Historical M01–M03 evidence | archived source campaign and immutable run data |

Dependencies are selected by repository URL and full commit in
`config/core-lock.json`. Local paths are deployment details and never become
the identity of a dependency.

## Generic record

A research record has a small typed envelope and a JSON payload. The envelope
states its kind, authoring session, cohort, target IDs, parent admissions and
base snapshot. Large artifacts are content-addressed separately and referenced
from the payload. The platform does not attempt to force all mathematical
thought into one ontology.

Initial kinds are `NOTE`, `NEED`, `ATTEMPT`, `RESULT`, `OBJECTION`, and
`CHECKPOINT`. Legacy M03 records are exposed through a read-only compatibility
view; they are never rewritten.

## Scheduling

`count` determines how many explicit session IDs are created. `concurrency`
is a semaphore bound and may be smaller than `count`. Each session settles
independently. A tool error or agent failure is local; other sessions continue.
The cohort receipt summarizes individual receipts but is not mathematical
authority.

## Research-default safety

The default policy has four layers:

1. OS containment for credentials, sibling workspaces, network and signals.
2. Coarse host guards for disk reserve, wall time, requests, context and cost.
3. Bounded capture: large output is drained, counted, hashed, tailed and
   optionally externalized; retained output never grows without bound.
4. Evidence admission: malformed or oversized submissions are rejected as an
   action, not retroactively treated as a failed research session.

Only containment drift, surviving leaked processes, hard provider/accounting
limits, or real disk emergency are session-terminal. Large files, hardlinks,
ordinary no-follow symlinks, build churn and retained-output truncation are
metrics or action-local outcomes. The historical v9 behavior is named
`strict-experiment` and stays available only for reproduction.

## Deliberate non-goals for v0.1

- no real model calls or OAuth canary;
- no new M04/M05 treatment;
- no ballot or all-agent barrier;
- no copy of the old campaign launcher;
- no claim that a verifier PASS is a novelty or final mathematical result.
