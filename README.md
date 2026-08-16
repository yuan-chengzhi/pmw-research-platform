# PMW Research Platform

一个面向开放数学研究的长期世界与多 session 控制面。

> 当前状态：**model-free foundation**。它不会启动 agent、Pi 或模型请求；
> `session start` 尚不存在。现在可以可靠地整理数学状态、冻结 cohort 输入、
> 验证并发/失败隔离和写回协议，为真实运行适配器提供清楚的边界。

## 三个概念

- **World**：持续存在的 PMW 数学状态。M03 之后不再创建 `M0i+1` 新世界，
  后续 cohort 直接接入当前 snapshot。
- **Session**：目标语义中的一个可替换研究进程。host 为它绑定身份；agent
  只能提交数学内容，不能自行填写 `world/cohort/session/base snapshot`。
- **Cohort**：一次计划与并发边界，不是新的数学世界。单个 cohort 最多
  4096 个显式 session，可用 `concurrency` 控制同时运行数；需要更多时自然
  新建下一个 cohort。

```text
one long-lived mathematical world
        │
        ├── cohort A: N sessions ──┐
        ├── cohort B: N sessions ──┼── immutable admissions → advancing head
        └── cohort C: N sessions ──┘
```

## 当前已经实现

| 能力 | 当前保证 |
|---|---|
| World | 注册、精确 snapshot 读取、delta、单条回读、完整 PMW audit |
| Briefing | 14 张问题卡全文 + 每条研究记录的有界投影、内容哈希和精确回读引用 |
| Artifacts | 独立 SHA-256 CAS；历史 store 无链接复制；world 引用闭包审计 |
| Plan | 冻结 world ref/snapshot、策略摘要、核心依赖摘要、briefing 摘要和全部 session ID |
| Publish API | `ResearchContribution` 不含身份字段；host-side API 注入 `SessionSpec` 并验证 artifact |
| Concurrency | 固定大小 worker pool；单 session 失败不取消 peers；取消状态可区分 |
| Safety primitives | 数据化 disposition 与有界输出捕获；默认不因大文件或 hardlink 杀 session |

尚未实现真实 agent/process launcher、OAuth/模型适配、OS containment 执行器、
provider 计费器和完整持久 settlement。因此本仓库目前不宣称“真实 session
平台已经完成”；未来 runtime 仍必须认证 plan 与 session，不能把调用方构造的
`SessionSpec` 当作身份凭证。

基础包可以独立安装；需要读写 PMW world 时，再暴露 source lock 指定的核心，
或在有权读取该仓库的环境中安装 `.[pmw]`。公共 CI 不持有跨私有仓库凭证，
因此只跳过依赖真实 PMW 的集成用例；精确 M03 连续性由本地锁定核心单独验证。

## 操作面

```bash
# 注册已经存在的 PMW world
pmw-research world add math-frontier \
  --repo ~/Documents/pmw-research-data/worlds/math-frontier.git \
  --world-ref refs/pmw/frontier-choice-world \
  --snapshot snapshot/sha256/...

# 查看数学世界
pmw-research world status math-frontier
pmw-research world briefing math-frontier
pmw-research world get math-frontier --admission admission/sha256/...
pmw-research world delta math-frontier --since snapshot/sha256/...

# 把历史 artifact store 独立复制到全局 CAS，然后检查引用闭包
pmw-research artifact import --source /path/to/artifacts --label frontier-m03
pmw-research artifact audit --world math-frontier

# 只生成输入完全冻结的 model-free cohort；不会调用模型
pmw-research session plan \
  --world math-frontier --count 8 --concurrency 4 \
  --profile research-default
```

运行数据默认位于
`~/Documents/pmw-research-data/{worlds,runs,objects,source-cache,archive}`，
不进入 Git。仓库身份与数据路径分离：问题/验证器权威属于
`agent-math-frontier`，数学状态权威属于 `persistent-mathematical-worlds`，
本仓库只负责通用控制面。

M01–M03 是历史实验与研究证据，不是工作流模板；M04 已冻结且从未启动。
设计边界见 [Architecture](docs/ARCHITECTURE.md)，迁移证据见
[Migration](docs/MIGRATION.md)，安全语义见 [Safety](docs/SAFETY.md)。
