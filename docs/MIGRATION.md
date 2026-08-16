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
- current zero-model acceptance bundle:
  `runs/foundation-acceptance-20260816/{plan.json,briefing.json}`, plan SHA-256
  `73562a9f2438f4474b12690e18c379b261ef55076fb958eb4c17d8a427103d8b`;
- briefing: 14 problems, 174 admissions, 530,618 bytes, SHA-256
  `dc3a2bfaaaa6740a5b0f544e30160079589143b9c6abb42e36d5992ef6b0e7a7`.

Earlier scaffold bundles were moved, not deleted, under
`archive/model-free-scaffolds/`; their names state which pre-contract identity
or briefing shape they predate.

## M04 freeze

M04 was never launched. Its authorization and billing consent were moved to an
owner-only revoked directory, and tag `archive/m04-v9-prelaunch` identifies the
exact prelaunch apparatus. The tag is also on the archived public remote. A
preflight now fails with `NO_LAUNCH_AUTHORIZATION` before any model call.

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
