# WP-A 能力增量：receipt 中的实测 usage

分支 `codex/wp-a-usage-metering`。本文件记录 WP-A 带来的能力变化，供合并时折进
README 的「能力边界一览」，避免各 WP 直接改同一张表造成冲突。

## 建议折进 README 能力矩阵的一行

| 能力 | 当前已有 | 当前尚无 |
|---|---|---|
| Usage 计量 | receipt 必带 typed usage block，三态自述：`MEASURED`（附 provenance、逐请求记录、两组聚合读数）/`ASSERTED`（被标注的 profile 断言）/`UNMEASURED`（surface 沉默）；Pi adapter 逐条转录 `message_end`、`compaction_end` 与 `get_session_stats` 报告的 token | 成本（货币）口径的权威账本、跨 cohort 的 usage 汇总、对 provider 计费单的对账、以及任何把「未报告」补成 0 的推断 |

## 落地内容

- `src/pmw_platform/runtime/usage.py`（新增，554 行）：typed usage evidence。
  - `UsageState` 三态枚举：`MEASURED` / `ASSERTED` / `UNMEASURED`。
  - `UsageRequestRecord`：逐请求记录（`ordinal`、`source_event`、`role`、
    `provider`、`model`、`stop_reason`、input/cached-input/cache-write/output/
    reasoning/total tokens）。字段一律「provider 没报就是 `None`」，不补 0。
  - `UsageTotals`：一条聚合读数 + 产生它的 `basis`。同一个 block 可以并列携带
    `HOST_SUMMED_OBSERVED_RECORDS`（host 对观测到的记录求和）与
    `RUNTIME_REPORTED_SESSION_TOTALS`（运行时自报的整会话总量）两条，二者口径
    不同（后者含 tool/compaction 全会话），发生分歧时在明面上分歧，不合并成一个
    无法归因的数。
  - `UsageEvidence`：receipt-ready block，`provenance` 是开放大写词表（与
    `terminal_reason` 同风格），构造期强制状态自洽——`UNMEASURED`/`ASSERTED`
    携带任何读数即抛错，`MEASURED` 携带 assertion 即抛错，`MEASURED` 但没有任何
    读数也抛错。
  - `observed_count()`：从不可信 frame 读一个计数；非 bounded 非负 int（含
    `bool`）一律判为「未报告」而不是 0。
  - `summed_totals()`：只对确实报告过该字段的记录求和；全场没人报告的字段保持
    `None`。
- `runtime/contracts.py`：`BackendOutcome` 增加 typed 字段 `usage_evidence`
  （可选构造参数，默认 `UNMEASURED`）。**`BACKEND_OUTCOME_SCHEMA` 的线格式未
  改动**：`to_value()`/`from_value()` 字段集与原来逐字一致，session 自己写的
  result envelope 无法声明 `usage_evidence`（多带该字段直接 malformed）。只有
  受信 adapter 代码能挂上实测块。
- `runtime/pi.py`：新增 `_PiUsageCollector`，在 RPC 事件循环里逐条转录
  `message_end`（含没有最终文本的 tool 轮次与 `toolResult` 的嵌套 LLM 用量）与
  `compaction_end` 的 usage，并转录 `get_session_stats` 的 `tokens` 与
  `contextUsage`。失败路径（`_failure`）同样带上已观测到的读数——半途死掉的
  session 也烧掉了 token，不该在 receipt 里消失。evidence 增加
  `observed_pi_usage_reports` 计数。
- `runtime/command.py`：model-free command backend 三条出口都挂上
  `ASSERTED` 块（`COMMAND_BACKEND_MODEL_FREE_PROFILE`，
  `{"adapter_model_calls": 0, "adapter_provider_requests": 0}`），detail 明写
  这是 profile 断言、不是测量，并写明其边界（协作式进程组不是 sandbox）。
- `runtime/orchestrator.py`：`_persist` 增加 8 行——receipt 顶层写入
  `usage`；没有 backend outcome 的 session 写 `UNMEASURED` +
  `NO_BACKEND_OUTCOME`。其余编排逻辑未动。
- `runtime/store.py`：`usage` 进入 `_RECEIPT_FIELDS`（**必填**），并在
  `_validate_receipt_value` 中结构校验。durable receipt 不允许出现状态不明的
  token 数字。
- `cli.py`：两处硬编码 0 保留，但各自加上 authority 标注，明确它们是断言：
  - `model_calls: 0` + `model_calls_authority:
    HOST_ASSERTION_NO_PROVIDER_TRANSPORT_IN_THIS_PATH`
  - `network_calls: 0` + `network_calls_authority:
    HOST_ASSERTION_GIT_PROTOCOL_ALLOW_NEVER`（materializer 的每次 git 调用都带
    `-c protocol.allow=never`）

## Pi RPC usage 面的勘察结论

勘察对象：本机 pinned 树
`/Users/ycz/.local/lib/node_modules/@earendil-works/pi-coding-agent`
（`@earendil-works/pi-coding-agent` 0.84.1），依据 `docs/rpc.md` 与
`node_modules/@earendil-works/pi-ai/dist/types.d.ts`、
`node_modules/@earendil-works/pi-protocol/dist/schemas.d.ts`。

**该 surface 确实暴露 usage，且比 adapter 原先取用的多得多。** 具体：

1. `message_end` 事件的 `message.usage`（`pi-ai` 的 `Usage` 接口）：
   `input`、`output`、`cacheRead`、`cacheWrite`、可选 `cacheWrite1h`（仅
   Anthropic 报告）、可选 `reasoning`（是 `output` 的子集）、`totalTokens`，以及
   `cost` 明细。**每一条完成的 assistant 消息都带**，即每次 provider 请求一条；
   `toolResult` 消息在工具内部做了嵌套 LLM 调用时也带 `usage`。
2. `compaction_end` 事件的 `result.usage`：生成摘要那次 LLM 调用的用量，另有
   `tokensBefore` / `estimatedTokensAfter`（后者是启发式估计，不是 provider
   精确值，故未采信为 token 读数）。
3. `get_session_stats` RPC：`tokens{input,output,cacheRead,cacheWrite,total}`、
   `cost`、`contextUsage{tokens,contextWindow,percent}` 以及消息计数。文档明确
   `tokens`/`cost` 覆盖 assistant 消息 + 工具自报用量 + compaction/branch-summary
   生成的全会话总量；`contextUsage` 在无模型/无窗口时省略，且刚 compaction 完
   `tokens`/`percent` 可能为 `null`。

**原实现的缺口**（WP-A 之前）：adapter 只把「最后一条带文本的 assistant 消息」
的 `usage` 原样克隆进 `usage["pi_rpc"]["assistant_usage"]`，是一团未定型的
free-form JSON；所有 tool 轮次、`toolResult` 嵌套用量与 compaction 的用量全部丢
弃，且没有任何 provenance 或 typed 形状。`get_session_stats` 整包克隆保留了，但
同样没有定型。live-run 观察到的成本结构（98%+ cache read、input:output ≈ 299:1）
在平台侧因此不可见。WP-A 之后逐请求记录与两组聚合读数都进 receipt。

原有的 `usage["pi_rpc"]`（含 `assistant_usage`、`session_stats` 原始克隆、
`pi_reported_context_window` 等）**保持不变**，作为原始证据继续存在；typed block
是并列新增，不是替换。

## 明确不主张的事

- **不主张成本权威。** provider 报告的 `cost` 是价格换算，不是 token 计量；typed
  形状只收 token，`cost` 仍以原样克隆留在 `usage["pi_rpc"]["session_stats"]` 与
  `assistant_usage` 里，平台不为金额背书。
- **不主张两组聚合必然一致。** `HOST_SUMMED_OBSERVED_RECORDS` 与
  `RUNTIME_REPORTED_SESSION_TOTALS` 口径不同（例如 host 求和只覆盖本次 prompt
  周期内观测到的事件，运行时总量覆盖整个 session 文件）。二者并列呈现，分歧由读
  者判读。
- **不主张 command backend 的零是测量。** 它是 profile 断言：adapter 不开
  provider transport、不向子进程传任何 credential；但协作式进程组不是 sandbox，
  子进程自身行为没有被任何 surface 观测。
- **`BACKEND_SELF_REPORT` provenance 目前无人产出。** 词表里保留它，是给「自己
  真的量到了 provider transport 用量」的未来 adapter 用的；session 自写 envelope
  里的 `usage`（如 model-free worker 的 `{"model_calls": 0}`）**不会**被提升为
  measured 证据，只作为 free-form 自报留在 `outcome.usage` 里。
- **逐请求记录有上限。** 超过 4096 条时列表截断并置 `requests_truncated: true`，
  但聚合仍继续计数——聚合不会因为截断而少算。
- **UNMEASURED 不是 0。** 任何把 `UNMEASURED` 读成零用量的下游分析都是误用。

## 零模型测试

新增 `tests/test_usage_metering.py`（7 个用例，全部零模型；fake Pi 子进程按
pinned 树文档的 frame 形状说话）：

1. `test_reported_pi_usage_reaches_the_receipt_verbatim` —— fake Pi 报 4 条读数
   （assistant tool 轮 / `toolResult` / `compaction_end` / 最终 assistant 轮）+
   session stats；经完整 `run_prepared_cohort` 后逐字校验 receipt 里的 4 条记录、
   两组聚合与 context 读数。
2. `test_silent_pi_usage_surface_is_unmeasured_and_never_zero` —— 同一 fake Pi
   去掉所有 usage 字段 → receipt 为 `UNMEASURED` / `PI_RPC_SURFACE_SILENT`，且
   typed block 的 JSON 里连字符 `0` 都不出现。
3. `test_model_free_command_backend_keeps_its_asserted_zero` —— `examples/
   model-free-worker.py` 经 command backend 跑完；receipt 为 `ASSERTED`，同时
   worker 自报的 `{"model_calls": 0, "network_calls": 0}` 原样留在
   `outcome.usage`。
4. `test_a_session_result_envelope_cannot_declare_its_own_measurement` ——
   envelope 里塞 `usage_evidence` 直接 malformed；正常 envelope 解析出
   `UNMEASURED`。
5. `test_an_unmeasured_or_asserted_block_can_never_carry_a_reading` —— 四种状态
   自相矛盾的构造全部抛错。
6. `test_summing_never_turns_an_unreported_field_into_zero`。
7. `test_a_runtime_that_answers_with_junk_has_still_measured_nothing` —— 垃圾计数
   判为未报告；超长 role 降级为 `unknown` 而不是让 session 失败。

全量测试：`python -m pytest` → **228 passed, 2 skipped**（本分支之前基线为
221 passed, 2 skipped；2 个 skip 均为可选 PMW core 集成依赖，与本 WP 无关）。
