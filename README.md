# Pico V3

[![CI](https://github.com/aohodo/picoV3/actions/workflows/ci.yml/badge.svg)](https://github.com/aohodo/picoV3/actions/workflows/ci.yml)

Pico V3 是一个面向真实代码仓库的本地 Coding Agent Runtime。它不是把“大模型 + 文件工具”包装成聊天程序，而是尝试把熟练程序员完成工程任务时可观察的工作方式，落实为可执行、可恢复、可审计的运行时机制。

项目以购入时的原始 Pico V1 为基线。V1 已经具备模型调用、文件工具、Shell、Memory、Checkpoint 和 Resume 等 Harness 组件，但真实跨文件任务容易陷入反复探索、上下文膨胀、负反馈丢失、状态互相矛盾，以及“模型说完成，Runtime 就认为完成”。V3 没有继续依靠读取次数、固定 token 和阶段阈值压制症状，而是重建了贯穿任务全生命周期的证据链、反馈链与交付链。

V3 使用 LangGraph 显式编排运行阶段，但不把领域状态外包给图框架。任务、证据、失败、验证、事务、会话和长期记忆分别有明确的状态所有者；LangGraph 只负责让它们沿同一条产品路径协作。

> Pico V3 面向由人拆分好的、可独立运行和验证的软件工程工作单元。大型项目仍由人决定阶段和边界，Pico 负责可靠完成每一个工作单元，并保证前后阶段能够安全集成。

## 项目要解决的真实问题

一次跨文件编码任务通常包含：

```text
理解目标与限制
  → 确认环境和仓库结构
  → 定位相关实现
  → 形成局部假设
  → 修改一组相互关联的文件
  → 运行测试或构建
  → 解释负反馈
  → 修正实现
  → 检查范围和代码质量
  → 安全交付
```

如果 Runtime 只保存聊天历史和工具输出，长任务中会发生以下失真：

- 代码虽然读过，但关键内容被后续工具输出挤出上下文；
- 测试已经给出明确错误，模型却转去浏览其他文件；
- 文件已经变化，旧摘要、旧验证仍被当成当前事实；
- 一次验收失败后执行无关成功命令，交付端只看到最后的退出码；
- 模型输出截断的 `<tool>` 或只有 reasoning，Runtime 却接受为完成；
- 中断恢复后，新请求与旧事务的交付授权发生混用；
- 多文件修改执行到一半失败，源仓库留下不可用的中间状态；
- 用户临时改变要求，历史记忆却继续压过当前意图。

这些不是一句 Prompt 能稳定解决的问题。Pico V3 把它们建模为运行时状态和跨模块协议，使“模型建议下一步”与“系统判定哪些事实有效、哪些代码可以交付”彼此分离。

## 设计背景

Pico V3 以购入时的原始 V1 为基线，经过真实仓库的编码、工具、Git、恢复、安全、Provider、性能和记忆压力测试逐步演进。README 只描述当前设计；各阶段问题、旧调用链、P0/P1/P2/P3 修复、V1/V3 对照与提交时间线见 [Pico V1 到 V3 的项目演进](docs/project-evolution.zh-CN.md)。

## Human-inspired 不是一条循环，而是一套系统设计

Pico 不声称复制人脑，也不保存或暴露模型的私有思维链。这里的 Human-inspired 指：从熟练程序员可观察的行为、软件工程实践和人机协作经验中抽取机制，再把机制转化为可测的 Runtime 状态。

它贯穿整个项目，而不只是一条“感知 → 判断 → 执行 → 反馈”流程。

| 人类/工程行为 | Pico V3 中的机制 | 代码落点 |
| --- | --- | --- |
| 开工前先确认机器、Shell、路径和构建工具 | 动态环境画像，不把 Unix/Windows 写死进 Prompt | `execution/`、runtime bootstrap |
| 围绕当前任务形成局部心智模型 | source working set、仓库图、上下文投影 | `context/`、`workspace/` |
| 注意力集中在会改变当前决定的信息 | work item、blocker、expected observation、evidence frontier | `domain/work_plan.py`、`progress/` |
| 获取信息后先消化，再决定继续查还是行动 | evidence assimilation 与 `decision_due` | `progress/progress_controller.py` |
| 对错误和异常结果更敏感 | 未消解失败进入高优先级反馈区 | progress ledger、feedback projection |
| 避免同一个失败重复发生 | 失败绑定代码修订，修复和重新验收后才退休 | verification state |
| 写一点、运行、解释、再调整 | diagnostic 与 mutation/verification 交替 | LangGraph action turn |
| 一次完成一个连贯的小工作单元 | 原子跨文件 `apply_patch` | `tools/`、`workspace/` |
| 工作记忆与长期经验不同 | Working Memory / Durable Memory 分层 | `memory/` |
| 人打断时通常有更紧急的新意图 | 当前请求优先于历史会话 | `domain/interaction_policy.py` |
| 临时要求不应自动改写长期习惯 | 本轮覆盖与长期偏好分离 | interaction policy、memory admission |
| “小一点”通常表示适度，不是极端最小化 | 比例原则 | interaction policy |
| 先跟随现有工程风格，再考虑个人偏好 | follow-repository / layer-first / feature-first | package layout policy |
| 正式交付前会复核改动、测试和范围 | Runtime delivery review | completion、transaction |
| 草稿、实验和正式代码不是同一个空间 | Transactional Shadow Workspace | `workspace/` |
| 长任务会阶段性告知同事正在做什么 | progress events、模型/工具耗时、重试提示 | `cli/`、runtime events |
| 大项目由人拆成可验收阶段 | 工作单元边界与分阶段集成 | task/work plan、delivery |

### 1. 环境定向：先知道自己在哪里

熟练程序员不会在不知道操作系统、Shell、仓库根、语言和构建工具的情况下随意执行命令。Pico 在 bootstrap 阶段动态生成环境画像，包括：

- 操作系统与 Shell 方言；
- 实际 workspace 与 Shadow 路径；
- Git/普通目录状态；
- Python、Java、Maven、Node 等可用工具及真实路径；
- 路径、编码和平台约束。

因此模型看到的是当前机器的事实，而不是写死的 Unix 示例。Windows `.cmd`、绝对路径、设备名、CRLF、BOM、长 Unicode 路径等由执行边界统一处理，不要求模型临场猜测。

### 2. 任务局部理解：不是把整个仓库塞进上下文

人类程序员通常只在脑中维护与当前问题相关的局部模型。Pico 的 `ContextManager` 根据当前任务投影：

- 用户目标、范围和验收要求；
- 初始 source working set；
- 当前工作项关联的源码、配置、测试和调用关系；
- 未消解失败与最近正式验收；
- 未验证修改和事务状态；
- Working Memory 与检索到的 Durable Memory；
- 已知事实、未决问题与下一步所需证据。

代码内容按“是否仍支撑当前判断”保留，而不是按“是否最近出现”保留。文件发生内部或外部变化后，旧读取证据会失效，旧摘要也不能继续冒充当前源码。

仓库定位采用逐层增强：

```text
文件树与文本搜索
  → Python AST / Java 语法与 import 关系
  → 配置、测试和构建结果
  → 可选 multilspy 定义/引用证据
```

LSP 是增强项而不是唯一真相。反射、依赖注入、动态调用和框架隐式关系仍需结合源码、配置与运行证据判断。

### 3. 目标导向注意力：每次探索都服务当前问题

`WorkPlan` 不是展示型待办列表，而是连接证据和行动的领域对象。活动工作项可以记录：

- `requirement`：这个工作单元必须满足什么；
- `hypothesis`：当前对实现或故障原因的判断；
- `blocker`：什么信息仍阻止行动；
- `expected_observation`：下一次读取或实验预计确认什么；
- `evidence_paths`：哪些文件或输出支撑判断；
- `candidate_action`：证据成立后准备做什么；
- `evidence_assessment`：新证据如何改变判断；
- `mutation_paths`：本工作项修改了什么；
- `status`：orienting、needs_evidence、decision_due、actionable、implemented、verified 或 repair。

这让 Runtime 能区分“必要探索”和“没有缩小问题空间的浏览”，而不是简单限制 `read_file` 次数。

### 4. 证据吸收：读到不等于理解并使用

V3 的关键机制是 evidence assimilation。当活动工作项取得新证据后，该 episode 进入 `decision_due`：

- 模型可以直接修改、运行验证或交付；
- 如果仍需探索，必须说明新证据意味着什么；
- 同时给出新的 blocker、expected observation 或 candidate action。

它不同于“读两次就封禁读取”：

- 没有读取次数魔法数字；
- 不强迫模型取得证据后立刻写代码；
- 不把任务硬切为只读/只写阶段；
- 允许有目的的冗余核验；
- 只阻止模型忽略刚得到的关键事实，机械扩大搜索面。

这个设计来自人类程序员“信息获得后更新当前判断”的行为，也吸收了工业 Agent 在 observation-to-action、stuck detection 和 tool boundary 上的经验，但避免把通用循环检测器当成任务理解本身。

### 5. 负反馈显著性：失败不是普通日志

人类对负反馈通常比普通成功输出更敏感。Pico 将失败单独建模，而不是让它与数十条文件内容平铺竞争 token：

- diagnostic failure 是定位信息；
- acceptance failure 是交付阻塞；
- 未解释失败保持在高优先级上下文中；
- 修改后旧失败不会凭空消失，而是等待新验证消解；
- 无关成功命令不能覆盖正式验收失败；
- 同类失败重复出现会保留完整因果链供下一轮修正。

这对应熟练程序员常见的 post-error adjustment：先理解刚才为何失败，避免带着未知错误继续扩张修改范围。

### 6. 小步行动与原子工作单元

“小步”不等于“极其小”或“一次只改一个文件”。Pico 的工作单元围绕一个可验证目标，可以同时修改：

```text
Controller
  + Service 接口
  + Service 实现
  + Mapper / Repository
  + DTO
  + 对应测试
```

`apply_patch` 将这些相互依赖的修改作为原子候选变更，避免中断后只留下半条调用链。代码组织默认跟随仓库现状；用户可以选择 `follow_repository`、`layer_first` 或 `feature_first`，因此人类偏好是显式输入，不是模型擅自决定。

### 7. 验证身份：测试通过必须对应当前代码

Pico 将验证绑定到代码修订、命令和工作项。一次正式验收会记录：

- 被验证的代码修订；
- 实际执行的命令；
- 退出状态与关键输出；
- 覆盖的工作项和产物；
- 此后是否发生新的修改。

若验证命令本身修改工作区，或验收后又修改代码，原结果立即变为 stale。提交端读取完整验证账本，而不是只看最近一条命令是否成功。这对应人类工程交付中的基本常识：刚才通过测试的版本必须就是准备提交的版本。

### 8. 工作记忆与长期记忆

Pico 区分不同时间尺度：

| 层级 | 保存内容 | 生命周期 |
| --- | --- | --- |
| 当前上下文 | 本轮工具观察和模型投影 | 单个模型轮次 |
| Working Memory | 当前任务摘要、最近文件、失败、工作项 | 当前 Session |
| Checkpoint | 图节点和可恢复执行位置 | 中断/恢复 |
| Session | 对话、任务与运行关联 | 跨进程继续 |
| Durable Memory | 经准入的工作区长期事实和用户偏好 | 跨 Session |

长期记忆经过“提取 → 准入 → 合并/冲突处理 → 持久化 → 按需检索”，不依赖 final answer 中某个特殊文本格式，也不保存整段聊天当作事实。当前用户要求优先于长期偏好；临时覆盖不会自动篡改长期约定。

### 9. 用户打断与意图层级

人在 Agent 运行中插入新消息，通常代表新的高优先级意图。Pico 的优先级是：

```text
当前明确请求
  > 当前任务约束
  > Session 临时状态
  > Workspace 偏好
  > Durable Memory
  > Runtime 推断
```

中断后恢复也不会把新请求与旧事务随意混合。解释类请求不会自动获得提交旧代码的授权；如果新要求明显否定历史约定，系统应把冲突呈现出来，而不是静默选择其中之一。

### 10. 比例原则与工程质量

V3 将真实使用中常见的不适纳入交互策略：

- 用户说“小一点”表示适度收敛，不表示极端最小实现；
- 用户要求功能时，不能只补一个烟雾路径而忽略 Controller → Service → Repository → Test 的既有工程链；
- 测试通过不是唯一质量标准，还要检查修改范围、调用方、重复逻辑、命名与已有抽象；
- 工具类和公共组件应服务真实复用点，也不为“看起来工程化”制造无意义层次；
- 简单任务少探索，复杂任务允许更多证据收集，但都必须说明下一步要消除什么不确定性。

### 11. 阶段性进度与协作透明度

长时间黑盒运行会让用户无法区分正常工作与卡死。Pico CLI 会报告当前阶段、工具步骤、目标文件、模型耗时、工具耗时、重试、恢复和事务状态。进度反馈不是装饰，它使人能够在错误方向扩大前及时中断和纠正。

### 12. 分阶段完成大型项目

成熟开发者通常不会一次完成几十个模块。Pico 把人的需求拆分权保留给人：

```text
人定义可独立运行的最小工作单元
  → Pico 完成、验证并提交
  → 人确认下一阶段边界
  → Pico 在已有事实和长期偏好上继续
```

这避免把“自主规划整个产品”错误当成 Coding Agent 可用性的前置条件，也让每一阶段都能回滚、复盘和独立验收。

## 三条贯穿式调用链

### 证据链

```text
用户目标与环境
  → 初始工作集和仓库证据
  → 带修订身份的读取/搜索/运行结果
  → evidence frontier
  → 工作项判断
  → 文件变化使旧证据过期
  → 新证据重新进入判断
```

### 反馈链

```text
候选实现
  → diagnostic / acceptance 命令
  → 预期与实际结果
  → 未消解失败
  → 修复动作
  → 当前修订重新验收
  → 失败退休或继续 repair
```

### 交付链

```text
源工作区
  → Transactional Shadow Workspace
  → 原子修改
  → 范围与保护文件检查
  → 当前修订正式验收
  → delivery review
  → Commit / Rollback
  → 销毁 Shadow 代码，仅保留审计元数据
```

## LangGraph 编排与状态权威

Pico 只有一条正式启动链：

```text
python -m pico / pico
  → pico/__main__.py
  → pico/cli/cli_runtime.py::main()
  → pico/runtime/pico_runtime.py::Pico
  → pico/runtime/agent_graph_runtime.py::AgentGraphRuntime
```

图节点为：

```text
START
  → bootstrap_runtime
  → action_turn ───────────────┐
       │ model requests tool   │
       └───────────────────────┘
       │ model requests finish
       ↓
    finalization
       ├─ incomplete / recoverable → action_turn
       ├─ valid and deliverable    → delivery
       └─ failed / interrupted     → stop
       ↓
      END
```

LangGraph 负责阶段编排，不维护第二份业务真相：

| 状态 | 权威组件 |
| --- | --- |
| 用户意图、模式、范围、风格偏好 | `InteractionPolicy` |
| 工作项、假设、阻碍、evidence episode | `WorkPlan` / `ProgressController` |
| 模型当前可见内容 | `ContextManager` |
| 文件内容与修订 | Workspace documents / repository intelligence |
| 失败与验证 | Progress ledger / verification state |
| 修改、回滚与提交 | Transactional workspace |
| Provider 响应完整性 | Provider outcome contract |
| Session、Checkpoint、Run | Persistence stores |
| 长期事实 | Durable memory store |

这避免 LangGraph Checkpointer、Pico Session 和 TSW 同时成为三套状态机。

## Transactional Shadow Workspace

所有候选修改先进入 Shadow：

```text
源仓库 → Shadow 中编辑和测试 → patch/review → 无冲突提交 → 源仓库
```

TSW 支持 Git 和普通目录后端，处理部分成功、中断、冲突、回滚、恢复与提交后的清理。Commit 成功后立即销毁 Shadow 代码，只保留必要审计元数据。

TSW 是代码事务边界，不是恶意进程安全沙箱。Shell 仍在宿主执行；进程、网络和系统级隔离由 Pico 外层 Docker 或虚拟机负责。部署时只需隔离整个 Runtime，无需每次工具调用启动嵌套 Docker：

```powershell
docker build -f docker/Dockerfile.runtime -t pico-runtime:3 .
docker run --rm -it --env-file .env `
  -v "${PWD}:/workspace" `
  -v pico-state:/home/pico/.local/state/pico `
  pico-runtime:3
```

## Provider 完整性与动态资源分配

V3 不再采用固定 512-token finalization，也不无界重放所有历史。执行策略根据本轮目的动态分配：

- 定向读取使用较小输出目标；
- 代码生成、修复和复杂解释获得更大空间；
- 旧工具历史压缩为带来源的工程事实；
- 当前失败、修改和验证状态优先保留；
- final 截断进入受控恢复轮次；
- Provider 最大上下文/输出是能力边界，不是每轮固定配额。

OpenAI-compatible Responses、Anthropic-compatible Messages、DeepSeek-compatible 与 Ollama 都通过统一 outcome contract 进入 Runtime。系统区分：

```text
completed
incomplete
truncated
transport_failed
protocol_failed
```

空文本、reasoning-only、截断工具协议、非法 JSON、SSE 中断和 socket timeout 都不能静默穿透完成判定。`--model-execution-policy adaptive|fast|deep` 控制每轮策略，显式 `--openai-reasoning-effort` 具有更高优先级。

## 包结构

`pico/` 根目录只保留包入口，内部按责任分区：

```text
pico/
├─ __init__.py
├─ __main__.py
├─ cli/          # 参数、.env、终端和进度输出
├─ runtime/      # Pico 总装、LangGraph、生命周期和完成判定
├─ domain/       # TaskState、WorkPlan、InteractionPolicy、模型合约
├─ context/      # 上下文管理、工作集和证据投影
├─ memory/       # Working/Durable Memory、准入和检索
├─ progress/     # 证据账本、work_focus、反馈和验证语义
├─ tools/        # 工具注册、schema、校验、执行和内置工具
├─ execution/    # Shell/process 边界、环境画像和执行策略
├─ workspace/    # TSW、文档、仓库图和 LSP 增强
├─ providers/    # Provider clients、完整性和错误映射
├─ persistence/  # Session、Run、Checkpoint、state root 和锁
├─ security/     # 路径边界、审批和密钥脱敏
├─ evaluation/   # Harness、真实任务指标和报告
└─ utils/        # 无领域状态的路径、文本和时间辅助函数
```

详细边界见 [LangGraph Runtime V3](docs/architecture/langgraph-runtime-v3.md)。V2 稳定实现保留在 `pico-v2-stable` 分支；V3 不在主干中并存两套主循环，而是用回归测试保证语义迁移。

## 已实现能力

- Python、Java、前端仓库的分析与真实代码修改；
- 单文件功能、跨文件调用链、Bug 修复和失败后继续修正；
- Python AST、Java 语法/import 仓库图与可选 LSP；
- 精确 patch 与原子跨文件 patch；
- Shell 诊断、测试、构建和验收；
- Windows/Linux 环境画像与命令适配；
- Git 工作区、普通目录、用户已有修改和外部漂移保护；
- TSW 创建、验证、Commit、Rollback、恢复和清理；
- Working/Durable Memory、Session、Checkpoint 和 Resume；
- Provider 完整性、截断恢复和统一错误语义；
- Prompt、Trace、Report、Session 中的密钥脱敏；
- 阶段、步骤、模型/工具耗时、重试和事务进度输出；
- 确定性 Harness 和真实仓库评测指标。

## 明确边界

Pico V3 当前不声称：

- 无人干预地一次生成完整大型产品；
- 已达到 Claude Code、Codex 等商业产品的通用成功率；
- 仓库图能完整解析反射、动态调用和所有框架隐式语义；
- TSW 能抵御恶意 Shell 或替代容器/虚拟机；
- 本地真实任务结果等同于 SWE-bench 成绩；
- 任意 Provider、网络和采样下都得到完全相同结果；
- 一次成功案例足以证明长期稳定性。

更准确的定位是：**具备真实跨文件编码、失败恢复和受控交付能力的学生级工程研究项目；核心机制已有实证支持，但仍需更大规模、可公开复现的统计评测。**

## 测试结果

### 自动化回归

当前稳定提交：

```text
pytest: 239 passed, 1 skipped
ruff:  All checks passed
CLI:   python -m pico --help passed
```

测试覆盖：

- 工具路径、审批、只读和密钥边界；
- Provider 空响应、截断、非法协议、超时和恢复；
- evidence frontier、失败账本和验证过期；
- 修改后重新读取、外部漂移和旧上下文失效；
- Session 损坏、Checkpoint、Resume 和事务恢复；
- Shadow 提交、冲突、回滚、部分成功和清理；
- Windows Shell、编码、BOM/GBK、Unicode 路径与时区；
- LangGraph 节点路由和 V2 行为语义回归；
- Working/Durable Memory 的准入、检索和冲突优先级。

CI 在 Windows、Linux 与 Python 3.10/3.12 上运行 CLI smoke、Ruff 和完整 pytest，不调用真实模型，也不读取本地 `.env`。

### 12 题确定性 Harness

```powershell
python scripts/run_harness_regression.py
```

本次稳定版本实测：

```text
任务通过：      12/12
独立 verifier： 12/12
预算内完成：    12/12
```

这组测试不需要 API Key，用于稳定复现工具边界、恢复、漂移、记忆和事务机制。它验证 Runtime，不应被解释成真实模型编码成功率。

### Pico V1 / V2 同任务基线

仓库保留两个固定 Java 任务、同一 Qwen3.8-Flash、`max_steps=24` 下的机器可读对照：

| 版本 | 通过 | 平均工具步骤 | 平均耗时 | 生产代码修改 |
| --- | ---: | ---: | ---: | --- |
| 原始 Pico V1 | 0/2 | 24 | 387.703 s | 0 个任务产生有效修改 |
| Pico V2 `c6d87bd` | 2/2 | 13 | 115.515 s | 均修改并通过独立 Maven 验证 |

报告与 JSON 位于 [benchmarks/results/v1-v2-java-2026-09-26](benchmarks/results/v1-v2-java-2026-09-26/)。小样本证明新架构修复了已复现的 V1 失败链，不代表通用排行榜成绩。

### Pico V3 真实仓库验收

以下任务在隔离的历史项目副本中使用 Qwen3.8-Flash 执行；测试副本在验收后按要求删除。它们是本地工程验收记录，不是公开 benchmark：

| 任务 | 结果 | 关键观察 |
| --- | --- | --- |
| Python 并发原子保存 | 通过 | 10 个工具步骤，独立 pytest 通过 |
| Java 跨文件角色搜索 | 中断恢复后通过 | 修改 6 个文件，独立 Maven 通过 |
| Java/JUnit 小型功能 | 首次通过 | 6 个工具步骤，范围符合要求 |
| Vue 三文件工具提取 | 通过 | 第 8 步编辑，第 10 步构建通过，第 14 步结束 |
| LeetCode 239 | 恢复后通过 | 算法、JUnit 和 Maven 验证完成 |

Java 统计验收使用 8 个任务、每题重复 3 次，共 24 个运行槽位：

- 初始批量首轮成功：**23/24**；
- 唯一失败槽位完成调用链修复并重跑通过；
- 缺陷闭环：**24/24 个槽位均已有成功证据**；
- 当前代码尚未重新执行一轮全新 24 槽位 pass@1，因此不把闭环结果误写成新的 24/24 首轮成功率。

最困难的 Java 跨文件任务历史上曾第 25 步才首次写入、约 12 分钟仍未完成。当前机制的一次运行在第 10 步首次修改；人工中断后恢复，累计约 17 个工具步骤完成提交，外部 `mvn -q test` 验证 16 个 Java 测试全部通过。另一次运行在第 11 步写入、第 12 步测试、第 15 步完成，约 252 秒。

这些结果支持：

1. 相比 V1，局部工作集、负反馈显著性、证据吸收和修订绑定验证提高了跨文件任务的可完成性与收敛速度。
2. V3 已证明能完成中等复杂工作单元，但尚未用足够大的公开样本证明任意任务都能稳定完成。

真实任务报告应拆分 Provider、工具、依赖准备、构建、Runtime 本地开销、总调用、token、重试和成本。Maven 冷缓存与虚拟网络延迟不能隐藏，也不能误判成 Agent 决策时间。评测规范见 [Real-repository coding benchmark](docs/repository-coding-benchmark.md)。

## 安装

需要 Python 3.10+。推荐 Conda：

```powershell
conda env create -f environment.yml
conda activate pico
python -m pico --help
```

不初始化 PowerShell 也可以：

```powershell
conda run -n pico python -m pico --help
```

如果环境按 prefix 创建（例如本机位于 `E:\conda-envs\pico`，在 `conda env list` 中名称栏为空），应使用 `-p` 而不是 `-n`：

```powershell
conda run -p E:\conda-envs\pico python -m pico --help
```

安装可选 LSP：

```powershell
python -m pip install -e ".[lsp]"
```

## Provider 配置

```powershell
Copy-Item .env.example .env
```

真实密钥只写入本地 `.env`；该文件已被 Git 忽略。不要把密钥写进 README、命令历史、测试样本或提交。

配置优先级：

```text
显式 CLI 参数
  > 启动 Pico 的目录中的 .env
  > --cwd 目标工作区中的 .env
  > 对应旧环境变量
```

启动目录 `.env` 具有更高优先级，防止目标仓库残留配置把 Pico 切换到错误 Provider。若没有明确配置 `--provider` 或 `PICO_PROVIDER`，系统会在网络请求前给出受控错误，不会隐式选择云模型。

OpenAI-compatible Responses 示例：

```dotenv
PICO_PROVIDER=openai
PICO_OPENAI_API_BASE=https://<WorkspaceId>.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
PICO_OPENAI_API_KEY=your-api-key
PICO_OPENAI_MODEL=qwen3.8-flash
```

其他 Provider：

| provider | Base URL | API Key | Model |
| --- | --- | --- | --- |
| `openai` | `PICO_OPENAI_API_BASE` | `PICO_OPENAI_API_KEY` | `PICO_OPENAI_MODEL` |
| `anthropic` | `PICO_ANTHROPIC_API_BASE` | `PICO_ANTHROPIC_API_KEY` | `PICO_ANTHROPIC_MODEL` |
| `deepseek` | `PICO_DEEPSEEK_API_BASE` | `PICO_DEEPSEEK_API_KEY` | `PICO_DEEPSEEK_MODEL` |
| `ollama` | `--host`，默认 `http://127.0.0.1:11434` | 不需要 | `--model`，默认 `qwen3.5:4b` |

额外敏感变量可通过 `PICO_SECRET_ENV_NAMES` 或 `--secret-env-name NAME` 加入脱敏范围。

## 使用

```powershell
# 当前仓库 REPL
pico

# 指定工作区
pico --cwd E:\path\to\repository

# one-shot
pico "修复当前失败测试，只修改相关实现并运行验收。"

# 恢复最近会话
pico --resume latest

# 关闭可选 LSP
pico --semantic-index off

# CI/benchmark 自动提交已验证 Shadow 修改
pico --commit-policy auto "实现需求并运行测试"
```

REPL 命令：

- `/help`：显示命令；
- `/memory`：查看工作记忆；
- `/session`：显示 Session 文件；
- `/reset`：重置当前会话；
- `/exit` 或 `/quit`：退出。

全部参数以 `pico --help` 为准。

### 界面示例

CLI 参数：

![pico help](assets/screenshots/pico-help.png)

启动与阶段进度：

![pico start](assets/screenshots/pico-start.png)

REPL 与 Session：

![pico repl](assets/screenshots/pico-repl.png)

## 状态、审计与安全

Windows 默认 state root 是 `%LOCALAPPDATA%\Pico`；Linux/macOS 使用 `$XDG_STATE_HOME/pico`，未设置时为 `~/.local/state/pico`。可用 `PICO_STATE_ROOT` 覆盖。

```text
<state-root>/workspaces/<workspace-id>/
├─ sessions/
└─ runs/<run_id>/
   ├─ task_state.json
   ├─ trace.jsonl
   └─ report.json
```

运行状态不会写入用户仓库的 `.pico/`。工具受 `--approval ask|auto|never`、只读模式、路径 containment、保护文件和密钥脱敏约束。Shell 属于可信本地边界；未知代码应在外层容器或虚拟机中运行。

## 开发与复现

```powershell
python -m pytest tests -q
python -m ruff check pico tests scripts
python -m pico --help
python scripts/run_harness_regression.py
```

主要文档：

- [Human-Inspired Programming Loop](docs/architecture/human-inspired-programming-loop.md)
- [LangGraph Runtime V3](docs/architecture/langgraph-runtime-v3.md)
- [Pico V1 到 V3 的项目演进](docs/project-evolution.zh-CN.md)
- [Pico V1 architecture baseline](docs/architecture/agent-harness-v1-overview.md)
- [Real-repository coding benchmark](docs/repository-coding-benchmark.md)
- [Runtime invariants ADR](docs/decisions/0001-runtime-governs-invariants-not-model-strategy.md)
- [Evidence assimilation ADR](docs/decisions/0002-evidence-assimilation-at-action-boundary.md)

## 工业实践与研究启发

Pico 综合而不是照搬以下思路：

- **LangGraph**：显式图编排与可观测节点，但领域状态仍由 Pico 管理；
- **SWE-bench**：FAIL_TO_PASS、PASS_TO_PASS、仓库修订和独立验证的评测原则；
- **mini-SWE-agent**：小而完整的模型—工具循环与可回放轨迹；
- **Cline / Roo Code**：确定性工具签名和在 tool boundary 阻止无价值重复；
- **OpenHands**：action/observation、失败和停滞检测需要共同考虑；
- **LSP 与 repository graph**：用结构化代码证据减少纯文本盲搜；
- **数据库事务与 Git 工作区**：Shadow、验证、冲突检查、Commit/Rollback；
- **认知负荷与程序理解研究**：工作记忆有限，任务局部模型比无界历史更可用；
- **程序员调试研究**：开发是理解、假设、实验、编辑和验证的交替过程；
- **错误监控与 post-error adjustment 研究**：负反馈应提高后续注意力并改变行动；
- **分层记忆研究**：短期工作状态与跨任务长期事实应分开存储和检索；
- **Human-in-the-loop 系统**：中断、偏好、范围和阶段拆分属于用户控制面。

这些来源提供设计启发，不自动证明实现有效。Pico 将其转为可证伪假设：

- **H1**：任务局部工作集减少无关读取和上下文膨胀；
- **H2**：未消解失败保持显著，减少同类失败重复；
- **H3**：证据取得后要求吸收或行动，缩短首次有效修改前的探索；
- **H4**：验证绑定代码修订，降低未验证代码被错误交付的概率；
- **H5**：原子工作单元与 TSW 减少跨文件 partial success；
- **H6**：当前意图优先和记忆分层减少长记忆干扰；
- **H7**：动态环境画像减少跨平台命令试错；
- **H8**：阶段进度和用户中断优先改善长任务可控性。

评测应记录 pass@1、重复运行稳定率、首次有效修改、无效工具比例、失败重复率、验证覆盖、总耗时、Provider/工具时间、token、成本和最终 patch，而不是只报告“最终成功”。研究来源、论文链接与架构映射详见 Human-Inspired Programming Loop 文档。

## 后续工作

Pico V3 接下来优先补足证据，而不是继续扩张功能：

1. 固定 Python、Java、前端和算法任务集，公开 pass@1、方差、轨迹和成本；
2. 对 working set、失败显著性、evidence assimilation 和动态环境画像做消融；
3. 用同一模型与原始 V1、简化 Agent baseline 做对照；
4. 继续拆分大型聚合模块，但不破坏状态所有权；
5. 完善 Linux/Windows 真实模型任务的可复现环境；
6. 提供诚实、可选的外层沙箱部署模板。

Pico V3 已经从“LLM 加几个工具”的演示 Harness，演进为能够在真实仓库中组织证据、执行跨文件修改、响应失败、恢复任务并受控交付的 Coding Agent Runtime。它的核心创新不是某一条规则，而是把熟练程序员的环境定向、目标注意、局部理解、证据吸收、错误修正、分层记忆、协作偏好和交付复核，统一成可审计、可测试的运行时系统。
