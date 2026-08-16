# PMW Research Platform

一个把长期数学状态与可替换研究 session 分开的通用运行平台。数学世界持续演进，
实验只创建新的 cohort；不再为了每次运行复制一套 `M0i → M0i+1` apparatus。

> M04 仍然冻结且从未启动。安装、测试、生成 plan 或读取状态都不会自动启动
> M04，也不会发起模型、OAuth 或网络请求；真实运行必须由操作者显式发起。

## 运行模型

- **World** 是持续存在的 PMW 数学状态，包含当前 snapshot、已有研究记录与
  artifact 引用。
- **Cohort plan** 冻结一次运行所读的 world ref/snapshot、完整数学 briefing、
  profile/core 摘要、显式 session ID 和并发数。
- **Session** 是一个可替换的研究进程。host 从已认证 plan 注入身份；backend
  只能返回数学内容，不能自填 `world/cohort/session/base snapshot`。
- **Backend** 只负责启动、等待和停止一种执行环境。协调器、持久化、终态判断
  与 PMW 写回均由 host 负责。

```text
canonical plan + briefing
          │ authenticate profile / core / world / artifacts
          ▼
       launch.json                 （plan + backend + publish + limits）
          │ fixed concurrency
          ├── session 0001 ── backend ── host publish ── receipt
          ├── session 0002 ── backend ── host publish ── receipt
          └── session 000N ── backend ── host publish ── receipt
                                                │
                                                ▼
                                          settlement.json
```

`plan` 回答“研究哪个数学状态、有哪些 session”；`launch` 回答“这次由哪个
backend、以什么运行边界执行”。`launch.json` 还明确记录 publication identity：
默认是只读的 `DISABLED`；只有 trusted host 配置 PMW writer authority 时才是
`PMW_BOUND`。两者分开后，同一个数学状态可以自然地接入 1、4、8 或更多并发
session，而不改变数学世界本身。

## 通用 runtime adapter

backend 的最小协议刻意很小：公开且可哈希的 `BackendIdentity`，以及
`start(request) → handle`；handle 提供 `wait()` 与幂等 `stop()`。host 在启动
任何外部进程前认证唯一的 `runs/<cohort>/plan.json`，为每个 session 分配独立的
`private/input/workspace/cache/evidence` 路径，并最终写入有界、不可变的 receipt
和 cohort settlement。

`startup_seconds`、`session_wall_seconds` 与 `stop_grace_seconds` 只由 host 的
launch limits 定义一次，再随已认证的 `SessionRequest` 交给 adapter；backend
配置不重复声明另一套 wall/stop 值。这样终止预算只有一个权威，也不会因为两套
计时器不一致而把已经正确停止的 session 错判为 `UNKNOWN`。

backend adapter 本身属于受信任的 host transport，必须可取消、遵守给定的
graceful-stop 时段，并在返回前同步完成有界的强制终止与证据收尾；host 不会超时后
遗弃后台 cleanup。不受信任的是 adapter 所管理的研究进程/VM。不能把任意阻塞
Python callable 直接冒充 backend，否则 host 无法证明取消时没有孤儿副作用。

backend 返回的是有界 `BackendOutcome` 和无身份 `ResearchContribution`。
只有 host 能把已认证的 `SessionSpec` 绑定到贡献并写入 PMW。因此更换 command、
Pi RPC 或其他 backend 不会改变数学记录的身份边界。

当前内置两个 adapter：

- **command backend** 以独立进程组运行显式命令，持续排空并摘要
  stdout/stderr，读取严格校验且有界的 result，停止时执行
  TERM → grace → KILL，并报告能否证明进程组已消失。它用于无模型的端到端
  验收；平台不会把测试 command 暗中替换为模型请求。
- **Pi RPC backend** 向内容固定的 Pi 安装发送一次通用 research prompt，等待
  `agent_settled`，关闭内置工具，只加载显式 pin 的 extension 入口。host 不替 Pi
  做 retry、compaction、模型降档或 context downcap。adapter 不把 OAuth
  credential 值或路径主动序列化进公开 identity；但 Pi 子进程的有界 raw
  frame/stderr evidence 仍是 trusted/redaction boundary，不能假定第三方错误消息
  永远不会回显敏感内容。

Pi adapter 目前通过 fake RPC 覆盖协议与生命周期，并只读加载过本机 Pi 安装及
OAuth 类型以检查配置兼容性；没有执行新的真实模型/provider canary，也没有发起
网络请求或消耗模型 token。`pi_reported_context_window` 只是 Pi 运行时/模型目录
报告的值，不能解释为该账号 OAuth 路由已在接近该上限处实测成功。

backend config 可以使用普通缩进 JSON；loader 会拒绝重复 key、非有限数值、未知
字段与越界值，再把有效内容 canonicalize 后纳入 identity。安全占位示例见
[`examples/command-backend.json`](examples/command-backend.json) 与
[`examples/pi-backend.example.json`](examples/pi-backend.example.json)；其中绝对路径
必须替换为操作者已经审查并信任的本地文件。

command backend 的边界是“受管但协作式的进程组”，不是 OS sandbox。它不继承
宿主环境中的 token/API key，但同一账户下的恶意程序仍可能主动探测宿主可读路径；
因此它适合受信任的本地 worker、测试和被更强 sandbox 包裹的执行器。

## 边界，而不是历史仪式

平台不继承 M01–M03 的 treatment、wave authorization、ballot 或 target-specific
审计，也不因正常 hardlink 或单个较大研究文件直接杀死 session。默认资源 guard
只在 session 激活与 stop 完成后做聚合扫描，并在运行期间低频检查 host
磁盘余量；它限制 workspace/cache 的总字节、entry 数与深度，按 inode 去重
hardlink 字节，不跟随 symlink，不执行单文件或“出现 hardlink 即击杀”规则。

平台层也不再自设 325k/360k 一类模型 context ceiling。模型 backend 应使用并
记录 provider 实际公开的 context window 与 usage；若 provider 拒绝请求，应如实
失败，而不是自动 compact、重试、静默降档或伪称已使用更大的窗口。command
backend 本身没有模型 context 概念。

generic runtime 是生命周期与证据边界，不是面向 hostile code 的 OS sandbox。
另外，PMW admission 与本地 receipt 分属两个 durable system：若进程恰好在
admission 成功后、receipt 落盘前崩溃，必须由操作者按 PMW admission/launch
证据 reconciliation；平台不会把该 session 静默重跑。

## 已有基础

| 能力 | 保证 |
|---|---|
| World | 注册、精确 snapshot 读取、delta、单条回读、完整 PMW audit |
| Briefing | 目标问题全文 + 当前研究状态的有界投影、内容哈希和精确回读引用 |
| Artifacts | 独立 SHA-256 CAS；历史 store 无链接复制；world 引用闭包审计 |
| Plan authentication | canonical plan、briefing、profile、core lock、world ancestry 与 artifact closure 一起认证 |
| Backend contract | 公开身份无凭证值；请求身份由 host 固定；结果有界且无 session 身份 |
| Publish API | launch 明示 `DISABLED`/`PMW_BOUND`；host 注入 `SessionSpec` 并在写回前验证 artifact 引用 |
| Safety | lifecycle 单一权威、有界输出、聚合资源 guard、独立工作/缓存路径；不把正常研究行为当入侵 |

基础包可以独立安装。需要读写 PMW world 时，再暴露 source lock 指定的核心，
或在有权读取该仓库的环境中安装 `.[pmw]`。公共 CI 不持有跨私有仓库凭证，
因此依赖真实 PMW 的连续性用例单独运行；runtime 契约与 command 验收不需要模型。

## 操作面

```bash
# 注册已经存在的 PMW world
pmw-research world add math-frontier \
  --repo ~/Documents/pmw-research-data/worlds/math-frontier.git \
  --world-ref refs/pmw/frontier-choice-world \
  --snapshot snapshot/sha256/...

# 查看完整数学状态
pmw-research world status math-frontier
pmw-research world briefing math-frontier
pmw-research world get math-frontier --admission admission/sha256/...
pmw-research world delta math-frontier --since snapshot/sha256/...

# 生成冻结输入；这一步不会运行 session
pmw-research session plan \
  --world math-frontier --count 8 --concurrency 4 \
  --profile research-default

# 显式运行；backend config 是独立的 launch identity，不进入数学 plan
# 先复制 example，并替换其中的安全本地绝对路径
pmw-research session start \
  --cohort cohort-... --backend command \
  --backend-config ./examples/command-backend.json \
  --startup-seconds 60 --wall-seconds 86400 --stop-grace-seconds 10

# 纯读取，不启动、不恢复任何进程
pmw-research session status --cohort cohort-...
```

运行数据默认位于
`~/Documents/pmw-research-data/{worlds,runs,objects,source-cache,archive}`，不进入
Git。问题/验证器权威属于 `agent-math-frontier`，数学状态权威属于
`persistent-mathematical-worlds`，本仓库只负责通用控制面与 runtime。

M01–M03 是历史实验和迁移证据，不是工作流模板。更详细的设计边界见
[Architecture](docs/ARCHITECTURE.md)，迁移证据见 [Migration](docs/MIGRATION.md)，
安全语义见 [Safety](docs/SAFETY.md)。
