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
repositories by URL and full commit. The managed source materializer resolves
those exact commits from an explicitly selected local Git object database; it
does not fetch a branch tip or use a dirty worktree as verifier authority. It
publishes the complete tree under `source-cache`, records a canonical manifest,
and audits the full tree digest against the lock rather than trusting a
self-authored manifest. `source materialize` is the explicit local publication
step; `source audit` is the read-only repeatable check. Runtime authentication
then loads `pmw_r2` directly from the audited tree's `src` directory. A wheel or
editable checkout may exist for development, but neither its parent `.git` nor
an old campaign worktree is runtime authority.

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

- every target card's mathematical content is included; predecessor-campaign
  `budget_contract` fields are omitted from operative problem content and
  represented separately by exact field/hash/byte provenance;
- every non-card admission appears once in a global record index as a bounded
  mathematical projection, parent refs, content hash, artifact refs and exact
  `world.get` retrieval key; problems join to it by admission ref;
- omissions are explicitly marked; no projection is presented as the full
  record or as truth ranking;
- the whole file is bound to the plan by SHA-256 and the exact snapshot ref.

The current M03-derived world has 14 targets and remains comfortably below the
16 MiB serialization guard. Exact briefing bytes and their digest are plan-
bound; the guard catches a world that needs a reviewed checkpoint instead of
silently truncating history.

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

Readiness also has one launch-bound identity. `session preflight` produces an
advisory read-only report without claiming the cohort, creating a runtime tree,
or starting a backend. `session start` then acquires the `RuntimeClaim`, repeats
the mutable backend-pin and required apparatus checks before creating the
launch, and binds their canonical public evidence plus digest into
`launch.json` and every invocation. This second check is authoritative; a
previous preflight PASS cannot be replayed across source or runtime drift.
`amf-production` is the default readiness scope and closes the briefing against
the locked AMF portfolio. Explicit `runtime-only` asserts only generic
transport readiness and must not be reported as mathematical-apparatus
readiness.

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

Model context is a separate immutable launch treatment. `ContextWindowPolicy`
has an optional cohort default and exact session overrides; unset means the
backend-declared window. A capable backend must report `NATIVE_MODEL_WINDOW`,
receive the selected value in `SessionRequest`, and verify it before useful
work. Pi applies it to the active model object, so Pi's native budgeting,
compaction threshold and overflow recognition see the chosen total window.
This is neither cumulative session token consumption nor a strict estimate-
then-block gate for provider input. Backends without a model window reject a
configured policy before a runtime directory is created.

For the current Pi implementation, a configured window is compatible only
with an empty external-extension list. Combining a configured window with any
external Pi extension fails before launch with
`PI_CONTEXT_EXTENSION_COMPATIBILITY_UNPROVEN`. Leaving the policy unset keeps
the backend-declared window and does not make a provider-route capacity claim.

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
installation tree, settings and explicit extension entry files. A sorted
allowlist may enable Pi's built-in workspace tools; custom tools still require
explicit pinned extensions. It sends one generic research prompt and observes
Pi-native events without host retry, implicit context downcap or model fallback.
Two required backend-identity fields make stronger runs explicit:
`expected_context_window_tokens` is either null or an exact positive state
assertion checked before and after the prompt, while `disable_auto_compaction`
is a strict boolean. When enabled, the adapter requires the first state to
already report `autoCompactionEnabled: false`, sends and checks an explicit
`set_auto_compaction(false)`, confirms a second state before prompting, and
fails immediately on any `compaction_start` or `compaction_end` frame.
The shipped example starts from `tools: []` and `extensions: []`; no workspace
tool is implicitly enabled. When a configured context window is selected,
external extensions are currently rejected as described above. The adapter
does not deliberately serialize credential values or paths into public identity. Raw
bounded child frames and stderr remain a trusted Pi/redaction boundary: a child
error could still echo data into evidence. Protocol/lifecycle tests use fake
RPC. One zero-provider local smoke started pinned Pi and issued only
`get_state`; it confirmed a selected 400000 window on the active model object
without changing settings, sending a prompt/provider request, refreshing OAuth,
making a network call or consuming model tokens. No real model/provider canary
was run. In particular,
`pi_reported_context_window` is not evidence that the account's OAuth route
accepted a request near that size.

Both adapters are trusted transports for managed cooperative processes. The
generic runtime does not claim to contain hostile code at the OS boundary; a
stronger sandbox/VM may implement the same four-method protocol.

In particular, Pi built-ins execute with the host account's permissions. An
explicitly enabled `bash` may inspect host-readable paths or use the network;
the prompt's instruction not to inspect credentials is cooperative, not
containment. Pi RPC frame and stderr capture are bounded as transport evidence,
but the adapter does not yet proxy and project each built-in tool result before
Pi puts it into model context. A very large `bash` result can therefore consume
the active context window. `tools: []` is the present least-privilege baseline,
not an assertion that the same safety holds after enabling built-ins.

## AMF apparatus and verifier lifecycle

The `amf-production` readiness checker derives every target/verifier binding
from the authenticated briefing, then cross-checks it against the full locked
`agent-math-frontier` portfolio, target cards, candidate schemas, registry and
verifier manifests. It audits the materialized source before launch, executes
no verifier and makes no model or network request during readiness.

Verifier execution is currently an explicit post-settlement host operation.
`verifier run` accepts a settled session, a briefing-bound target ID and a
workspace-relative candidate path. The host no-follow captures stable candidate
bytes into the artifact CAS, stages the pinned verifier source privately,
re-executes the verifier with bounded time/output and persists a
content-addressed receipt under the session evidence tree. The verifier runner
removes credentials and installs a top-level Python socket audit denial, but it
does not claim kernel-level network isolation.

A launch may additionally materialize a read-only, content-pinned **in-session
verifier kit** into each session workspace: a wrapper CLI executing the same
pinned verifier bytes locally, whose per-invocation verdict and receipt land in
a session-local evidence directory and are explicitly
`ADVISORY_IN_SESSION_VERIFICATION`. The kit's byte identity is frozen into
`launch.json`, the invocation surface announces its existence and command
without recommending a route, and the host counts observed invocations into the
session receipt. Because the workspace belongs to the session, that ledger is an
observation and not a tamper-proof measurement; the post-settlement host
execution remains the only authoritative verifier receipt.

There is still no live PMW read/query/peer-update coordination plane comparable
to the historical M01–M03 apparatus, and no agent-facing path can produce an
authoritative verdict. The runtime can carry an authenticated static briefing,
an advisory verification path and an optional end-of-session publisher; those
facts must not be presented as a full interactive mathematical-research
platform.

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
- no automatic model/OAuth canary, retry, host-triggered compaction or hidden
  context ceiling; config may explicitly require an exact reported window and
  forbid Pi auto-compaction;
- no authoritative agent-facing verifier and no PMW coordination tool plane;
  the in-session kit is advisory and its invocation ledger is observed, not
  proven;
- no automatic resume or reuse of a crashed/settled launch; cross-system
  publication gaps require explicit reconciliation;
- no new M04/M05 treatment, ballot or all-agent barrier; the agenda arm wired
  in WP-D is an instrument exposure validated at publication time, and it
  adds no ballot, no barrier and no host-side research control;
- no claim that a verifier PASS proves novelty or solves an open problem.
- launch identity binds the platform protocol, not an exact byte digest of the
  installed host package; archival reproduction must retain the repository
  commit alongside runtime evidence;
- explicit Pi extension entry files are pinned, but their transitive imports,
  package dependencies and external binaries are not discovered automatically;
  moreover, configured context windows currently reject every external Pi
  extension. A future extension-enabled production cohort needs both a
  reviewed dependency manifest and a reviewed context-mutation compatibility
  design;
- custom asynchronous publishers must supply their own bounded, cancellable
  transport; the built-in PMW publisher is synchronous.
