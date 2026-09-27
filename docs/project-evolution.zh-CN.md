# Pico 从 V1 到 V3 的项目演进

本文记录 Pico V3 的问题来源、旧调用链、阶段性架构决策和提交时间线。当前产品设计与使用方式以根目录 [README](../README.md) 为准。

## 1. 方法：先建立 baseline，再用真实任务打穿假设

Pico V3 不是先画出理想架构再一次性实现。项目先确认购入时的 Pico V1 能正常启动，再把它放进真实废弃仓库，按人类使用方式执行读、写、修复、测试、中断和恢复。每一阶段都要求先复现调用链，再从工业实现、软件工程和程序员行为研究中选择可解释的机制。

最初压力测试包括七类：

1. 真实编码：单文件、多文件、失败后修复、调用链分析、范围控制；
2. 工具极端场景：零/一次/多次 patch、编码、长路径、锁、权限、超时和 partial success；
3. Git 工作区：未提交修改、未跟踪文件、子目录、嵌套仓库、detached HEAD 和普通目录；
4. 会话恢复：退出恢复、强制中断、外部修改、损坏 Session 和并发会话；
5. 安全：路径与 symlink 逃逸、Shell 绕过、只读、审批和密钥泄漏；
6. Provider 异常：401/429/500、超时、空响应、非法 JSON、截断协议和 SSE 中断；
7. 性能体验：首次响应、模型/工具耗时、token、重试、阶段进度和取消。

另外加入长期/临时记忆、Python/Java 复杂功能、前端任务和算法题。这个顺序使新增机制来自真实失败，而不是为了“看起来像 Agent”堆功能。

## 2. 阶段 0：原始 Pico V1

V1 已经具备本地 CLI、模型调用、文件工具、Shell、Memory、Checkpoint、Resume、Trace 和 Report，因此不是纯聊天 Demo。但它的公开 benchmark 主要是小型固定 patch，无法证明真实 repository-level coding。

真实 Python/Java 任务暴露的共同模式是：

```text
读取
  → 搜索
  → 再读取
  → 上下文增长
  → 工具预算耗尽
  → 0 写入 / 0 验证
```

这说明“拥有工具”和“能完成工程任务”是两个不同层次。

## 3. 阶段 1：P0——Transactional Shadow Workspace

### 问题

工具副作用直接落在源工作区。命令执行一半失败、模型被中断、Session 恢复错位或多文件修改只完成一部分时，用户可能拿到不可运行代码。

### 设计来源

- 数据库事务的 begin/commit/rollback；
- Git worktree、staging 与三方冲突检查；
- 工业 Coding Agent 的隔离候选变更；
- 人类开发者在分支或草稿副本中试验，验收后才合入正式代码。

### 新调用链

```text
源工作区
  → 建立 Shadow 事务
  → 在 Shadow 中修改和验证
  → 生成候选 patch
  → 检查源工作区漂移、范围和保护文件
  → Review
  → Commit / Rollback
  → Commit 后销毁 Shadow 代码
```

TSW 解决代码事务，不声称提供恶意进程隔离。Docker/虚拟机属于外层部署边界。

## 4. 阶段 2：P1——模型调用、完成与上下文可靠性

P1 压测暴露：重复读取耗尽预算、残缺 `<tool>` 被接受、空 `{}` 或 reasoning-only 被当成完成、SSE 中断保留部分文本、thinking 打满输出、固定 final token 过小、工具历史反复重放导致上下文膨胀。

旧链路混淆了四件事：

```text
HTTP/流传输结束
  = 文本完整
  = 协议合法
  = 任务完成
```

V2/V3 把它们拆开：

- Provider transport outcome；
- response completeness；
- model/tool protocol contract；
- Runtime completion/delivery admission。

主要机制包括：

- canonical tool/evidence identity；
- completed/incomplete/truncated/transport-failed/protocol-failed 分类；
- bounded recovery 和 finalization recovery；
- adaptive/fast/deep model execution policy；
- Qwen thinking 显式策略；
- 动态输出目标，不使用固定 512 token final；
- 上下文压缩并优先保留失败、修改和任务状态；
- 工具步骤、模型恢复轮次和交付轮次分别记账。

这里参考工业 provider adapter、structured outcome、retry budget 与 context compaction，也遵循可靠系统中“传输成功不等于业务成功”的原则。

## 5. 阶段 3：P2/P3——状态、平台与体验

这一阶段处理基础工程问题：

- 长期记忆从 final 特殊文本改为 admission、merge、persistence、retrieval；
- 损坏 Session 进入 `SessionLoadError` 和受控恢复；
- state root 移出仓库，避免 `.pico/` 污染 Git；
- Windows Shell、PATH、`.cmd`、引号、长 Unicode 路径、BOM/GBK 和时区统一处理；
- socket timeout 进入统一 Provider 错误；
- CLI 展示阶段、步骤、模型/工具耗时、重试和事务状态；
- Provider 网络、Maven 冷缓存、构建与 Runtime 本地时间分开统计。

人类启发是：环境不是背景噪声，而是任务事实；长任务中的协作者需要知道系统当前在做什么。

## 6. 阶段 4：Human-in-the-loop 交互策略

真实使用提出了四类问题：

1. 用户运行中打断，通常表示新任务更紧急；
2. 当前要求可能临时违背长期偏好，不能由旧记忆压过新意图；
3. “小一点”表示适度，而不是极端最小实现；
4. 工程结构偏好因团队而异，不能强制按 feature 或 layer 组织。

这些要求进入 `InteractionPolicy`：

```text
当前请求
  > 当前任务约束
  > Session 临时状态
  > Workspace 偏好
  > Durable Memory
  > Runtime 推断
```

同时加入 proportionality、package layout、protected path、测试义务和工程链检查。人类偏好成为结构化控制面，而不是散落在聊天历史中的一句话。

## 7. 阶段 5：Repository-level coding

复杂 Python/Java/Vue 任务说明 `read_file + patch_file + shell` 仍不足以形成真实编码能力。项目加入：

- Python AST 与 Java 语法/import 仓库图；
- 可选 multilspy 定义/引用；
- 带修订身份的 source working set；
- read/mutation observation；
- 原子跨文件 patch set；
- run outcome、独立验证和调用方检查；
- 配置、测试、实现与构建输出联合定位。

它综合 LSP、静态程序分析、SWE-agent Agent-Computer Interface 和 repository benchmark 的做法，同时保留动态运行结果，避免把静态图夸大为完整语义证明。

## 8. 阶段 6：Evidence-driven coding loop

### 旧问题

V1 式总步数和重复阈值只能终止任务，不能使任务收敛。模型可能读到关键报错后又读取其他文件；旧错误被后续输出稀释，再次回到已经走过的路径。

### 机制

- evidence frontier：当前决定缺少什么；
- revisioned evidence：事实属于哪个代码版本；
- unresolved failure：仍未解释的负反馈；
- verification identity：哪条命令验证哪个修订；
- work focus：当前已知、开放问题与建议阶段；
- obligation-driven plan：探索服务哪个工程义务；
- mutation feedback：修改是否实际落盘并作用于目标文件。

这一步把重点从“限制模型调用多少次”转为“模型目前掌握什么、下一步要消除什么不确定性”。

## 9. 阶段 7：V3 LangGraph 与代码质量

V2 已具备主要产品语义，但快速迭代造成主循环超过 1500 行、状态交接集中和目录扁平。V3 保持行为等价，完成：

- bootstrap/action/finalization/delivery 的 LangGraph 编排；
- 大型主循环按阶段拆分；
- cli/runtime/domain/context/memory/progress/tools/execution/workspace 等包边界；
- 删除独立 demo Runtime 和废弃 Docker-in-Docker sandbox；
- TSW、Provider、Memory、Session 等状态保持单一权威；
- 完整继承 V2 回归并增加图路由测试。

LangGraph 是生命周期编排和可观测性工具，不是第二套业务状态或持久化系统。

## 10. 阶段 8：统计验收与 Evidence Assimilation

8 个 Java 任务每题重复 3 次，初始获得 23/24 首轮成功。唯一失败任务不是丢失 Maven 错误，而是模型持续产生“不同但不能解决决定”的观察。

失败轨迹中：

- 36 个工具事件；
- 29 个记为 `NEW_EVIDENCE`；
- 4 个记为 `NO_PROGRESS`；
- 最大连续探索 18 步；
- work focus 只有建议权；
- 改写 plan 措辞也会被当作决策变化。

旧链路为：

```text
新 observation identity
  → NEW_EVIDENCE
  → advisory work_focus
  → 所有 discovery tools 可用
  → 另一个新观察或措辞变化
  → 同一个决定仍未解决
```

关键结论是：

```text
输出是新的
  ≠ 解决了决策问题
  ≠ 更接近正确修改
```

当前 evidence episode 链路为：

```text
work obligation
  → hypothesis / blocker
  → 带 decision question 的判别动作
  → actual observation
  → decision_due
  → 基于证据行动
       或 evidence_assessment + concrete next decision
  → 下一 episode
```

普通文字改写不能关闭 `decision_due`；mutation、verification 和 finalization 保持可用，因此它不是“为了动作强迫动作”。更多细节见 [ADR 0002](decisions/0002-evidence-assimilation-at-action-boundary.md)。

## 11. V1 与 V3 机制对照

| 维度 | 原始 Pico V1 | Pico V3 |
| --- | --- | --- |
| 主循环 | 模型文本与工具组成的集中循环 | LangGraph 生命周期，领域服务持有业务真相 |
| 探索 | 模型自行决定何时读够 | obligation、blocker、expected observation、evidence episode |
| 重复 | 总步数和重复工具判断 | 区分调用重复、观察新颖、决策进展、交付进展 |
| 上下文 | 历史增长，关键证据可能被挤出 | 状态投影、轨迹压缩、失败与当前修订优先 |
| 失败 | 普通工具输出 | 高优先级、可消解和退休的领域状态 |
| 修改 | 直接工具副作用 | Shadow 原子工作单元，Review 后提交 |
| 多文件 | 可能 partial success | rollback-capable patch set 和事务提交 |
| 验证 | 最近成功命令容易代表完成 | diagnostic/acceptance 分层并绑定修订/构建环境 |
| 完成 | 依赖模型 final | Provider、产物、验证、事务联合审查 |
| Provider | 文本/截断/网络错误边界模糊 | typed outcome、恢复轮次、动态 execution policy |
| 记忆 | 长短期边界不完整 | Working/Durable 分层与准入、冲突、检索、过期 |
| 恢复 | 能打开记录但状态可能失真 | Session、Checkpoint、事务日志、workspace drift 对账 |
| 环境 | 隐含 Unix 假设 | 动态 OS/Shell/toolchain profile |
| 用户偏好 | 自然语言历史 | 意图优先级、比例、包布局、临时/长期偏好 |
| 可见性 | 长请求接近黑盒 | 阶段、耗时、重试和事务事件 |
| 评测 | 小型固定 patch | Harness 与真实模型重复运行分开报告 |

## 12. 提交时间线

| 提交 | 主要演进 |
| --- | --- |
| `bd90c9a` | 原始 Pico V1 baseline |
| `6134e8f` | Transactional Safety Workspace |
| `7784ff3` | P1 Runtime/Provider/完成判定可靠性 |
| `45eefca` | P2/P3 状态、跨平台、进度与 UX |
| `45f566b` | 用户中断、记忆冲突、比例和包结构偏好 |
| `5c6b35b` | 仓库图与真实编码可靠性 |
| `c6d87bd` | 自适应上下文、状态可靠性、repository intelligence |
| `1db60bd` | Evidence-driven coding loop |
| `e9e4fbf` | 读取、修改、失败和验证证据统一 |
| `782665e` | 动态环境画像 |
| `28a7a4f` | 执行环境和验证身份可信化 |
| `1e4bfaa` | working set、原子 patch、work focus |
| `7c1b99d` | obligation-driven exploration |
| `f7cf34d` | V2 交付和执行链稳定化 |
| `6c86019` | V3 LangGraph 与包结构迁移 |
| `e934083` | 验证绑定实际构建环境 |
| `90be45a` | evidence-to-action 闭环 |

## 13. 演进证据的边界

演进记录用于解释“为什么这样设计”，不应取代当前 README 和正式测试：

- 历史失败证明某条调用链存在，不证明所有任务都会失败；
- 单个成功案例支持方向，不构成统计显著性；
- V1/V2 小样本对照不能包装为通用排行榜成绩；
- 23/24 是初始批量首轮结果；缺陷修复后失败槽位重跑通过，但尚未重新完成一轮全新 24 槽位 pass@1；
- 当前行为以代码、自动化测试、ADR 和新的重复运行结果为准。

V3 与 V1 面目全非，不是因为换了一个 Prompt，而是压力测试逐层打穿了 Runtime 对状态、执行、记忆、反馈和交付的旧假设。
