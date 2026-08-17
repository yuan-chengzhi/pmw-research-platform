# Safety profiles

The policy layer separates containment, resource protection, bounded capture,
and evidence admission. The host owns lifecycle and durable settlement, the
resource guard owns aggregate workspace/cache and disk accounting, and each
adapter owns cleanup of its managed process. Every named policy condition has
one of four action dispositions. Receipt events separately record whether the
observation is session-scoped or shared across the cohort:

- `SESSION_STOP`: verified root/mount/containment drift, accounting corruption,
  process leakage, wall limit or runtime drift ends the affected session.
- `JOB_STOP`: stop the current command or durable job, without using that event
  alone to stop peer sessions or the cohort.
- `REJECT`: reject one artifact, verifier result, or bounded read.
- `WARN`: retain an observation without interrupting research.

A host-disk reserve is shared, so its event is broadcast to the cohort even
though the historical profile action label is `SESSION_STOP`; the event's
`scope: COHORT` is the operational authority. Workspace/cache breaches remain
session-scoped.

The generic resource guard is intentionally narrower than the historical
campaign audit. It enforces only:

- aggregate logical bytes, entry count and depth for each workspace/cache;
- a host-filesystem free-space reserve; and
- reliable accounting (an uncertain scan makes the cohort `UNSAFE`).

Regular-file bytes are counted once per `(device, inode)`, while each directory
name still counts as an entry. Symlinks are counted but never followed. There
is no per-file check and no “hardlink exists” check, so a large research file,
an autoconf hardlink or a symlink is not by itself a kill condition.

`research-default` selects `QUIESCENT`: tree accounting occurs when a session
is activated and after its adapter stop operation settles, not continuously
while it writes. An unproven stop already makes the session `UNKNOWN`, whether
or not that final snapshot is stable. A low-frequency disk-reserve check remains live because exhaustion
can affect the whole platform. A profile may explicitly select `LIVE_LATCHED`
for live aggregate tree checks.

Provider context/request limits are job-local in this default. The platform
does not impose a hidden 325k/360k ceiling, retry or silently change models.
An operator may explicitly bind a total model window per launch/per session;
unset preserves the backend declaration. Pi applies a configured value before
the first prompt and the host checks the reported active model window. This is
not a cumulative token allowance or a strict pre-HTTP input gate. A model
adapter records both the configured and backend-reported values and returns any
provider refusal as the actual outcome. For Pi,
`pi_reported_context_window` is runtime/model-catalog metadata, not a canary of
the account's OAuth route near that limit. A malformed PMW proposal is rejected
as one action.

Configured Pi context and external extensions are not presently a supported
combination. Preflight and launch reject that combination with
`PI_CONTEXT_EXTENSION_COMPATIBILITY_UNPROVEN`; an unset policy continues to use
the backend declaration. This fail-closed compatibility rule does not establish
that an OAuth route will accept an input near any declared or selected window.

Readiness has an advisory and an authoritative phase. `session preflight` is a
read-only snapshot and creates no runtime claim or launch. `session start`
rechecks backend pins and every required readiness checker while holding the
`RuntimeClaim`, before creating runtime state, and binds the canonical public
evidence to `launch.json`. The default `amf-production` scope also audits the
locked source tree and briefing-bound verifier portfolio; `runtime-only` makes
no mathematical-apparatus assertion.

`strict-experiment` retains historical profile metadata and selects live
aggregate scans, but the generic guard still does not enforce its legacy
64 MiB per-file field. Exact reproduction of v9-era kill behavior belongs to
the archived apparatus; this profile must not be presented as a drop-in replay.
For backward-compatible profile identity, a cache-depth breach uses the existing
`RUNTIME_CACHE_ENTRY_LIMIT_EXCEEDED` policy code; its receipt evidence names
`maximum_depth` in both observed and limit fields, so it is not confused with
an entry-count measurement.

## Output capture

Output semantics and limits belong to the selected backend's public launch
identity: command has stdout/stderr bytes, while Pi has JSONL frames and a
separate stderr stream. The profile's historical `captures.{bash,long_job}`
rows remain validated legacy metadata and are not a second runtime authority.

For command, the retained cap is an evidence-storage cap, not an execution
cap. Once it is reached, the capture accumulator continues to drain the stream,
count all observed bytes, hash them, and keep a bounded tail. Only the separate
observed output safety cap returns `JOB_STOP`; the command adapter then stops
that backend process group and leaves peer sessions alone.

The policy module remains free of process-control code. The command adapter
keeps draining through cleanup. Its canonical backend config and the host's
single set of startup/wall/stop lifecycle limits are bound into `launch.json`,
so output and lifecycle treatment are visible and each has one authority.

The Pi example intentionally has `tools: []` and `extensions: []`. Explicitly
enabling a Pi built-in changes the threat surface: built-in `bash` runs with the
host account's filesystem and network authority, so filtered environment
variables and process groups do not stop it from probing other readable paths.
The adapter bounds retained RPC frame/stderr evidence, but it does not yet
provide a bounded-output proxy between a built-in tool and Pi's model context.
Consequently a large tool result may consume the configured active context
window even when retained transport evidence remains bounded.

The AMF verifier is host-authoritative and post-settlement. It captures a
workspace-relative candidate into CAS and re-executes a verifier from the
audited locked source, with bounded output/time, stripped credentials and a
top-level Python socket audit denial. This is not kernel network isolation.

A launch may also materialize a read-only, content-pinned verifier kit into a
session workspace. It ships only platform bytes, pinned verifier source bytes
and public pin digests — no credential material — and runs the pinned verifier
under the same isolated interpreter, credential-free environment, socket-audit
bootstrap, manifest time/output caps and process-group cleanup as the host
path. Its verdicts are `ADVISORY_IN_SESSION_VERIFICATION` and never settle
anything. Read-only mode bits inside a session-owned workspace are hygiene, not
containment, so the kit re-checks every pinned digest at each invocation, and
the host counts the session-local invocation ledger as an observation rather
than a proven measurement. The runtime still exposes no live PMW
read/query/peer-update tool plane.

These controls are operational guardrails, not a hostile-code OS sandbox.
Process groups and filtered environments do not prevent a deliberately
adversarial program from using everything the host OS account can access.
