# Architecture v0.1

Status: **generic session runtime implemented; M04 remains explicitly frozen**.

## Authority boundaries

| Concern | Authority |
|---|---|
| Problem statements, formalization status and verifier contracts | `agent-math-frontier` |
| Immutable records, provenance, admissions and snapshots | `persistent-mathematical-worlds` |
| Cohort/launch identity, briefing, artifact CAS, runtime settlement and safety policy | this repository |
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

A plan stores mathematical input identity once and only varying session IDs per
entry. It freezes:

```text
world_id + world_ref + base_snapshot_ref
safety_profile + safety_profile_sha256
core_lock_sha256 + briefing_sha256
explicit session IDs + concurrency
```

`count` is only a construction convenience. The persisted session list is the
authority. A separate immutable `runtime/launch.json` binds that plan to one
backend identity, its public config, publication identity and lifecycle limits.
Publication is visibly either `DISABLED` or `PMW_BOUND`, and both the backend
and publication identities have canonical SHA-256 bindings. A fixed worker pool
starts no more than `concurrency` backend handles. Every session receives
private `input/workspace/cache/evidence` roots, an immutable terminal receipt,
and one ordered cohort settlement.

The backend protocol is deliberately only `identity`, `start`, `wait` and
idempotent `stop`. The host alone decides `SUCCEEDED/FAILED/CANCELLED/UNKNOWN`.
`CANCELLED` requires either no start or a positive cleanup proof; `UNKNOWN`
stops new work and is never recycled into a fresh concurrency slot.

`pmw_platform.sessions.run_cohort` remains a deterministic, model-free helper
for plan/scheduler tests; it does not create a launch or authorize an external
process. `pmw_platform.runtime.run_prepared_cohort` is the sole durable runtime
path. Keeping that distinction explicit avoids a second launch authority.

Lifecycle limits have one authority. `startup_seconds`,
`session_wall_seconds` and `stop_grace_seconds` live in the host-authenticated
launch; wall and grace are carried in `SessionRequest`. Command/Pi configs do
not carry competing wall/stop values. `stop()` must remain idempotent and close
all adapter-owned work before returning. The trusted adapter bounds each
graceful, forced-cleanup and evidence-closure phase; the host never writes a
settlement after timing out and abandoning a hidden cleanup task.

## Trusted publish boundary

The agent-facing proposal is a `ResearchContribution`: mathematical content,
parents and artifact refs, with no identity fields. The trusted host binds it
to a frozen `SessionSpec` and constructs the durable `ResearchRecord`.

The low-level PMW writer is private to `ResearchWorld`; callers receive a
`BoundResearchSession`. Binding checks world ID/ref and base snapshot.
Publishing checks parents and requires every referenced artifact to resolve in
the global CAS. Runtime authentication reloads the canonical saved plan and
never accepts a caller-created `SessionSpec`. A writer authority is provisioned
only to trusted host code and never appears in `SessionRequest`, backend
identity, workspace or receipt. Its bounded public identity is nevertheless
recorded in `launch.json` as `PMW_BOUND`; without it the launch records
`DISABLED` and contributions cannot be admitted.

PMW admission and the local runtime receipt are two durable commits. A host
crash after an admission succeeds but before its receipt is written therefore
requires operator reconciliation against the launch and PMW admission log.
The runtime is deliberately non-resumable and must not silently rerun that
session.

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

## Runtime adapters

The built-in command adapter starts a new process group, uses a filtered
session-local environment, drains both output streams, retains bounded evidence
while hashing every observed byte, and performs TERM → grace → KILL. A leader
exit with a surviving group is cleaned and reported as failure. This is
cooperative process-group containment, not an OS sandbox.

The Pi RPC adapter pins the Node executable, Pi entrypoint, complete Pi
installation tree, settings and explicit extension entry files. It disables
built-in tools, sends one generic research prompt, and observes Pi-native events without
host retry, compaction, context downcap or model fallback. The adapter does not
deliberately serialize credential values or paths into public identity. Raw
bounded child frames and stderr remain a trusted Pi/redaction boundary: a child
error could still echo data into evidence. Protocol/lifecycle tests use fake RPC;
the local installation and OAuth *type* have only been loaded read-only for
compatibility. No new real model/provider canary was run. In particular,
`pi_reported_context_window` is not evidence that the account's OAuth route
accepted a request near that size.

Both adapters are trusted transports for managed cooperative processes. The
generic runtime does not claim to contain hostile code at the OS boundary; a
stronger sandbox/VM may implement the same four-method protocol.

## Resource and safety enforcement

The authenticated profile classifies named observations with four
dispositions:

- `SESSION_STOP`: real containment drift, unclean process leakage, accounting
  corruption, session wall limit or runtime/root drift;
- `JOB_STOP`: one command/request limit or denied boundary action;
- `REJECT`: one invalid artifact, read or PMW proposal;
- `WARN`: observations such as ordinary hardlinks, large files and build churn.

Backend output bounds are separate launch identity, not profile policy:
command byte streams and Pi RPC frames need different schemas. The retained
profile `captures.{bash,long_job}` rows are legacy metadata only and are not a
second enforcement path.

The resource guard enforces host free-space reserve plus aggregate workspace and
cache bytes, entry count and depth. It never follows symlinks; regular-file
bytes are deduplicated by `(device, inode)`, so hardlinks are not punished
(their names still count as entries). It does not enforce a per-file limit.
`research-default` performs tree scans at session activation and after the
adapter's stop operation settles, with a low-frequency live disk-reserve check;
only a profile selecting `LIVE_LATCHED` adds live tree scans. Historical
single-file fields remain profile metadata, not a promise that this generic
guard reproduces the old 64 MiB kill rule. Exact legacy replay still needs the
archived apparatus. A tree breach is scoped to its session; a disk-reserve
breach or unreliable accounting stops new cohort work, and unproven cleanup
makes the settlement `UNSAFE`.

To preserve the already frozen profile hash, cache-depth overflow reuses the
existing `RUNTIME_CACHE_ENTRY_LIMIT_EXCEEDED` policy code. The typed receipt
still distinguishes it by recording `maximum_depth` as the observed and limit
metric; this compatibility alias can be split only in a deliberate profile
schema migration.

## Deliberate non-goals for v0.1

- no claim that process-group control equals OS containment;
- no automatic model/OAuth canary, retry, compaction or context ceiling;
- no automatic resume or reuse of a crashed/settled launch; cross-system
  publication gaps require explicit reconciliation;
- no new M04/M05 treatment, ballot or all-agent barrier;
- no claim that a verifier PASS proves novelty or solves an open problem.
- launch identity binds the platform protocol, not an exact byte digest of the
  installed host package; archival reproduction must retain the repository
  commit alongside runtime evidence;
- explicit Pi extension entry files are pinned, but their transitive imports,
  package dependencies and external verifier binaries are not discovered
  automatically; a tool-enabled production cohort needs a reviewed,
  adapter-specific dependency manifest in public identity;
- custom asynchronous publishers must supply their own bounded, cancellable
  transport; the built-in PMW publisher is synchronous.
