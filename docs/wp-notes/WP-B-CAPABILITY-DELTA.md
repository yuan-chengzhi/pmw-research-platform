# WP-B capability delta — in-session verifier kit

Branch `codex/wp-b-verifier-kit`, off `codex/generic-session-runtime`.
Zero model calls, zero network calls, no push. Fold this into the README
capability matrix and the two architecture/safety paragraphs named below at
merge time; the branch deliberately does not rewrite the README.

## What changed, in one sentence

The host can now materialize a **read-only, content-pinned AMF verifier kit**
into every session workspace, so a research session has a verification path it
can actually exercise during research; every invocation writes an advisory
verdict plus an invocation receipt, and the host counts those invocations into
the session receipt. The post-settlement `AmfVerifierService` path is
unchanged and remains the sole authority.

## Capability matrix delta

| Capability | Before this branch | After this branch |
|---|---|---|
| Verifier | host-authoritative re-verification of a settled candidate only; nothing verifier-shaped inside a session | unchanged authority, **plus** a pinned read-only kit in each workspace (`.pmw-verifier-kit/bin/amf-verify`) whose verdicts are explicitly `ADVISORY_IN_SESSION_VERIFICATION` |
| Session evidence | host-written transport evidence only | additionally a session-local advisory ledger under `<workspace>/.pmw-verifier-evidence/`, counted (not trusted) by the host |
| Launch identity | backend / publication / readiness / context identities | additionally `verifier_kit` + `verifier_kit_sha256`, binding the exact kit bytes |
| Session receipt | status, stop proof, outcome, resource guard, context window | additionally `verifier_kit`: invocation count, per-status verdict counts, rejected entries, ledger state |
| Prompt surface | briefing + invocation JSON | invocation JSON additionally carries a non-prescriptive `verifier_kit` announcement; the Pi prompt header points at it in one sentence |

Still absent, and not claimed by this branch: a live PMW `search/get/updates_since`
plane, live peer-update notification, live artifact submission, and any
agent-facing path that can produce an *authoritative* verdict.

## Design points a reviewer should check

**The kit runs the same bytes settlement runs.** `build_verifier_kit` loads the
verifier registry, manifests and source artifacts through the authoritative
`AmfVerifierService` loader (its read-only inspector construction, as
`audit_portfolio` already does) and ships those exact bytes. The in-session
runner executes them through the *same* offline bootstrap string imported from
`pmw_platform.verifier`, with the same isolated interpreter flags, the same
credential-free environment, the same timeout/output caps from the manifest,
and the same process-group cleanup. Two copies of that bootstrap could drift,
so there is only one.

**The kit is path-independent, so one digest covers the whole cohort.** The
wrapper resolves its own root from `$0`; the runner derives the workspace and
the evidence directory from the kit root. Nothing session-specific is baked in,
so every session in a cohort receives byte-identical files and `launch.json`
carries one `kit_sha256`.

**Session-local evidence lives in the workspace, not in the host evidence
tree.** Two reasons, both load-bearing: the resource guard accounts for
workspace bytes and would not account for agent writes under `evidence/`, and
`runtime/sessions/<id>/evidence/` must stay host-written. The consequence is
stated plainly rather than hidden: the workspace belongs to the session, so the
ledger is *observed*, never trusted. The receipt records
`counting_authority: HOST_OBSERVED_SESSION_LOCAL_ADVISORY_LEDGER_NOT_TAMPER_PROOF`,
counts only receipts whose schema, authority and `kit_content_sha256` match the
launch-bound kit, and reports everything else as `rejected_entries`. An
unreadable ledger is reported as `UNREADABLE`, never as a measured zero.

**No credentials enter the workspace.** Kit content is exactly three things:
platform-shipped runner/wrapper bytes, pinned AMF verifier source bytes, and
public pin digests plus the resolved interpreter path. The bindings document is
scanned for credential-shaped keys before it is written, the manifest asserts
`credential_material: false`, and the launch validator rejects a materialized
kit that does not assert it. A test asserts the host data root (and therefore
the `source-cache` path) appears in no kit byte.

**Read-only mode bits are hygiene, not containment.** Kit directories are
`0555` and files `0444`/`0555`, but the session owns the workspace and can
change them. That is why the runner re-hashes every pinned source artifact, the
interpreter and the bootstrap at each invocation and fails closed with
`KIT_SOURCE_PIN_MISMATCH` / `KIT_INTERPRETER_DRIFT` / `KIT_BOOTSTRAP_PIN_MISMATCH`.

**The announcement announces and does not recommend.** The invocation record
states that the kit exists, the exact command, accepted arguments, exit codes,
where verdicts are written, and that the count reaches the session receipt. It
then states four facts: the bytes match settlement's; an in-session verdict is
not an admission, a novelty claim or a solved problem; running the kit is
neither required nor recommended; and not running it is a permitted outcome
recorded as zero. No route, no ordering, no success criterion.

## Surfaces added

- `src/pmw_platform/verifier_kit.py` — build, materialize, announce, count.
- `src/pmw_platform/verifier_kit_runner.py` — the standard-library-only runner
  shipped into the workspace as `lib/amf_verify.py`. It never imports
  `pmw_platform`; the platform package must not become a session dependency.
- `bin/amf-verify [--target TARGET_ID] CANDIDATE` / `--list-targets`.
  Exit codes: `0` PASS, `1` REJECTED, `2` APPARATUS_ERROR, `64` invalid
  invocation (no verdict written).
- `pmw-research session start --no-verifier-kit` opts out; `runtime-only`
  readiness scope ships no kit, because that scope asserts no mathematical
  apparatus. The start result reports `verifier_kit_sha256`.

## Schema changes that need a merge decision

`launch.json` gained two **required** fields (`verifier_kit`,
`verifier_kit_sha256`) and `receipt.json` gained one (`verifier_kit`), while
`PMW_RUNTIME_LAUNCH_1` / `PMW_RUNTIME_SESSION_RECEIPT_1` kept their names. A
`launch.json` written before this branch is therefore rejected as
`MALFORMED_RUNTIME_LAUNCH`. That is fail-closed and correct for an
incompatible identity, but the *name* is now overloaded. WP-A adds a usage
block to the same receipt, so the version bump is a coordinated merge decision
rather than a per-WP one; do it once, for both documents, at fold-in.

## Docs that are now stale

Two sentences elsewhere in the tree were corrected in place because leaving a
false capability claim in this repository would be worse than a small merge
conflict:

- `docs/ARCHITECTURE.md`, "AMF apparatus and verifier lifecycle" and the v0.1
  non-goals list.
- `docs/SAFETY.md`, the AMF verifier paragraph.

The README capability matrix row for "Verifier" is **not** edited here; use the
table above.

## Tests

Full suite: **231 passed, 2 skipped** (baseline on this branch point: 221
passed, 2 skipped). New coverage, all model-free and network-free:

- `tests/test_verifier_kit.py` (8 tests)
  - a command-backend session writes a fixture candidate, invokes the kit once,
    settles `SUCCEEDED`; the launch carries the kit digest, the receipt shows
    `invocation_count: 1` with `PASS: 1`, the workspace holds verdict
    `000001.json` and invocation receipt `000001.json`, `invocation.json`
    carries the announcement, and the unchanged host verifier independently
    re-verifies the same candidate to `PASS` under
    `HOST_REEXECUTED_PINNED_AMF_VERIFIER`;
  - a cohort launched without a kit announces `available: false` and records a
    `DISABLED` launch block and a `NOT_MATERIALIZED` receipt block;
  - two workspaces receive byte-identical read-only files, the data-root path
    appears nowhere in the kit, and the manifest self-describes correctly;
  - direct wrapper invocation covers `--list-targets`, ambiguous target
    selection (`64`), PASS (`0`), REJECTED (`1`), an out-of-workspace candidate
    (`2`, `UNSAFE_CANDIDATE_PATH`), and ordinal allocation `000001`–`000003`;
  - ledger counting rejects a forged `kit_content_sha256`, unparsable bytes and
    an entry that tries to promote itself to the settlement authority;
  - a session that never starts still settles, with an `ABSENT` ledger;
  - a second materialization into one workspace fails `VERIFIER_KIT_PATH_OCCUPIED`;
  - the store's in-session vocabulary literals equal the producer's constants.
- `tests/test_runtime_cli.py` (2 tests) — the default `amf-production` scope
  routes a built kit into `run_prepared_cohort` and reports its digest;
  `runtime-only` builds none.

## Honest unfinished items

1. **Surfacing is implemented, not validated.** The announcement is structured
   data on the authenticated invocation surface plus one sentence in the Pi
   prompt header. Whether that is salient enough to move invocation counts off
   zero is exactly the open empirical question this apparatus exists to
   measure; no claim is made here that it will.
2. **No live model has ever run this kit.** Every test uses the command backend
   or a direct subprocess. The Pi path is covered only by the prompt-text
   change; there is no zero-provider Pi smoke for the kit.
3. **The advisory ledger is forgeable.** A session can delete, duplicate or
   fabricate entries. The host's counting rejects entries not bound to the
   launch kit digest, but a session that re-uses that public digest can inflate
   its count. Invocation counts are behavioural evidence about a cooperative
   session, not a tamper-proof measurement, and must not be used as an
   incentive-bearing score without a stronger channel.
4. **In-session and host results can legitimately differ.** The kit verifies
   the bytes at the moment of invocation; the host verifies the bytes present
   at settlement. Nothing pins the candidate between the two, and the kit does
   not capture candidates into the artifact CAS.
5. **Interpreter re-hashing is per invocation.** Cheap for a typical launcher
   binary, but a large statically linked interpreter would make each invocation
   pay for it.
6. **Kit materialization is not transactional.** A failure part-way leaves a
   partial `.pmw-verifier-kit/` and fails that session closed; the operator
   sees the partial tree rather than an automatic rollback.
7. **`--list-targets` is the only complete target enumeration** when a
   portfolio exceeds 64 targets; the launch block deliberately carries only
   `target_count` and `target_ids_sha256` so it stays a fixed-size identity.
8. **No preflight report of the kit.** `session preflight` neither builds nor
   describes it, so an operator learns the kit digest only from `start`.
