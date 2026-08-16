# Migration and provenance

## Starting point

The canonical seed is the settled M03 world, not an M04 fresh world:

- source campaign worktree (relative to `~/Documents`):
  `pmw-live-pi-frontier-choice-m02-official-context`
- source campaign commit: `03fffadabcd983dd58c12671c6af799b62e49824`
- source world ref: `refs/pmw/frontier-choice-world`
- settled snapshot:
  `snapshot/sha256/803bfcd0604ff01c9b560d9e82d6e2a9f606d6c36312418f2e95cf1193f3535a`
- PMW core commit: `4880f184c60bd34181302c5343ec0db95f154851`
- archived source Git remote:
  `https://github.com/yuan-chengzhi/pmw-frontier-choice-experiments`

The source world is copied into the managed data root before use. The M03
repository remains immutable evidence; the platform never writes into it.

## Managed continuation now

The following model-free continuation is complete:

- managed world: `pmw-research-data/worlds/math-frontier.git`;
- exact ref/snapshot: the M03 values above, with no Git alternates;
- 63/63 world artifact refs independently copied and resolved under
  `pmw-research-data/objects` (13,289,736 payload bytes);
- closure manifest:
  `objects/imports/math-frontier-m03-closure.json`, SHA-256
  `fbf436eec89f7c9c711faaf8a1ee490c5c699ff858d1a32ca46d0316cd9ff749`;
- current zero-model production-candidate cohort:
  `runs/platform-runtime-acceptance-v2-20260816`, with four explicit sessions
  at concurrency four;
- situation-v2 briefing: 14 problems, 174 admissions, 501,644 bytes, SHA-256
  `401c39cbd5a52ac3834f6b0dfaa37095ea0da9a0ce007bd9db9c9acd5ee52d04`;
- plan SHA-256:
  `d736354c87543e1d7016cac4802343459c581fe48437c3a60367c53a9b9ef683`;
- launch SHA-256:
  `83f902f0d7cd17a29d9d94fdb1f04496fb79a83f41831bad84873b4789b8090d`;
- settlement SHA-256:
  `0408d9ea9891e7929a72a2c7a0b141ff5f4ddef6f5116472a203263f3544f610`;
- settlement: `SUCCEEDED`, four succeeded, zero failed/cancelled/unknown;
  every receipt reports zero model calls and zero network calls.

The launch-bound AMF readiness closure covers 14 briefing targets, 15 catalog
verifiers, registry SHA-256
`1dac2db72030b8c0be6ae8d233e4665ef32583e72ad7abb3aaa65b734a0b3571`,
and required-readiness SHA-256
`94664b72b28554c6bd13fc60d38c745846ab7c53ef5c0bac1761e6651a345db6`.

Earlier scaffold bundles were moved, not deleted, under
`archive/model-free-scaffolds/`; their names state which pre-contract identity
or briefing shape they predate. The old foundation bundle is now
`foundation-acceptance-20260816-pre-core-lock-v2`. A first real runtime
acceptance attempt exposed a wrong nested invocation-field assumption in the
example worker; its honest four-failure settlement is retained under
`archive/model-free-acceptance-failures/platform-runtime-candidate-20260816`.
The worker was corrected before the successful cohort above. Neither attempt
called a model or network.

## M04 freeze

M04 was never launched. Its authorization and billing consent were moved to an
owner-only revoked directory, and tag `archive/m04-v9-prelaunch` identifies the
exact prelaunch apparatus. The tag is also on the archived public remote. A
preflight in that archived v9 M04 apparatus fails with
`NO_LAUNCH_AUTHORIZATION` before any model call. This is historical M04 state,
not the generic platform's `session preflight` result.

## Retirement rule

Old worktrees and detached pin directories may be removed only after all of
the following hold:

1. every unique commit is reachable from a named remote branch or tag;
2. all absolute path references have been replaced by URL + commit locks or a
   managed source cache;
3. M01–M03 run data is indexed with hashes and required artifacts have an
   independent content-addressed copy;
4. the 1/4/8 model-free acceptance suite and one copied-M03 continuity test
   pass.

Until then, old worktrees are read-only evidence, not active platform code.
