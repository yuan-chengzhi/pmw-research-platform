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
does not impose a 325k/360k ceiling, compact, retry or silently change models;
a model adapter records the backend-reported context window and returns any
provider refusal as the actual outcome. For Pi,
`pi_reported_context_window` is runtime/model-catalog metadata, not a canary of
the account's OAuth route near that limit. A malformed PMW proposal is rejected
as one action.

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

These controls are operational guardrails, not a hostile-code OS sandbox.
Process groups and filtered environments do not prevent a deliberately
adversarial program from using everything the host OS account can access.
