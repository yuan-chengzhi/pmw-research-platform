# PMW Research Platform

这是一个面向开放数学研究的常驻世界与多 session 运行平台。

它只有三个核心概念：

1. **World**：持续存在的数学状态，包括问题、已有尝试、结果、反例、开放 needs 与精确来源。
2. **Session**：一个可替换的研究者进程。它从 world 的某个精确 snapshot 开始，读取统一的数学现状，并把新记录写回 world。
3. **Cohort**：一次资源与结算边界。它可以包含任意数量的 session，并通过 `concurrency` 限制同时运行数；它不是新的数学世界。

```text
one long-lived world
        │
        ├── cohort A: 4 sessions ──┐
        ├── cohort B: 8 sessions ──┼── immutable records + advancing head
        └── cohort C: 1 session  ──┘
```

## First usable surface

```bash
pmw-research world add math-frontier \
  --repo ~/Documents/pmw-research-data/worlds/math-frontier.git \
  --snapshot snapshot/sha256/...

pmw-research world status math-frontier
pmw-research session start --world math-frontier --count 8 --concurrency 4
pmw-research world delta math-frontier --since snapshot/sha256/...
```

The first milestone is deliberately model-free: deterministic 1/4/8-session
smokes establish snapshot continuity, concurrent admission, failure isolation,
and bounded safety behavior. Pi/OpenAI account runtime integration comes only
after those mechanics are stable.

## Boundaries

- PMW remains the authority for immutable mathematical state; this repository
  does not fork or reimplement the PMW core.
- Problem definitions and verifiers remain owned by `agent-math-frontier`.
- Runtime data lives outside Git, by default under
  `~/Documents/pmw-research-data/{worlds,runs,objects,source-cache,archive}`.
- `M01`–`M03`, sealed ballots, barriers, and treatment enums are historical
  experiment evidence. They are not the default research workflow.
- A failed session does not abort its peers. Only isolation failure, process
  leakage, hard provider/billing limits, or real host resource danger can end
  a whole session.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the frozen v0.1 design and
[docs/MIGRATION.md](docs/MIGRATION.md) for provenance and retirement rules.
