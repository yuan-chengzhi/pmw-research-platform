# WP-D 能力增量：arm wiring as toolset exposure

- **分支：** `codex/wp-d-arm-wiring`（基于 `codex/agenda-apparatus`）
- **需求：** `when-agents-taskify/docs/apparatus/WP-D-ARM-WIRING.md` D1–D7；语义权威
  `when-agents-taskify/docs/decisions/2026-08-18-adaptive-toolset-and-worklist-repair.md`
- **状态：** WP-C 的纯校验器第一次接进 host 的 publish → settlement 路径。
  **arm = 工具集，不是 regime**；控制路径里没有 hardening trigger。
- **用途：** 合并时折进 README 能力矩阵。README 里 `Agenda treatment` 行的
  「当前尚无」半边（"treatment 的 runtime 接线：无 arm 编排、无 host 自动拒绝、
  verdict 不入 receipt/settlement"）**已经过时**，替换文本见 §2。

## 1. 落地位置

```text
src/pmw_platform/experiments/
├── agenda_arm.py               # 新：arm 配置、host 侧 arm 控制器（review/observe/settle）
├── agenda_observables.py       # 新：分析期 trigger 时间序列（不在任何控制路径上）
└── agenda_treatments/
    ├── schemas.py              # +RouteDeclarationPayload（第八个 payload schema）
    ├── route.py                # 新：route 校验器（peer_trigger_refs fail-closed）
    ├── verdict.py              # +ROUTE_TRIGGER_REF_UNKNOWN
    └── __init__.py             # 导出上述新符号

src/pmw_platform/runtime/
├── orchestrator.py             # AgendaArm Protocol + launch/invocation/publish/receipt 接线
└── store.py                    # launch.agenda_arm 与 receipt.agenda_arm 的校验

src/pmw_platform/cli.py         # session start --agenda-arm/--agenda-coordinator/--agenda-admitting-slot
tests/test_agenda_arm.py        # 新：35 个测试（零模型、零网络、零子进程）
```

**依赖方向没有反转。** runtime 只持有一个 `AgendaArm` Protocol（与既有
`ContributionPublisher` 同一写法），`grep -rn "experiments" src/pmw_platform/runtime`
为空；`cli.py`（允许知道实验）负责构造具体 arm。`store.py` 按既有政策自持
vocabulary 字面量，不 import 生产者；`orchestrator.py` 同理持有「未配置 arm」三个块的
字面量，并由测试钉死它们与 `experiments/agenda_arm.py` 常量逐字相等。

## 2. README 能力矩阵替换行

| 能力 | 当前已有 | 当前尚无 |
|---|---|---|
| Agenda arm | launch 冻结 arm 配置（`agenda_arm` + `agenda_arm_sha256`，P/D/A/C 四臂 = 工具集）；publish 路径按 arm 校验每条 contribution，verdict 进 receipt；invocation 面非规定性地公告工具集；agenda clock = 世界 admission 计数器；租约在持有者结算时自动释放 | 会话内 live 认领（无 live read plane）；`require_claim_for_primary_action` 的执行层（只记录不执行）；imposed-switch 臂；C 臂只做到「可用」，未做过实验 |
| Route telemetry | typed `RouteDeclaration`（骑 ATTEMPT）：route 陈述、`peer_trigger_refs`（悬空即 fail-closed 拒绝）、`differentiation_note`；每 session 的解析计数进 settlement 证据 | 没有把 route 声明与实际发布记录做语义比对；「差异化」仍是自述 |
| Agenda observable | host 侧 trigger 时间序列 + taskification/trigger 双 onset 摘要，可在一次 run 之后重放 | 序列只能由 admission-ordered ledger 或显式 snapshot 列表驱动；裸世界无法恢复 admission 顺序 |

## 3. arm 配置形状（D1）

`AgendaArmConfig(arm, coordinator_session_ids, admitting_slots, require_claim_for_primary_action, enforce_directive_citation)`，
其 `launch_value()` 被哈希进 `launch.json`：

```json
{
  "schema": "PMW_AGENDA_ARM_LAUNCH_1",
  "mode": "ENFORCED",
  "arm": "D",
  "instruments": ["binding"],
  "admitted_payload_schemas": ["PMW_AGENDA_DECOMPOSITION_1", "...", "PMW_AGENDA_ROUTE_DECLARATION_1"],
  "coordinator_session_ids": [],
  "admitting_slots": "ALL_SESSIONS",
  "open_admission": true,
  "require_claim_for_primary_action": true,
  "enforce_directive_citation": false,
  "agenda_clock": "WORLD_ADMISSION_COUNTER_TICK_1",
  "lease_release": "HOST_SESSION_SETTLEMENT_RELEASES_HOLDER_LEASES_1",
  "enforcement": "PUBLICATION_TIME_RECORD_VALIDATION_ONLY_NO_RESEARCH_BEHAVIOUR_POLICING",
  "rejection_semantics": "REJECTED_INSTRUMENT_IS_A_RESEARCH_EVENT_NOT_A_SESSION_FAILURE"
}
```

| arm | instruments | 被接纳的 typed payload |
|---|---|---|
| P | `{advisory}` | 仅 telemetry（六 kind 原样合法） |
| D | `{binding}` | worklist 五件 + decomposition + telemetry |
| A | `{advisory, binding}` | 同 D |
| C | `{advisory, directive}` | directive + telemetry |

三条要点：

1. **`admitting_slots: "ALL_SESSIONS"` 就是 D 的开放准入**：没有 initializer 角色、
   没有 quiescence gate，任何 session 任何时刻都能提 `TaskProposal`，也能自己把私有
   信号 admit 上 worklist。解析时它展开为「本次 launch 的全部 session ∪ 已经在这个世界
   里写过东西的全部 session」——否则每跨一个 cohort 边界，worklist 就会忘掉上一代的
   任务，那就不是 worklist 了。coordinator 槽位相反，**必须**是本次 launch 的 session：
   死掉的 session 发的 directive 永远无法被 supersede。
2. **D 与 A 目前在「什么记录合法」上是同一个集合**，区别在配置与公告：D 记录
   `require_claim_for_primary_action: true` 并如实公告「这条臂用 worklist 认领行动」，
   A 公告「两种工具都在，用哪个你定」。这正是需求书说的——本 WP 只 validate 与 stamp，
   不 police 研究行为。**这也是 D-vs-A 目前唯一的实验差异，必须随行标注。**
3. **`RouteDeclaration` 是 telemetry，不属于任何 instrument family，四臂皆合法。**
   这是刻意的：如果 route 测量随 treatment 变化，那么「测量 route 的仪器」本身就成了
   treatment 的一部分，route 对比就不可解释了。代价是 P 臂并非「零 typed payload」，
   这一点与 D1 字面的「agenda-treatment payloads 一律 out-of-arm」有偏差，在此显式登记。

## 4. publication 路径接线方式（D2）

`_Controller._publish` 里，每条 contribution 依次经过：

```
arm.review(spec, contribution) → 不接纳则 continue（跳过这一条发布）
                               → 接纳则 publisher(spec, contribution) → arm.observe(...)
```

- **verdict 全部落进 receipt**（`receipt["agenda_arm"]`），包含每条 decision 的
  `{ordinal, kind, payload_schema, instrument, code, admitted, detail}`。
- **拒绝不让 session 失败。** 这需要放宽 store 的既有不变量：原来 `SUCCEEDED` 要求
  `len(publications) == contribution_count`，现在是
  **`len(publications) + agenda_arm.rejected == contribution_count`**。会计仍然闭合——
  成功 session 的每条 contribution 要么被发布，要么带着一条 verdict 被拒绝——但
  「被拒绝」不再是 apparatus failure。这是本 WP 对 runtime core 最实质的一处修改。
- **out-of-arm** 用 arm 层自己的码 `OUT_OF_ARM_INSTRUMENT`（`ARM_VERDICT_CODES` =
  WP-C 的 `VERDICT_CODES` ∪ 这一个）。含义与 plugin 的拒绝不同：这条仪器**根本没有被
  评估**，因为这次 launch 不暴露它。
- 有 arm 时整个 review→publish→observe 序列由一把 `asyncio.Lock` 串行化（世界一次只
  admit 一条记录，tick 计数器也这么说）；**没有 arm 时不取任何锁**（走 `nullcontext`），
  发布并发行为与本 WP 之前完全一致。
- publication 被禁用（`PMW_PUBLISH_UNAVAILABLE`）时不做任何 review：没有发布路径就没有
  发布期校验。

## 5. tick 与租约生命周期实现（D3）

**tick = 世界的 admission 计数器；一条记录的 tick = 它的 admission index。** host 持账：

- launch 开局 tick = 世界当时的 admission 数（`build_agenda_arm` 只读一次世界）；
- 本次 launch 每发布一条 admission，计数器 +1，该 ref 的 tick 就是新值；
- **早于本次 launch 的 admission 一律给开局 tick**——这是它们真实 index 的**上界**，
  于是继承来的租约只会显得更年轻，**绝不会提前过期**（fail-closed 方向）。

租约释放有两条来源，plugin 与 host 各管一半：

- plugin（世界内容）：持有者自己写 `TaskRelease`/`TaskOutcome` → `CLOSED`；
  `now_tick >= observed_at_tick + lease_ticks` → `EXPIRED`。
- host（runtime 证据）：**持有者 session 结算时，它的租约自动释放**。实现是
  `_claim_verdict` 里一层**只放松、不收紧**的覆盖：当 `validate_task_claim` 因
  `TASK_CLAIM_CONFLICT` / `LEASE_LIVENESS_UNDECIDABLE` 拒绝时，检查全部阻塞 claim 的
  持有者——若**每一个**都已结算，或**根本不属于本次 launch**（session id 是
  cohort-scoped 且永不复用，因此按构造已死），则改判 ACCEPTED 并在 detail 里写明原因。

由此得到的、可测且互相区分的两种行为（D7 要求）：

| 场景 | 结果 |
|---|---|
| 同一 session 在自己这一批 contribution 里连发两个同任务 claim | 第二个 `TASK_CLAIM_CONFLICT`（持有者还活着） |
| 后一个 session 认领前一个 session 已结算时持有的任务 | `ACCEPTED`（结算即释放） |
| 认领上一代 cohort 留下的租约 | `ACCEPTED`（持有者按构造已死） |
| 上一代租约的 TTL | 到期照常判 `EXPIRED`（见上面的上界规则） |

`receipt["agenda_arm"]["lease_release"]` 记录本次结算释放了哪些 claim ref、在哪个
tick 释放；`agenda_clock` 记录 `base_tick` 与 `settled_tick`。

**接受并登记的停滞世界警告**：世界不动时计数器不动，TTL 永不到期；结算释放覆盖了实际
重要的那种情形（agent 死掉）。

## 6. RouteDeclaration 语义（D5）

```json
{
  "schema": "PMW_AGENDA_ROUTE_DECLARATION_1",
  "route_statement": "...",
  "peer_trigger_refs": ["admission/sha256/…"],
  "differentiation_note": "…" | null
}
```

- 骑 `ATTEMPT`（在做的事，不是已成立的结论）；无角色要求、无作者身份要求——声明
  「我走了哪条路」不主张任何权威。
- `peer_trigger_refs` 必须在 host 校验时所用的快照里**全部可解析**，否则整条记录被拒，
  码 `ROUTE_TRIGGER_REF_UNKNOWN`（新增进 WP-C 的封闭 verdict 集合）。**悬空引用不是弱
  证据，是没有证据**；briefing 本来就暴露 admission ref，agent 完全有能力引对。
- settlement 证据里的 `route_declarations` 给出五个数：`count`、
  `with_peer_trigger_refs`、`resolved_peer_trigger_refs`、`dangling_rejected`、
  `differentiation_notes`。这正是 replay 里丢掉 `DIFFERENTIATED_ROUTE`、
  `peer_trigger_refs` 全空之后失去的那部分可测量性。

## 7. 分析期 trigger observable（D6）

`experiments/agenda_observables.py`：

- `trigger_time_series(admissions, target_ref, roles=…, base_count=…)` 在 admission
  有序账本的每个前缀上求 `settled_decomposition_refs`，每个样本带
  `tick / admission_ref / fired / decomposition_refs / admitted_task_count / proposal_count`；
  `AgendaArm.admissions()` 正好给出这个顺序。
- `trigger_time_series_from_world(world, snapshot_refs, …)` 走显式快照序列，tick = 该
  快照的 admission 数（与账本形式一致）。调用方提供顺序：单个 snapshot ref 不携带自己的
  历史，本函数不臆造顺序。
- `trigger_cooccurrence(...)` 给出 `trigger_onset_tick`、`taskification_onset_tick`、
  `taskification_lead_ticks`（正数 = agent 先 taskify，负数 = trigger 先响），以及
  `non_monotone`（有人后来 objection 把已 fire 翻回未 fire）。摘要只陈述关联，
  `authority` 字段写死 `ANALYSIS_TIME_OBSERVABLE_NEVER_A_CONTROL_INPUT`。
- 有一条测试直接断言 `agenda_arm.py` 源码里**不出现** `agenda_hardening_trigger` 与
  `settled_decomposition_refs`——控制路径与观测量的分离是被检查的，不是被承诺的。

## 8. 测试数字

- 新增 `tests/test_agenda_arm.py`：**35 个测试**，零模型调用、零网络、零子进程。
- 全量套件：**340 passed, 2 skipped**（本 WP 之前的基线 305 passed, 2 skipped；
  新增 35，既有用例无一回归）。
- `python -m compileall -q src tests` 通过（与 CI 一致）。
- 覆盖：四臂各自的 mini-cohort run（P/D/A/C）+ 受限 admitting 槽位 + 未配置 arm；
  in-arm admission、out-of-arm 拒绝、同批次 claim 冲突、结算自动释放、跨生命 TTL 过期、
  开局 tick 上界、route ref 解析与悬空拒绝、trigger 时间序列（含 objection 翻转与显式
  快照序列）、launch↔receipt 绑定与篡改拒绝、旧 launch fail-closed、publisher 内容
  digest 不一致时的 divergence 记账、C 臂 directive 引用强制、CLI `--agenda-arm` 路由、
  runtime 字面量与生产者常量相等、四 session 双并发下的账本隔离与单一 tick 计数器、
  以及 §9.3 那条「epoch = lifetime 下没人能关自己的租约」的结构性限制（用测试把它钉死，
  而不是留在文档里）。

## 9. 诚实未尽事项

1. **D 与 A 在「合法记录集合」上目前无差别**（§3.2）。两臂的实验差异现在只落在
   arm 配置与 briefing 措辞上。要让 D 真的比 A 更紧，需要
   `require_claim_for_primary_action` 的执行层，那是被需求书排除的 non-goal。
2. **会话内不可能有跨 session 的租约冲突。** epoch = lifetime：contribution 在结算时
   才发布，而结算同时释放租约，所以同代 peer 之间的独占从来不会真正咬合。目前能观察到
   的冲突只有「同一 session 自己这一批里的重复认领」。理论里那条**排他性把 OR 探索
   串行化**的代价，在 live read plane 出现之前**测不到**——这是本 WP 最重要的一条限制。
3. **`TaskRelease` / `TaskOutcome` 在 epoch = lifetime 下实际不可用**，这是上一条的
   结构性推论，必须单独记：一个 session 的记录在它结算时才被 admit，所以它**永远拿不到
   自己那条 claim 的 admission ref**，也就永远写不出关闭它的 outcome；同伴也不行——
   chain of custody 要求 outcome 的作者就是租约持有者（有测试固定这一行为）。
   直接后果：`task_is_completed` 恒为假，于是**带依赖的任务永远 `TASK_DEPENDENCIES_UNREADY`**，
   实验设计在 live read plane 出现之前只能用无依赖任务。这同时说明**结算即释放不是便利
   功能而是承重件**：没有它，任何被认领过一次的任务将永远锁死。
4. **P 臂并非零 typed payload**：RouteDeclaration 在四臂皆合法（§3.3）。理由已给，
   但它确实偏离 D1 的字面表述，需 owner 知悉。
5. **arm 配置写在 `launch.json` 而不是 `plan.json`。** D1 开头说「cohort plan carries
   an `agenda_arm` config」，落地句说「hashed into launch.json」。选了后者：arm 是执行
   身份（换臂不改变世界的数学身份），且 `CohortPlan` 字段集一改就会作废既有 plan.json。
   代价：同一个 plan 可以被两次不同 arm 的 launch 使用，防止这件事得靠操作纪律。
6. **旧 launch/receipt 一律 fail-closed**（`MALFORMED_RUNTIME_LAUNCH` /
   `MALFORMED_SESSION_RECEIPT`），沿用 merge note 的 `_1`-name 政策。WP-D 之前写出的
   runtime 目录无法被本版本读取。
7. **每条 contribution 都会重建一次 `AgendaSnapshot`**，即把整个已观察世界重新解码一遍。
   N 条 admission、M 条 contribution 就是 O(N·M) 次记录校验；research 规模下可接受，
   大世界下是已知成本，尚未做增量快照。
8. **`observe` 的分歧处理是记账而非阻断。** publisher 返回的 `content_sha256` 与 arm
   校验过的记录不一致时，该 admission 以**不透明条目**进账本（它满足不了任何 treatment
   规则），并在此后每份 receipt 的 `publication_divergences` 里累计。它**不会**中止 run。
9. **发布中途抛错的 session**（`RuntimePublicationError`）其 `reviewed` 可能大于
   `publications + rejected`：某条已判 ACCEPTED 但发布失败。仅出现在 FAILED 状态下，
   成功 session 的会计不变量不对该状态生效。
10. **arm 自己抛异常 = apparatus failure。** `review` 抛错会让该 session FAILED，
    `session_evidence` 抛错会让 settlement 不完整。这与「拒绝不是失败」并不矛盾：
    拒绝是判决，抛异常是仪器坏了。
11. **C 臂仍是 config-only 的次要臂**：directive 校验与引用校验都可用
    （`enforce_directive_citation` 默认关闭），但没有跑过任何实验，也没有 coordinator
    的 briefing 侧特殊说明。
12. **没有 preflight 报告 arm**：`session preflight` 既不构造也不描述 arm，操作者只能
    从 `session start` 的输出或 `launch.json` 看到它。
13. **没有 JSON Schema 文件**：`schemas/` 下未新增 `agenda-arm-*.schema.json`；当前权威
    仍是 Python 解析器与 `store.py` 的校验器。
14. **零真实模型调用。** 没有任何模型见过这份工具集公告；「公告是否足够显著到让 agent
    真的去用某种仪器」仍是本装置要测的开放经验问题，本 WP 不作任何预言。
