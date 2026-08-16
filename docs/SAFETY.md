# Safety profiles

The policy layer separates containment, resource protection, bounded capture,
and evidence admission. It classifies observations; the real process adapter
that will enforce them is not implemented yet. An accounting observation does
not implicitly terminate a session. Every condition maps to one of four scopes:

- `SESSION_STOP`: verified root/mount/containment drift, accounting corruption,
  process leakage, wall limit, runtime drift, or real disk danger ends the
  session cleanly.
- `JOB_STOP`: stop the current command or durable job; denied boundary access,
  ordinary workspace limits, and tool failures leave the session and its peers
  available.
- `REJECT`: reject one artifact, verifier result, or bounded read.
- `WARN`: retain an observation without interrupting research.

`research-default` has no independent single-file ceiling and does no live
full-tree polling. A workspace still has aggregate byte, entry and depth
bounds, while a host-wide free-space reserve directly protects the platform.
Large files, hardlinks, no-follow symlinks, build churn and output truncation
are observations rather than session failures.

Provider context/request limits are job-local in this default: an adapter may
compact, checkpoint or ask for a new request without declaring the research
session corrupt. A malformed PMW proposal is rejected as one action. The only
ten session-stopping codes are the real platform/session-integrity conditions
listed in the shipped profile.

`strict-experiment` preserves the important v9 experimental boundaries: a
64 MiB single-file ceiling, 500 ms live scans, and session-stopping workspace
or cache violations. It exists for historical reproduction and is not the
default research policy.

## Output capture

The retained cap is an evidence-storage cap, not an execution cap. Once it is
reached, the capture accumulator continues to drain the stream, count all
observed bytes, hash them, and keep a bounded tail. Only the separate observed
output safety cap returns `JOB_STOP`; the caller then terminates that job's
process group without terminating the owning research session.

The module is intentionally free of process-control code. Runtime adapters are
responsible for applying the returned disposition at exactly the documented
scope.
