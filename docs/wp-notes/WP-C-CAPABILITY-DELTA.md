# WP-C 能力增量：agenda-treatment plugin（records + validators）

- **分支：** `codex/wp-c-agenda-treatments`（基于 `codex/generic-session-runtime`）
- **状态：** 记录 schema 与纯校验器已落地；**没有**任何编排接线。
- **用途：** 本文件用于合并时折进 README 能力矩阵，避免多个 WP 同时改 README 造成冲突。
  在折入之前，README 的现状描述仍然是准确的——本 WP 没有改变 runtime 的任何行为。

## 1. 落地位置与定位

```text
src/pmw_platform/experiments/agenda_treatments/
├── verdict.py      # 稳定裁决码 + Verdict
├── schemas.py      # 七个 typed payload schema、严格解析器、identity 守卫、builder
├── snapshot.py     # AgendaSnapshot / AgendaEntry / AgendaRoles（含 agenda clock）
├── candidates.py   # 候选形状闸门（必须是 identity-free 的 ResearchContribution）
├── worklist.py     # D 臂：租约代数与校验器
├── central.py      # C 臂：directive 与引用规则
└── adaptive.py     # 自适应臂：decomposition 与 hardening trigger
```

这是 **experiment plugin，不是 runtime core**。`src/pmw_platform` 下没有任何 runtime
模块 import 它（可用 `grep -rn experiments src/pmw_platform` 复核）；`orchestrator.py`
零改动。要让某个 treatment 生效，host 必须自己调用这些校验器。

## 2. README 能力矩阵候选行

| 能力 | 当前已有 | 当前尚无 |
|---|---|---|
| Agenda treatment | 三个臂的 record schema 与纯校验器（可作为 plugin 独立调用）：D 臂独占租约 + TTL、C 臂 coordinator directive 与引用规则、自适应 hardening trigger | treatment 的 runtime 接线：没有 arm 编排、没有 host 自动拒绝、没有把 verdict 写进 receipt 或 settlement |
| Task 租约 | 任一 snapshot 下每任务至多一个活租约，可前向拒绝（`validate_task_claim`）也可事后审计（`check_lease_exclusivity`） | 租约续期原语（续期＝显式 release + claim）、跨 snapshot 的租约事件流 |
| Agenda clock | TTL 由 host 显式提供的整数 tick 判定；缺时钟时如实报 `UNDECIDABLE` 并 fail closed | 平台自带时钟：plugin 不读任何 wall clock，tick 必须由 host 的 runtime 证据供给 |

## 3. 记录形状：骑在现有六 kind 上，不新增 kind

**不新增 record kind。** 七个 treatment payload schema 各自声明一个 kind 绑定，
`schemas.py` 在 **import 时**断言每个绑定都是 `RESEARCH_KINDS` 的子集——即使有人后
来改坏绑定表，也会在导入阶段炸掉而不是悄悄引入第七种 kind。

| Treatment 记录 | payload schema | 承载 kind | 选择理由 |
|---|---|---|---|
| TaskProposal | `PMW_AGENDA_TASK_PROPOSAL_1` | `NEED` | 已识别、尚待完成的工作 |
| TaskAdmission | `PMW_AGENDA_TASK_ADMISSION_1` | `CHECKPOINT` | 经审核的 agenda 状态：该任务进入 worklist |
| TaskClaim | `PMW_AGENDA_TASK_CLAIM_1` | `NOTE` | 协调公告，不是数学主张 |
| TaskRelease | `PMW_AGENDA_TASK_RELEASE_1` | `NOTE` | 同上 |
| TaskOutcome | `PMW_AGENDA_TASK_OUTCOME_1` | `RESULT` / `ATTEMPT` | `COMPLETED` 必须骑 `RESULT`；`ABANDONED`/`BLOCKED` 必须骑 `ATTEMPT` |
| Directive | `PMW_AGENDA_DIRECTIVE_1` | `CHECKPOINT` | coordinator 的 agenda 状态断言 |
| DecompositionRecord | `PMW_AGENDA_DECOMPOSITION_1` | `RESULT` | “子引理合起来足以推出目标”是数学主张 |

**任务身份 = 其 `TaskAdmission` 的 admission ref。** 不引入第二套 ID 空间。副产品：
依赖图天然无环——记录只能引用写入时已存在的 ref。

**引用 directive 用 payload 字段 `cited_directive_refs`，不占用 `parent_refs`**，
后者留给真正的数学 lineage。该字段被允许出现在任意 treatment payload 上，因此 C 臂与
D 臂可以组合（在中央协调下写的 claim 也能声明它依据哪条 directive）。

### 身份注入边界

- payload **无任何作者字段**。租约持有者 = host 在 `bind` 时注入的
  `ResearchRecord.session_id`。
- `reject_self_asserted_identity` 在**任意嵌套深度**拒绝
  `session_id`/`cohort_id`/`world_id`/`claimant`/`base_snapshot_ref` 等 host 专属键。
- 校验器只接受 identity-free 的 `ResearchContribution`；传入已 bind 的
  `ResearchRecord` 会被 `CANDIDATE_NOT_IDENTITY_FREE` 拒绝。
- 需要作者身份的校验器（admission / release / outcome / directive）通过
  **host 提供的关键字参数** `prospective_session_id` 获得它；缺失即
  `AUTHOR_IDENTITY_REQUIRED`，绝不从 payload 里读。
- `validate_task_claim` **完全不需要**作者身份：排他性是世界的性质，不是“谁在问”的
  性质。因此当前持有者自己也无法叠加第二个活租约。

## 4. 校验器清单

全部是纯函数：位置参数 `(snapshot, candidate)`，其余为显式关键字参数；返回
`Verdict(code, detail)`，`code` 取自封闭集合 `VERDICT_CODES`。正常拒绝不抛异常。

| 函数 | 强制的规则 |
|---|---|
| `validate_task_proposal` | schema/kind 绑定；声明的依赖必须已在 worklist 上 |
| `validate_task_admission` | 作者必须属于 `admitting_session_ids`；`proposal_ref` 必须可解析；依赖必须已存在 |
| `validate_task_claim` | 任务已被授权 admit；未完成；依赖全部完成；**无任何仍占用该任务的租约** |
| `validate_task_release` | 只有持有者能关自己的租约；task/claim 必须匹配；不可重复关闭；已过期的租约仍可 release（记账） |
| `validate_task_outcome` | release 的全部规则 + 租约必须 `LIVE`；kind 必须匹配 disposition；`COMPLETED` 的 evidence 必须对上 admit 时的 completion contract；artifact-backed contract 必须真的带 artifact ref |
| `validate_directive` | 只有 `coordinator_session_ids` 里的槽位有效；`supersedes_refs` 必须可解析 |
| `validate_directive_citation` | primary action 记录必须引用至少一条 **live** directive |
| `validate_decomposition` | target 必须在 snapshot 中；每个子引理必须是已授权的 admitted task 且陈述逐字节相同 |
| `check_lease_exclusivity` | 全快照审计：每个任务至多一个仍占用它的租约 |

辅助只读函数：`admitted_tasks`、`proposals`、`claim_state`、`blocking_claim_refs`、
`task_is_completed`、`directives`、`live_directive_refs`、`superseded_directive_refs`、
`settled_decomposition_refs`。

### 租约代数

某个 claim 在一个 snapshot 下恰好处于一个状态：

- `CLOSED`——持有者本人发布了匹配的 `TaskRelease` 或 `TaskOutcome`（**关闭优先于过期**；
  他人写的 release 不生效）；
- `EXPIRED`——`now_tick >= observed_at_tick + lease_ticks`；
- `LIVE`——未关闭且未过期；
- `UNDECIDABLE`——未关闭，但 host 没给这条 claim 提供时钟。

`blocking_claim_refs` 返回 `LIVE ∪ UNDECIDABLE`。**`UNDECIDABLE` 一律按“可能活着”处理**，
所以缺时钟时不会误发第二个租约；对应地，`check_lease_exclusivity` 在无时钟的快照上会
报 `TASK_CLAIM_CONFLICT`——含义是**排他性未被证明**，而不是排他性被违反。

### 完成的 chain of custody

任务“已完成”当且仅当存在 `COMPLETED` 的 `TaskOutcome`，它引用的 claim 属于**同一任务**
且**由写该 outcome 的同一 session 持有**。同伴无法替别人宣布任务完成。

## 5. C 臂 directive 引用规则的确切范围

- 需要引用的：`ATTEMPT`、`RESULT`、`CHECKPOINT`（`PRIMARY_ACTION_KINDS`）。
- **不需要引用的：`NOTE`、`NEED`、`OBJECTION`。** 这是刻意的：若“没有 directive 就不能
  写 OBJECTION”，treatment 就会污染它本该测量的证据。
- directive 记录自身豁免（它是被引用的权威来源，不是在权威之下的行动）。
- 可以同时引用已被 supersede 的 directive 作为 lineage；只要**至少一条是 live** 即通过。
- directive 的 liveness 是 snapshot-local 的：一条 directive 保持 live，直到另一条
  **有效的 coordinator directive** 在 `supersedes_refs` 里点名它。无需可变的撤销标志；
  “撤销而不替换”表达为一条说明撤销的后继 directive。

## 6. 自适应 hardening trigger 的精确语义

```python
agenda_hardening_trigger(snapshot, target_ref, *, roles) -> bool
```

返回 `True` **当且仅当** snapshot 中存在一条 *settled 且覆盖 target* 的 decomposition
记录 `D`，即以下五条同时成立：

1. `D` 能解析为 `PMW_AGENDA_DECOMPOSITION_1` 且骑在 `RESULT` 上；
2. `D.target_ref == target_ref`，且该 ref 在 snapshot 中可解析（目标不在快照里 → `False`）；
3. `D` 至少列出 `MINIMUM_SUBLEMMAS_FOR_TRIGGER = 2` 条子引理——归约到单条引理改变了陈述，
   但没有产生需要协调的 agenda，而 hardening 正是为协调而存在；
4. **grounded**：每条子引理的 `admission_ref` 都解析为快照中**由授权 admitting 槽位写的**
   `TaskAdmission`，且其 `statement` 与子引理陈述**逐字节相同**（防止悄悄改写）；
5. **unobjected**：快照中没有任何 `OBJECTION` 记录在 `parent_refs` 里点名 `D`。

无模型调用、无启发式、无评分、无对散文的阈值判断。写 decomposition 不需要特殊角色——
权威落在第 4 条：子引理必须是 admitting 槽位放上 worklist 的任务，所以单个 session
无法靠自己写一条记录就 harden 整个 agenda。

配套的 `settled_decomposition_refs(...)` 返回**具体是哪条记录**触发的，便于 host 记录证据
而不是只记一个 bit。

**两条必须随行的诚实标注：**

- **非单调。** PMW 快照只增不减，但第 5 条可以把已经为 `True` 的判定翻回 `False`（有人后来
  提出 objection）。trigger 是**关于某个快照的谓词**，不是不可逆事件；host 若据此行动，
  必须记录触发时的精确 snapshot ref。
- **结构性，非数学性。** 触发只意味着“decomposition 被写下、落在 admitted task 上、且
  未被质疑”，**不是**子引理确实足以推出目标的证据。

## 7. agenda clock 的来源（重要边界）

PMW 记录内容刻意不含时间戳。TTL 需要时钟，因此 plugin 从 **host** 取：
`AgendaSnapshot` 携带 `now_tick`（评估时刻）与 `observed_at_ticks`（每条 admission 被
host 观察到的 tick）。二者都是整数，单位由 host 选定并在一次实验内保持一致。

- 这两个值来自 host 的 runtime/settlement 证据（本仓库的权威范围），**不来自世界内容**，
  也**无法由研究进程编写**——claim payload 只声明 `lease_ticks`（时长），不声明起始时刻，
  所以持有者无法通过篡改起始时间延长自己的租约。
- plugin 自身**不读任何时钟**（模块内无 `time`/`datetime`/`random`/`os.environ` 引用，可
  grep 复核）。
- 缺失时钟不会被默认为“租约已过期”，而是 `UNDECIDABLE` + fail closed。

## 8. 明确不具备的能力

- **没有编排接线。** 不启动 session、不改世界、不发布 admission、不写 receipt。
- **不验证数学。** completion contract 检查是 *形状与出处* 检查：它核对 evidence 是否声明了
  该任务 admit 时的 contract、artifact-backed contract 是否真的带了 artifact ref。它
  **不重跑 verifier**，也不断言数学正确；权威验证仍是 host 结算后的既有路径，未被改动。
- **不提供 live 协调面。** 仍然没有运行中的 PMW `search/get/updates_since`、ballot、barrier。
  treatment 目前只能在 host 侧对候选记录做判定。
- **没有跨快照的租约事件流**，也没有租约续期原语。
- 已被 admit 的畸形记录不会影响 treatment 判定（会被排除），但也因此**不会自动报警**；
  `AgendaSnapshot.malformed_admission_refs` 与 `AgendaSnapshot.invalid_typed(...)` 供 host
  自行审计。

## 9. 测试证据

- 新增 `tests/test_agenda_treatments.py`：**67 个测试**，全部 fixture 快照，零模型调用、
  零网络、零子进程。
- 全量套件：**288 passed, 2 skipped**（本 WP 之前的基线为 221 passed, 2 skipped；
  即新增 67，既有用例无一回归）。2 个 skip 是既有的、与本 WP 无关的用例。
- `python -m compileall -q src tests` 通过（与 CI 一致）。
- 覆盖的关键行为：租约排他、TTL 边界（第 149 tick 仍 live / 第 150 tick 过期）、
  冲突拒绝、他人 release 无效、缺时钟 `UNDECIDABLE`、顺序租约满足排他、依赖就绪、
  chain of custody、completion contract 失配、directive 引用（live / 已 supersede /
  未知 / 非 coordinator 所写 / 缺字段 / 豁免 kind）、trigger 的 true / false /
  边界例（目标缺失、单子引理、子引理非 admitted task、admit 槽位无授权、陈述被改写、
  decomposition 骑错 kind、objection 点名与不点名、与时钟无关）。

## 10. 未尽事项（交给后续 WP 或 owner 决定）

1. **arm 编排未接线。** 需要一个 launch 级的 treatment 选择（`FREE`/`C`/`D`/`ADAPTIVE`），
   把 verdict 接进 host 的 publish 路径，并把裁决写进 receipt/settlement。
2. **`observed_at_ticks` 的具体来源未确定。** 本 WP 定义了接口与失败语义，但 host 用哪份
   runtime 证据填充它（session 结算时刻？publish 时刻？）尚未拍板；这直接决定 TTL 的含义。
3. **trigger 的非单调性未做策略处理。** 目前如实暴露；是否需要“一旦触发即锁存”的实验策略
   由 owner 决定，不应由 plugin 擅自决定。
4. **`MINIMUM_SUBLEMMAS_FOR_TRIGGER = 2` 是一个定义性阈值**，已命名并文档化，但它是设计
   选择而非从理论推导出来的常数。
5. **completion contract 与真实 verifier 未打通**（依赖 WP-B 的 in-session verifier kit）。
6. **未提供 JSON Schema 文件**（`schemas/` 目录下的三个既有 schema 未扩充）；当前权威是
   Python 解析器。若需要跨语言消费，应补 `schemas/agenda-treatment-*.schema.json`。
