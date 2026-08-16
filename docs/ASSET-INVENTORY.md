# PMW asset inventory and retirement gates

Audited 2026-08-16. Paths below are relative to `~/Documents` unless stated
otherwise. This inventory distinguishes live authority, immutable research
evidence, source checkouts, and cold historical data. A Git commit being
reachable remotely does **not** by itself make a path removable: legacy
launchers still bind several physical paths.

## Canonical active repositories

| Repository | Authority | Keep active |
|---|---|---|
| `pmw-research-platform` | World/briefing/artifact/plan identity plus the generic session runtime and safety policy | Yes; canonical active runtime repository |
| `agent-math-frontier` | Problem statements, formalization status, verifier registry and target portfolio | Yes; mathematical problem authority |
| `persistent-mathematical-worlds` | Typed PMW records, snapshots, provenance and world semantics | Yes; state-semantic authority |

`world-kernel` is a remotely preserved implementation reference, not a fourth
canonical authority. Its small runtime-adapter role is now represented by the
generic backend contract in `pmw-research-platform`; new cohorts do not need a
physical `world-kernel` worktree.

## Active runtime implementation

The canonical platform now has one host lifecycle/settlement path and two
adapters:

| Component | Validation state | External activity in this implementation pass |
|---|---|---|
| Command adapter | Model-free process-group, output-capture, result and cancellation tests plus one four-session/four-concurrency settled acceptance | Local deterministic workers only; zero model/network calls |
| Pi RPC adapter | Fake RPC protocol/lifecycle tests; local Pi installation and OAuth type loaded read-only for config compatibility | No new real model/provider canary, network request or model token use |
| Resource guard | Quiescent initial/terminal aggregate scans plus live disk-reserve tests | Local temporary trees only |

`runtime/launch.json` binds the backend, the host's single
startup/session-wall/stop-grace policy, and a publication identity of either
`DISABLED` or `PMW_BOUND`. M04 remains frozen: no M04 run root, provider call or
settlement was created while implementing or validating this runtime.

## Frontier campaign Git topology

The two remaining directories are one Git repository. Its common directory is
`pmw-live-pi-frontier-choice-2026-08-14/.git`. All five historical branches
and the M04 archive tag are preserved in the public, read-only archive
`https://github.com/yuan-chengzhi/pmw-frontier-choice-experiments`.

| Worktree | Branch | HEAD | Size | Status |
|---|---|---|---:|---|
| `pmw-live-pi-frontier-choice-2026-08-14` | `codex/frontier-choice-v3-oauth` | `a5abba2b2ea22c606259d486636addbdb09ca131` | 169M | Holds M01 and the common Git directory; do not move |
| `pmw-live-pi-frontier-choice-m02-official-context` | `codex/m02-official-sol-context` | `03fffadabcd983dd58c12671c6af799b62e49824` | 1.0G | Holds M02, M03 and the frozen M04 apparatus; do not move yet |

The former `pmw-live-pi-frontier-choice-m02-output-projection` worktree was
retired on 2026-08-16 after its branch was pushed. Its orphaned read-only
directory was moved recoverably to
`~/.Trash/pmw-live-pi-frontier-choice-m02-output-projection-retired-20260816`.

All earlier frontier branches are remotely preserved. The annotated remote tag
`archive/m04-v9-prelaunch` points to `03fffadabcd983dd58c12671c6af799b62e49824`; tag
object `e49fea78212f78fa665971ce22e9c66d050bd756`. M04 has no run root or
settlement and its local authorization was revoked before consumption.

## M01–M03 immutable run ledger

| Run | Root | Size | Settlement | Final snapshot | World ref OID | World admissions / snapshots |
|---|---|---:|---|---|---|---:|
| M01 | `pmw-live-pi-frontier-choice-2026-08-14/runs/M01` | 105M | `SETTLED / TERMINAL_FAILURE` | `snapshot/sha256/84cfe717de019650898509c8e3c0efeb3c732a56d67be0d0d0faea9f0fa3badd` | `01d3d765a378acf981f50173955e407ce910e3e9` | 53 / 54 |
| M02 | `pmw-live-pi-frontier-choice-m02-official-context/runs/M02` | 357M | `SETTLED / TERMINAL_FAILURE` | `snapshot/sha256/6d618b380037a9e98bf5c4c84afaccf7dd8ddea8028460e6cf3a88041edf6a35` | `5e13f31426199ac8e1f311c1ac7880d6689e2782` | 87 / 88 |
| M03 | `pmw-live-pi-frontier-choice-m02-official-context/runs/M03` | 607M | `SETTLED / TERMINAL_FAILURE` | `snapshot/sha256/803bfcd0604ff01c9b560d9e82d6e2a9f606d6c36312418f2e95cf1193f3535a` | `470ad75bb70df5a061a224d4a735a27abe3958fa` | 174 / 175 |

Complete bindings:

```text
M01
  campaign_git_head  a5abba2b2ea22c606259d486636addbdb09ca131
  manifest_sha256    37f5ef943fd06d0b0e21e5484ec9a2724ccf45b5e6b685e88f2d7824e1122059
  projection_sha256  6785779787afc6e4167f8d80b3629e9881c9c815d40a51b45d5d5969afc51426
  ballot_set_sha256  b44e189a8818a231de03b2c115bda7dc1c3826bec867ea5a30965163e63a9264
  barrier_snapshot   5e61b91270212d9379ea3308e8b382367ff8ff2fc3f89f5281e023dfe3d25a03
  final_snapshot     84cfe717de019650898509c8e3c0efeb3c732a56d67be0d0d0faea9f0fa3badd
  settlement_sha256 35ca2d4b8664d80be5660269a103216bc4ff6b2d35e88c3458e6a773712d6c74
  wave_sha256       2390e8575d7f2881b06d24aaf09ebe539f69cf9360f9390902dd28ca8814c179
  state_sha256      ff53578bca377398f5d015a31242e9fdbe6d246008fe5238374a0ed5074b962b
  run_tree_sha256   null (settlement recorded RUN_EVIDENCE_AUDIT_FAILED)

M02
  campaign_git_head  cc3f8ccf674e99e77fc4547c322b47cf1d0ec492
  manifest_sha256    d09c9bd822fb5f34a10185282350a7ec97deebd6d3c9aeb3ebb7db21394f0ee1
  projection_sha256  18c8cc695dfa753ac5b8a72ad1f482d3ce472e79ac8918f8f1d8b9598059ea3b
  ballot_set_sha256  b8dbd141e48fbc20ea41e7befc0174e5e09a0ffe761fb8c256b13e1d03a2c214
  barrier_snapshot   4df37e66333ce70ca58fbe3df7402d1f14926b52070a8484641dda7733dcad30
  final_snapshot     6d618b380037a9e98bf5c4c84afaccf7dd8ddea8028460e6cf3a88041edf6a35
  settlement_sha256 11e1383ed0fd0a191d45a4c792ff405a60e27b03fa77b59c2b2c163d25dbf1c4
  wave_sha256       f9fbfa638de7896f0360787598795ebd52ad9f982c8d35487bcb180948b7eb04
  state_sha256      be01f5ed7e3051bdd15c85e3df799b8122254ee8aaf31ca41fbeb93c7535b738
  run_tree_sha256   7724f5e471a458e45bd9db3af58228856ac098267f67e622a66c2972ad018910
  artifact_set      9ce1de029733e68bbf982569a862c4037315830096b725daee8115e41152ca05

M03
  campaign_git_head  67c982de619e52c395bf5a863d5fb29f74c50707
  manifest_sha256    372ded9e9cef716c4902bf6d82c86952b5b4af3815009d60a372d9113d217504
  projection_sha256  69c9e85c29711039a77446017d58a44ff5d53cb371ba73523a037e9894a8cb0f
  ballot_set_sha256  783aa64b1a54aa77a01c8dacfa35d21057308e8fa13398c8fac3278050513870
  barrier_snapshot   a0650bd67d165e7cb5f5f814024f8ba5318fa65cd33d2d0e26b8e5830f6d5503
  final_snapshot     803bfcd0604ff01c9b560d9e82d6e2a9f606d6c36312418f2e95cf1193f3535a
  settlement_sha256 38936c322c4f7b10903141637815efe08ea4d71ded5dc642c98098eded977d56
  wave_sha256       5927692fdc75a49111c164337b85a4653a312f09320f1365113e182ee4cd9ea2
  state_sha256      3e2cfc7a4b8e85c9b2d9046e9bcc96b187c7f9de14cccbbcc14806205d0445da
  run_tree_sha256   ca0b254debf232950fc483ec03f4521b6ffce0f71699c268d25b76b7a3d63a26
  artifact_set      a94e43cd4b165f303dbf95d6f839ce7a5e094ad0f1f1645aa8ddc262ca024620
```

M01 failed through provider/local context boundaries and cache-lock symlink
audits. M02 failed through three workspace scan-race latches and one invalid
provider-stop classification. M03 produced two valid final handoffs; its
terminal settlement was caused by the `m03-a` 74.7MB single file and the
`m03-c` autoconf hardlink, plus a separate context-stop classification. These
apparatus outcomes do not invalidate the admitted mathematical records.

## Managed M03 continuation

The active mathematical continuation no longer depends on the campaign path.
It is independently materialized as:

```text
pmw-research-data/
  worlds/math-frontier.git
  objects/sha256/                         # 63 objects
  objects/artifact-receipts/sha256/       # 63 exact receipts
  objects/imports/math-frontier-m03-closure.json
  source-cache/agent-math-frontier/c737df34.../
  source-cache/persistent-mathematical-worlds/4880f184.../
  runs/platform-runtime-acceptance-v2-20260816/
```

The world has no alternates. Its exact M03 snapshot has 174 admissions; all 63
unique artifact refs resolve in the independent CAS. Closure manifest SHA-256:
`fbf436eec89f7c9c711faaf8a1ee490c5c699ff858d1a32ca46d0316cd9ff749`.
The situation-v2 briefing contains all 14 target cards, omits predecessor
runtime budgets as content-bound non-operative provenance, and includes a
loss-aware index of every admission (501,644 bytes; SHA-256
`401c39cbd5a52ac3834f6b0dfaa37095ea0da9a0ce007bd9db9c9acd5ee52d04`).
The active zero-model cohort settled four of four sessions successfully:
plan `d736354c87543e1d7016cac4802343459c581fe48437c3a60367c53a9b9ef683`,
launch `83f902f0d7cd17a29d9d94fdb1f04496fb79a83f41831bad84873b4789b8090d`,
settlement `0408d9ea9891e7929a72a2c7a0b141ff5f4ddef6f5116472a203263f3544f610`.
Superseded plan-only bundles and the first failed worker-contract acceptance are
retained under the corresponding `archive/model-free-*` directories.

The following source set remains immutable historical experiment evidence, but
is no longer the active continuation dependency:

```text
pmw-live-pi-frontier-choice-m02-official-context/
  .launch/settlements/M03.json
  runs/M03/evidence/wave.json
  runs/M03/world/host-sealed/state.json
  runs/M03/world/world.git/
  runs/M03/artifacts/receipts/
  runs/M03/artifacts/objects/sha256/
```

The old continuation-critical subset is about 20M. The full 607M run remains cold
audit evidence: roughly 495M workspaces and 85M evidence/logs. Preserve it
immutably, but do not make new sessions ingest it wholesale.

Mathematical handoffs:

| Ref | Durable claim ceiling | Sharp successor need |
|---|---|---|
| `admission/sha256/57ac1472ab65417a81376a8b6105c046a913c8adee6ea93c7a2d55af3236debc` | `m03-b`: exact finite no-negative census for normalized side six, outer sizes 19–22; 774,775 necessary representatives, 100,865 nontrivial exact profiles, all 1,613 caps resolved | Audit/replay, then obtain the missing side-six 23–30 lanes; no KTT closure is claimed |
| `admission/sha256/4ec4d84e11427ab73dc21a6790b7f4a2e9467e1cbf9b376d4cdfca6ed713e9a2` | `m03-d`: exact finite no-negative boundary for normalized side seven through outer size 17; 50,834 representatives and 2,092 nontrivial profiles | Resume size 18: 17 profiles retain 33 missing interpolation-node values |

Supporting roots that must remain resolvable include:

```text
side-six 19–22 result       admission/sha256/84f7d1cfafee92a9913790d663ab39276802fac581b39f6f7d6c80035d10ec45
side-six through-18 replay  admission/sha256/453178cc5b49f57adf0bcc8071dbd2031fb39a86d09e5be3e54d1c864d41bc17
side-seven through-16       admission/sha256/e7463a908a173ea56bc5cf5e286675b31908af4cfa506a4ef1a9b413316557e0
side-seven size-17          admission/sha256/0cd909738677862da9f75d932e4eec90603afbe073d16fb4ca9a46a821919a73
side-seven size-18 partial  admission/sha256/7ca9882cfa5d70fb59e6c877103aaf94fb3cb7910f2adcc662d48f8d782ee957
```

The frozen M04 loader still reads the old physical paths, but M04 is revoked and
will not be launched. Keep the source set until its full logs/workspaces have a
cold-archive manifest; new research uses only the managed copy.

## Absolute-path blockers

The legacy official-context apparatus still has executable absolute bindings,
not merely documentary references.

| Role | Physical path | Pinned revision / gate |
|---|---|---|
| Problems and verifiers | `agent-math-frontier-m02-pin` | `cd7ec71fd76541f3ee2a5134753aedd1b6ddbf8d`; GitHub-reachable ancestor |
| PMW implementation | `persistent-mathematical-worlds-r2-c07-apparatus-fix` | `4880f184c60bd34181302c5343ec0db95f154851`; exact GitHub head; also hardcoded in `host/portfolio_world.py` |
| Runtime/search adapter | `world-kernel-frontier-choice-pin` | `c1ae08aa8f4bf4e2cdcbedd8e968d5c677ee82fe`; exact GitHub head |
| Legacy runtime reference | `pmw-live-pi-frankl-continuation-2026-08-12` | `d3cbba06533afa82e2a3f394bdc5baf8cdbb2d3d`; local-only |
| Lean tool extension | `pi-lean-tools` | `1136b26f2c71cdf59664c7646013d8f492f81b43`; not reachable from any configured remote branch (remote main is `b14403f`) |
| Lean packages / Loogle | `loogle` | Physical `.lake/packages` and built binary paths; outside this Git audit |

Generated run sandbox profiles also contain absolute paths. They are immutable
historical evidence and should not be rewritten. Once archived they are not an
execution dependency; only source/config references block worktree retirement.
The packaged source lock now binds `agent-math-frontier@c737df3` and PMW
`@4880f18` by repository, full commit and complete materialized-tree SHA-256.
Both exact trees are present in managed `source-cache/` and pass repeatable
full-tree audit. `world-kernel`, `pi-lean-tools` and Loogle remain outside that
lock. Historical sandbox paths remain evidence and are never rewritten.

## Frankl local-only warning

`pmw-live-pi-frankl-continuation-2026-08-12` is 6.2G, almost entirely
`runs/`. Its `main@d3cbba06533afa82e2a3f394bdc5baf8cdbb2d3d` and sibling
`codex/c05-frontier-delta@02d924e8abe8bb8b549346dcbc5cc466199f6316`
are not reachable from a durable remote. Do not delete or move the common Git
directory before creating and verifying a bundle or remote checkpoint.

The run tree also contains explicitly cited proof assets, including large
DRAT/LRAT material. Representative bound objects are:

```text
CardThirtyEight.lean   d209c2df684b4e9995fa94ebdd2ccf9b2474e72dcaa6eab4bdf8690fd29f9922
q7m27max3.cnf          3f5a13b878748e004e363358bd9cad6871a354626447892ff9c979f537b99b29
q7m27max3-cadical.drat dc7863fb33b2313852d1a580f642e69a9e0ee2cda8cd16e87233b51df10b9ae1
q7m27max3-cadical.lrat 79b086bc8f47f808eea377e73bfbd192670fa8ca860b9899f7f597d47c695394
q8m39.cnf              f97a85802a2390233efcea2ada9591b4dfd4dde464707176dd93bf619fa606b6
q8m39 alternate CNF    faff298312df916fd902c20f070cc8e1fad2fd3962d000f697b97b2676a620bc
MatchingFC.lean        ae9c54292401704a4ed0c695ab22bbf4b602ed9bad5a163d8b806e1b522e534f
MatchingBV LRAT        effdbe03c2fe5c7e4a1ded55915884ca09ca95fbcfea4a8b5031d038013a7848
```

The new platform only needs the legacy source as a migration reference. After
the source commit is durably backed up and the cited raw evidence has a verified
cold-archive manifest, the 6.2G run directory can leave the active workspace.

## Retirement checklist

No legacy directory is retired until every applicable item is checked:

- [ ] Record repository URL or durable bundle, exact commit, tree hash and
  required subpath in the platform source lock.
- [ ] Materialize the source from that lock in a fresh managed cache and pass
  readiness without the legacy physical path.
- [x] Copy the M03 PMW world and complete M02+M03 artifact reference closure
  into independent managed storage; recompute and compare their hashes.
- [ ] Copy canonical settlements, waves and host state into the cold archive;
  the active platform does not need them as session input.
- [ ] Store raw sessions, frames, workspaces and audit logs in an immutable cold
  archive with a whole-tree manifest. Do not treat cold logs as session context.
- [ ] Push every unique Git ref or verify a self-contained bundle with
  `git bundle verify`; local branch names alone are not backup.
- [ ] Scan active code and configuration for the retiring basename and confirm
  no executable absolute-path reference remains.
- [ ] Remove leaf checkouts with `git worktree remove`; never delete a linked
  worktree directory directly.
- [ ] Remove a common-dir owner only after all linked worktrees and local-only
  refs have been retired or relocated.

Recommended order:

1. **Complete:** retire `pmw-live-pi-frontier-choice-m02-output-projection` after
   pushing the frontier history; the directory is recoverable from Trash.
2. Retire PMW `pr3-review`, `c05-frontier-delta` and `live-research-loop`
   worktrees after source-lock validation; their commits are remotely reachable.
3. Retire the agent and world-kernel detached pins, and PMW c07, only after the
   managed source cache replaces their physical paths.
4. Move standalone observational/failed-run material to cold archive only after
   local-only Git refs and evidence trees are bound by manifests.
5. Retire the Frankl continuation active path after its Git and proof assets are
   independently backed up.
6. Move `pmw-live-pi-frontier-choice-m02-official-context` to cold storage only
   after its full M02/M03 logs/workspaces receive a whole-tree manifest. The
   active world and artifact closure are already imported and replay-validated.
7. Retire `pmw-live-pi-frontier-choice-2026-08-14` last because it owns the
   frontier common Git directory and M01.
