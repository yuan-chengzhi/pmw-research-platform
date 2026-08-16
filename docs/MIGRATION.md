# Migration and provenance

## Starting point

The canonical seed is the settled M03 world, not an M04 fresh world:

- source campaign repository:
  `/Users/ycz/Documents/pmw-live-pi-frontier-choice-m02-official-context`
- source campaign commit: `03fffadabcd983dd58c12671c6af799b62e49824`
- source world ref: `refs/pmw/frontier-choice-world`
- settled snapshot:
  `snapshot/sha256/803bfcd0604ff01c9b560d9e82d6e2a9f606d6c36312418f2e95cf1193f3535a`
- PMW core commit: `4880f184c60bd34181302c5343ec0db95f154851`

The source world is copied into the managed data root before use. The M03
repository remains immutable evidence; the platform never writes into it.

## M04 freeze

M04 was never launched. Its authorization and billing consent were moved to an
owner-only revoked directory, and tag `archive/m04-v9-prelaunch` identifies the
exact prelaunch apparatus. A preflight now fails with
`NO_LAUNCH_AUTHORIZATION` before any model call.

## Retirement rule

Old worktrees and detached pin directories may be removed only after all of
the following hold:

1. every unique commit is reachable from a named remote branch or tag;
2. all absolute path references have been replaced by URL + commit locks or a
   managed source cache;
3. M01–M03 run data is indexed in the archive with hashes;
4. the 1/4/8 model-free acceptance suite passes against a copied M03 world.

Until then, old worktrees are read-only evidence, not active platform code.
