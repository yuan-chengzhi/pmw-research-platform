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
read-only preflight ───────┘          （advisory；不创建 runtime）
          ▼
   RuntimeClaim 内重做 required readiness
          │ bind source / verifier portfolio / backend pins
          ▼
       launch.json          （plan + backend + publish + limits + context + readiness）
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
  `agent_settled`，只启用配置中精确列出的内置工具和显式 pin 的 extension 入口。
  示例配置默认是 `tools: []` 与 `extensions: []`；工具必须由操作者显式开启。host
  不替 Pi 做 retry、模型降档或隐式 context downcap。adapter 不把 OAuth
  credential 值或路径主动序列化进公开 identity；但 Pi 子进程的有界 raw
  frame/stderr evidence 仍是 trusted/redaction boundary，不能假定第三方错误消息
  永远不会回显敏感内容。

Pi adapter 通过 fake RPC 覆盖协议与生命周期；另有一次 zero-provider 本机 smoke
真正启动了 pinned Pi，但只发送 `get_state`，确认 400000 已进入 active model object
且 settings 未改变，未发送 prompt/provider 请求、未刷新 OAuth、未联网或消耗模型
token。它不是模型/provider canary。`pi_reported_context_window` 只是 Pi 运行时/模型
目录报告的值，不能解释为该账号 OAuth 路由已在接近该上限处实测成功。

backend config 可以使用普通缩进 JSON；loader 会拒绝重复 key、非有限数值、未知
字段与越界值，再把有效内容 canonicalize 后纳入 identity。安全占位示例见
[`examples/command-backend.json`](examples/command-backend.json) 与
[`examples/pi-backend.example.json`](examples/pi-backend.example.json)；其中绝对路径
必须替换为操作者已经审查并信任的本地文件。

command backend 的边界是“受管但协作式的进程组”，不是 OS sandbox。它不继承
宿主环境中的 token/API key，但同一账户下的恶意程序仍可能主动探测宿主可读路径；
因此它适合受信任的本地 worker、测试和被更强 sandbox 包裹的执行器。

Pi 内置工具也处在协作式边界。显式开启 `bash` 后，它使用宿主账户权限，可能读取
workspace 之外的路径或发起网络访问。当前 adapter 只对 Pi RPC frame/stderr 证据做
总量限制，尚没有介于内置 `bash` 与模型之间的 bounded-output projection；超大工具
结果仍可能消耗 active context。因此 `tools: []` 是当前最小权限起点，开启 `bash`
是明确的能力/风险决策，不是默认保证。

## 边界，而不是历史仪式

平台不继承 M01–M03 的 treatment、wave authorization、ballot 或 target-specific
审计，也不因正常 hardlink 或单个较大研究文件直接杀死 session。默认资源 guard
只在 session 激活与 stop 完成后做聚合扫描，并在运行期间低频检查 host
磁盘余量；它限制 workspace/cache 的总字节、entry 数与深度，按 inode 去重
hardlink 字节，不跟随 symlink，不执行单文件或“出现 hardlink 即击杀”规则。

平台层没有隐藏的 325k/360k ceiling。每次 launch 可以不设值（默认，沿用 backend
声明的模型窗口），也可以用 `--context-window-tokens` 为全部 session 选择总 context
window，并用可重复的 `--session-context-window SESSION_ID=TOKENS` 精确覆盖某个
session。Pi 在首个 prompt 前原生应用并回读核对这个值；它影响 Pi 的输出预算、
compaction 与 overflow 判断，但不是累计 token 配额，也不是声称 OAuth 路由已做过
近上限 canary 的严格 pre-HTTP input gate。provider 拒绝仍应如实失败，不会自动
重试或静默降档。command backend 没有模型 context 概念，因此配置 context 会在
创建 runtime 前被拒绝。

当 Pi launch 显式设置 context window 时，当前实现还要求 `extensions: []`；任何
外部 Pi extension 与这个 context mutation 的组合都会在启动前以
`PI_CONTEXT_EXTENSION_COMPATIBILITY_UNPROVEN` 拒绝。这是当前组合的兼容性边界，
不是对 extension 的通用禁止。若必须加载 extension，应暂时留空 context 设置并沿用
backend-declared window，或先完成独立的兼容性设计与验收。

generic runtime 是生命周期与证据边界，不是面向 hostile code 的 OS sandbox。
另外，PMW admission 与本地 receipt 分属两个 durable system：若进程恰好在
admission 成功后、receipt 落盘前崩溃，必须由操作者按 PMW admission/launch
证据 reconciliation；平台不会把该 session 静默重跑。

## 已有基础

| 能力 | 保证 |
|---|---|
| World | 注册、精确 snapshot 读取、delta、单条回读、完整 PMW audit |
| Briefing | 目标问题数学内容 + 非生效历史 budget 的显式 omission provenance + 当前研究状态的有界投影与精确回读引用 |
| Artifacts | 独立 SHA-256 CAS；历史 store 无链接复制；world 引用闭包审计 |
| Locked source | 从操作者指定的本地 Git object database 物化 core-lock 精确 commit；完整 tree digest 可重审 |
| Plan authentication | canonical plan、briefing、profile、core lock、world ancestry 与 artifact closure 一起认证 |
| Readiness | `preflight` 只读预检；`start` 在 RuntimeClaim 内重做 backend/source/apparatus 检查并把证据绑定进 launch |
| Backend contract | 公开身份无凭证值；请求身份由 host 固定；结果有界且无 session 身份 |
| Publish API | launch 明示 `DISABLED`/`PMW_BOUND`；host 注入 `SessionSpec` 并在写回前验证 artifact 引用 |
| Verifier | 结算后由 host 捕获 candidate 到 CAS、重执行 briefing-bound 的 pinned AMF verifier，并持久化不可变 receipt |
| Safety | lifecycle 单一权威、有界输出、聚合资源 guard、独立工作/缓存路径；不把正常研究行为当入侵 |

基础包可以独立安装。需要读写 PMW world 时，先从操作者指定的本地 Git object
database 把 source lock 中的 PMW commit 物化进 managed `source-cache`；CLI 会在完整
tree audit 后直接从该只读树加载 `pmw_r2`，不要求旧工作树、editable install 或
父目录 `.git` 继续存在。`.[pmw]` 只方便直接使用上游 Python 包，不是 runtime 的
source identity。公共 CI 不持有跨私有仓库凭证，因此依赖真实 PMW 的连续性用例
单独运行；runtime 契约与 command 验收不需要模型。

## 操作面

```bash
# 先从已审查的本地 Git object database 物化 core-lock 精确源码；不会 fetch
pmw-research source materialize agent-math-frontier \
  --local-repo ~/Documents/agent-math-frontier
pmw-research source materialize persistent-mathematical-worlds \
  --local-repo ~/Documents/persistent-mathematical-worlds
pmw-research source audit agent-math-frontier
pmw-research source audit persistent-mathematical-worlds

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

# 先复制 example 并替换其中已审查 worker 的绝对路径；再只读预检精确 launch 配置
# preflight 不建 runtime、不启动 backend
# amf-production 是默认 scope；runtime-only 只表示 transport 就绪
pmw-research session preflight \
  --cohort cohort-... --backend command \
  --backend-config ./examples/command-backend.json \
  --startup-seconds 60 --wall-seconds 86400 --stop-grace-seconds 10

# 显式运行；start 会在 launch claim 内重做 required readiness 检查
pmw-research session start \
  --cohort cohort-... --backend command \
  --backend-config ./examples/command-backend.json \
  --startup-seconds 60 --wall-seconds 86400 --stop-grace-seconds 10

# Pi/account 路由希望采用约 400k 总窗口时；值会冻结进 launch.json
pmw-research session start \
  --cohort cohort-... --backend pi \
  --backend-config ./pi-backend.json \
  --context-window-tokens 400000 \
  --session-context-window cohort-...-session-0002=360000

# 纯读取，不启动、不恢复任何进程
pmw-research session status --cohort cohort-...

# 仅对已结算 session 的 workspace-relative candidate 做 host 复验
pmw-research verifier run \
  --cohort cohort-... --session-id cohort-...-session-0001 \
  --target-id aim-60-first-prime --candidate relative/path/to/candidate.json
```

`session preflight` 的 PASS 是建议性快照，不是启动授权。真正的 `start` 获取
RuntimeClaim 后会重做可变的 backend pin、source 和 apparatus 检查，然后才创建
`launch.json`；其 canonical 公开证据与哈希会进入 launch 并下发到 session invocation。

当前产物应准确理解为“通用 cohort/runtime production candidate”。它已有结算后的
host verifier，但尚无 agent-facing 的 live verifier tool，也没有 M01–M03 那种实时
PMW read/query/peer-update 协调 tool plane。这些是后续独立能力层，不能从当前
runtime 验收中推导出来。

运行数据默认位于
`~/Documents/pmw-research-data/{worlds,runs,objects,source-cache,archive}`，不进入
Git。问题/验证器权威属于 `agent-math-frontier`，数学状态权威属于
`persistent-mathematical-worlds`，本仓库只负责通用控制面与 runtime。

M01–M03 是历史实验和迁移证据，不是工作流模板。更详细的设计边界见
[Architecture](docs/ARCHITECTURE.md)，迁移证据见 [Migration](docs/MIGRATION.md)，
安全语义见 [Safety](docs/SAFETY.md)。
