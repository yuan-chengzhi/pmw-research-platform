# Architecture v0.1

Status: **model-free control plane implemented; real agent runtime pending**.

## Authority boundaries

| Concern | Authority |
|---|---|
| Problem statements, formalization status and verifier contracts | `agent-math-frontier` |
| Immutable records, provenance, admissions and snapshots | `persistent-mathematical-worlds` |
| Cohort identity, briefing, artifact CAS, scheduling primitives and safety policy | this repository |
| M01–M03 experiment evidence | archived frontier campaign |

The packaged lock at `src/pmw_platform/locks/core-lock.json` binds the first two
repositories by URL and full commit. It is a validated identity lock, not yet a
source materializer; `source-cache/` remains future adapter work.

## Long-lived world

The managed `math-frontier` world continues the exact settled M03 snapshot.
There is no predecessor wrapper and no automatic `M0i → M0i+1` import. A
cohort freezes one base snapshot for orientation; later cohorts attach to the
then-current head.

The world adapter currently provides exact read, delta, get, audit and PMW
admission. Immutable snapshot views are cached in a small bounded LRU. The PMW
core still owns linear Git CAS, visibility, idempotency and audit semantics.

## Mathematical situation

`build_mathematical_situation` produces the exact `briefing.json` used by a
cohort:

- every target card is included in full;
- every non-card admission appears once in a global record index as a bounded
  mathematical projection, parent refs, content hash, artifact refs and exact
  `world.get` retrieval key; problems join to it by admission ref;
- omissions are explicitly marked; no projection is presented as the full
  record or as truth ranking;
- the whole file is bound to the plan by SHA-256 and the exact snapshot ref.

The settled M03 briefing is about 519 KiB for 14 problems and 174 admissions.
A 16 MiB serialization guard catches a world that needs a reviewed checkpoint
instead of silently truncating history.

## Session and cohort identity

A plan stores common launch identity once and only varying session IDs per
entry. It freezes:

```text
world_id + world_ref + base_snapshot_ref
safety_profile + safety_profile_sha256
core_lock_sha256 + briefing_sha256
explicit session IDs + concurrency
```

`count` is only a construction convenience. The persisted session list is the
authority. One cohort supports 1–4096 sessions; a fixed worker pool limits
active callables to `concurrency`, so memory use does not grow by creating N
tasks up front. Receipts carry the full plan identity. This scheduler has been
tested with deterministic callables, not real agent processes.

## Trusted publish boundary

The intended agent-facing proposal is a `ResearchContribution`: mathematical content,
parents and artifact refs, with no identity fields. The trusted host binds it
to a frozen `SessionSpec` and constructs the durable `ResearchRecord`.

The low-level PMW writer is private to `ResearchWorld`; callers receive a
`BoundResearchSession`. Binding checks world ID/ref and base snapshot.
Publishing checks parents and requires every referenced artifact to resolve in
the global CAS. A future process/tool server must preserve this boundary and
must never hand the PMW host capability to an agent. The current model-free API
does not authenticate a caller-created `SessionSpec`; real runtime work must
authenticate it against the saved plan before exposing this bound surface.

## Artifact ownership

Large evidence is stored under:

```text
pmw-research-data/objects/
├── sha256/<artifact digest>
├── artifact-receipts/sha256/<receipt digest>.json
└── imports/<validated import manifest>.json
```

Imports use exact-byte copies, no-follow reads, atomic publication and
byte-identical deduplication. They intentionally do not symlink or hardlink to
retirable campaign directories. Hardlinked *source files* are accepted as
ordinary files and copied; hardlinks are not treated as research misconduct.

## Safety: policy now, enforcement later

Implemented primitives are a validated policy map and a bounded output
accumulator that drains, counts, hashes and tails data without retaining it
unboundedly. They do not themselves spawn, scan or kill processes.

The future process adapter must apply these scopes exactly:

- `SESSION_STOP`: real containment drift, unclean process leakage, accounting
  corruption, session wall limit, runtime/root drift or disk emergency;
- `JOB_STOP`: one command/request limit or denied boundary action;
- `REJECT`: one invalid artifact, read or PMW proposal;
- `WARN`: observations such as ordinary hardlinks, large files and build churn.

`research-default` has no single-file ceiling and no live full-tree scan.
`strict-experiment` preserves v9-era behavior only for historical replay.

## Deliberate non-goals for v0.1

- no `session start`, agent subprocess, Pi/OpenAI request or OAuth canary;
- no claim that policy parsing equals OS containment;
- no provider usage/cost accounting or final durable settlement implementation;
- no new M04/M05 treatment, ballot or all-agent barrier;
- no claim that a verifier PASS proves novelty or solves an open problem.
